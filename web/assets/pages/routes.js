/* 频繁轨迹挖掘：规则网格热力 + 有向转移边 + 频繁航线（连续网格序列） */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox } from "./_ui.js";

let map = null;
let d = null;
let layers = { heat: true, edges: true, route: true };

export async function render(view) {
  const f = form([
    {
      key: "grid_mode", label: "网格方式", type: "select", value: "rows_cols", numeric: false,
      options: [{ v: "rows_cols", t: "按行×列均分" }, { v: "cell_size", t: "按固定边长" }],
    },
    { key: "grid_rows", label: "行数", type: "number", value: 10, min: 2, max: 60 },
    { key: "grid_cols", label: "列数", type: "number", value: 10, min: 2, max: 60 },
    { key: "cell_size_m", label: "网格边长", type: "number", value: 5000, min: 200, step: 500, unit: "米", hint: "固定边长模式生效" },
    { key: "min_support_count", label: "最小支持航次", type: "number", value: 3, min: 1, step: 1 },
    { key: "min_pattern_len", label: "最短路长", type: "number", value: 3, min: 2, step: 1 },
    { key: "max_pattern_len", label: "最长路长", type: "number", value: 8, min: 3, step: 1 },
    { key: "top_k", label: "输出航线数", type: "number", value: 20, min: 5, step: 5 },
  ]);
  const runBtn = el("button", { class: "btn primary" }, "运行频繁轨迹挖掘");

  view.appendChild(el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "支持度按「不同航次数」去重：同一航次在同一网格产生更多报文不增加支持度。方向保留，u→v 与 v→u 为不同转移边。当前数据无法识别真实港到港航次，以轨迹段作为航次代理。")));

  const status = statusBox();
  view.appendChild(status);

  const kBox = el("div", {});
  const hud = el("div", { class: "map-hud" }, "—");
  const mapBox = el("div", { class: "mapbox chart map", id: "routeMap", style: { marginBottom: "14px" } });
  const layerCtl = el("div", { class: "toolbar", style: { marginBottom: "10px" } });
  view.appendChild(kBox);
  view.appendChild(layerCtl);
  view.appendChild(mapBox);

  const routeBox = el("div", {});
  const cellBox = el("div", {});
  const edgeBox = el("div", {});
  view.appendChild(el("div", { class: "grid g23", style: { marginBottom: "14px", alignItems: "start" } },
    card("频繁航线 TOP", "连续网格序列模式，含支持度与几何路径", routeBox),
    card("高频网格 TOP", "按支持航次数排序", cellBox)));
  view.appendChild(card("转移边明细", "有向转移与支持度", edgeBox));

  /* 图层开关 */
  const mkChk = (key, label, color) => {
    const c = el("input", { type: "checkbox" });
    c.checked = layers[key];
    c.addEventListener("change", () => { layers[key] = c.checked; repaint(); });
    return el("div", { class: "field" }, el("label", {}, label), c);
  };
  layerCtl.appendChild(el("span", { style: { fontSize: "11px", color: "var(--txt3)" } }, "图层："));
  layerCtl.appendChild(mkChk("heat", "网格热力"));
  layerCtl.appendChild(mkChk("edges", "转移边"));
  layerCtl.appendChild(mkChk("route", "频繁航线"));
  const fitBtn = el("button", { class: "btn sm" }, "重置视野");
  fitBtn.addEventListener("click", () => { if (d && d.grid) map.fitBBox(d.grid.extent); });
  layerCtl.appendChild(fitBtn);

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    runBtn.disabled = true;
    try {
      const out = await runTask("frequent_route", {
        grid_mode: v.grid_mode, grid_rows: v.grid_rows, grid_cols: v.grid_cols,
        cell_size_m: v.cell_size_m, min_support_count: v.min_support_count,
        min_pattern_len: v.min_pattern_len, max_pattern_len: v.max_pattern_len,
        top_k: v.top_k,
      }, {}, { onProgress: t => status.set(t) });
      status.set(out.task);
      toast("频繁轨迹挖掘完成");
      show(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  kBox.appendChild(loading("正在加载频繁轨迹结果…"));
  try {
    const t = await latestSucceeded("frequent_route", state.dataset);
    if (t) { status.set(t); kBox.innerHTML = ""; show(await getSummary(t.task_id)); }
    else {
      kBox.innerHTML = "";
      kBox.appendChild(card("还没有频繁轨迹结果", null,
        el("div", { class: "note" }, "点击「运行频繁轨迹挖掘」生成网格统计、转移边与频繁航线。")));
    }
  } catch (e) {
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }

  /* 网格单元几何补全（由 extent + rows/cols 推导经纬度包围盒） */
  function enrichCells(g, cells) {
    const [x0, y0, x1, y1] = g.extent;
    const w = (x1 - x0) / g.cols, h = (y1 - y0) / g.rows;
    const byId = {};
    for (let r = 0; r < g.rows; r++) {
      for (let c = 0; c < g.cols; c++) {
        const id = r * g.cols + c;
        byId[id] = {
          grid_id: id, row_no: r, col_no: c,
          minlon: x0 + c * w, maxlon: x0 + (c + 1) * w,
          maxlat: y1 - r * h, minlat: y1 - (r + 1) * h,
          cx: x0 + (c + 0.5) * w, cy: y1 - (r + 0.5) * h,
          support_ratio: 0, distinct_voyage_count: 0, distinct_mmsi_count: 0,
          observed_point_count: 0, visit_event_count: 0,
        };
      }
    }
    for (const c of cells || []) {
      const t = byId[c.grid_id];
      if (t) Object.assign(t, {
        support_ratio: c.support_ratio, distinct_voyage_count: c.distinct_voyage_count,
        distinct_mmsi_count: c.distinct_mmsi_count, observed_point_count: c.observed_point_count,
        visit_event_count: c.visit_event_count,
      });
    }
    return byId;
  }

  function repaint() {
    if (!map || !d) return;
    const g = d.grid || {};
    const byId = enrichCells(g, d.cells);
    const all = Object.values(byId);
    const observed = all.filter(c => c.distinct_voyage_count > 0);
    const ls = [];
    if (layers.heat) {
      ls.push(MapLayers.gridCells(observed.length ? observed : all, { metric: "support_ratio" }));
    }
    if (layers.edges && (d.edges || []).length) {
      ls.push(MapLayers.edges(d.edges, byId));
    }
    if (layers.route) {
      const routes = (d.routes || []).slice(0, 5);
      routes.forEach((r, i) => {
        if ((r.route_geometry || []).length > 1) {
          ls.push(MapLayers.route(r));
        }
      });
    }
    map.setLayers(ls);
    hud.textContent = `${d.n_observed_cells} 个高频网格 · ${d.n_edges} 条转移边 · ${d.n_routes} 条航线`;
  }

  function show(res) {
    d = res;
    const g = res.grid || {};

    if (!map) {
      map = new TileMap(document.getElementById("routeMap"), { fitBBox: g.extent || [7, 54, 13, 58] });
    } else {
      map.fitBBox(g.extent || [7, 54, 13, 58]);
    }
    repaint();

    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
      kpi("网格规模", `${g.rows} × ${g.cols}`, `单格约 ${(g.cell_width_m / 1000).toFixed(1)} × ${(g.cell_height_m / 1000).toFixed(1)} km`),
      kpi("统计单位（航次）", FMT.num(res.n_units), res.counting_unit_note ? "以轨迹段代理航次" : "—"),
      kpi("高频网格", FMT.num(res.n_observed_cells), `转移边 ${FMT.num(res.n_edges)} 条`),
      kpi("频繁航线", FMT.num(res.n_routes), `最大频率 ${res.routes && res.routes.length ? FMT.num(res.routes[0].support_count) : "—"} 航次`)));

    /* 航线表 */
    routeBox.innerHTML = "";
    const routes = res.routes || [];
    if (routes.length) {
      routeBox.appendChild(table([
        { label: "排名", num: true, render: r => "#" + r.rank },
        { label: "网格序列", render: r => (r.grid_sequence || []).join(" → ") },
        { label: "长度", num: true, render: r => FMT.num(r.length) },
        { label: "支持航次", num: true, render: r => FMT.num(r.support_count) },
        { label: "支持度", num: true, render: r => FMT.pct(r.support_ratio) },
        { label: "船舶数", num: true, render: r => FMT.num(r.distinct_mmsi_count) },
        {
          label: "操作", render: r => el("button", {
            class: "btn sm", onclick: () => {
              const pts = r.route_geometry || [];
              if (pts.length > 1) {
                const lon = pts.map(p => p[0]), lat = pts.map(p => p[1]);
                map.fitBBox([Math.min(...lon), Math.min(...lat), Math.max(...lon), Math.max(...lat)], 90);
              }
              layers.route = true;
              repaint();
              toast(`已定位航线 #${r.rank}`);
            },
          }, "定位"),
        },
      ], routes, { tall: true }));
    } else routeBox.appendChild(empty("未挖掘到满足条件的频繁航线"));

    /* 高频网格 */
    cellBox.innerHTML = "";
    const cells = res.cells_top || (res.cells || []).slice(0, 20);
    if (cells.length) {
      cellBox.appendChild(table([
        { label: "网格", num: true, key: "grid_id" },
        { label: "行/列", render: r => `${r.row_no}/${r.col_no}` },
        { label: "支持航次", num: true, render: r => FMT.num(r.distinct_voyage_count) },
        { label: "支持度", num: true, render: r => FMT.pct(r.support_ratio) },
        { label: "船舶数", num: true, render: r => FMT.num(r.distinct_mmsi_count) },
        { label: "报文量", num: true, render: r => FMT.big(r.observed_point_count) },
      ], cells, { tall: true }));
    } else cellBox.appendChild(empty("无高频网格"));

    /* 转移边 */
    edgeBox.innerHTML = "";
    const edges = (res.edges || []).slice(0, 200);
    if (edges.length) {
      edgeBox.appendChild(table([
        { label: "起点网格", num: true, key: "from_grid" },
        { label: "终点网格", num: true, key: "to_grid" },
        { label: "支持航次", num: true, render: r => FMT.num(r.distinct_voyage_count) },
        { label: "支持度", num: true, render: r => FMT.pct(r.support_ratio) },
        { label: "转移事件", num: true, render: r => FMT.num(r.transition_event_count) },
      ], edges, { tall: true }));
    } else edgeBox.appendChild(empty("无转移边数据"));

    if ((res.notes || []).length) {
      edgeBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        ...res.notes.map(n => el("div", {}, "· " + n))));
    }
  }
}
