"""用户与角色管理（技术方案 16.1）。

设计取向与 `auth.py` 一致，但把「令牌 → 角色」的映射从环境变量搬到元数据库，
以便在系统管理页里增删用户、改角色、重置令牌，无需重启服务。

安全要点：
1. **令牌只存 SHA-256 摘要**，库被拿走也无法反推原始令牌；明文只在创建/重置那一次返回；
2. 令牌必须可撤销——删除用户或停用后立即失效；
3. 角色取值受 `auth.ROLE_PERMS` 约束，不接受任意字符串；
4. 任何写操作都写审计，带操作者；
5. 内置 `system` 管理员不允许删除/降级，避免把自己锁在门外。

与环境变量的关系：`AISP_TOKENS` 仍然有效（便于 CI / 一次性调试），
数据库中的用户与之**合并**生效，同名时以数据库为准。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import store
from .auth import ROLE_CN, ROLE_PERMS

# 令牌前缀：便于在日志/审计里识别，同时不泄露内容
_TOKEN_PREFIX = "ais_"
# 不允许被删除或降级的保底管理员
PROTECTED_USER = "system"

USER_DDL = """
CREATE TABLE IF NOT EXISTS app_user (
    user_id      VARCHAR PRIMARY KEY,
    display_name VARCHAR,
    role         VARCHAR,
    token_hash   VARCHAR,
    token_hint   VARCHAR,
    enabled      BOOLEAN,
    created_at   TIMESTAMP,
    created_by   VARCHAR,
    updated_at   TIMESTAMP,
    note         VARCHAR
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_schema() -> None:
    store.exec_sql(USER_DDL)


def hash_token(token: str) -> str:
    """令牌摘要。用 SHA-256 而非 bcrypt：令牌是 256 位随机串，
    不存在弱口令爆破风险，哈希只为「库里不存明文」。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hint(token: str) -> str:
    """给用户看的指纹：只能用于区分不同令牌，不能用于还原。"""
    return token[:8] + "…" + token[-4:]


def _row(r) -> Dict[str, Any]:
    return {
        "user_id": str(r.user_id),
        "display_name": str(r.display_name or r.user_id),
        "role": str(r.role),
        "role_cn": ROLE_CN.get(str(r.role), str(r.role)),
        "token_hint": str(r.token_hint or ""),
        "enabled": bool(r.enabled),
        "created_at": str(r.created_at or ""),
        "created_by": str(r.created_by or ""),
        "updated_at": str(r.updated_at or ""),
        "note": str(r.note or ""),
        "protected": str(r.user_id) == PROTECTED_USER,
    }


def list_users() -> List[Dict[str, Any]]:
    ensure_schema()
    df = store.q("SELECT * FROM app_user ORDER BY created_at")
    if df.empty:
        return []
    return [_row(r) for r in df.itertuples()]


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    df = store.q("SELECT * FROM app_user WHERE user_id = ?", [user_id])
    if df.empty:
        return None
    return _row(df.iloc[0])


def _validate_role(role: str) -> str:
    role = str(role or "").strip().lower()
    if role not in ROLE_PERMS:
        raise ValueError(f"未知角色 {role}；可选：{', '.join(ROLE_PERMS)}")
    return role


def create_user(user_id: str, role: str = "viewer", display_name: str = "",
                note: str = "", created_by: str = "system",
                token: Optional[str] = None) -> Dict[str, Any]:
    """创建用户并返回**一次性明文令牌**（库中只存摘要）。"""
    ensure_schema()
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("用户标识不能为空")
    if len(user_id) > 64:
        raise ValueError("用户标识过长（最多 64 字符）")
    role = _validate_role(role)
    if get_user(user_id) is not None:
        raise ValueError(f"用户 {user_id} 已存在")

    raw = token or new_token()
    store.exec_sql(
        "INSERT INTO app_user (user_id, display_name, role, token_hash, token_hint, "
        "enabled, created_at, created_by, updated_at, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [user_id, display_name or user_id, role, hash_token(raw), _hint(raw),
         True, _now(), created_by, _now(), note],
    )
    store.audit("user_create", f"user:{user_id}", f"role={role}",
                user_id=created_by)
    out = get_user(user_id) or {}
    out["token"] = raw          # 仅此一次返回明文
    return out


def update_user(user_id: str, *, role: Optional[str] = None,
                enabled: Optional[bool] = None, display_name: Optional[str] = None,
                note: Optional[str] = None, actor: str = "system") -> Dict[str, Any]:
    """改角色 / 启停 / 改名。内置管理员不允许降级或停用。"""
    ensure_schema()
    cur = get_user(user_id)
    if cur is None:
        raise ValueError(f"用户 {user_id} 不存在")
    sets: List[str] = []
    params: List[Any] = []
    changes: List[str] = []

    if role is not None:
        new_role = _validate_role(role)
        if user_id == PROTECTED_USER and new_role != "admin":
            raise ValueError("内置管理员 system 不能被降级")
        if new_role != cur["role"]:
            sets.append("role = ?")
            params.append(new_role)
            changes.append(f"role:{cur['role']}→{new_role}")

    if enabled is not None:
        if user_id == PROTECTED_USER and not enabled:
            raise ValueError("内置管理员 system 不能被停用")
        if bool(enabled) != cur["enabled"]:
            sets.append("enabled = ?")
            params.append(bool(enabled))
            changes.append("enabled" if enabled else "disabled")

    if display_name is not None:
        sets.append("display_name = ?")
        params.append(display_name or user_id)
        changes.append("display_name")

    if note is not None:
        sets.append("note = ?")
        params.append(note)
        changes.append("note")

    if not sets:
        return cur

    sets.append("updated_at = ?")
    params.append(_now())
    params.append(user_id)
    store.exec_sql(f"UPDATE app_user SET {', '.join(sets)} WHERE user_id = ?", params)
    store.audit("user_update", f"user:{user_id}", ",".join(changes), user_id=actor)
    return get_user(user_id) or cur


def reset_token(user_id: str, actor: str = "system") -> Dict[str, Any]:
    """重置令牌：旧令牌立即失效，新令牌只在此次返回明文。"""
    ensure_schema()
    if get_user(user_id) is None:
        raise ValueError(f"用户 {user_id} 不存在")
    raw = new_token()
    store.exec_sql(
        "UPDATE app_user SET token_hash = ?, token_hint = ?, updated_at = ? WHERE user_id = ?",
        [hash_token(raw), _hint(raw), _now(), user_id],
    )
    store.audit("user_reset_token", f"user:{user_id}", "令牌已重置", user_id=actor)
    out = get_user(user_id) or {}
    out["token"] = raw
    return out


def delete_user(user_id: str, actor: str = "system") -> None:
    ensure_schema()
    if user_id == PROTECTED_USER:
        raise ValueError("内置管理员 system 不能被删除")
    if get_user(user_id) is None:
        raise ValueError(f"用户 {user_id} 不存在")
    store.exec_sql("DELETE FROM app_user WHERE user_id = ?", [user_id])
    store.audit("user_delete", f"user:{user_id}", "", user_id=actor)


def resolve_token(token: str) -> Optional[str]:
    """按令牌查到角色；停用用户返回 None。供 auth 中间件调用。"""
    if not token:
        return None
    ensure_schema()
    df = store.q(
        "SELECT role FROM app_user WHERE token_hash = ? AND enabled = TRUE LIMIT 1",
        [hash_token(token)],
    )
    if df.empty:
        return None
    return str(df.iloc[0]["role"])


def ensure_system_admin() -> Optional[str]:
    """保底：库里没有任何可用管理员时，补一个 system 管理员。

    只在没有任何启用的 admin 时触发，避免每次启动都重置令牌
    （那样会悄悄让已分发的令牌失效）。

    返回新生成的明文令牌（仅在首次引导时有值），供调用方落盘/打印一次；
    正常启动（已有管理员）返回 None。
    """
    ensure_schema()
    df = store.q("SELECT count(*) AS n FROM app_user WHERE role = 'admin' AND enabled = TRUE")
    if not df.empty and int(df.iloc[0]["n"]) > 0:
        return None
    existing = get_user(PROTECTED_USER)
    if existing is None:
        created = create_user(PROTECTED_USER, role="admin", display_name="系统管理员",
                              note="内置管理员，不可删除", created_by="bootstrap")
        return created.get("token")
    # system 已存在但被停用（或没有任何可用管理员）：恢复启用并重置令牌，
    # 否则会卡在「有账户但无处登录」的状态。
    store.exec_sql(
        "UPDATE app_user SET role = 'admin', enabled = TRUE, updated_at = ? WHERE user_id = ?",
        [_now(), PROTECTED_USER],
    )
    return reset_token(PROTECTED_USER, actor="bootstrap").get("token")
