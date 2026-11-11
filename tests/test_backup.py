"""备份与恢复的单元测试（技术方案 19.1 / 20.1）。

覆盖：
1. 备份包必须含 manifest，且能被校验回读（拒绝任意压缩包）；
2. 默认不打包 CLEAN 明细，显式开启才纳入；
3. 恢复元数据库前后原库自动留存为 pre_restore 副本；
4. 恢复 CLEAN 时默认不覆盖已有版本，overwrite=True 才替换；
5. 非法备份包必须抛 ValueError，而不是部分解压污染产物目录。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from aisp import backup as backup_mod
from aisp.config import settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 artifacts 目录 + 一个最小元数据库 + 一个 CLEAN 数据集。"""
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings.ensure_dirs()

    con = duckdb.connect(str(settings.meta_db))
    con.execute("CREATE TABLE data_batch (batch_id VARCHAR, row_count BIGINT)")
    con.execute("INSERT INTO data_batch VALUES ('b1', 42)")
    con.close()

    d = tmp_path / "clean" / "clean_demo" / "event_date=2025-01-01"
    d.mkdir(parents=True)
    pd.DataFrame({"mmsi": ["1", "2"], "lon": [10.0, 10.1], "lat": [55.0, 55.1]}
                 ).to_parquet(d / "part-0.parquet", index=False)
    return tmp_path


def test_backup_contains_manifest_and_meta(env):
    m = backup_mod.create_backup()
    zp = Path(m["zip_path"])
    assert zp.is_file()
    assert m["meta_db"] is True
    assert m["include_clean"] is False

    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
        assert "meta/schema.sql" in names
        assert "manifest.json" in names
        # 默认不打包明细
        assert not [n for n in names if n.startswith("clean/")]
        man = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert man["magic"] == backup_mod.BACKUP_MAGIC
    assert man["datasets"][0]["dataset_version"] == "clean_demo"

    # 回读校验
    again = backup_mod.read_manifest(zp)
    assert again["magic"] == backup_mod.BACKUP_MAGIC


def test_backup_include_clean_packs_parquet_and_counts(env):
    m = backup_mod.create_backup(include_clean=True)
    with zipfile.ZipFile(m["zip_path"]) as zf:
        names = zf.namelist()
        assert any(n.startswith("clean/clean_demo/") for n in names)
    ds = m["datasets"][0]
    assert ds["n_files"] == 1
    assert ds["n_points"] == 2


def test_backup_can_limit_dataset_versions(env):
    other = env / "clean" / "clean_other" / "event_date=2025-01-02"
    other.mkdir(parents=True)
    pd.DataFrame({"mmsi": ["3"], "lon": [10.2], "lat": [55.2]}
                 ).to_parquet(other / "part-0.parquet", index=False)

    m = backup_mod.create_backup(dataset_versions=["clean_demo"])
    versions = [d["dataset_version"] for d in m["datasets"]]
    assert versions == ["clean_demo"]


def test_restore_rejects_foreign_zip(env, tmp_path):
    bad = tmp_path / "not_a_backup.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "hello")
    with pytest.raises(ValueError, match="manifest"):
        backup_mod.read_manifest(bad)
    with pytest.raises(ValueError):
        backup_mod.restore_backup(bad)


def test_restore_meta_keeps_previous_copy(env):
    from aisp import store

    m = backup_mod.create_backup()
    store.close_shared()

    # 备份后改动元数据库，确保恢复能真正回滚数据
    con = duckdb.connect(str(settings.meta_db))
    con.execute("DELETE FROM data_batch")
    con.close()

    out = backup_mod.restore_backup(Path(m["zip_path"]))
    assert out["restored_meta"] is True
    assert out["pre_restore_backup"] and Path(out["pre_restore_backup"]).is_file()

    # 恢复后元数据库被重新打开，用 store.q 读（不能再另开不同配置的连接）
    rows = int(store.q("SELECT count(*) AS n FROM data_batch").iloc[0]["n"])
    assert rows == 1


def test_restore_clean_requires_overwrite_for_existing(env):
    from aisp import store

    m = backup_mod.create_backup(include_clean=True)
    store.close_shared()

    out = backup_mod.restore_backup(Path(m["zip_path"]), restore_meta=False,
                                    restore_clean=True, overwrite=False)
    # 版本已存在 → 默认跳过，不覆盖
    assert out["restored_datasets"] == []
    assert out["skipped"] == ["clean_demo"]

    out2 = backup_mod.restore_backup(Path(m["zip_path"]), restore_meta=False,
                                     restore_clean=True, overwrite=True)
    assert out2["restored_datasets"] == ["clean_demo"]


def test_list_backups_reports_entries(env):
    backup_mod.create_backup()
    items = backup_mod.list_backups()
    assert len(items) == 1
    assert items[0]["manifest"]["magic"] == backup_mod.BACKUP_MAGIC
    assert items[0]["bytes"] > 0
