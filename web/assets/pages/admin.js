/* 系统管理：数据字典、默认算法参数、队列配置与审计日志 */
import { state } from "../state.js";

export async function render(view) {
  const top = el("div", {});
  const mid = el("div", {});
  const bot = el("div", {});
  view.appendChild(top);
  view.appendChild(mid);
  view.appendChild(bot);

  top.appendChild(loading("正在读取系统元数据…"));

  let meta = null, cfg = null, me = null;
  try {
    meta = await api("/admin/meta");
    cfg = await api("/admin/config");
    me = await api("/whoami");
  } catch (e) {
    top.innerHTML = "";
    top.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
    return;
  }
  top.innerHTML = "";

  /* ---------------- 概览 ---------------- */
  const s = cfg.settings || {};
  const ident = (me || {}).identity || {};
  top.appendChild(el("div", { class: "grid g5", style: { marginBottom: "14px" } },
    kpi("服务地址", `${s.host}:${s.port}`, "FastAPI + Uvicorn"),
    kpi("当前角色", ident.role_cn || "—",
      me && me.auth_enabled ? `已启用鉴权 · ${ident.subject}` : "未启用鉴权（本机管理员）"),
    kpi("DuckDB 内存上限", s.duckdb_memory_limit || "—", "可经 .env 覆盖"),
    kpi("数据目录", shortPath(s.data_dir), "原始 CSV 存放位置"),
    kpi("产物目录", shortPath(s.artifacts_dir), "清洗结果 / 分析产物 / 元数据库")));

  /* ---------------- 数据字典 ---------------- */
  const dict = (meta.data_dictionary || {}).fields || [];
  const dictBox = el("div", {});
  const mapping = (meta.data_dictionary || {}).source_mapping || {};
  top.appendChild(el("div", { class: "grid g23", style: { marginBottom: "14px", alignItems: "start" } },
    card("数据字典", `共 ${dict.length} 个标准字段`, dictBox),
    card("源字段映射", "原始 CSV 列名 → 标准字段", el("div", {},
      el("div", { class: "note", style: { marginBottom: "8px" } },
        "映射用于把不同来源的 AIS 数据统一到标准 Schema；未识别的列进入 extra_* 字段。"),
      table([
        { label: "源字段", key: "src" },
        { label: "标准字段", render: r => el("span", { class: "tag ok" }, r.std) },
      ], Object.entries(mapping).map(([src, std]) => ({ src, std })), { tall: true })))));

  if (dict.length) {
    dictBox.appendChild(table([
      { label: "字段", render: r => el("b", {}, r.field) },
      { label: "名称", key: "name" },
      { label: "类型", render: r => el("span", { class: "tag" }, r.dtype || "—") },
      { label: "单位", key: "unit" },
      { label: "说明", render: r => el("span", { style: { color: "var(--txt2)" } }, r.desc || "") },
    ], dict, { tall: true }));
  } else dictBox.appendChild(empty("无字典数据"));

  /* ---------------- 默认参数 ---------------- */
  const defaults = cfg.defaults || {};
  const algoNames = {
    ingest: "数据导入", clean: "数据治理", hotspot: "热点区域分析",
    profile: "船舶画像", correlate: "属性关联", formation: "船舶编群",
    frequent_route: "频繁轨迹", predict: "航迹预测",
  };
  const grid = el("div", { class: "grid g3" });
  for (const [k, p] of Object.entries(defaults)) {
    const rows = Object.entries(p || {}).map(([kk, vv]) => ({
      key: kk,
      val: Array.isArray(vv) ? vv.join(", ") : String(vv),
    }));
    grid.appendChild(el("div", { class: "card" },
      el("h3", {}, el("span", {}, algoNames[k] || k), el("span", { class: "sub" }, `${rows.length} 项`)),
      table([
        { label: "参数", key: "key" },
        { label: "默认值", render: r => el("code", { style: { fontSize: "11px", color: "#a8d8fb" } }, r.val) },
      ], rows)));
  }
  mid.appendChild(card("默认算法参数（附录 B）", "任务未显式传参时使用；可在前端各分析页覆盖", grid));

  /* ---------------- 队列 ---------------- */
  const limits = meta.queue_limits || {};
  const queues = meta.task_queues || {};
  mid.appendChild(card("任务队列与并发", "每个队列独立并发上限，避免长任务互相阻塞",
    table([
      { label: "队列", key: "q" },
      { label: "并发上限", num: true, render: r => FMT.num(r.limit) },
      { label: "包含算法", render: r => (r.algs || []).join(", ") || "—" },
    ], Object.keys(queues).map(q => ({ q, algs: queues[q], limit: limits[q] ?? "—" })))));

  /* ---------------- 审计日志 ---------------- */
  const auditBox = el("div", {});
  const refreshBtn = el("button", { class: "btn sm" }, "刷新日志");
  const auditCard = card("审计日志", "任务创建 / 完成 / 失败 / 取消等关键操作记录", auditBox);
  auditCard.querySelector("h3").appendChild(refreshBtn);
  bot.appendChild(auditCard);

  const loadAudit = async () => {
    auditBox.innerHTML = "";
    auditBox.appendChild(loading("读取中…"));
    try {
      const rows = await api("/admin/audit?limit=200");
      auditBox.innerHTML = "";
      if (!rows.length) { auditBox.appendChild(empty("暂无审计记录")); return; }
      auditBox.appendChild(table([
        { label: "时间", render: r => FMT.ts(r.event_time) },
        { label: "事件", render: r => {
            // 后端字段为 action，兼容旧的 event 字段
            const act = r.action || r.event || "";
            const map = {
              task_create: "创建任务", task_done: "任务完成", task_fail: "任务失败",
              task_cancel: "任务取消", task_cancelled: "任务取消",
              import: "数据导入", dataset_import: "数据导入",
              clean: "数据治理", preflight: "文件预检",
              config_view: "查看配置",
            };
            const c = act.includes("fail") ? "bad" : act.includes("done") ? "ok" : act.includes("cancel") ? "vio" : "";
            return el("span", { class: "tag " + c }, map[act] || act || "—");
          } },
        { label: "对象", render: r => String(r.resource || r.target || "—") },
        { label: "说明", render: r => String(r.detail || "").slice(0, 110) },
        { label: "结果", render: r => el("span", { class: "tag " + (r.result === "failed" ? "bad" : r.result === "ok" ? "ok" : "") }, r.result || "—") },
        { label: "操作者", render: r => String(r.user_id || r.actor || "—") },
      ], rows, { tall: true }));
    } catch (e) {
      auditBox.innerHTML = "";
      auditBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
    }
  };
  refreshBtn.addEventListener("click", loadAudit);
  await loadAudit();

  /* ---------------- 备份与恢复（技术方案 19.1 / 20.1） ---------------- */
  await renderBackup(view);
}

async function renderBackup(view) {
  const host = el("div", {});
  view.appendChild(host);

  const includeClean = el("input", { type: "checkbox" });
  const mkBtn = el("button", { class: "btn primary sm" }, "生成备份");
  const hint = el("div", { class: "note" },
    "默认仅备份元数据库（批次 / 任务 / 结果索引 / 审计，体积通常 < 1 MB）；"
    + "勾选「含清洗明细」会一并打包 CLEAN Parquet，体积约等于原始清洗结果。");
  const listBox = el("div", {});

  host.appendChild(card("备份与恢复", "元数据库 + 可选 CLEAN 明细，zip 归档可离线保存",
    el("div", { class: "toolbar" },
      el("div", { class: "field" }, el("label", {}, "含清洗明细"), includeClean),
      mkBtn),
    hint,
    listBox));

  const loadList = async () => {
    listBox.innerHTML = "";
    listBox.appendChild(loading("读取备份清单…"));
    let rows = [];
    try {
      rows = await api("/admin/backups");
    } catch (e) {
      listBox.innerHTML = "";
      listBox.appendChild(el("div", { class: "note", style: { color: "#fca5a5" } }, e.message));
      return;
    }
    listBox.innerHTML = "";
    if (!rows.length) { listBox.appendChild(empty("暂无备份包，点击「生成备份」创建")); return; }
    listBox.appendChild(table([
      { label: "生成时间", render: r => fmtStamp((r.manifest || {}).created_at) || r.created_at },
      { label: "文件名", render: r => String(r.path).replace(/^.*[\\/]/, "") },
      { label: "体积", num: true, render: r => FMT.bytes(r.bytes) },
      {
        label: "含明细", render: r => el("span", { class: "tag " + ((r.manifest || {}).include_clean ? "warn" : "") },
          (r.manifest || {}).include_clean ? "是" : "否"),
      },
      {
        label: "数据集", render: r => ((r.manifest || {}).datasets || [])
          .map(d => d.dataset_version).join(", ") || "—",
      },
      {
        label: "操作", render: r => el("span", {},
          el("button", {
            class: "btn sm", style: { marginRight: "6px" },
            onclick: async (ev) => {
              const name = String(r.path).replace(/^.*[\\/]/, "");
              ev.target.disabled = true;
              try {
                await downloadFile(`/api/v1/admin/backups/${encodeURIComponent(name)}`, name);
              } catch (e) { toast(`下载失败：${e.message}`, true); }
              finally { ev.target.disabled = false; }
            },
          }, "下载"),
          el("button", {
            class: "btn sm",
            onclick: async (ev) => {
              const name = String(r.path).replace(/^.*[\\/]/, "");
              if (!confirm(`将用备份 ${name} 覆盖当前元数据库（原库会先另存为 pre_restore）\n继续？`)) return;
              ev.target.disabled = true;
              try {
                await api("/admin/restore", {
                  method: "POST",
                  body: JSON.stringify({ name, restore_clean: false, overwrite: false }),
                });
                toast("元数据库已从备份恢复");
                await loadList();
              } catch (e) { toast(e.message, true); }
              finally { ev.target.disabled = false; }
            },
          }, "恢复")),
      },
    ], rows, { tall: true }));
  };

  mkBtn.addEventListener("click", async () => {
    mkBtn.disabled = true;
    mkBtn.textContent = includeClean.checked ? "打包中…" : "生成中…";
    try {
      const m = await api("/admin/backup", {
        method: "POST",
        body: JSON.stringify({ include_clean: includeClean.checked }),
      });
      toast(`备份完成：${FMT.bytes(m.zip_bytes)} · ${(m.files || []).length} 个文件`);
      await loadList();
    } catch (e) { toast(e.message, true); }
    finally {
      mkBtn.disabled = false;
      mkBtn.textContent = "生成备份";
    }
  });

  await loadList();
}

function fmtStamp(v) {
  // 备份目录名用的是 20261111_214942 这种紧凑时间戳，表格里展示成可读时间
  const s = String(v || "");
  if (!/^\d{8}_\d{6}$/.test(s)) return s;
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} `
    + `${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}`;
}

function shortPath(p) {
  if (!p) return "—";
  const parts = String(p).replace(/\\/g, "/").split("/");
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : p;
}
