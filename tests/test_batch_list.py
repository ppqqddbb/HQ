"""批次列表字段展开的契约测试。

背景（真实缺陷）：`/api/v1/datasets` 里的 `mmsi_count` / `encoding` 一直是空，
数据中心「批次表」的这两列永远显示「—」。原因是接口直接把
`data_batch.profile_json` 丢掉，而这两个值恰好只存在 profile 里。

本文件锁定「批次列表必须把 profile 里的关键指标展开成顶层字段」，
避免以后再被 drop 掉。
"""

from __future__ import annotations

import json

import pytest

from aisp import dataservice, store


@pytest.fixture()
def meta_db(tmp_path, monkeypatch):
    """把元数据库指向临时目录并重建表。

    conftest 已把 artifacts 重定向到全局临时目录，但多个测试文件可能互相
    重建/削表，因此这里单独换一份干净的库，做法与 test_clean_frozen 一致。
    """
    monkeypatch.setattr(store.settings, "artifacts_dir", tmp_path)
    store.close_shared()
    store.init_meta()
    yield
    store.close_shared()


def _insert(batch_id: str, profile: dict) -> None:
    store.exec_sql(
        "INSERT INTO data_batch (batch_id, source_name, source_path, file_hash, "
        "file_size, row_count, valid_row_count, status, dataset_version, "
        "created_at, created_by, note, profile_json) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [batch_id, "ais", "data", "h", 1, 100, 100, "cleaned", "clean_x",
         store._now(), "system", "", json.dumps(profile, ensure_ascii=False)],
    )


def test_batches_expands_profile_fields(meta_db):
    _insert("batch_a", {"m": 2460, "encoding": "utf-8-sig",
                        "delimiter": ",", "t0": "2025-01-02T00:00:00"})
    rows = dataservice.batches()
    assert len(rows) == 1
    r = rows[0]
    assert r["mmsi_count"] == 2460
    assert r["encoding"] == "utf-8-sig"
    assert r["time_range"] == ["2025-01-02T00:00:00", None]
    # profile_json 不应再原样透出（体积大且前端用不到）
    assert "profile_json" not in r


def test_batches_tolerates_broken_profile(meta_db):
    """profile_json 损坏时必须降级为 None，而不是让整个接口 500。"""
    store.exec_sql(
        "INSERT INTO data_batch (batch_id, source_name, source_path, file_hash, "
        "file_size, row_count, valid_row_count, status, dataset_version, "
        "created_at, created_by, note, profile_json) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["batch_bad", "ais", "data", "h", 1, 100, 100, "cleaned", "clean_x",
         store._now(), "system", "", "{not json"],
    )
    rows = dataservice.batches()
    assert rows[0]["mmsi_count"] is None
    assert rows[0]["encoding"] is None


def test_batches_empty(meta_db):
    assert dataservice.batches() == []
