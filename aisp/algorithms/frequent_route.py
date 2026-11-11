"""频繁轨迹挖掘：规则网格 → 高频网格 → 有向转移边 → 连续网格序列（频繁航线）。

严格按技术方案第 9 章的统计口径实现：
- 网格在局部米制投影内划分，left-closed/right-open、bottom-closed/top-open，外边界归入最后一行/列；
- counting_unit 默认 trajectory_segment_id（无法识别真实港到港航次时的代理口径，结果中明确标注）；
- 支持度一律按「不同航次数」去重：同一航次在同一网格产生多少报文都只计 1；
- 方向必须保留：u→v 与 v→u 是两条不同的边；
- 连续重复压缩后仍保留 A-B-A；不同航次绝不拼接，断点处切断序列；
- 连续 n-gram 滑动窗口挖掘频繁航线，同一航次对同一模式的贡献只计 1，occurrence_count 另存；
- 推断网格（DDA 穿越）默认关闭，启用时必须通过时间/速度/位移质量门槛并单独标记。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .common import Filter, dataset_extent, jsonable, open_dataset, write_result
from ..geo import GridSpec, compress_consecutive, grid_from_cell_size, grid_from_rows_cols, traverse_cells

ProgressCb = Optional[Callable[[float, str], None]]


def _p(cb: ProgressCb, v: float, s: str) -> None:
    if cb:
        cb(v, s)


def _resolve_extent(con, flt: Filter, params: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """分析范围解析：custom（用户 bbox）> percentile（去除偏远孤点）> data（全量）。

    AIS 数据常含远离作业区的异常/越界点，直接用 min/max 会把网格拉得极大；
    percentile 模式默认取 0.5%/99.5% 分位，使网格密度更贴近实际作业海域。
    """
    mode = str(params.get("extent_mode", "percentile"))
    if flt.bbox:
        return tuple(float(x) for x in flt.bbox[:4])
    where = flt.where()
    if mode == "data":
        return dataset_extent(con, flt)
    q = float(params.get("extent_quantile", 0.005))
    row = con.execute(
        f"""SELECT quantile_cont(lon, [{q}, {1 - q}]), quantile_cont(lat, [{q}, {1 - q}]), count(*)
            FROM clean_points WHERE {where}"""
    ).fetchone()
    if not row or row[2] == 0:
        return None
    lonq, latq = row[0], row[1]
    return (float(lonq[0]), float(latq[0]), float(lonq[1]), float(latq[1]))


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
        extent = _resolve_extent(con, flt, params)
        if extent is None:
            return {"error": "筛选条件下没有数据"}
        buf = float(params.get("extent_buffer_deg", 0.0) or 0.0)
        min_lon, min_lat = extent[0] - buf, extent[1] - buf
        max_lon, max_lat = extent[2] + buf, extent[3] + buf

        grid_mode = str(params.get("grid_mode", "rows_cols"))
        version_seed = f"{dataset_version}|{min_lon:.5f},{min_lat:.5f},{max_lon:.5f},{max_lat:.5f}"
        if grid_mode == "fixed_size":
            cell = float(params.get("cell_size_m", 1000.0))
            version = "g" + hashlib.md5(f"{version_seed}|cell{cell}".encode()).hexdigest()[:10]
            grid = grid_from_cell_size(min_lon, min_lat, max_lon, max_lat, cell, version)
        else:
            rows = int(params.get("grid_rows", 10))
            cols = int(params.get("grid_cols", 10))
            version = "g" + hashlib.md5(f"{version_seed}|{rows}x{cols}".encode()).hexdigest()[:10]
            grid = grid_from_rows_cols(min_lon, min_lat, max_lon, max_lat, rows, cols, version)

        unit = str(params.get("counting_unit", "trajectory_segment_id"))
        unit_col = "traj_id" if unit.startswith("traj") else "mmsi"
        min_support = int(params.get("min_support_count", 3))
        min_ratio = float(params.get("min_support_ratio", 0.05))
        lmin = max(2, int(params.get("min_pattern_len", 3)))
        lmax = max(lmin, int(params.get("max_pattern_len", 6)))
        max_gap_s = float(params.get("max_gap_seconds", 3600.0))
        max_jump_m = float(params.get("max_jump_m", 20000.0))
        enable_traverse = bool(params.get("enable_grid_traversal", False))
        top_k = int(params.get("top_k", 20))

        _p(progress, 0.1, f"生成规则网格（{grid.rows}×{grid.cols}，单元 {grid.cell_width:.0f}×{grid.cell_height:.0f} m）")

        # ---- 1. 读取航次序列（按 unit + 轨迹段分组） ----
        _p(progress, 0.2, "读取有序轨迹点")
        where = flt.where()
        df = con.execute(
            f"""
            SELECT {unit_col} AS unit_id, traj_id, mmsi, event_time, lon, lat, sog,
                   coalesce(quality_score, 0) AS quality_score
            FROM clean_points
            WHERE {where} AND lon BETWEEN {min_lon!r} AND {max_lon!r}
                                 AND lat BETWEEN {min_lat!r} AND {max_lat!r}
            ORDER BY {unit_col}, traj_id, event_time
            """
        ).fetchdf()
        if df.empty:
            return {"error": "范围内没有轨迹点"}

        # ---- 2. 点映射 ----
        _p(progress, 0.35, "AIS 点到网格映射与序列生成")
        rows_c = np.empty(len(df), dtype=np.int32)
        cols_c = np.empty(len(df), dtype=np.int32)
        inarea = np.zeros(len(df), dtype=bool)
        lon_v = df["lon"].to_numpy(dtype=np.float64)
        lat_v = df["lat"].to_numpy(dtype=np.float64)
        # 向量化行列计算（与 GridSpec.locate 同规则）
        x = (lon_v - grid.proj.lon0) * grid.proj.kx
        y = (lat_v - grid.proj.lat0) * grid.proj.ky
        eps = 1e-9
        ok = (x >= grid.x_min - eps) & (x <= grid.x_max + eps) & (y >= grid.y_min - eps) & (y <= grid.y_max + eps)
        col_f = np.where(x >= grid.x_max - eps, grid.cols - 1, np.floor((x - grid.x_min) / grid.cell_width))
        row_f = np.where(y <= grid.y_min + eps, grid.rows - 1, np.floor((grid.y_max - y) / grid.cell_height))
        rows_c[:] = np.clip(np.nan_to_num(row_f, nan=0), 0, grid.rows - 1).astype(np.int32)
        cols_c[:] = np.clip(np.nan_to_num(col_f, nan=0), 0, grid.cols - 1).astype(np.int32)
        inarea[:] = ok
        gid = rows_c.astype(np.int64) * grid.cols + cols_c
        df["grid_id"] = np.where(inarea, gid, -1)
        df["in_area"] = inarea
        out_of_area = int((~inarea).sum())
        df = df[df["in_area"]].copy()
        if df.empty:
            return {"error": "所有点均在网格范围外，请检查分析范围"}

        # ---- 3. 航次级序列：压缩 + 断点隔离 + 可选穿越补格 ----
        _p(progress, 0.5, "连续重复压缩、断点隔离与（可选）穿越补格")
        seq_store: Dict[str, Dict[str, Any]] = {}
        traverse_stats = {"segments_enabled": 0, "inferred_cells": 0}
        for (uid, tid), g in df.groupby(["unit_id", "traj_id"], sort=False):
            ev = g["event_time"].to_numpy(dtype="datetime64[s]").astype("int64")
            gids = g["grid_id"].to_numpy(dtype=np.int64)
            lons = g["lon"].to_numpy(dtype=np.float64)
            lats = g["lat"].to_numpy(dtype=np.float64)
            if len(gids) < 2:
                continue
            # 断点切分为多段（同一航次内的缺口不拼接）
            segs_idx: List[Tuple[int, int]] = []
            start = 0
            for i in range(1, len(gids)):
                dt = ev[i] - ev[i - 1]
                if dt > max_gap_s or dt <= 0:
                    segs_idx.append((start, i - 1))
                    start = i
                    continue
                # 跳点：位移/时间推算速度过大
                d = _hav(lons[i - 1], lats[i - 1], lons[i], lats[i])
                if d > max_jump_m or d / max(dt, 1) > 40.0:
                    segs_idx.append((start, i - 1))
                    start = i
            segs_idx.append((start, len(gids) - 1))

            unit_seqs: List[List[int]] = []
            unit_obs: List[int] = []
            for a, b in segs_idx:
                if b - a < 1:
                    continue
                sub_g = gids[a:b + 1]
                seq = compress_consecutive(sub_g.tolist())
                obs = len(set(seq))
                if enable_traverse and len(sub_g) >= 2:
                    expanded: List[int] = []
                    for k in range(len(sub_g) - 1):
                        dt = ev[a + k + 1] - ev[a + k]
                        d = _hav(lons[a + k], lats[a + k], lons[a + k + 1], lats[a + k + 1])
                        okq = (dt > 0 and dt <= 600 and d <= 5000)
                        if okq:
                            path = traverse_cells(grid, lons[a + k], lats[a + k], lons[a + k + 1], lats[a + k + 1])
                            if len(path) > 1:
                                traverse_stats["inferred_cells"] += len(path) - 2
                                expanded.extend(path[:-1])
                                continue
                        expanded.append(int(sub_g[k]))
                    expanded.append(int(sub_g[-1]))
                    seq = compress_consecutive(expanded)
                    traverse_stats["segments_enabled"] += 1
                if len(seq) >= 2:
                    unit_seqs.append(seq)
                    unit_obs.append(obs)
            if unit_seqs:
                seq_store[str(uid)] = {"traj_ids": [str(tid)], "seqs": unit_seqs, "obs": unit_obs}

        # 同一 unit（MMSI）可能含多个 traj 段，合并但保留段边界
        n_units = len(seq_store)
        if n_units == 0:
            return {"error": "没有生成有效网格序列，请检查断点阈值或网格范围"}

        # ---- 4. 高频网格与有向转移边 ----
        _p(progress, 0.62, "统计高频网格与有向转移边")
        cell_support: Dict[int, set] = defaultdict(set)
        cell_points: Counter = Counter()
        cell_visits: Counter = Counter()
        edge_support: Dict[Tuple[int, int], set] = defaultdict(set)
        edge_events: Counter = Counter()
        for uid, rec in seq_store.items():
            cells_this: set = set()
            edges_this: set = set()
            for seq in rec["seqs"]:
                for g in seq:
                    cells_this.add(g)
                    cell_visits[g] += 1
                for u, v in zip(seq, seq[1:]):
                    if u != v:
                        edges_this.add((u, v))
            for g in cells_this:
                cell_support[g].add(uid)
            for e in edges_this:
                edge_support[e].add(uid)
                edge_events[e] += 1
        point_counts = df.groupby("grid_id").size().to_dict()
        mmsi_per_cell = df.groupby("grid_id")["mmsi"].nunique().to_dict()

        cells_df = pd.DataFrame(
            [
                {
                    "grid_id": int(g),
                    "row_no": grid.from_grid_id(g)[0],
                    "col_no": grid.from_grid_id(g)[1],
                    "distinct_voyage_count": len(cell_support[g]),
                    "support_ratio": round(len(cell_support[g]) / n_units, 4),
                    "distinct_mmsi_count": int(mmsi_per_cell.get(g, 0)),
                    "observed_point_count": int(point_counts.get(g, 0)),
                    "visit_event_count": int(cell_visits[g]),
                }
                for g in sorted(cell_support)
            ]
        ).sort_values("distinct_voyage_count", ascending=False).reset_index(drop=True)

        edges_df = pd.DataFrame(
            [
                {
                    "from_grid": int(u), "to_grid": int(v),
                    "distinct_voyage_count": len(s),
                    "support_ratio": round(len(s) / n_units, 4),
                    "transition_event_count": int(edge_events[(u, v)]),
                }
                for (u, v), s in edge_support.items()
                if len(s) >= max(1, min_support - 1)
            ]
        ).sort_values("distinct_voyage_count", ascending=False).reset_index(drop=True) if edge_support else pd.DataFrame()

        # ---- 5. 连续 n-gram 频繁航线 ----
        _p(progress, 0.75, f"挖掘连续网格序列（长度 {lmin}-{lmax}）")
        pattern_units: Dict[Tuple[int, ...], set] = defaultdict(set)
        pattern_occ: Counter = Counter()
        for uid, rec in seq_store.items():
            local: set = set()
            for seq in rec["seqs"]:
                n = len(seq)
                for L in range(lmin, lmax + 1):
                    if n < L:
                        break
                    for i in range(n - L + 1):
                        pat = tuple(seq[i:i + L])
                        local.add(pat)
                        pattern_occ[pat] += 1
            for pat in local:
                pattern_units[pat].add(uid)

        routes = []
        for pat, units in pattern_units.items():
            sup = len(units)
            ratio = sup / n_units
            if sup < min_support or ratio < min_ratio:
                continue
            routes.append({"seq": pat, "support": sup, "ratio": ratio, "occ": pattern_occ[pat], "units": units})
        # 优先支持度，其次长度（更长更能代表完整路线）
        routes.sort(key=lambda r: (r["support"], len(r["seq"]), r["occ"]), reverse=True)

        # 去重：若某模式是同支持度的更长模式的子串，则保留更长者
        kept: List[Dict[str, Any]] = []
        kept_sets: List[Tuple[Tuple[int, ...], set]] = []
        for r in routes:
            redundant = False
            for pat2, units2 in kept_sets:
                if r["support"] <= len(units2) and _is_contiguous_subseq(r["seq"], pat2):
                    redundant = True
                    break
            if redundant:
                continue
            kept.append(r)
            kept_sets.append((r["seq"], r["units"]))
            if len(kept) >= top_k * 4:
                break

        _p(progress, 0.85, "计算路线几何与支持航次证据")
        unit_median_t = df.groupby(df["unit_id"].astype(str))["event_time"].median().to_dict()
        unit_mmsi = df.groupby(df["unit_id"].astype(str))["mmsi"].nunique().to_dict()
        route_out: List[Dict[str, Any]] = []
        for rank, r in enumerate(kept[:top_k], 1):
            pts = [grid.cell_center(*grid.from_grid_id(g)) for g in r["seq"]]
            mids = [unit_median_t[u] for u in r["units"] if u in unit_median_t]
            route_out.append(
                {
                    "pattern_id": "r" + hashlib.md5(str(r["seq"]).encode()).hexdigest()[:10],
                    "rank": rank,
                    "grid_sequence": list(r["seq"]),
                    "length": len(r["seq"]),
                    "support_count": r["support"],
                    "support_ratio": round(r["ratio"], 4),
                    "occurrence_count": r["occ"],
                    "distinct_mmsi_count": int(sum(unit_mmsi.get(u, 0) for u in r["units"])),
                    "route_geometry": [[round(p[0], 6), round(p[1], 6)] for p in pts],
                    "median_travel_time": str(pd.Series(mids).median()) if mids else None,
                    "matching_units": sorted(r["units"])[:50],
                }
            )

        # ---- 6. 结果落盘 ----
        _p(progress, 0.93, "输出网格定义、统计与路线文件")
        grid_geo = grid.cells_geojson()
        for f in grid_geo["features"]:
            g = f["properties"]["grid_id"]
            f["properties"]["distinct_voyage_count"] = int(
                cells_df.loc[cells_df["grid_id"] == g, "distinct_voyage_count"].iloc[0]
            ) if not cells_df.empty and (cells_df["grid_id"] == g).any() else 0
            f["properties"]["support_ratio"] = float(
                cells_df.loc[cells_df["grid_id"] == g, "support_ratio"].iloc[0]
            ) if not cells_df.empty and (cells_df["grid_id"] == g).any() else 0.0

        summary = {
            "algorithm": "frequent_route",
            "dataset_version": dataset_version,
            "filters": flt.to_dict(),
            "params": params,
            "grid": grid.to_dict(),
            "counting_unit": unit,
            "counting_unit_note": (
                "以轨迹段作为航次代理：当前数据无法可靠识别真实港到港航次，"
                "支持度分母为有效轨迹段数。" if unit.startswith("traj") else
                "以 MMSI 作为统计单位。"
            ),
            "n_units": n_units,
            "n_cells": grid.n_cells,
            "out_of_area_points": out_of_area,
            "n_observed_cells": int(len(cells_df)),
            "n_edges": int(len(edges_df)),
            "n_routes": len(route_out),
            "cells": cells_df.head(200).to_dict(orient="records"),
            "cells_top": cells_df.head(20).to_dict(orient="records"),
            "edges": edges_df.head(400).to_dict(orient="records"),
            "routes": route_out,
            "traversal": traverse_stats,
            "units": {"grid": "grid_id = row_no * cols + col_no", "support": "不同航次数"},
            "notes": [
                "支持度一律按不同航次数去重：同一航次在同一网格产生更多 AIS 报文不增加支持度。",
                "方向保留：u→v 与 v→u 为不同转移边，不合并。",
                "连续重复压缩保留 A-B-A，不做全局去重；断点处切断序列，不跨缺口拼接。",
                "第一版默认仅使用观测网格；推断网格需显式开启并单独标记。",
                "本结果为历史 AIS 统计通行模式，不等同官方航道或安全航线。",
            ],
        }
        files = {
            "grid_definition.geojson": grid_geo,
            "grid_cells.parquet": cells_df,
            "grid_edges.parquet": edges_df if not edges_df.empty else pd.DataFrame(),
            "frequent_routes.parquet": pd.DataFrame(
                [{k: v for k, v in r.items() if k != "matching_units"} for r in route_out]
            ) if route_out else pd.DataFrame(),
        }
        write_result(task_id, jsonable(summary), files)
        _p(progress, 1.0, f"完成：{len(cells_df)} 个高频网格、{len(edges_df)} 条转移边、{len(route_out)} 条频繁航线")
        return jsonable(summary)
    finally:
        con.close()


def _hav(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def _is_contiguous_subseq(short: Sequence[int], long: Sequence[int]) -> bool:
    """short 是否为 long 的“连续”子序列（逐元素相等、不跨缺口）。

    注意：与数学意义上的 subsequence（允许挖空）不同，这里要求连续，
    因为航线模式判定中“隔了几个网格再出现”不算同一条航线。
    """
    n, m = len(short), len(long)
    if n > m:
        return False
    for i in range(m - n + 1):
        if tuple(long[i:i + n]) == tuple(short):
            return True
    return False


# 向后兼容的别名：保留旧名，避免外部脚本引用失效
def _is_subseq(short: Sequence[int], long: Sequence[int]) -> bool:
    return _is_contiguous_subseq(short, long)


def _contains_run(seq: Sequence[int], pat: List[int]) -> bool:
    """seq 中是否包含 pat 这一“连续”片段。"""
    return _is_contiguous_subseq(pat, seq)


def _route_mmsi(units: set, df: pd.DataFrame) -> int:
    sub = df[df["unit_id"].astype(str).isin(units)]
    return int(sub["mmsi"].nunique()) if not sub.empty else 0
