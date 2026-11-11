"""报告导出模块的单元测试。

覆盖目标（对应技术方案 11.4 与 20.2）：
1. HTML 报告必须自包含（不引用外部 CDN / 脚本 / 字体）；
2. 报告必须包含可追溯信息：任务 ID、数据版本、代码版本、参数、口径说明；
3. HTML 转义必须生效，避免结果字段里的 `<` 破坏页面结构；
4. 大点集必须降采样，避免报告体积失控；
5. 未知任务/未完成任务必须以 ValueError 拒绝，而不是生成空报告。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aisp import report as report_mod


@pytest.fixture()
def fake_task(tmp_path, monkeypatch):
    """构造一个已成功的 predict 类任务，summary 写入临时结果目录。"""
    from aisp.config import settings

    monkeypatch.setattr(type(settings), "results_root",
                        property(lambda self: tmp_path / "results"))
    task_id = "task_test_report"
    summary = {
        "algorithm": "hdbscan_hotspot",
        "dataset_version": "clean_test",
        "filters": {},
        "params": {"min_cluster_size": 150, "min_samples": 20},
        "n_clusters": 2,
        "n_significant_clusters": 1,
        "noise_ratio": 0.207,
        "extent": [8.0, 54.0, 12.0, 58.0],
        "extent_robust": [8.5, 54.5, 11.5, 57.5],
        "sampling": {"time_slot_minutes": 5, "method": "每 MMSI 每时间槽保留 1 点",
                     "n_before_sampling": 100000, "n_sampled": 50000, "n_used": 20000},
        "clusters": [
            {"cluster_id": 1, "rank": 1, "point_count": 1234, "distinct_mmsi": 56,
             "center_lon": 10.5, "center_lat": 55.5, "area_km2": 12.5,
             "median_sog": 9.1, "dwell_ratio": 0.12, "hotspot_kind": "交通流热点",
             "top_ship_types": {"Cargo": 100, "Tanker": 60}},
            {"cluster_id": 2, "rank": 2, "point_count": 800, "distinct_mmsi": 20,
             "center_lon": 11.0, "center_lat": 56.0, "area_km2": 3.0,
             "median_sog": 0.2, "dwell_ratio": 0.95, "hotspot_kind": "停留/锚泊热点",
             "top_ship_types": {"Tug": 10}},
        ],
        "sensitivity": {
            "base": {"min_cluster_size": 150, "min_samples": 20,
                     "n_clusters": 2, "noise_ratio": 0.207},
            "variants": [{"min_cluster_size": 225, "min_samples": 20,
                          "n_clusters": 1, "noise_ratio": 0.30,
                          "stability_vs_base": 0.55}],
        },
        "heat_grid": [{"lon": 10.0 + i * 0.01, "lat": 55.0, "n": 100 + i}
                      for i in range(40)],
        "notes": ["热点基于历史 AIS 点的密度聚类结果，不代表官方航道或安全航区。"],
    }
    out = tmp_path / "results" / task_id
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False),
                                      encoding="utf-8")

    class FakeTasks:
        @staticmethod
        def get_task(tid):
            if tid != task_id:
                return None
            return {"task_id": task_id, "algorithm": "hdbscan_hotspot",
                    "dataset_version": "clean_test", "status": "succeeded",
                    "created_at": "2025-01-01T00:00:00", "finished_at": "2025-01-01T00:05:00",
                    "params": summary["params"], "filters": {}}

        @staticmethod
        def result_summary(tid):
            p = out / "summary.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    monkeypatch.setattr(report_mod, "tasks", FakeTasks)
    return task_id


def test_html_report_is_self_contained(fake_task):
    html = report_mod.render_html(fake_task)
    assert html.startswith("<!DOCTYPE html>")
    # 自包含：不得引用任何外部资源（离线可打开、可直接打印）
    for bad in ("http://", "https://", "<script", "cdn."):
        assert bad not in html, f"报告不应包含外部依赖：{bad}"
    assert "<style>" in html


def test_html_report_contains_traceability_fields(fake_task):
    html = report_mod.render_html(fake_task)
    assert fake_task in html                  # 任务 ID
    assert "clean_test" in html               # 数据版本
    assert "代码版本" in html                  # 代码版本
    assert "min_cluster_size" in html         # 参数摘要
    assert "结论口径与使用边界" in html        # 口径说明
    assert "报告生成时间" in html


def test_html_report_renders_tables_and_inline_svg(fake_task):
    html = report_mod.render_html(fake_task)
    assert "<table>" in html
    assert "<svg" in html, "热点报告应包含内嵌散点图"
    # 口径说明不得重复出现同一条（ALGO_CAVEATS 与算法 notes 措辞近似，须去重）
    assert html.count("不代表官方航道") == 1
    assert html.count("热点由历史 AIS 点密度聚类得到") == 1
    assert "热点基于历史 AIS 点的密度聚类结果" not in html


def test_markdown_report_has_front_matter_and_caveats(fake_task):
    md = report_mod.render_markdown(fake_task)
    assert md.startswith("# ")
    assert "关键指标" in md
    assert "结论口径与使用边界" in md
    assert "```json" in md


def test_report_escapes_html_in_summary_fields(tmp_path, monkeypatch):
    """摘要里出现 HTML 片段时必须转义，不能注入页面结构。"""
    from aisp.config import settings

    monkeypatch.setattr(type(settings), "results_root",
                        property(lambda self: tmp_path / "results"))
    task_id = "task_escape"
    summary = {"algorithm": "vessel_profile", "dataset_version": "dv",
               "params": {}, "filters": {}, "n_vessels": 1, "totals": {},
               "vessels": [{"mmsi": "<script>alert(1)</script>", "name": "<b>x</b>",
                            "ship_type": "Cargo", "points": 10, "segments": 1,
                            "distance_km": 1.0}]}
    out = tmp_path / "results" / task_id
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    class FakeTasks:
        get_task = staticmethod(lambda tid: {
            "task_id": task_id, "algorithm": "vessel_profile",
            "dataset_version": "dv", "status": "succeeded", "params": {}, "filters": {},
        } if tid == task_id else None)
        result_summary = staticmethod(lambda tid: summary if tid == task_id else None)

    monkeypatch.setattr(report_mod, "tasks", FakeTasks)
    html = report_mod.render_html(task_id)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_rejects_unknown_and_unfinished_tasks(tmp_path, monkeypatch):
    from aisp.config import settings

    monkeypatch.setattr(type(settings), "results_root",
                        property(lambda self: tmp_path / "results"))

    class FakeTasks:
        @staticmethod
        def get_task(tid):
            return None if tid == "nope" else {
                "task_id": "running_task", "algorithm": "predict",
                "dataset_version": "dv", "status": "running",
                "params": {}, "filters": {},
            }

        @staticmethod
        def result_summary(tid):
            return None  # 尚未产出结果

    monkeypatch.setattr(report_mod, "tasks", FakeTasks)
    with pytest.raises(ValueError, match="任务不存在"):
        report_mod.render_html("nope")
    with pytest.raises(ValueError, match="尚未产出结果"):
        report_mod.render_html("running_task")


def test_build_report_writes_file(tmp_path, monkeypatch):
    from aisp.config import settings

    monkeypatch.setattr(type(settings), "results_root",
                        property(lambda self: tmp_path / "results"))
    task_id = "task_write"
    out = tmp_path / "results" / task_id
    out.mkdir(parents=True)
    summary = {"algorithm": "correlate", "dataset_version": "dv", "params": {},
               "filters": {}, "n_samples": 10, "features": [], "edges": []}
    (out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    class FakeTasks:
        get_task = staticmethod(lambda tid: {
            "task_id": task_id, "algorithm": "correlate", "dataset_version": "dv",
            "status": "succeeded", "params": {}, "filters": {},
        } if tid == task_id else None)
        result_summary = staticmethod(lambda tid: summary if tid == task_id else None)

    monkeypatch.setattr(report_mod, "tasks", FakeTasks)
    p = report_mod.build_report(task_id, "html")
    assert p.is_file() and p.name == "report.html"
    assert p.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    p2 = report_mod.build_report(task_id, "md")
    assert p2.name == "report.md"

    with pytest.raises(ValueError, match="不支持的报告格式"):
        report_mod.build_report(task_id, "pdf")


def test_decimate_keeps_high_weight_points():
    """降采样必须保住高热度点，否则损失图会把重点区域抹平。"""
    pts = [[float(i) * 0.001, 55.0, float(i)] for i in range(5000)]
    out = report_mod._decimate(pts, limit=200)
    assert len(out) <= 200
    weights = [p[2] for p in out]
    # 最高热度点必须保留
    assert max(weights) == 4999.0
    # 保留点中高热度占比应明显高于均匀抽样（>10%）
    assert sum(1 for w in weights if w > 4000) >= 20


def test_extent_and_number_formatters():
    assert report_mod._extent([8.0, 54.0, 12.5, 58.0]) == "4.5° × 4.0°"
    assert report_mod._extent(None) == "—"
    assert report_mod._extent([1, 2]) == "—"
    assert report_mod._num(None) == "—"
    assert report_mod._num(1234.0) == "1,234"
    assert report_mod._num(1.23456, 2) == "1.23"
    assert report_mod._num("abc") == "abc"
    # 极小 p 值不能显示成 0，否则会被误读为「完全显著」
    assert report_mod._p(0.0) == "<1e-300"
    assert report_mod._p(1e-9) != "0.0000"
