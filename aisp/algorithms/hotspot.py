"""热点区域分析（HDBSCAN，haversine 度量）。

算法要点（技术方案 5.3-5.7）：
- 输入 [纬度, 经度] 弧度，metric="haversine"，不把经纬度当平面欧氏距离；
- 上报频率偏差控制：按 MMSI + 时间槽抽样，避免高频上报船舶过度贡献密度；
- 交通流热点与停留热点分开统计口径，界面必须声明；
- 每簇输出中心、凸包边界、面积、点数、独立 MMSI/航次、速度统计、时间强度序列；
- 噪声点（label=-1）单独输出，不并入热点。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import settings
from .common import Filter, dataset_extent, jsonable, open_dataset, downsample, write_result
from ..geo import haversine_m, circular_mean_deg

ProgressCb = Optional[Callable[[float, str], None]]


def _p(cb: ProgressCb, v: float, s: str) -> None:
    if cb:
        cb(v, s)


def _convex_hull(points: np.ndarray) -> List[List[float]]:
    """Andrew 单调链凸包，返回闭合经纬度环。"""
    pts = points[np.lexsort((points[:, 1], points[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(tuple(p))
    upper: List[Tuple[float, float]] = []
    for p in pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(tuple(p))
    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return []
    hull.append(hull[0])
    return [[round(x, 6), round(y, 6)] for x, y in hull]


def _polygon_area_m2(ring: List[List[float]], lat0: float) -> float:
    """球面近似多边形面积（局部等距圆柱）。"""
    if len(ring) < 4:
        return 0.0
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0
    pts = [(p[0] * kx, p[1] * ky) for p in ring]
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(s) / 2.0


def run(
    dataset_version: str,
    task_id: str,
    params: Dict[str, Any],
    filters: Optional[Dict[str, Any]] = None,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        extent = dataset_extent(con, flt)
        if extent is None:
            return {"error": "筛选条件下没有数据"}
        min_lon, min_lat, max_lon, max_lat = extent
        # 极少数漂移点会拉大包络，地图与 KPI 使用 0.1%~99.9% 分位的稳健范围
        rb = con.execute(
            f"""SELECT quantile_cont(lon,0.001), quantile_cont(lat,0.001),
                       quantile_cont(lon,0.999), quantile_cont(lat,0.999)
                FROM clean_points WHERE {flt.where()}"""
        ).fetchone()
        extent_robust = [float(rb[0]), float(rb[1]), float(rb[2]), float(rb[3])] if rb and rb[0] is not None else list(extent)

        min_cluster_size = int(params.get("min_cluster_size", 60))
        min_samples = int(params.get("min_samples", 12))
        method = str(params.get("cluster_selection_method", "eom"))
        eps = float(params.get("cluster_selection_epsilon", 0.0))
        slot_min = int(params.get("time_slot_minutes", 5))
        max_points = int(params.get("max_points", 300_000))
        res_deg = float(params.get("grid_resolution", 0.01))
        top_n = int(params.get("top_n", 20))
        hotspot_hours = params.get("hours")

        # ---- 1. 抽样：MMSI + 时间槽，抑制上报频率偏差 ----
        _p(progress, 0.1, "按船舶与时间槽抽样，控制上报频率偏差")
        where = flt.where()
        if hotspot_hours:
            hrs = ",".join(str(int(h)) for h in hotspot_hours)
            where += f" AND hour(event_time) IN ({hrs})"
        sample = con.execute(
            f"""
            SELECT mmsi, lat, lon, sog, cog, ship_type, traj_id,
                   epoch(event_time)::BIGINT // {slot_min * 60} AS slot, event_time
            FROM clean_points
            WHERE {where} AND coalesce(quality_flags,'') NOT LIKE '%JUMP%'
              AND coalesce(quality_flags,'') NOT LIKE '%GPS_DRIFT%'
              AND coalesce(quality_flags,'') NOT LIKE '%FROZEN_GPS%'
            QUALIFY row_number() OVER (
                PARTITION BY mmsi, slot ORDER BY event_time
            ) = 1
            """
        ).fetchdf()
        n_sampled = len(sample)
        if sample.empty or n_sampled < min_cluster_size:
            return {"error": f"抽样后样本量不足（{n_sampled} 点），请扩大时间或区域范围"}

        sample = downsample(sample, max_points)
        _p(progress, 0.25, f"抽样完成：{n_sampled} → {len(sample)} 点，开始 HDBSCAN 聚类")

        # ---- 2. 密度网格（用于热力图与后续统计） ----
        grid = con.execute(
            f"""
            WITH s AS (
                SELECT floor(lon / {res_deg}) * {res_deg} AS gx, floor(lat / {res_deg}) * {res_deg} AS gy,
                       mmsi, sog
                FROM clean_points WHERE {where}
            )
            SELECT round(gx + {res_deg}/2, 4) AS lon, round(gy + {res_deg}/2, 4) AS lat,
                   count(*) AS n, count(DISTINCT mmsi) AS vessels,
                   round(avg(sog), 2) AS avg_sog
            FROM s GROUP BY 1, 2 ORDER BY n DESC LIMIT 20000
            """
        ).fetchdf()

        # ---- 3. HDBSCAN ----
        _p(progress, 0.35, "执行 HDBSCAN（haversine）聚类")
        import hdbscan

        coords = np.radians(sample[["lat", "lon"]].to_numpy(dtype="float64"))
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=max(5, min_cluster_size),
            min_samples=max(1, min_samples),
            metric="haversine",
            cluster_selection_method=method,
            cluster_selection_epsilon=eps,
            core_dist_n_jobs=max(1, (settings.__dict__.get("cpu_workers") or 8)),
        )
        labels = clusterer.fit_predict(coords)
        sample["cluster"] = labels
        n_clusters = int(len(set(labels)) - (1 if -1 in labels else 0))
        noise_ratio = float((labels == -1).mean())

        # 参数敏感性：用相邻参数再跑一次小样本，报告稳定性
        _p(progress, 0.6, "参数敏感性对比")
        sensitivity = _sensitivity(coords, sample, min_cluster_size, min_samples, method, eps, labels)

        # ---- 4. 每个簇的统计与几何 ----
        _p(progress, 0.7, "计算簇统计、边界与强度序列")
        clusters = _cluster_stats(sample, min_vessels=int(params.get("min_vessels", 8)))
        # 排序：主要热点（船数达标）优先，其次按点数；保证 TOP 列表是业务关注的对象
        clusters.sort(key=lambda c: (not c["significant"], -c["point_count"]))
        for rank, c in enumerate(clusters, 1):
            c["rank"] = rank
        top = clusters[:top_n]
        # 控制 summary 体积：仅输出前 600 簇（完整结果在 parquet 中）
        clusters_out = clusters[:600]
        cluster_df = pd.DataFrame([{k: v for k, v in c.items() if k != "hull"} for c in clusters])

        # 时间强度序列（按小时）
        ts = (
            sample[sample["cluster"] >= 0]
            .assign(hour=lambda d: pd.to_datetime(d["event_time"]).dt.floor("h"))
            .groupby(["cluster", "hour"]).size().rename("n").reset_index()
        )
        ts_series = []
        for c in top:
            sub = ts[ts["cluster"] == c["cluster_id"]].sort_values("hour")
            ts_series.append(
                {
                    "cluster_id": c["cluster_id"],
                    "points": [
                        {"t": str(r.hour), "n": int(r.n)} for r in sub.itertuples()
                    ],
                }
            )

        # ---- 5. 结果落盘 ----
        _p(progress, 0.9, "输出热点边界与结果文件")
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": c["cluster_id"],
                    "properties": {k: v for k, v in c.items() if k != "hull"},
                    "geometry": {"type": "Polygon", "coordinates": [c["hull"]]} if c["hull"] else None,
                }
                for c in clusters
                if c["hull"]
            ],
        }
        noise = sample[sample["cluster"] == -1][["mmsi", "lat", "lon", "sog", "event_time"]]

        summary = {
            "algorithm": "hdbscan_hotspot",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "extent": [min_lon, min_lat, max_lon, max_lat],
            "extent_robust": extent_robust,
            "sampling": {
                "time_slot_minutes": slot_min,
                "method": "每 MMSI 每个时间槽保留 1 点（抑制上报频率偏差）",
                "n_before_sampling": int(con.execute(
                    f"SELECT count(*) FROM clean_points WHERE {where}").fetchone()[0]),
                "n_sampled": int(n_sampled),
                "n_used": int(len(sample)),
                "max_points": max_points,
            },
            "n_clusters": n_clusters,
            "n_significant_clusters": int(sum(1 for c in clusters if c.get("significant"))),
            "noise_ratio": round(noise_ratio, 4),
            "clusters": clusters_out,
            "n_clusters_returned": len(clusters_out),
            "sensitivity": sensitivity,
            "time_series": ts_series,
            "heat_grid": grid.to_dict(orient="records"),
            "units": {"area": "m2/km2", "sog": "kn", "cog": "degree"},
            "notes": [
                "热点基于历史 AIS 点的密度聚类结果，不代表官方航道或安全航区。",
                "凸包仅用于概览边界，不等同真实水域边界。",
                "交通流热点与停留热点通过 dwell_ratio（低速点占比）区分。",
            ],
        }
        summary = jsonable(summary)
        write_result(
            task_id,
            summary,
            {
                "hotspot_clusters.parquet": cluster_df,
                "hotspot_boundaries.geojson": geojson,
                "hotspot_noise.parquet": noise,
            },
        )
        _p(progress, 1.0, f"完成：识别 {n_clusters} 个热点区域")
        return summary
    finally:
        con.close()


def _cluster_stats(sample: pd.DataFrame, min_vessels: int = 8) -> List[Dict[str, Any]]:
    """按簇分组计算统计量与几何；significant 标记“船数达标且具规模”的主要热点。"""
    clusters: List[Dict[str, Any]] = []
    if sample.empty:
        return clusters
    grouped = sample[sample["cluster"] >= 0].groupby("cluster", sort=False)
    for cid, g in grouped:
        pts = g[["lon", "lat"]].to_numpy(dtype="float64")
        ring = _convex_hull(pts)
        lat0 = float(np.nanmedian(pts[:, 1]))
        area = _polygon_area_m2(ring, lat0) if ring else 0.0
        # 去重后再统计中心，避免单船密集上报主导中心位置
        uniq = g.drop_duplicates(subset=["lon", "lat"])
        cx = float(np.nanmean(uniq["lon"].to_numpy()))
        cy = float(np.nanmean(uniq["lat"].to_numpy()))
        sog = g["sog"]
        med_sog = float(np.nanmedian(sog)) if sog.notna().any() else 0.0
        dwell_ratio = float((sog.fillna(0) < 1.0).mean())
        n_mmsi = int(g["mmsi"].nunique())
        clusters.append(
            {
                "cluster_id": int(cid),
                "point_count": int(len(g)),
                "distinct_mmsi": n_mmsi,
                "distinct_traj": int(g["traj_id"].nunique()),
                "center_lon": round(cx, 6),
                "center_lat": round(cy, 6),
                "area_m2": round(area, 1),
                "area_km2": round(area / 1e6, 3),
                "avg_sog": round(float(sog.mean()), 2) if sog.notna().any() else None,
                "median_sog": round(med_sog, 2),
                "max_sog": round(float(sog.max()), 2) if sog.notna().any() else None,
                "main_cog_deg": round(circular_mean_deg(g["cog"].dropna().tolist()), 1)
                if g["cog"].notna().any() else None,
                "dwell_ratio": round(dwell_ratio, 3),
                "hotspot_kind": "停留/锚泊热点" if dwell_ratio > 0.5 else "交通流热点",
                "significant": bool(n_mmsi >= min_vessels),
                "t0": str(g["event_time"].min()),
                "t1": str(g["event_time"].max()),
                "top_ship_types": g["ship_type"].value_counts().head(3).to_dict(),
                "hull": ring,
            }
        )
    return clusters


def _sensitivity(coords, sample, mcs, ms, method, eps, labels) -> Dict[str, Any]:
    """3 组参数对比，报告簇数与噪声率变化，用于稳定性说明。"""
    import hdbscan

    base = np.array(labels)
    base_clusters = len(set(base.tolist())) - (1 if -1 in base else 0)
    base_noise = float((base == -1).mean())
    variants = []
    for dmcs, dms in ((1.5, 1.0), (1.0, 1.5), (0.7, 1.0)):
        try:
            c2 = hdbscan.HDBSCAN(
                min_cluster_size=max(5, int(mcs * dmcs)),
                min_samples=max(1, int(ms * dms)),
                metric="haversine",
                cluster_selection_method=method,
                cluster_selection_epsilon=eps,
            )
            lb = c2.fit_predict(coords)
            n = len(set(lb.tolist())) - (1 if -1 in lb else 0)
            variants.append(
                {
                    "min_cluster_size": max(5, int(mcs * dmcs)),
                    "min_samples": max(1, int(ms * dms)),
                    "n_clusters": int(n),
                    "noise_ratio": round(float((lb == -1).mean()), 4),
                    "stability_vs_base": round(_ari(base, lb), 4),
                }
            )
        except Exception as exc:  # pragma: no cover
            variants.append({"error": str(exc)})
    return {
        "base": {"min_cluster_size": mcs, "min_samples": ms, "n_clusters": base_clusters,
                 "noise_ratio": round(base_noise, 4)},
        "variants": variants,
        "note": "stability_vs_base 为与基准参数的 Adjusted Rand Index（1 表示完全一致）",
    }


def _ari(a: np.ndarray, b: np.ndarray) -> float:
    from sklearn.metrics import adjusted_rand_score

    try:
        return float(adjusted_rand_score(a, b))
    except Exception:
        return float("nan")
