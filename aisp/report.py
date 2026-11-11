"""分析报告导出：把一次分析任务的结果汇总为可打印的独立报告（HTML / Markdown）。

设计要点（对应技术方案 11.4「报告导出」与 20.2「可解释性」）：
1. 报告必须标明：生成时间、数据批次/数据版本、任务 ID、算法与代码版本、配置摘要、数据范围；
2. 报告自包含：不引用任何外部 CDN、脚本或字体，离线可打开、可直接打印成 PDF；
3. 表格只放「摘要级」数据（TOP N），明细仍通过结果文件下载，避免报告体积失控；
4. 每类算法都附带口径说明与已知限制，避免把统计模式误读为事实。
"""

from __future__ import annotations

import difflib
import html
import json
import math
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import dataservice, store, tasks
from .config import settings

ALGO_CN = {
    "hdbscan_hotspot": "热点区域分析",
    "vessel_profile": "船舶目标画像",
    "correlate": "属性关联分析",
    "formation_dtw": "船舶编群分析",
    "frequent_route": "频繁轨迹挖掘",
    "predict": "航迹预测与回测",
    "clean": "数据治理",
}

# 每类算法的口径与使用边界（报告中「结论口径」小节）
ALGO_CAVEATS: Dict[str, List[str]] = {
    "hdbscan_hotspot": [
        "热点由历史 AIS 点密度聚类得到，不代表官方航道、锚地或安全航区。",
        "凸包只是密度簇的简化边界，不能直接作为水域边界使用。",
        "交通流热点与停留热点通过低速点占比区分，阈值见参数表。",
    ],
    "vessel_profile": [
        "统计单位为单船（MMSI），逐点汇总前已剔除隐含速度超限的漂移点对。",
        "平均航速 = 有效航程 / 有效航行时长，单位节；累计航程不代表实际对地里程（受采样间隔影响）。",
        "包含基站/岸台等非船舶目标，其航速与航程接近 0，不应计入船舶行为统计。",
    ],
    "correlate": [
        "样本单位是单船或航次，避免 AIS 高频上报造成的逐点伪重复。",
        "相关系数只反映统计依赖，不构成因果关系。",
        "显著性使用 Benjamini-Hochberg 校正，判定阈值 q < 0.05。",
    ],
    "formation_dtw": [
        "编群要求时间重叠、空间接近、航向航速一致且 DTW 距离小于阈值，缺一不可。",
        "静止船舶位置天然接近，可能形成「假编群」，须结合 SOG 与业务经验复核。",
        "DTW 阈值为经验值，需在人工标注集上标定后才能用于业务结论。",
    ],
    "frequent_route": [
        "支持度按独立航次（轨迹段）去重统计，同一航次重复上报不增加支持度。",
        "当前数据无法区分真实挂靠港航次，以轨迹段代理航次，属近似口径。",
        "有向转移 [u→v] 与 [v→u] 视为不同转移；频繁航线为网格级模式，非官方航道。",
    ],
    "predict": [
        "评估使用按窗口起点时间的外推切分（前 70% 训练 / 后 30% 测试），不使用随机切分。",
        "截尾 ADE 为 95% 截尾均值，对少数漂移样本更稳健；平均 ADE 保留长尾影响。",
        "静止船舶样本占比高会拉低整体误差，必须结合分层评估表阅读。",
        "GRU 指标来自单日数据，上线前需在更长时段与独立数据集上复验。",
    ],
    "clean": [
        "原始 CSV 永久保留在 RAW 层，清洗结果另行生成，不可覆盖原始数据。",
        "质量分为五维加权，异常标记分为 ERROR（扣分）与 INFO（仅提示）两类。",
    ],
}

_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:0;background:#f5f7fa;color:#1b2734;
  font:14px/1.65 "Segoe UI","Microsoft YaHei",-apple-system,sans-serif}
.page{max-width:1080px;margin:0 auto;padding:36px 40px 64px;background:#fff;
  box-shadow:0 2px 18px rgba(20,40,70,.08)}
h1{font-size:24px;margin:0 0 6px;letter-spacing:.5px}
h2{font-size:17px;margin:34px 0 12px;padding-bottom:7px;border-bottom:2px solid #e3e9f2}
h3{font-size:14px;margin:20px 0 8px;color:#33475f}
.sub{color:#64748b;font-size:12.5px}
.meta{display:grid;grid-template-columns:repeat(4,1fr);gap:10px 18px;margin:18px 0 8px;
  padding:14px 16px;background:#f8fafc;border:1px solid #e3e9f2;border-radius:8px}
.meta div{font-size:12.5px}
.meta b{display:block;color:#64748b;font-weight:500;font-size:11.5px;margin-bottom:2px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
.kpi{padding:12px 14px;background:#f8fafc;border:1px solid #e3e9f2;border-left:3px solid #0ea5e9;border-radius:6px}
.kpi .k{font-size:11.5px;color:#64748b}
.kpi .v{font-size:20px;font-weight:600;margin-top:3px;letter-spacing:.3px}
.kpi .s{font-size:11px;color:#94a3b8;margin-top:3px}
table{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:12.5px}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef2f7}
th{background:#f1f5f9;color:#475569;font-weight:600;font-size:12px;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:nth-child(even){background:#fbfcfe}
.note{padding:10px 12px;background:#f8fafc;border-left:3px solid #cbd5e1;
  border-radius:0 6px 6px 0;font-size:12.5px;color:#475569;margin:10px 0}
.warn{border-left-color:#f59e0b;background:#fffbeb}
ul.caveats{margin:8px 0;padding-left:20px;font-size:12.5px;color:#475569}
ul.caveats li{margin:4px 0}
.svgwrap{margin:12px 0;border:1px solid #e3e9f2;border-radius:8px;background:#fbfdff;padding:8px}
footer{margin-top:40px;padding-top:14px;border-top:1px solid #e3e9f2;
  font-size:11.5px;color:#94a3b8;display:flex;justify-content:space-between}
@media print{
  body{background:#fff}
  .page{box-shadow:none;max-width:none;padding:0}
  h2{page-break-after:avoid}
  table{page-break-inside:auto}
  tr{page-break-inside:avoid}
}
"""


# --------------------------------------------------------------------------- 工具

def _e(v: Any) -> str:
    """HTML 转义。"""
    return html.escape("" if v is None else str(v))


def _num(v: Any, d: int = 0) -> str:
    """数字格式化；非数值原样返回并转义。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _e(v)
    if d == 0 and abs(f) >= 1000:
        return f"{f:,.0f}"
    if d == 0:
        return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"
    return f"{f:,.{d}f}"


def _iso(v: Any) -> str:
    return "" if v is None else str(v).replace("T", " ")[:19]


def _git_version() -> str:
    """取当前代码提交短哈希，用于报告可追溯性。失败时返回 unknown。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(settings.root), capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           numeric: Optional[Sequence[bool]] = None) -> str:
    """生成表格；numeric 标记该列是否右对齐。"""
    numeric = list(numeric or [False] * len(headers))
    th = "".join(
        f'<th class="num">{_e(h)}</th>' if n else f"<th>{_e(h)}</th>"
        for h, n in zip(headers, numeric))
    body = []
    for r in rows:
        body.append("<tr>" + "".join(
            f'<td class="num">{c}</td>' if n else f"<td>{c}</td>"
            for c, n in zip(r, numeric)) + "</tr>")
    return (f"<table><thead><tr>{th}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def _kpi_block(items: Sequence[Sequence[Any]]) -> str:
    """items: [(标签, 值, 说明)]"""
    cells = "".join(
        f'<div class="kpi"><div class="k">{_e(a)}</div>'
        f'<div class="v">{b}</div>'
        f'<div class="s">{_e(c)}</div></div>'
        for a, b, c in items)
    return f'<div class="kpis">{cells}</div>'


# --------------------------------------------------------------------------- 内嵌图

def _decimate(pts: List[List[float]], limit: int = 2500) -> List[List[float]]:
    """把散点降采样到 limit 个以内。

    热度值（第三列）差异很大，均匀抽样会丢掉高密度区域，
    因此改成「按热度分箱 + 层内等距抽取」：高热度点全部保留，
    低热度点稀疏化，图面观感与实际密度成正比。
    """
    if len(pts) <= limit:
        return pts
    ranked = sorted(pts, key=lambda p: -(p[2] or 0))
    keep = ranked[: max(1, int(limit * 0.55))]          # 高热度重点保真
    rest = ranked[len(keep):]
    if rest:
        step = len(rest) / max(1, limit - len(keep))
        keep += [rest[min(len(rest) - 1, int(i * step))]
                 for i in range(limit - len(keep))]
    return keep


def _svg_scatter(points: Sequence[Any], labels: Sequence[Sequence[Any]] = (),
                 w: int = 1000, h: int = 420, title: str = "",
                 max_points: int = 2500) -> str:
    """经纬度散点图（含点标注）。

    points 兼容两种形式：`[lon, lat, weight]` 或 `{lon, lat, n}`，
    因为热点与网格两种算法给出的点结构不同。
    """
    pts: List[List[float]] = []
    for p in points:
        if isinstance(p, dict):
            lon, lat = p.get("lon"), p.get("lat")
            wt = p.get("n") or p.get("vessels") or 1.0
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            lon, lat = p[0], p[1]
            wt = p[2] if len(p) > 2 else 1.0
        else:
            continue
        if lon is None or lat is None:
            continue
        try:
            pts.append([float(lon), float(lat), float(wt or 1.0)])
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        return ""
    total_pts = len(pts)
    pts = _decimate(pts, max_points)
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    x0, x1, y0, y1 = min(lons), max(lons), min(lats), max(lats)
    dx = (x1 - x0) or 0.01
    dy = (y1 - y0) or 0.01
    pad = 24

    def px(lon: float) -> float:
        return pad + (lon - x0) / dx * (w - 2 * pad)

    def py(lat: float) -> float:
        return h - pad - (lat - y0) / dy * (h - 2 * pad)

    wmax = max([p[2] for p in pts] or [1.0]) or 1.0
    circles = []
    for p in pts:
        wt = p[2] or 1.0
        r = 1.2 + 3.4 * (wt / wmax) ** 0.45
        op = 0.28 + 0.5 * (wt / wmax) ** 0.5
        circles.append(f'<circle cx="{px(p[0]):.1f}" cy="{py(p[1]):.1f}" '
                       f'r="{r:.1f}" fill="#0ea5e9" fill-opacity="{op:.2f}"/>')
    marks = []
    for lb in labels:
        if not lb or len(lb) < 3:
            continue
        lon, lat, text = float(lb[0]), float(lb[1]), str(lb[2])
        marks.append(f'<circle cx="{px(lon):.1f}" cy="{py(lat):.1f}" r="3.4" '
                     f'fill="#f43f5e"/>'
                     f'<text x="{px(lon) + 6:.1f}" y="{py(lat) + 3.5:.1f}" '
                     f'font-size="9.5" fill="#475569">{_e(text)}</text>')
    cap = (f'<text x="{pad}" y="16" font-size="11" fill="#64748b">{_e(title)}'
           f'（共 {total_pts:,} 点，图中抽样展示 {len(pts):,} 点）</text>') if title else ""
    return (f'<div class="svgwrap"><svg viewBox="0 0 {w} {h}" width="100%" '
            f'style="display:block">{cap}'
            f'<rect x="{pad}" y="{pad}" width="{w - 2 * pad}" height="{h - 2 * pad}" '
            f'fill="none" stroke="#e3e9f2"/>'
            f'{"".join(circles)}{"".join(marks)}'
            f'<text x="{w - pad}" y="{h - 8}" font-size="9.5" fill="#94a3b8" '
            f'text-anchor="end">{x0:.2f}° ~ {x1:.2f}° ／ {y0:.2f}° ~ {y1:.2f}°</text>'
            f'</svg></div>')


def _svg_tracks(samples: Sequence[Dict[str, Any]], w: int = 1000, h: int = 300) -> str:
    """预测样例轨迹对比图：左为历史 + 真值 + 各模型预测。"""
    if not samples:
        return ""
    panels = []
    n = len(samples)
    pw = w / max(1, n)
    for i, s in enumerate(samples):
        hist = [[p[0], p[1]] for p in (s.get("history") or []) if p and len(p) >= 2]
        truth = [[p[0], p[1]] for p in (s.get("truth") or []) if p and len(p) >= 2]
        preds = (s.get("predictions") or {})
        series = [("history", hist, "#0ea5e9", 2.0),
                  ("truth", truth, "#111827", 1.6)]
        colors = {"cv": "#f59e0b", "kalman": "#a855f7", "gru": "#16a34a"}
        for k, arr in preds.items():
            pts = [[p[0], p[1]] for p in arr if p and len(p) >= 2]
            if pts:
                series.append((k, pts, colors.get(k, "#64748b"), 1.3))
        allp = [p for _, pts, _, _ in series for p in pts]
        if len(allp) < 2:
            continue
        xs = [p[0] for p in allp]
        ys = [p[1] for p in allp]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        dx = (x1 - x0) or 1.0
        dy = (y1 - y0) or 1.0
        pad = 12
        ox = i * pw
        # 等比例缩放：同一张图里 x/y 用相同米制比例尺，
        # 否则长直线航段会被拉成不同斜率，看不出轨迹转弯与偏差量级。
        iw, ih = pw - 2 * pad - 4, h - 2 * pad - 22
        scale = min(iw / dx, ih / dy)
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2

        def X(v: float) -> float:
            return ox + pad + (pw - 2 * pad) / 2 + (v - cx) * scale

        def Y(v: float) -> float:
            return pad + 16 + ih / 2 - (v - cy) * scale

        paths = []
        for name, pts, col, wd in series:
            d = " ".join(f"{'M' if j == 0 else 'L'}{X(p[0]):.1f},{Y(p[1]):.1f}"
                         for j, p in enumerate(pts))
            dash = ' stroke-dasharray="4 3"' if name in ("cv", "kalman", "gru") else ""
            paths.append(f'<path d="{d}" fill="none" stroke="{col}" '
                         f'stroke-width="{wd}"{dash} stroke-linejoin="round"/>')
        label = f'#{i + 1} {str(s.get("traj_id", ""))[:10]} ADE {_num(s.get("ade_m"), 0)}m'
        panels.append(
            f'<g>{"".join(paths)}'
            f'<rect x="{ox + 2}" y="16" width="{pw - 8}" height="{h - 18}" '
            f'fill="none" stroke="#eef2f7"/>'
            f'<text x="{ox + pad}" y="12" font-size="9.5" fill="#475569">'
            f'{_e(label)}</text></g>')
    if not panels:
        return ""
    legend = ('<div style="font-size:11px;color:#64748b;margin:2px 0 6px">'
              '<span style="color:#0ea5e9">■</span> 历史　'
              '<span style="color:#111827">■</span> 真值　'
              '<span style="color:#f59e0b">┅</span> 匀速外推　'
              '<span style="color:#a855f7">┅</span> 卡尔曼　'
              '<span style="color:#16a34a">┅</span> GRU　'
              '（单位：米，原点为历史末点）</div>')
    return (f'{legend}<div class="svgwrap"><svg viewBox="0 0 {w} {h}" width="100%" '
            f'style="display:block">{"".join(panels)}</svg></div>')


# --------------------------------------------------------------------------- 各算法小节

def _sec_hotspot(s: Dict[str, Any]) -> Dict[str, Any]:
    sampling = s.get("sampling") or {}
    kpis = [
        ("识别热点簇", _num(s.get("n_clusters")), f"显著簇 {_num(s.get('n_significant_clusters'))}"),
        ("噪声占比", f"{float(s.get('noise_ratio') or 0) * 100:.1f}%", "未归入任何簇的点"),
        ("参与聚类点数", _num((sampling or {}).get("n_used")),
         f"抽样前 {_num((sampling or {}).get('n_before_sampling'))}"),
        ("覆盖范围", _extent(s.get("extent_robust") or s.get("extent")), "聚类点位经纬度包络"),
    ]
    body = []
    if sampling:
        body.append(f'<div class="note">抽样方法：{_e(sampling.get("method"))}；'
                    f'时间槽 {_e(sampling.get("time_slot_minutes"))} 分钟；'
                    f'样本 {_num(sampling.get("n_sampled"))} 点 → '
                    f'使用 {_num(sampling.get("n_used"))} 点。</div>')

    clusters = [c for c in (s.get("clusters") or []) if c]
    heat = s.get("heat_grid") or []
    if heat:
        labels = [[c.get("center_lon"), c.get("center_lat"), f"#{c.get('cluster_id')}"]
                  for c in clusters[:12]]
        body.append(_svg_scatter(heat, labels, title="热点密度分布（点越大代表落点越密集）"))

    if clusters:
        body.append("<h3>热点簇清单（TOP 15）</h3>")
        body.append(_table(
            ["排名", "簇 ID", "类型", "点数", "船舶数", "中心经度", "中心纬度",
             "面积(km²)", "中位 SOG(kn)", "低速占比", "主要船型"],
            [[
                _num(c.get("rank")), _num(c.get("cluster_id")), _e(c.get("hotspot_kind")),
                _num(c.get("point_count")), _num(c.get("distinct_mmsi")),
                _num(c.get("center_lon"), 3), _num(c.get("center_lat"), 3),
                _num(c.get("area_km2"), 1), _num(c.get("median_sog"), 1),
                f"{float(c.get('dwell_ratio') or 0) * 100:.1f}%",
                _e(_top_types(c.get("top_ship_types"))),
            ] for c in clusters[:15]],
            [True, True, False, True, True, True, True, True, True, True, False]))

    sens = s.get("sensitivity") or {}
    if sens.get("variants"):
        body.append("<h3>参数敏感性（与基准参数的 Adjusted Rand Index）</h3>")
        rows = [[_num(v.get("min_cluster_size")), _num(v.get("min_samples")),
                 _num(v.get("n_clusters")),
                 f"{float(v.get('noise_ratio') or 0) * 100:.1f}%",
                 _num(v.get("stability_vs_base"), 3)]
                for v in sens["variants"]]
        base = sens.get("base") or {}
        rows.insert(0, [_num(base.get("min_cluster_size")), _num(base.get("min_samples")),
                        _num(base.get("n_clusters")),
                        f"{float(base.get('noise_ratio') or 0) * 100:.1f}%", "1.000（基准）"])
        body.append(_table(["min_cluster_size", "min_samples", "簇数", "噪声率", "ARI"],
                           rows, [True, True, True, True, True]))
    return {"kpis": kpis, "html": "".join(body)}


def _svg_bars(labels: Sequence[str], values: Sequence[float], w: int = 980, h: int = 240,
              title: str = "", xlabel: str = "", ylabel: str = "",
              log_scale: bool = False, note: str = "") -> str:
    """简单柱状图（内嵌 SVG，不依赖外部图表库）。

    log_scale=True 时用对数刻度：AIS 航速分布里静止点常占 2/3，
    线性刻度会把各速度层压成一条线，看不出分布形状。
    """
    raw = [float(v or 0) for v in values]
    if not raw or max(raw) <= 0:
        return ""
    # 显式判断：只有对数刻度且各柱数量级差异过大时才切换
    positive = [v for v in raw if v > 0]
    use_log = False
    if log_scale and positive:
        use_log = max(positive) / min(positive) > 20
    vals = [math.log10(v) if use_log else v for v in raw]
    # 有副标题时把绘图区下移，避免标题与首根柱子重叠
    pad_l, pad_r, pad_t, pad_b = 54, 16, (48 if note else 30), 52
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    top_val = max(vals)
    base = min(0.0, min(vals))
    span = (top_val - base) or 1.0
    bw = iw / max(1, len(vals))
    parts = [f'<text x="8" y="16" font-size="11" fill="#64748b">{_e(title)}</text>']
    if note:
        parts.append(f'<text x="8" y="29" font-size="9" fill="#94a3b8">{_e(note)}</text>')
    # 网格线（4 等分）
    for k in range(5):
        y = pad_t + ih - ih * k / 4
        tick = base + span * k / 4
        lab = f"1e{tick:.0f}" if use_log else f"{tick:,.0f}"
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
                     f'stroke="#eef2f7"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 3.5:.1f}" font-size="9" '
                     f'fill="#94a3b8" text-anchor="end">{lab}</text>')
    for i, v in enumerate(vals):
        bh = ih * ((v - base) / span)
        x = pad_l + i * bw + bw * 0.14
        parts.append(f'<rect x="{x:.1f}" y="{pad_t + ih - bh:.1f}" '
                     f'width="{bw * 0.72:.1f}" height="{max(0.6, bh):.1f}" '
                     f'fill="#38bdf8" fill-opacity="0.85" rx="1.5"/>')
    # x 轴刻度：分箱过多时按间隔抽样避免文字重叠
    step = max(1, math.ceil(len(vals) / 14))
    for i in range(0, len(vals), step):
        parts.append(f'<text x="{pad_l + i * bw + bw / 2:.1f}" y="{h - pad_b + 14}" '
                     f'font-size="8.5" fill="#64748b" text-anchor="middle">{_e(labels[i])}</text>')
    xlab = (f'<text x="{w / 2:.1f}" y="{h - 4}" font-size="9.5" fill="#94a3b8" '
            f'text-anchor="middle">{_e(xlabel)}</text>') if xlabel else ""
    ylab = (f'<text x="10" y="{pad_t + ih / 2:.1f}" font-size="9.5" fill="#94a3b8" '
            f'transform="rotate(-90 10 {pad_t + ih / 2:.1f})" text-anchor="middle">{_e(ylabel)}</text>') if ylabel else ""
    return (f'<div class="svgwrap"><svg viewBox="0 0 {w} {h}" width="100%" '
            f'style="display:block">{"".join(parts)}{xlab}{ylab}</svg></div>')


def _sec_profile(s: Dict[str, Any]) -> Dict[str, Any]:
    totals = s.get("totals") or {}
    vessels = [v for v in (s.get("vessels") or []) if v]
    kpis = [
        ("画像船舶数", _num(s.get("n_vessels")), "统计单位为单船 MMSI"),
        ("总航程", f"{_num((totals or {}).get('distance_km'), 1)} km", "剔除漂移点对后累计"),
        ("总点数", _num((totals or {}).get("points")), "清洗后有效位置点"),
        ("驻留点", _num((totals or {}).get("stay_count")), "半径与时长双阈值识别"),
    ]
    body = []
    if vessels:
        bodies = [v for v in vessels if v.get("mean_sog") is not None]
        med = sorted(float(v["mean_sog"]) for v in bodies)
        if med:
            body.append(f'<div class="note">有航速统计的船舶 {len(bodies)} 艘，'
                        f'平均航速中位数 {med[len(med) // 2]:.1f} kn；'
                        f'其余为基站/岸台等无 SOG 目标，已在画像中标注。</div>')
        body.append("<h3>船舶画像清单（按累计航程 TOP 12）</h3>")
        top = sorted(vessels, key=lambda v: -(v.get("distance_km") or 0))[:12]
        body.append(_table(
            ["MMSI", "船名", "船型", "点数", "航段", "中位 SOG(kn)", "最大 SOG(kn)",
             "累计航程(km)", "活动范围(km)", "驻留次数", "质量分"],
            [[_e(v.get("mmsi")), _e((v.get("name") or "未知")[:16]),
              _e(v.get("ship_type")), _num(v.get("points")), _num(v.get("segments")),
              _num(v.get("median_sog"), 1), _num(v.get("max_sog"), 1),
              _num(v.get("distance_km"), 1), _num(v.get("activity_span_km"), 1),
              _num(v.get("stay_count")), _num(v.get("avg_quality"), 1)] for v in top],
            [False, False, False, True, True, True, True, True, True, True, True]))

    # 航速直方图：speed_hist 是「每艘船 × 每分箱」的长表，
    # 直接画会把同一分箱在不同船舶上重复画成多根柱子，必须先按分箱键聚合。
    hist = [h for h in (s.get("speed_hist") or []) if h]
    if hist:
        agg: Dict[int, float] = {}
        for h in hist:
            try:
                k = int(h.get("k"))
            except (TypeError, ValueError):
                continue
            agg[k] = agg.get(k, 0.0) + float(h.get("n") or 0)
        if agg:
            keys = sorted(agg)
            bar = _svg_bars([f"{k}" for k in keys], [agg[k] for k in keys],
                            title=f"全数据集航速分布（逐 1 kn 分箱）",
                            xlabel="SOG（kn）", ylabel="点数", log_scale=True,
                            note="纵轴为对数刻度：静止点（SOG≈0）数量远高于航行点，线性刻度会压扁其余速度层")
            if bar:
                body.append("<h3>航速分布</h3>")
                body.append(bar)
    return {"kpis": kpis, "html": "".join(body)}


def _svg_heatmatrix(features: Sequence[Dict[str, Any]], matrix: Sequence[Sequence[Any]],
                    w: int = 760, h: int = 620, title: str = "") -> str:
    """相关系数热力图（发散色阶：负相关红、正相关蓝）。"""
    labs = [str(f.get("label") or f.get("name") or f.get("key") or "")
            for f in features]
    n = len(labs)
    if n < 2 or len(matrix) < 2:
        return ""
    left, top = 150, 40
    cell = min((w - left - 70) / n, (h - top - 100) / n)
    if cell <= 0:
        return ""

    def color(v: float) -> str:
        """-1 深红 → 0 浅灰 → +1 深蓝。"""
        v = max(-1.0, min(1.0, v))
        if v >= 0:
            t = v
            r = int(247 - t * (247 - 2))
            gg = int(250 - t * (250 - 90))
            b = int(252 - t * (252 - 130))
        else:
            t = -v
            r = int(247 - t * (247 - 220))
            gg = int(250 - t * (250 - 45))
            b = int(252 - t * (252 - 45))
        return f"rgb({r},{gg},{b})"

    parts = []
    for i in range(n):
        row = matrix[i] if i < len(matrix) else []
        parts.append(f'<text x="{left - 6}" y="{top + i * cell + cell / 2 + 3.5:.1f}" '
                     f'font-size="9.5" fill="#475569" text-anchor="end">{_e(labs[i][:14])}</text>')
        for j in range(n):
            try:
                v = float(row[j])
            except (TypeError, ValueError, IndexError):
                continue
            fill = color(v)
            parts.append(f'<rect x="{left + j * cell:.1f}" y="{top + i * cell:.1f}" '
                         f'width="{cell - 1:.1f}" height="{cell - 1:.1f}" fill="{fill}"/>')
            if cell >= 30 and abs(v) >= 0.005:
                tc = "#ffffff" if abs(v) > 0.55 else "#1b2734"
                parts.append(f'<text x="{left + j * cell + cell / 2:.1f}" '
                             f'y="{top + i * cell + cell / 2 + 3:.1f}" font-size="9" '
                             f'fill="{tc}" text-anchor="middle">{v:.2f}</text>')
        if cell >= 34:
            parts.append(f'<text x="{left + i * cell + cell / 2:.1f}" '
                         f'y="{top - 6}" font-size="9" fill="#64748b" '
                         f'text-anchor="start" transform="rotate(-60 {left + i * cell + cell / 2:.1f} {top - 6})">{_e(labs[i][:10])}</text>')
    # 色标
    ly = top + n * cell + 24
    for k in range(41):
        v = -1 + k / 20
        parts.append(f'<rect x="{left + k * 6}" y="{ly}" width="6" height="10" fill="{color(v)}"/>')
    parts += [
        f'<text x="{left}" y="{ly + 24}" font-size="9.5" fill="#64748b">-1 负相关</text>',
        f'<text x="{left + 120}" y="{ly + 24}" font-size="9.5" fill="#64748b">0</text>',
        f'<text x="{left + 228}" y="{ly + 24}" font-size="9.5" fill="#64748b">+1 正相关</text>',
    ]
    return (f'<div class="svgwrap"><svg viewBox="0 0 {w} {h}" width="100%" '
            f'style="display:block">'
            f'<text x="8" y="16" font-size="11" fill="#64748b">{_e(title)}</text>'
            f'{"".join(parts)}</svg></div>')


def _sec_correlate(s: Dict[str, Any]) -> Dict[str, Any]:
    edges = [e for e in (s.get("edges") or []) if e]
    features = s.get("features") or []
    kpis = [
        ("样本数量", _num(s.get("n_samples")), f"单位：{_e(s.get('unit') or '单船')}"),
        ("参与变量", _num(len(features)), "数值与类别变量分别处理"),
        ("显著关系", _num(len([e for e in edges if e.get("significant")])),
         "Benjamini-Hochberg 校正后 q<0.05"),
        ("最强 |r|", _num(max([abs(float(e.get("r") or 0)) for e in edges] or [0]), 3),
         "Spearman 秩相关"),
    ]
    body = []
    feats = [f for f in (s.get("features") or []) if f]
    mat = s.get("matrix") or []
    hm = _svg_heatmatrix(feats, mat, title="相关矩阵（颜色越浓 |r| 越大）")
    if hm:
        body.append(hm)
    if edges:
        body.append("<h3>显著关系清单（按 |r| 降序 TOP 20）</h3>")
        top = sorted(edges, key=lambda e: -abs(float(e.get("r") or 0)))[:20]
        body.append(_table(
            ["变量 A", "变量 B", "r", "p 值", "q 值 (BH)", "方向", "强度", "显著"],
            [[_e(e.get("source_label") or e.get("source")),
              _e(e.get("target_label") or e.get("target")),
              _num(e.get("r"), 3), _p(e.get("p_value")), _p(e.get("q_value")),
              _e(e.get("direction")), _e(e.get("strength")),
              "是" if e.get("significant") else "否"] for e in top],
            [False, False, True, True, True, False, False, False]))
    lag = s.get("lag") or {}
    if lag.get("available") and lag.get("series"):
        body.append("<h3>滞后相关（相关系数随滞后小时变化）</h3>")
        peak = max(lag["series"], key=lambda r: abs(float(r.get("r") or 0)))
        body.append(f'<div class="note">峰值出现在滞后 '
                    f'{_e(peak.get("lag_hours"))} 小时，r = {_num(peak.get("r"), 3)}。</div>')
        body.append(_table(["滞后（小时）", "r", "p 值"],
                           [[_num(r.get("lag_hours")), _num(r.get("r"), 3), _p(r.get("p_value"))]
                            for r in lag["series"]],
                           [True, True, True]))
    return {"kpis": kpis, "html": "".join(body)}


def _sec_formation(s: Dict[str, Any]) -> Dict[str, Any]:
    groups = [g for g in (s.get("groups") or []) if g]
    thr = s.get("thresholds") or {}
    kpis = [
        ("参与序列", _num(s.get("n_sequences")), f"时间窗口 {_e((s.get('window') or {}).get('minutes'))} 分钟"),
        ("候选船对", _num(s.get("n_candidate_pairs")), f"实算 {_num(s.get('n_pairs_evaluated'))} 对"),
        ("识别编群", _num(s.get("n_groups")), f"DTW 阈值 {_e(thr.get('dtw'))}"),
        ("计算耗时", f"{_num(s.get('dtw_seconds'), 1)} 秒", "DTW + 层次聚类"),
    ]
    body = []
    if groups:
        body.append("<h3>编群清单</h3>")
        body.append(_table(
            ["编群", "成员数", "成员 MMSI", "船型", "最大 DTW", "最大间距(m)",
             "平均重叠", "置信度"],
            [[_e(g.get("group_id")),
              _num(g.get("member_count") or len(g.get("members") or [])),
              _e(", ".join(str(m) for m in (g.get("mmis") or g.get("mmsis") or [])[:6])),
              _e(" / ".join(g.get("ship_types") or [])),
              _num(g.get("max_pair_dtw") or g.get("max_dtw"), 3),
              _num(g.get("max_sep_m") or g.get("max_distance_m")),
              f"{float(g.get('mean_overlap') or 0) * 100:.0f}%",
              _e(g.get("confidence"))] for g in groups[:15]],
            [False, True, False, False, True, True, True, False]))
    pairs = [p for p in (s.get("pairs") or []) if p]
    if pairs:
        body.append("<h3>候选船对明细（TOP 15，按 DTW 升序）</h3>")
        body.append(_table(
            ["MMSI A", "MMSI B", "DTW", "时间重叠", "平均间距(m)", "航向差(°)"],
            [[_e(p.get("mmsi_a")), _e(p.get("mmsi_b")), _num(p.get("dtw"), 3),
              f"{float(p.get('overlap_ratio') or 0) * 100:.0f}%",
              _num(p.get("mean_sep_m")), _num(p.get("delta_cog"), 1)]
             for p in sorted(pairs, key=lambda x: float(x.get("dtw") or 9e9))[:15]],
            [False, False, True, True, True, True]))
    return {"kpis": kpis, "html": "".join(body)}


def _svg_polylines(routes: Sequence[Dict[str, Any]], w: int = 1000, h: int = 420,
                   title: str = "", limit: int = 12) -> str:
    """频繁航线几何：每条航线画一条带顶点的折线，便于看出走向。"""
    items = [r for r in routes if (r.get("route_geometry") or [])][:limit]
    if not items:
        return ""
    allp = [list(map(float, pt)) for r in items for pt in r["route_geometry"] if len(pt) >= 2]
    if len(allp) < 2:
        return ""
    lons = [p[0] for p in allp]
    lats = [p[1] for p in allp]
    x0, x1, y0, y1 = min(lons), max(lons), min(lats), max(lats)
    dx, dy = (x1 - x0) or 0.01, (y1 - y0) or 0.01
    pad = 30
    iw, ih = w - 2 * pad, h - 2 * pad - 14
    # 等比例：经度按纬度余弦压缩，避免高纬地区图面变形
    kx = math.cos(math.radians((y0 + y1) / 2)) or 1.0
    scale = min(iw / (dx * kx), ih / dy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # 按内容实际占位裁剪画布：航线只覆盖局部区域时，
    # 固定 w/h 会在图里留下大片空白，看不出走向。
    vw = max(320.0, dx * kx * scale + 2 * pad)
    vh = max(200.0, dy * scale + 2 * pad + 14)

    def X(lon: float) -> float:
        return vw / 2 + (lon - cx) * kx * scale

    def Y(lat: float) -> float:
        return pad + 14 + (vh - 2 * pad - 14) / 2 - (lat - cy) * scale

    palette = ["#0284c7", "#16a34a", "#dc2626", "#a855f7", "#f59e0b",
               "#0891b2", "#db2777", "#65a30d", "#7c3aed", "#ea580c",
               "#0d9488", "#9333ea"]
    # 四角经纬度标注 + 淡框：航线分居相隔较远的海域时，
    # 空白区域容易被误认为是绘图错误，标注后可读作真实地理距离。
    frame = (f'<rect x="{pad / 2:.0f}" y="{pad / 2 + 12:.0f}" '
             f'width="{vw - pad:.0f}" height="{vh - pad - 12:.0f}" '
             f'fill="none" stroke="#eef2f7"/>'
             f'<text x="{pad / 2:.0f}" y="{vh - 4:.0f}" font-size="8.5" '
             f'fill="#94a3b8">{x0:.2f}°E</text>'
             f'<text x="{vw - pad / 2:.0f}" y="{vh - 4:.0f}" font-size="8.5" '
             f'fill="#94a3b8" text-anchor="end">{x1:.2f}°E</text>'
             f'<text x="{vw - pad / 2:.0f}" y="{pad / 2 + 24:.0f}" font-size="8.5" '
             f'fill="#94a3b8" text-anchor="end">{y1:.2f}°N</text>'
             f'<text x="{pad / 2:.0f}" y="{pad / 2 + 24:.0f}" font-size="8.5" '
             f'fill="#94a3b8">{y0:.2f}°N</text>')
    parts = []
    for i, r in enumerate(items):
        pts = [list(map(float, pt)) for pt in r["route_geometry"] if len(pt) >= 2]
        col = palette[i % len(palette)]
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(p[0]):.1f},{Y(p[1]):.1f}"
                     for j, p in enumerate(pts))
        parts.append(f'<path d="{d}" fill="none" stroke="{col}" '
                     f'stroke-width="2" stroke-linejoin="round" opacity="0.85"/>')
        for j, p in enumerate(pts):
            parts.append(f'<circle cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="2.6" '
                         f'fill="{col}"/>')
            if j == 0:
                parts.append(f'<text x="{X(p[0]) + 5:.1f}" y="{Y(p[1]) - 4:.1f}" '
                             f'font-size="9" fill="{col}">#{_e(r.get("rank"))}</text>')
    return (f'<div class="svgwrap"><svg viewBox="0 0 {vw:.0f} {vh:.0f}" width="100%" '
            f'style="display:block">'
            f'<text x="{pad}" y="16" font-size="11" fill="#64748b">{_e(title)}'
            f'（前 {len(items)} 条，小圈为途经网格中心）</text>'
            f'{frame}{"".join(parts)}</svg></div>')


def _sec_routes(s: Dict[str, Any]) -> Dict[str, Any]:
    g = s.get("grid") or {}
    kpis = [
        ("网格规模", f"{_num(g.get('rows'))} × {_num(g.get('cols'))}",
         f"模式 {_e(g.get('mode'))}"),
        ("统计单位（航次）", _num(s.get("n_units")), "以轨迹段代理航次"),
        ("高频网格", _num(s.get("n_observed_cells")), f"转移边 {_num(s.get('n_edges'))} 条"),
        ("频繁航线", _num(s.get("n_routes")), "连续有序网格序列"),
    ]
    body = []
    cells = [c for c in (s.get("cells_top") or s.get("cells") or []) if c]
    cells_by_id = {c.get("grid_id"): c for c in (s.get("cells") or []) if c}
    if cells:
        body.append("<h3>高频网格（按支持航次 TOP 15）</h3>")
        body.append(_table(
            ["网格 ID", "行", "列", "观测点数", "支持航次", "支持率", "船舶数", "访问事件"],
            [[_e(c.get("grid_id")), _num(c.get("row_no")), _num(c.get("col_no")),
              _num(c.get("observed_point_count")), _num(c.get("distinct_voyage_count")),
              f"{float(c.get('support_ratio') or 0) * 100:.1f}%",
              _num(c.get("distinct_mmsi_count")), _num(c.get("visit_event_count"))]
             for c in sorted(cells, key=lambda x: -(x.get("distinct_voyage_count") or 0))[:15]],
            [False, True, True, True, True, True, True, True]))
    routes = [r for r in (s.get("routes") or []) if r]
    if routes:
        pl = _svg_polylines(routes, title="频繁航线几何")
        if pl:
            body.append(pl)
        body.append("<h3>频繁航线清单（TOP 15，按支持航次降序）</h3>")
        body.append(_table(
            ["排名", "网格序列", "长度", "支持航次", "支持率", "涉及船舶"],
            [[_num(r.get("rank")),
              _e(" → ".join(str(x) for x in (r.get("grid_sequence") or []))),
              _num(r.get("length") or len(r.get("grid_sequence") or [])),
              _num(r.get("support_count") or r.get("support")),
              f"{float(r.get('support_ratio') or 0) * 100:.1f}%",
              _num(r.get("distinct_mmsi_count"))] for r in routes[:15]],
            [True, False, True, True, True, True]))
        body.append('<div class="note">网格—行列对照：'
                    + "；".join(f"{_e(k)} → r{_num(v.get('row_no'))}c{_num(v.get('col_no'))}"
                              for k, v in list(cells_by_id.items())[:12])
                    + " ……（完整网格表见结果文件）</div>")
    return {"kpis": kpis, "html": "".join(body)}


def _sec_predict(s: Dict[str, Any]) -> Dict[str, Any]:
    m = s.get("metrics") or {}
    verdict = s.get("verdict") or {}
    # 最优模型：在截尾 ADE 上取最小（缺失则回退到平均 ADE）
    ranked = [(k, float(v.get("trimmed_ADE_m") or v.get("ADE_m") or 9e18))
              for k, v in m.items() if v]
    best = min(ranked, key=lambda kv: kv[1])[0] if ranked else ""
    best_cn = {"cv": "匀速外推 CV", "kalman": "卡尔曼滤波", "gru": "GRU 残差学习"}.get(best, best)
    n_test = (m.get(best) or m.get("cv") or {}).get("n_test")
    kpis = [
        ("测试样本", _num(n_test), "按时间外推切分的独立测试集"),
        ("最优模型", _e(best_cn or "—"),
         f"截尾 ADE {_num((m.get(best) or {}).get('trimmed_ADE_m'), 0)} m"),
        ("GRU 相对基线", f"{_num(verdict.get('improvement_pct'), 1)}%",
         f"基线 {_e(verdict.get('baseline') or '—')}，负值表示劣于基线"),
        ("上线建议", "建议上线" if verdict.get("can_go_online") else "暂不上线",
         "依据独立时间测试集判定"),
    ]
    body = []
    if m:
        body.append("<h3>模型误差对比</h3>")
        name_cn = {"cv": "匀速外推 CV", "kalman": "卡尔曼滤波", "gru": "GRU 残差学习"}
        body.append(_table(
            ["模型", "样本数", "平均 ADE(m)", "截尾 ADE(m)", "中位误差(m)",
             "FDE(m)", "P90(m)"],
            [[_e(name_cn.get(k, k)), _num(v.get("n_test")), _num(v.get("ADE_m"), 1),
              _num(v.get("trimmed_ADE_m"), 1), _num(v.get("median_m"), 1),
              _num(v.get("FDE_m"), 1), _num(v.get("P90_m"), 1)]
             for k, v in m.items()],
            [False, True, True, True, True, True, True]))
        # 逐步误差（每个预测步的均值位移误差）
        steps = [f"+{(i + 1) * int((s.get('config') or {}).get('step_minutes') or 5)}min"
                 for i in range(len((m.get("cv") or {}).get("per_step_m") or []))]
        if steps:
            body.append("<h3>逐步误差（各预测步的平均位移误差）</h3>")
            body.append(_table(
                ["步长", "CV(m)", "Kalman(m)", "GRU(m)"],
                [[steps[i],
                  _num(((m.get("cv") or {}).get("per_step_m") or [None] * len(steps))[i], 0),
                  _num(((m.get("kalman") or {}).get("per_step_m") or [None] * len(steps))[i], 0),
                  _num(((m.get("gru") or {}).get("per_step_m") or [None] * len(steps))[i], 0)]
                 for i in range(len(steps))],
                [False, True, True, True]))
        samples = [x for x in (s.get("samples") or []) if x]
        svg = _svg_tracks(samples)
        if svg:
            body.append("<h3>预测样例轨迹对比</h3>")
            body.append(svg)

    strat = s.get("stratified") or {}
    for model_key, model_cn in (("cv", "匀速外推 CV"), ("gru", "GRU 残差学习")):
        item = strat.get(model_key) or {}
        by_speed = item.get("by_speed") or {}
        by_type = item.get("by_ship_type") or {}
        if by_speed:
            body.append(f"<h3>分层评估 · {_e(model_cn)}（按航速层）</h3>")
            body.append('<div class="note">静止层误差仅百米级，巡航以上达数公里；'
                        '整体指标必须结合本表阅读，否则会被静止样本稀释。</div>')
            body.append(_table(
                ["航速层", "样本数", "截尾 ADE(m)", "中位误差(m)"],
                [[_e(lab), _num(v.get("n")), _num(v.get("trimmed_ADE_m"), 0),
                  _num(v.get("median_m"), 0)] for lab, v in by_speed.items()],
                [False, True, True, True]))
        if by_type:
            body.append(f"<h3>分层评估 · {_e(model_cn)}（按船型，按样本量 TOP 10）</h3>")
            items = sorted(by_type.items(), key=lambda kv: -(kv[1].get("n") or 0))[:10]
            body.append(_table(
                ["船型", "样本数", "截尾 ADE(m)"],
                [[_e(lab), _num(v.get("n")), _num(v.get("trimmed_ADE_m"), 0)]
                 for lab, v in items],
                [False, True, True]))

    gru = s.get("gru") or {}
    if gru.get("loss_curve"):
        body.append(f'<div class="note">GRU 训练：{_num(gru.get("epochs"))} 轮，'
                    f'隐藏维度 {_num(gru.get("hidden"))}，参数 {_num(gru.get("n_params"))} 个；'
                    f'初始损失 {_num(gru.get("initial_loss"), 6)} → '
                    f'最终 {_num(gru.get("final_loss"), 6)}。'
                    f'训练损失仅用于监控收敛，不作为上线依据。</div>')
    split = s.get("split") or {}
    if split:
        ratio = split.get("train_ratio")
        ratio_txt = f"，训练占比 {float(ratio) * 100:.0f}%" if ratio is not None else ""
        body.append(f'<div class="note">切分方式：{_e(split.get("method"))}；'
                    f'训练 {_num(split.get("n_train"))} / 测试 {_num(split.get("n_test"))} '
                    f'窗口{ratio_txt}。</div>')
    return {"kpis": kpis, "html": "".join(body)}


def _sec_clean(s: Dict[str, Any]) -> Dict[str, Any]:
    dims = s.get("dimensions") or {}
    kpis = [
        ("综合质量分", _num(s.get("overall_score"), 2), "五维加权"),
        ("原始行数", _num(s.get("row_raw")), "含重复与无效坐标"),
        ("清洗后行数", _num(s.get("row_clean")),
         f"去重率 {float(s.get('duplicate_rate') or 0) * 100:.2f}%"),
        ("异常点占比", f"{float(s.get('error_ratio') or 0) * 100:.2f}%",
         f"标记点 {_num(s.get('flagged_points'))}"),
    ]
    body = []
    if dims:
        body.append("<h3>质量维度得分</h3>")
        body.append(_table(["维度", "得分"],
                           [[_e(k), _num(v, 2)] for k, v in dims.items()],
                           [False, True]))
    return {"kpis": kpis, "html": "".join(body)}


_SECTIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "hdbscan_hotspot": _sec_hotspot,
    "vessel_profile": _sec_profile,
    "correlate": _sec_correlate,
    "formation_dtw": _sec_formation,
    "frequent_route": _sec_routes,
    "predict": _sec_predict,
    "clean": _sec_clean,
}


# --------------------------------------------------------------------------- 小工具

def _p(v: Any) -> str:
    """p 值格式化：极小值用科学计数法，避免显示为 0。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f == 0:
        return "<1e-300"
    if f < 1e-4:
        return f"{f:.1e}"
    return f"{f:.4f}"


def _extent(e: Any) -> str:
    if not isinstance(e, (list, tuple)) or len(e) != 4:
        return "—"
    try:
        return f"{(float(e[2]) - float(e[0])):.1f}° × {(float(e[3]) - float(e[1])):.1f}°"
    except (TypeError, ValueError):
        return "—"


def _top_types(d: Any) -> str:
    if isinstance(d, dict) and d:
        return "、".join(f"{k}({_num(v)})" for k, v in list(d.items())[:2])
    return "—"


# --------------------------------------------------------------------------- 报告主体

def _params_table(params: Dict[str, Any]) -> str:
    if not params:
        return '<div class="note">本次任务未显式传参，全部使用系统默认基线（见附录 B）。</div>'
    rows = [[f"<code>{_e(k)}</code>", _e(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False))]
            for k, v in sorted(params.items())]
    return _table(["参数", "取值"], rows)


def _caveat_list(caveats: Sequence[str], notes: Sequence[str]) -> str:
    """合并「结论口径」与「算法说明」，去重后输出。

    两类来源内容经常重叠（如热点报告的凸包说明），而且措辞略有差别
    （「代表官方航道、锚地或安全航区」对「代表官方航道或安全航区」），
    仅做字面去重会在报告里留下两条同义结论，因此用字符序列相似度去重。
    """
    def norm(text: str) -> str:
        return "".join(ch for ch in text if ch.isalnum())

    kept: List[tuple] = []
    for raw in list(caveats) + list(notes or []):
        text = str(raw or "").strip()
        if not text:
            continue
        key = norm(text)
        if not key:
            continue
        if any(key == prev or difflib.SequenceMatcher(None, key, prev).ratio() >= 0.4
               for prev, _ in kept):
            continue
        kept.append((key, text))
    if not kept:
        return ""
    return ('<ul class="caveats">'
            + "".join(f"<li>{_e(t)}</li>" for _, t in kept) + "</ul>")


def render_html(task_id: str) -> str:
    """生成自包含 HTML 报告正文。任务不存在或未产出结果时抛 ValueError。"""
    task = tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在：{task_id}")
    summary = tasks.result_summary(task_id)
    if summary is None:
        raise ValueError(f"任务尚未产出结果（当前状态：{task.get('status')}）")

    algo = task["algorithm"]
    dv = task["dataset_version"]
    sec = _SECTIONS.get(algo, lambda s: {"kpis": [], "html": ""})(summary)
    caveats = ALGO_CAVEATS.get(algo, [])

    # 数据集概况（时间范围、bbox、点数），对应「数据范围」可追溯要求
    ds_info: Dict[str, Any] = {}
    try:
        if dataservice.dataset_exists(dv):
            ds_info = dataservice.overview(dv) or {}
    except Exception:
        ds_info = {}

    extent_meta = [
        ("报告生成时间", _e(datetime.now().strftime("%Y-%m-%d %H:%M:%S")), None),
        ("任务 ID", f"<code>{_e(task_id)}</code>", None),
        ("算法 / 队列", _e(ALGO_CN.get(algo, algo)), None),
        ("数据版本", _e(dv), None),
        ("任务状态", _e(task.get("status")), None),
        ("创建 / 完成",
         f'{_e(_iso(task.get("created_at")))} → {_e(_iso(task.get("finished_at")))}', None),
        ("代码版本", f"<code>{_e(_git_version())}</code>", None),
        ("数据行数", _num(ds_info.get("n_points")) or "—", None),
        ("时间范围", f'{_e(_iso((ds_info.get("time_range") or ["—"])[0]))}<br>'
                     f'{_e(_iso((ds_info.get("time_range") or ["", "—"])[-1]))}', None),
        ("空间范围", _e(_extent(ds_info.get("bbox_robust") or ds_info.get("bbox"))), None),
        ("综合质量分", _num((ds_info.get("quality") or {}).get("overall_score"), 2), None),
        ("数据版本船数", _num(ds_info.get("n_mmsi")), None),
    ]
    meta_html = "".join(f"<div><b>{_e(k)}</b>{v}</div>" for k, v, _ in extent_meta)

    notes = summary.get("notes") or []
    caveat_html = _caveat_list(caveats, notes)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_e(ALGO_CN.get(algo, algo))} · 分析报告 · {_e(task_id)}</title>
<style>{_CSS}</style></head><body><div class="page">
<h1>{_e(ALGO_CN.get(algo, algo))} — 分析报告</h1>
<div class="sub">AIS 船舶航迹数据分析与可视化系统 · 数据版本 {_e(dv)}</div>
<div class="meta">{meta_html}</div>

<h2>1. 关键指标</h2>
{_kpi_block(sec["kpis"])}

<h2>2. 分析结果</h2>
{sec["html"] or '<div class="note">该任务未产出可视化结果，请查看结果文件。</div>'}

<h2>3. 参数与配置</h2>
{_params_table(task.get("params") or {})}
<div class="note">过滤条件：<code>{_e(json.dumps(task.get("filters") or {}, ensure_ascii=False))}</code></div>

<h2>4. 结论口径与使用边界</h2>
{caveat_html or '<div class="note">本算法无额外口径限制。</div>'}

<footer>
<span>任务 {_e(task_id)} · 代码版本 {_e(_git_version())}</span>
<span>生成于 {_e(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</span>
</footer>
</div></body></html>
"""


def render_markdown(task_id: str) -> str:
    """生成 Markdown 版报告（便于归档到 Git / 知会文档）。"""
    task = tasks.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在：{task_id}")
    summary = tasks.result_summary(task_id)
    if summary is None:
        raise ValueError(f"任务尚未产出结果（当前状态：{task.get('status')}）")

    algo = task["algorithm"]
    dv = task["dataset_version"]
    sec = _SECTIONS.get(algo, lambda s: {"kpis": [], "html": ""})(summary)
    caveats = ALGO_CAVEATS.get(algo, [])

    lines = [
        f"# {ALGO_CN.get(algo, algo)} — 分析报告",
        "",
        f"- 报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 任务 ID：`{task_id}`",
        f"- 算法：{ALGO_CN.get(algo, algo)}",
        f"- 数据版本：`{dv}`",
        f"- 任务状态：{task.get('status')}",
        f"- 创建 / 完成：{_iso(task.get('created_at'))} → {_iso(task.get('finished_at'))}",
        f"- 代码版本：`{_git_version()}`",
        "",
        "## 1. 关键指标",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---:|---|",
    ]
    for k, v, sub in sec["kpis"]:
        clean = str(v).replace("—", "-")
        lines.append(f"| {k} | {clean} | {sub or ''} |")

    lines += ["", "## 2. 参数与配置", "",
              "```json", json.dumps(task.get("params") or {}, ensure_ascii=False, indent=2), "```",
              "",
              f"过滤条件：`{json.dumps(task.get('filters') or {}, ensure_ascii=False)}`",
              "", "## 3. 结论口径与使用边界", ""]
    seen = set()
    for c in list(caveats) + list(summary.get("notes") or []):
        text = str(c or "").strip()
        if text and text not in seen:
            seen.add(text)
            lines.append(f"- {text}")
    lines += ["", "---", "",
              f"> 任务 {task_id} · 代码版本 {_git_version()} · 明细以结果文件为准。"]
    return "\n".join(lines) + "\n"


def build_report(task_id: str, fmt: str = "html") -> Path:
    """生成报告并写入任务结果目录，返回文件路径。"""
    fmt = (fmt or "html").lower()
    if fmt not in ("html", "md", "markdown"):
        raise ValueError(f"不支持的报告格式：{fmt}")
    text = render_markdown(task_id) if fmt != "html" else render_html(task_id)
    ext = "md" if fmt != "html" else "html"
    out_dir = settings.results_root / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"report.{ext}"
    p.write_text(text, encoding="utf-8")
    try:
        store.audit("report_export", f"task:{task_id}", f"format={ext}", "web")
    except Exception:
        pass
    return p


def _unused_guard() -> None:  # pragma: no cover - 仅用于保持类型导入可见
    _ = (Path, Optional)
