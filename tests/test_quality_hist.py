"""质量报告字段名的契约测试。

背景（真实缺陷）：前端数据中心页把航速直方图的取值写成 `d.k`，
而后端质量报告输出的是 `d.knot`，导致图表横轴全部渲染成「undefinedkn」。
这类「字段改名但前端没跟着改」的缺陷无法被后端逻辑测试发现，
因此这里把质量报告里各直方图的字段名固化成契约：
前端只要按这些键取值，就不会再出现空标签。
"""

from __future__ import annotations

import pytest

from test_clean_frozen import _clean, _moving_rows, _write_batch, staging_root  # noqa: F401


@pytest.fixture()
def report(staging_root):  # noqa: F811
    batch = _write_batch(staging_root, "b_hist", _moving_rows(n=60))
    return _clean(batch)["quality_report"]


def test_speed_histogram_uses_knot_key(report):
    """航速直方图每项必须是 {knot, n}，不能是 {k, n}。"""
    rows = report["speed_histogram"]
    assert rows, "航速直方图不应为空"
    for r in rows:
        assert "knot" in r, f"缺少 knot 字段：{r}"
        assert "k" not in r, f"不应再用缩写 k：{r}"
        assert isinstance(r["n"], int) and r["n"] > 0


def test_cog_histogram_uses_deg_key(report):
    """航向直方图每项必须是 {deg, n}，按 10 度分箱。"""
    rows = report["cog_histogram"]
    assert rows, "航向直方图不应为空"
    for r in rows:
        assert "deg" in r, f"缺少 deg 字段：{r}"
        assert r["deg"] % 10 == 0


def test_interval_histogram_uses_bucket_key(report):
    """上报间隔直方图每项必须是 {bucket, n}。"""
    rows = report["interval_histogram"]
    assert rows, "间隔直方图不应为空"
    for r in rows:
        assert set(r) == {"bucket", "n"}, f"字段不符：{r}"
        assert isinstance(r["bucket"], str)


def test_hourly_uses_h_key(report):
    """小时分布每项必须是 {h, n}，h 覆盖 0-23 的整数。"""
    rows = report["hourly"]
    assert rows
    for r in rows:
        assert set(r) == {"h", "n"}
        assert 0 <= r["h"] <= 23
