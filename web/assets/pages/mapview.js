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
  if (map && map.__cleanup) { try { map.__cleanup(); } catch (e) { } }
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

  /* 空间筛选：pickMode = none | rect | poly | circle
     - rect  ：按住拖拽得到矩形 bbox
     - poly  ：逐点点击，双击/回车闭合多边形
     - circle：按住拖拽得到圆形（圆心 = 起点，半径 = 拖拽距离） */
  const pickModeSel = el("select", { style: { minWidth: "96px" } },
    el("option", { value: "none" }, "不筛选"),
    el("option", { value: "rect" }, "矩形"),
    el("option", { value: "poly" }, "多边形"),
    el("option", { value: "circle" }, "圆形"));
  const fenceInfo = el("span", { class: "tag", style: { display: "none" } }, "");
  const resetFenceBtn = el("button", { class: "btn sm ghost", style: { display: "none" } }, "\u6e05\u9664\u8303\u56f4");

  const toolbar = el("div", { class: "toolbar" },
    el("div", { class: "field" }, el("label", {}, "船型筛选"), typeSel.node),
    el("div", { class: "field" }, el("label", {}, "起始时间"), t0In),
    el("div", { class: "field" }, el("label", {}, "结束时间"), t1In),
    el("div", { class: "field" }, el("label", {}, "轨迹条数上限"), limitIn),
    el("div", { class: "field" }, el("label", {}, "轨迹线"), chkTraj),
    el("div", { class: "field" }, el("label", {}, "密度点"), chkHeat),
    el("div", { class: "field" }, el("label", {}, "驻留点"), chkStay),
    el("div", { class: "field" }, el("label", {}, "空间筛选"), pickModeSel),
    fenceInfo, resetFenceBtn,
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
      "空间筛选支持矩形拖拽 / 多边形逐点点击（双击闭合）/ 圆形拖拽"));
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

  /* 叠加图层（多边形预览 / 圆形预览 / 已确认筛区）。
     先建空数组再在下面 push，避免 repaint() 在常量定义前被调用时触发 TDZ。 */
  const overlayLayers = [];

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
    for (const ly of overlayLayers) layers.push(ly);
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

  /* ---------- 空间筛选（矩形 / 多边形 / 圆形，技术方案 11.2） ----------
     三种模式共用一套拖拽/点击交互，区别只在最终生成的过滤参数：
     - rect   → bbox=[minLon,minLat,maxLon,maxLat]
     - poly   → polygon=[[lon,lat],...]（逐点点击，双击闭合）
     - circle → circle=[lon,lat,radius_km]
     所有参数都下推到后端 SQL（射线法 / 等距投影近似），不是前端假过滤。 */

  let fenceBBox = null;          // 矩形：[minLon, minLat, maxLon, maxLat]
  let fencePoly = null;          // 多边形：[[lon, lat], ...]
  let fenceCircle = null;        // 圆形：[lon, lat, radiusKm]
  let dragging = false;
  let dragStart = null;          // [x, y] 屏幕坐标
  let polyPts = [];              // 绘制中的多边形顶点（经纬度）
  let polyHover = null;          // 多边形绘制鼠标位置（橡皮筋预览）
  let lastPtScreen = null;       // 上一个顶点的屏幕坐标，用于去重双击重复点
  const selDiv = el("div", { class: "map-select" });
  selDiv.style.display = "none";
  mapBox.appendChild(selDiv);

  const pickMode = () => pickModeSel.value;
  const hasFence = () => !!(fenceBBox || fencePoly || fenceCircle);

  /** 把当前空间筛选追加到查询参数上；供轨迹/点/船舶三个接口统一使用。 */
  const appendFence = (qs) => {
    if (fenceBBox) qs.set("bbox", fenceBBox.map(v => v.toFixed(5)).join(","));
    if (fencePoly) qs.set("polygon", JSON.stringify(fencePoly.map(p => [+p[0].toFixed(5), +p[1].toFixed(5)])));
    if (fenceCircle) qs.set("circle", fenceCircle.map(v => +Number(v).toFixed(5)).join(","));
  };

  /** 多边形筛选时计算外包围盒，供密度点聚合做粗筛。 */
  const polyBBox = () => {
    if (!fencePoly) return null;
    const lons = fencePoly.map(p => p[0]), lats = fencePoly.map(p => p[1]);
    return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
  };

  const circleBBox = () => {
    if (!fenceCircle) return null;
    const [lon, lat, r] = fenceCircle;
    const dlat = r / 110.57;
    const dlon = r / (111.32 * Math.max(Math.cos(lat * Math.PI / 180), 1e-6));
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat];
  };

  /** 当前视野 bbox：优先空间筛选范围（保证聚合格落在筛区内）。 */
  const viewBBox = () => fenceBBox || polyBBox() || circleBBox() || bbox;

  const updateFenceUI = () => {
    const show = hasFence();
    fenceInfo.style.display = show ? "" : "none";
    resetFenceBtn.style.display = show ? "" : "none";
    if (!show) return;
    if (fenceBBox) {
      fenceInfo.textContent =
        `已框选 ${(fenceBBox[2] - fenceBBox[0]).toFixed(2)}° × ${(fenceBBox[3] - fenceBBox[1]).toFixed(2)}°`;
    } else if (fencePoly) {
      fenceInfo.textContent = `多边形 ${fencePoly.length} 个顶点`;
    } else if (fenceCircle) {
      fenceInfo.textContent = `圆形 半径 ${Number(fenceCircle[2]).toFixed(1)} km`;
    }
  };

  /** 清空所有空间筛选（切换模式或手动清除时调用）。 */
  const clearFence = () => {
    fenceBBox = null; fencePoly = null; fenceCircle = null;
    polyPts = []; polyHover = null; lastPtScreen = null;
    selDiv.style.display = "none";
    updateFenceUI();
    repaint();
  };

  /* 捕获阶段在容器上拦截：筛选模式下禁止地图平移。
     注意不能把监听器绑到 canvas 本身——同一元素上的多个监听器
     按注册顺序执行，stopPropagation 拦不住 TileMap 自己的监听。 */
  const canvas = map.canvas;

  /* 多边形预览图层：顶点之间的连线 + 鼠标位置的橡皮筋 */
  const polyLayer = {
    paint(m, ctx) {
      if (!polyPts.length) return;
      const scr = polyPts.map(p => m.toScreen(p[0], p[1]));
      if (polyHover) scr.push(m.toScreen(polyHover[0], polyHover[1]));
      ctx.strokeStyle = "rgba(34,211,238,.95)";
      ctx.lineWidth = 1.8; ctx.setLineDash([5, 4]);
      ctx.beginPath();
      scr.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      ctx.setLineDash([]);
      /* 顶点圆点（首个顶点标红，便于看出闭合方向） */
      scr.slice(0, polyPts.length).forEach(([x, y], i) => {
        ctx.beginPath(); ctx.arc(x, y, i === 0 ? 4.5 : 3, 0, 7);
        ctx.fillStyle = i === 0 ? "#f472b6" : "#22d3ee";
        ctx.fill();
      });
    },
  };

  /* 已确认的多边形/圆形边界也要一直画出来 */
  const fenceLayer = {
    paint(m, ctx) {
      if (fencePoly) {
        const scr = fencePoly.map(p => m.toScreen(p[0], p[1]));
        ctx.beginPath();
        scr.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.fillStyle = "rgba(34,211,238,.08)"; ctx.fill();
        ctx.strokeStyle = "rgba(34,211,238,.85)";
        ctx.lineWidth = 1.8; ctx.stroke();
      }
      if (fenceCircle) {
        const [lon, lat, r] = fenceCircle;
        const [cx, cy] = m.toScreen(lon, lat);
        const [ex] = m.toScreen(lon + r / (111.32 * Math.max(Math.cos(lat * Math.PI / 180), 1e-6)), lat);
        const rpx = Math.abs(ex - cx);
        ctx.beginPath(); ctx.arc(cx, cy, Math.max(2, rpx), 0, 7);
        ctx.fillStyle = "rgba(34,211,238,.08)"; ctx.fill();
        ctx.strokeStyle = "rgba(34,211,238,.85)";
        ctx.lineWidth = 1.8; ctx.stroke();
      }
    },
  };

  /* 把两个叠加图层注册到 repaint 的图层栈（多边形预览 + 已确认筛区） */
  overlayLayers.push(polyLayer, fenceLayer);

  mapBox.addEventListener("mousedown", e => {
    if (pickMode() === "none" || e.button !== 0 || e.target !== canvas) return;
    e.stopPropagation();
    e.preventDefault();
    if (pickMode() === "poly") {
      /* 多边形：单击添加顶点；双击闭合由 dblclick 处理。
         双击会先触发两次 mousedown，这里按屏幕距离去重，
         否则最终多边形会多出两个重合顶点。 */
      const sx = e.offsetX, sy = e.offsetY;
      if (lastPtScreen && Math.hypot(sx - lastPtScreen[0], sy - lastPtScreen[1]) < 6) return;
      lastPtScreen = [sx, sy];
      const ll = map.unproject(sx, sy);
      polyPts.push([ll[0], ll[1]]);
      polyHover = null;
      map.draw();
      return;
    }
    dragging = true;
    dragStart = [e.offsetX, e.offsetY];
    selDiv.style.display = "";
    selDiv.style.left = `${e.offsetX}px`;
    selDiv.style.top = `${e.offsetY}px`;
    selDiv.style.width = "0px";
    selDiv.style.height = "0px";
    if (pickMode() === "circle") {
      selDiv.style.borderRadius = "50%";
      selDiv.style.borderStyle = "dashed";
    } else {
      selDiv.style.borderRadius = "0";
      selDiv.style.borderStyle = "solid";
    }
  }, true);

  mapBox.addEventListener("mousemove", e => {
    if (e.target !== canvas) return;
    if (pickMode() === "poly" && polyPts.length) {
      /* 悬停时实时预览「下一条边」 */
      polyHover = map.unproject(e.offsetX, e.offsetY);
      map.draw();
      return;
    }
    if (!dragging) return;
    e.stopPropagation();
    const x = e.offsetX, y = e.offsetY;
    if (pickMode() === "circle") {
      const rp = Math.hypot(x - dragStart[0], y - dragStart[1]);
      selDiv.style.left = `${dragStart[0] - rp}px`;
      selDiv.style.top = `${dragStart[1] - rp}px`;
      selDiv.style.width = `${rp * 2}px`;
      selDiv.style.height = `${rp * 2}px`;
    } else {
      selDiv.style.left = `${Math.min(x, dragStart[0])}px`;
      selDiv.style.top = `${Math.min(y, dragStart[1])}px`;
      selDiv.style.width = `${Math.abs(x - dragStart[0])}px`;
      selDiv.style.height = `${Math.abs(y - dragStart[1])}px`;
    }
  }, true);

  mapBox.addEventListener("mouseup", (e) => {
    if (!dragging || e.target !== canvas) return;
    e.stopPropagation();
    dragging = false;
    const x = e.offsetX, y = e.offsetY;
    const mode = pickMode();

    if (mode === "circle") {
      selDiv.style.display = "none";
      const a = map.unproject(dragStart[0], dragStart[1]);
      /* Web 遗卡托是等角投影：同一屏幕上横向与纵向的比例尺一致，
         因此「像素半径 × 每像素纬度度数 × 110.57 km」就是真实半径。 */
      const degPerPx = Math.abs(map.unproject(0, 0)[1] - map.unproject(0, 1)[1]);
      const rpx = Math.hypot(x - dragStart[0], y - dragStart[1]);
      const rKm = rpx * degPerPx * 110.57;
      if (rKm < 1) return;                    // 误触，忽略
      fenceBBox = null; fencePoly = null;
      fenceCircle = [a[0], a[1], +rKm.toFixed(2)];
      updateFenceUI();
      repaint();
      loadAll();
      return;
    }

    /* 矩形 */
    const a = map.unproject(dragStart[0], dragStart[1]);
    const b = map.unproject(x, y);
    if (Math.abs(x - dragStart[0]) < 8 || Math.abs(y - dragStart[1]) < 8) {
      selDiv.style.display = "none";
      return;                                  // 视为误触，忽略
    }
    fenceBBox = [Math.min(a[0], b[0]), Math.min(a[1], b[1]),
                 Math.max(a[0], b[0]), Math.max(a[1], b[1])];
    fencePoly = null; fenceCircle = null;
    updateFenceUI();
    repaint();
    loadAll();
  }, true);

  /* 双击闭合多边形（顶点数 ≥ 3 时生效） */
  canvas.addEventListener("dblclick", e => {
    if (pickMode() !== "poly") return;
    e.stopPropagation();
    e.preventDefault();
    if (polyPts.length < 3) { toast("多边形至少需要 3 个顶点", true); return; }
    fenceBBox = null; fenceCircle = null;
    fencePoly = polyPts.map(p => [p[0], p[1]]);
    polyPts = [];
    polyHover = null;
    lastPtScreen = null;
    updateFenceUI();
    repaint();
    loadAll();
  }, true);

  /* Esc 取消正在绘制中的多边形 */
  const onKeyEsc = (e) => {
    if (e.key === "Escape" && polyPts.length) {
      polyPts = [];
      polyHover = null;
      lastPtScreen = null;
      repaint();
      toast("已取消多边形绘制");
    }
  };
  window.addEventListener("keydown", onKeyEsc);
  /* 路由切走时移除全局监听，避免在其它页面误响应 */
  map.__cleanup = () => window.removeEventListener("keydown", onKeyEsc);

  pickModeSel.addEventListener("change", () => {
    const mode = pickMode();
    canvas.style.cursor = mode === "none" ? "grab" : "crosshair";
    clearFence();
    if (mode === "none") {
      loadAll();
    } else if (mode === "rect") {
      toast("在地图上按住拖拽，框选矩形范围");
    } else if (mode === "circle") {
      toast("在地图上从圆心按住拖拽，松手确定半径");
    } else {
      toast("逐点单击绘制多边形，双击闭合（Esc 取消）");
    }
  });

  resetFenceBtn.addEventListener("click", () => {
    pickModeSel.value = "none";
    canvas.style.cursor = "grab";
    clearFence();
    loadAll();
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
    appendFence(qs);
    /* 密度点聚合：视口 bbox 作为粗筛，筛区（多边形/圆形）再下推精确判定 */
    const pq = new URLSearchParams({
      dataset_version: state.dataset, bbox: viewBBox().join(","), zoom: String(Math.round(map.zoom)),
    });
    if (f.ship_types) pq.set("ship_types", f.ship_types.join(","));
    if (f.t0) pq.set("t0", f.t0);
    if (f.t1) pq.set("t1", f.t1);
    if (fencePoly) pq.set("polygon", JSON.stringify(fencePoly));
    if (fenceCircle) pq.set("circle", fenceCircle.map(v => +Number(v).toFixed(5)).join(","));
    try {
      const [traj, pts, ves] = await Promise.all([
        api(`/map/trajectories?${qs.toString()}`),
        api(`/map/points?${pq.toString()}`),
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
    /* 空间筛选模式下不触发单船高亮，避免拖拽/点击被误认为选船 */
    if (pickMode() !== "none") return;
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
