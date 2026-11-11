"""身份认证与授权（技术方案 16.1 / 16.2 / 20.2）。

设计取向：
- **本地单机部署默认免认证**（`AISP_AUTH=off`），避免单人开发环境被登录流程卡住；
- 一旦 `AISP_AUTH=on`，所有 `/api/v1/*`（健康检查除外）都必须携带令牌，
  并按 **RBAC** 角色判定权限，越权返回 401/403 且写入审计；
- 令牌到角色的映射放在环境变量 `AISP_TOKENS`（`token=role` 用逗号分隔），
  不写入代码库；正式环境应替换为企业 IAM/LDAP/OIDC，接口形态保持一致。

角色与权限（最小可用集，覆盖方案里的分工）：
| 角色 | 权限 |
|---|---|
| admin | 全部（含备份恢复、运行时配置） |
| data_manager | 数据集读写、分析运行、审计查看 |
| analyst | 数据集只读、分析运行 |
| viewer | 数据集只读、结果只读 |
| auditor | 数据集只读、审计查看（不可运行分析、不可改动数据） |
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

ROLE_PERMS: Dict[str, Set[str]] = {
    "admin": {"*"},
    "data_manager": {"dataset:read", "dataset:write", "analysis:read", "analysis:run",
                     "report:read", "audit:read", "admin:read"},
    "analyst": {"dataset:read", "analysis:read", "analysis:run", "report:read"},
    "viewer": {"dataset:read", "analysis:read", "report:read"},
    "auditor": {"dataset:read", "analysis:read", "report:read", "audit:read"},
}

ROLE_CN = {
    "admin": "管理员",
    "data_manager": "数据管理员",
    "analyst": "分析人员",
    "viewer": "只读用户",
    "auditor": "审计人员",
}

# 公开端点：健康检查与前端静态资源
_PUBLIC_PATHS = ("/api/v1/health",)


@dataclass(frozen=True)
class Identity:
    """一次请求的身份。"""

    subject: str
    role: str
    source: str          # token / auth-off
    authenticated: bool

    @property
    def role_cn(self) -> str:
        return ROLE_CN.get(self.role, self.role)

    def can(self, perm: str) -> bool:
        perms = ROLE_PERMS.get(self.role, set())
        return "*" in perms or perm in perms

    def to_dict(self) -> Dict[str, object]:
        return {"subject": self.subject, "role": self.role, "role_cn": self.role_cn,
                "source": self.source, "authenticated": self.authenticated}


def auth_enabled() -> bool:
    return str(os.environ.get("AISP_AUTH", "off")).strip().lower() in ("1", "true", "on", "yes")


def _parse_tokens() -> Dict[str, str]:
    """解析 `AISP_TOKENS="tok1=admin,tok2=analyst"`；非法条目直接忽略。"""
    raw = os.environ.get("AISP_TOKENS", "") or ""
    out: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        tok, _, role = item.partition("=")
        tok, role = tok.strip(), role.strip().lower()
        if tok and role in ROLE_PERMS:
            out[tok] = role
    return out


_TOKEN_RE = re.compile(r"^(?:Bearer\s+)?(.+)$", re.IGNORECASE)


def identity_from_headers(headers: Dict[str, str]) -> Identity:
    """根据请求头解析身份。

    优先 `Authorization: Bearer <token>`，其次 `X-AIS-Token`。
    auth 关闭时一律视为本机管理员（本地单机部署形态）。
    """
    if not auth_enabled():
        return Identity(subject=headers.get("x-ais-user", "local"), role="admin",
                        source="auth-off", authenticated=False)

    raw = headers.get("authorization") or headers.get("x-ais-token") or ""
    m = _TOKEN_RE.match(raw.strip())
    token = (m.group(1).strip() if m else "")
    tokens = _parse_tokens()
    role = tokens.get(token)
    if role is None:
        return Identity(subject="anonymous", role="anonymous", source="token",
                        authenticated=False)
    return Identity(subject=f"token:{len(token)}位", role=role, source="token",
                    authenticated=True)


def required_permission(method: str, path: str) -> Optional[str]:
    """把 (方法, 路径) 映射为所需权限；返回 None 表示公开访问。

    路径规则刻意保持「前后端都可预测」的显式表，不做模糊匹配，
    避免新增端点时因漏配而默认放行。
    """
    m = method.upper()
    p = path.rstrip("/") or "/"

    if any(p == x or p.startswith(x + "/") for x in _PUBLIC_PATHS):
        return None
    if not p.startswith("/api/"):
        return None                      # 前端静态资源
    if p == "/api/v1/whoami":
        return None                      # 身份自检，带不带令牌都能访问

    if p == "/api/v1/datasets/import" or p == "/api/v1/datasets/upload":
        return "dataset:write"
    if p.startswith("/api/v1/datasets"):
        return "dataset:read"
    if p.startswith("/api/v1/admin/backup") or p.startswith("/api/v1/admin/restore"):
        return "backup:manage"
    if p.startswith("/api/v1/models"):
        # 监控页需要看模型卡 / 跑漂移检测 / 登记监控点；写操作视为运行类
        if m == "POST":
            return "analysis:run"
        return "analysis:read"
    if p.startswith("/api/v1/admin/audit"):
        return "audit:read"
    if p.startswith("/api/v1/admin"):
        return "admin:read"
    if p == "/api/v1/analysis/tasks" and m == "POST":
        return "analysis:run"
    if p.startswith("/api/v1/tasks"):
        # 取消任务视为运行类操作，避免只读用户影响他人任务
        if p.endswith("/cancel"):
            return "analysis:run"
        return "analysis:read"
    if p.startswith("/api/v1/algorithms"):
        return "analysis:read"
    # 地图 / 船舶 / 驻留点都读清洗结果，归入数据集只读
    return "dataset:read"


def denied_roles(perm: str) -> Iterable[Tuple[str, str]]:
    """调试辅助：列出不具备某权限的角色与中文名。"""
    for role in ROLE_PERMS:
        if not (("*" in ROLE_PERMS[role]) or (perm in ROLE_PERMS[role])):
            yield role, ROLE_CN.get(role, role)
