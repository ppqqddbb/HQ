/* 通用工具：请求、格式化、DOM 构建、轻量地图（Canvas + 瓦片） */
const API = "/api/v1";

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body;
  try { body = await r.json(); } catch { body = { ok: false, error: { message: "响应解析失败" } }; }
  if (!r.ok || body.ok === false) {
    const msg = (body && body.error && (body.error.message || body.error.code)) || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return body.data;
}

const FMT = {
  num(v, d = 0) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  big(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + " 万";
    return n.toLocaleString("zh-CN");
  },
  pct(v, d = 1) {
    if (v === null || v === undefined) return "—";
    return (Number(v) * 100).toFixed(d) + "%";
  },
  m(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (Math.abs(n) >= 1000) return (n / 1000).toFixed(2) + " km";
    return n.toFixed(0) + " m";
  },
  bytes(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    if (Math.abs(n) >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
    if (Math.abs(n) >= 1024 ** 2) return (n / 1024 ** 2).toFixed(2) + " MB";
    if (Math.abs(n) >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n.toFixed(0) + " B";
  },
  ts(v) {
    if (!v) return "—";
    return String(v).replace("T", " ").slice(0, 19);
  },
  dt(sec) {
    if (sec === null || sec === undefined) return "—";
    const s = Number(sec);
    if (s < 60) return s.toFixed(0) + " 秒";
    if (s < 3600) return (s / 60).toFixed(1) + " 分钟";
    if (s < 86400) return (s / 3600).toFixed(1) + " 小时";
    return (s / 86400).toFixed(1) + " 天";
  },
};

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return e;
}

function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), isErr ? 5200 : 2600);
}

function card(title, sub, ...body) {
  return el("div", { class: "card" },
    el("h3", {}, el("span", {}, title), sub ? el("span", { class: "sub" }, sub) : null),
    ...body);
}

function kpi(label, value, sub) {
  return el("div", { class: "card kpi" },
    el("div", { class: "k-label" }, label),
    el("div", { class: "k-value" }, value),
    sub ? el("div", { class: "k-sub" }, sub) : null);
}

function table(cols, rows, opts = {}) {
  const thead = el("thead", {}, el("tr", {}, ...cols.map(c =>
    el("th", { class: c.num ? "num" : "" }, c.label))));
  const tbody = el("tbody", {}, ...rows.map(r => {
    const tr = el("tr", {}, ...cols.map(c => {
      const v = c.render ? c.render(r) : r[c.key];
      if (v instanceof Node) return el("td", {}, v);
      return el("td", { class: c.num ? "num" : "" }, v === null || v === undefined ? "—" : String(v));
    }));
    // 支持行高亮（例如当前选中行）与整行点击
    if (opts.highlight && opts.highlight(r)) tr.classList.add("sel");
    if (opts.onRowClick) {
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => opts.onRowClick(r, tr));
    }
    return tr;
  }));
  const t = el("table", { class: "dt" }, thead, tbody);
  const wrap = el("div", { class: "scroll " + (opts.tall ? "tall-scroll" : "") }, t);
  if (opts.maxHeight) wrap.style.maxHeight = typeof opts.maxHeight === "number" ? opts.maxHeight + "px" : opts.maxHeight;
  return wrap;
}

function bars(items, opts = {}) {
  const max = Math.max(1, ...items.map(i => i.value));
  const color = opts.color || "linear-gradient(90deg,#22d3ee,#38bdf8)";
  return el("div", {}, ...items.map(i => el("div", { class: "bar-row" },
    el("div", { class: "lab", title: i.label }, i.label),
    el("div", { class: "bar-track" }, el("i", { style: { width: (i.value / max * 100).toFixed(1) + "%", background: i.color || color } })),
    el("div", { class: "val" }, opts.fmt ? opts.fmt(i.value) : FMT.num(i.value)))));
}

function loading(text = "加载中…") {
  return el("div", { class: "loading" }, el("div", { class: "spin" }), text);
}

function empty(text) { return el("div", { class: "empty" }, text); }

function debounce(fn, ms = 300) {
  let h;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

/* ============================ 轻量地图 ============================
 * 纯 Canvas 实现的底图 + 图层绘制，支持：
 * - 经纬度 → 屏幕坐标（Web 墨卡托）
 * - 平移 / 滚轮缩放
 * - 轨迹折线、散点（带透明度）、热力网格、多边形、标签
 * 不依赖外部 GIS 服务，断网亦可使用（无底图纹理时仅绘制坐标网格）。
 */
class TileMap {
  constructor(container, opts = {}) {
    this.el = container;
    this.canvas = document.createElement("canvas");
    this.canvas.style.width = "100%";
    this.canvas.style.height = "100%";
    this.canvas.style.display = "block";
    this.canvas.style.cursor = "grab";
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this.center = opts.center || [10.5, 56.0];
    this.zoom = opts.zoom || 6;
    this.layers = [];
    this.onClick = opts.onClick || null;
    this.onHover = opts.onHover || null;
    this.tileCache = new Map();
    this.tileOk = true;
    this._dragging = false;
    this._hoverPt = null;
    this._pendingFit = opts.fitBBox || null;
    this._fitted = false;
    this._bind();
    // 容器尺寸可能在构造时尚未完成布局（width/height 为 0），
    // 用 ResizeObserver 在拿到真实尺寸后再首次适配视野。
    if (typeof ResizeObserver !== "undefined") {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.el);
    }
    this.resize();
  }

  resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = this.el.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    const sizeChanged = w !== this.w || h !== this.h;
    const bmpW = Math.max(1, Math.floor(w * dpr)), bmpH = Math.max(1, Math.floor(h * dpr));
    this.w = w; this.h = h;
    // 注意：即使尺寸相同，给 canvas.width 赋值也会清空位图，
    // 所以只在尺寸真正变化时改位图尺寸，并立即重绘。
    if (this.canvas.width !== bmpW || this.canvas.height !== bmpH) {
      this.canvas.width = bmpW;
      this.canvas.height = bmpH;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    if (!this._fitted && this._pendingFit && w > 20 && h > 20) {
      this._fitted = true;
      this.fitBBox(this._pendingFit);
      return;
    }
    // 尺寸未变时也需要重绘（位图可能刚被重置）
    this.draw();
  }

  destroy() {
    if (this._ro) { try { this._ro.disconnect(); } catch (e) { } }
  }

  _bind() {
    const c = this.canvas;
    c.addEventListener("mousedown", e => { this._dragging = true; this._p0 = [e.offsetX, e.offsetY]; c.style.cursor = "grabbing"; });
    window.addEventListener("mouseup", () => { this._dragging = false; c.style.cursor = "grab"; });
    c.addEventListener("mousemove", e => {
      if (this._dragging) {
        const dx = e.offsetX - this._p0[0], dy = e.offsetY - this._p0[1];
        this._p0 = [e.offsetX, e.offsetY];
        this.center = this.unproject(this.w / 2 - dx, this.h / 2 - dy);
        this.draw();
      } else if (this.onHover) {
        this.onHover(this.unproject(e.offsetX, e.offsetY), e);
      }
    });
    c.addEventListener("wheel", e => {
      e.preventDefault();
      const before = this.unproject(e.offsetX, e.offsetY);
      this.zoom = Math.max(0.5, Math.min(14, this.zoom + (e.deltaY < 0 ? 0.35 : -0.35)));
      const after = this.unproject(e.offsetX, e.offsetY);
      this.center = [this.center[0] + (before[0] - after[0]), this.center[1] + (before[1] - after[1])];
      this.draw();
    }, { passive: false });
    let down = null;
    c.addEventListener("mousedown", e => { down = [e.offsetX, e.offsetY]; });
    c.addEventListener("click", e => {
      if (!this.onClick) return;
      if (down && Math.hypot(e.offsetX - down[0], e.offsetY - down[1]) > 4) return;
      this.onClick(this.unproject(e.offsetX, e.offsetY), e);
    });
    window.addEventListener("resize", () => this.resize());
  }

  project(lon, lat) {
    const s = 256 * 2 ** this.zoom;
    const x = (lon + 180) / 360 * s;
    const sy = Math.sin(lat * Math.PI / 180);
    const y = (0.5 - Math.log((1 + sy) / (1 - sy)) / (4 * Math.PI)) * s;
    return [x, y];
  }
  unproject(px, py) {
    const s = 256 * 2 ** this.zoom;
    const cx = (this.center[0] + 180) / 360 * s;
    const sy = Math.sin(this.center[1] * Math.PI / 180);
    const cy = (0.5 - Math.log((1 + sy) / (1 - sy)) / (4 * Math.PI)) * s;
    const wx = cx + (px - this.w / 2);
    const wy = cy + (py - this.h / 2);
    const lon = wx / s * 360 - 180;
    const n = Math.PI - (2 * Math.PI * wy) / s;
    const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    return [lon, lat];
  }
  toScreen(lon, lat) {
    const [x, y] = this.project(lon, lat);
    const s = 256 * 2 ** this.zoom;
    const sx = Math.sin(this.center[1] * Math.PI / 180);
    const cx = (this.center[0] + 180) / 360 * s;
    const cy = (0.5 - Math.log((1 + sx) / (1 - sx)) / (4 * Math.PI)) * s;
    return [this.w / 2 + (x - cx), this.h / 2 + (y - cy)];
  }

  fitBBox(bbox, pad = 30) {
    if (!bbox || bbox.length !== 4) return;
    if (!(this.w > 20 && this.h > 20)) { this._pendingFit = bbox; this._fitted = false; return; }
    const [x0, y0, x1, y1] = bbox;
    this.center = [(x0 + x1) / 2, (y0 + y1) / 2];
    // 二分查找合适缩放
    let lo = 0.3, hi = 12;
    for (let i = 0; i < 24; i++) {
      this.zoom = (lo + hi) / 2;
      const a = this.toScreen(x0, y1), b = this.toScreen(x1, y0);
      const w = Math.abs(b[0] - a[0]), h = Math.abs(b[1] - a[1]);
      if (w > this.w - pad * 2 || h > this.h - pad * 2) hi = this.zoom; else lo = this.zoom;
    }
    this.zoom = lo;
    this._fitted = true;
    this.draw();
  }

  setLayers(layers) { this.layers = layers; this.draw(); }
  setCenter(lon, lat, zoom) {
    this.center = [lon, lat];
    if (zoom !== undefined) this.zoom = zoom;
    this.draw();
  }

  drawGrid() {
    const ctx = this.ctx;
    const [lon0, lat0] = this.unproject(0, 0);
    const [lon1, lat1] = this.unproject(this.w, this.h);
    const span = Math.abs(lon1 - lon0);
    let step = 10;
    if (span < 1) step = 0.1; else if (span < 4) step = 0.5; else if (span < 12) step = 2; else if (span < 40) step = 5;
    ctx.strokeStyle = "rgba(56,189,248,.07)";
    ctx.fillStyle = "rgba(157,176,201,.4)";
    ctx.font = "10px 'Segoe UI',sans-serif";
    ctx.lineWidth = 1;
    for (let lon = Math.floor(lon0 / step) * step; lon <= lon1; lon += step) {
      const [x] = this.toScreen(lon, 0);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, this.h); ctx.stroke();
      if (Math.abs(lon) > 1) ctx.fillText(lon.toFixed(step < 1 ? 1 : 0) + "°", x + 3, this.h - 6);
    }
    for (let lat = Math.floor(lat1 / step) * step; lat <= lat0; lat += step) {
      const [, y] = this.toScreen(0, lat);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(this.w, y); ctx.stroke();
      ctx.fillText(lat.toFixed(step < 1 ? 1 : 0) + "°", 4, y - 3);
    }
  }

  draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);
    const g = ctx.createLinearGradient(0, 0, 0, this.h);
    g.addColorStop(0, "#0a1526"); g.addColorStop(1, "#0c1b2e");
    ctx.fillStyle = g; ctx.fillRect(0, 0, this.w, this.h);
    this.drawGrid();
    for (const L of this.layers) {
      try { L.paint(this, this.ctx); } catch (e) { console.warn("layer paint failed", e); }
    }
  }
}

/* 图层工厂 */
const MapLayers = {
  gridCells(cells, opts = {}) {
    const { gridRows, gridCols, metric = "support_ratio", colorScale } = opts;
    return {
      paint(map, ctx) {
        const vals = cells.map(c => Number(c[metric] || 0));
        const max = Math.max(1e-9, ...vals);
        cells.forEach((c, i) => {
          const v = Number(c[metric] || 0);
          const t = v / max;
          const [sx, sy] = map.toScreen(c.minlon, c.maxlat);
          const [ex, ey] = map.toScreen(c.maxlon, c.minlat);
          const w = ex - sx, h = ey - sy;
          if (w < 1 || h < 1) return;
          const col = colorScale ? colorScale(t) : `rgba(34,211,238,${0.06 + t * 0.55})`;
          ctx.fillStyle = col;
          ctx.fillRect(sx, sy, w, h);
          ctx.strokeStyle = "rgba(56,189,248,.22)";
          ctx.strokeRect(sx, sy, w, h);
          if (w > 40 && h > 24) {
            ctx.fillStyle = "rgba(232,238,247,.86)";
            ctx.font = "10.5px 'Segoe UI',sans-serif";
            ctx.fillText(`${c.grid_id}`, sx + 4, sy + 12);
            if (w > 60) ctx.fillText(v.toFixed(2), sx + 4, sy + 24);
          }
        });
      },
    };
  },

  edges(edges, cellsById, opts = {}) {
    return {
      paint(map, ctx) {
        const max = Math.max(1, ...edges.map(e => e.distinct_voyage_count || 1));
        ctx.lineCap = "round";
        for (const e of edges) {
          const a = cellsById[e.from_grid], b = cellsById[e.to_grid];
          if (!a || !b) continue;
          const [x1, y1] = map.toScreen(a.cx, a.cy);
          const [x2, y2] = map.toScreen(b.cx, b.cy);
          const t = (e.distinct_voyage_count || 1) / max;
          // 略微偏移，避免正反方向完全重叠
          const dx = x2 - x1, dy = y2 - y1;
          const len = Math.hypot(dx, dy) || 1;
          const ox = -dy / len * 4, oy = dx / len * 4;
          ctx.strokeStyle = `rgba(167,139,250,${0.18 + t * 0.6})`;
          ctx.lineWidth = 1 + t * 4;
          ctx.beginPath();
          ctx.moveTo(x1 + ox, y1 + oy);
          ctx.lineTo(x2 + ox * 0.6, y2 + oy * 0.6);
          ctx.stroke();
          // 箭头
          const ax = x2 + ox * 0.6, ay = y2 + oy * 0.6;
          const ang = Math.atan2(dy, dx);
          ctx.fillStyle = `rgba(167,139,250,${0.35 + t * 0.6})`;
          ctx.beginPath();
          ctx.moveTo(ax, ay);
          ctx.lineTo(ax - 8 * Math.cos(ang - 0.4), ay - 8 * Math.sin(ang - 0.4));
          ctx.lineTo(ax - 8 * Math.cos(ang + 0.4), ay - 8 * Math.sin(ang + 0.4));
          ctx.closePath(); ctx.fill();
        }
      },
    };
  },

  lines(features, opts = {}) {
    const colorOf = opts.colorOf || (() => "rgba(56,189,248,.55)");
    const width = opts.width || 1.2;
    return {
      paint(map, ctx) {
        ctx.lineCap = "round"; ctx.lineJoin = "round";
        for (const f of features) {
          const coords = f.geometry.coordinates;
          if (!coords || coords.length < 2) continue;
          ctx.strokeStyle = colorOf(f);
          ctx.lineWidth = f._w || width;
          ctx.beginPath();
          let started = false;
          for (const [lon, lat] of coords) {
            const [x, y] = map.toScreen(lon, lat);
            if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }
      },
    };
  },

  route(route, opts = {}) {
    return {
      paint(map, ctx) {
        const pts = route.route_geometry || [];
        if (pts.length < 2) return;
        const scr = pts.map(p => map.toScreen(p[0], p[1]));
        ctx.lineWidth = 3.2; ctx.lineCap = "round"; ctx.lineJoin = "round";
        ctx.strokeStyle = "rgba(52,211,153,.92)";
        ctx.beginPath(); scr.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke();
        ctx.strokeStyle = "rgba(52,211,153,.22)"; ctx.lineWidth = 9;
        ctx.beginPath(); scr.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke();
        scr.forEach(([x, y], i) => {
          ctx.fillStyle = i === 0 ? "#22d3ee" : (i === scr.length - 1 ? "#f472b6" : "#34d399");
          ctx.beginPath(); ctx.arc(x, y, i === 0 || i === scr.length - 1 ? 6 : 4, 0, 7); ctx.fill();
          ctx.fillStyle = "rgba(232,238,247,.9)";
          ctx.font = "10px 'Segoe UI',sans-serif";
          ctx.fillText(String(route.grid_sequence ? route.grid_sequence[i] : i), x + 7, y - 5);
        });
        // 方向箭头
        for (let i = 0; i < scr.length - 1; i++) {
          const [x1, y1] = scr[i], [x2, y2] = scr[i + 1];
          const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
          const ang = Math.atan2(y2 - y1, x2 - x1);
          ctx.fillStyle = "rgba(52,211,153,.9)";
          ctx.beginPath();
          ctx.moveTo(mx + 6 * Math.cos(ang), my + 6 * Math.sin(ang));
          ctx.lineTo(mx - 5 * Math.cos(ang - 0.5), my - 5 * Math.sin(ang - 0.5));
          ctx.lineTo(mx - 5 * Math.cos(ang + 0.5), my - 5 * Math.sin(ang + 0.5));
          ctx.closePath(); ctx.fill();
        }
      },
    };
  },

  points(pts, opts = {}) {
    const r = opts.radius || 4;
    const colorOf = opts.colorOf || (() => "rgba(56,189,248,.8)");
    return {
      paint(map, ctx) {
        for (const p of pts) {
          const [x, y] = map.toScreen(p.lon, p.lat);
          if (x < -20 || y < -20 || x > map.w + 20 || y > map.h + 20) continue;
          ctx.fillStyle = colorOf(p);
          ctx.beginPath(); ctx.arc(x, y, p._r || r, 0, 7); ctx.fill();
        }
      },
    };
  },

  polygons(features, opts = {}) {
    const colorOf = opts.colorOf || (() => "rgba(251,191,36,.16)");
    const stroke = opts.stroke || "rgba(251,191,36,.65)";
    return {
      paint(map, ctx) {
        for (const f of features) {
          const ring = f.geometry && f.geometry.type === "Polygon" ? f.geometry.coordinates[0] : null;
          if (!ring || ring.length < 3) continue;
          ctx.beginPath();
          ring.forEach(([lon, lat], i) => {
            const [x, y] = map.toScreen(lon, lat);
            i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
          });
          ctx.closePath();
          ctx.fillStyle = colorOf(f);
          ctx.fill();
          ctx.strokeStyle = stroke; ctx.lineWidth = 1.4; ctx.stroke();
        }
      },
    };
  },

  labels(items) {
    return {
      paint(map, ctx) {
        ctx.font = "bold 11px 'Segoe UI',sans-serif";
        for (const it of items) {
          const [x, y] = map.toScreen(it.lon, it.lat);
          const t = String(it.text);
          const w = ctx.measureText(t).width + 10;
          ctx.fillStyle = "rgba(9,16,27,.8)";
          ctx.fillRect(x - w / 2, y - 20, w, 15);
          ctx.fillStyle = it.color || "#e8eef7";
          ctx.fillText(t, x - w / 2 + 5, y - 9);
        }
      },
    };
  },

  heat(points, opts = {}) {
    const radius = opts.radius || 26;
    const max = Math.max(1, ...points.map(p => p.n || p.v || 1));
    return {
      paint(map, ctx) {
        for (const p of points) {
          const v = (p.n || p.v || 1) / max;
          const [x, y] = map.toScreen(p.lon, p.lat);
          if (x < -radius || y < -radius || x > map.w + radius || y > map.h + radius) continue;
          const g = ctx.createRadialGradient(x, y, 0, x, y, radius * (0.5 + v * 0.9));
          const a = 0.06 + v * 0.5;
          g.addColorStop(0, `rgba(34,211,238,${a})`);
          g.addColorStop(0.55, `rgba(56,189,248,${a * 0.45})`);
          g.addColorStop(1, "rgba(56,189,248,0)");
          ctx.fillStyle = g;
          ctx.beginPath(); ctx.arc(x, y, radius * (0.5 + v * 0.9), 0, 7); ctx.fill();
        }
      },
    };
  },
};

window.API = API; window.api = api; window.FMT = FMT; window.el = el;
window.toast = toast; window.card = card; window.kpi = kpi; window.table = table;
window.bars = bars; window.loading = loading; window.empty = empty; window.debounce = debounce;
window.TileMap = TileMap; window.MapLayers = MapLayers;
