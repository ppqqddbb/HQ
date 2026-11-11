"""用户与角色管理的单元测试（技术方案 16.1）。

锁定四条安全不变量：
1. 令牌只存摘要，任何接口都不回显明文（创建/重置那一刻除外）；
2. 删除或停用用户后令牌立即失效，不需要重启；
3. 内置管理员 system 不能被删除、降级或停用，避免把自己锁在门外；
4. 角色必须落在白名单内，非法角色直接拒绝。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aisp import auth, store, users
from aisp.config import settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时元数据库 + 清理鉴权环境变量。"""
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)
    store.close_shared()
    settings.ensure_dirs()
    store.init_meta()
    for t in ("app_user", "audit_log", "data_batch", "dataset_version"):
        store.exec_sql(f"DROP TABLE IF EXISTS {t}")
    store.init_meta()
    users.ensure_schema()
    monkeypatch.delenv("AISP_AUTH", raising=False)
    monkeypatch.delenv("AISP_TOKENS", raising=False)
    yield tmp_path
    store.close_shared()


# ---------------------------------------------------------------- 存储与令牌

def test_token_is_hashed_and_not_stored_plaintext(env):
    u = users.create_user("alice", role="analyst")
    token = u["token"]
    assert token.startswith("ais_")
    # 库里不能出现明文
    df = store.q("SELECT token_hash, token_hint FROM app_user WHERE user_id = 'alice'")
    assert token not in str(df.iloc[0]["token_hash"])
    assert df.iloc[0]["token_hash"] == users.hash_token(token)
    # 列表接口不回显明文
    listed = users.list_users()
    assert all("token" not in row for row in listed)


def test_resolve_token_returns_role(env):
    u = users.create_user("bob", role="viewer")
    assert users.resolve_token(u["token"]) == "viewer"
    assert users.resolve_token("ais_not_a_real_token") is None
    assert users.resolve_token("") is None


def test_disabled_user_token_stops_working(env):
    u = users.create_user("carol", role="analyst")
    assert users.resolve_token(u["token"]) == "analyst"
    users.update_user("carol", enabled=False)
    assert users.resolve_token(u["token"]) is None
    users.update_user("carol", enabled=True)
    assert users.resolve_token(u["token"]) == "analyst"


def test_reset_token_invalidates_old_one(env):
    u = users.create_user("dave", role="viewer")
    old = u["token"]
    new = users.reset_token("dave")["token"]
    assert new != old
    assert users.resolve_token(old) is None
    assert users.resolve_token(new) == "viewer"


def test_deleted_user_token_stops_working(env):
    u = users.create_user("erin", role="viewer")
    users.delete_user("erin")
    assert users.resolve_token(u["token"]) is None
    assert users.get_user("erin") is None


# ---------------------------------------------------------------- 角色白名单

def test_unknown_role_rejected(env):
    with pytest.raises(ValueError, match="未知角色"):
        users.create_user("frank", role="superuser")


def test_duplicate_user_rejected(env):
    users.create_user("gina", role="viewer")
    with pytest.raises(ValueError, match="已存在"):
        users.create_user("gina", role="analyst")


def test_empty_user_id_rejected(env):
    with pytest.raises(ValueError, match="不能为空"):
        users.create_user("   ", role="viewer")


# ---------------------------------------------------------------- 保护账号

def test_protected_admin_cannot_be_deleted_or_demoted(env):
    token = users.ensure_system_admin()
    assert token  # 首次引导必须给出令牌
    with pytest.raises(ValueError, match="不能被删除"):
        users.delete_user(users.PROTECTED_USER)
    with pytest.raises(ValueError, match="不能被降级"):
        users.update_user(users.PROTECTED_USER, role="viewer")
    with pytest.raises(ValueError, match="不能被停用"):
        users.update_user(users.PROTECTED_USER, enabled=False)


def test_bootstrap_only_once(env):
    first = users.ensure_system_admin()
    assert first
    # 已有可用管理员时不再重置，避免悄悄让已分发的令牌失效
    assert users.ensure_system_admin() is None
    assert users.resolve_token(first) == "admin"


# ---------------------------------------------------------------- 已接通鉴权

def test_db_user_token_authenticates(env, monkeypatch):
    u = users.create_user("helen", role="analyst")
    monkeypatch.setenv("AISP_AUTH", "on")
    ident = auth.identity_from_headers({"authorization": f"Bearer {u['token']}"})
    assert ident.authenticated is True
    assert ident.role == "analyst"
    assert ident.source == "user"
    assert ident.can("analysis:run")
    assert not ident.can("user:manage")


def test_user_manage_requires_admin(env):
    assert auth.required_permission("GET", "/api/v1/admin/users") == "user:manage"
    assert auth.required_permission("PATCH", "/api/v1/admin/users/alice") == "user:manage"
    assert auth.required_permission("DELETE", "/api/v1/admin/users/alice") == "user:manage"
    admin = auth.Identity(subject="t", role="admin", source="user", authenticated=True)
    analyst = auth.Identity(subject="t", role="analyst", source="user", authenticated=True)
    assert admin.can("user:manage")
    assert not analyst.can("user:manage")


# ---------------------------------------------------------------- API 契约

def test_api_user_crud_flow(env):
    from aisp.api import app

    client = TestClient(app)
    r = client.post("/api/v1/admin/users",
                    json={"user_id": "ivan", "role": "viewer", "display_name": "伊万"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    token = body["data"]["token"]
    assert token.startswith("ais_")
    assert "只显示一次" in body.get("notice", "")

    lst = client.get("/api/v1/admin/users").json()["data"]
    assert any(u["user_id"] == "ivan" for u in lst["users"])
    # 列表绝不含明文令牌
    assert token not in r.text.replace(body["data"]["token"], "")

    up = client.patch("/api/v1/admin/users/ivan", json={"role": "analyst"})
    assert up.status_code == 200 and up.json()["data"]["role"] == "analyst"

    rs = client.post("/api/v1/admin/users/ivan/token")
    assert rs.status_code == 200
    assert rs.json()["data"]["token"] != token

    dl = client.delete("/api/v1/admin/users/ivan")
    assert dl.status_code == 200 and dl.json()["data"]["deleted"] == "ivan"


def test_api_rejects_unknown_role(env):
    from aisp.api import app

    client = TestClient(app)
    r = client.post("/api/v1/admin/users", json={"user_id": "jack", "role": "god"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_delete_missing_user_returns_400(env):
    from aisp.api import app

    client = TestClient(app)
    r = client.delete("/api/v1/admin/users/nobody_here")
    assert r.status_code == 400


def test_api_roles_lists_all_roles(env):
    from aisp.api import app

    client = TestClient(app)
    rows = client.get("/api/v1/admin/roles").json()["data"]
    assert {r["role"] for r in rows} == set(auth.ROLE_PERMS)
    assert all(r["role_cn"] for r in rows)
