/* AIS 轨迹地图：视口内轨迹线 + 密度点 + 驻留点 + 单船回放 + 矩形空间筛选 */
import { state } from "../state.js";
import { field, typeMultiSelect } from "./_ui.js";

let map = null;
let layerState = { traj: true, heat: true, stay: false };

/* 回放状态：pts 为按时间排序的轨迹点，curMs 为当前播放到的数据时刻 */
const play = {
  pts: [], t0: 0, t1: 0, curMs: 0, idx: 0,
  playing: false, speed: 10, raf: null, lastTs: null, mmsi: null, btn: null,
};

/* 速度档位：真实 1 秒对应多少「数据分钟」 */
const PLAY_SPEEDS = [
  { label: "1 分钟/秒", v: 1 },
  { label: "10 分钟/秒", v: 10 },
  { label: "1 小时/秒", v: 60 },
  { label: "5 小时/秒", v: 300 },
];

/** 解析 "YYYY-MM-DD HH:mm:ss" 为毫秒（按 UTC 解析，只用于相对时间差） */
function parseTs(s) {
  if (!s) return 0;
  const t = Date.parse(String(s).replace(" ", "T") + "Z");
  return Number.isFinite(t) ? t : 0;
}

/** 毫秒 → HH:mm:ss（与 parseTs 对称，仅取时分秒） */
function fmtClock(ms) {
  const d = new Date(ms);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

/** 停止回放（模块级：路由切换时也要能安全调用） */
function stopPlay() {
  play.playing = false;
  play.lastTs = null;
  if (play.raf) { try { cancelAnimationFrame(play.raf); } catch (e) { } play.raf = null; }
  if (play.btn) play.btn.textContent = "播放";
}

/** 路由切换时释放地图资源 */
export function destroy() {
  stopPlay();
  if (map && map.destroy) { try { map.destroy(); } catch (e) { } }
  map = null;
  play.pts = [];
  play.btn = null;
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

  /* 空间筛选开关：开启后在地图上按住拖拽框选矩形范围 */
  const chkFence = el("input", { type: "checkbox" });
  const fenceInfo = el("span", { class: "tag", style: { display: "none" } }, "");

  const toolbar = el("div", { class: "toolbar" },
    el("div", { class: "field" }, el("label", {}, "船型筛选"), typeSel.node),
    el("div", { class: "field" }, el("label", {}, "起始时间"), t0In),
    el("div", { class: "field" }, el("label", {}, "结束时间"), t1In),
    el("div", { class: "field" }, el("label", {}, "轨迹条数上限"), limitIn),
    el("div", { class: "field" }, el("label", {}, "轨迹线"), chkTraj),
    el("div", { class: "field" }, el("label", {}, "密度点"), chkHeat),
    el("div", { class: "field" }, el("label", {}, "驻留点"), chkStay),
    el("div", { class: "field" }, el("label", {}, "框选范围"), chkFence),
    fenceInfo,
    applyBtn);
  view.appendChild(toolbar);

  /* 地图容器 */
  const hud = el("div", { class: "map-hud" }, "缩放 6.0 · 0 条轨迹");
  const legend = el("div", { class: "map-legend" },
    el("div", {}, el("b", {}, "图例")),
    el("div", { style: { marginTop: "4px" } },
      el("span", { class: "tag" }, "轨迹线"), " ",
      el("span", { class: "tag vio" }, "密度点"), " ",
      el("span", { class: "tag warn" }, "驻留点"), " ",
      el("span", { class: "tag ok" }, "已回放")),
    el("div", { style: { marginTop: "2px", color: "#8fa3bd" } },
      "勾选「框选范围」后在地图上拖拽可限定区域"));
  /* 回放控制条（浮于地图右下角） */
  const playBtn = el("button", { class: "btn sm primary", disabled: true, style: { minWidth: "58px" } }, "播放");
  play.btn = playBtn;
  const playRange = el("input", {
    type: "range", min: 0, max: 1000, value: 0, disabled: true,
    style: { flex: "1", minWidth: "150px" },
  });
  const speedSel = el("select", { style: { minWidth: "110px" } },
    ...PLAY_SPEEDS.map(s => el("option", { value: String(s.v) }, s.label)));
  speedSel.value = String(play.speed);
  const playTime = el("span", { style: { color: "#9db0c9", fontSize: "11.5px", minWidth: "150px" } }, "未选择轨迹");
  const playPanel = el("div", { class: "map-play" },
    el("div", { style: { display: "flex", gap: "8px", alignItems: "center" } },
      playBtn, playRange),
    el("div", { style: { display: "flex", gap: "8px", alignItems: "center", marginTop: "6px" } },
      el("span", { style: { color: "#8fa3bd" } }, "速度"), speedSel, playTime));
  playPanel.style.display = "none";

  const mapBox = el("div", { class: "mapbox" },
    el("div", { class: "chart map", id: "trajMap" }), hud, legend, playPanel);
  view.appendChild(mapBox);

  const info = el("div", { style: { marginTop: "14px" } });
  view.appendChild(info);
  info.appendChild(card("船舶与轨迹清单", "点击行可在图上高亮单船轨迹并进入回放", el("div", { id: "vesselList" })));

  map = new TileMap(document.getElementById("trajMap"), { fitBBox: bbox });
  /* HUD 文字由「地图状态 + 当前选中的单船」两部分组成，
     否则 fitBBox 触发的重绘会把单船信息覆盖掉。 */
  let hudNote = "";
  const hudUpdate = () => {
    hud.textContent = `缩放 ${map.zoom.toFixed(1)} · 中心 ${map.center[0].toFixed(3)}, ${map.center[1].toFixed(3)}`
      + (hudNote ? ` · ${hudNote}` : "");
  };
  const origDraw = map.draw.bind(map);
  map.draw = () => { origDraw(); hudUpdate(); };
  map.draw();

  let lastTraj = null;
  let lastPoints = null;
  let lastStay = null;

  /* 回放图层：常驻在图层栈顶端，无回放数据时不绘制 */
  const playLayer = { paint: paintPlayback };

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
    layers.push(playLayer);
    map.setLayers(layers);
  }

  /* ---------- 回放渲染 ---------- */

  function paintPlayback(m, ctx) {
    const pts = play.pts;
    if (!pts || pts.length < 2) return;
    const i = Math.min(play.idx, pts.length - 1);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    /* 全轨迹淡色底 */
    ctx.strokeStyle = "rgba(148,163,184,.30)";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (let k = 0; k < pts.length; k++) {
      const [x, y] = m.toScreen(pts[k].lon, pts[k].lat);
      if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    /* 已回放段 */
    if (i > 0) {
      ctx.strokeStyle = "rgba(52,211,153,.95)";
      ctx.lineWidth = 2.4;
      ctx.beginPath();
      for (let k = 0; k <= i; k++) {
        const [x, y] = m.toScreen(pts[k].lon, pts[k].lat);
        if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    /* 当前船位：圆点 + 航向箭头 */
    const cur = pts[i];
    const [cx, cy] = m.toScreen(cur.lon, cur.lat);
    const cog = Number(cur.cog);
    if (Number.isFinite(cog)) {
      /* 航向以正北为 0、顺时针计；地图 y 轴向下，故取 -(cog-90) */
      const rad = (cog - 90) * Math.PI / 180;
      const len = 13;
      const ax = cx + Math.cos(rad) * len;
      const ay = cy + Math.sin(rad) * len;
      ctx.strokeStyle = "rgba(251,191,36,.95)";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(ax, ay); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(ax - 5 * Math.cos(rad - 0.45), ay - 5 * Math.sin(rad - 0.45));
      ctx.lineTo(ax - 5 * Math.cos(rad + 0.45), ay - 5 * Math.sin(rad + 0.45));
      ctx.closePath();
      ctx.fillStyle = "rgba(251,191,36,.95)";
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = "#34d399";
    ctx.fill();
    ctx.strokeStyle = "rgba(6,20,32,.9)";
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }

  /* ---------- 回放控制 ---------- */

  function syncIdx() {
    const n = play.pts.length;
    if (!n) return;
    /* 二分定位「当前时刻之前最后一个点」 */
    let lo = 0, hi = n - 1, ans = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (play.pts[mid].t <= play.curMs) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    play.idx = ans;
    const span = (play.t1 - play.t0) || 1;
    const ratio = Math.max(0, Math.min(1, (play.curMs - play.t0) / span));
    playRange.value = String(Math.round(ratio * 1000));
    const cur = play.pts[ans] || {};
    const sog = Number.isFinite(Number(cur.sog)) ? `${Number(cur.sog).toFixed(1)} kn` : "—";
    playTime.textContent = `${fmtClock(play.curMs)} · SOG ${sog} · ${ans + 1}/${n} 点`;
  }

  function tick(ts) {
    if (!play.playing) return;
    if (play.lastTs == null) play.lastTs = ts;
    /* 限制单帧步长：页面切到后台时 rAF 会暂停，回来第一帧
       的时间差可能是几秒甚至几分钟，直接累加会把轨迹一下跳完。 */
    const dt = Math.min(120, Math.max(0, ts - play.lastTs));
    play.lastTs = ts;
    /* speed 的单位是「数据分钟 / 真实秒」 */
    play.curMs += dt * play.speed * 60;
    if (play.curMs >= play.t1) {
      play.curMs = play.t1;
      syncIdx();
      stopPlay();
      map.draw();
      toast(`回放结束：${play.mmsi || ""} 共 ${play.pts.length} 点`);
      return;
    }
    syncIdx();
    map.draw();
    play.raf = requestAnimationFrame(tick);
  }

  function startPlay() {
    if (play.pts.length < 2) return;
    if (play.curMs >= play.t1) play.curMs = play.t0;   // 已播完则重头开始
    play.playing = true;
    play.lastTs = null;
    playBtn.textContent = "暂停";
    playPanel.style.display = "";
    play.raf = requestAnimationFrame(tick);
  }

  function resetPlay() {
    stopPlay();
    play.pts = [];
    play.idx = 0;
    play.t0 = play.t1 = play.curMs = 0;
    play.mmsi = null;
    playBtn.disabled = true;
    playRange.disabled = true;
    playRange.value = "0";
    playTime.textContent = "未选择轨迹";
    playPanel.style.display = "none";
  }

  playBtn.addEventListener("click", () => {
    if (play.playing) stopPlay(); else startPlay();
  });
  playRange.addEventListener("input", () => {
    const n = play.pts.length;
    if (!n) return;
    const ratio = Number(playRange.value) / 1000;
    play.curMs = play.t0 + ratio * ((play.t1 - play.t0) || 0);
    syncIdx();
    map.draw();
  });
  speedSel.addEventListener("change", () => { play.speed = Number(speedSel.value) || 10; });

  /* ---------- 图层开关 ---------- */

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

  /* ---------- 矩形框选（空间筛选） ---------- */

  let fenceBBox = null;          // [minLon, minLat, maxLon, maxLat]
  let dragging = false;
  let dragStart = null;          // [x, y] 屏幕坐标
  const selDiv = el("div", { class: "map-select" });
  selDiv.style.display = "none";
  mapBox.appendChild(selDiv);

  /* 捕获阶段在容器上拦截：框选模式下禁止地图平移。
     注意不能把监听器绑到 canvas 本身——同一元素上的多个监听器
     按注册顺序执行，stopPropagation 拦不住 TileMap 自己的监听。 */
  const canvas = map.canvas;
  mapBox.addEventListener("mousedown", e => {
    if (!chkFence.checked || e.button !== 0 || e.target !== canvas) return;
    e.stopPropagation();
    e.preventDefault();
    dragging = true;
    dragStart = [e.offsetX, e.offsetY];
    selDiv.style.display = "";
    selDiv.style.left = `${e.offsetX}px`;
    selDiv.style.top = `${e.offsetY}px`;
    selDiv.style.width = "0px";
    selDiv.style.height = "0px";
  }, true);
  mapBox.addEventListener("mousemove", e => {
    if (!dragging || e.target !== canvas) return;
    e.stopPropagation();
    const x = e.offsetX, y = e.offsetY;
    selDiv.style.left = `${Math.min(x, dragStart[0])}px`;
    selDiv.style.top = `${Math.min(y, dragStart[1])}px`;
    selDiv.style.width = `${Math.abs(x - dragStart[0])}px`;
    selDiv.style.height = `${Math.abs(y - dragStart[1])}px`;
  }, true);
  mapBox.addEventListener("mouseup", (e) => {
    if (!dragging || e.target !== canvas) return;
    e.stopPropagation();
    dragging = false;
    const x = e.offsetX, y = e.offsetY;
    const a = map.unproject(dragStart[0], dragStart[1]);
    const b = map.unproject(x, y);
    if (Math.abs(x - dragStart[0]) < 8 || Math.abs(y - dragStart[1]) < 8) {
      selDiv.style.display = "none";
      return;                                  // 视为误触，忽略
    }
    fenceBBox = [Math.min(a[0], b[0]), Math.min(a[1], b[1]),
                 Math.max(a[0], b[0]), Math.max(a[1], b[1])];
    fenceInfo.style.display = "";
    fenceInfo.textContent =
      `已框选 ${(fenceBBox[2] - fenceBBox[0]).toFixed(2)}° × ${(fenceBBox[3] - fenceBBox[1]).toFixed(2)}°`;
    loadAll();
  }, true);

  chkFence.addEventListener("change", () => {
    canvas.style.cursor = chkFence.checked ? "crosshair" : "grab";
    if (!chkFence.checked) {
      selDiv.style.display = "none";
      fenceInfo.style.display = "none";
      fenceBBox = null;
      loadAll();
    } else {
      toast("在地图上按住拖拽，框选分析范围");
    }
  });

  /* ---------- 数据加载 ---------- */

  async function loadAll() {
    applyBtn.disabled = true;
    applyBtn.textContent = "加载中…";
    const f = filters();
    const qs = new URLSearchParams({ dataset_version: state.dataset, limit: String(limitIn.value || 150) });
    if (f.ship_types) qs.set("ship_types", f.ship_types.join(","));
    if (f.t0) qs.set("t0", f.t0);
    if (f.t1) qs.set("t1", f.t1);
    if (fenceBBox) qs.set("bbox", fenceBBox.map(v => v.toFixed(5)).join(","));
    try {
      const [traj, pts, ves] = await Promise.all([
        api(`/map/trajectories?${qs.toString()}`),
        api(`/map/points?dataset_version=${encodeURIComponent(state.dataset)}&bbox=${(fenceBBox || bbox).join(",")}&zoom=${Math.round(map.zoom)}${f.ship_types ? "&ship_types=" + encodeURIComponent(f.ship_types.join(",")) : ""}${f.t0 ? "&t0=" + encodeURIComponent(f.t0) : ""}${f.t1 ? "&t1=" + encodeURIComponent(f.t1) : ""}`),
        api(`/vessels?${qs.toString()}`),
      ]);
      lastTraj = traj;
      lastPoints = pts.points || [];
      resetPlay();
      repaint();
      hudNote = `${traj.count} 条轨迹 · ${lastPoints.length} 个密度格`;
      hudUpdate();
      renderVesselList(ves || []);
      toast(`已加载 ${traj.count} 条轨迹 / ${FMT.num((ves || []).length)} 艘船`);
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

  /** 载入单船轨迹：绘制图层并准备回放数据 */
  async function showVessel(mmsi) {
    try {
      /* 触发点通常在下方表格，先把地图滚回视野，否则用户看不到回放 */
      try { mapBox.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { }
      const d = await api(`/vessels/${encodeURIComponent(mmsi)}/trajectory?dataset_version=${encodeURIComponent(state.dataset)}`);
      const rows = (d.points || []).filter(p => Number.isFinite(Number(p.lon)) && Number.isFinite(Number(p.lat)));
      if (rows.length < 2) return toast("该船轨迹点不足", true);
      const feats = [{
        geometry: { type: "LineString", coordinates: rows.map(p => [p.lon, p.lat]) },
        properties: { mmsi },
      }];
      const stays = (d.staypoints || []).map(s => ({ lon: s.center_lon ?? s.lon, lat: s.center_lat ?? s.lat, n: 1 }));
      const layers = [
        MapLayers.lines(feats, { colorOf: () => "rgba(52,211,153,.35)", width: 1.6 }),
        MapLayers.points(stays, { radius: 5, colorOf: () => "rgba(251,191,36,.9)" }),
        MapLayers.labels([{ lon: rows[0].lon, lat: rows[0].lat, text: "起点", color: "#22d3ee" },
        { lon: rows[rows.length - 1].lon, lat: rows[rows.length - 1].lat, text: "终点", color: "#f472b6" }]),
        playLayer,
      ];
      map.setLayers(layers);

      /* 准备回放：按时间排序、换算相对时刻 */
      const pts = rows.map(p => ({
        lon: Number(p.lon), lat: Number(p.lat),
        sog: Number(p.sog), cog: Number(p.cog ?? p.heading),
        t: parseTs(p.event_time),
      })).filter(p => p.t > 0);
      pts.sort((a, b) => a.t - b.t);
      play.pts = pts;
      play.t0 = pts.length ? pts[0].t : 0;
      play.t1 = pts.length ? pts[pts.length - 1].t : 0;
      play.curMs = play.t0;
      play.idx = 0;
      play.mmsi = String(mmsi);
      playBtn.disabled = !(pts.length > 1);
      playRange.disabled = !(pts.length > 1);
      playPanel.style.display = "";
      syncIdx();

      const lon = rows.map(p => Number(p.lon)), lat = rows.map(p => Number(p.lat));
      map.fitBBox([Math.min(...lon), Math.min(...lat), Math.max(...lon), Math.max(...lat)]);
      const total = Number(d.total_points) || rows.length;
      hudNote = `单船 ${mmsi} · ${FMT.num(rows.length)}${total > rows.length ? `/${FMT.num(total)}` : ""} 点 · 驻留 ${stays.length}`;
      hudUpdate();
      toast(total > rows.length
        ? `已载入 ${mmsi}（${FMT.num(total)} 点中等间隔抽样 ${rows.length} 点），可点击播放`
        : `已载入 ${mmsi}，可拖动时间轴或点击播放`);
    } catch (e) { toast(e.message, true); }
  }

  map.onClick = (lonlat) => {
    if (chkFence.checked) return;          // 框选模式下不触发选中
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
