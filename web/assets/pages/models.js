/* 模型监控：模型卡、输入分布漂移（PSI）、回填误差趋势（技术方案 10.10） */
import { state } from "../state.js";

const LEVEL_CN = {
  stable: "稳定", warn: "需关注", alert: "显著漂移", insufficient: "样本不足",
};
const LEVEL_TAG = { stable: "ok", warn: "warn", alert: "bad", insufficient: "" };
const ACTION_CN = {
  ok: "可继续在线", watch: "加密监控", retrain: "建议重训/降级", insufficient: "样本不足",
};
const ACTION_TAG = { ok: "ok", watch: "warn", retrain: "bad", insufficient: "" };

let last = null;
/* 模块级节点缓存：render 时填充，供模块级子函数（renderDrift/renderTrend/renderActions）访问 */
const ui = {};

export async function render(view) {
  const head = el("div", {});
  const cardBox = el("div", {});
  const driftBox = el("div", {});
  const trendBox = el("div", {});
  const actionBox = el("div", {});

  view.appendChild(head);
  view.appendChild(cardBox);
  view.appendChild(el("div", { class: "grid g2", style: { marginBottom: "14px" } },
    card("输入分布漂移（PSI）", "以训练期为参照，对比当前数据集；>0.25 视为显著漂移", driftBox),
    card("回填误差趋势", "上线后实际回填误差相对首评的变化", trendBox)));
  view.appendChild(card("模型卡与操作", "训练范围 · 指标 · 特征 · 失效场景 · 监控登记", actionBox));
  ui.card = cardBox; ui.drift = driftBox; ui.trend = trendBox; ui.action = actionBox;

  head.appendChild(loading("读取模型卡…"));
  let models = [];
  try {
    models = await api("/models");
  } catch (e) {
    head.innerHTML = "";
    head.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
    return;
  }
  head.innerHTML = "";

  if (!models.length) {
    view.insertBefore(empty("暂无模型卡。运行一次「航迹预测」任务后会自动生成。"), head);
    head.appendChild(newTaskHint());
    return;
  }

  const sel = el("select", { style: { minWidth: "260px" } },
    ...models.map(m => el("option", { value: m.dir },
      `${m.model_id}  ·  ${FMT.ts(m.created_at)}`)));
  const driftBtn = el("button", { class: "btn" }, "运行漂移检测");
  const pointBtn = el("button", { class: "btn ghost" }, "登记监控点");
  head.appendChild(el("div", { class: "toolbar" },
    el("div", { class: "field" }, el("label", {}, "模型"), sel),
    driftBtn, pointBtn,
    el("span", { class: "tag", style: { marginLeft: "4px" } },
      `共 ${models.length} 个模型卡`)));

  sel.addEventListener("change", () => loadDetail(sel.value));
  driftBtn.addEventListener("click", async () => {
    driftBtn.disabled = true;
    driftBtn.textContent = "检测中…";
    try {
      const r = await api(`/models/${encodeURIComponent(sel.value)}/drift`, {
        method: "POST",
        body: JSON.stringify({ dataset_version: state.dataset, sample: 20000 }),
      });
      toast(`漂移检测完成：${ACTION_CN[r.verdict.action] || r.verdict.action}`);
      renderDrift(r.features, r.verdict);
      await loadDetail(sel.value, true);
    } catch (e) {
      toast(e.message, true);
    } finally {
      driftBtn.disabled = false;
      driftBtn.textContent = "运行漂移检测";
    }
  });
  pointBtn.addEventListener("click", () => openPointDialog(sel.value, () => loadDetail(sel.value, true)));

  await loadDetail(sel.value);

  async function loadDetail(dir, quiet = false) {
    if (!quiet) {
      ui.card.innerHTML = "";
      ui.card.appendChild(loading("读取模型卡…"));
    }
    let d;
    try {
      d = await api(`/models/${encodeURIComponent(dir)}`);
    } catch (e) {
      ui.card.innerHTML = "";
      ui.card.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
      return;
    }
    last = d;
    ui.card.innerHTML = "";
    ui.card.appendChild(renderCard(d.card));
    renderDrift(d.latest_drift || [], d.latest_drift_verdict || null);
    renderTrend(d.error_trend);
    renderActions(d);
  }
}

function newTaskHint() {
  return el("div", { class: "note" },
    "模型卡在「航迹预测」任务成功后自动生成，包含训练数据范围、评估指标、特征清单、依赖版本与失效场景；" +
    "可在预测页运行一次任务后回到本页查看。");
}

/* ---------------------------------------------------------------- 模型卡 */

function renderCard(c) {
  const td = c.training_data || {};
  const ev = c.evaluation || {};
  const gru = ev.gru || {};
  const base = ev.baseline || {};
  const v = ev.verdict || {};
  const cov = td.coverage || {};

  const box = el("div", {});
  box.appendChild(el("div", { class: "grid g5", style: { marginBottom: "14px" } },
    kpi("模型", c.model_id || "—", `任务 ${c.task_id || "—"}`),
    kpi("GRU 截尾 ADE", gru.trimmed_ADE_m !== undefined ? FMT.m(gru.trimmed_ADE_m) : "—",
      `基线 ${base.model || "—"} ${base.trimmed_ADE_m !== undefined ? FMT.m(base.trimmed_ADE_m) : "—"}`),
    kpi("改善幅度", v.improvement_pct !== undefined ? Number(v.improvement_pct).toFixed(1) + "%" : "—",
      v.can_go_online ? "达到上线门槛" : "未达上线门槛"),
    kpi("样本量", FMT.num(td.n_test), `训练 ${FMT.num(td.n_train)} / 窗口 ${FMT.num(td.n_windows)}`),
    kpi("负责人", c.owner || "—", `生成于 ${FMT.ts(c.created_at)}`)));

  const kv = (k, val) => el("div", {}, el("span", { style: { color: "var(--txt3)" } }, k + " "), val);
  const range = td.time_range || [];
  box.appendChild(el("div", { class: "grid g2" },
    card("训练数据边界", "模型卡记录的适用时间与区域", el("div", { class: "kv" },
      el("dt", {}, "数据集"), el("dd", {}, td.dataset_version || "—"),
      el("dt", {}, "时间范围"), el("dd", {}, range.length === 2 ? `${FMT.ts(range[0])} → ${FMT.ts(range[1])}` : "—"),
      el("dt", {}, "区域范围"), el("dd", {}, (td.region || []).length === 4
        ? (td.region || []).map(x => Number(x).toFixed(2)).join(", ") : "—"),
      el("dt", {}, "轨迹数"), el("dd", {}, FMT.num(td.n_trajectories)),
      el("dt", {}, "步长 / 输入 / 预测"), el("dd", {},
        `${td.step_minutes ?? "—"} / ${td.input_minutes ?? "—"} / ${td.horizon_minutes ?? "—"} 分钟`),
      el("dt", {}, "质量门槛"), el("dd", {}, td.min_quality_score ?? "—"),
      el("dt", {}, "切分方式"), el("dd", {}, (td.split || {}).method || "—"))),
    card("特征与依赖", "上线复现所需的最小信息", el("div", {},
      el("div", { style: { marginBottom: "8px" } },
        el("div", { style: { fontSize: "11px", color: "var(--txt3)", marginBottom: "4px" } }, "输入特征"),
        el("div", {}, ...(c.features || []).map(f => el("span", { class: "tag", style: { marginRight: "5px" } }, f)))),
      el("div", { style: { marginBottom: "8px" } },
        el("div", { style: { fontSize: "11px", color: "var(--txt3)", marginBottom: "4px" } }, "依赖版本"),
        el("div", {}, ...Object.entries(c.dependencies || {}).map(([k, val]) =>
          el("span", { class: "tag ok", style: { marginRight: "5px" } }, `${k} ${val}`)))),
      el("div", { class: "note", style: { marginTop: "4px" } }, c.license || "")))));

  const fm = c.failure_modes || [];
  if (fm.length) {
    box.appendChild(el("div", { style: { marginTop: "14px" } },
      card("已知失效场景", "上线前必须与业务方确认能否接受", el("div", {},
        ...fm.map((t, i) => el("div", { style: { fontSize: "12px", color: "var(--txt2)", margin: "4px 0" } },
          `${i + 1}. ${t}`))))));
  }

  const covs = Object.entries(cov);
  if (covs.length) {
    const rows = [];
    for (const [group, block] of covs) {
      for (const [k, val] of Object.entries(block || {})) {
        rows.push({ group, name: k, n: val.n, ade: val.trimmed_ADE_m });
      }
    }
    box.appendChild(el("div", { style: { marginTop: "14px" } },
      card("训练覆盖与分层表现", "覆盖不足的分组上线后误差会明显放大", table([
        { label: "分组", render: r => el("span", { class: "tag" }, r.group === "by_speed" ? "航速" : "船型") },
        { label: "取值", key: "name" },
        { label: "样本数", num: true, render: r => FMT.num(r.n) },
        { label: "截尾 ADE", num: true, render: r => (r.ade === null || r.ade === undefined) ? "—" : FMT.m(r.ade) },
      ], rows))));
  }
  return box;
}

/* ---------------------------------------------------------------- 漂移 */

function renderDrift(rows, verdict) {
  ui.drift.innerHTML = "";
  if (!rows || !rows.length) {
    ui.drift.appendChild(empty("尚未运行漂移检测；点击上方「运行漂移检测」"));
    return;
  }
  if (verdict) {
    const selfCmp = verdict.self_compare;
    ui.drift.appendChild(el("div", {
      class: "note",
      style: { marginBottom: "10px" },
    },
      el("span", { class: "tag " + (selfCmp ? "warn" : (ACTION_TAG[verdict.action] || "")) },
        selfCmp ? "自比提醒" : (ACTION_CN[verdict.action] || verdict.action)),
      ` ${verdict.message}`));
  }
  ui.drift.appendChild(table([
    { label: "特征", render: r => el("b", {}, r.feature) },
    { label: "说明", render: r => el("span", { style: { color: "var(--txt2)" } }, r.label) },
    { label: "PSI", num: true, render: r => r.psi === null ? "—" : Number(r.psi).toFixed(4) },
    { label: "判定", render: r => el("span", { class: "tag " + (LEVEL_TAG[r.level] || "") }, LEVEL_CN[r.level] || r.level) },
    { label: "缺失率(参照→当前)", num: true, render: r =>
      (r.missing_reference === null ? "—" : (r.missing_reference * 100).toFixed(1) + "%") + " → " +
      (r.missing_current === null ? "—" : (r.missing_current * 100).toFixed(1) + "%") },
  ], rows));
}

/* ---------------------------------------------------------------- 误差趋势 */

function renderTrend(trend) {
  ui.trend.innerHTML = "";
  if (!trend || !trend.available) {
    ui.trend.appendChild(empty((trend && trend.message) || "暂无回填误差记录"));
    return;
  }
  const ok = !trend.worsened;
  ui.trend.appendChild(el("div", { class: "note", style: ok ? {} : { borderColor: "rgba(248,113,113,.5)" } },
    el("span", { class: "tag " + (ok ? "ok" : "bad") }, ok ? "误差可控" : "持续恶化"),
    ` 首评 ${FMT.m(trend.baseline_ade_m)} → 最新 ${FMT.m(trend.latest_ade_m)}` +
    (trend.worsen_pct === null ? "" : `（${trend.worsen_pct > 0 ? "+" : ""}${trend.worsen_pct}%）`)));
  const series = trend.series || [];
  if (series.length >= 2) {
    const cEl = el("div", { class: "chart", style: { height: "200px", marginTop: "10px" } });
    ui.trend.appendChild(cEl);
    chartLine(cEl, series.map(p => String(p.ts).slice(5, 16)),
      [{ name: "回填 ADE (m)", data: series.map(p => p.ade_m), area: true, showSymbol: true }],
      { yName: "m", boundaryGap: false });
  }
  ui.trend.appendChild(el("div", { class: "note", style: { marginTop: "8px" } },
    trend.message || ""));
}

/* ---------------------------------------------------------------- 操作区 */

function renderActions(d) {
  ui.action.innerHTML = "";
  const log = (d.log || {}).points || [];
  const mon = (d.card || {}).monitoring || {};
  ui.action.appendChild(el("div", { class: "note", style: { marginBottom: "10px" } },
    `监控阈值：PSI ≥ ${mon.psi_warn ?? 0.1} 需关注，≥ ${mon.psi_alert ?? 0.25} 建议重训；` +
    `回填误差较首评恶化 ≥ ${mon.error_worsen_pct ?? 20}% 触发重训提示。` +
    "模型上线前须形成模型卡并通过业务与合规确认，预测结果不得作为航行安全或执法的唯一依据。"));
  if (!log.length) {
    ui.action.appendChild(empty("暂无监控点；点击「登记监控点」补录推理延迟 / 回填误差 / 覆盖率"));
    return;
  }
  ui.action.appendChild(table([
    { label: "时间", render: r => FMT.ts(r.ts) },
    { label: "预测数", num: true, render: r => FMT.num(r.n_predictions) },
    { label: "覆盖率", num: true, render: r => r.coverage === null ? "—" : (r.coverage * 100).toFixed(1) + "%" },
    { label: "推理延迟", num: true, render: r => r.latency_ms === null ? "—" : Number(r.latency_ms).toFixed(1) + " ms" },
    { label: "回填 ADE", num: true, render: r => r.backfill_ade_m === null ? "—" : FMT.m(r.backfill_ade_m) },
    { label: "漂移", render: r => r.drift_action
      ? el("span", { class: "tag " + (ACTION_TAG[r.drift_action] || "") }, ACTION_CN[r.drift_action] || r.drift_action) : "—" },
    { label: "备注", render: r => el("span", { style: { color: "var(--txt3)" } }, r.note || "") },
  ], log.slice().reverse().slice(0, 30)));
}

/* ---------------------------------------------------------------- 登记监控点 */

function openPointDialog(modelDir, onDone) {
  const wrap = document.getElementById("authModal");
  if (!wrap) return;
  // 复用全局 modal 容器样式，但独立渲染表单，避免与鉴权弹窗互相干扰
  const holder = el("div", {});
  const inputs = {};
  const fields = [
    ["n_predictions", "预测条数", "1000"],
    ["n_backfilled", "已回填条数", "950"],
    ["latency_ms", "推理延迟 (ms)", "50"],
    ["backfill_ade_m", "回填 ADE (m)", ""],
    ["missing_rate", "缺失率 (0~1)", ""],
  ];
  const grid = el("div", { class: "grid g2" });
  for (const [k, label, ph] of fields) {
    const inp = el("input", { type: "text", placeholder: ph });
    inputs[k] = inp;
    grid.appendChild(el("div", { class: "field" }, el("label", {}, label), inp));
  }
  const noteInp = el("input", { type: "text", placeholder: "例如：2025-01-02 全量回填" });
  const tsInp = el("input", { type: "text", placeholder: "YYYY-MM-DD HH:MM（留空=当前时间）" });
  holder.appendChild(el("div", { style: { fontSize: "12px", color: "var(--txt2)", marginBottom: "10px" } },
    "手工补录一条监控点（可用于离线回填评估或人工压测结果）。留空表示该项无数据。"));
  holder.appendChild(grid);
  holder.appendChild(el("div", { class: "field", style: { marginTop: "10px" } },
    el("label", {}, "上报时间（补录历史结果时填）"), tsInp));
  holder.appendChild(el("div", { class: "field", style: { marginTop: "10px" } },
    el("label", {}, "备注"), noteInp));
  const err = el("div", { style: { color: "#fca5a5", fontSize: "11.5px", minHeight: "16px", marginTop: "6px" } });

  const submit = el("button", { class: "btn primary" }, "提交");
  const cancel = el("button", { class: "btn ghost" }, "取消");
  holder.appendChild(err);
  holder.appendChild(el("div", { class: "modal-actions" }, cancel, submit));

  // 构造一个临时 modal（使用与鉴权弹窗相同的样式类）
  const modal = el("div", { class: "modal" }, el("div", { class: "modal-box" },
    el("h3", {}, "登记监控点"), holder));
  document.body.appendChild(modal);

  const close = () => modal.remove();
  cancel.addEventListener("click", close);
  modal.addEventListener("click", e => { if (e.target === modal) close(); });

  submit.addEventListener("click", async () => {
    err.textContent = "";
    const num = k => {
      const raw = String(inputs[k].value).trim();
      if (!raw) return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : NaN;
    };
    const body = { note: noteInp.value.trim() };
    const tsRaw = tsInp.value.trim();
    if (tsRaw) body.ts = tsRaw;
    for (const k of Object.keys(inputs)) {
      const v = num(k);
      if (Number.isNaN(v)) { err.textContent = `${k} 必须是数字`; return; }
      if (k === "n_predictions" || k === "n_backfilled") body[k] = v === null ? 0 : Math.round(v);
      else if (v !== null) body[k] = v;
    }
    if (body.n_backfilled > body.n_predictions) {
      err.textContent = "已回填条数不能大于预测条数";
      return;
    }
    submit.disabled = true;
    try {
      const r = await api(`/models/${encodeURIComponent(modelDir)}/monitor`, {
        method: "POST", body: JSON.stringify(body),
      });
      toast("已登记监控点");
      close();
      if (onDone) onDone(r);
    } catch (e) {
      err.textContent = e.message;
    } finally {
      submit.disabled = false;
    }
  });
}
