"""船舶目标属性分析与属性关联分析。

属性体系（技术方案第 6 章）：速度、航向、活动区域、驻留行为、时间行为、轨迹复杂度。
关联分析（第 7 章）：以 MMSI / 航次为独立样本单位计算相关矩阵，避免逐点伪重复；
多重检验使用 Benjamini-Hochberg 校正；同时给出滞后关系曲线。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from .common import Filter, dataset_extent, jsonable, open_dataset, write_result
from ..geo import circular_mean_deg, wrap_deg

ProgressCb = Optional[Callable[[float, str], None]]


def _p(cb: ProgressCb, v: float, s: str) -> None:
    if cb:
        cb(v, s)


# ============================================================ 单船画像

def vessel_profile(
    dataset_version: str,
    task_id: str,
    params: Dict[str, Any],
    filters: Optional[Dict[str, Any]] = None,
    progress: ProgressCb = None,
    mmsi: Optional[str] = None,
) -> Dict[str, Any]:
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        where = flt.where()
        if mmsi:
            where += f" AND mmsi = '{str(mmsi).replace(chr(39), chr(39)*2)}'"
        _p(progress, 0.15, "统计船舶基础属性")
        base = con.execute(
            f"""
            SELECT mmsi,
                   any_value(coalesce(nullif(name,''),'未知')) AS name,
                   any_value(coalesce(ship_type,'Unknown')) AS ship_type,
                   any_value(coalesce(mobile_type,'Unknown')) AS mobile_type,
                   any_value(length) AS length, any_value(width) AS width,
                   any_value(draught) AS draught, any_value(imo) AS imo,
                   any_value(callsign) AS callsign, any_value(destination) AS destination,
                   count(*) AS points, count(DISTINCT traj_id) AS segments,
                   min(event_time) AS t0, max(event_time) AS t1,
                   count(DISTINCT CAST(event_time AS DATE)) AS active_days,
                   round(avg(sog), 2) AS mean_sog,
                   round(median(sog), 2) AS median_sog,
                   round(max(sog), 2) AS max_sog,
                   round(quantile_cont(sog, 0.95), 2) AS p95_sog,
                   round(avg(CASE WHEN sog < 1 THEN 1.0 ELSE 0 END), 3) AS low_speed_ratio,
                   round(min(lat), 5) AS min_lat, round(max(lat), 5) AS max_lat,
                   round(min(lon), 5) AS min_lon, round(max(lon), 5) AS max_lon,
                   round(avg(quality_score), 1) AS avg_quality
            FROM clean_points WHERE {where}
            GROUP BY mmsi ORDER BY points DESC
            """
        ).fetchdf()
        if base.empty:
            return {"error": "筛选条件下没有船舶数据"}

        _p(progress, 0.35, "计算航向圆统计与航程")
        circ = con.execute(
            f"""
            SELECT mmsi,
                   round(atan2(avg(sin(radians(cog))), avg(cos(radians(cog)))) * 180 / pi(), 1) AS main_cog,
                   round(sqrt(pow(avg(sin(radians(cog))),2) + pow(avg(cos(radians(cog))),2)), 3) AS cog_concentration
            FROM clean_points WHERE {where} AND cog IS NOT NULL GROUP BY mmsi
            """
        ).fetchdf()
        base = base.merge(circ, on="mmsi", how="left")

        _p(progress, 0.5, "计算航程与活动范围")
        # 说明：航程只统计“位移隐含速度不超过 60 kn”的相邻点对，避免 GPS 漂移
        # （如基站/系泊设备的定位抖动）把累计航程放大成天文数字；
        # avg_speed_kn 采用“有效位移总里程 / 有效位移总时长”的加权均值，
        # 单位换算：m/s -> kn 的系数为 1.943844。
        ext = con.execute(
            f"""
            WITH step AS (
                SELECT mmsi,
                       2 * 6371008.8 * asin(least(1.0, sqrt(
                         pow(sin(radians(lat - lag(lat) OVER w) / 2), 2) +
                         cos(radians(lag(lat) OVER w)) * cos(radians(lat)) *
                         pow(sin(radians(lon - lag(lon) OVER w) / 2), 2)))) AS d,
                       date_diff('second', lag(event_time) OVER w, event_time) AS dt
                FROM clean_points WHERE {where}
                WINDOW w AS (PARTITION BY mmsi ORDER BY event_time)
            ), valid AS (
                SELECT mmsi, d, dt FROM step
                WHERE dt > 0 AND dt <= 3600 AND d IS NOT NULL AND d / dt * 1.943844 <= 60.0
            )
            SELECT mmsi,
                   round(sum(d) / 1000.0, 1) AS distance_km,
                   round(sum(d) / nullif(sum(dt), 0) * 1.943844, 2) AS avg_speed_kn
            FROM valid GROUP BY mmsi
            """
        ).fetchdf()
        base = base.merge(ext, on="mmsi", how="left")

        _p(progress, 0.62, "汇总驻留行为")
        stays = _stay_summary(con, dataset_version, where)
        if stays is not None and not stays.empty:
            base = base.merge(stays, on="mmsi", how="left")
        for c in ("stay_count", "stay_hours", "avg_stay_min"):
            if c not in base.columns:
                base[c] = 0

        _p(progress, 0.72, "时间行为与轨迹复杂度")
        hourly = con.execute(
            f"""SELECT mmsi, hour(event_time) AS h, count(*) AS n FROM clean_points
                WHERE {where} GROUP BY 1,2"""
        ).fetchdf()
        dow = con.execute(
            f"""SELECT mmsi, dayofweek(event_time) AS d, count(*) AS n FROM clean_points
                WHERE {where} GROUP BY 1,2"""
        ).fetchdf()

        top_n = int(params.get("top_n", 50))
        prof = base.head(top_n).copy()
        prof["activity_span_km"] = (
            np.sqrt(
                ((prof["max_lon"] - prof["min_lon"]) * 111.32 * np.cos(np.radians(prof["max_lat"])) ) ** 2
                + ((prof["max_lat"] - prof["min_lat"]) * 110.57) ** 2
            )
        ).round(1)
        prof["turn_rate"] = (prof["segments"] / prof["active_days"].replace(0, np.nan)).round(2)

        # 单船明细（画雷达/曲线用）：与 hist 同步扩到 100 艘，结果体积仍然很小
        detail: List[Dict[str, Any]] = []
        for r in prof.head(100).itertuples():
            hh = hourly[hourly["mmsi"] == r.mmsi].sort_values("h")
            dd = dow[dow["mmsi"] == r.mmsi].sort_values("d")
            detail.append(
                {
                    "mmsi": r.mmsi,
                    "hour_profile": [{"h": int(x.h), "n": int(x.n)} for x in hh.itertuples()],
                    "dow_profile": [{"d": int(x.d), "n": int(x.n)} for x in dd.itertuples()],
                }
            )

        _p(progress, 0.85, "计算速度/航向分布")
        # 只对“有有效航速记录”的头部船舶计算分布，避免把无 SOG 的基站/AtoN 混进来；
        # 上限 100 艘，保证前端选中任意列表船只要能看到自己的分布，同时控制结果体积。
        hist_pool = prof[prof["median_sog"].notna()]
        hist_target = hist_pool["mmsi"].head(100).tolist()
        if not hist_target:
            hist_target = prof["mmsi"].head(100).tolist()
        if hist_target:
            lst = ",".join("'" + str(m).replace("'", "''") + "'" for m in hist_target)
            spd = con.execute(
                f"""SELECT mmsi, CAST(floor(least(sog, 30)) AS INT) AS k, count(*) AS n
                    FROM clean_points WHERE {where} AND mmsi IN ({lst}) AND sog IS NOT NULL
                    GROUP BY 1,2 ORDER BY 1,2"""
            ).fetchdf()
            cogg = con.execute(
                f"""SELECT mmsi, CAST(floor(cog/10)*10 AS INT) AS k, count(*) AS n
                    FROM clean_points WHERE {where} AND mmsi IN ({lst}) AND cog IS NOT NULL
                    GROUP BY 1,2 ORDER BY 1,2"""
            ).fetchdf()
        else:
            spd, cogg = pd.DataFrame(), pd.DataFrame()

        summary = {
            "algorithm": "vessel_profile",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "n_vessels": int(len(base)),
            "totals": {
                "points": int(base["points"].sum()),
                "distance_km": round(float(base["distance_km"].fillna(0).sum()), 1),
                "stay_count": int(base["stay_count"].fillna(0).sum()),
            },
            "vessels": prof.to_dict(orient="records"),
            "profiles": detail,
            "speed_hist": spd.to_dict(orient="records"),
            "cog_hist": cogg.to_dict(orient="records"),
            "units": {"sog": "kn", "distance": "km", "length": "m", "draught": "m"},
        }
        write_result(task_id, jsonable(summary), {"vessel_profile.parquet": prof})
        _p(progress, 1.0, f"完成：{len(base)} 艘船舶画像")
        return jsonable(summary)
    finally:
        con.close()


def _stay_summary(con, dataset_version: str, where: str):
    from ..clean import staypoints_path
    p = staypoints_path(dataset_version)
    if not p.exists():
        return None
    return con.execute(
        f"""
        SELECT s.mmsi, count(*) AS stay_count, round(sum(s.duration_min)/60.0, 1) AS stay_hours,
               round(avg(s.duration_min), 1) AS avg_stay_min
        FROM read_parquet('{p.as_posix()}') s
        JOIN (SELECT DISTINCT mmsi FROM clean_points WHERE {where}) c USING (mmsi)
        GROUP BY 1
        """
    ).fetchdf()


# ============================================================ 属性关联

def correlate(
    dataset_version: str,
    task_id: str,
    params: Dict[str, Any],
    filters: Optional[Dict[str, Any]] = None,
    progress: ProgressCb = None,
) -> Dict[str, Any]:
    """属性关联分析：以船舶（或航次）为独立样本，避免逐点伪重复。"""
    flt = Filter(filters)
    con = open_dataset(dataset_version)
    try:
        where = flt.where()
        _p(progress, 0.2, "按船舶聚合属性样本")
        unit = str(params.get("unit", "mmsi"))
        key = "mmsi" if unit == "mmsi" else "traj_id"
        df = con.execute(
            f"""
            SELECT {key} AS unit_id, any_value(ship_type) AS ship_type,
                   count(*) AS points,
                   avg(sog) AS mean_sog, median(sog) AS median_sog,
                   stddev_pop(sog) AS std_sog,
                   max(sog) AS max_sog,
                   avg(CASE WHEN sog < 1 THEN 1.0 ELSE 0 END) AS low_speed_ratio,
                   sqrt(pow(avg(sin(radians(cog))),2)+pow(avg(cos(radians(cog))),2)) AS cog_concentration,
                   avg(heading) AS mean_heading,
                   avg(coalesce(draught,0)) AS mean_draught,
                   avg(coalesce(length,0)) AS mean_length,
                   avg(coalesce(width,0)) AS mean_width,
                   count(DISTINCT hour(event_time)) AS active_hours,
                   date_diff('second', min(event_time), max(event_time)) / 3600.0 AS span_hours,
                   count(*) / greatest(1, date_diff('second', min(event_time), max(event_time)) / 3600.0) AS report_rate,
                   count(DISTINCT traj_id) AS segments,
                   avg(quality_score) AS quality
            FROM clean_points WHERE {where}
            GROUP BY {key} HAVING count(*) >= {int(params.get('min_samples', 30))}
            """
        ).fetchdf()

        if len(df) < 12:
            return {"error": f"独立样本不足（{len(df)}），无法计算稳定相关性，请扩大筛选范围"}

        feats = {
            "mean_sog": "平均航速(kn)", "std_sog": "航速标准差", "max_sog": "最大航速(kn)",
            "low_speed_ratio": "低速时间占比", "span_hours": "活动时长(h)",
            "report_rate": "上报频率(点/h)", "segments": "轨迹段数",
            "mean_length": "船长(m)", "mean_draught": "平均吃水(m)",
            "active_hours": "活跃小时数", "cog_concentration": "航向集中度",
        }
        cols = [c for c in feats if c in df.columns and df[c].notna().sum() > 10][: int(params.get("max_features", 10))]
        data = df[cols].astype(float)

        method = str(params.get("method", "spearman"))
        _p(progress, 0.45, f"计算 {method} 相关矩阵与显著性")
        from scipy import stats

        n = len(data)
        k = len(cols)
        r = np.full((k, k), np.nan)
        pmat = np.full((k, k), np.nan)
        for i in range(k):
            for j in range(i, k):
                a, b = data[cols[i]], data[cols[j]]
                m = a.notna() & b.notna()
                if m.sum() < 12:
                    continue
                if method == "pearson":
                    rv, pv = stats.pearsonr(a[m], b[m])
                elif method == "kendall":
                    rv, pv = stats.kendalltau(a[m], b[m])
                else:
                    rv, pv = stats.spearmanr(a[m], b[m])
                r[i, j] = r[j, i] = rv
                pmat[i, j] = pmat[j, i] = pv

        # Benjamini-Hochberg 校正
        _p(progress, 0.6, "多重检验校正（BH）")
        pairs = []
        for i in range(k):
            for j in range(i + 1, k):
                if not np.isnan(pmat[i, j]):
                    pairs.append((i, j, float(r[i, j]), float(pmat[i, j])))
        adj = _bh([pp[3] for pp in pairs]) if pairs else []

        edges = []
        for (i, j, rv, pv), q in zip(pairs, adj):
            edges.append(
                {
                    "source": cols[i], "target": cols[j],
                    "source_label": feats[cols[i]], "target_label": feats[cols[j]],
                    "r": round(rv, 4), "p_value": round(pv, 6), "q_value": round(float(q), 6),
                    "significant": bool(q < 0.05),
                    "direction": "正相关" if rv > 0 else "负相关",
                    "strength": _strength(abs(rv)),
                }
            )
        edges.sort(key=lambda e: -abs(e["r"]))

        # 滞后关系（限速与时长等时序变量）
        _p(progress, 0.75, "计算滞后相关曲线")
        lag = _lag_curve(con, where, key, params)

        summary = {
            "algorithm": "correlate",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "unit": key,
            "n_samples": int(n),
            "features": [{"key": c, "label": feats[c]} for c in cols],
            "matrix": [[None if np.isnan(v) else round(float(v), 4) for v in row] for row in r],
            "p_matrix": [[None if np.isnan(v) else round(float(v), 6) for v in row] for row in pmat],
            "edges": edges,
            "lag": lag,
            "correction": {
                "method": "Benjamini-Hochberg",
                "n_tests": len(pairs),
                "n_significant_raw": int(sum(1 for e in edges if e["p_value"] < 0.05)),
                "n_significant_adj": int(sum(1 for e in edges if e["significant"])),
            },
            "notes": [
                "样本单位为船舶或航次，已避免逐点分析的伪重复问题。",
                "结论使用“正/负相关、统计依赖”，未经因果识别设计，不表示因果。",
                "已对全部两两检验执行 BH 多重检验校正，请以 q_value 判断显著性。",
            ],
        }
        write_result(task_id, jsonable(summary), {"correlation_matrix.parquet": pd.DataFrame(r, index=cols, columns=cols)})
        _p(progress, 1.0, f"完成：{n} 个独立样本，{len(edges)} 组变量对")
        return jsonable(summary)
    finally:
        con.close()


def _strength(v: float) -> str:
    if v >= 0.7:
        return "强"
    if v >= 0.4:
        return "中等"
    if v >= 0.2:
        return "弱"
    return "极弱"


def _bh(pvals: List[float]) -> List[float]:
    """Benjamini-Hochberg FDR 校正。"""
    if not pvals:
        return []
    n = len(pvals)
    order = np.argsort(pvals)
    q = np.empty(n)
    prev = 1.0
    for rank, idx in enumerate(order[::-1]):
        i = n - rank
        val = min(prev, pvals[idx] * n / i)
        q[idx] = val
        prev = val
    return list(np.clip(q, 0, 1))


def _lag_curve(con, where: str, key: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """以固定时间桶聚合活跃船数与平均航速，计算 0..N 桶滞后相关。"""
    try:
        agg = con.execute(
            f"""
            SELECT floor(epoch(event_time) / 3600) AS bucket,
                   count(DISTINCT mmsi) AS vessels, avg(sog) AS mean_sog, count(*) AS reports
            FROM clean_points WHERE {where} GROUP BY 1 ORDER BY 1
            """
        ).fetchdf()
        if len(agg) < 12:
            return {"available": False, "reason": "时间桶过少"}
        from scipy import stats

        out = []
        for lg in range(0, min(7, len(agg) // 3)):
            a = agg["vessels"].to_numpy(dtype=float)
            b = agg["mean_sog"].to_numpy(dtype=float)
            if lg == 0:
                x, y = a, b
            else:
                x, y = a[:-lg], b[lg:]
            m = ~(np.isnan(x) | np.isnan(y))
            if m.sum() < 10:
                continue
            rv, pv = stats.spearmanr(x[m], y[m])
            out.append({"lag_hours": lg, "r": round(float(rv), 4), "p_value": round(float(pv), 6)})
        return {"available": True, "series": out, "x": "活跃船舶数", "y": "平均航速", "bucket": "1 小时"}
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
