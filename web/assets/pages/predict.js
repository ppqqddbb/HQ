/* 航迹预测：CV / Kalman / GRU 三模型回测对比（ADE / FDE / P90 + 分层指标） */
import { state } from "../state.js";
import { runTask, latestSucceeded, getSummary } from "../taskrun.js";
import { form, statusBox } from "./_ui.js";

const MODEL_CN = { cv: "匀速外推 CV", kalman: "卡尔曼滤波", gru: "GRU 残差学习" };
const MODEL_COLOR = { cv: "#f472b6", kalman: "#a78bfa", gru: "#fbbf24" };

export async function render(view) {
  const f = form([
    { key: "input_minutes", label: "历史输入", type: "number", value: 60, min: 10, step: 5, unit: "分钟" },
    { key: "horizon_minutes", label: "预测时长", type: "number", value: 30, min: 5, step: 5, unit: "分钟" },
    { key: "step_minutes", label: "时间步长", type: "number", value: 5, min: 1, step: 1, unit: "分钟" },
    { key: "max_trajectories", label: "参与轨迹数", type: "number", value: 800, min: 50, step: 50 },
    { key: "gru_epochs", label: "GRU 训练轮数", type: "number", value: 30, min: 5, step: 5 },
    { key: "gru_hidden", label: "GRU 隐藏维度", type: "number", value: 64, min: 16, step: 16 },
    { key: "min_quality_score", label: "最低质量分", type: "number", value: 60, min: 0, max: 100, step: 5 },
  ]);
  const runBtn = el("button", { class: "btn primary" }, "运行预测回测");

  view.appendChild(el("div", { style: { marginBottom: "14px" } },
    el("div", { class: "toolbar", style: { marginBottom: "6px" } }, f, runBtn),
    el("div", { style: { fontSize: "11.5px", color: "var(--txt3)" } },
      "评估口径：按窗口起点时间做时间外推切分（前 70% 训练、后 30% 测试），不使用随机切分以免时间泄露；" +
      "坐标使用以轨迹起点为原点的局部 ENU 米制坐标。同时报告均值与 95% 截尾均值，避免极少数异常样本主导结论。")));

  const status = statusBox();
  view.appendChild(status);

  const kBox = el("div", {});
  const cmpBody = el("div", {});
  const stepBody = el("div", {});
  const stratBody = el("div", {});
  const sampleBody = el("div", {});
  const lossBody = el("div", {});
  const verdictBody = el("div", {});

  view.appendChild(kBox);
  view.appendChild(el("div", { class: "grid g2", style: { marginBottom: "14px" } },
    card("模型误差对比", "截尾 ADE（米，越低越好）与平均 ADE", cmpBody),
    card("逐步误差曲线", "各预测步的平均位移误差", stepBody)));
  view.appendChild(card("分层评估", "按航速 / 船型分层，避免静止船舶样本稀释整体结论", stratBody));
  view.appendChild(card("预测样例轨迹对比", "以起点为原点的相对位移（米）；历史 → 真值 → 各模型预测", sampleBody));
  view.appendChild(el("div", { class: "grid g2" },
    card("训练过程", "GRU 训练损失曲线（仅用于监控收敛，不作为上线依据）", lossBody),
    card("上线判定", "仅当在独立时间测试集上稳定优于基线时才建议上线", verdictBody)));

  runBtn.addEventListener("click", async () => {
    const v = f.values();
    runBtn.disabled = true;
    try {
      const out = await runTask("predict", {
        input_minutes: v.input_minutes, horizon_minutes: v.horizon_minutes,
        step_minutes: v.step_minutes, max_trajectories: v.max_trajectories,
        gru_epochs: v.gru_epochs, gru_hidden: v.gru_hidden,
        min_quality_score: v.min_quality_score,
      }, {}, { onProgress: t => status.set(t) });
      status.set(out.task);
      toast("预测回测完成");
      show(out.summary);
    } catch (e) {
      toast(e.message, true);
      status.set(e.task || { status: "failed", current_stage: e.message, progress: 1 });
    } finally { runBtn.disabled = false; }
  });

  kBox.appendChild(loading("正在加载预测结果…"));
  try {
    const t = await latestSucceeded("predict", state.dataset);
    if (t) { status.set(t); kBox.innerHTML = ""; show(await getSummary(t.task_id)); }
    else {
      kBox.innerHTML = "";
      kBox.appendChild(card("还没有预测结果", null,
        el("div", { class: "note" }, "点击「运行预测回测」训练 CV / Kalman / GRU 并评估。首次运行约 2–5 分钟。")));
    }
  } catch (e) {
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
  }

  /* ------------------------------ 渲染 ------------------------------ */
  function show(d) {
    const cfg = d.config || {};
    const m = d.metrics || {};
    const models = Object.keys(m);
    if (!models.length) {
      kBox.innerHTML = "";
      kBox.appendChild(el("div", { class: "note" }, "结果中没有模型指标。"));
      return;
    }
    const verdict = d.verdict || {};
    const best = models.reduce((a, b) =>
      ((m[b].trimmed_ADE_m ?? 1e18) < (m[a].trimmed_ADE_m ?? 1e18) ? b : a), models[0]);

    /* KPI */
    kBox.innerHTML = "";
    kBox.appendChild(el("div", { class: "grid g4", style: { marginBottom: "14px" } },
      kpi("样本窗口", FMT.num(cfg.n_windows),
        `训练 ${FMT.big(cfg.n_train)} / 测试 ${FMT.big(cfg.n_test)}`),
      kpi("参与轨迹", FMT.num(cfg.n_trajectories),
        `步长 ${cfg.step_minutes} 分钟 · 预测 ${cfg.horizon_steps} 步`),
      kpi("最优模型", MODEL_CN[best] || best,
        m[best] ? `截尾 ADE ${FMT.m(m[best].trimmed_ADE_m)}` : "—"),
      kpi("GRU 相对基线", verdict.improvement_pct !== undefined ? verdict.improvement_pct.toFixed(1) + "%" : "—",
        verdict.can_go_online ? "具备上线条件" : "暂不建议上线")));

    /* 误差对比：截尾 ADE 与平均 ADE 用分组柱状图展示，柱高可比且省空间 */
    cmpBody.innerHTML = "";
    const c1 = el("div", { class: "chart", style: { height: "300px" } });
    cmpBody.appendChild(c1);
    chartBar(c1, models.map(x => MODEL_CN[x] || x), null, {
      yName: "ADE（米）",
      showLabel: true,
      labelFmt: v => FMT.m(v),
      series: [
        { name: "截尾 ADE（P95）", data: models.map(x => Number((m[x].trimmed_ADE_m ?? 0).toFixed(1))), color: "#38bdf8" },
        { name: "平均 ADE", data: models.map(x => Number((m[x].ADE_m ?? 0).toFixed(1))), color: "#f472b6", color2: "rgba(244,114,182,.15)" },
      ],
    });
    cmpBody.appendChild(el("div", { class: "note", style: { marginTop: "8px" } },
      `截尾 ADE 取 95% 截尾均值，对少数漂移轨迹更稳健；平均 ADE 保留长尾影响。` +
      `当前最优：${MODEL_CN[best] || best}（截尾 ${FMT.m(m[best].trimmed_ADE_m)}，` +
      `相比匀速基线改善 ${verdict.improvement_pct !== undefined ? verdict.improvement_pct.toFixed(1) + "%" : "—"}）。`));

    /* 逐步误差 */
    stepBody.innerHTML = "";
    const s1 = el("div", { class: "chart", style: { height: "300px" } });
    stepBody.appendChild(s1);
    const steps = (m[models[0]] && m[models[0]].per_step_m) ? m[models[0]].per_step_m.length : 6;
    chartLine(s1, Array.from({ length: steps }, (_, i) => `+${(i + 1) * (cfg.step_minutes || 5)}min`),
      models.map(x => ({ name: MODEL_CN[x] || x, data: m[x].per_step_m || [], showSymbol: true, color: MODEL_COLOR[x] })),
      { yName: "误差（米）" });

    /* 指标表 + 分层 */
    stratBody.innerHTML = "";
    stratBody.appendChild(table([
      { label: "模型", render: r => MODEL_CN[r.model] || r.model },
      { label: "样本数", num: true, render: r => FMT.num(r.n_test) },
      { label: "平均 ADE", num: true, render: r => FMT.m(r.ADE_m) },
      { label: "截尾 ADE (95%)", num: true, render: r => FMT.m(r.trimmed_ADE_m) },
      { label: "中位误差", num: true, render: r => FMT.m(r.median_m) },
      { label: "FDE", num: true, render: r => FMT.m(r.FDE_m) },
      { label: "P90", num: true, render: r => FMT.m(r.P90_m) },
    ], models.map(x => ({ model: x, ...m[x] }))));

    const strat = d.stratified || {};
    if (Object.keys(strat).length) {
      const bySpeed = (strat[models[0]] || {}).by_speed || {};
      const rows = Object.keys(bySpeed).map(k => {
        const row = { bucket: k };
        for (const mdl of models) row[mdl] = ((strat[mdl] || {}).by_speed || {})[k] || null;
        return row;
      });
      stratBody.appendChild(el("div", { style: { marginTop: "14px" } },
        el("div", { style: { fontSize: "12px", color: "var(--txt2)", marginBottom: "6px" } }, "按航速分层（截尾 ADE）"),
        table([
          { label: "航速区间", key: "bucket" },
          ...models.map(mdl => ({
            label: MODEL_CN[mdl] || mdl, num: true,
            render: r => r[mdl] ? `${FMT.m(r[mdl].trimmed_ADE_m)}（n=${FMT.num(r[mdl].n)}）` : "—",
          })),
        ], rows)));

      const byType = (strat[models[0]] || {}).by_ship_type || {};
      const rows2 = Object.keys(byType)
        .sort((a, b) => ((byType[b] || {}).n || 0) - ((byType[a] || {}).n || 0))
        .map(k => {
          const row = { bucket: k };
          for (const mdl of models) row[mdl] = ((strat[mdl] || {}).by_ship_type || {})[k] || null;
          return row;
        });
      stratBody.appendChild(el("div", { style: { marginTop: "14px" } },
        el("div", { style: { fontSize: "12px", color: "var(--txt2)", marginBottom: "6px" } }, "按船型分层（截尾 ADE）"),
        table([
          { label: "船型", key: "bucket" },
          { label: "样本", num: true, render: r => r[models[0]] ? FMT.num(r[models[0]].n) : "—" },
          ...models.map(mdl => ({
            label: MODEL_CN[mdl] || mdl, num: true,
            render: r => r[mdl] ? FMT.m(r[mdl].trimmed_ADE_m) : "—",
          })),
        ], rows2)));
    }

    /* 样例轨迹 */
    sampleBody.innerHTML = "";
    const samples = (d.samples || []).slice(0, 3);
    if (samples.length) {
      const wrap = el("div", { class: "grid g3" });
      samples.forEach((s, i) => {
        const cEl = el("div", { class: "chart", style: { height: "260px" } });
        const holder = el("div", {});
        holder.appendChild(el("div", { style: { fontSize: "11px", color: "var(--txt3)", marginBottom: "2px" } },
          `样例 ${i + 1} · ${String(s.traj_id).slice(0, 8)}… · 均速 ${s.mean_sog_kn ?? "—"} kn · ADE ${s.ade_m !== undefined ? FMT.m(s.ade_m) : "—"}`));
        holder.appendChild(cEl);
        wrap.appendChild(holder);
        drawPath(cEl, s, models);
      });
      sampleBody.appendChild(wrap);
      sampleBody.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        "坐标已平移到“历史最后一点”为原点，单位为米；蓝色历史线位于左下方，右上方为真值与各模型预测。" +
        "样例优先选取在航且误差接近中位水平的窗口，仅用于直观检查形态，量化结论以全测试集指标为准。"));
    } else sampleBody.appendChild(empty("无样例数据"));

    /* 训练损失 */
    lossBody.innerHTML = "";
    const gru = d.gru || {};
    if ((gru.loss_curve || []).length) {
      const lEl = el("div", { class: "chart", style: { height: "240px" } });
      lossBody.appendChild(lEl);
      chartLine(lEl, gru.loss_curve.map((_, i) => "E" + (i + 1)),
        [{ name: "训练损失", data: gru.loss_curve, area: true, showSymbol: false }],
        { yName: "MSE", boundaryGap: false });
      lossBody.appendChild(el("div", { class: "note", style: { marginTop: "8px" } },
        `Epochs ${gru.epochs} · 隐藏维度 ${gru.hidden} · 参数量 ${FMT.num(gru.n_params)} · ` +
        `初始损失 ${fmtSci(gru.initial_loss)} → 最终 ${fmtSci(gru.final_loss)}`));
    } else lossBody.appendChild(empty("未启用 GRU 模型"));

    /* 上线判定 */
    verdictBody.innerHTML = "";
    const ok = !!verdict.can_go_online;
    verdictBody.appendChild(el("div", { class: "grid g2", style: { marginBottom: "10px" } },
      kpi("GRU 截尾 ADE", verdict.gru_ade !== undefined ? FMT.m(verdict.gru_ade) : "—", "独立时间测试集"),
      kpi("基线截尾 ADE", verdict.baseline_ade !== undefined ? FMT.m(verdict.baseline_ade) : "—",
        `基线模型 ${MODEL_CN[verdict.baseline] || verdict.baseline || "—"}`)));
    verdictBody.appendChild(el("div", { class: "note", style: ok ? {} : { borderColor: "rgba(251,191,36,.4)" } },
      el("div", {}, el("span", { class: "tag " + (ok ? "ok" : "warn") }, ok ? "建议上线" : "暂不上线"),
        ` 相对基线改善 ${verdict.improvement_pct !== undefined ? verdict.improvement_pct.toFixed(2) + "%" : "—"}`),
      el("div", { style: { marginTop: "6px" } }, verdict.note || "")));

    if (d.split) {
      verdictBody.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        `切分方式：${d.split.method}；归一化：${d.split.normalization}`));
    }
    if ((d.notes || []).length) {
      verdictBody.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
        ...d.notes.map(n => el("div", {}, "· " + n))));
    }
  }
}

/** 用二维折线绘制历史 / 真值 / 各模型预测轨迹 */
function drawPath(dom, s, models) {
  const series = [
    { name: "历史", data: (s.history || []).map(p => [p[0], p[1]]), color: "#38bdf8", w: 2 },
    { name: "真值", data: (s.truth || []).map(p => [p[0], p[1]]), color: "#34d399", w: 2 },
  ];
  for (const mdl of models) {
    const pr = (s.predictions || {})[mdl];
    if (pr && pr.length) series.push({
      name: MODEL_CN[mdl] || mdl, data: pr.map(p => [p[0], p[1]]),
      color: MODEL_COLOR[mdl] || "#9db0c9", w: 2,
    });
  }
  mkChart(dom, {
    tooltip: Object.assign({ trigger: "axis" }, THEME.tooltip),
    legend: { textStyle: { color: "#9db0c9", fontSize: 10 }, top: 0, icon: "roundRect", itemWidth: 9, itemHeight: 9 },
    grid: { left: 62, right: 22, top: 44, bottom: 42, containLabel: true },
    // 设置等比例坐标轴，避免经纬度/米制比例失真导致轨迹形态被拉歪
    xAxis: Object.assign({
      type: "value", scale: true, name: "东向 (m)",
      nameTextStyle: { color: "#6f839c", fontSize: 10 },
      nameGap: 24,
    }, AXIS),
    yAxis: Object.assign({
      type: "value", scale: true, name: "北向 (m)",
      nameTextStyle: { color: "#6f839c", fontSize: 10 },
      nameGap: 38,
    }, AXIS),
    series: series.map(x => ({
      name: x.name, type: "line", data: x.data, showSymbol: true,
      symbolSize: 3.5, smooth: false, lineStyle: { width: x.w, color: x.color },
      itemStyle: { color: x.color }, emphasis: { focus: "series" },
    })),
  });
}

function fmtSci(v) {
  if (v === null || v === undefined) return "—";
  return Number(v) < 0.001 ? Number(v).toExponential(2) : Number(v).toFixed(4);
}
