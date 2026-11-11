"""统计与规则单元测试：BH 校正、相关系数强度分档、频繁序列判定、质量评分口径。

这些函数是分析结论的“正确性护栏”，必须能独立于数据库和任务框架验证。
"""

from __future__ import annotations

import numpy as np
import pytest

from aisp.algorithms.frequent_route import _contains_run, _is_contiguous_subseq
from aisp.algorithms.profile import _bh, _strength
from aisp.clean import ERROR_FLAGS, INFO_FLAGS, QUALITY_WEIGHTS


# --------------------------------------------------------------- BH 校正

def test_bh_empty():
    assert _bh([]) == []


def test_bh_single_is_unchanged():
    assert _bh([0.02])[0] == pytest.approx(0.02)


def test_bh_never_decreases_ranked_qvalues():
    """BH 的 q 值按 p 排序后必须单调不减（这是 BH 的核心性质）。"""
    pvals = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    q = _bh(pvals)
    order = np.argsort(pvals)
    ranked = [q[i] for i in order]
    for a, b in zip(ranked, ranked[1:]):
        assert b >= a - 1e-12


def test_bh_bounded_in_unit_interval():
    q = _bh([0.0001, 0.5, 0.999, 1.0])
    assert all(0.0 <= v <= 1.0 for v in q)


def test_bh_is_not_more_conservative_than_bonferroni_at_largest_p():
    """BH 的 q 值不大于 Bonferroni 校正值（同序位）。"""
    pvals = [0.01, 0.02, 0.03, 0.04]
    n = len(pvals)
    q = _bh(pvals)
    for p, qv in zip(pvals, q):
        assert qv <= min(1.0, p * n) + 1e-12


def test_bh_preserves_input_order():
    pvals = [0.9, 0.01, 0.5]
    q = _bh(pvals)
    assert len(q) == 3
    # 最小的 p 在中间位置，其 q 也应最小
    assert q[1] == min(q)


# --------------------------------------------------------------- 强度分档

@pytest.mark.parametrize(
    "value,expect",
    [(0.95, "强"), (0.70, "强"), (0.69, "中等"), (0.40, "中等"),
     (0.39, "弱"), (0.20, "弱"), (0.19, "极弱"), (0.0, "极弱")],
)
def test_strength_thresholds(value, expect):
    assert _strength(value) == expect


def test_strength_is_symmetric_for_negative():
    """强度只看绝对值，负相关同样应给出相同档位。"""
    assert _strength(-0.85) == _strength(0.85)


# --------------------------------------------------------------- 航线模式匹配
# 语义澄清：这里的匹配要求“连续出现”，与数学意义上的 subsequence（允许挖空）不同。
# 因为航线判定中“隔了几个网格再出现”不算同一条航线。

def test_contiguous_match_true_when_block_present():
    assert _is_contiguous_subseq([1, 3, 5], [0, 1, 3, 5, 9])


def test_contiguous_match_false_when_interleaved():
    """[1,3,5] 在 [1,2,3,4,5] 中并非连续出现，不能算匹配。"""
    assert not _is_contiguous_subseq([1, 3, 5], [1, 2, 3, 4, 5])


def test_contiguous_match_false_when_order_broken():
    assert not _is_contiguous_subseq([5, 1], [1, 2, 3, 4, 5])


def test_contiguous_match_empty_short():
    assert _is_contiguous_subseq([], [1, 2])


def test_contiguous_match_longer_than_sequence():
    assert not _is_contiguous_subseq([1, 2, 3], [1, 2])


def test_contains_run_true_for_consecutive_block():
    assert _contains_run([1, 2, 3, 4, 5], [2, 3, 4])


def test_contains_run_false_when_broken():
    """必须连续出现；[2,3] 与 [4,5] 之间断开，不能拼成 [2,3,4,5]。"""
    assert not _contains_run([2, 3, 99, 4, 5], [2, 3, 4, 5])


def test_contains_run_longer_than_sequence():
    assert not _contains_run([1, 2], [1, 2, 3])


# --------------------------------------------------------------- 质量评分

def test_quality_weights_sum_to_one():
    """加权总分必须是 0–100 区间的凸组合，权重和必须为 1。"""
    assert sum(QUALITY_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-12)


def test_quality_weights_all_positive():
    assert all(w > 0 for w in QUALITY_WEIGHTS.values())


def test_error_and_info_flags_are_disjoint():
    """异常标记计入扣分、提示标记不计分，两类不能重叠。"""
    assert set(ERROR_FLAGS).isdisjoint(set(INFO_FLAGS))


def test_overall_score_matches_weighted_dimensions():
    """按 aisp/clean.py 的口径复算综合质量分，验证加权公式。"""
    dims = {"完整性": 92.40, "有效性": 99.30, "时序连续性": 99.83,
            "空间合理性": 99.86, "字段一致性": 92.93}
    overall = sum(dims[k] * w for k, w in QUALITY_WEIGHTS.items())
    assert overall == pytest.approx(97.18, abs=0.01)


def test_overall_score_is_monotone_in_every_dimension():
    """任一维度提升都不应降低综合分。"""
    dims = {"完整性": 80.0, "有效性": 80.0, "时序连续性": 80.0,
            "空间合理性": 80.0, "字段一致性": 80.0}
    base = sum(dims[k] * w for k, w in QUALITY_WEIGHTS.items())
    for k in QUALITY_WEIGHTS:
        up = dict(dims)
        up[k] = 90.0
        assert sum(up[d] * w for d, w in QUALITY_WEIGHTS.items()) >= base
