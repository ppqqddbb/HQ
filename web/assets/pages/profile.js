/* 船舶目标画像：船舶列表 + 单船速度直方 / 航向玫瑰 / 小时分布 / 雷达对比 */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox } from "./_ui.js";

let data = null;
let current = null;

export async function render(view) {
  const f = form([
    { key: "top_n", label: "画像船舶数", type: "number", value: 50, min: 5, max: 500, step: 5 },
    { key: "ship_types", label: "船型筛选", type: "text", value: "", width: 140, placeholder: "Cargo" },
  ]);
  const runBtn = el("button", { class: "btn primary" }, "重新计算画像");
  view.appendChild(el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "画像维度：速度统计、航向圆均值与集中度、活动范围、驻留行为、时间行为、轨迹复杂度。样本单位为单船，避免逐点伪重复。")));

  const status = statusBox();
  view.appendChild(status);

  const listCard = card("船舶列表", "点击行查看该船画像", el("div", { id: "pvList" }));
  const detailCard = card("船舶画像详情", "选择左侧船舶", el("div", { id: "pvDetail" }));
  view.appendChild(el("div", { class: "grid g32", style: { alignItems: "start" } },
    listCard, detailCard));

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    const filters = {};
    const st = (v.ship_types || "").split(",").map(s => s.trim()).filter(Boolean);
    if (st.length) filters.ship_types = st;
    runBtn.disabled = true;
    try {
      const out = await runTask("vessel_profile", { top_n: v.top_n }, filters,
        { onProgress: t => status.set(t) });
      status.set(out.task);
      toast("画像计算完成");
      await load(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  view.querySelector("#pvList").appendChild(loading("正在加载画像…"));
  try {
    const t = await latestSucceeded("vessel_profile", state.dataset);
    if (t) { status.set(t); await load(await getSummary(t.task_id)); }
    else {
      view.querySelector("#pvList").innerHTML = "";
      view.querySelector("#pvList").appendChild(card("暂无画像结果", null,
        el("div", { class: "note" }, "点击「重新计算画像」生成船舶属性画像（依赖已治理数据集，约 1–2 分钟）。")));
    }
  } catch (e) {
    view.querySelector("#pvList").innerHTML = "";
    view.querySelector("#pvList").appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }

  async function load(d) {
    data = d;
    const host = view.querySelector("#pvList");
    host.innerHTML = "";
    const vs = d.vessels || [];
    if (!vs.length) { host.appendChild(empty("无船舶数据")); return; }

    host.appendChild(el("div", { class: "note", style: { marginBottom: "8px" } },
      `共 ${FMT.num(d.n_vessels)} 艘船舶 · 总点数 ${FMT.big((d.totals || {}).points || 0)} · ` +
      `总航程 ${FMT.big((d.totals || {}).distance_km || 0)} km · 驻留 ${FMT.num((d.totals || {}).stay_count || 0)} 次`));

    host.appendChild(table([
      { label: "MMSI", key: "mmsi" },
      { label: "船名", render: r => r.name && r.name !== "未知" ? String(r.name).slice(0, 14) : "未知" },
      { label: "船型", render: r => r.ship_type || "—" },
      { label: "点数", num: true, render: r => FMT.big(r.points) },
      { label: "中位 SOG", num: true, render: r => fmtN(r.median_sog, 1) },
      { label: "航程 km", num: true, render: r => fmtN(r.distance_km, 0) },
      {
        label: "", render: r => el("button", { class: "btn sm", onclick: () => select(r.mmsi) }, "画像"),
      },
    ], vs, { tall: true }));

    if (vs.length) await select(vs[0].mmsi);
  }

  async function select(mmsi) {
    current = mmsi;
    const host = view.querySelector("#pvDetail");
    host.innerHTML = "";
    const v = (data.vessels || []).find(x => String(x.mmsi) === String(mmsi));
    if (!v) { host.appendChild(empty("未找到该船")); return; }

    /* 基础信息 */
    host.appendChild(el("div", { class: "grid g4", style: { marginBottom: "12px" } },
      kpi("轨迹点数", FMT.big(v.points), `轨迹段 ${FMT.num(v.segments)}`),
      kpi("中位 / 最大航速", `${fmtN(v.median_sog, 1)} / ${fmtN(v.max_sog, 1)} kn`,
        `均值 ${fmtN(v.mean_sog, 2)} · P95 ${fmtN(v.p95_sog, 1)}`),
      kpi("累计航程", fmtN(v.distance_km, 1) + " km",
        `活动范围 ${fmtN(v.activity_span_km, 1)} km`),
      kpi("数据质量", fmtN(v.avg_quality, 1), `驻留 ${fmtN(v.stay_count, 0)} 次`)));

    host.appendChild(el("div", { class: "grid g2", style: { marginBottom: "12px" } },
      el("dl", { class: "kv", style: { background: "var(--panel)", border: "1px solid var(--line)", borderRadius: "8px", padding: "10px 12px" } },
        el("dt", {}, "船型"), el("dd", {}, `${v.ship_type || "—"} / ${v.mobile_type || "—"}`),
        el("dt", {}, "IMO"), el("dd", {}, v.imo || "—"),
        el("dt", {}, "呼号"), el("dd", {}, v.callsign || "—"),
        el("dt", {}, "目的港"), el("dd", {}, v.destination || "—"),
        el("dt", {}, "船长 × 船宽"), el("dd", {}, `${fmtN(v.length, 1)} × ${fmtN(v.width, 1)} m`),
        el("dt", {}, "吃水"), el("dd", {}, fmtN(v.draught, 1) + " m"),
        el("dt", {}, "活跃时段"), el("dd", {}, `${FMT.ts(v.t0)} → ${FMT.ts(v.t1)}`),
        el("dt", {}, "活跃天数"), el("dd", {}, fmtN(v.active_days, 0) + " 天")),
      el("div", {},
        el("div", { style: { fontSize: "11.5px", color: "var(--txt3)", marginBottom: "6px" } },
          `主航向 ${fmtN(v.main_cog, 0)}° · 航向集中度 ${fmtN(v.cog_concentration, 2)}（1 = 航向高度一致）`),
        el("div", { id: "pvRadar", class: "chart", style: { height: "210px" } }))));

    /* 图表 */
    const spd = el("div", { class: "chart" });
    const cog = el("div", { class: "chart" });
    const hr = el("div", { class: "chart" });
    const cmp = el("div", { class: "chart" });
    host.appendChild(el("div", { class: "grid g2", style: { marginBottom: "12px" } },
      card("航速分布", "逐 1 kn 分桶", spd),
      card("航向玫瑰图", "逐 10° 分桶", cog)));
    host.appendChild(el("div", { class: "grid g2" },
      card("逐小时上报量", "时间行为", hr),
      card("多船对比", "航速 / 活动范围 / 复杂度", cmp)));

    /* 速度直方 */
    const sh = (data.speed_hist || []).filter(r => String(r.mmsi) === String(v.mmsi));
    if (sh.length) {
      const map = {};
      for (const r of sh) map[r.k] = (map[r.k] || 0) + r.n;
      const ks = Object.keys(map).map(Number).sort((a, b) => a - b);
      chartBar(spd, ks.map(k => k + " kn"), ks.map(k => map[k]), { yName: "点数", labelFmt: x => FMT.big(x) });
    } else spd.appendChild(empty("无速度分布数据"));

    /* 航向玫瑰 */
    const ch = (data.cog_hist || []).filter(r => String(r.mmsi) === String(v.mmsi));
    if (ch.length) {
      const map = {};
      for (const r of ch) map[r.k] = (map[r.k] || 0) + r.n;
      const ks = Object.keys(map).map(Number).sort((a, b) => a - b);
      chartRose(cog, ks.map(k => String(k).padStart(3, "0")), ks.map(k => map[k]));
    } else cog.appendChild(empty("无航向分布数据"));

    /* 小时分布 */
    const pr = (data.profiles || []).find(p => String(p.mmsi) === String(v.mmsi));
    if (pr && (pr.hour_profile || []).length) {
      chartBar(hr, pr.hour_profile.map(p => String(p.h).padStart(2, "0")),
        pr.hour_profile.map(p => p.n), { yName: "点数", labelFmt: x => FMT.big(x) });
    } else {
      // 该船不在 TOP12 明细范围内时给出提示
      hr.appendChild(el("div", { class: "note" },
        "逐小时明细仅对前 12 艘船计算（为控制结果体积）。选择列表靠前的船舶可查看曲线。"));
    }

    /* 多船对比（TOP 8 的归一化雷达） */
    const top = (data.vessels || []).slice(0, 8);
    const maxOf = k => Math.max(1e-9, ...top.map(r => Math.abs(Number(r[k]) || 0)));
    const indicators = [
      { name: "航速", max: 1 },
      { name: "航程", max: 1 },
      { name: "活动范围", max: 1 },
      { name: "航段数", max: 1 },
      { name: "驻留次数", max: 1 },
    ];
    const series = top.map(r => ({
      name: String(r.name && r.name !== "未知" ? r.name.slice(0, 10) : r.mmsi),
      value: [
        (Number(r.median_sog) || 0) / (maxOf("median_sog") || 1),
        (Number(r.distance_km) || 0) / (maxOf("distance_km") || 1),
        (Number(r.activity_span_km) || 0) / (maxOf("activity_span_km") || 1),
        (Number(r.segments) || 0) / (maxOf("segments") || 1),
        (Number(r.stay_count) || 0) / (maxOf("stay_count") || 1),
      ].map(x => Number((x * 100).toFixed(1))),
    }));
    if (top.length > 1) chartRadar(cmp, indicators, series);
    else cmp.appendChild(empty("仅 1 艘船，无法对比"));

    /* 单船雷达 */
    const rEl = host.querySelector("#pvRadar");
    if (rEl) {
      const one = [{
        name: "本船",
        value: [
          Math.min(100, (Number(v.median_sog) || 0) / 20 * 100),
          Math.min(100, (Number(v.distance_km) || 0) / (maxOf("distance_km") || 1) * 100),
          Math.min(100, (Number(v.activity_span_km) || 0) / (maxOf("activity_span_km") || 1) * 100),
          Math.min(100, (Number(v.segments) || 0) / (maxOf("segments") || 1) * 100),
          Math.min(100, (Number(v.stay_count) || 0) / (maxOf("stay_count") || 1) * 100),
        ].map(x => Number(x.toFixed(1))),
      }];
      chartRadar(rEl, indicators, one, {});
    }
  }
}

function fmtN(v, d = 1) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(d);
}
