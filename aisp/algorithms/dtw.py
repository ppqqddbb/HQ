"""DTW 内核：优先使用 Numba JIT，缺少时回退到纯 NumPy 实现。

多变量局部代价（各项先按物理单位归一化再加权）：
    c = w_pos * dist(normalized) + w_sog * |ΔSOG|/norm_sog
        + w_cog * (1 - cos ΔCOG)/2 + w_time * |Δt|/norm_time

全局约束：Sakoe-Chiba 带（band），分母为对齐路径长度，得到可比的平均代价。
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

try:  # pragma: no cover - 取决于环境
    from numba import njit

    _HAS_NUMBA = True
except Exception:  # pragma: no cover
    _HAS_NUMBA = False

    def njit(*a, **k):
        def deco(f):
            return f

        return deco if not (len(a) == 1 and callable(a[0])) else a[0]


@njit(cache=True, fastmath=True)
def _dtw_core(ax, ay, bx, by, asog, bsg, acg, bcg, at, bt, band, w_pos, w_sog, w_cog, w_time,
              n_pos, n_sog, n_time):
    n = ax.shape[0]
    m = bx.shape[0]
    INF = 1e18
    D = np.full((n + 1, m + 1), INF)
    D[0, 0] = 0.0
    two_pi = 6.283185307179586
    for i in range(1, n + 1):
        lo = i - band
        if lo < 1:
            lo = 1
        hi = i + band
        if hi > m:
            hi = m
        for j in range(lo, hi + 1):
            dx = ax[i - 1] - bx[j - 1]
            dy = ay[i - 1] - by[j - 1]
            dist = math.sqrt(dx * dx + dy * dy)
            c = w_pos * dist / n_pos
            s1 = asog[i - 1]
            s2 = bsg[j - 1]
            if not (math.isnan(s1) or math.isnan(s2)):
                c += w_sog * abs(s1 - s2) / n_sog
            c1 = acg[i - 1]
            c2 = bcg[j - 1]
            if not (math.isnan(c1) or math.isnan(c2)):
                d = (c1 - c2 + 180.0) % 360.0 - 180.0
                c += w_cog * (1.0 - math.cos(d * 0.017453292519943295)) * 0.5
            c += w_time * abs(at[i - 1] - bt[j - 1]) / n_time
            a = D[i - 1, j]
            b = D[i, j - 1]
            e = D[i - 1, j - 1]
            mn = a if a < b else b
            if e < mn:
                mn = e
            D[i, j] = c + mn
    dist = D[n, m]
    if not math.isfinite(dist) or dist >= INF:
        return -1.0, -1.0, 0
    i = n
    j = m
    total_sep = 0.0
    steps = 0
    while i > 0 and j > 0:
        dx = ax[i - 1] - bx[j - 1]
        dy = ay[i - 1] - by[j - 1]
        total_sep += math.sqrt(dx * dx + dy * dy)
        steps += 1
        d1 = D[i - 1, j - 1]
        d2 = D[i - 1, j]
        d3 = D[i, j - 1]
        if d1 <= d2 and d1 <= d3:
            i -= 1
            j -= 1
        elif d2 <= d3:
            i -= 1
        else:
            j -= 1
    if steps == 0:
        return -1.0, -1.0, 0
    return dist / steps, total_sep / steps, steps


def dtw_cost(a, b, band: int, w: dict, norm: dict) -> Tuple[float, float, int]:
    """返回 (归一化平均代价, 平均空间分离米, 对齐步数)；不可达返回 (-1, -1, 0)。"""
    band = max(int(band), abs(len(a) - len(b)) + 1)
    d, sep, steps = _dtw_core(
        a["x"].to_numpy(dtype=np.float64), a["y"].to_numpy(dtype=np.float64),
        b["x"].to_numpy(dtype=np.float64), b["y"].to_numpy(dtype=np.float64),
        a["sog"].to_numpy(dtype=np.float64), b["sog"].to_numpy(dtype=np.float64),
        a["cog"].to_numpy(dtype=np.float64), b["cog"].to_numpy(dtype=np.float64),
        a["t"].to_numpy(dtype=np.float64), b["t"].to_numpy(dtype=np.float64),
        band,
        float(w["pos"]), float(w["sog"]), float(w["cog"]), float(w["time"]),
        float(norm["pos"]), float(norm["sog"]), float(norm["time"]),
    )
    return float(d), float(sep), int(steps)
