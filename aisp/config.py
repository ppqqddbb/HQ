"""配置与路径管理。

原则：
1. 所有路径以项目根目录为基准解析，不依赖启动时的当前工作目录；
2. 配置中只保存可移植的相对路径，运行时解析为绝对路径；
3. 机器专属配置放在本机 `.env`（被 .gitignore 忽略），不写入源码。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

ROOT: Path = Path(__file__).resolve().parents[1]


def _parse_env_file(path: Path) -> Dict[str, str]:
    """极简 .env 解析，避免强制依赖 python-dotenv。"""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def _env(key: str, default: str | None = None) -> str | None:
    if key in os.environ:
        return os.environ[key]
    return _ENV_FILE_VALUES.get(key, default)


_ENV_FILE_VALUES = _parse_env_file(ROOT / ".env")


@dataclass
class Settings:
    """运行时配置。相对路径一律相对于项目根目录。"""

    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    artifacts_dir: Path = field(default_factory=lambda: ROOT / "artifacts")
    logs_dir: Path = field(default_factory=lambda: ROOT / "logs")
    web_dir: Path = field(default_factory=lambda: ROOT / "web")
    # 分析引擎参数
    duckdb_threads: int = 0          # 0 表示按 CPU 核数自动
    duckdb_memory_limit: str = "12GB"
    # 服务参数
    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        self.host = _env("APP_HOST", "127.0.0.1") or "127.0.0.1"
        self.port = int(_env("APP_PORT", "8000") or 8000)
        self.duckdb_memory_limit = _env("DUCKDB_MEMORY_LIMIT", self.duckdb_memory_limit) or self.duckdb_memory_limit
        # 允许通过环境变量覆盖数据目录（容器/共享存储场景）
        if _env("DATA_DIR"):
            p = Path(_env("DATA_DIR"))  # type: ignore[arg-type]
            self.data_dir = p if p.is_absolute() else self.root / p
        if _env("ARTIFACTS_DIR"):
            p = Path(_env("ARTIFACTS_DIR"))  # type: ignore[arg-type]
            self.artifacts_dir = p if p.is_absolute() else self.root / p

    # ---- 派生路径 ----
    @property
    def meta_db(self) -> Path:
        return self.artifacts_dir / "meta.duckdb"

    @property
    def tmp_dir(self) -> Path:
        return self.artifacts_dir / "tmp"

    @property
    def clean_root(self) -> Path:
        return self.artifacts_dir / "clean"

    @property
    def staging_root(self) -> Path:
        return self.artifacts_dir / "staging"

    @property
    def results_root(self) -> Path:
        return self.artifacts_dir / "results"

    @property
    def tasks_root(self) -> Path:
        return self.artifacts_dir / "tasks"

    @property
    def models_root(self) -> Path:
        return self.artifacts_dir / "models"

    @property
    def upload_root(self) -> Path:
        """浏览器上传的原始 CSV 落盘目录（原始层，只增不改）。"""
        return self.data_dir / "uploads"

    @property
    def config_versions_dir(self) -> Path:
        return self.artifacts_dir / "config_versions"

    def ensure_dirs(self) -> None:
        for p in (
            self.artifacts_dir,
            self.tmp_dir,
            self.clean_root,
            self.staging_root,
            self.results_root,
            self.tasks_root,
            self.models_root,
            self.config_versions_dir,
            self.logs_dir,
            self.upload_root,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "data_dir": str(self.data_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "logs_dir": str(self.logs_dir),
            "upload_dir": str(self.upload_root),
            "duckdb_memory_limit": self.duckdb_memory_limit,
            "host": self.host,
            "port": self.port,
        }


settings = Settings()
settings.ensure_dirs()

# ---- 默认算法参数基线（附录 B），全部可被任务参数覆盖 ----
DEFAULT_PARAMS: Dict[str, Dict[str, Any]] = {
    "ingest": {
        "max_implied_speed_kn": 60.0,   # 物理上限，用于跳点判定
        "valid_lat_range": [-90.0, 90.0],
        "valid_lon_range": [-180.0, 180.0],
    },
    "clean": {
        "max_gap_minutes": 60.0,        # 轨迹断点时间阈值（可在 30/60/120/240 中调整）
        "speed_spike_kn": 80.0,          # SOG 物理阈值
        "jump_speed_kn": 90.0,           # 推算速度超过该值判定为跳点
        "max_jump_m": 50000.0,           # 单点位移上限（米）
        "min_segment_points": 5,         # 轨迹段最小点数
        "stay_radius_m": 200.0,          # 驻留点半径 R
        "stay_minutes": 20.0,            # 驻留点最短持续 T
        "stay_max_sog_kn": 1.5,          # 驻留候选点的航速上限
        "frozen_gps_sog_kn": 2.0,       # 位移为 0 但 SOG 高于该值 → 判定为冻结/卡死 GPS
        "frozen_gps_min_points": 20,     # 冻结段最少连续点数
        "frozen_gps_min_minutes": 15.0,   # 冻结段最少持续时长（分钟）
        "dedup_keys": ["mmsi", "event_time"],
        # 基站/航标/SAR 飞机不是船舶目标，默认不进入船舶轨迹分析
        "exclude_mobile_types": [
            "Base Station", "AtoN", "SAR Airborne", "Search and Rescue Transponder"
        ],
    },
    "hotspot": {
        "min_cluster_size": 150,
        "min_samples": 20,
        "cluster_selection_method": "eom",
        "cluster_selection_epsilon": 0.0,
        "time_slot_minutes": 5,
        "max_points": 150_000,
        "min_vessels": 8,
        "grid_resolution": 0.01,
        "top_n": 20,
    },
    "profile": {
        "top_n": 50,
        "bin_count": 30,
    },
    "correlate": {
        "method": "spearman",
        "max_features": 10,
        "min_samples": 30,
    },
    "formation": {
        "resample_seconds": 120,
        "time_overlap_min": 0.6,
        "max_sep_m": 3000.0,
        "cog_tol_deg": 45.0,
        "sog_tol_kn": 4.0,
        "dtw_threshold": 1.2,
        "linkage": "average",
        "min_members": 2,
        "max_pairs": 20000,
        "max_sequences": 120,
        "window_minutes": 240,
    },
    "frequent_route": {
        "grid_mode": "rows_cols",
        "grid_rows": 10,
        "grid_cols": 10,
        "cell_size_m": 1000.0,
        "min_support_count": 3,
        "min_support_ratio": 0.01,
        "min_pattern_len": 3,
        "max_pattern_len": 8,
        "max_gap_seconds": 3600.0,
        "max_jump_m": 20000.0,
        "enable_grid_traversal": False,
        "counting_unit": "trajectory_segment_id",
        "top_k": 20,
        "extent_mode": "percentile",
        "extent_quantile": 0.005,
        "extent_buffer_deg": 0.0,
    },
    "predict": {
        "input_minutes": 60,
        "horizon_minutes": 30,
        "step_minutes": 5,
        "models": ["cv", "kalman", "gru"],
        "gru_epochs": 30,
        "gru_hidden": 64,
        "gru_target": "residual",
        "max_windows": 60000,
        "max_trajectories": 800,
        "min_quality_score": 60.0,
        "seed": 42,
        "min_points": 8,
    },
}

TASK_QUEUES = {
    "ingest": ["ingest", "clean"],
    "spatial": ["hotspot", "staypoint"],
    "sequence": ["formation", "frequent_route", "vessel_profile", "correlate"],
    "ml-train": ["predict_train", "predict"],
    "ml-infer": ["predict_infer"],
    "export": ["export"],
}


def default_params(algorithm: str) -> Dict[str, Any]:
    """返回算法默认参数副本（含通用 clean 基线）。"""
    params: Dict[str, Any] = {}
    if algorithm not in ("clean", "ingest"):
        params.update(json.loads(json.dumps(DEFAULT_PARAMS["clean"])))
    params.update(json.loads(json.dumps(DEFAULT_PARAMS.get(algorithm, {}))))
    return params
