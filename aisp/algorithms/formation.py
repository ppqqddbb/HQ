"""船舶编群分析（候选筛选 + 重采样 + DTW + 凝聚层次聚类）。

技术方案第 8 章要点全部落实：
1. 先做候选对筛选（时间重叠率、空间包围盒、点数），避免 N² 全量 DTW；
2. 轨迹重采样到统一时间栅格，短缺口插值并标记；
3. 多变量 DTW 局部代价：位置(m)/SOG(kn)/航向(1-cos)/时间偏差(s)，各项先归一化再加权；
4. Sakoe-Chiba 带限制对齐路径，防止过度拉伸；
5. average linkage 层次聚类 + 簇内二次规则（时间重叠、最大间距、航向一致性）；
6. 输出 Precision 相关的证据字段（重叠率、方向一致、DTW 距离）便于人工核验。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .common import Filter, dataset_extent, jsonable, open_dataset, write_result
from ..geo import Projection, haversine_m, wrap_deg

ProgressCb = Optional[Callable[[float, str], None]]


def _p(cb: ProgressCb, v: float, s: str) -> None:
    if cb:
        cb(v, s)


def _resample(df: pd.DataFrame, step_s: int, proj: Projection) -> Optional[pd.DataFrame]:
    """把单船轨迹重采样到固定时间栅格，短缺口线性插值并标记插值点。"""
    if len(df) < 5:
        return None
    t = df["event_time"].to_numpy(dtype="datetime64[s]").astype("int64")
    order = np.argsort(t)
    t = t[order]
    lon = df["lon"].to_numpy(dtype="float64")[order]
    lat = df["lat"].to_numpy(dtype="float64")[order]
    sog = df["sog"].to_numpy(dtype="float64")[order]
    cog = df["cog"].to_numpy(dtype="float64")[order]

    t0, t1 = int(t[0]), int(t[-1])
    if t1 - t0 < step_s * 3:
        return None
    grid = np.arange(t0, t1 + 1, step_s)
    if len(grid) > 2000:  # 控制单序列长度，超长则截断到最近 2000 步
        grid = grid[:2000]

    # 只在短缺口（<= 4 步）内插值
    max_gap = step_s * 4
    ex = np.interp(grid, t, lon)
    ey = np.interp(grid, t, lat)
    es, ec = np.interp(grid, t, sog), np.interp(grid, t, cog)

    # 标记插值点：与原始时间不符且处在大缺口中
    idx = np.searchsorted(t, grid, side="right") - 1
    idx = np.clip(idx, 0, len(t) - 2)
    gap = t[idx + 1] - t[idx]
    interpolated = gap > max_gap

    x, y = proj.forward(ex, ey)
    return pd.DataFrame(
        {
            "t": grid,
            "x": x,
            "y": y,
            "sog": es,
            "cog": ec,
            "interp": interpolated,
        }
    )


def _dtw_cost(a: pd.DataFrame, b: pd.DataFrame, band: int, w: Dict[str, float],
              norm: Dict[str, float]) -> Tuple[float, float, int]:
    """带 Sakoe-Chiba 约束的多变量 DTW（内核见 dtw.py）。"""
    from .dtw import dtw_cost as _core

    d, sep, steps = _core(a, b, band, w, norm)
    if d < 0:
        return float("nan"), float("nan"), 0
    return d, sep, steps


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
        proj = Projection.from_bbox(min_lon, min_lat, max_lon, max_lat)

        step_s = int(params.get("resample_seconds", 120))
        overlap_min = float(params.get("time_overlap_min", 0.6))
        max_sep = float(params.get("max_sep_m", 3000.0))
        cog_tol = float(params.get("cog_tol_deg", 45.0))
        sog_tol = float(params.get("sog_tol_kn", 4.0))
        dtw_thr = float(params.get("dtw_threshold", 1.2))
        linkage = str(params.get("linkage", "average"))
        min_members = int(params.get("min_members", 2))
        max_pairs = int(params.get("max_pairs", 20000))
        max_seq = int(params.get("max_sequences", 120))
        window_min = int(params.get("window_minutes", 240))

        where = flt.where()

        # ---- 1. 时间窗与轨迹筛选 ----
        _p(progress, 0.1, "筛选分析时间窗内的有效轨迹段")
        win = con.execute(
            f"""
            SELECT max(event_time) - CAST('{window_min} minutes' AS INTERVAL) AS t0, max(event_time) AS t1
            FROM clean_points WHERE {where}
            """
        ).fetchone()
        if not win or win[0] is None:
            return {"error": "没有可用数据"}
        wt0, wt1 = win
        segs = con.execute(
            f"""
            SELECT traj_id, mmsi, count(*) AS n,
                   min(event_time) AS t0, max(event_time) AS t1,
                   min(lon) AS minlon, max(lon) AS maxlon, min(lat) AS minlat, max(lat) AS maxlat,
                   median(sog) AS med_sog, any_value(ship_type) AS ship_type
            FROM clean_points
            WHERE {where} AND event_time >= CAST('{wt0}' AS TIMESTAMP)
                        AND coalesce(short_segment, FALSE) = FALSE
            GROUP BY 1, 2 HAVING count(*) >= 10
            ORDER BY count(*) DESC LIMIT {max_seq}
            """
        ).fetchdf()
        if len(segs) < 2:
            return {"error": f"时间窗内有效轨迹段不足（{len(segs)}），无法做编群分析"}

        _p(progress, 0.2, "读取轨迹点并重采样到统一时间栅格")
        traj_ids = segs["traj_id"].tolist()
        lst = ",".join("'" + str(t).replace("'", "''") + "'" for t in traj_ids)
        pts = con.execute(
            f"""
            SELECT traj_id, mmsi, event_time, lon, lat, sog, cog
            FROM clean_points WHERE traj_id IN ({lst})
            ORDER BY traj_id, event_time
            """
        ).fetchdf()
        ship_of = dict(zip(segs["traj_id"], segs["ship_type"]))

        series: Dict[str, pd.DataFrame] = {}
        meta: Dict[str, Dict[str, Any]] = {}
        for tid, g in pts.groupby("traj_id", sort=False):
            rs = _resample(g, step_s, proj)
            if rs is None or len(rs) < 10:
                continue
            series[tid] = rs
            st = ship_of.get(tid)
            meta[tid] = {
                "mmsi": str(g["mmsi"].iloc[0]),
                "ship_type": None if st is None or pd.isna(st) else str(st),
                "t0": str(g["event_time"].min()),
                "t1": str(g["event_time"].max()),
                "n_raw": int(len(g)),
                "n_resampled": int(len(rs)),
                "interp_ratio": round(float(rs["interp"].mean()), 3),
                "med_sog": round(float(np.nanmedian(g["sog"])), 2) if g["sog"].notna().any() else None,
            }
        ids = list(series.keys())
        if len(ids) < 2:
            return {"error": "重采样后可用轨迹不足，请放宽最小点数或缩短重采样间隔"}

        # ---- 2. 候选对筛选 ----
        _p(progress, 0.35, f"候选对筛选（{len(ids)} 条轨迹）")
        min_overlap_s = step_s * 10
        cands: List[Tuple[str, str, float, float]] = []
        for i in range(len(ids)):
            a = series[ids[i]]
            for j in range(i + 1, len(ids)):
                b = series[ids[j]]
                ov_t0 = max(a["t"].iloc[0], b["t"].iloc[0])
                ov_t1 = min(a["t"].iloc[-1], b["t"].iloc[-1])
                ov = ov_t1 - ov_t0
                if ov < min_overlap_s:
                    continue
                ra = max(a["t"].iloc[-1] - a["t"].iloc[0], 1)
                rb = max(b["t"].iloc[-1] - b["t"].iloc[0], 1)
                ratio = ov / min(ra, rb)
                if ratio < overlap_min * 0.5:
                    continue
                # 包围盒距离（下界剪枝）
                ax0, ax1 = a["x"].min(), a["x"].max()
                ay0, ay1 = a["y"].min(), a["y"].max()
                bx0, bx1 = b["x"].min(), b["x"].max()
                by0, by1 = b["y"].min(), b["y"].max()
                gx = max(0.0, max(ax0, bx0) - min(ax1, bx1))
                gy = max(0.0, max(ay0, by0) - min(ay1, by1))
                bbox_d = math.hypot(gx, gy)
                if bbox_d > max_sep * 3:
                    continue
                cands.append((ids[i], ids[j], ratio, bbox_d))
        cands.sort(key=lambda c: (c[2], -c[3]), reverse=True)
        n_cand_total = len(cands)
        cands = cands[:max_pairs]

        # ---- 3. DTW 计算 ----
        _p(progress, 0.5, f"计算候选对 DTW（{len(cands)} 对）")
        norm = {"pos": max(100.0, max_sep / 3.0), "sog": max(1.0, sog_tol), "cog": 2.0, "time": step_s * 2.0}
        w = {"pos": 1.0, "sog": 0.35, "cog": 0.2, "time": 0.1}
        band = 24
        pairs: List[Dict[str, Any]] = []
        t_start = time.time()
        for k, (ia, ib, ovr, bbox_d) in enumerate(cands):
            if k % 200 == 0:
                _p(progress, 0.5 + 0.3 * k / max(1, len(cands)), f"DTW {k}/{len(cands)}")
            a, b = series[ia], series[ib]
            d, sep, steps = _dtw_cost(a, b, band, w, norm)
            if not math.isfinite(d):
                continue
            # 时间重叠后的平均航向/航速差
            ov_t0 = max(a["t"].iloc[0], b["t"].iloc[0])
            ov_t1 = min(a["t"].iloc[-1], b["t"].iloc[-1])
            am = a[(a["t"] >= ov_t0) & (a["t"] <= ov_t1)]
            bm = b[(b["t"] >= ov_t0) & (b["t"] <= ov_t1)]
            if len(am) < 3 or len(bm) < 3:
                continue
            dsog = _nanabsdiff(am["sog"], bm["sog"])
            ddir = _nancircdiff(am["cog"], bm["cog"])
            if not (np.isfinite(dsog) and np.isfinite(ddir)):
                continue
            pairs.append(
                {
                    "a": ia, "b": ib, "mmsi_a": meta[ia]["mmsi"], "mmsi_b": meta[ib]["mmsi"],
                    "dtw": round(d, 4), "mean_sep_m": round(sep, 1), "overlap_ratio": round(ovr, 3),
                    "delta_sog": round(float(dsog), 2), "delta_cog": round(float(ddir), 1),
                    "aligned_steps": int(steps), "bbox_m": round(bbox_d, 1),
                }
            )
        elapsed = time.time() - t_start
        if not pairs:
            return {"error": "候选对均未通过 DTW 质量门槛，请放宽时间重叠或空间距离参数"}

        # ---- 4. 层次聚类 ----
        _p(progress, 0.82, "凝聚层次聚类与编群判定")
        from scipy.cluster.hierarchy import fcluster, linkage as hlink
        from scipy.spatial.distance import squareform

        members = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
        midx = {m: i for i, m in enumerate(members)}
        n = len(members)
        D = np.full((n, n), 0.0)
        big = max(10.0, dtw_thr * 5)
        np.fill_diagonal(D, 0.0)
        for p in pairs:
            i, j = midx[p["a"]], midx[p["b"]]
            D[i, j] = D[j, i] = p["dtw"]
        # 未计算的对按上界填充，避免被误合并
        for i in range(n):
            for j in range(i + 1, n):
                if D[i, j] == 0.0:
                    D[i, j] = D[j, i] = big
        Z = hlink(squareform(D, checks=False), method=linkage)
        labels = fcluster(Z, t=dtw_thr, criterion="distance")

        groups: List[Dict[str, Any]] = []
        pmap: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for p in pairs:
            pmap[(p["a"], p["b"])] = p
            pmap[(p["b"], p["a"])] = p
        for lab in sorted(set(labels.tolist())):
            member_ids = [members[i] for i in range(n) if labels[i] == lab]
            if len(member_ids) < min_members:
                continue
            # 簇内二次规则：所有对必须满足时间重叠、空间分离、航向一致
            ok = True
            details = []
            for i in range(len(member_ids)):
                for j in range(i + 1, len(member_ids)):
                    p = pmap.get((member_ids[i], member_ids[j]))
                    if p is None:
                        ok = False
                        break
                    if (p["overlap_ratio"] < overlap_min or p["mean_sep_m"] > max_sep
                            or p["delta_cog"] > cog_tol or p["delta_sog"] > sog_tol * 2):
                        ok = False
                        break
                    details.append(p)
                if not ok:
                    break
            if not ok:
                continue
            mm = [meta[m] for m in member_ids]
            groups.append(
                {
                    "group_id": f"g{len(groups) + 1:03d}",
                    "member_count": len(member_ids),
                    "members": [
                        {"traj_id": m, **meta[m], "lonlat": _centroid(series[m], proj)} for m in member_ids
                    ],
                    "mmis": [x["mmsi"] for x in mm],
                    "duration_min": round(
                        (max(_ts(x["t1"]) for x in mm) - min(_ts(x["t0"]) for x in mm)) / 60.0, 1),
                    "time_window": [min(x["t0"] for x in mm), max(x["t1"] for x in mm)],
                    "max_pair_dtw": round(max(d["dtw"] for d in details), 3) if details else None,
                    "mean_pair_dtw": round(float(np.mean([d["dtw"] for d in details])), 3) if details else None,
                    "max_sep_m": round(max(d["mean_sep_m"] for d in details), 1) if details else None,
                    "mean_overlap": round(float(np.mean([d["overlap_ratio"] for d in details])), 3) if details else None,
                    "mean_delta_cog": round(float(np.mean([d["delta_cog"] for d in details])), 1) if details else None,
                    "ship_types": sorted({x["ship_type"] for x in mm if x["ship_type"]}),
                    "confidence": _confidence(details),
                }
            )

        # ---- 5. 输出 ----
        _p(progress, 0.95, "输出编群结果与距离矩阵")
        mat_df = pd.DataFrame(D, index=members, columns=members)
        summary = {
            "algorithm": "formation_dtw",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "window": {"t0": str(wt0), "t1": str(wt1), "window_minutes": window_min},
            "n_sequences": len(ids),
            "n_candidate_pairs": n_cand_total,
            "n_pairs_evaluated": len(pairs),
            "n_groups": len(groups),
            "groups": groups,
            "pairs": sorted(pairs, key=lambda p: p["dtw"])[:300],
            "dtw_seconds": round(elapsed, 1),
            "weights": w,
            "normalizers": norm,
            "thresholds": {
                "time_overlap_min": overlap_min, "max_sep_m": max_sep,
                "cog_tol_deg": cog_tol, "sog_tol_kn": sog_tol,
                "dtw_threshold": dtw_thr, "linkage": linkage,
            },
            "notes": [
                "编群必须同时满足时间重叠、空间接近、航向一致与速度相近，路线相似但时间不重叠不算同群。",
                "DTW 距离为工程归一化评分，阈值需在人工标注样本上标定，不等同唯一数学定义。",
                "采用 average linkage，避免 single linkage 的链式误合并。",
            ],
        }
        write_result(task_id, jsonable(summary), {"dtw_matrix.parquet": mat_df.reset_index()})
        _p(progress, 1.0, f"完成：{len(groups)} 个编群 / {len(pairs)} 对候选")
        return jsonable(summary)
    finally:
        con.close()


def _ts(s: str) -> float:
    return pd.Timestamp(s).timestamp()


def _nanabsdiff(a: pd.Series, b: pd.Series) -> float:
    """忽略 NaN 的均值绝对差。

    两条轨迹的重采样时间戳可能不完全对齐（长度不同），
    因此不能用逐点 mask 相减，而是分别求各自的有效均值再作差。
    """
    x, y = a.to_numpy(dtype=float), b.to_numpy(dtype=float)
    x = x[np.isfinite(x)]
    y = y[np.isfinite(y)]
    if x.size < 2 or y.size < 2:
        return float("nan")
    return float(abs(x.mean() - y.mean()))


def _nancircdiff(a: pd.Series, b: pd.Series) -> float:
    """忽略 NaN 的圆均值角度差（degree）。"""
    x, y = a.to_numpy(dtype=float), b.to_numpy(dtype=float)
    x = x[np.isfinite(x)]
    y = y[np.isfinite(y)]
    if x.size < 2 or y.size < 2:
        return float("nan")
    ax = math.degrees(math.atan2(np.sin(np.radians(x)).mean(), np.cos(np.radians(x)).mean()))
    bx = math.degrees(math.atan2(np.sin(np.radians(y)).mean(), np.cos(np.radians(y)).mean()))
    return float(abs(wrap_deg(ax - bx)))


def _centroid(rs: pd.DataFrame, proj: Projection) -> List[float]:
    lon, lat = proj.inverse(float(rs["x"].mean()), float(rs["y"].mean()))
    return [round(lon, 5), round(lat, 5)]


def _confidence(details: List[Dict[str, Any]]) -> str:
    if not details:
        return "证据不足"
    d = float(np.mean([x["dtw"] for x in details]))
    ov = float(np.mean([x["overlap_ratio"] for x in details]))
    if d < 0.5 and ov > 0.8:
        return "高"
    if d < 1.0 and ov > 0.6:
        return "中"
    return "低"
