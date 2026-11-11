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


# ------------------------------------------------------------------ 11.2 空间筛选

def test_trajectories_respect_polygon_filter(small_dataset):
    """多边形（射线法）必须下推到 SQL：只包住前 1/4 航迹的多边形，
    其轨迹条数/点数应明显小于全量，而完全在外的多边形应为 0。"""
    # 600 个点沿 lon 从 10.0 递增到 10.599，lat 从 55.0 到 55.2995
    inside_poly = [[9.99, 54.99], [10.16, 54.99], [10.16, 55.09], [9.99, 55.09]]
    out = dataservice.trajectories(small_dataset, {"polygon": inside_poly})
    assert out["count"] == 1
    n_inside = out["features"][0]["properties"]["points"]
    assert 0 < n_inside < 600, "多边形内的点数应少于全量"

    outside_poly = [[20.0, 60.0], [21.0, 60.0], [21.0, 61.0], [20.0, 61.0]]
    assert dataservice.trajectories(small_dataset, {"polygon": outside_poly})["count"] == 0


def test_circle_filter_is_km_based(small_dataset):
    """圆形半径单位为 km：20 km 圈住航迹前半段，1 km 只圈住起点附近。"""
    narrow = dataservice.trajectories(small_dataset, {"circle": [10.0, 55.0, 1.0]})
    wide = dataservice.trajectories(small_dataset, {"circle": [10.0, 55.0, 20.0]})
    assert narrow["count"] == 1 and wide["count"] == 1
    n_narrow = narrow["features"][0]["properties"]["points"]
    n_wide = wide["features"][0]["properties"]["points"]
    assert n_narrow < n_wide, "半径越大应包含越多点"
    assert n_wide < 600
    # 跑出圈外的圆形应为空
    assert dataservice.trajectories(small_dataset, {"circle": [30.0, 70.0, 5.0]})["count"] == 0


def test_points_map_supports_polygon_without_bbox(small_dataset):
    """只给多边形（不给视口 bbox）也要能聚合，便于前端纯框选模式。"""
    poly = [[9.99, 54.99], [10.20, 54.99], [10.20, 55.10], [9.99, 55.10]]
    out = dataservice.points_map(small_dataset, None, zoom=8, polygon=poly)
    assert out["points"]
    total = sum(p["n"] for p in out["points"])
    assert 0 < total < 600
    # 注意：聚合格坐标是「网格中心」，可能落在多边形外半格，
    # 所以只验证大致范围，不要求严格在多边形内。
    for p in out["points"]:
        assert 9.95 <= p["lon"] <= 10.25
        assert 54.95 <= p["lat"] <= 55.15
