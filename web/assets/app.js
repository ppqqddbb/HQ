/* 应用外壳：路由、数据版本选择、侧栏、全局状态 */
import { state } from "./state.js";
import * as overview from "./pages/overview.js";
import * as datacenter from "./pages/datacenter.js";
import * as mapview from "./pages/mapview.js";
import * as hotspot from "./pages/hotspot.js";
import * as profile from "./pages/profile.js";
import * as correlate from "./pages/correlate.js";
import * as formation from "./pages/formation.js";
import * as routes from "./pages/routes.js";
import * as predict from "./pages/predict.js";
import * as admin from "./pages/admin.js";

const ROUTES = [
  { group: "分析" },
  { id: "overview", name: "总览工作台", icon: "◉", mod: overview, title: "总览工作台", sub: "数据概况 · 质量 · 任务状态" },
  { group: "数据" },
  { id: "datacenter", name: "文件与批次", icon: "▤", mod: datacenter, title: "数据中心", sub: "导入 · 字段映射 · 质量报告" },
  { id: "map", name: "AIS 轨迹地图", icon: "⌖", mod: mapview, title: "AIS 轨迹地图", sub: "轨迹回放 · 船舶筛选 · 驻留点" },
  { group: "挖掘" },
  { id: "hotspot", name: "热点区域分析", icon: "◈", mod: hotspot, title: "热点区域分析", sub: "HDBSCAN 密度聚类" },
  { id: "profile", name: "船舶目标画像", icon: "◐", mod: profile, title: "船舶目标画像", sub: "速度 · 航向 · 活动范围" },
  { id: "correlate", name: "属性关联分析", icon: "⬡", mod: correlate, title: "属性关联分析", sub: "相关矩阵 · BH 校正 · 滞后" },
  { id: "formation", name: "船舶编群分析", icon: "⧉", mod: formation, title: "船舶编群分析", sub: "DTW · 层次聚类" },
  { id: "routes", name: "频繁轨迹挖掘", icon: "⇢", mod: routes, title: "频繁轨迹挖掘", sub: "网格 · 转移 · 频繁航线" },
  { id: "predict", name: "航迹预测", icon: "◈", mod: predict, title: "航迹预测与回测", sub: "CV · Kalman · GRU" },
  { group: "系统" },
  { id: "admin", name: "系统管理", icon: "⚙", mod: admin, title: "系统管理", sub: "字典 · 配置 · 审计" },
];

function buildNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = "";
  for (const r of ROUTES) {
    if (r.group) { nav.appendChild(el("div", { class: "nav-group" }, r.group)); continue; }
    const a = el("a", { href: `#/${r.id}`, dataset: { id: r.id } },
      el("i", {}, r.icon), el("span", {}, r.name));
    nav.appendChild(a);
  }
}

async function loadDatasets() {
  const sel = document.getElementById("datasetSelect");
  try {
    const prof = await api("/map/overview");
    state.overview = prof;
    const list = prof.datasets || [];
    state.dataset = state.dataset || prof.dataset_version;
    sel.innerHTML = "";
    if (!list.length) {
      sel.appendChild(el("option", { value: "" }, "（无数据集）"));
      state.dataset = null;
    } else {
      for (const d of list) {
        sel.appendChild(el("option", { value: d.dataset_version },
          `${d.dataset_version}`));
      }
      if (!list.find(d => d.dataset_version === state.dataset)) state.dataset = list[0].dataset_version;
      sel.value = state.dataset;
      const cur = list.find(d => d.dataset_version === state.dataset);
      state.overview = { ...prof, ...(cur ? { n_points: cur.row_count, n_mmsi: cur.mmsi_count } : {}) };
    }
    const h = document.getElementById("healthPill");
    h.textContent = "● 服务正常";
    h.className = "pill ok";
  } catch (e) {
    document.getElementById("healthPill").textContent = "● 服务异常";
    document.getElementById("healthPill").className = "pill bad";
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "" }, "（连接失败）"));
  }
  renderMiniStats();
}

function renderMiniStats() {
  const box = document.getElementById("miniStats");
  const o = state.overview || {};
  box.innerHTML = "";
  const add = (k, v) => box.appendChild(el("div", {}, k, el("b", {}, v)));
  add("点位", o.n_points ? FMT.big(o.n_points) : "—");
  add("船舶", o.n_mmsi ? FMT.num(o.n_mmsi) : "—");
  add("航段", o.n_trajectories ? FMT.big(o.n_trajectories) : "—");
  add("质量分", o.avg_quality ? o.avg_quality.toFixed(1) : "—");
}

let current = null;

async function route() {
  const hash = (location.hash || "#/overview").replace(/^#\/?/, "");
  const item = ROUTES.find(r => r.id === hash) || ROUTES.find(r => r.id === "overview");
  document.querySelectorAll("#nav a").forEach(a => {
    a.classList.toggle("active", a.dataset.id === item.id);
  });
  document.getElementById("crumb").innerHTML =
    `${item.title}<small>${item.sub || ""}</small>`;

  disposeCharts();
  if (current && current.destroy) { try { current.destroy(); } catch (e) { } }
  const view = document.getElementById("view");
  view.innerHTML = "";
  current = item.mod;
  try {
    await item.mod.render(view, item);
  } catch (e) {
    console.error(e);
    view.appendChild(card("渲染失败", null,
      el("div", { class: "note" }, String(e.message || e)),
      el("div", { style: { marginTop: "10px" } },
        el("button", { class: "btn", onclick: () => route() }, "重试"))));
  }
}

function boot() {
  buildNav();
  document.getElementById("datasetSelect").addEventListener("change", async e => {
    state.dataset = e.target.value;
    await loadDatasets();
    route();
  });
  document.getElementById("btnRefresh").addEventListener("click", async () => {
    await loadDatasets();
    route();
    toast("已刷新");
  });
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", debounce(() => {
    // ECharts 由 ResizeObserver 处理，地图自行监听
  }, 200));
  loadDatasets().then(route);
}

boot();
