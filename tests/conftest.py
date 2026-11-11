"""pytest 全局配置。

关键点：在导入任何 aisp 模块之前，把产物目录（含 DuckDB 元数据库）重定向到临时目录。
否则测试进程会与正在运行的开发服务器争抢 artifacts/meta.duckdb 的文件锁，
导致大量假失败（IO Error: Cannot open file ... 另一个程序正在使用此文件）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 aisp.config 被导入前设置：config 在模块加载时读取 .env 与环境变量
_TEST_ARTIFACTS = Path(tempfile.mkdtemp(prefix="aisp_test_artifacts_"))
os.environ.setdefault("ARTIFACTS_DIR", str(_TEST_ARTIFACTS))
os.environ.setdefault("DATA_DIR", str(_TEST_ARTIFACTS / "data"))
os.environ.setdefault("APP_PORT", "8123")
