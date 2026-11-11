/* 热点区域分析：HDBSCAN 参数表单 → 异步任务 → 凸包/热力渲染 + 簇表格 + 敏感性 */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox, statusTag } from "./_ui.js";

const DEFAULTS = {
  min_cluster_size: 150, min_samples: 20, time_slot_minutes: 5,
  max_points: 150000, min_vessels: 8, grid_resolution: 0.01, top_n: 20,
  ship_types: "", t0: "", t1: "",
};

let map = null;
let clusters = [];
let result = null;

/** 路由切换时由 app.js 调用，释放 ResizeObserver 与 DOM 引用 */
export function destroy() {
  if (map && map.destroy) { try { map.destroy(); } catch (e) { } }
  map = null; clusters = []; result = null;
}

export async function render(view) {
  destroy();
  const f = form([
    { key: "min_cluster_size", label: "最小簇规模", type: "number", value: DEFAULTS.min_cluster_size, min: 5, step: 5, hint: "HDBSCAN min_cluster_size" },
    { key: "min_samples", label: "核心点邻域", type: "number", value: DEFAULTS.min_samples, min: 1, step: 1, hint: "越大簇越保守" },
    { key: "time_slot_minutes", label: "抽样时间槽", type: "number", value: DEFAULTS.time_slot_minutes, min: 1, unit: "分钟", hint: "每船每槽保留 1 点" },
    { key: "max_points", label: "参与聚类点数", type: "number", value: DEFAULTS.max_points, min: 1000, step: 10000 },
    { key: "min_vessels", label: "最少船舶数", type: "number", value: DEFAULTS.min_vessels, min: 1, hint: "低于此值的簇不参与排名" },
    { key: "top_n", label: "展示前 N 簇", type: "number", value: DEFAULTS.top_n, min: 5, step: 5 },
    { key: "ship_types", label: "船型筛选", type: "text", value: "", width: 140, placeholder: "Cargo,Tanker" },
    { key: "t0", label: "起始时间", type: "text", value: "", width: 160, placeholder: "2025-01-01 00:00:00" },
    { key: "t1", label: "结束时间", type: "text", value: "", width: 160, placeholder: "2025-01-02 00:00:00" },
  ]);

  const runBtn = el("button", { class: "btn primary" }, "运行热点分析");
  const head = el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "说明：按 MMSI + 时间槽抽样抑制上报频率偏差；交通流热点与停留热点分别标记；参数敏感性使用 Adjusted Rand Index 衡量稳定性。"));
  view.appendChild(head);

  const status = statusBox();
  view.appendChild(status);

  const sumBox = el("div", {});
  view.appendChild(sumBox);

  const mapHud = el("div", { class: "map-hud" }, "—");
  const mapBox = el("div", { class: "mapbox", style: { marginBottom: "14px" } },
    el("div", { class: "chart map", id: "hotmap" }), mapHud);
  const mapCard = card("热点空间分布", "凸包为簇边界（非行政/法定水域边界），颜色深浅表示密度", mapBox);
  mapCard.style.display = "none";
  view.appendChild(mapCard);

  const tableBox = el("div", {});
  const tableCard = card("热点簇列表", "点击行可在地图定位", tableBox);
  tableCard.style.display = "none";
  view.appendChild(tableCard);

  const sensBox = el("div", {});
  const sensCard = card("参数敏感性", "与基准参数的 Adjusted Rand Index（1 表示完全一致）", sensBox);
  sensCard.style.display = "none";
  view.appendChild(sensCard);

  const tsBox = el("div", {});
  const tsCard = card("簇时间强度曲线", "按小时统计簇内点数（TOP 5 簇）", tsBox);
  tsCard.style.display = "none";
  view.appendChild(tsCard);

  function ensureMap() {
    if (map) return map;
    map = new TileMap(document.getElementById("hotmap"), { fitBBox: [7.0, 54.0, 13.0, 58.0] });
    return map;
  }

  async function show(res) {
    result = res;
    clusters = res.clusters || [];
    sumBox.innerHTML = "";
    sumBox.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
      kpi("识别热点簇", FMT.num(res.n_clusters), `显著簇 ${FMT.num(res.n_significant_clusters ?? 0)}`),
      kpi("噪声占比", FMT.pct(res.noise_ratio || 0), "未归入任何簇的点"),
      kpi("参与聚类点数", FMT.num((res.sampling || {}).n_used || 0),
        `抽样前 ${FMT.big((res.sampling || {}).n_before_sampling || 0)}`),
      kpi("覆盖范围", fmtExtent(res.extent_robust || res.extent), "聚类点位经纬度包络")));

    if (res.sampling) {
      sumBox.appendChild(el("div", { class: "note", style: { marginBottom: "14px" } },
        `抽样方法：${res.sampling.method}；时间槽 ${res.sampling.time_slot_minutes} 分钟；` +
        `样本 ${FMT.big(res.sampling.n_sampled)} 点 → 使用 ${FMT.big(res.sampling.n_used)} 点。`));
    }

    /* 地图渲染 */
    mapCard.style.display = "";
    ensureMap();
    const hulls = clusters.filter(c => c.hull && c.hull.length > 3).map(c => ({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [c.hull] },
      properties: { id: c.cluster_id, rank: c.rank, kind: c.hotspot_kind },
    }));
    const heat = res.heat_grid || [];
    const layers = [];
    if (heat.length) layers.push(MapLayers.heat(heat, { radius: 24 }));
    else if (clusters.length) layers.push(MapLayers.points(clusters, {
      radius: 6, colorOf: c => c.hotspot_kind === "停留热点" ? "rgba(251,191,36,.8)" : "rgba(56,189,248,.8)",
    }));
    if (hulls.length) {
      layers.push(MapLayers.polygons(hulls, {
        colorOf: f => (f.properties.kind === "停留热点" ? "rgba(251,191,36,.18)" : "rgba(56,189,248,.16)"),
        stroke: "rgba(56,189,248,.6)",
      }));
      layers.push(MapLayers.labels(clusters.slice(0, 20).map(c => ({
        lon: c.center_lon, lat: c.center_lat, text: "#" + c.cluster_id,
      }))));
    }
    ensureMap().fitBBox(res.extent_robust || res.extent || [7, 54, 13, 58]);
    ensureMap().setLayers(layers);
    mapHud.textContent = `${res.n_clusters} 个簇 · 噪声 ${((res.noise_ratio || 0) * 100).toFixed(1)}%`;

    /* 簇表格 */
    tableCard.style.display = "";
    tableBox.innerHTML = "";
    const onlySig = el("input", { type: "checkbox" });
    onlySig.checked = false;
    const rowsHost = el("div", {});
    tableBox.appendChild(el("div", { class: "toolbar", style: { marginBottom: "8px" } },
      el("div", { class: "field" }, el("label", {}, "仅看主要热点（船舶数达标）"), onlySig),
      el("span", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
        `共 ${FMT.num(res.n_clusters)} 簇，其中主要热点 ${FMT.num(res.n_significant_clusters ?? 0)} 个；` +
        `当前列表显示前 ${FMT.num((res.clusters || []).length)} 条`)));
    tableBox.appendChild(rowsHost);

    const drawRows = () => {
      rowsHost.innerHTML = "";
      const src = onlySig.checked
        ? clusters.filter(c => c.significant)
        : clusters;
      if (!src.length) { rowsHost.appendChild(empty("当前筛选下无热点")); return; }
      rowsHost.appendChild(table([
        { label: "排名", num: true, render: r => "#" + r.rank },
        { label: "簇 ID", key: "cluster_id" },
        { label: "类型", render: r => el("span", { class: "tag " + (r.hotspot_kind && r.hotspot_kind.startsWith("停留") ? "warn" : "acc") }, r.hotspot_kind || "—") },
        { label: "点数", num: true, render: r => FMT.num(r.point_count) },
        { label: "船舶数", num: true, render: r => FMT.num(r.distinct_mmsi) },
        { label: "航次", num: true, render: r => FMT.num(r.distinct_traj) },
        { label: "中心", render: r => `${(r.center_lon ?? 0).toFixed(3)}, ${(r.center_lat ?? 0).toFixed(3)}` },
        { label: "面积", num: true, render: r => (r.area_km2 ?? 0).toFixed(1) + " km²" },
        { label: "均速", num: true, render: r => (r.avg_sog === null || r.avg_sog === undefined ? "—" : r.avg_sog.toFixed(1) + " kn") },
        { label: "驻留占比", num: true, render: r => FMT.pct(r.dwell_ratio || 0) },
        { label: "主航向", num: true, render: r => (r.main_cog_deg === null || r.main_cog_deg === undefined ? "—" : r.main_cog_deg.toFixed(0) + "°") },
        {
          label: "操作", render: r => el("button", {
            class: "btn sm", onclick: () => zoomCluster(r),
          }, "定位"),
        },
      ], src.slice(0, 300), { tall: true }));
    };
    onlySig.addEventListener("change", drawRows);
    drawRows();

    /* 敏感性 */
    const sens = res.sensitivity || {};
    if (sens.base) {
      sensCard.style.display = "";
      sensBox.innerHTML = "";
      const items = [{
        label: `基准 ${sens.base.min_cluster_size}/${sens.base.min_samples}`,
        value: sens.base.n_clusters,
        color: "linear-gradient(90deg,#22d3ee,#38bdf8)",
      }].concat((sens.variants || []).map(v => ({
        label: `${v.min_cluster_size}/${v.min_samples}`,
        value: v.n_clusters,
      })));
      sensBox.appendChild(el("div", { class: "grid g2" },
        el("div", {}, bars(items)),
        el("div", {}, table([
          { label: "最小簇规模", num: true, key: "min_cluster_size" },
          { label: "邻域样本", num: true, key: "min_samples" },
          { label: "簇数量", num: true, render: r => FMT.num(r.n_clusters) },
          { label: "噪声比", num: true, render: r => FMT.pct(r.noise_ratio) },
          { label: "ARI", num: true, render: r => r.stability_vs_base === undefined ? "基准" : r.stability_vs_base.toFixed(3) },
        ], (sens.variants || [])))));
      sensBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } }, sens.note || ""));
    }

    /* 时间序列 */
    const ts = res.time_series || [];
    if (ts.length) {
      tsCard.style.display = "";
      tsBox.innerHTML = "";
      const el2 = el("div", { class: "chart" });
      tsBox.appendChild(el2);
      const hours = (ts[0].points || []).map(p => p.t.slice(11, 16));
      chartLine(el2, hours, ts.slice(0, 5).map((s, i) => ({
        name: `簇 #${s.cluster_id}`,
        data: (s.points || []).map(p => p.n),
        smooth: false,
        showSymbol: false,
      })), { yName: "点数" });
    }
  }

  function zoomCluster(c) {
    if (!map) return;
    const hull = c.hull || [];
    if (hull.length > 3) {
      const lon = hull.map(p => p[0]), lat = hull.map(p => p[1]);
      map.fitBBox([Math.min(...lon), Math.min(...lat), Math.max(...lon), Math.max(...lat)], 80);
    } else map.setCenter(c.center_lon, c.center_lat, 9);
    toast(`定位到簇 #${c.cluster_id}（${c.point_count} 点）`);
  }

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    const filters = {};
    const ship = (v.ship_types || "").split(",").map(s => s.trim()).filter(Boolean);
    if (ship.length) filters.ship_types = ship;
    if (v.t0 || v.t1) filters.time_range = [v.t0 || null, v.t1 || null];
    const params = {
      min_cluster_size: v.min_cluster_size, min_samples: v.min_samples,
      time_slot_minutes: v.time_slot_minutes, max_points: v.max_points,
      min_vessels: v.min_vessels, top_n: v.top_n,
    };
    runBtn.disabled = true;
    try {
      const out = await runTask("hdbscan_hotspot", params, filters, {
        onProgress: t => status.set(t),
      });
      status.set(out.task);
      toast("热点分析完成");
      await show(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  /* 首屏：载入最近成功结果 */
  sumBox.appendChild(loading("正在查找最近一次热点分析结果…"));
  try {
    const t = await latestSucceeded("hdbscan_hotspot", state.dataset);
    if (t) {
      const s = await getSummary(t.task_id);
      sumBox.innerHTML = "";
      await show(s);
      status.set(t);
    } else {
      sumBox.innerHTML = "";
      sumBox.appendChild(card("还没有热点分析结果", null,
        el("div", { class: "note" }, "调整上方参数后点击「运行热点分析」。首轮抽样与聚类约需 1–3 分钟。")));
    }
  } catch (e) {
    sumBox.innerHTML = "";
    sumBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }
}

function fmtExtent(e) {
  if (!e || e.length !== 4) return "—";
  return `${(e[2] - e[0]).toFixed(1)}° × ${(e[3] - e[1]).toFixed(1)}°`;
}
