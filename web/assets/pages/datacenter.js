/* 数据中心：CSV 预检、导入、质量报告与产物下载 */
import { state } from "../state.js";
import { getTask, getSummary } from "../taskrun.js";
import { statusTag, field } from "./_ui.js";

let selected = null;      // 当前选中的数据集版本
let quality = null;

export async function render(view) {
  const top = el("div", {});
  const mid = el("div", {});
  const bot = el("div", {});
  view.appendChild(top);
  view.appendChild(mid);
  view.appendChild(bot);

  /* ---------------- 1. 导入区 ---------------- */
  const pathInput = el("textarea", {
    rows: 4,
    style: { minWidth: "520px", resize: "vertical" },
    placeholder: "每行一个服务器本地 CSV 路径，例如：\nD:/data/aisdk-2025-01-02.csv",
  });
  const nameInput = el("input", { type: "text", placeholder: "数据源名称，如 aisdk" });
  nameInput.value = "aisdk";
  const dvInput = el("input", { type: "text", placeholder: "留空自动生成" });
  const timeFmtInput = el("input", { type: "text", placeholder: "自动识别，如 %d/%m/%Y %H:%M:%S" });
  const cleanChk = el("input", { type: "checkbox" });
  cleanChk.checked = true;

  const preBtn = el("button", { class: "btn" }, "预检");
  const impBtn = el("button", { class: "btn primary" }, "导入并治理");
  const preOut = el("div", {});

  /* 浏览器直传：选文件 → POST /datasets/upload（流式落盘到 data/uploads）
   * 落盘成功后把服务器路径自动回填到上面的路径框，用户确认后再执行预检/导入。 */
  const filePick = el("input", { type: "file", accept: ".csv,.txt,.tsv", multiple: true, style: { display: "none" } });
  const upBtn = el("button", { class: "btn" }, "选择文件上传");
  const upHint = el("div", { style: { fontSize: "11px", color: "var(--txt3)" } },
    "上传只负责把原始文件安全存到 data/uploads；导入仍需在下方确认。");
  upBtn.addEventListener("click", () => filePick.click());
  filePick.addEventListener("change", async () => {
    const fs = [...filePick.files];
    if (!fs.length) return;
    upBtn.disabled = true;
    const old = upBtn.textContent;
    upBtn.textContent = "上传中…";
    preOut.innerHTML = "";
    preOut.appendChild(loading(`正在上传 ${fs.length} 个文件…`));
    try {
      const fd = new FormData();
      for (const f of fs) fd.append("files", f, f.name);
      const r = await fetch(`/api/v1/datasets/upload?preflight=true`, { method: "POST", body: fd });
      const body = await r.json();
      if (!r.ok || body.ok === false) {
        throw new Error((body.error && body.error.message) || `HTTP ${r.status}`);
      }
      const d = body.data;
      const lines = (pathInput.value || "").split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      for (const p of d.paths) if (!lines.includes(p)) lines.push(p);
      pathInput.value = lines.join("\n");
      preOut.innerHTML = "";
      preOut.appendChild(el("div", { class: "note" }, `已上传 ${d.files.length} 个文件并回填路径，请确认后执行导入。`));
      const pfs = d.files.map(f => f.preflight).filter(Boolean);
      if (pfs.length) preOut.appendChild(renderPreflight(pfs));
      toast(`上传完成：${d.files.length} 个文件`);
    } catch (e) {
      preOut.innerHTML = "";
      preOut.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
    } finally {
      upBtn.disabled = false;
      upBtn.textContent = old;
      filePick.value = "";
    }
  });

  const importCard = card("导入 AIS 原始数据", "先预检确认编码 / 分隔符 / 时间格式，再执行导入",
    el("div", { class: "toolbar" },
      el("div", { class: "field", style: { minWidth: "520px" } },
        el("label", {}, "CSV 文件路径（每行一个）"), pathInput),
      el("div", { class: "field" }, el("label", {}, "数据源名称"), nameInput),
      el("div", { class: "field" }, el("label", {}, "数据集版本标识"), dvInput),
      el("div", { class: "field" }, el("label", {}, "时间格式（可选）"), timeFmtInput),
      el("div", { class: "field" }, el("label", {}, "导入后立即治理"), cleanChk),
      preBtn, impBtn, upBtn, filePick),
    upHint,
    preOut);
  top.appendChild(importCard);

  const paths = () => pathInput.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);

  preBtn.addEventListener("click", async () => {
    if (!paths().length) return toast("请填写 CSV 路径", true);
    preBtn.disabled = true;
    preOut.innerHTML = "";
    preOut.appendChild(loading("正在预检文件（读取表头与样本行）…"));
    try {
      const res = await api("/datasets/preflight", {
        method: "POST",
        body: JSON.stringify({ paths: paths(), source_name: nameInput.value || "ais" }),
      });
      preOut.innerHTML = "";
      preOut.appendChild(renderPreflight(res));
    } catch (e) {
      preOut.innerHTML = "";
      preOut.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
    } finally { preBtn.disabled = false; }
  });

  impBtn.addEventListener("click", async () => {
    if (!paths().length) return toast("请填写 CSV 路径", true);
    const big = !confirm("导入大文件会占用较长时间（数分钟），期间可在任务面板查看进度。是否继续？");
    if (!big) return;
    impBtn.disabled = true;
    try {
      const res = await api("/datasets/import", {
        method: "POST",
        body: JSON.stringify({
          paths: paths(),
          source_name: nameInput.value || "ais",
          dataset_version: dvInput.value.trim() || null,
          time_format: timeFmtInput.value.trim() || null,
          clean: cleanChk.checked,
          created_by: "web",
        }),
      });
      toast(`导入完成：${FMT.big(res.row_count || 0)} 行`);
      if (res.clean_task_id) {
        preOut.innerHTML = "";
        preOut.appendChild(el("div", { class: "note" },
          `导入已落盘，治理任务已提交：${res.clean_task_id}。正在等待治理完成…`));
        await waitTask(res.clean_task_id, preOut);
        selected = res.dataset_version;
      }
      await refresh();
    } catch (e) {
      toast(e.message, true);
    } finally { impBtn.disabled = false; }
  });

  /* ---------------- 2. 数据集与批次 ---------------- */
  const listBox = el("div", {});
  mid.appendChild(card("数据集版本", "点击查看质量报告与可下载产物", listBox));

  /* ---------------- 3. 质量报告 ---------------- */
  const qBox = el("div", {});
  bot.appendChild(card("数据质量报告", "五维加权评分 · 异常标记 · 轨迹切分统计", qBox));

  async function refresh() {
    listBox.innerHTML = "";
    listBox.appendChild(loading());
    let ds = [];
    try { ds = await api("/datasets"); } catch (e) { ds = []; }
    listBox.innerHTML = "";
    if (!ds.length) {
      listBox.appendChild(empty("暂无批次记录，请先导入数据"));
    } else {
      const rows = ds.map(d => ({ ...d }));
      listBox.appendChild(table([
        { label: "批次", key: "batch_id" },
        { label: "数据源", key: "source_name" },
        { label: "行数", num: true, render: r => FMT.big(r.row_count) },
        { label: "MMSI", num: true, render: r => FMT.num(r.mmsi_count) },
        { label: "编码", render: r => r.encoding || "—" },
        { label: "导入时间", render: r => FMT.ts(r.created_at) },
        {
          label: "操作", render: r => el("button", {
            class: "btn sm", onclick: () => { nav("#/datacenter"); toast("批次详情：" + r.batch_id); }
          }, "详情"),
        },
      ], rows));
    }

    // 数据集版本列表
    const vers = (state.overview && state.overview.datasets) || [];
    if (vers.length) {
      const vbox = el("div", { style: { marginTop: "12px" } },
        el("div", { class: "legend" }, el("span", {}, el("b", {}, "已治理数据集："))),
        el("div", { class: "toolbar", style: { marginTop: "6px" } },
          ...vers.map(v => el("button", {
            class: "btn sm" + (v.dataset_version === selected ? " primary" : ""),
            onclick: async () => { selected = v.dataset_version; await loadQuality(); await refresh(); },
          }, v.dataset_version))));
      listBox.appendChild(vbox);
      if (!selected) selected = vers[0].dataset_version;
    }
    await loadQuality();
  }

  async function loadQuality() {
    qBox.innerHTML = "";
    if (!selected) { qBox.appendChild(empty("请选择一个数据集版本")); return; }
    qBox.appendChild(loading("正在读取质量报告…"));
    try {
      quality = await api(`/datasets/${encodeURIComponent(selected)}/quality`);
    } catch (e) {
      qBox.innerHTML = "";
      qBox.appendChild(el("div", { class: "note" },
        `该数据集暂无质量报告（${e.message}）。可能是导入时未开启治理。`));
      return;
    }
    qBox.innerHTML = "";
    renderQuality(qBox, quality, selected);
  }

  await refresh();
}

function nav(h) { location.hash = h; }

async function waitTask(taskId, box) {
  const t0 = Date.now();
  const bar = el("i", { style: { width: "2%" } });
  const txt = el("div", { style: { fontSize: "12px", color: "var(--txt2)" } }, "等待治理…");
  box.appendChild(el("div", { class: "taskbar", style: { marginTop: "10px" } },
    el("div", { class: "grow" }, txt, el("div", { class: "progress", style: { marginTop: "5px" } }, bar))));
  while (Date.now() - t0 < 3600 * 1000) {
    await new Promise(r => setTimeout(r, 1500));
    let t;
    try { t = await getTask(taskId); } catch (e) { txt.textContent = "任务状态查询失败"; return; }
    bar.style.width = Math.max(2, (t.progress || 0) * 100).toFixed(1) + "%";
    txt.textContent = `${t.status_cn || t.status} · ${t.current_stage || ""} · ${((t.progress || 0) * 100).toFixed(0)}%`;
    if (t.status === "succeeded") { txt.textContent = "治理完成 ✓"; toast("数据治理完成"); return; }
    if (t.status === "failed" || t.status === "cancelled") {
      txt.textContent = `治理${t.status_cn || t.status}：${String(t.error_message || "").split("\n")[0]}`;
      toast("治理失败", true);
      return;
    }
  }
}

/* ------------------------------- 预检结果 ------------------------------- */
function renderPreflight(list) {
  const wrap = el("div", { style: { marginTop: "12px" } });
  for (const p of list) {
    if (p.error) {
      wrap.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } },
        `${p.path}：${p.error}`));
      continue;
    }
    const cols = (p.columns || []).map(c => ({
      name: c,
      std: (p.mapped_columns || {})[c] || "—",
      type: (p.inferred_types || {})[c] || "—",
      miss: (p.missing_ratio || {})[c],
    }));
    wrap.appendChild(el("div", {
      style: {
        border: "1px solid var(--line)", borderRadius: "8px",
        padding: "10px 12px", marginBottom: "10px",
      },
    },
      el("div", { class: "toolbar", style: { marginBottom: "8px" } },
        el("span", { class: "tag ok" }, "预检通过"),
        el("span", { class: "tag" }, `编码 ${p.encoding}`),
        el("span", { class: "tag" }, `分隔符 "${p.delimiter}"`),
        el("span", { class: "tag " + (p.time_format ? "ok" : "warn") },
          `时间格式 ${p.time_format || "未识别，需手工指定"}`),
        el("span", { class: "tag" }, `大小 ${(p.file_size / 1048576).toFixed(1)} MB`)),
      el("div", { class: "note", style: { wordBreak: "break-all" } },
        el("b", {}, "文件："), p.path, el("br"),
        el("b", {}, "SHA256："), p.sha256 || "—"),
      el("div", { style: { marginTop: "8px" } }, table([
        { label: "原始列名", key: "name" },
        { label: "标准字段", render: r => r.std === "—" ? el("span", { class: "tag" }, "未识别") : r.std },
        { label: "推断类型", key: "type" },
        { label: "缺失率", num: true, render: r => r.miss === undefined ? "—" : FMT.pct(r.miss) },
      ], cols, { tall: true }))));
  }
  return wrap;
}

/* ------------------------------- 质量报告 ------------------------------- */
function renderQuality(box, q, dv) {
  const dims = q.dimensions || {};
  const sc = el("div", { class: "chart", style: { height: "220px" } });
  const gc = el("div", {});
  const flagBox = el("div", {});

  box.appendChild(el("div", { class: "grid g4", style: { marginBottom: "12px" } },
    kpi("综合质量分", (q.overall_score ?? 0).toFixed(2), `数据集 ${dv}`),
    kpi("原始行数", FMT.big(q.row_raw), `有效位置 ${FMT.big(q.row_valid_position)}`),
    kpi("清洗后行数", FMT.big(q.row_clean), `去重率 ${FMT.pct(q.duplicate_rate || 0)}`),
    kpi("异常点占比", FMT.pct(q.error_ratio || 0), `标记点 ${FMT.big(q.flagged_points)}`)));

  box.appendChild(el("div", { class: "grid g2", style: { marginBottom: "12px" } },
    card("质量维度得分", "加权后得到综合分", sc),
    card("异常标记统计", "ERROR 类影响质量分，INFO 类仅提示", flagBox)));

  const keys = Object.keys(dims);
  if (keys.length) {
    chartBar(sc, keys, keys.map(k => Number(dims[k])),
      { yName: "得分", yOpt: { min: 60, max: 100 }, showLabel: true, labelFmt: v => v }); 
  } else sc.appendChild(empty("无维度数据"));

  const fc = q.flag_counts || {};
  const errFlags = q.error_flags || [], infoFlags = q.info_flags || [];
  const flagCN = {
    INVALID_TIME: "时间无效", TIME_CONFLICT: "时间冲突", SPEED_SPIKE: "航速突变",
    JUMP: "位置跳变", GPS_DRIFT: "GPS 漂移", INVALID_COG: "航向无效",
    STATIONARY_NOISE: "静止噪声", LOW_SPEED_COG_UNRELIABLE: "低速航向不可靠",
  };
  const items = Object.entries(fc).map(([k, v]) => ({
    label: (flagCN[k] || k) + (errFlags.includes(k) ? "（错误）" : ""),
    value: v,
    color: errFlags.includes(k)
      ? "linear-gradient(90deg,#f87171,#fca5a5)"
      : "linear-gradient(90deg,#fbbf24,#fde68a)",
  }));
  if (items.length) {
    flagBox.appendChild(bars(items.sort((a, b) => b.value - a.value), { fmt: v => FMT.big(v) }));
    flagBox.appendChild(el("div", { class: "note", style: { marginTop: "10px" } },
      "错误类标记（INVALID_TIME / TIME_CONFLICT / SPEED_SPIKE / JUMP / GPS_DRIFT / INVALID_COG）计入质量扣分；" +
      "静止噪声与低速航向不可靠仅作提示，不扣分。"));
  } else flagBox.appendChild(empty("无异常标记"));

  /* 分布与切分 */
  const hist = q.interval_histogram || [];
  const hc = el("div", { class: "chart", style: { height: "220px" } });
  const gradeBox = el("div", {});
  const segBox = el("div", {});
  const typeBox = el("div", {});

  box.appendChild(el("div", { class: "grid g3", style: { marginBottom: "12px" } },
    card("上报间隔分布", "秒级间隔分桶", hc),
    card("轨迹质量分级", "A/B/C/D 四级", gradeBox),
    card("轨迹段与驻留", "切分原因与可用性", segBox)));

  if (hist.length) {
    chartBar(hc, hist.map(d => d.bucket), hist.map(d => d.n),
      { yName: "点数", labelFmt: v => FMT.big(v) });
  } else hc.appendChild(empty("无数据"));

  const grades = q.quality_grades || [];
  if (grades.length) {
    const gEl = el("div", { class: "chart", style: { height: "220px" } });
    gradeBox.appendChild(gEl);
    chartPie(gEl, grades.map(g => ({ name: g.quality_grade + " 级", value: g.n })));
  } else gradeBox.appendChild(empty("无数据"));

  const sg = q.segments || {};
  const sr = q.split_reasons || [];
  segBox.appendChild(el("dl", { class: "kv" },
    el("dt", {}, "轨迹段总数"), el("dd", {}, FMT.num(sg.n_segments)),
    el("dt", {}, "可用轨迹段"), el("dd", {}, FMT.num(sg.usable)),
    el("dt", {}, "平均点数"), el("dd", {}, FMT.num(sg.avg_pts, 1)),
    el("dt", {}, "最大点数"), el("dd", {}, FMT.num(sg.max_pts)),
    el("dt", {}, "中位质量分"), el("dd", {}, FMT.num(sg.med_q, 1)),
    el("dt", {}, "驻留点数"), el("dd", {}, FMT.num(q.staypoints))));
  if (sr.length) {
    segBox.appendChild(el("div", { style: { marginTop: "10px" } },
      bars(sr.map(r => ({ label: r.reason, value: r.n })), { fmt: v => FMT.num(v) })));
  }

  box.appendChild(el("div", { class: "grid g2" },
    card("船型分布", "按清洗后点数", typeBox),
    card("产物下载", "清洗结果与质量报告文件", downloadBox(dv, q))));

  const bt = q.by_ship_type || [];
  if (bt.length) {
    const bEl = el("div", { class: "chart", style: { height: "260px" } });
    typeBox.appendChild(bEl);
    chartBar(bEl, bt.map(d => d.ship_type), bt.map(d => d.n),
      { yName: "点数", rotate: true, labelFmt: v => FMT.big(v) });
  } else typeBox.appendChild(empty("无数据"));
}

function downloadBox(dv, q) {
  const names = ["quality_report.json", "staypoints.parquet", "trajectory_segments.parquet"];
  const wrap = el("div", {});
  wrap.appendChild(el("div", { class: "note" },
    "大文件按需下载，不进入接口 JSON 响应。Parquet 可直接用 DuckDB / pandas 读取。"));
  const tb = el("div", { class: "toolbar", style: { marginTop: "10px" } },
    ...names.map(n => el("button", {
      class: "btn sm",
      onclick: async () => {
        try {
          await downloadFile(
            `/api/v1/datasets/${encodeURIComponent(dv)}/download/${encodeURIComponent(n)}`, n);
        } catch (e) { toast(`下载失败：${e.message}`, true); }
      },
    }, n)));
  wrap.appendChild(tb);
  const bc = el("div", { class: "chart", style: { height: "180px", marginTop: "10px" } });
  wrap.appendChild(bc);
  const sh = q.speed_histogram || [];
  if (sh.length) {
    chartBar(bc, sh.map(d => (d.k >= 30 ? "30+" : d.k) + "kn"), sh.map(d => d.n),
      { yName: "点数", labelFmt: v => FMT.big(v) });
  } else bc.style.display = "none";
  return wrap;
}
