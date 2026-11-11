"""DuckDB / Parquet 访问层。

- 元数据（批次、任务、结果、网格版本、模型注册、审计）存放在 artifacts/meta.duckdb；
- CLEAN 明细按 dataset_version + event_date 分区写入 Parquet；
- 分析任务通过视图读取 Parquet，避免把全量明细常驻内存。
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import duckdb
import pandas as pd

from .config import settings

_LOCK = threading.RLock()

META_DDL = [
    """
    CREATE TABLE IF NOT EXISTS data_batch (
        batch_id          VARCHAR PRIMARY KEY,
        source_name       VARCHAR,
        source_path       VARCHAR,
        file_hash         VARCHAR,
        file_size         BIGINT,
        row_count         BIGINT,
        valid_row_count   BIGINT,
        status            VARCHAR,
        dataset_version   VARCHAR,
        created_at        TIMESTAMP,
        created_by        VARCHAR,
        note              VARCHAR,
        profile_json      VARCHAR
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS data_field_mapping (
        batch_id      VARCHAR,
        source_column VARCHAR,
        target_column VARCHAR,
        dtype         VARCHAR,
        unit          VARCHAR,
        timezone      VARCHAR,
        PRIMARY KEY (batch_id, source_column)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_task (
        task_id         VARCHAR PRIMARY KEY,
        algorithm       VARCHAR,
        dataset_version VARCHAR,
        config_json     VARCHAR,
        filters_json    VARCHAR,
        status          VARCHAR,
        progress        DOUBLE,
        current_stage   VARCHAR,
        queue           VARCHAR,
        created_by      VARCHAR,
        created_at      TIMESTAMP,
        started_at      TIMESTAMP,
        finished_at     TIMESTAMP,
        result_uri      VARCHAR,
        error_code      VARCHAR,
        error_message   VARCHAR,
        cancel_request  BOOLEAN,
        metrics_json    VARCHAR
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_result (
        result_id     VARCHAR PRIMARY KEY,
        task_id       VARCHAR,
        result_type   VARCHAR,
        object_key    VARCHAR,
        summary_json  VARCHAR,
        created_at    TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS grid_definition (
        grid_version  VARCHAR PRIMARY KEY,
        extent_json   VARCHAR,
        crs           VARCHAR,
        rows          INTEGER,
        cols          INTEGER,
        cell_size_m   DOUBLE,
        mode          VARCHAR,
        geometry_json VARCHAR,
        created_at    TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS model_registry (
        model_id      VARCHAR,
        version       VARCHAR,
        algorithm     VARCHAR,
        artifact_uri  VARCHAR,
        feature_version VARCHAR,
        metrics_json  VARCHAR,
        created_at    TIMESTAMP,
        PRIMARY KEY (model_id, version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        audit_id   VARCHAR PRIMARY KEY,
        user_id    VARCHAR,
        action     VARCHAR,
        resource   VARCHAR,
        detail     VARCHAR,
        ip         VARCHAR,
        result     VARCHAR,
        event_time TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS config_version (
        config_version VARCHAR PRIMARY KEY,
        module         VARCHAR,
        params_json    VARCHAR,
        created_at     TIMESTAMP,
        created_by     VARCHAR
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dataset_version (
        dataset_version VARCHAR PRIMARY KEY,
        created_at      TIMESTAMP,
        row_count       BIGINT,
        mmsi_count      BIGINT,
        batch_ids       VARCHAR,
        clean_root      VARCHAR,
        quality_json    VARCHAR
    );
    """,
]

_PARAM_HINT = "SET memory_limit='%s';"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


_DB_CONN = None


def _shared_db():
    """进程内共享的元数据库实例。

    DuckDB 同一文件在同一进程内只能打开一个 Database 实例；
    重复 duckdb.connect(同路径) 会创建两个实例并互相锁冲突：
    “另一个程序正在使用此文件”。这里全局复用同一实例，
    每次操作再 cursor() 派生独立连接。
    """
    global _DB_CONN
    with _LOCK:
        if _DB_CONN is None:
            config = {"temp_directory": str(settings.tmp_dir)}
            _DB_CONN = duckdb.connect(str(settings.meta_db), config=config)
    return _DB_CONN


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """派生一个元数据库连接（共享同一 Database 实例）并应用资源参数。

    注意：同一 Database 实例上不要让多个线程同时执行语句。
    写操作统一走 exec_sql / insert_df（持有全局锁），读操作走 q()。
    """
    settings.ensure_dirs()
    con = _shared_db().cursor()
    _apply_resources(con)
    return con


def connect_memory() -> duckdb.DuckDBPyConnection:
    """独立内存 DuckDB 连接：只用于读 Parquet 数据集，不碰元数据库。

    分析任务只需要建 clean_points 视图后读 Parquet，不需要元数据库；
    这样可避免长任务与 API 请求争抢 meta.duckdb 的写锁。
    """
    con = duckdb.connect(database=":memory:")
    _apply_resources(con)
    return con


def _apply_resources(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"SET memory_limit='{settings.duckdb_memory_limit}'")
    cores = os.cpu_count() or 4
    con.execute(f"SET threads={max(2, cores - 2)}")
    con.execute("SET temp_directory=?", [str(settings.tmp_dir)])


def init_meta() -> None:
    with _LOCK, connect() as con:
        for ddl in META_DDL:
            con.execute(ddl)


@contextlib.contextmanager
def cursor(read_only: bool = False):
    """短生命周期连接上下文。"""
    con = connect(read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def q(sql: str, params: Optional[Sequence[Any]] = None) -> pd.DataFrame:
    with _LOCK, cursor() as con:
        return con.execute(sql, list(params or [])).fetch_df()


def exec_sql(statement: str, params: Optional[Sequence[Any]] = None) -> None:
    with _LOCK, cursor() as con:
        con.execute(statement, list(params or []))


def insert_df(table: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    with _LOCK, cursor() as con:
        con.register("_payload", df)
        cols = ", ".join(f'"{c}"' for c in df.columns)
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _payload")
        con.unregister("_payload")


# ---------------- 数据集 ----------------

def clean_dataset_root(dataset_version: str) -> Path:
    return settings.clean_root / dataset_version


def dataset_glob(dataset_version: str) -> Path:
    return clean_dataset_root(dataset_version) / "event_date=*" / "*.parquet"


def dataset_available(dataset_version: str) -> bool:
    return len(list(dataset_glob(dataset_version).parent.parent.glob("event_date=*"))) > 0


def register_costals(con: duckdb.DuckDBPyConnection, dataset_version: str) -> None:
    """在连接中创建常用视图。"""
    p = dataset_glob(dataset_version).as_posix()
    con.execute(f"CREATE OR REPLACE VIEW clean_points AS SELECT * FROM read_parquet('{p}', hive_partitioning=true)")


def dataset_versions() -> pd.DataFrame:
    return q("SELECT * FROM dataset_version ORDER BY created_at DESC")


def batch_list() -> pd.DataFrame:
    return q("SELECT * FROM data_batch ORDER BY created_at DESC")


def audit(action: str, resource: str, detail: str = "", user_id: str = "system", result: str = "ok", ip: str = "") -> None:
    with _LOCK, cursor() as con:
        con.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?)",
            [new_id("audit"), user_id, action, resource, detail, ip, result, _now()],
        )


def save_config_version(module: str, params: Dict[str, Any], created_by: str = "system") -> str:
    import json

    ver = new_id(f"cfg_{module}")
    exec_sql(
        "INSERT INTO config_version VALUES (?,?,?,?,?)",
        [ver, module, json.dumps(params, ensure_ascii=False, default=str), _now(), created_by],
    )
    return ver


def save_grid_definition(spec) -> None:
    import json

    geo = spec.cells_geojson()
    exec_sql(
        "INSERT OR REPLACE INTO grid_definition VALUES (?,?,?,?,?,?,?,?,?)",
        [
            spec.version,
            json.dumps(spec.to_dict(), ensure_ascii=False),
            "EPSG:4326",
            spec.rows,
            spec.cols,
            float(getattr(spec, "cell_size_m", 0.0) or 0.0),
            spec.mode,
            json.dumps(geo),
            _now(),
        ],
    )
