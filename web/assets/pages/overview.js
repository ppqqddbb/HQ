/* 总览工作台：数据规模、质量、时间分布、任务状态 */
import { state } from "../state.js";
import { listTasks } from "../taskrun.js";
import { statusTag, STATUS_CN } from "./_ui.js";

export async function render(view) {
  const box = el("div", {}, loading("正在汇总数据概况…"));
  view.appendChild(box);

  let o = null;
  try {
    o = await api(`/map/overview?dataset_version=${encodeURIComponent(state.dataset || "")}`);
    state.overview = { ...(state.overview || {}), ...o };
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(card("数据概况加载失败", null,
      el("div", { class: "note" }, String(e.message || e)),
      el("div", { style: { marginTop: "10px" } },
        el("button", { class: "btn", onclick: () => toast("请检查服务是否启动", true) }, "说明"))));
    return;
  }

  box.innerHTML = "";
  if (o.empty || !o.n_points) {
    box.appendChild(card("尚无可用数据集", null,
      el("div", { class: "note" },
        "当前没有已治理的数据集。请前往「文件与批次」页面导入 CSV 并执行数据治理。"),
      el("div", { style: { marginTop: "12px" } },
        el("a", { class: "btn primary", href: "#/datacenter" }, "前往数据中心"))));
    return;
  }

  const q = o.quality || {};
  const score = q.overall_score !== undefined && q.overall_score !== null ? q.overall_score : (o.avg_quality || 0);
  const t0 = (o.time_range || [])[0] || "";
  const t1 = (o.time_range || [])[1] || "";
  const spanHours = t0 && t1 ? (new Date(t1.replace(" ", "T")) - new Date(t0.replace(" ", "T"))) / 3600000 : null;
  const rate = spanHours ? o.n_points / spanHours : null;

  /* ---------- KPI ---------- */
  box.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
    kpi("有效轨迹点", FMT.big(o.n_points), `原始 ${FMT.big(q.row_raw || 0)} 条 · 去重率 ${FMT.pct(q.duplicate_rate || 0)}`),
    kpi("船舶目标（MMSI）", FMT.num(o.n_mmsi), `轨迹段 ${FMT.num(o.n_trajectories)} 条`),
    kpi("数据质量分", Number(score).toFixed(1) + " / 100",
      `点位平均 ${(o.avg_quality || 0).toFixed(1)} 分 · 异常点占比 ${q.error_ratio !== undefined ? FMT.pct(q.error_ratio) : "—"}`),
    kpi("覆盖时段", spanHours ? FMT.dt(spanHours * 3600) : "—",
      t0 ? `${FMT.ts(t0)} → ${FMT.ts(t1)}` : "—")));

  /* ---------- 时间/船型 ---------- */
  const hourly = o.hourly || [];
  const lc = el("div", { class: "chart" });
  const sc = el("div", { class: "chart" });
  box.appendChild(el("div", { class: "grid g23", style: { marginBottom: "14px" } },
    card("逐小时上报量", `小时级 · 峰值 ${FMT.big(Math.max(...hourly.map(h => h.n), 0))}`, lc),
    card("船型构成", "按轨迹点统计（前 12 类）", sc)));

  /* ---------- 速度分布 + 质量维度 ---------- */
  const vc = el("div", { class: "chart" });
  const gc = el("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px" } });
  const tc = el("div", {});
  const tcChart = el("div", { class: "chart", style: { height: "230px" } });
  tc.appendChild(tcChart);
  box.appendChild(el("div", { class: "grid g3", style: { marginBottom: "14px" } },
    card("航速分布（SOG）", `平均上报 ${rate ? FMT.num(rate) + " 点/小时" : "—"}`, vc),
    card("数据质量维度", q.overall_score ? `综合 ${q.overall_score} 分` : "—", gc),
    card("航速构成分析", "静止 / 低速 / 航行 三类占比", tc)));

  /* ---------- 高频船舶 + 数据资产 ---------- */
  const topBox = el("div", {});
  const dsBox = el("div", {});
  const taskBox = el("div", {});
  box.appendChild(el("div", { class: "grid g3", style: { marginBottom: "14px" } },
    card("最快船舶 TOP10", "按单点最大 SOG 排序", topBox),
    card("数据资产", "最近 10 个数据集版本", dsBox),
    card("任务状态", "按状态汇总", taskBox)));

  /* ---------- 渲染图表 ---------- */
  if (hourly.length) {
    chartBar(lc, hourly.map(h => String(h.h).padStart(2, "0") + "时"),
      hourly.map(h => h.n), { yName: "点数", labelFmt: v => FMT.big(v) });
  } else lc.appendChild(empty("无数据"));

  const st = (o.by_ship_type || []).filter(d => d.name && d.name !== "未知");
  if (st.length) {
    chartPie(sc, st.map(d => ({ name: d.name, value: d.n })));
  } else sc.appendChild(empty("无船型信息"));

  const sh = (o.speed_hist || []).filter(d => d.k !== null);
  if (sh.length) {
    chartBar(vc, sh.map(d => (d.k >= 30 ? "30+" : d.k) + " kn"), sh.map(d => d.n),
      {
        yName: "点数（对数轴）", labelFmt: v => FMT.big(v),
        yOpt: { type: "log", min: 1 },
      });
  } else vc.appendChild(empty("无航速数据"));

  const dims = q.dimensions || {};
  const wk = q.weights || {};
  const keys = Object.keys(dims);
  if (keys.length) {
    gc.appendChild(bars(keys.map(k => ({ label: k, value: Number(dims[k]), })),
      { fmt: v => v.toFixed(1) }));
    gc.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
      "权重：" + Object.entries(wk).map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`).join(" · ")));
    gc.appendChild(el("div", { class: "note", style: { marginTop: "6px" } },
      `驻留点 ${FMT.num(q.staypoints || 0)} 个 · 有效轨迹段 ${num(q.segments && q.segments.usable)} / ${num(q.segments && q.segments.n_segments)}`));
  } else gc.appendChild(empty("暂无质量报告"));

  const daily = o.daily || [];
  const buckets = { 静止: 0, 低速: 0, 航行: 0 };
  for (const d of sh) {
    const k = Number(d.k);
    if (k < 1) buckets["静止"] += d.n;
    else if (k < 5) buckets["低速"] += d.n;
    else buckets["航行"] += d.n;
  }
  const totalN = buckets["静止"] + buckets["低速"] + buckets["航行"];
  if (totalN > 0) {
    chartPie(tcChart, Object.entries(buckets).map(([name, value]) => ({ name, value })));
    tc.appendChild(el("div", { class: "note", style: { marginTop: "4px" } },
      `静止点占比 ${FMT.pct(buckets["静止"] / totalN)} —— 大量静止上报是 AIS 常态，` +
      `因此热点分析需按时间槽抽样、预测评估需分速度层报告。`));
  } else if (daily.length) {
    chartBar(tcChart, daily.map(d => d.d), daily.map(d => d.n), { yName: "点数", labelFmt: v => FMT.big(v) });
  } else tc.appendChild(empty("无数据"));

  /* 表格区 */
  const tops = o.top_speed_vessels || [];
  topBox.appendChild(topShipsTable(tops));

  const ds = o.datasets || [];
  dsBox.appendChild(table([
    { label: "版本", key: "dataset_version" },
    { label: "点数", num: true, render: r => FMT.big(r.row_count) },
    { label: "船舶", num: true, render: r => FMT.num(r.mmsi_count) },
    { label: "创建时间", render: r => FMT.ts(r.created_at) },
  ], ds));

  let tasks = [];
  try { tasks = await listTasks(60); } catch (e) { tasks = []; }
  const byStatus = {};
  for (const t of tasks) byStatus[t.status] = (byStatus[t.status] || 0) + 1;
  taskBox.appendChild(bars(Object.entries(byStatus).map(([k, v]) => ({ label: (STATUS_CN[k] || k), value: v }))));
  const recent = tasks.slice(0, 6);
  if (recent.length) {
    taskBox.appendChild(el("div", { style: { marginTop: "10px" } },
      table([
        { label: "算法", render: r => ALGO_CN[r.algorithm] || r.algorithm },
        { label: "状态", render: r => statusTag(r.status) },
        { label: "进度", num: true, render: r => Math.round((r.progress || 0) * 100) + "%" },
        { label: "创建", render: r => FMT.ts(r.created_at) },
      ], recent)));
  } else {
    taskBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
      "还没有分析任务。可在左侧「挖掘」菜单发起首次分析。"));
  }

  /* 已产出的分析结果入口 */
  box.appendChild(quickAccess());
}

const ALGO_CN = {
  hdbscan_hotspot: "热点区域", vessel_profile: "目标画像", correlate: "属性关联",
  formation_dtw: "编群分析", frequent_route: "频繁轨迹", predict: "航迹预测",
  clean: "数据治理",
};

function num(v) { return v === null || v === undefined ? "—" : FMT.num(v); }

function topShipsTable(rows) {
  if (!rows.length) return empty("无数据");
  return table([
    { label: "MMSI", key: "mmsi" },
    { label: "船名", render: r => (r.name && r.name !== "Unknown" ? String(r.name).slice(0, 16) : "未知") },
    { label: "船型", render: r => r.ship_type || "未知" },
    { label: "最大 SOG", num: true, render: r => (r.max_sog ?? 0).toFixed(1) + " kn" },
    { label: "点数", num: true, render: r => FMT.num(r.points) },
  ], rows, { tall: true });
}

/** 七个分析结果的一键入口（显示最近一次任务） */
function quickAccess() {
  const defs = [
    ["hotspot", "hdbscan_hotspot", "热点区域分析", "HDBSCAN 密度聚类"],
    ["profile", "vessel_profile", "船舶目标画像", "速度 / 航向 / 活动范围"],
    ["correlate", "correlate", "属性关联分析", "相关矩阵 + BH 校正"],
    ["formation", "formation_dtw", "船舶编群分析", "DTW + 层次聚类"],
    ["routes", "frequent_route", "频繁轨迹挖掘", "网格 / 转移 / 航线"],
    ["predict", "predict", "航迹预测", "CV / Kalman / GRU"],
  ];
  const notes = [];
  const wrap = el("div", { class: "grid g3" });
  for (const [page, algo, name, desc] of defs) {
    const note = el("div", { class: "note" }, "查询中…");
    const box = el("div", { class: "card", style: { cursor: "pointer" } },
      el("h3", {}, el("span", {}, name), el("span", { class: "sub" }, desc)), note);
    box.addEventListener("click", () => { location.hash = "#/" + page; });
    wrap.appendChild(box);
    notes.push({ note, algo });
  }
  const holder = card("分析结果直达", "点击卡片进入对应分析页（自动载入最近结果）", wrap);
  // 一次性拉取任务列表，避免每个卡片各发一次请求
  listTasks(80).then(ts => {
    for (const { note, algo } of notes) {
      const t = ts.find(x => x.algorithm === algo && x.status === "succeeded" &&
        (!state.dataset || x.dataset_version === state.dataset));
      note.textContent = t
        ? `最近运行：${FMT.ts(t.created_at)} · 耗时 ${dur(t)}`
        : "尚未运行，点击进入并发起分析";
    }
  }).catch(() => {
    for (const { note } of notes) note.textContent = "任务列表不可用";
  });
  return holder;
}

function dur(t) {
  // 任务列表接口只返回 created_at / finished_at，没有 started_at，
  // 之前只看 started_at 会导致所有卡片都显示「耗时 —」。
  // 用 created_at 兜底（任务排队时间极短，与执行耗时接近）。
  const begin = t.started_at || t.created_at;
  if (!t.finished_at || !begin) return "—";
  const s = (new Date(t.finished_at.replace(" ", "T")) - new Date(begin.replace(" ", "T"))) / 1000;
  return isFinite(s) && s >= 0 ? FMT.dt(s) : "—";
}
