"""地图与数据集查询服务。

供前端地图使用：按 bbox / 时间 / 船型 / 缩放等级过滤并聚合，
绝不一次性返回全量 AIS 明细（技术方案 11.2 / 14.3）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from . import store
from .algorithms.common import Filter, open_dataset
from .config import settings


def dataset_exists(dataset_version: str) -> bool:
    """判断某个数据集版本是否真的存在于磁盘上（有已治理的点表 parquet）。"""
    if not dataset_version:
        return False
    d = settings.clean_root / dataset_version
    if not d.is_dir():
        return False
    return any(d.glob("event_date=*/*.parquet")) or any(d.glob("*.parquet"))


def overview(dataset_version: Optional[str] = None) -> Dict[str, Any]:
    """总览工作台：数据规模、时间范围、覆盖区域、质量与任务状态。"""
    dv = dataset_version or _latest_dataset()
    out: Dict[str, Any] = {"dataset_version": dv}
    if not dv:
        out["empty"] = True
        return out
    # 显式指定了不存在的数据集版本时，直接返回空结果，
    # 不要让它走到 open_dataset() 抛 DuckDB 的 IO 异常（那是 500，不是 404）。
    if dataset_version and not dataset_exists(dataset_version):
        out["empty"] = True
        out["reason"] = f"数据集 {dataset_version} 不存在或尚未完成治理"
        return out

    con = open_dataset(dv)
    try:
        row = con.execute(
            """SELECT count(*) n, count(DISTINCT mmsi) m, min(event_time) t0, max(event_time) t1,
                      min(lon) minlon, min(lat) minlat, max(lon) maxlon, max(lat) maxlat,
                      count(DISTINCT traj_id) traj, round(avg(quality_score),1) q
               FROM clean_points"""
        ).fetchone()
        # 稳健包络：极少数漂移点会拉大 min/max，地图取 0.1%~99.9% 分位作为默认视野
        q = con.execute(
            """SELECT quantile_cont(lon,0.001) a, quantile_cont(lat,0.001) b,
                      quantile_cont(lon,0.999) c, quantile_cont(lat,0.999) d
               FROM clean_points"""
        ).fetchone()
        out.update(
            {
                "n_points": int(row[0]),
                "n_mmsi": int(row[1]),
                "time_range": [str(row[2]), str(row[3])],
                "bbox": [float(row[4]), float(row[5]), float(row[6]), float(row[7])],
                "bbox_robust": [float(q[0]), float(q[1]), float(q[2]), float(q[3])] if q and q[0] is not None else None,
                "n_trajectories": int(row[8]),
                "avg_quality": float(row[9] or 0),
            }
        )
        out["by_ship_type"] = con.execute(
            """SELECT coalesce(ship_type,'未知') AS name, count(*) AS n, count(DISTINCT mmsi) AS mmsi
               FROM clean_points GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""
        ).fetchdf().to_dict(orient="records")
        out["hourly"] = con.execute(
            "SELECT hour(event_time) AS h, count(*) AS n FROM clean_points GROUP BY 1 ORDER BY 1"
        ).fetchdf().to_dict(orient="records")
        out["daily"] = con.execute(
            "SELECT CAST(event_time AS DATE) AS d, count(*) AS n, count(DISTINCT mmsi) AS mmsi "
            "FROM clean_points GROUP BY 1 ORDER BY 1"
        ).fetchdf().assign(d=lambda x: x["d"].astype(str)).to_dict(orient="records")
        out["speed_hist"] = con.execute(
            "SELECT CAST(floor(least(coalesce(sog,0),30)) AS INT) AS k, count(*) AS n "
            "FROM clean_points GROUP BY 1 ORDER BY 1"
        ).fetchdf().to_dict(orient="records")
        out["top_speed_vessels"] = con.execute(
            """SELECT mmsi, coalesce(any_value(name),'未知') AS name,
                      coalesce(any_value(ship_type),'未知') AS ship_type,
                      round(max(sog),1) AS max_sog, count(*) AS points,
                      round(max(lat),4) AS lat, round(max(lon),4) AS lon
               FROM clean_points GROUP BY mmsi ORDER BY max_sog DESC LIMIT 10"""
        ).fetchdf().to_dict(orient="records")
        out["stay_count"] = 0
        sp = settings.clean_root / dv / "staypoints.parquet"
        if sp.is_file():
            out["stay_count"] = int(con.execute(f"SELECT count(*) FROM read_parquet('{sp.as_posix()}')").fetchone()[0])
    finally:
        con.close()

    # 质量报告
    qr = settings.clean_root / dv / "quality_report.json"
    if qr.is_file():
        import json

        try:
            out["quality"] = json.loads(qr.read_text(encoding="utf-8"))
        except Exception:
            pass
    out["datasets"] = store.q(
        "SELECT dataset_version, row_count, mmsi_count, created_at FROM dataset_version ORDER BY created_at DESC LIMIT 10"
    ).assign(created_at=lambda d: d["created_at"].astype(str)).to_dict(orient="records")
    out["tasks"] = store.q(
        """SELECT status, count(*) n FROM analysis_task GROUP BY 1"""
    ).to_dict(orient="records")
    return out


def _latest_dataset() -> Optional[str]:
    df = store.q("SELECT dataset_version FROM dataset_version ORDER BY created_at DESC LIMIT 1")
    return None if df.empty else str(df.iloc[0]["dataset_version"])


def _rdp(points: List[List[float]], eps_m: float) -> List[List[float]]:
    """Douglas-Peucker 轨迹简化（米制），减少前端线数据量。"""
    if len(points) < 3 or eps_m <= 0:
        return points
    lat0 = np.radians(np.mean([p[1] for p in points]))
    kx = 111320.0 * np.cos(lat0)
    ky = 110574.0
    pts = np.array([[p[0] * kx, p[1] * ky] for p in points])

    def _rec(a: int, b: int, keep: np.ndarray) -> None:
        if b <= a + 1:
            return
        seg = pts[b] - pts[a]
        norm = np.hypot(*seg)
        sub = pts[a + 1:b]
        if norm < 1e-9:
            d = np.hypot(sub[:, 0] - pts[a][0], sub[:, 1] - pts[a][1])
        else:
            d = np.abs(np.cross(seg, sub - pts[a])) / norm
        i = int(np.argmax(d))
        if d[i] > eps_m:
            keep[a + 1 + i] = True
            _rec(a, a + 1 + i, keep)
            _rec(a + 1 + i, b, keep)

    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    _rec(0, len(pts) - 1, keep)
    return [points[i] for i in np.where(keep)[0]]


def trajectories(
    dataset_version: str,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 150,
    simplify_m: float = 15.0,
    max_points_per_traj: int = 400,
) -> Dict[str, Any]:
    """返回地图用的轨迹线（已简化），按点数排序取前 N 条。"""
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        where = flt.where()
        seg = con.execute(
            f"""SELECT traj_id, mmsi, count(*) AS n, min(event_time) AS t0, max(event_time) AS t1,
                       coalesce(any_value(ship_type),'未知') AS ship_type,
                       coalesce(any_value(name),'') AS name,
                       any_value(quality_grade) AS grade
                FROM clean_points WHERE {where}
                GROUP BY 1,2 ORDER BY n DESC LIMIT {int(limit)}"""
        ).fetchdf()
        if seg.empty:
            return {"type": "FeatureCollection", "features": [], "count": 0}
        ids = ",".join("'" + str(t).replace("'", "''") + "'" for t in seg["traj_id"])
        pts = con.execute(
            f"""SELECT traj_id, lon, lat, sog, event_time FROM clean_points
                WHERE traj_id IN ({ids}) ORDER BY traj_id, event_time"""
        ).fetchdf()
    finally:
        con.close()

    feats = []
    meta_by = seg.set_index("traj_id").to_dict(orient="index")
    for tid, g in pts.groupby("traj_id", sort=False):
        coords = [[round(float(a), 6), round(float(b), 6)] for a, b in zip(g["lon"], g["lat"])]
        if len(coords) > 40:
            coords = _rdp(coords, simplify_m)
        if len(coords) > max_points_per_traj:
            step = len(coords) / max_points_per_traj
            coords = [coords[int(i * step)] for i in range(max_points_per_traj)]
        m = meta_by.get(tid, {})
        feats.append(
            {
                "type": "Feature",
                "properties": {
                    "traj_id": tid,
                    "mmsi": m.get("mmsi"),
                    "ship_type": m.get("ship_type"),
                    "name": m.get("name") or "",
                    "points": int(m.get("n", 0)),
                    "grade": m.get("grade"),
                    "t0": str(m.get("t0")),
                    "t1": str(m.get("t1")),
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": feats, "count": len(feats)}


def points_map(
    dataset_version: str,
    bbox: List[float],
    time_range: Optional[List[str]] = None,
    zoom: int = 8,
    ship_types: Optional[List[str]] = None,
    max_points: int = 30000,
) -> Dict[str, Any]:
    """按视口与缩放级别返回聚合点（WebGL 友好的轻量结构）。"""
    res = max(0.002, 0.6 / (2 ** max(0, zoom - 4)))
    con = open_dataset(dataset_version)
    try:
        conds = [
            f"lon BETWEEN {bbox[0]!r} AND {bbox[2]!r}",
            f"lat BETWEEN {bbox[1]!r} AND {bbox[3]!r}",
        ]
        if time_range and len(time_range) == 2 and time_range[0]:
            conds.append(f"event_time >= CAST('{time_range[0]}' AS TIMESTAMP)")
            conds.append(f"event_time < CAST('{time_range[1]}' AS TIMESTAMP)")
        if ship_types:
            lst = ",".join("'" + s.replace("'", "''") + "'" for s in ship_types)
            conds.append(f"ship_type IN ({lst})")
        w = " AND ".join(conds)
        df = con.execute(
            f"""
            SELECT round(floor(lon/{res})*{res} + {res}/2, 4) AS lon,
                   round(floor(lat/{res})*{res} + {res}/2, 4) AS lat,
                   count(*) AS n, count(DISTINCT mmsi) AS vessels,
                   round(avg(sog),1) AS sog
            FROM clean_points WHERE {w}
            GROUP BY 1,2 ORDER BY n DESC LIMIT {int(max_points)}
            """
        ).fetchdf()
        return {"resolution_deg": res, "zoom": zoom, "points": df.to_dict(orient="records")}
    finally:
        con.close()


def vessels(dataset_version: str, filters: Optional[Dict[str, Any]] = None, limit: int = 500) -> List[Dict[str, Any]]:
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        df = con.execute(
            f"""SELECT mmsi, coalesce(any_value(name),'') AS name,
                       coalesce(any_value(ship_type),'未知') AS ship_type,
                       coalesce(any_value(length),0) AS length,
                       count(*) AS points, round(median(sog),1) AS med_sog,
                       round(max(sog),1) AS max_sog,
                       count(DISTINCT traj_id) AS segments,
                       min(event_time) AS t0, max(event_time) AS t1,
                       round(avg(lat),4) AS lat, round(avg(lon),4) AS lon,
                       any_value(quality_grade) AS grade
                FROM clean_points WHERE {flt.where()}
                GROUP BY mmsi ORDER BY points DESC LIMIT {int(limit)}"""
        ).fetchdf()
    finally:
        con.close()
    df["t0"] = df["t0"].astype(str)
    df["t1"] = df["t1"].astype(str)
    return df.to_dict(orient="records")


def vessel_track(dataset_version: str, mmsi: str, max_points: int = 3000) -> Dict[str, Any]:
    con = open_dataset(dataset_version)
    try:
        df = con.execute(
            f"""SELECT event_time, lon, lat, sog, cog, heading, traj_id, quality_flags
                FROM clean_points WHERE mmsi = ? ORDER BY event_time
                LIMIT {int(max_points)}""",
            [str(mmsi)],
        ).fetchdf()
        stays = []
        sp = settings.clean_root / dataset_version / "staypoints.parquet"
        if sp.is_file():
            stays = con.execute(
                f"SELECT * FROM read_parquet('{sp.as_posix()}') WHERE mmsi = ? ORDER BY start_time",
                [str(mmsi)],
            ).fetchdf()
            for c in ("start_time", "end_time"):
                stays[c] = stays[c].astype(str)
    finally:
        con.close()
    df["event_time"] = df["event_time"].astype(str)
    return {"mmsi": str(mmsi), "points": df.to_dict(orient="records"), "staypoints": stays.to_dict(orient="records") if len(stays) else []}


def staypoints(dataset_version: str, limit: int = 5000) -> List[Dict[str, Any]]:
    sp = settings.clean_root / dataset_version / "staypoints.parquet"
    if not sp.is_file():
        return []
    con = open_dataset(dataset_version)
    try:
        df = con.execute(
            f"""SELECT stay_id, mmsi, round(center_lon,5) AS lon, round(center_lat,5) AS lat,
                       start_time, end_time, duration_min, point_count, radius_m, mean_sog
                FROM read_parquet('{sp.as_posix()}') ORDER BY duration_s DESC LIMIT {int(limit)}"""
        ).fetchdf()
    finally:
        con.close()
    for c in ("start_time", "end_time"):
        df[c] = df[c].astype(str)
    return df.to_dict(orient="records")


def batches() -> List[Dict[str, Any]]:
    df = store.batch_list()
    if df.empty:
        return []
    df["created_at"] = df["created_at"].astype(str)
    return df.drop(columns=["profile_json"], errors="ignore").to_dict(orient="records")


def data_dictionary() -> Dict[str, Any]:
    from .schema import DATA_DICTIONARY, FIELD_MAPPING

    return {
        "fields": [{"field": k, **v} for k, v in DATA_DICTIONARY.items()],
        "source_mapping": FIELD_MAPPING,
    }
