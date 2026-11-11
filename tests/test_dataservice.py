"""dataservice（地图/船舶查询）的单元测试。

重点覆盖 11.2 地图能力改造后的两条行为：
1. 单船轨迹必须覆盖全天并按时间等间隔抽样，不能只返回开头一段；
2. 矩形框选（bbox 过滤）必须真正下推到查询，而不是前端假过滤。
"""

from __future__ import annotations

import pandas as pd
import pytest

from aisp import dataservice
from aisp.config import settings


@pytest.fixture()
def small_dataset(tmp_path, monkeypatch):
    """在临时 artifacts 下构造一天的 clean_points parquet（单船 600 点）。"""
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)
    dv = "clean_test"
    d = tmp_path / "clean" / dv / "event_date=2025-01-01"
    d.mkdir(parents=True)
    n = 600
    base = pd.Timestamp("2025-01-01 00:00:00")
    df = pd.DataFrame({
        "event_time": [base + pd.Timedelta(seconds=i * 60) for i in range(n)],
        "mmsi": ["219000001"] * n,
        "lon": [10.0 + i * 0.001 for i in range(n)],
        "lat": [55.0 + i * 0.0005 for i in range(n)],
        "sog": [7.5] * n,
        "cog": [90.0] * n,
        "heading": [90.0] * n,
        "ship_type": ["Cargo"] * n,
        "name": ["TEST"] * n,
        "traj_id": ["t1"] * n,
        "quality_flags": [""] * n,
        "quality_grade": ["A"] * n,
        "short_segment": [False] * n,
    })
    df.to_parquet(d / "part-0.parquet", index=False)
    return dv


def test_vessel_track_samples_across_whole_day(small_dataset):
    out = dataservice.vessel_track(small_dataset, "219000001", max_points=60)
    pts = out["points"]
    assert out["total_points"] == 600
    assert out["sampled"] is True
    # 抽样后点数受控，但必须覆盖首尾（否则回放会在中途突然结束）
    assert 55 <= len(pts) <= 65
    assert pts[0]["event_time"].startswith("2025-01-01 00:00:00")
    assert pts[-1]["event_time"].startswith("2025-01-01 09:59:00")


def test_vessel_track_keeps_all_points_when_under_limit(small_dataset):
    out = dataservice.vessel_track(small_dataset, "219000001", max_points=5000)
    assert len(out["points"]) == 600
    assert out["sampled"] is False


def test_vessel_track_unknown_mmsi_is_empty(small_dataset):
    out = dataservice.vessel_track(small_dataset, "999999999")
    assert out["points"] == []
    assert out["total_points"] == 0


def test_trajectories_respect_bbox_filter(small_dataset):
    """bbox 必须下推到 SQL：框选外的小区域应查不到轨迹。"""
    inside = dataservice.trajectories(small_dataset, {"bbox": [9.9, 54.9, 10.2, 55.2]})
    assert inside["count"] == 1
    outside = dataservice.trajectories(small_dataset, {"bbox": [20.0, 60.0, 21.0, 61.0]})
    assert outside["count"] == 0


def test_points_map_aggregates_inside_bbox(small_dataset):
    out = dataservice.points_map(small_dataset, [9.9, 54.9, 10.7, 55.4], zoom=8)
    assert out["points"], "框选区域内应返回聚合格"
    # 聚合格必须落在请求的 bbox 内
    for p in out["points"]:
        assert 9.9 <= p["lon"] <= 10.7
        assert 54.9 <= p["lat"] <= 55.4
    assert sum(p["n"] for p in out["points"]) > 0
