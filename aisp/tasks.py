"""任务调度：异步任务、状态机、队列并发控制与可观测性。

状态机：created → queued → running → succeeded / failed / cancelled
- 每个队列有独立并发上限（参考技术方案 15.1）；
- 取消是协作式的：worker 在进度回调中检查取消标志，界面显示“取消请求中”；
- 进度与当前阶段写入 analysis_task，供前端轮询或 SSE 推送；
- 所有异常记录 error_code / error_message，并写入审计日志。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from . import store
from .config import TASK_QUEUES, default_params

log = logging.getLogger(__name__)

QUEUE_LIMITS = {"ingest": 1, "spatial": 1, "sequence": 1, "ml-train": 1, "ml-infer": 2, "export": 2}

_ACTIVE: Dict[str, threading.Event] = {}
_LOCK = threading.RLock()
_EXECUTORS: Dict[str, ThreadPoolExecutor] = {}


def _queue_of(algorithm: str) -> str:
    for q, algs in TASK_QUEUES.items():
        if algorithm in algs:
            return q
    return "spatial"


def _executor(queue: str) -> ThreadPoolExecutor:
    with _LOCK:
        if queue not in _EXECUTORS:
            _EXECUTORS[queue] = ThreadPoolExecutor(
                max_workers=QUEUE_LIMITS.get(queue, 2), thread_name_prefix=f"aisp-{queue}"
            )
        return _EXECUTORS[queue]


# --------------------------------------------------------------------------- 状态读写

def create_task(
    algorithm: str,
    dataset_version: str,
    parameters: Optional[Dict[str, Any]] = None,
    filters: Optional[Dict[str, Any]] = None,
    created_by: str = "system",
) -> str:
    store.init_meta()
    task_id = store.new_id(f"task_{algorithm}")
    queue = _queue_of(algorithm)
    params = default_params(algorithm)
    params.update(parameters or {})
    store.exec_sql(
        "INSERT INTO analysis_task VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            task_id, algorithm, dataset_version,
            json.dumps(params, ensure_ascii=False, default=str),
            json.dumps(filters or {}, ensure_ascii=False, default=str),
            "queued", 0.0, "排队中", queue, created_by,
            store._now(), None, None, None, None, None, False, None,
        ],
    )
    store.audit("task_create", f"task:{task_id}", f"{algorithm}@{dataset_version}", created_by)
    return task_id


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    store.exec_sql(f"UPDATE analysis_task SET {cols} WHERE task_id=?", [*fields.values(), task_id])


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    df = store.q("SELECT * FROM analysis_task WHERE task_id=?", [task_id])
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    for k in ("config_json", "filters_json", "metrics_json"):
        if row.get(k):
            try:
                row[k.replace("_json", "")] = json.loads(row[k])
            except Exception:
                pass
    out = {
        "task_id": row["task_id"],
        "algorithm": row["algorithm"],
        "dataset_version": row["dataset_version"],
        "status": row["status"],
        "progress": float(row["progress"] or 0.0),
        "current_stage": row["current_stage"],
        "queue": row["queue"],
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "result_uri": row["result_uri"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "cancel_request": bool(row["cancel_request"]),
        "params": row.get("config", {}),
        "filters": row.get("filters", {}),
        "metrics": row.get("metrics", {}),
    }
    return out


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def list_tasks(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT task_id, algorithm, dataset_version, status, progress, current_stage, queue, created_at, finished_at FROM analysis_task"
    params: List[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    df = store.q(sql, params)
    for c in ("created_at", "finished_at"):
        df[c] = df[c].map(_iso)
    return df.to_dict(orient="records")


def request_cancel(task_id: str) -> bool:
    with _LOCK:
        ev = _ACTIVE.get(task_id)
    if ev is not None:
        ev.set()
        update_task(task_id, cancel_request=True, current_stage="取消请求中…")
        store.audit("task_cancel", f"task:{task_id}", "协作式取消")
        return True
    t = get_task(task_id)
    if t and t["status"] in ("queued", "created"):
        update_task(task_id, status="cancelled", current_stage="已取消", finished_at=store._now())
        return True
    return False


def result_summary(task_id: str) -> Optional[Dict[str, Any]]:
    from .algorithms.common import write_result  # noqa: F401  (保持依赖方向一致)
    from .config import settings

    p = settings.results_root / task_id / "summary.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 执行

RUNNERS: Dict[str, Callable[..., Dict[str, Any]]] = {}


def _load_runners() -> None:
    global RUNNERS
    if RUNNERS:
        return
    from .algorithms import formation, frequent_route, hotspot, predict, profile

    RUNNERS = {
        "hdbscan_hotspot": hotspot.run,
        "vessel_profile": profile.vessel_profile,
        "correlate": profile.correlate,
        "formation_dtw": formation.run,
        "frequent_route": frequent_route.run,
        "predict": predict.run,
    }


def submit(task_id: str, runner: Optional[Callable[..., Dict[str, Any]]] = None,
           **kwargs: Any) -> None:
    """把任务提交到对应队列执行。"""
    task = get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在：{task_id}")
    queue = task["queue"]
    _executor(queue).submit(_run, task_id, runner, kwargs)


def run_sync(task_id: str, runner: Optional[Callable[..., Dict[str, Any]]] = None,
             **kwargs: Any) -> Dict[str, Any]:
    return _run(task_id, runner, kwargs)


def _run(task_id: str, runner: Optional[Callable[..., Dict[str, Any]]], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    _load_runners()
    task = get_task(task_id)
    if task is None:
        return {"error": "任务不存在"}
    if task["status"] == "cancelled":
        return {"error": "任务已取消"}

    fn = runner or RUNNERS.get(task["algorithm"])
    if fn is None:
        update_task(task_id, status="failed", error_code="NO_RUNNER",
                    error_message=f"未注册算法：{task['algorithm']}", finished_at=store._now())
        return {"error": "未注册算法"}

    ev = threading.Event()
    with _LOCK:
        _ACTIVE[task_id] = ev
    update_task(task_id, status="running", started_at=store._now(), progress=0.01,
                current_stage="开始执行", error_code=None, error_message=None)

    def progress(pct: float, stage: str) -> None:
        if ev.is_set():
            raise TaskCancelled("任务已被取消")
        update_task(task_id, progress=float(min(0.99, max(0.0, pct))), current_stage=stage)

    try:
        result = fn(
            dataset_version=task["dataset_version"],
            task_id=task_id,
            params=task["params"] or {},
            filters=task["filters"] or {},
            progress=progress,
            **kwargs,
        )
        if isinstance(result, dict) and result.get("error"):
            update_task(task_id, status="failed", progress=1.0, current_stage="失败",
                        error_code="ALGO_ERROR", error_message=str(result["error"])[:2000],
                        finished_at=store._now())
            store.audit("task_fail", f"task:{task_id}", str(result["error"])[:500], result="failed")
            return result

        from .config import settings

        update_task(
            task_id, status="succeeded", progress=1.0, current_stage="完成",
            finished_at=store._now(), result_uri=str(settings.results_root / task_id),
            metrics_json=json.dumps(_metrics_of(result), ensure_ascii=False, default=str),
        )
        store.exec_sql(
            "INSERT OR REPLACE INTO analysis_result VALUES (?,?,?,?,?,?)",
            [store.new_id("res"), task_id, task["algorithm"],
             str(settings.results_root / task_id / "summary.json"),
             json.dumps(_metrics_of(result), ensure_ascii=False, default=str), store._now()],
        )
        store.audit("task_done", f"task:{task_id}", task["algorithm"])
        return result
    except TaskCancelled:
        update_task(task_id, status="cancelled", current_stage="已取消", finished_at=store._now())
        store.audit("task_cancel", f"task:{task_id}", "已取消", result="cancelled")
        return {"error": "任务已取消"}
    except Exception as exc:  # pragma: no cover
        log.exception("任务失败 %s", task_id)
        update_task(task_id, status="failed", current_stage="失败", error_code=type(exc).__name__,
                    error_message=f"{exc}\n{traceback.format_exc()[-1500:]}", finished_at=store._now())
        store.audit("task_fail", f"task:{task_id}", str(exc)[:500], result="failed")
        return {"error": str(exc)}
    finally:
        with _LOCK:
            _ACTIVE.pop(task_id, None)


class TaskCancelled(Exception):
    pass


def _metrics_of(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    keep = {}
    for k in ("n_clusters", "noise_ratio", "n_groups", "n_routes", "n_edges", "n_cells",
              "n_samples", "n_sequences", "row_count", "mmsi_count", "overall_score",
              "metrics", "verdict", "n_units", "staypoints"):
        if k in result:
            keep[k] = result[k]
    return keep


def wait(task_id: str, timeout: float = 600.0, interval: float = 0.5) -> Dict[str, Any]:
    """阻塞等待任务终态（供脚本与测试使用）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = get_task(task_id)
        if t and t["status"] in ("succeeded", "failed", "cancelled"):
            return t
        time.sleep(interval)
    return get_task(task_id) or {}
