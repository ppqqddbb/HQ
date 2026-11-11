/* 属性关联分析：相关矩阵热力图 + 显著性边表（BH 校正） + 滞后曲线 */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox } from "./_ui.js";

export async function render(view) {
  const f = form([
    {
      key: "method", label: "相关系数", type: "select", value: "spearman", numeric: false,
      options: [{ v: "spearman", t: "Spearman（秩相关，稳健）" }, { v: "pearson", t: "Pearson（线性相关）" }, { v: "kendall", t: "Kendall（秩相关）" }],
    },
    {
      key: "unit", label: "样本单位", type: "select", value: "mmsi", numeric: false,
      options: [{ v: "mmsi", t: "单船（MMSI）" }, { v: "traj", t: "航段（traj_id）" }],
      hint: "避免逐点伪重复",
    },
    { key: "max_features", label: "参与变量数", type: "number", value: 10, min: 3, max: 20 },
    { key: "min_samples", label: "最小样本数", type: "number", value: 30, min: 5 },
  ]);
  const runBtn = el("button", { class: "btn primary" }, "运行关联分析");

  view.appendChild(el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "统计口径：以单船（或航段）为独立样本，避免 AIS 高频上报造成的伪相关；多重检验使用 Benjamini-Hochberg 校正，以 q_value < 0.05 判定显著。")));

  const status = statusBox();
  view.appendChild(status);

  const kBox = el("div", {});
  const heatBox = el("div", {});
  const edgeBox = el("div", {});
  const lagBox = el("div", {});
  view.appendChild(kBox);
  const heatCard = card("相关矩阵", "颜色越亮表示 |r| 越大；悬停可查看具体数值", heatBox);
  const edgeCard = card("显著关系边表", "按 |r| 降序；q_value 为 BH 校正后的 p 值", edgeBox);
  view.appendChild(el("div", { class: "grid g2", style: { marginBottom: "14px", alignItems: "start" } }, heatCard, edgeCard));
  const lagCard = card("滞后相关曲线", "不同滞后小时下的相关系数，用于判断领先/滞后关系", lagBox);
  view.appendChild(lagCard);

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    runBtn.disabled = true;
    try {
      const out = await runTask("correlate", {
        method: v.method, unit: v.unit,
        max_features: v.max_features, min_samples: v.min_samples,
      }, {}, { onProgress: t => status.set(t) });
      status.set(out.task);
      toast("关联分析完成");
      show(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  kBox.appendChild(loading("正在加载关联分析结果…"));
  try {
    const t = await latestSucceeded("correlate", state.dataset);
    if (t) { status.set(t); kBox.innerHTML = ""; show(await getSummary(t.task_id)); }
    else {
      kBox.innerHTML = "";
      kBox.appendChild(card("还没有关联分析结果", null,
        el("div", { class: "note" }, "点击「运行关联分析」生成相关矩阵与显著性检验结果。")));
    }
  } catch (e) {
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }

  function show(d) {
    const feats = d.features || [];
    const labels = feats.map(f => f.label || f.key);
    const corr = d.correction || {};
    const edges = d.edges || [];

    /* KPI */
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
      kpi("样本数量", FMT.num(d.n_samples), `单位：${d.unit === "mmsi" ? "单船" : "航段"}`),
      kpi("参与变量", FMT.num(feats.length), `方法 ${(d.params || {}).method || "—"}`),
      kpi("显著关系", FMT.num(corr.n_significant_adj ?? 0),
        `检验 ${FMT.num(corr.n_tests ?? 0)} 组 · 校正 ${corr.method || "BH"}`),
      kpi("最强 |r|", edges.length ? Math.abs(edges[0].r).toFixed(3) : "—",
        edges.length ? `${edges[0].source_label} ↔ ${edges[0].target_label}` : "—")));

    /* 热力图 */
    heatBox.innerHTML = "";
    const hEl = el("div", { class: "chart", style: { height: "420px" } });
    heatBox.appendChild(hEl);
    chartHeatmap(hEl, labels, labels,
      d.matrix.map((row, i) => row.map((v, j) => [j, i, Number(v)])), {
        min: -1, max: 1,
        // 相关系数的语义是“方向 + 强度”，用发散色阶：红负、蓝正
        colors: ["#f472b6", "#7c2d4a", "#16233a", "#1e6f9e", "#38bdf8"],
        showLabel: true,
        fmt: v => Number(v).toFixed(3),
        mapFmt: v => Number(v).toFixed(1),
        yOpt: { axisLabel: { color: "#8fa3bd", fontSize: 10, width: 78, overflow: "truncate" } },
        xOpt: { axisLabel: { color: "#8fa3bd", fontSize: 10, rotate: 34, width: 78, overflow: "truncate" } },
      });

    /* 边表 */
    edgeBox.innerHTML = "";
    if (edges.length) {
      edgeBox.appendChild(table([
        { label: "变量 A", render: r => r.source_label || r.source },
        { label: "变量 B", render: r => r.target_label || r.target },
        { label: "r", num: true, render: r => Number(r.r).toFixed(3) },
        { label: "p", num: true, render: r => fmtP(r.p_value) },
        { label: "q (BH)", num: true, render: r => fmtP(r.q_value) },
        { label: "方向", render: r => el("span", { class: "tag " + (r.r > 0 ? "ok" : "bad") }, r.direction || "—") },
        { label: "强度", render: r => el("span", { class: "tag " + (r.strength === "强" ? "acc" : r.strength === "中" ? "warn" : "") }, r.strength || "—") },
        { label: "显著", render: r => r.significant ? el("span", { class: "tag ok" }, "是") : el("span", { class: "tag" }, "否") },
      ], edges.slice(0, 200), { tall: true }));
    } else edgeBox.appendChild(empty("无显著关系"));

    /* 滞后曲线 */
    lagBox.innerHTML = "";
    const lag = d.lag || {};
    if (lag.available && (lag.series || []).length) {
      const lEl = el("div", { class: "chart", style: { height: "280px" } });
      lagBox.appendChild(el("div", { style: { fontSize: "11.5px", color: "var(--txt3)", marginBottom: "6px" } },
        `变量对：${lag.x} → ${lag.y}（按 ${lag.bucket} 聚合）`));
      lagBox.appendChild(lEl);
      chartLine(lEl, lag.series.map(s => s.lag_hours + "h"),
        [{ name: "相关系数 r", data: lag.series.map(s => s.r), area: true }],
        { yName: "r", boundaryGap: false });
      const best = lag.series.reduce((a, b) => (Math.abs(b.r) > Math.abs(a.r) ? b : a), lag.series[0]);
      lagBox.appendChild(el("div", { class: "note", style: { marginTop: "8px" } },
        `峰值出现在滞后 ${best.lag_hours} 小时（r = ${best.r}，p = ${fmtP(best.p_value)}）。`));
    } else {
      lagBox.appendChild(empty("该数据集时间跨度不足，无法计算滞后关系"));
    }

    /* 备注 */
    if ((d.notes || []).length) {
      lagBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        ...d.notes.map(n => el("div", {}, "· " + n))));
    }
  }
}

function fmtP(p) {
  if (p === null || p === undefined) return "—";
  const v = Number(p);
  if (v === 0) return "<1e-300";
  return v < 0.001 ? v.toExponential(1) : v.toFixed(4);
}
