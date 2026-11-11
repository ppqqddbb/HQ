"""模型卡与上线监控的单元测试（技术方案 10.10）。

覆盖点：
1. PSI 计算正确：同分布≈0、明显右移必然很大、样本不足返回 None；
2. 漂移结论分级：stable / warn / alert 到 advice 动作的映射；
3. 模型卡字段完整性：训练数据范围、指标、特征、依赖、失效场景齐备；
4. 监控点与回填误差趋势：记录、覆盖率、恶化判定；
5. API 端点的 404 语义（模型不存在）。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from aisp import modelmon
from aisp.config import settings


@pytest.fixture()
def models_root(tmp_path, monkeypatch):
    # models_root 是 Settings 的只读 property，需在类级别替换
    root = tmp_path / "models"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(type(settings), "models_root", property(lambda self: root))
    return root


# ------------------------------------------------------------------ PSI

def test_psi_is_near_zero_for_same_distribution():
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 5000)
    b = rng.normal(0, 1, 5000)
    v = modelmon.psi(a, b)
    assert v is not None
    assert v < 0.05, f"同分布 PSI 应接近 0，实际 {v}"


def test_psi_detects_large_shift():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 1, 5000)
    b = rng.normal(3, 1, 5000)          # 整体右移 3 个标准差
    v = modelmon.psi(a, b)
    assert v is not None and v >= modelmon.PSI_ALERT


def test_psi_returns_none_on_small_sample():
    assert modelmon.psi([1, 2, 3], np.linspace(0, 1, 500)) is None


def test_psi_level_thresholds():
    assert modelmon.psi_level(None) == "insufficient"
    assert modelmon.psi_level(0.01) == "stable"
    assert modelmon.psi_level(modelmon.PSI_WARN) == "warn"
    assert modelmon.psi_level(modelmon.PSI_ALERT) == "alert"


def test_distribution_shift_reports_missing_rate():
    rng = np.random.default_rng(2)
    ref = pd.DataFrame({"sog": rng.normal(8, 2, 2000), "cog": rng.normal(180, 30, 2000)})
    cur = pd.DataFrame({"sog": rng.normal(8.1, 2, 2000), "cog": rng.normal(181, 31, 2000)})
    cur.loc[:400, "sog"] = np.nan          # 制造 20% 缺失
    rows = modelmon.distribution_shift(ref, cur, ["sog", "cog"])
    assert len(rows) == 2
    sog = next(r for r in rows if r["feature"] == "sog")
    assert sog["missing_current"] is not None and sog["missing_current"] > 0.1
    assert sog["missing_reference"] is not None and sog["missing_reference"] < 0.01


def test_drift_verdict_actions():
    stable = [{"feature": "sog", "level": "stable"}]
    warn = [{"feature": "sog", "level": "warn"}]
    alert = [{"feature": "sog", "level": "alert"}]
    assert modelmon.drift_verdict(stable)["action"] == "ok"
    assert modelmon.drift_verdict(warn)["action"] == "watch"
    assert modelmon.drift_verdict(alert)["action"] == "retrain"
    assert modelmon.drift_verdict([{"feature": "sog", "level": "insufficient"}])["action"] \
        == "insufficient"


# ------------------------------------------------------------------ 模型卡

def _fake_summary(task_id: str = "task_x") -> dict:
    return {
        "algorithm": "predict",
        "dataset_version": "clean_test",
        "config": {"step_minutes": 5, "input_minutes": 60, "horizon_minutes": 30,
                   "n_trajectories": 800, "n_windows": 30652, "n_train": 21456,
                   "n_test": 9196, "min_quality_score": 60.0},
        "metrics": {
            "gru": {"ADE_m": 531.69, "trimmed_ADE_m": 297.34, "median_m": 168.81,
                    "FDE_m": 650.73, "P90_m": 1046.11, "n_test": 9196},
            "cv": {"ADE_m": 2556.99, "trimmed_ADE_m": 1586.12},
        },
        "verdict": {"baseline": "cv", "gru_ade": 297.34, "baseline_ade": 1586.12,
                    "improvement_pct": 81.3, "can_go_online": True},
        "gru": {"epochs": 30, "hidden": 64, "n_params": 44000,
                "initial_loss": 1.2, "final_loss": 0.3,
                "artifact": f"gru_{task_id}.pt"},
        "data_range": {"time_range": ["2025-01-01 00:00:00", "2025-01-01 23:59:00"],
                       "bbox": [8.0, 54.5, 13.0, 58.0]},
        "stratified": {"gru": {"by_speed": {"静止(<0.5kn)": {"n": 7374, "trimmed_ADE_m": 3.25}}}},
        "split": {"method": "时间外推"},
    }


def test_model_card_has_required_fields(models_root):
    card = modelmon.build_model_card("task_x", _fake_summary("task_x"))
    assert card["task_id"] == "task_x"
    assert card["training_data"]["dataset_version"] == "clean_test"
    assert card["training_data"]["time_range"][0].startswith("2025-01-01")
    assert card["evaluation"]["gru"]["trimmed_ADE_m"] == 297.34
    assert card["evaluation"]["baseline"]["model"] == "cv"
    assert card["features"], "特征清单不能为空"
    assert card["dependencies"], "依赖版本不能为空"
    assert card["failure_modes"], "失效场景不能为空"
    assert card["monitoring"]["watch_features"]


def test_model_card_written_to_disk(models_root):
    modelmon.build_model_card("task_y", _fake_summary("task_y"))
    p = models_root / "gru_task_y" / "model_card.json"
    assert p.is_file(), "模型卡应落在 artifacts/models/<model_id>/model_card.json"
    card = json.loads(p.read_text(encoding="utf-8"))
    assert card["model_id"] == "gru_task_y.pt"


# ------------------------------------------------------------------ 监控与趋势

def test_monitor_point_and_error_trend(models_root):
    modelmon.build_model_card("task_z", _fake_summary("task_z"))
    mid = "gru_task_z"
    p1 = modelmon.record_monitor_point(mid, n_predictions=1000, n_backfilled=980,
                                      latency_ms=12.5, backfill_ade_m=300.0)
    assert p1["coverage"] == pytest.approx(0.98)
    p2 = modelmon.record_monitor_point(mid, n_predictions=1000, n_backfilled=900,
                                       latency_ms=15.0, backfill_ade_m=420.0)
    assert p2["coverage"] == pytest.approx(0.9)

    log = modelmon.load_monitor_log(mid)
    assert len(log["points"]) == 2
    trend = modelmon.error_trend(log, 297.34)
    assert trend["available"] is True
    assert trend["worsen_pct"] == pytest.approx(41.25, abs=0.1)
    assert trend["worsened"] is True


def test_error_trend_without_points(models_root):
    t = modelmon.error_trend({"points": []})
    assert t["available"] is False


def test_monitor_point_accepts_backfill_ts_and_sorts(models_root):
    """补录历史监控点：时间可指定，且落盘后按时间排序，趋势图顺序才对。"""
    modelmon.build_model_card("task_ts", _fake_summary("task_ts"))
    mid = "gru_task_ts"
    modelmon.record_monitor_point(mid, n_predictions=100, n_backfilled=100,
                                  backfill_ade_m=420.0, ts="2026-11-01")
    modelmon.record_monitor_point(mid, n_predictions=100, n_backfilled=100,
                                  backfill_ade_m=310.0, ts="2026-11-02 08:00")
    pts = modelmon.load_monitor_log(mid)["points"]
    assert [p["ts"] for p in pts] == ["2026-11-01 00:00:00", "2026-11-02 08:00:00"]
    # 后一条误差更小，趋势应判定为“改善”而不是恶化
    trend = modelmon.error_trend({"points": pts})
    assert trend["baseline_ade_m"] == pytest.approx(420.0)
    assert trend["latest_ade_m"] == pytest.approx(310.0)
    assert trend["worsen_pct"] < 0
    assert trend["worsened"] is False


def test_monitor_point_rejects_bad_ts(models_root):
    modelmon.build_model_card("task_badts", _fake_summary("task_badts"))
    with pytest.raises(ValueError):
        modelmon.record_monitor_point("gru_task_badts", n_predictions=1, ts="昨天")


def test_drift_verdict_persisted_in_detail(models_root):
    """漂移结论要落盘：否则页面刷新后只剩逐特征表，无法判断“自比”等上下文。"""
    modelmon.build_model_card("task_dv", _fake_summary("task_dv"))
    mid = "gru_task_dv"
    rows = [{"feature": "sog", "label": "对地航速 SOG (kn)", "psi": 0.0,
             "level": "stable", "n_reference": 100, "n_current": 100,
             "missing_reference": 0.0, "missing_current": 0.0}]
    verdict = {"action": "ok", "message": "自比", "n_alert": 0, "n_warn": 0,
               "self_compare": True}
    modelmon.record_monitor_point(mid, drift=rows, verdict=verdict, note="手动漂移检测")
    d = modelmon.model_detail(mid)
    assert d["latest_drift"] and d["latest_drift"][0]["feature"] == "sog"
    assert d["latest_drift_verdict"]["self_compare"] is True
    assert d["latest_drift_verdict"]["action"] == "ok"


def test_list_models_and_detail(models_root):
    modelmon.build_model_card("task_a", _fake_summary("task_a"))
    rows = modelmon.list_models()
    assert len(rows) == 1
    assert rows[0]["model_id"] == "gru_task_a.pt"

    d = modelmon.model_detail("gru_task_a")
    assert d is not None
    assert d["card"]["task_id"] == "task_a"
    assert modelmon.model_detail("no_such_model") is None


def test_monitor_log_is_capped(models_root):
    modelmon.build_model_card("task_cap", _fake_summary("task_cap"))
    mid = "gru_task_cap"
    for i in range(205):
        modelmon.record_monitor_point(mid, n_predictions=10, n_backfilled=10, latency_ms=float(i))
    log = modelmon.load_monitor_log(mid)
    assert len(log["points"]) == 200, "监控日志应只保留最近 200 条，避免无限增长"
