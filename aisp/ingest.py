"""CSV 预检、字段映射与分块导入。

流程：预检（编码/分隔符/表头/样本/类型推断）→ 字段映射 → 类型转换 → 写入 STAGING Parquet。
本模块有意不清理数据，只做格式标准化与坏行隔离，治理逻辑见 clean.py。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from . import store
from .config import settings
from .schema import DATA_DICTIONARY, FIELD_MAPPING

log = logging.getLogger(__name__)

SAMPLE_BYTES = 512 * 1024
ENCODINGS = ("utf-8-sig", "utf-8", "latin-1", "cp1252")

# 常见 AIS 时间格式，按优先级尝试（无法确认时必须由用户覆盖）
TIME_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)


# --------------------------------------------------------------------------- 预检

def detect_encoding(path: Path) -> str:
    buf = path.open("rb").read(SAMPLE_BYTES)
    for enc in ENCODINGS:
        try:
            buf.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


_DUCKDB_ENCODING = {
    "utf-8-sig": "utf-8",
    "utf-8": "utf-8",
    "latin-1": "latin-1",
    "cp1252": "latin-1",
}


def duckdb_encoding(enc: str) -> str:
    """DuckDB read_csv 不识别 'utf-8-sig'，其 utf-8 读取会自动去掉 BOM。"""
    return _DUCKDB_ENCODING.get(enc, "utf-8")


def _read_head(path: Path, encoding: str, n: int = 5) -> List[List[str]]:
    with path.open("r", encoding=encoding, errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        rows = []
        for i, row in enumerate(reader):
            rows.append(row)
            if i + 1 >= n:
                break
        return rows


def detect_delimiter(head: List[List[str]]) -> str:
    if not head or not head[0]:
        return ","
    best, best_score = ",", -1
    for cand in (",", ";", "\t", "|"):
        counts = {r.count(cand) for r in head if r}
        if len(counts) == 1 and head[0].count(cand) >= 1 and len(head[0]) > best_score:
            best, best_score = cand, len(head[0])
    return best


def detect_time_format(series: pd.Series) -> Optional[str]:
    sample = series.dropna().astype(str).head(500)
    for fmt in TIME_FORMATS:
        parsed = pd.to_datetime(sample, format=fmt, errors="coerce")
        if parsed.notna().mean() > 0.9:
            return fmt
    return None


def preflight(path: Path | str) -> Dict[str, Any]:
    """返回文件预检画像：编码、分隔符、表头、样本、类型推断、缺失率。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    encoding = detect_encoding(p)
    head = _read_head(p, encoding, 5)
    delimiter = detect_delimiter(head)
    columns = [c.strip() for c in head[0]] if head else []
    sample = pd.read_csv(p, encoding=encoding, sep=delimiter, nrows=2000, dtype=str, low_memory=False)
    sample.columns = [c.strip() for c in sample.columns]
    mapping = {c: FIELD_MAPPING.get(c, f"extra_{i}") for i, c in enumerate(sample.columns)}
    missing = {c: round(float(sample[c].isna().mean()), 4) for c in sample.columns}
    inferred: Dict[str, str] = {}
    for c in sample.columns:
        s = sample[c].dropna().astype(str).str.strip()
        s = s[(s != "") & (s.str.lower() != "nan")]
        if s.empty:
            inferred[c] = "empty"
            continue
        numeric = pd.to_numeric(s, errors="coerce")
        if numeric.notna().mean() > 0.95:
            inferred[c] = "number"
        elif detect_time_format(s) is not None:
            inferred[c] = "datetime"
        else:
            inferred[c] = "text"
    time_col = next((src for src, std in mapping.items() if std == "event_time"), None)
    time_fmt = detect_time_format(sample[time_col]) if time_col and time_col in sample.columns else None
    return {
        "path": str(p),
        "file_size": p.stat().st_size,
        "encoding": encoding,
        "delimiter": delimiter,
        "columns": list(sample.columns),
        "mapped_columns": mapping,
        "inferred_types": inferred,
        "missing_ratio": missing,
        "time_format": time_fmt,
        "sample_rows": json.loads(sample.head(5).to_json(orient="records", force_ascii=False)),
        "header_ok": _header_ok(list(sample.columns)),
        "sha256": file_sha256(p),
    }


def _header_ok(columns: Sequence[str]) -> bool:
    return len([c for c in columns if c in FIELD_MAPPING]) >= 4


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def estimate_rows(path: Path | str) -> int:
    with store.cursor() as con:
        return int(con.execute("SELECT COUNT(*) FROM read_csv_auto(?, header=true)", [str(path)]).fetchone()[0])


# --------------------------------------------------------------------------- 导入

def _time_expr(col: str, time_format: Optional[str]) -> str:
    formats = ([time_format] if time_format else []) + [f for f in TIME_FORMATS if f != time_format]
    parts = [f"try_strptime({col}, '{f}')" for f in formats]
    return "coalesce(" + ", ".join(parts) + ")"


def _std_expr(std: str, col: str) -> str:
    if not col:
        return "NULL"
    src = f'"{col}"'
    if std == "mmsi":
        return f"nullif(trim(CAST({src} AS VARCHAR)), '')"
    if std in ("lat", "lon", "sog", "cog", "heading", "rot", "draught", "length", "width"):
        return f"try_cast({src} AS DOUBLE)"
    # 文本字段：把供应商占位值 Unknown/Undefined、空串统一归为 NULL，避免污染统计
    return (
        f"nullif(nullif(nullif(trim(CAST({src} AS VARCHAR)), ''), 'Unknown'), 'Undefined')"
    )


def ingest_csv(
    paths: Sequence[Path | str],
    source_name: str,
    mapping_override: Optional[Dict[str, str]] = None,
    created_by: str = "system",
    note: str = "",
    time_format: Optional[str] = None,
) -> Dict[str, Any]:
    """导入一或多个 CSV，生成一个批次，并写出 STAGING Parquet。"""
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("未提供 CSV 文件")
    profiles = [preflight(p) for p in paths]
    first = profiles[0]
    encoding, delimiter = duckdb_encoding(first["encoding"]), first["delimiter"]
    mapping = dict(first["mapped_columns"])
    if mapping_override:
        mapping.update(mapping_override)
    mapped = {v: k for k, v in mapping.items()}
    fmt = time_format or first["time_format"]

    for std in ("mmsi", "event_time", "lat", "lon"):
        if std not in mapped:
            raise ValueError(f"缺少必需标准字段映射：{std}")

    batch_id = store.new_id("batch")
    staging = settings.staging_root / batch_id
    staging.mkdir(parents=True, exist_ok=True)
    source_hash = first["sha256"][:12]

    def col(std: str) -> str:
        c = mapped.get(std)
        return c if c else ""

    select_cols = [
        ("mmsi", "mmsi"),
        ("event_time", "event_time"),
        ("lon", "lon"),
        ("lat", "lat"),
        ("sog", "sog"),
        ("cog", "cog"),
        ("heading", "heading"),
        ("rot", "rot"),
        ("nav_status", "nav_status"),
        ("ship_type", "ship_type"),
        ("cargo_type", "cargo_type"),
        ("draught", "draught"),
        ("length", "length"),
        ("width", "width"),
        ("imo", "imo"),
        ("callsign", "callsign"),
        ("name", "name"),
        ("destination", "destination"),
        ("mobile_type", "mobile_type"),
    ]
    exprs = []
    for std, alias in select_cols:
        if std == "event_time":
            exprs.append(f"{_time_expr(chr(34) + col(std) + chr(34), fmt) if col(std) else 'NULL'} AS event_time")
        else:
            exprs.append(f"{_std_expr(std, col(std))} AS {alias}")

    colspec = ", ".join(f"'{c}': 'VARCHAR'" for c in first["columns"])
    total_rows = valid_rows = 0
    with store.cursor() as con:
        for p in paths:
            con.execute("DROP TABLE IF EXISTS _stage")
            con.execute(
                f"""
                CREATE OR REPLACE TABLE _stage AS
                SELECT
                    {', '.join(exprs)},
                    CAST(row_number() OVER () AS BIGINT) AS row_no,
                    '{source_hash}' AS source_id,
                    '{p.name}' AS source_file,
                    '{batch_id}' AS batch_id
                FROM read_csv(?, header=true, delim='{delimiter}', quote='"', escape='"',
                              encoding='{encoding}', ignore_errors=true, null_padding=true,
                              columns={{ {colspec} }}, sample_size=-1, parallel=true)
                """,
                [str(p)],
            )
            total_rows += int(con.execute("SELECT COUNT(*) FROM _stage").fetchone()[0])
            valid_rows += int(
                con.execute(
                    "SELECT COUNT(*) FROM _stage WHERE event_time IS NOT NULL AND mmsi IS NOT NULL "
                    "AND lat IS NOT NULL AND lon IS NOT NULL"
                ).fetchone()[0]
            )
            out = staging / f"part-{len(list(staging.glob('part-*.parquet'))):05d}.parquet"
            con.execute(
                f"COPY (SELECT * FROM _stage) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
            )
        con.execute("DROP TABLE IF EXISTS _stage")

    profile = staging_profile(batch_id)

    store.exec_sql(
        "INSERT INTO data_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            batch_id, source_name, str(paths[0].parent), first["sha256"], first["file_size"],
            total_rows, valid_rows, "staged", "", store._now(), created_by, note,
            json.dumps(profile, ensure_ascii=False, default=str),
        ],
    )
    for src, std in mapping.items():
        store.exec_sql(
            "INSERT OR REPLACE INTO data_field_mapping VALUES (?,?,?,?,?,?)",
            [batch_id, src, std, DATA_DICTIONARY.get(std, {}).get("dtype", "text"),
             DATA_DICTIONARY.get(std, {}).get("unit", "-"), "UTC"],
        )
    store.audit("ingest", f"batch:{batch_id}", f"导入 {len(paths)} 个文件，{total_rows} 行")

    return {
        "batch_id": batch_id,
        "row_count": total_rows,
        "valid_row_count": valid_rows,
        "staging_dir": str(staging),
        "profile": profile,
        "field_mapping": mapping,
        "time_format": fmt,
        "files": [p["path"] for p in profiles],
    }


def staging_profile(batch_id: str) -> Dict[str, Any]:
    glob = batch_points_glob(batch_id)
    with store.cursor() as con:
        row = con.execute(
            f"""SELECT COUNT(*) n, COUNT(DISTINCT mmsi) m,
                       min(event_time) t0, max(event_time) t1,
                       min(lat) minlat, max(lat) maxlat, min(lon) minlon, max(lon) maxlon,
                       avg(CASE WHEN event_time IS NULL THEN 1 ELSE 0 END) bad_time,
                       avg(CASE WHEN lon IS NULL OR lat IS NULL THEN 1 ELSE 0 END) bad_pos,
                       median(sog) med_sog, quantile_cont(sog, 0.99) p99_sog,
                       count(DISTINCT ship_type) n_ship_type
                FROM read_parquet('{glob}')"""
        ).fetchdf()
    d = row.iloc[0].to_dict()
    out = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif v is None or (isinstance(v, float) and pd.isna(v)):
            out[k] = None
        elif isinstance(v, float):
            out[k] = round(v, 4)
        else:
            out[k] = v
    return out


def batch_points_glob(batch_id: str) -> str:
    return (settings.staging_root / batch_id / "*.parquet").as_posix()
