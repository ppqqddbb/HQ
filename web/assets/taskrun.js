/* 分析任务运行器：提交 → 轮询状态 → 取回结果摘要
 * 约定：所有耗时分析都走异步任务（POST /analysis/tasks），前端只轮询任务状态；
 * 结果摘要从 /tasks/{id}/summary 获取，大文件走下载接口。
 */
import { state } from "./state.js";

export async function listTasks(limit = 80) {
  return api(`/tasks?limit=${limit}`);
}

/** 查找该算法在当前数据集下最近一次成功的任务（用于首屏秒开） */
export async function latestSucceeded(algorithm, datasetVersion) {
  try {
    const ts = await listTasks(80);
    return ts.find(t => t.algorithm === algorithm &&
      t.status === "succeeded" &&
      (!datasetVersion || t.dataset_version === datasetVersion)) || null;
  } catch (e) {
    return null;
  }
}

export async function getTask(taskId) {
  return api(`/tasks/${taskId}`);
}

export async function getSummary(taskId) {
  return api(`/tasks/${taskId}/summary`);
}

export async function cancelTask(taskId) {
  return api(`/tasks/${taskId}/cancel`, { method: "POST" });
}

/** 提交并等待任务结束；返回 {taskId, task, summary} */
export async function runTask(algorithm, parameters, filters, handlers = {}) {
  const body = {
    algorithm,
    dataset_version: state.dataset,
    parameters: parameters || {},
    filters: filters || {},
    created_by: "web",
  };
  const res = await api("/analysis/tasks", { method: "POST", body: JSON.stringify(body) });
  const taskId = res.task_id;
  if (handlers.onStart) handlers.onStart(taskId);
  let last = null;
  const interval = handlers.interval || 1200;
  const timeoutMs = handlers.timeoutMs || 45 * 60 * 1000;
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await new Promise(r => setTimeout(r, interval));
    const t = await getTask(taskId);
    last = t;
    if (handlers.onProgress) handlers.onProgress(t);
    if (t.status === "succeeded") break;
    if (t.status === "failed" || t.status === "cancelled") {
      const raw = String(t.error_message || "").split("\n")[0] || `任务${t.status}`;
      const err = new Error(raw);
      err.task = t;
      throw err;
    }
  }
  if (!last || last.status !== "succeeded") {
    const err = new Error("任务超时或状态异常");
    err.task = last;
    throw err;
  }
  return { taskId, task: last, summary: await getSummary(taskId) };
}
