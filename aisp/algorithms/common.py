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
    """统一的业务过滤条件。

    空间筛选支持三种形态（技术方案 11.2）：
    - ``bbox``   ：矩形 ``[min_lon, min_lat, max_lon, max_lat]``；
    - ``polygon``：任意多边形顶点列表 ``[[lon, lat], ...]``（首尾可不闭合）；
    - ``circle`` ：圆形 ``[lon, lat, radius_km]``。

    三者都用纯 SQL 表达式下推到 DuckDB 执行：多边形用射线法（Ray Casting），
    圆形用等距圆柱投影近似（半径 ≤ 数百 km 时误差可忽略）；
    同时先加一个经纬度外包围盒条件，让 DuckDB 的行组裁剪先筛掉绝大部分数据。
    """

    def __init__(self, filters: Optional[Dict[str, Any]] = None):
        f = dict(filters or {})
        tr = f.get("time_range") or []
        self.t0: Optional[str] = tr[0] if len(tr) > 0 and tr[0] else None
        self.t1: Optional[str] = tr[1] if len(tr) > 1 and tr[1] else None
        self.bbox = f.get("bbox")  # [min_lon, min_lat, max_lon, max_lat]
        self.polygon = _norm_polygon(f.get("polygon"))
        self.circle = _norm_circle(f.get("circle"))
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
        if self.polygon:
            conds.append(_polygon_sql(self.polygon, p))
        if self.circle:
            conds.append(_circle_sql(self.circle, p))
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
            "polygon": self.polygon,
            "circle": self.circle,
            "ship_types": self.ship_types,
            "mmsi": self.mmsi,
            "drop_short_segments": self.drop_short,
            "exclude_flagged": self.exclude_flagged,
        }


def _norm_polygon(raw: Any) -> Optional[List[List[float]]]:
    """把前端传来的多边形整理成顶点列表；顶点不足 3 个视为无效。"""
    if not raw:
        return None
    try:
        pts = [[float(p[0]), float(p[1])] for p in raw if len(p) >= 2]
    except (TypeError, ValueError, IndexError):
        return None
    # 去掉重复的闭合点
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts if len(pts) >= 3 else None


def _norm_circle(raw: Any) -> Optional[Tuple[float, float, float]]:
    """圆形参数：``[lon, lat, radius_km]``，半径必须为正。"""
    if not raw:
        return None
    try:
        lon, lat, r = float(raw[0]), float(raw[1]), float(raw[2])
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and math.isfinite(r)) or r <= 0:
        return None
    return (lon, lat, r)


def _polygon_sql(pts: List[List[float]], alias: str = "") -> str:
    """射线法（Ray Casting）判定点是否在多边形内，纯 SQL 实现。

    先加外包围盒条件，让 DuckDB 先按行组裁剪；再对候选行做
    「向右发射水平射线、统计与边界的交点个数」判定。
    边界上的点采用「上边界计入、下边界不计入」的半开规则，与主流 GIS 一致。
    """
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    box = (f"{alias}lon BETWEEN {min(lons)!r} AND {max(lons)!r} "
           f"AND {alias}lat BETWEEN {min(lats)!r} AND {max(lats)!r}")
    ring = pts + [pts[0]]
    edges = ",".join(
        f"({a[0]!r},{a[1]!r},{b[0]!r},{b[1]!r})" for a, b in zip(ring, ring[1:])
    )
    inside = (
        f"(SELECT count(*) FROM (VALUES {edges}) AS _e(x1,y1,x2,y2) "
        f"WHERE ((_e.y1 > {alias}lat) <> (_e.y2 > {alias}lat)) "
        f"AND ({alias}lon < (_e.x2-_e.x1)*({alias}lat-_e.y1)"
        f"/NULLIF(_e.y2-_e.y1,0)+_e.x1)) % 2 = 1"
    )
    return f"({box} AND {inside})"


def _circle_sql(c: Tuple[float, float, float], alias: str = "") -> str:
    """圆形范围过滤：等距圆柱投影近似（半径较小时误差可忽略）。

    1 度纬度 ≈ 110.57 km；1 度经度 ≈ 111.32 * cos(lat) km，
    这里用圆心的 cos(lat) 作为常数，避免逐行算三角函数带来的开销。
    """
    lon, lat, r_km = c
    kx = 111.32 * max(math.cos(math.radians(lat)), 1e-6)   # km / 度经度
    ky = 110.57                                            # km / 度纬度
    dlon = r_km / kx
    dlat = r_km / ky
    box = (f"{alias}lon BETWEEN {lon - dlon!r} AND {lon + dlon!r} "
           f"AND {alias}lat BETWEEN {lat - dlat!r} AND {lat + dlat!r}")
    dist = (f"(pow(({alias}lon-{lon!r})*{kx!r},2) + pow(({alias}lat-{lat!r})*{ky!r},2))")
    return f"({box} AND {dist} <= {r_km * r_km!r})"


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
