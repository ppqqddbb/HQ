/* 船舶编群分析：候选对筛选 + DTW + average linkage 层次聚类 */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox } from "./_ui.js";

export async function render(view) {
  const f = form([
    { key: "resample_seconds", label: "重采样间隔", type: "number", value: 120, min: 30, step: 30, unit: "秒" },
    { key: "time_overlap_min", label: "最小时间重叠", type: "number", value: 0.6, min: 0.1, max: 1, step: 0.1, hint: "0–1 之间" },
    { key: "max_sep_m", label: "最大距离", type: "number", value: 3000, min: 100, step: 100, unit: "米" },
    { key: "cog_tol_deg", label: "航向容差", type: "number", value: 45, min: 5, step: 5, unit: "度" },
    { key: "sog_tol_kn", label: "航速容差", type: "number", value: 4, min: 0.5, step: 0.5, unit: "节" },
    { key: "dtw_threshold", label: "DTW 阈值", type: "number", value: 1.2, min: 0.1, step: 0.1 },
    { key: "window_minutes", label: "时间窗口", type: "number", value: 240, min: 30, step: 30, unit: "分钟" },
    { key: "max_sequences", label: "最多序列", type: "number", value: 120, min: 10, step: 10 },
  ]);
  const runBtn = el("button", { class: "btn primary" }, "运行编群分析");

  view.appendChild(el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "编群定义：同一时间窗口内、空间接近、航速航向一致、路径形态相近（DTW 归一化距离小）的船舶集合。DTW 为无量纲归一化距离，阈值需结合业务经验标定。")));

  const status = statusBox();
  view.appendChild(status);

  const kBox = el("div", {});
  const groupBox = el("div", {});
  const matrixBox = el("div", {});
  const pairBox = el("div", {});
  view.appendChild(kBox);
  view.appendChild(el("div", { class: "grid g23", style: { marginBottom: "14px", alignItems: "start" } },
    card("识别出的编群", "同群船舶在窗口内共同航行", groupBox),
    card("DTW 距离矩阵", "候选船对之间的归一化 DTW 距离（越小越相似）", matrixBox)));
  view.appendChild(card("候选船对明细", "按 DTW 升序，展示重叠率与方位差", pairBox));

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    runBtn.disabled = true;
    try {
      const out = await runTask("formation_dtw", {
        resample_seconds: v.resample_seconds,
        time_overlap_min: v.time_overlap_min,
        max_sep_m: v.max_sep_m,
        cog_tol_deg: v.cog_tol_deg,
        sog_tol_kn: v.sog_tol_kn,
        dtw_threshold: v.dtw_threshold,
        window_minutes: v.window_minutes,
        max_sequences: v.max_sequences,
      }, {}, { onProgress: t => status.set(t) });
      status.set(out.task);
      toast("编群分析完成");
      show(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  kBox.appendChild(loading("正在加载编群结果…"));
  try {
    const t = await latestSucceeded("formation_dtw", state.dataset);
    if (t) { status.set(t); kBox.innerHTML = ""; show(await getSummary(t.task_id)); }
    else {
      kBox.innerHTML = "";
      kBox.appendChild(card("还没有编群结果", null,
        el("div", { class: "note" }, "点击「运行编群分析」计算共同航行船舶群。Numba 优化后 120 条序列约需 5–10 秒。")));
    }
  } catch (e) {
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }

  function show(d) {
    const groups = d.groups || [];
    const w = d.window || {};
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
      kpi("参与序列", FMT.num(d.n_sequences), `时间窗口 ${FMT.num(w.window_minutes)} 分钟`),
      kpi("候选船对", FMT.num(d.n_candidate_pairs), `实算 ${FMT.num(d.n_pairs_evaluated)} 对`),
      kpi("识别编群", FMT.num(d.n_groups), `阈值 DTW ≤ ${(d.pairs && d.pairs.length) ? ((d.thresholds || {}).dtw_threshold ?? "—") : "—"}`),
      kpi("计算耗时", d.dtw_seconds !== undefined ? d.dtw_seconds.toFixed(1) + " 秒" : "—",
        `窗口 ${FMT.ts(w.t0)} → ${FMT.ts(w.t1)}`)));

    /* 编群卡片 */
    groupBox.innerHTML = "";
    if (!groups.length) {
      groupBox.appendChild(el("div", { class: "note" },
        "当前参数下未发现满足条件的编群。可尝试放宽 DTW 阈值或增大最大距离。"));
    } else {
      for (const g of groups) {
        groupBox.appendChild(el("div", {
          style: {
            border: "1px solid var(--line)", borderRadius: "8px", padding: "10px 12px",
            marginBottom: "10px", background: "rgba(56,189,248,.04)",
          },
        },
          el("div", { class: "toolbar", style: { marginBottom: "6px" } },
            el("span", { class: "tag acc", style: { fontSize: "12px", padding: "2px 10px" } },
              g.group_id + ` · ${g.member_count} 艘`),
            el("span", { class: "tag " + (g.confidence === "高" ? "ok" : g.confidence === "中" ? "warn" : "") },
              `置信度 ${g.confidence || "—"}`)),
          el("dl", { class: "kv" },
            el("dt", {}, "MMSI"), el("dd", {}, (g.mmis || []).join(", ")),
            el("dt", {}, "船型"), el("dd", {}, (g.ship_types || []).join(" / ")),
            el("dt", {}, "持续时间"), el("dd", {}, FMT.dt((g.duration_min || 0) * 60)),
            el("dt", {}, "最大 DTW"), el("dd", {}, (g.max_pair_dtw ?? 0).toFixed(3)),
            el("dt", {}, "最大间距"), el("dd", {}, FMT.m(g.max_sep_m)),
            el("dt", {}, "平均重叠率"), el("dd", {}, FMT.pct(g.mean_overlap || 0)),
            el("dt", {}, "航向差"), el("dd", {}, (g.mean_delta_cog ?? 0).toFixed(1) + "°"),
            el("dt", {}, "时间窗"), el("dd", {}, (g.time_window || []).map(FMT.ts).join(" → ")))));
      }
    }

    /* DTW 矩阵 */
    matrixBox.innerHTML = "";
    const pairs = d.pairs || [];
    if (pairs.length) {
      const ids = [];
      const idx = {};
      for (const p of pairs) {
        for (const [a, m] of [[p.a, p.mmsi_a], [p.b, p.mmsi_b]]) {
          if (!(a in idx)) { idx[a] = ids.length; ids.push(a); }
        }
      }
      const cap = Math.min(ids.length, 40);
      const xs = ids.slice(0, cap).map(id => shortId(id));
      const data = [];
      for (const p of pairs) {
        const i = idx[p.a], j = idx[p.b];
        if (i < cap && j < cap) {
          data.push([i, j, Number(p.dtw)]);
          data.push([j, i, Number(p.dtw)]);
        }
      }
      const el2 = el("div", { class: "chart", style: { height: Math.max(280, cap * 16) } });
      matrixBox.appendChild(el2);
      chartHeatmap(el2, xs, xs, data, { min: 0, colors: ["#0c1d33", "#1e3a5f", "#7c3aed", "#f472b6"] });
      matrixBox.appendChild(el("div", { class: "note", style: { marginTop: "6px" } },
        `矩阵展示前 ${cap} 条候选轨迹；未配对的格子表示未通过空间/时间预筛（不是 0 距离）。`));
    } else matrixBox.appendChild(empty("无候选船对"));

    /* 船对明细 */
    pairBox.innerHTML = "";
    if (pairs.length) {
      pairBox.appendChild(table([
        { label: "MMSI A", key: "mmsi_a" },
        { label: "MMSI B", key: "mmsi_b" },
        { label: "DTW", num: true, render: r => Number(r.dtw).toFixed(4) },
        { label: "平均间距", num: true, render: r => FMT.m(r.mean_sep_m) },
        { label: "时间重叠", num: true, render: r => FMT.pct(r.overlap_ratio) },
        { label: "航速差", num: true, render: r => (r.delta_sog ?? 0).toFixed(2) + " kn" },
        { label: "航向差", num: true, render: r => (r.delta_cog ?? 0).toFixed(1) + "°" },
        { label: "对齐步数", num: true, render: r => FMT.num(r.aligned_steps) },
        { label: "包围盒", num: true, render: r => r.bbox_m ? FMT.m(r.bbox_m) : "—" },
      ], pairs.slice(0, 300), { tall: true }));
    } else pairBox.appendChild(empty("无数据"));

    if ((d.notes || []).length) {
      pairBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        ...d.notes.map(n => el("div", {}, "· " + n))));
    }
  }
}

function shortId(id) { return String(id).slice(0, 6); }
