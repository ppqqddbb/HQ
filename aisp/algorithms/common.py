"""公共查询与结果写盘工具。

所有算法用同一套过滤条件（时间范围 / 区域 bbox / 船型 / MMSI），
保证前端切换筛选条件时口径一致。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pandas as pd

from .. import store
from ..config import settings


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, np.integer, np.floating)):
        if isinstance(v, float) and not math.isfinite(v):
            return "NULL"
        return repr(float(v)) if isinstance(v, float) else str(int(v))
    return "'" + str(v).replace("'", "''") + "'"


class Filter:
    """统一的业务过滤条件。"""

    def __init__(self, filters: Optional[Dict[str, Any]] = None):
        f = dict(filters or {})
        tr = f.get("time_range") or []
        self.t0: Optional[str] = tr[0] if len(tr) > 0 and tr[0] else None
        self.t1: Optional[str] = tr[1] if len(tr) > 1 and tr[1] else None
        self.bbox = f.get("bbox")  # [min_lon, min_lat, max_lon, max_lat]
        self.ship_types: List[str] = list(f.get("ship_types") or [])
        self.mmsi: List[str] = [str(m) for m in (f.get("mmsi") or [])]
        self.drop_short = bool(f.get("drop_short_segments", False))
        self.exclude_flagged = bool(f.get("exclude_flagged", False))

    def where(self, alias: str = "") -> str:
        p = f"{alias}." if alias else ""
        conds: List[str] = []
        if self.t0:
            conds.append(f"{p}event_time >= CAST({_sql_literal(self.t0)} AS TIMESTAMP)")
        if self.t1:
            conds.append(f"{p}event_time < CAST({_sql_literal(self.t1)} AS TIMESTAMP)")
        if self.bbox and len(self.bbox) == 4:
            min_lon, min_lat, max_lon, max_lat = [float(x) for x in self.bbox]
            conds.append(
                f"{p}lon BETWEEN {min_lon!r} AND {max_lon!r} AND {p}lat BETWEEN {min_lat!r} AND {max_lat!r}"
            )
        if self.ship_types:
            lst = ",".join(_sql_literal(s) for s in self.ship_types)
            conds.append(f"{p}ship_type IN ({lst})")
        if self.mmsi:
            lst = ",".join(_sql_literal(m) for m in self.mmsi)
            conds.append(f"{p}mmsi IN ({lst})")
        if self.drop_short:
            conds.append(f"coalesce({p}short_segment, FALSE) = FALSE")
        if self.exclude_flagged:
            conds.append(f"coalesce({p}quality_flags, '') = ''")
        return " AND ".join(conds) if conds else "TRUE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time_range": [self.t0, self.t1],
            "bbox": self.bbox,
            "ship_types": self.ship_types,
            "mmsi": self.mmsi,
            "drop_short_segments": self.drop_short,
            "exclude_flagged": self.exclude_flagged,
        }


def open_dataset(dataset_version: str) -> duckdb.DuckDBPyConnection:
    """打开只读数据集连接（内存实例 + Parquet 视图，不占用元数据库写锁）。"""
    con = store.connect_memory()
    store.register_costals(con, dataset_version)
    return con


def dataset_extent(con, flt: Filter) -> Optional[Tuple[float, float, float, float]]:
    row = con.execute(
        f"""SELECT min(lon), min(lat), max(lon), max(lat), count(*) FROM clean_points WHERE {flt.where()}"""
    ).fetchone()
    if not row or row[4] == 0 or row[0] is None:
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


def dataset_range(con) -> Dict[str, Any]:
    row = con.execute(
        "SELECT min(event_time), max(event_time), count(*), count(DISTINCT mmsi) FROM clean_points"
    ).fetchone()
    return {
        "t0": str(row[0]) if row[0] else None,
        "t1": str(row[1]) if row[1] else None,
        "n_points": int(row[2] or 0),
        "n_mmsi": int(row[3] or 0),
    }


def write_result(task_id: str, payload: Dict[str, Any], files: Optional[Dict[str, Any]] = None) -> str:
    """把结果写入 artifacts/results/<task_id>/ 并返回目录。"""
    d = settings.results_root / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    for name, obj in (files or {}).items():
        p = d / name
        if isinstance(obj, dict):
            p.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
        elif isinstance(obj, pd.DataFrame):
            obj.to_parquet(p, index=False)
        elif isinstance(obj, (list, tuple)):
            p.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
        else:
            p.write_text(str(obj), encoding="utf-8")
    return str(d)


def downsample(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    """确定性等距抽样，保证同一输入重复运行结果一致。"""
    if len(df) <= limit:
        return df
    idx = (np.arange(limit) * (len(df) / float(limit))).astype(int)
    return df.iloc[idx].reset_index(drop=True)


def jsonable(obj: Any) -> Any:
    """把 numpy / pandas / datetime 递归转为 JSON 友好结构。"""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return None if not math.isfinite(v) else round(v, 6)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else round(obj, 6)
    if isinstance(obj, pd.DataFrame):
        return jsonable(obj.to_dict(orient="records"))
    try:
        return obj.item()
    except Exception:
        return str(obj)
