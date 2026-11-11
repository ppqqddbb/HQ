/* 页面通用控件：参数表单、任务状态条、状态标签、空态与错误块
 * 注意：本文件位于 pages/ 下，需通过 ES module import 使用。
 */

export const STATUS_CN = {
  queued: "排队中", running: "运行中", succeeded: "已完成",
  failed: "失败", cancelled: "已取消", created: "已创建",
};

export function statusTag(status) {
  const map = {
    succeeded: "ok", running: "acc", queued: "warn", failed: "bad", cancelled: "vio",
  };
  return el("span", { class: "tag " + (map[status] || "") }, STATUS_CN[status] || status || "—");
}

/** 单个表单字段 */
export function field(def) {
  let input;
  if (def.type === "select") {
    input = el("select", {}, ...(def.options || []).map(o =>
      el("option", { value: o.v }, o.t)));
    if (def.value !== undefined && def.value !== null) input.value = String(def.value);
  } else if (def.type === "checkbox") {
    input = el("input", { type: "checkbox" });
    input.checked = !!def.value;
  } else if (def.type === "textarea") {
    input = el("textarea", {
      rows: def.rows || 3,
      placeholder: def.placeholder || "",
      style: { minWidth: (def.width || 320) + "px", resize: "vertical" },
    });
    input.value = def.value || "";
  } else {
    input = el("input", {
      type: def.type === "text" ? "text" : "number",
      step: def.step !== undefined ? def.step : 1,
      min: def.min !== undefined ? def.min : null,
      max: def.max !== undefined ? def.max : null,
      placeholder: def.placeholder || "",
    });
    input.value = def.value !== undefined && def.value !== null ? String(def.value) : "";
  }
  const lab = el("label", {}, def.label + (def.unit ? `（${def.unit}）` : ""));
  const w = el("div", { class: "field", style: { minWidth: (def.width || 110) + "px" } },
    lab, input);
  if (def.hint) w.appendChild(el("div", { style: { fontSize: "10.5px", color: "var(--txt3)" } }, def.hint));
  w._input = input;
  w._def = def;
  return w;
}

/** 参数表单：返回带 values() 的工具栏节点 */
export function form(fields) {
  const nodes = fields.map(field);
  const box = el("div", { class: "toolbar" }, ...nodes);
  box.values = () => {
    const out = {};
    for (const n of nodes) {
      const d = n._def, i = n._input;
      if (d.type === "checkbox") out[d.key] = i.checked;
      else if (d.type === "select") out[d.key] = d.numeric ? Number(i.value) : i.value;
      else if (d.type === "text" || d.type === "textarea") {
        const s = i.value.trim();
        out[d.key] = s === "" ? null : s;
      } else {
        const v = i.value === "" ? null : Number(i.value);
        out[d.key] = v === null || Number.isNaN(v) ? (d.value ?? null) : v;
      }
    }
    return out;
  };
  box.reset = (vals) => {
    for (const n of nodes) {
      const d = n._def, v = vals[d.key];
      if (v === undefined) continue;
      if (d.type === "checkbox") n._input.checked = !!v; else n._input.value = String(v);
    }
  };
  return box;
}

/** 任务状态条（进度 + 阶段 + 取消） */
export function statusBox(onCancel) {
  const bar = el("i", { style: { width: "0%" } });
  const txt = el("div", { style: { fontSize: "12px", color: "var(--txt2)" } }, "等待提交…");
  const btnCancel = el("button", { class: "btn ghost sm", style: { display: "none" } }, "取消");
  const wrap = el("div", { class: "taskbar", style: { display: "none", marginBottom: "12px" } },
    el("span", { class: "pill acc" }, "任务"),
    el("div", { class: "grow" }, txt,
      el("div", { class: "progress", style: { marginTop: "5px" } }, bar)),
    btnCancel);
  let currentTask = null;
  btnCancel.addEventListener("click", async () => {
    if (!currentTask) return;
    btnCancel.disabled = true;
    btnCancel.textContent = "取消中…";
    try { await api(`/tasks/${currentTask}/cancel`, { method: "POST" }); toast("已请求取消"); }
    catch (e) { toast(e.message, true); }
  });
  wrap.set = (t) => {
    if (!t) return;
    wrap.style.display = "";
    currentTask = t.task_id;
    const pct = (Number(t.progress || 0) * 100);
    bar.style.width = Math.max(1.5, pct).toFixed(1) + "%";
    txt.textContent = `${STATUS_CN[t.status] || t.status} · ${t.current_stage || ""} · ${pct.toFixed(0)}%`;
    const running = t.status === "running" || t.status === "queued";
    btnCancel.style.display = running ? "" : "none";
    if (running) { btnCancel.disabled = false; btnCancel.textContent = "取消"; }
  };
  wrap.hide = () => { wrap.style.display = "none"; currentTask = null; };
  return wrap;
}

/** 错误提示卡片 */
export function errorCard(title, err) {
  return card(title, null,
    el("div", { class: "note", style: { borderColor: "rgba(248,113,113,.4)", color: "#fca5a5" } },
      String(err && err.message ? err.message : err)));
}

/** 分析页通用的「首屏加载最近结果 + 重新运行」外壳 */
export function analysisShell(view, opts) {
  /* opts: { title, sub, form: node, runLabel, onRun(values), height } */
  const head = el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "0" } },
      opts.form,
      el("button", { class: "btn primary", onclick: () => opts.onRun() }, opts.runLabel || "开始分析")),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } }, opts.hint || ""));
  const status = statusBox();
  const body = el("div", {});
  view.appendChild(head);
  view.appendChild(status);
  view.appendChild(body);
  return { head, status, body };
}
