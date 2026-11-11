"""任务调度的状态机与孤儿任务回收测试。

覆盖点：
1. 创建任务时状态为 created/queued，字段完整；
2. 服务重启后，遗留的 queued/running 任务会被回收为 failed，并写明中断原因；
3. 已到终态的任务不被回收，避免覆盖真实结果；
4. 取消请求是协作式的，标记后状态可被查询到。
"""

from __future__ import annotations

import pytest

from aisp import store, tasks


@pytest.fixture()
def meta_db():
    """准备一个干净的元数据库（conftest 已把 artifacts 指向临时目录）。"""
    store.init_meta()
    # 清空目标任务表，避免用例之间互相影响
    store.exec_sql("DELETE FROM analysis_task WHERE task_id LIKE 'task_unit_%'")
    yield
    store.exec_sql("DELETE FROM analysis_task WHERE task_id LIKE 'task_unit_%'")


def _make_task(tid: str, status: str = "queued") -> str:
    tasks.create_task("hotspot", "clean_unit", {"k": 1}, {}, "tester", task_id=tid)
    if status != "created":
        tasks.update_task(tid, status=status)
    return tid


def test_create_task_initial_state(meta_db):
    tid = _make_task("task_unit_a")
    t = tasks.get_task(tid)
    assert t is not None
    assert t["status"] == "queued"
    assert t["algorithm"] == "hotspot"
    assert t["dataset_version"] == "clean_unit"
    assert t["queue"] == "spatial"


def test_reclaim_orphans_marks_running_and_queued_as_failed(meta_db):
    a = _make_task("task_unit_run", "running")
    b = _make_task("task_unit_queued", "queued")
    reclaimed = tasks.reclaim_orphans()
    assert set([a, b]) <= set(reclaimed)

    for tid in (a, b):
        t = tasks.get_task(tid)
        assert t["status"] == "failed"
        assert t["error_code"] == "INTERRUPTED"
        assert "重启" in (t["error_message"] or "")


def test_reclaim_orphans_keeps_finished_tasks(meta_db):
    done = _make_task("task_unit_done", "succeeded")
    tasks.update_task(done, progress=1.0, current_stage="完成")
    tasks.reclaim_orphans()
    t = tasks.get_task(done)
    assert t["status"] == "succeeded"
    assert t["current_stage"] == "完成"


def test_reclaim_orphans_is_idempotent(meta_db):
    tid = _make_task("task_unit_idem", "running")
    tasks.reclaim_orphans()
    # 第二次调用不应再返回它（已经是终态）
    assert tid not in tasks.reclaim_orphans()


def test_cancel_queued_task_sets_cancelled(meta_db):
    """排队中的任务可以被直接取消；running 但无活动执行现场的视为孤儿任务。"""
    tid = _make_task("task_unit_cancel", "queued")
    assert tasks.request_cancel(tid) is True
    t = tasks.get_task(tid)
    assert t["status"] == "cancelled"
    assert t["current_stage"] == "已取消"


def test_cancel_returns_false_for_finished_task(meta_db):
    tid = _make_task("task_unit_cancel2", "succeeded")
    assert tasks.request_cancel(tid) is False
    assert tasks.get_task(tid)["status"] == "succeeded"
