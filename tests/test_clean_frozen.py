"""冻结 GPS（FROZEN_GPS）检测的回归测试。

背景（真实缺陷）：某船 MMSI 244710047 在 2025-01-02 连续 6136 条记录上报
完全相同的坐标 (-85.392, -41.696)（大西洋），但同时报告 SOG=25.4 kn 持续 17 小时。
这显然是设备卡死而非真实航行。由于相邻点位移恰为 0，原有全部规则
（跳点 / 推算速度 / 漂移）都不会命中，这条错误坐标一路进入 CLEAN 数据集，
把数据集包络和模型卡的「训练区域」拉到全球尺度。

本文件锁定修复后的三条不变量：
1. 位置不变 + 报告在航行 + 持续足够久 → 必须标记 FROZEN_GPS；
2. 真实静止（SOG≈0 的锚泊/靠泊）绝不能被误判为冻结；
3. 真实施工中的船（位置连续变化）不能被误判。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from datetime import datetime, timedelta

from datetime import datetime, timedelta

from datetime import datetime, timedelta

from datetime import datetime, timedelta

from datetime import datetime, timedelta

from datetime import datetime, timedelta

import pandas as pd
import pytest

from aisp import clean, store
from aisp.config import default_params, settings


STAGING_COLS = [
    "mmsi", "event_time", "lon", "lat", "sog", "cog", "heading", "rot",
    "nav_status", "ship_type", "cargo_type", "draught", "length", "width",
    "imo", "callsign", "name", "destination", "mobile_type", "row_no",
    "source_id", "source_file", "batch_id",
]


def _row(mmsi, ts, lon, lat, sog=None, cog=None, row_no=1):
    return {
        "mmsi": mmsi, "event_time": datetime.fromisoformat(str(ts)) if not isinstance(ts, datetime) else ts, "lon": lon, "lat": lat,
        "sog": sog, "cog": cog, "heading": None, "rot": None,
        "nav_status": None, "ship_type": "Cargo", "cargo_type": None,
        "draught": None, "length": None, "width": None, "imo": None,
        "callsign": None, "name": None, "destination": None,
        "mobile_type": "Class A", "row_no": row_no, "source_id": "src1",
        "source_file": "f.csv", "batch_id": "b_test",
    }


@pytest.fixture()
def staging_root(tmp_path, monkeypatch):
    """构造 STAGING 分区并让 build_clean 只读到它。"""
    root = tmp_path / "staging"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(type(settings), "staging_root", property(lambda self: root))
    monkeypatch.setattr(type(settings), "clean_root", property(lambda self: tmp_path / "clean"))
    monkeypatch.setattr(type(settings), "tmp_dir", property(lambda self: tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    store.close_shared()
    # 共享元数据库连接可能指向上个用例的临时目录（例如 test_backup 自建了
    # 只有 batch_id/row_count 两列的精简表）。切回本用例目录前必须释放连接。
    store.init_meta()
    for t in ("data_batch", "dataset_version"):
        store.exec_sql(f"DROP TABLE IF EXISTS {t}")
    store.init_meta()
    return root


def _write_batch(root, batch_id, rows):
    d = root / batch_id
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=STAGING_COLS)
    # 真实 STAGING 的 event_time 是微秒精度 TIMESTAMP；nanosecond 会让 now() 比较报类型错误
    df["event_time"] = pd.to_datetime(df["event_time"]).astype("datetime64[us]")
    df.to_parquet(d / "part-0.parquet", index=False)
    # build_clean 从 data_batch 读取 batch 元数据
    store.exec_sql("DELETE FROM data_batch WHERE batch_id = ?", [batch_id])
    store.exec_sql(
        "INSERT INTO data_batch (batch_id, source_name, source_path, file_hash, file_size, "
        "row_count, valid_row_count, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [batch_id, "f.csv", str(d), "h", 0, len(rows), len(rows), "staged", store._now()],
    )
    return batch_id


def _frozen_rows(mmsi="244710047", n=40, step_s=30, sog=25.4, lon=-85.392423, lat=-41.696212):
    """位置逐位不变、段内报告航行速度的「卡死设备」记录。

    只给首行带 SOG，其余为 None：这与真实 AIS 导出完全一致，
    也是「逐行看 sog」会漏判的原因。
    """
    base = datetime(2025, 1, 2, 6, 47, 0)
    rows = []
    for i in range(n):
        rows.append(_row(
            mmsi, base + timedelta(seconds=i * step_s), lon, lat,
            sog=sog if i == 0 else None, cog=240.0, row_no=i + 1,
        ))
    return rows


def _moving_rows(mmsi="219000001", n=40, step_s=30, lon0=10.0, lat0=55.0):
    base = datetime(2025, 1, 2, 8, 0, 0)
    return [
        _row(mmsi, base + timedelta(seconds=i * step_s),
             lon0 + i * 0.005, lat0 + i * 0.002, sog=12.0, cog=90.0, row_no=i + 1)
        for i in range(n)
    ]


def _anchored_rows(mmsi="219000002", n=40, step_s=30):
    """真实锚泊：位置几乎不变（极小的 GPS 抖动），SOG≈0。"""
    base = datetime(2025, 1, 2, 9, 0, 0)
    return [
        _row(mmsi, base + timedelta(seconds=i * step_s),
             12.0 + (0.00001 if i % 2 else -0.00001), 55.0 + (0.00001 if i % 2 else -0.00001),
             sog=0.0, cog=None, row_no=i + 1)
        for i in range(n)
    ]


def _clean(batch_id, params=None):
    p = default_params("clean")
    if params:
        p.update(params)
    return clean.build_clean([batch_id], "clean_frozen_test", p)


# ------------------------------------------------------------------ 必须命中

def test_frozen_gps_is_flagged(staging_root):
    """位置不变 + 报告在航行 + 持续 ≥15 分钟 → 必须标记 FROZEN_GPS。"""
    batch = _write_batch(staging_root, "b_frozen", _frozen_rows())
    _clean(batch)

    df = pd.read_parquet(settings.clean_root / "clean_frozen_test" / "event_date=2025-01-02")
    hit = df["quality_flags"].fillna("").str.contains("FROZEN_GPS", regex=False)
    assert hit.sum() >= 35, f"应识别出冻结段，实际命中 {int(hit.sum())} 点"
    assert df.loc[hit, "mmsi"].nunique() == 1


def test_frozen_gps_counted_as_error(staging_root):
    """FROZEN_GPS 属于异常类标记，必须计入质量扣分口径。"""
    assert "FROZEN_GPS" in clean.ERROR_FLAGS
    assert "FROZEN_GPS" not in clean.INFO_FLAGS

    batch = _write_batch(staging_root, "b_frozen_err", _frozen_rows())
    rep = _clean(batch)["quality_report"]
    assert rep["flag_counts"].get("FROZEN_GPS", 0) > 0
    assert rep["error_points"] > 0


# ------------------------------------------------------------------ 不能误伤

def test_real_moving_vessel_not_flagged(staging_root):
    """正常航行（位置连续变化）不能被判成冻结。"""
    batch = _write_batch(staging_root, "b_moving", _moving_rows())
    _clean(batch)
    df = pd.read_parquet(settings.clean_root / "clean_frozen_test" / "event_date=2025-01-02")
    assert not df["quality_flags"].fillna("").str.contains("FROZEN_GPS", regex=False).any()


def test_anchored_vessel_not_flagged(staging_root):
    """真实锚泊（SOG≈0，位置仅微小抖动）不能被判成冻结。"""
    batch = _write_batch(staging_root, "b_anchor", _anchored_rows())
    _clean(batch)
    df = pd.read_parquet(settings.clean_root / "clean_frozen_test" / "event_date=2025-01-02")
    assert not df["quality_flags"].fillna("").str.contains("FROZEN_GPS", regex=False).any()


def test_short_stationary_gap_not_flagged(staging_root):
    """短暂停留（点数/时长未达门槛）不算冻结，避免把排队等待判成故障。"""
    batch = _write_batch(staging_root, "b_short", _frozen_rows(n=8, step_s=30))
    _clean(batch)
    df = pd.read_parquet(settings.clean_root / "clean_frozen_test" / "event_date=2025-01-02")
    assert not df["quality_flags"].fillna("").str.contains("FROZEN_GPS", regex=False).any()


def test_thresholds_are_configurable(staging_root):
    """阈值可通过参数收紧：把最小点数调到 100 后，40 点的段不再命中。"""
    batch = _write_batch(staging_root, "b_cfg", _frozen_rows(n=40))
    res = _clean(batch, {"frozen_gps_min_points": 100})
    assert res["quality_report"]["flag_counts"].get("FROZEN_GPS", 0) == 0
