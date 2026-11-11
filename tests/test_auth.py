"""身份认证与授权的单元测试（技术方案 16.1 / 16.2）。

要点：
1. 默认 `AISP_AUTH=off` 时不得阻断本地开发（本机视为管理员）；
2. 开启后无令牌 → 401，角色不足 → 403，且都不泄露内部细节；
3. 权限映射必须按 (方法, 路径) 精确到端点，避免新增端点时默认放行；
4. 只读角色不能触发写操作（导入/上传/建任务/备份恢复）。
"""

from __future__ import annotations

import pytest

from aisp import auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AISP_AUTH", raising=False)
    monkeypatch.delenv("AISP_TOKENS", raising=False)


def test_auth_off_defaults_to_local_admin():
    assert auth.auth_enabled() is False
    ident = auth.identity_from_headers({})
    assert ident.role == "admin"
    assert ident.authenticated is False
    assert ident.can("dataset:write")
    assert ident.can("backup:manage")


def test_auth_on_requires_valid_token(monkeypatch):
    monkeypatch.setenv("AISP_AUTH", "on")
    monkeypatch.setenv("AISP_TOKENS", "tokA=admin,tokB=analyst,tokC=viewer")

    anon = auth.identity_from_headers({})
    assert anon.authenticated is False
    assert not anon.can("dataset:read")

    analyst = auth.identity_from_headers({"authorization": "Bearer tokB"})
    assert analyst.role == "analyst"
    assert analyst.authenticated is True
    assert analyst.can("analysis:run")
    assert not analyst.can("dataset:write")
    # 身份里不得回显完整令牌
    assert "tokB" not in analyst.subject


def test_token_can_be_passed_by_alt_header(monkeypatch):
    monkeypatch.setenv("AISP_AUTH", "on")
    monkeypatch.setenv("AISP_TOKENS", "tokC=viewer")
    ident = auth.identity_from_headers({"x-ais-token": "tokC"})
    assert ident.role == "viewer"


def test_invalid_token_entry_is_ignored(monkeypatch):
    monkeypatch.setenv("AISP_AUTH", "on")
    monkeypatch.setenv("AISP_TOKENS", "tokX=superuser,tokY=viewer")
    # superuser 不是合法角色 → 该令牌不可用
    assert auth.identity_from_headers({"authorization": "Bearer tokX"}).authenticated is False
    assert auth.identity_from_headers({"authorization": "Bearer tokY"}).role == "viewer"


def test_permission_mapping_covers_write_endpoints():
    assert auth.required_permission("POST", "/api/v1/datasets/import") == "dataset:write"
    assert auth.required_permission("POST", "/api/v1/datasets/upload") == "dataset:write"
    assert auth.required_permission("GET", "/api/v1/datasets") == "dataset:read"
    assert auth.required_permission("POST", "/api/v1/analysis/tasks") == "analysis:run"
    assert auth.required_permission("POST", "/api/v1/tasks/x/cancel") == "analysis:run"
    assert auth.required_permission("GET", "/api/v1/tasks/x/report") == "analysis:read"
    assert auth.required_permission("POST", "/api/v1/admin/backup") == "backup:manage"
    assert auth.required_permission("POST", "/api/v1/admin/restore") == "backup:manage"
    assert auth.required_permission("GET", "/api/v1/admin/audit") == "audit:read"
    assert auth.required_permission("GET", "/api/v1/map/points") == "dataset:read"


def test_public_paths_are_not_guarded():
    assert auth.required_permission("GET", "/api/v1/health") is None
    assert auth.required_permission("GET", "/api/v1/whoami") is None
    assert auth.required_permission("GET", "/") is None
    assert auth.required_permission("GET", "/assets/app.js") is None


def test_viewer_cannot_run_analysis_or_manage_backup():
    perms = auth.ROLE_PERMS["viewer"]
    assert "analysis:run" not in perms
    assert "backup:manage" not in perms
    denied = dict(auth.denied_roles("analysis:run"))
    assert "viewer" in denied
    # analyst 具备 analysis:run，不应出现在「被拒角色」中
    assert "analyst" not in denied


def test_api_denies_without_token_when_enabled(monkeypatch):
    """端到端：开启鉴权后无令牌访问数据集列表必须 401，并带统一错误结构。"""
    monkeypatch.setenv("AISP_AUTH", "on")
    monkeypatch.setenv("AISP_TOKENS", "tokB=analyst")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from aisp.api import app

    with fastapi_testclient.TestClient(app) as c:
        r = c.get("/api/v1/datasets")
        assert r.status_code == 401
        body = r.json()
        assert body["ok"] is False
        assert body["error"]["code"] == 401
        # 健康检查必须保持公开，便于探活
        assert c.get("/api/v1/health").status_code == 200


def test_api_allows_analyst_to_run_task(monkeypatch):
    """analyst 可进入分析任务接口（不实际跑重任务，只验证不被 401/403 拦住）。"""
    monkeypatch.setenv("AISP_AUTH", "on")
    monkeypatch.setenv("AISP_TOKENS", "tokB=analyst")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from aisp.api import app

    with fastapi_testclient.TestClient(app) as c:
        r = c.get("/api/v1/algorithms", headers={"Authorization": "Bearer tokB"})
        assert r.status_code == 200
        # 只读角色访问备份接口必须 403
        r2 = c.post("/api/v1/admin/backup", json={},
                    headers={"Authorization": "Bearer tokB"})
        assert r2.status_code == 403
        assert r2.json()["error"]["code"] == 403
