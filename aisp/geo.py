"""空间工具：球面距离、局部 ENU 投影、规则网格与网格穿越。

约定：
- 经纬度单位为 degree，距离单位为 meter；
- 局部投影一律使用以分析范围中心为原点的等距圆柱近似（ENU），
  仅在局部区域（数百公里内）使用，避免把全球经纬度当平面；
- 网格编号 grid_id = row_no * cols + col_no，行号自北向南递增。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

EARTH_R = 6371008.8  # 平均地球半径（m）
M_PER_DEG_LAT = math.pi * EARTH_R / 180.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离（m）。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def wrap_deg(delta: float) -> float:
    """把角度差折算到 [-180, 180]。"""
    d = (delta + 180.0) % 360.0 - 180.0
    return d


def circular_mean_deg(angles: Sequence[float]) -> float:
    """角度圆均值，正确跨 359°/1°。"""
    if not angles:
        return float("nan")
    s = sum(math.sin(math.radians(a)) for a in angles)
    c = sum(math.cos(math.radians(a)) for a in angles)
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return float("nan")
    return math.degrees(math.atan2(s, c)) % 360.0


@dataclass(frozen=True)
class Projection:
    """局部米制等距圆柱投影（以 lon0/lat0 为原点），同时输出 ENU(x=East, y=North)。"""

    lon0: float
    lat0: float

    @property
    def kx(self) -> float:
        return M_PER_DEG_LAT * math.cos(math.radians(self.lat0))

    @property
    def ky(self) -> float:
        return M_PER_DEG_LAT

    def forward(self, lon: float, lat: float) -> Tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def inverse(self, x: float, y: float) -> Tuple[float, float]:
        return self.lon0 + x / self.kx, self.lat0 + y / self.ky

    def scale_error(self, lat: float) -> float:
        """给定纬度的横向尺度相对误差（近似）。"""
        return abs(math.cos(math.radians(lat)) / math.cos(math.radians(self.lat0)) - 1.0)

    @classmethod
    def from_bbox(cls, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> "Projection":
        return cls(lon0=(min_lon + max_lon) / 2.0, lat0=(min_lat + max_lat) / 2.0)


@dataclass
class GridSpec:
    """规则矩形网格定义。"""

    version: str
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    rows: int
    cols: int
    mode: str = "rows_cols"
    cell_size_m: float = 0.0

    def __post_init__(self) -> None:
        self.proj = Projection.from_bbox(self.min_lon, self.min_lat, self.max_lon, self.max_lat)
        x0, y1 = self.proj.forward(self.min_lon, self.min_lat)
        x1, y0 = self.proj.forward(self.max_lon, self.max_lat)
        self.x_min, self.x_max = min(x0, x1), max(x0, x1)
        self.y_min, self.y_max = min(y0, y1), max(y0, y1)
        self.cell_width = (self.x_max - self.x_min) / self.cols
        self.cell_height = (self.y_max - self.y_min) / self.rows

    # ---- 投影 ----
    def project(self, lon: float, lat: float) -> Tuple[float, float]:
        return self.proj.forward(lon, lat)

    # ---- 行列计算 ----
    def locate(self, lon: float, lat: float) -> Tuple[int, int, bool]:
        """返回 (row_no, col_no, in_area)。左闭右开、下闭上开；外边界归入最后一行/列。"""
        x, y = self.project(lon, lat)
        eps = 1e-9
        if x < self.x_min - eps or x > self.x_max + eps or y < self.y_min - eps or y > self.y_max + eps:
            return -1, -1, False
        if x >= self.x_max - eps:
            col = self.cols - 1
        else:
            col = int(math.floor((x - self.x_min) / self.cell_width))
        if y <= self.y_min + eps:
            row = self.rows - 1
        else:
            row = int(math.floor((self.y_max - y) / self.cell_height))
        col = min(max(col, 0), self.cols - 1)
        row = min(max(row, 0), self.rows - 1)
        return row, col, True

    def grid_id(self, row: int, col: int) -> int:
        return row * self.cols + col

    def from_grid_id(self, gid: int) -> Tuple[int, int]:
        return gid // self.cols, gid % self.cols

    # ---- 几何 ----
    def cell_bounds(self, row: int, col: int) -> Tuple[float, float, float, float]:
        """返回单元格 (min_lon, min_lat, max_lon, max_lat)。"""
        x_a = self.x_min + col * self.cell_width
        x_b = x_a + self.cell_width
        y_top = self.y_max - row * self.cell_height
        y_bot = y_top - self.cell_height
        lon_a, lat_b = self.proj.inverse(x_a, y_bot)
        lon_b, lat_t = self.proj.inverse(x_b, y_top)
        return min(lon_a, lon_b), min(lat_b, lat_t), max(lon_a, lon_b), max(lat_b, lat_t)

    def cell_center(self, row: int, col: int) -> Tuple[float, float]:
        x = self.x_min + (col + 0.5) * self.cell_width
        y = self.y_max - (row + 0.5) * self.cell_height
        return self.proj.inverse(x, y)

    @property
    def n_cells(self) -> int:
        return self.rows * self.cols

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "mode": self.mode,
            "extent": [self.min_lon, self.min_lat, self.max_lon, self.max_lat],
            "rows": self.rows,
            "cols": self.cols,
            "cell_width_m": round(self.cell_width, 2),
            "cell_height_m": round(self.cell_height, 2),
            "crs": "EPSG:4326 + 局部 ENU 米制投影",
            "n_cells": self.n_cells,
        }

    def cells_geojson(self) -> dict:
        feats = []
        for row in range(self.rows):
            for col in range(self.cols):
                minx, miny, maxx, maxy = self.cell_bounds(row, col)
                gid = self.grid_id(row, col)
                feats.append(
                    {
                        "type": "Feature",
                        "id": gid,
                        "properties": {"grid_id": gid, "row_no": row, "col_no": col},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [minx, miny],
                                    [maxx, miny],
                                    [maxx, maxy],
                                    [minx, maxy],
                                    [minx, miny],
                                ]
                            ],
                        },
                    }
                )
        return {"type": "FeatureCollection", "features": feats}


def grid_from_rows_cols(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    rows: int, cols: int, version: str,
) -> GridSpec:
    return GridSpec(version, min_lon, min_lat, max_lon, max_lat, rows, cols, "rows_cols")


def grid_from_cell_size(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    cell_size_m: float, version: str,
) -> GridSpec:
    proj = Projection.from_bbox(min_lon, min_lat, max_lon, max_lat)
    x0, y0 = proj.forward(min_lon, min_lat)
    x1, y1 = proj.forward(max_lon, max_lat)
    width = abs(x1 - x0)
    height = abs(y1 - y0)
    cols = max(1, int(round(width / cell_size_m)))
    rows = max(1, int(round(height / cell_size_m)))
    return GridSpec(version, min_lon, min_lat, max_lon, max_lat, rows, cols, "fixed_size", cell_size_m)


def traverse_cells(grid: GridSpec, lon1: float, lat1: float, lon2: float, lat2: float) -> List[int]:
    """二维规则网格 DDA 穿越，返回线段依次穿过的 grid_id（含起终点所在格）。

    仅用于"合格短线段"的推断补格；调用方必须先做时间/速度/位移质量门槛。
    """
    x1, y1 = grid.project(lon1, lat1)
    x2, y2 = grid.project(lon2, lat2)
    r1, c1, ok1 = grid.locate(lon1, lat1)
    r2, c2, ok2 = grid.locate(lon2, lat2)
    if not (ok1 and ok2):
        return []
    if r1 == r2 and c1 == c2:
        return [grid.grid_id(r1, c1)]

    cw, ch = grid.cell_width, grid.cell_height
    eps = 1e-9
    # 归一化到以左下角为原点的格坐标
    gx1 = (x1 - grid.x_min) / cw
    gy1 = (grid.y_max - y1) / ch
    gx2 = (x2 - grid.x_min) / cw
    gy2 = (grid.y_max - y2) / ch
    dx, dy = gx2 - gx1, gy2 - gy1

    step_c = 1 if dx > 0 else -1
    step_r = 1 if dy > 0 else -1
    t_max_c = ((math.floor(gx1) + (1 if dx > 0 else 0)) - gx1) / dx if abs(dx) > eps else math.inf
    t_max_r = ((math.floor(gy1) + (1 if dy > 0 else 0)) - gy1) / dy if abs(dy) > eps else math.inf
    t_delta_c = abs(1.0 / dx) if abs(dx) > eps else math.inf
    t_delta_r = abs(1.0 / dy) if abs(dy) > eps else math.inf

    col, row = c1, r1
    out = [grid.grid_id(row, col)]
    guard = 0
    while (row != r2 or col != c2) and guard < 4 * (grid.rows + grid.cols) + 8:
        guard += 1
        if t_max_c < t_max_r:
            col += step_c
            t_max_c += t_delta_c
        else:
            row += step_r
            t_max_r += t_delta_r
        if not (0 <= row < grid.rows and 0 <= col < grid.cols):
            break
        out.append(grid.grid_id(row, col))
    return out


def compress_consecutive(seq: Iterable[int]) -> List[int]:
    """连续重复压缩：A-A-A-B-B-C -> A-B-C；A-B-A 保留。"""
    out: List[int] = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out
