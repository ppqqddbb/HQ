"""空间工具单元测试：投影、网格边界、DDA 穿越与角度统计。

约定复核（见 aisp/geo.py 文档字符串）：
- 行列编号：grid_id = row_no * cols + col_no，行号自北向南递增；
- 单元格边界语义：左闭右开、下闭上开，外边界归入最后一行/列。
"""

from __future__ import annotations

import math

import pytest

from aisp.geo import (
    GridSpec,
    Projection,
    circular_mean_deg,
    grid_from_cell_size,
    grid_from_rows_cols,
    haversine_m,
    traverse_cells,
    wrap_deg,
)


# --------------------------------------------------------------- 基础几何

def test_haversine_known_distance():
    """赤道上经度差 1 度约 111.19 km（误差 < 0.5%）。"""
    d = haversine_m(0.0, 0.0, 0.0, 1.0)
    assert 110_000 < d < 112_000


def test_haversine_symmetric_and_zero():
    assert haversine_m(55.0, 12.0, 55.0, 12.0) == pytest.approx(0.0, abs=1e-9)
    a = haversine_m(55.0, 12.0, 56.0, 13.0)
    b = haversine_m(56.0, 13.0, 55.0, 12.0)
    assert a == pytest.approx(b, rel=1e-12)


def test_projection_roundtrip():
    proj = Projection.from_bbox(8.0, 54.0, 16.0, 58.0)
    for lon, lat in [(8.0, 54.0), (12.0, 56.0), (16.0, 58.0), (10.25, 55.75)]:
        x, y = proj.forward(lon, lat)
        back_lon, back_lat = proj.inverse(x, y)
        assert back_lon == pytest.approx(lon, abs=1e-9)
        assert back_lat == pytest.approx(lat, abs=1e-9)


def test_projection_origin_is_center():
    proj = Projection.from_bbox(8.0, 54.0, 16.0, 58.0)
    x, y = proj.forward(12.0, 56.0)
    assert abs(x) < 1e-6 and abs(y) < 1e-6


# --------------------------------------------------------------- 角度工具

def test_wrap_deg_bounds():
    assert wrap_deg(190.0) == pytest.approx(-170.0)
    assert wrap_deg(-190.0) == pytest.approx(170.0)
    assert wrap_deg(0.0) == pytest.approx(0.0)
    assert -180.0 <= wrap_deg(12345.0) <= 180.0


def test_circular_mean_crosses_north():
    """359° 与 1° 的圆均值应为 0°，而不是算术平均的 180°。"""
    assert circular_mean_deg([359.0, 1.0]) == pytest.approx(0.0, abs=1e-6)
    assert circular_mean_deg([350.0, 10.0]) == pytest.approx(0.0, abs=1e-6)


def test_circular_mean_empty_is_nan():
    assert math.isnan(circular_mean_deg([]))


# --------------------------------------------------------------- 网格定义

def _grid(rows: int = 4, cols: int = 5) -> GridSpec:
    return grid_from_rows_cols(8.0, 54.0, 16.0, 58.0, rows, cols, "t1")


def test_grid_dimensions():
    g = _grid(4, 5)
    assert g.n_cells == 20
    assert g.rows == 4 and g.cols == 5


def test_grid_id_roundtrip():
    g = _grid(4, 5)
    for row in range(g.rows):
        for col in range(g.cols):
            gid = g.grid_id(row, col)
            assert 0 <= gid < g.n_cells
            assert g.from_grid_id(gid) == (row, col)


def test_grid_row_increases_southward():
    """行号自北向南递增：北侧点行号小于南侧点。"""
    g = _grid(4, 5)
    north_row, _, ok_n = g.locate(12.0, 57.9)
    south_row, _, ok_s = g.locate(12.0, 54.1)
    assert ok_n and ok_s
    assert north_row < south_row


def test_grid_boundary_left_closed_right_open():
    """相邻单元格共享边界时，点归属右侧（东侧）单元格，验证左闭右开。"""
    g = _grid(4, 5)
    # 第 2 列与第 3 列的共享经线：x = x_min + 2 * cell_width
    lon_edge = g.proj.inverse(g.x_min + 2 * g.cell_width, 0.0)[0]
    _, col_left, _ = g.locate(lon_edge - 1e-6, 56.0)
    _, col_right, _ = g.locate(lon_edge + 1e-6, 56.0)
    assert col_left == 1
    assert col_right == 2


def test_grid_outer_boundary_goes_to_last_cell():
    """右边界 / 下边界归入最后一列 / 最后一行。"""
    g = _grid(4, 5)
    row, col, ok = g.locate(16.0, 54.0)  # 右下角
    assert ok
    assert (row, col) == (g.rows - 1, g.cols - 1)


def test_grid_outside_is_rejected():
    g = _grid(4, 5)
    for lon, lat in [(7.0, 56.0), (17.0, 56.0), (12.0, 53.0), (12.0, 59.0)]:
        _, _, ok = g.locate(lon, lat)
        assert not ok


def test_cell_bounds_contains_center_and_id_matches():
    g = _grid(4, 5)
    for row in range(g.rows):
        for col in range(g.cols):
            clon, clat = g.cell_center(row, col)
            minlon, minlat, maxlon, maxlat = g.cell_bounds(row, col)
            assert minlon <= clon <= maxlon
            assert minlat <= clat <= maxlat
            r2, c2, ok = g.locate(clon, clat)
            assert ok and (r2, c2) == (row, col)


def test_grid_from_cell_size_is_near_requested_size():
    g = grid_from_cell_size(8.0, 54.0, 16.0, 58.0, 1000.0, "t2")
    assert g.mode == "fixed_size"
    assert g.cell_width == pytest.approx(1000.0, rel=0.05)
    assert g.cell_height == pytest.approx(1000.0, rel=0.05)


# --------------------------------------------------------------- DDA 穿越

def test_traverse_same_cell_returns_single():
    g = _grid(4, 5)
    clon, clat = g.cell_center(1, 1)
    cells = traverse_cells(g, clon, clat, clon + 1e-5, clat + 1e-5)
    assert cells == [g.grid_id(1, 1)]


def test_traverse_horizontal_crosses_contiguous_cells():
    """沿同一行水平穿越，返回的网格必须首尾相接且无跳跃。"""
    g = _grid(4, 5)
    cells = traverse_cells(g, 8.05, 57.5, 15.95, 57.5)
    assert len(cells) >= 5
    assert cells[0] == g.grid_id(*g.locate(8.05, 57.5)[:2])
    assert cells[-1] == g.grid_id(*g.locate(15.95, 57.5)[:2])
    # 相邻网格必须四连通
    for a, b in zip(cells, cells[1:]):
        ra, ca = g.from_grid_id(a)
        rb, cb = g.from_grid_id(b)
        assert abs(ra - rb) + abs(ca - cb) == 1


def test_traverse_diagonal_has_no_duplicates_in_a_row():
    g = _grid(4, 5)
    cells = traverse_cells(g, 8.05, 54.05, 15.95, 57.95)
    assert len(set(cells)) == len(cells)


def test_traverse_outside_returns_empty():
    g = _grid(4, 5)
    assert traverse_cells(g, 0.0, 0.0, 12.0, 56.0) == []
    assert traverse_cells(g, 12.0, 56.0, 0.0, 0.0) == []


def test_traverse_endpoints_included():
    g = _grid(4, 5)
    start = (8.1, 54.1)
    end = (15.9, 57.9)
    cells = traverse_cells(g, *start, *end)
    assert g.grid_id(*g.locate(*start)[:2]) in cells
    assert g.grid_id(*g.locate(*end)[:2]) in cells
