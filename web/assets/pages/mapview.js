/* AIS 轨迹地图：视口内轨迹线 + 密度点 + 驻留点，支持点击查看单船轨迹 */
import { state } from "../state.js";
import { field, typeMultiSelect } from "./_ui.js";

let map = null;
let layerState = { traj: true, heat: true, stay: false };

/** 路由切换时释放地图资源 */
export function destroy() {
  if (map && map.destroy) { try { map.destroy(); } catch (e) { } }
  map = null;
}

export async function render(view) {
  destroy();
  const o = state.overview || {};
  const bbox = (o.bbox_robust && o.bbox_robust.length === 4) ? o.bbox_robust
    : (o.bbox && o.bbox.length === 4 ? o.bbox : [7.5, 54.0, 13.5, 58.2]);

  /* 工具条 */
  const shipTypes = (o.by_ship_type || []).map(d => d.name).filter(t => t && t !== "未知");
  const typeSel = typeMultiSelect(shipTypes);
  const t0In = el("input", { type: "datetime-local", style: { minWidth: "190px" } });
  const t1In = el("input", { type: "datetime-local", style: { minWidth: "190px" } });
  const limitIn = el("input", { type: "number", value: 200, min: 10, max: 1000, style: { minWidth: "80px" } });
  const applyBtn = el("button", { class: "btn primary" }, "加载轨迹");

  /* 图层开关 */
  const chkTraj = el("input", { type: "checkbox" }); chkTraj.checked = true;
  const chkHeat = el("input", { type: "checkbox" }); chkHeat.checked = true;
  const chkStay = el("input", { type: "checkbox" }); chkStay.checked = false;

  const toolbar = el("div", { class: "toolbar" },
    el("div", { class: "field" }, el("label", {}, "船型筛选"), typeSel.node),
    el("div", { class: "field" }, el("label", {}, "起始时间"), t0In),
    el("div", { class: "field" }, el("label", {}, "结束时间"), t1In),
    el("div", { class: "field" }, el("label", {}, "轨迹条数上限"), limitIn),
    el("div", { class: "field" }, el("label", {}, "轨迹线"), chkTraj),
    el("div", { class: "field" }, el("label", {}, "密度点"), chkHeat),
    el("div", { class: "field" }, el("label", {}, "驻留点"), chkStay),
    applyBtn);
  view.appendChild(toolbar);

  /* 地图容器 */
  const hud = el("div", { class: "map-hud" }, "缩放 6.0 · 0 条轨迹");
  const legend = el("div", { class: "map-legend" },
    el("div", {}, el("b", {}, "图例")),
    el("div", { style: { marginTop: "4px" } },
      el("span", { class: "tag" }, "轨迹线"), " ",
      el("span", { class: "tag vio" }, "密度点"), " ",
      el("span", { class: "tag warn" }, "驻留点")));
  const mapBox = el("div", { class: "mapbox" },
    el("div", { class: "chart map", id: "trajMap" }), hud, legend);
  view.appendChild(mapBox);

  const info = el("div", { style: { marginTop: "14px" } });
  view.appendChild(info);
  info.appendChild(card("船舶与轨迹清单", "点击行可在图上高亮单船轨迹", el("div", { id: "vesselList" })));

  map = new TileMap(document.getElementById("trajMap"), { fitBBox: bbox });
  const hudUpdate = () => {
    hud.textContent = `缩放 ${map.zoom.toFixed(1)} · 中心 ${map.center[0].toFixed(3)}, ${map.center[1].toFixed(3)}`;
  };
  const origDraw = map.draw.bind(map);
  map.draw = () => { origDraw(); hudUpdate(); };
  map.draw();

  let lastTraj = null;
  let lastPoints = null;
  let lastStay = null;

  function repaint() {
    const layers = [];
    if (layerState.heat && lastPoints && lastPoints.length) {
      layers.push(MapLayers.heat(lastPoints, { radius: 22 }));
    }
    if (layerState.traj && lastTraj && lastTraj.features) {
      layers.push(MapLayers.lines(lastTraj.features, {
        colorOf: f => colorByType(f.properties.ship_type),
        width: 1.3,
      }));
    }
    if (layerState.stay && lastStay && lastStay.length) {
      layers.push(MapLayers.points(lastStay, {
        radius: 4, colorOf: p => "rgba(251,191,36,.85)",
      }));
    }
    map.setLayers(layers);
  }

  chkTraj.addEventListener("change", () => { layerState.traj = chkTraj.checked; repaint(); });
  chkHeat.addEventListener("change", () => { layerState.heat = chkHeat.checked; repaint(); });
  chkStay.addEventListener("change", async () => {
    layerState.stay = chkStay.checked;
    if (chkStay.checked && !lastStay) await loadStay();
    repaint();
  });

  async function loadStay() {
    try {
      lastStay = await api(`/staypoints?dataset_version=${encodeURIComponent(state.dataset)}&limit=2000`);
      toast(`驻留点 ${lastStay.length} 个`);
    } catch (e) { toast(e.message, true); lastStay = []; }
  }

  const filters = () => {
    const f = {};
    const types = typeSel.values();
    if (types.length) f.ship_types = types;
    if (t0In.value) f.t0 = t0In.value.replace("T", " ");
    if (t1In.value) f.t1 = t1In.value.replace("T", " ");
    return f;
  };

  async function loadAll() {
    applyBtn.disabled = true;
    applyBtn.textContent = "加载中…";
    const f = filters();
    const qs = new URLSearchParams({ dataset_version: state.dataset, limit: String(limitIn.value || 150) });
    if (f.ship_types) qs.set("ship_types", f.ship_types.join(","));
    if (f.t0) qs.set("t0", f.t0);
    if (f.t1) qs.set("t1", f.t1);
    try {
      const [traj, pts, ves] = await Promise.all([
        api(`/map/trajectories?${qs.toString()}`),
        api(`/map/points?dataset_version=${encodeURIComponent(state.dataset)}&bbox=${bbox.join(",")}&zoom=${Math.round(map.zoom)}${f.ship_types ? "&ship_types=" + encodeURIComponent(f.ship_types.join(",")) : ""}${f.t0 ? "&t0=" + encodeURIComponent(f.t0) : ""}${f.t1 ? "&t1=" + encodeURIComponent(f.t1) : ""}`),
        api(`/vessels?${qs.toString()}`),
      ]);
      lastTraj = traj;
      lastPoints = pts.points || [];
      repaint();
      hud.textContent = `缩放 ${map.zoom.toFixed(1)} · ${traj.count} 条轨迹 · ${lastPoints.length} 个密度格`;
      renderVesselList(ves || []);
      toast(`已加载 ${traj.count} 条轨迹 / ${FMT.num(ves.length)} 艘船`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = "加载轨迹";
    }
  }

  function renderVesselList(rows) {
    const host = document.getElementById("vesselList");
    if (!host) return;
    host.innerHTML = "";
    if (!rows.length) { host.appendChild(empty("无船舶数据")); return; }
    host.appendChild(table([
      { label: "MMSI", key: "mmsi" },
      { label: "船名", render: r => r.name && r.name !== "Unknown" ? String(r.name).slice(0, 18) : "未知" },
      { label: "船型", render: r => r.ship_type || "—" },
      { label: "点数", num: true, render: r => FMT.num(r.points) },
      { label: "中位 SOG", num: true, render: r => (r.med_sog ?? 0).toFixed(1) + " kn" },
      { label: "最大 SOG", num: true, render: r => (r.max_sog ?? 0).toFixed(1) + " kn" },
      { label: "航段", num: true, render: r => FMT.num(r.segments) },
      {
        label: "操作", render: r => el("button", {
          class: "btn sm", onclick: () => showVessel(r.mmsi),
        }, "查看轨迹"),
      },
    ], rows, { tall: true }));
  }

  async function showVessel(mmsi) {
    try {
      const d = await api(`/vessels/${encodeURIComponent(mmsi)}/trajectory?dataset_version=${encodeURIComponent(state.dataset)}`);
      const pts = (d.points || []).map(p => [p.lon, p.lat]);
      if (pts.length < 2) return toast("该船轨迹点不足", true);
      const feats = [{ geometry: { type: "LineString", coordinates: pts }, properties: { mmsi } }];
      const stays = (d.staypoints || []).map(s => ({ lon: s.center_lon ?? s.lon, lat: s.center_lat ?? s.lat, n: 1 }));
      const layers = [
        MapLayers.lines(feats, { colorOf: () => "rgba(52,211,153,.95)", width: 2.2 }),
        MapLayers.points(stays, { radius: 5, colorOf: () => "rgba(251,191,36,.9)" }),
        MapLayers.labels([{ lon: pts[0][0], lat: pts[0][1], text: "起点", color: "#22d3ee" },
        { lon: pts[pts.length - 1][0], lat: pts[pts.length - 1][1], text: "终点", color: "#f472b6" }]),
      ];
      map.setLayers(layers);
      const lon = pts.map(p => p[0]), lat = pts.map(p => p[1]);
      map.fitBBox([Math.min(...lon), Math.min(...lat), Math.max(...lon), Math.max(...lat)]);
      hud.textContent = `单船 ${mmsi} · ${pts.length} 点 · 驻留 ${stays.length}`;
      toast(`已高亮船舶 ${mmsi}`);
    } catch (e) { toast(e.message, true); }
  }

  map.onClick = (lonlat) => {
    // 点击靠近某条轨迹起点时高亮（简单最近点匹配）
    if (!lastTraj || !lastTraj.features) return;
    let best = null, bd = 0.02;
    for (const f of lastTraj.features) {
      const c = f.geometry.coordinates[0];
      const d = Math.hypot(c[0] - lonlat[0], c[1] - lonlat[1]);
      if (d < bd) { bd = d; best = f; }
    }
    if (best) showVessel(best.properties.mmsi);
  };

  await loadAll();
  if (!chkStay.checked) { /* 驻留点按需加载 */ }
}

function colorByType(t) {
  const palette = {
    Cargo: "rgba(56,189,248,.72)",
    Tanker: "rgba(167,139,250,.72)",
    Passenger: "rgba(52,211,153,.72)",
    Fishing: "rgba(251,191,36,.72)",
    Tug: "rgba(244,114,182,.72)",
    Pleasure: "rgba(148,163,184,.6)",
    Sailing: "rgba(125,211,252,.6)",
    Pilot: "rgba(253,224,71,.72)",
    HSC: "rgba(34,211,238,.75)",
  };
  return palette[t] || "rgba(148,163,184,.45)";
}
