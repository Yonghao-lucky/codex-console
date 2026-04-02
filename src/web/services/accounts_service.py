"""
账号业务服务层：对路由层暴露稳定的查询/聚合接口。
"""

from __future__ import annotations

import logging
from typing import Dict, Iterator, Optional, Any
from http.cookies import SimpleCookie

from ...config.constants import RoleTag
from ...config.constants import OPENAI_API_ENDPOINTS
from ...core.openai.payment import check_subscription_status_detail
from ...core.openai.token_refresh import TokenRefreshManager
from ...core.timezone_utils import utcnow_naive
from ...database import crud
from ...database.models import Account
from ...database.session import get_db
from ..repositories.account_repository import iter_query_in_batches, query_role_tag_counts

logger = logging.getLogger(__name__)


def stream_accounts(query, *, batch_size: int = 200) -> Iterator:
    return iter_query_in_batches(query, batch_size=batch_size)


def get_role_tag_counts(db) -> Dict[str, int]:
    return query_role_tag_counts(db)


def _normalize_role_tag(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if text in {"mother", "parent", "manager", "母号"}:
        return RoleTag.PARENT.value
    if text in {"child", "member", "子号"}:
        return RoleTag.CHILD.value
    return RoleTag.NONE.value


def _normalize_account_label_from_role(role_tag: str) -> str:
    if role_tag == RoleTag.PARENT.value:
        return "mother"
    if role_tag == RoleTag.CHILD.value:
        return "child"
    return "none"


def _normalize_plan(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if "team" in text or "enterprise" in text:
        return "team"
    if "plus" in text or "pro" in text:
        return "plus"
    return "free"


def _extract_jwt_plan(token: Optional[str]) -> str:
    payload = _safe_decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
    if isinstance(auth, dict):
        return _normalize_plan(auth.get("chatgpt_plan_type"))
    return "free"


def _safe_decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    import base64
    import json

    raw = str(token or "").strip()
    if raw.count(".") < 2:
        return {}
    try:
        payload_part = raw.split(".")[1]
        padding = "=" * (-len(payload_part) % 4)
        decoded = base64.urlsafe_b64decode((payload_part + padding).encode("utf-8"))
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _resolve_workspace_id(account: Account) -> str:
    for value in (
        getattr(account, "workspace_id", None),
        getattr(account, "account_id", None),
    ):
        text = str(value or "").strip()
        if text:
            return text

    for token in (getattr(account, "access_token", None), getattr(account, "id_token", None)):
        payload = _safe_decode_jwt_payload(token)
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            for key in ("chatgpt_account_id", "workspace_id", "account_id"):
                text = str(auth.get(key) or "").strip()
                if text:
                    return text

    extra = getattr(account, "extra_data", None)
    if isinstance(extra, dict):
        for key in ("workspace_id", "account_id", "chatgpt_account_id"):
            text = str(extra.get(key) or "").strip()
            if text:
                return text
    return ""


def _fetch_team_workspace_candidates(access_token: str, proxy_url: Optional[str]) -> list[dict]:
    from curl_cffi import requests as cffi_requests

    token = str(access_token or "").strip()
    if not token:
        return []

    session_kwargs: Dict[str, Any] = {"impersonate": "chrome120", "timeout": 20}
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    try:
        session = cffi_requests.Session(**session_kwargs)
        response = session.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
            },
        )
        if int(response.status_code or 0) != 200:
            return []
        payload = response.json() or {}
    except Exception:
        return []

    accounts_data = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts_data, dict):
        return []

    result: list[dict] = []
    for account_id, item in accounts_data.items():
        if not isinstance(item, dict):
            continue
        account_info = item.get("account") or {}
        entitlement = item.get("entitlement") or {}
        if not isinstance(account_info, dict):
            account_info = {}
        if not isinstance(entitlement, dict):
            entitlement = {}
        plan = _normalize_plan(
            account_info.get("plan_type") or entitlement.get("subscription_plan") or ""
        )
        if plan != "team":
            continue
        result.append(
            {
                "account_id": str(account_id or "").strip(),
                "role": str(account_info.get("account_user_role") or "").strip(),
                "subscription_plan": str(
                    entitlement.get("subscription_plan") or account_info.get("plan_type") or ""
                ).strip(),
                "name": str(account_info.get("name") or "").strip(),
                "is_default": bool(account_info.get("is_default")),
            }
        )

    result.sort(
        key=lambda item: (
            0 if item.get("is_default") else 1,
            0 if str(item.get("role") or "").strip().lower() in {"owner", "admin", "manager"} else 1,
        )
    )
    return result


def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
    try:
        token = cookie_jar.get("__Secure-next-auth.session-token")
        if token:
            return str(token).strip()
    except Exception:
        return ""
    return ""


def _capture_auth_session_tokens(access_token: str, session_token: str, proxy_url: Optional[str]) -> Dict[str, str]:
    from curl_cffi import requests as cffi_requests

    result = {"access_token": str(access_token or "").strip(), "session_token": str(session_token or "").strip()}
    session_kwargs: Dict[str, Any] = {"impersonate": "chrome120", "timeout": 20}
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    try:
        session = cffi_requests.Session(**session_kwargs)
        if result["session_token"]:
            session.cookies.set("__Secure-next-auth.session-token", result["session_token"], domain=".chatgpt.com", path="/")
        headers = {
            "accept": "application/json",
            "referer": "https://chatgpt.com/",
            "origin": "https://chatgpt.com",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if result["access_token"]:
            headers["authorization"] = f"Bearer {result['access_token']}"
        response = session.get("https://chatgpt.com/api/auth/session", headers=headers)
        if int(response.status_code or 0) == 200:
            try:
                data = response.json() or {}
                new_access_token = str(data.get("accessToken") or "").strip()
                if new_access_token:
                    result["access_token"] = new_access_token
            except Exception:
                pass
        if not result["session_token"]:
            result["session_token"] = _extract_session_token_from_cookie_jar(session.cookies)
    except Exception:
        return result
    return result


def _switch_workspace_and_capture_tokens(account: Account, workspace_id: str, proxy_url: Optional[str]) -> Dict[str, str]:
    from curl_cffi import requests as cffi_requests

    token = str(getattr(account, "access_token", "") or "").strip()
    session_token = str(getattr(account, "session_token", "") or "").strip()
    if not workspace_id or (not token and not session_token):
        return {"access_token": token, "session_token": session_token}

    session_kwargs: Dict[str, Any] = {"impersonate": "chrome120", "timeout": 20}
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
    try:
        session = cffi_requests.Session(**session_kwargs)
        if session_token:
            session.cookies.set("__Secure-next-auth.session-token", session_token, domain=".chatgpt.com", path="/")
        headers = {
            "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        response = session.post(
            OPENAI_API_ENDPOINTS["select_workspace"],
            headers=headers,
            data=f'{{"workspace_id":"{workspace_id}"}}',
            allow_redirects=False,
        )
        if int(response.status_code or 0) not in {200, 301, 302, 303, 307, 308}:
            return {"access_token": token, "session_token": session_token}

        result = _capture_auth_session_tokens(token, _extract_session_token_from_cookie_jar(session.cookies) or session_token, proxy_url)
        return result
    except Exception:
        return {"access_token": token, "session_token": session_token}


def _bootstrap_session_token_by_relogin(account_id: int, proxy_url: Optional[str]) -> str:
    from ..routes.payment import _bootstrap_session_token_by_relogin as relogin_bootstrap

    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return ""
        return str(relogin_bootstrap(db, account, proxy_url) or "").strip()


def reconcile_account_runtime_state(
    account_id: int,
    *,
    proxy_url: Optional[str] = None,
    desired_role_tag: Optional[str] = None,
    target_email: Optional[str] = None,
    allow_team_inference: bool = True,
    refresh_subscription: bool = True,
) -> Dict[str, Any]:
    """
    统一收敛账号的 role_tag / subscription_type / workspace_id / token 状态。
    用于注册后、邀请后、手动刷新和上传前预校准。
    """
    with get_db() as db:
        account = crud.get_account_by_id(db, account_id)
        if not account:
            return {"success": False, "reason": "account_not_found"}

        changed = False
        role_tag = _normalize_role_tag(desired_role_tag or getattr(account, "role_tag", None))
        if role_tag != str(getattr(account, "role_tag", "") or "").strip().lower():
            account.role_tag = role_tag
            account.account_label = _normalize_account_label_from_role(role_tag)
            changed = True

        current_access_plan = _extract_jwt_plan(getattr(account, "access_token", None))
        current_id_plan = _extract_jwt_plan(getattr(account, "id_token", None))
        current_workspace = str(getattr(account, "workspace_id", "") or getattr(account, "account_id", "") or "").strip()
        skip_refresh = current_access_plan == "team" and bool(current_workspace)

        manager = TokenRefreshManager(proxy_url=proxy_url)
        refreshed = False
        if not skip_refresh:
            refresh_result = manager.refresh_account(account)
            refreshed = bool(getattr(refresh_result, "success", False))
            if refreshed:
                refreshed_access = str(getattr(refresh_result, "access_token", None) or "").strip()
                refreshed_access_plan = _extract_jwt_plan(refreshed_access)
                if current_id_plan == "team" and refreshed_access_plan == "free" and current_workspace:
                    logger.info(
                        "检测到 Team 账号 refresh 回落为 free，保留当前工作区上下文并跳过覆盖: account_id=%s email=%s workspace=%s",
                        account.id,
                        account.email,
                        current_workspace,
                    )
                else:
                    if refreshed_access:
                        account.access_token = refreshed_access
                    if getattr(refresh_result, "refresh_token", None):
                        account.refresh_token = refresh_result.refresh_token
                    if getattr(refresh_result, "expires_at", None):
                        account.expires_at = refresh_result.expires_at
                    account.last_refresh = utcnow_naive()
                    changed = True

        access_token = str(getattr(account, "access_token", "") or "").strip()
        candidates = _fetch_team_workspace_candidates(access_token, proxy_url)
        selected_workspace_id = _resolve_workspace_id(account)
        if candidates and allow_team_inference:
            selected_workspace_id = str(candidates[0].get("account_id") or "").strip() or selected_workspace_id
            token_plan = _extract_jwt_plan(access_token) or _extract_jwt_plan(getattr(account, "id_token", None))
            if selected_workspace_id and token_plan != "team" and str(getattr(account, "password", "") or "").strip():
                relogin_session = _bootstrap_session_token_by_relogin(account.id, proxy_url)
                if relogin_session and relogin_session != str(getattr(account, "session_token", "") or "").strip():
                    account.session_token = relogin_session
                    changed = True
            if selected_workspace_id and token_plan != "team":
                switched = _switch_workspace_and_capture_tokens(account, selected_workspace_id, proxy_url)
                switched_access_token = str(switched.get("access_token") or "").strip()
                switched_session_token = str(switched.get("session_token") or "").strip()
                if switched_access_token and switched_access_token != access_token:
                    account.access_token = switched_access_token
                    access_token = switched_access_token
                    changed = True
                if switched_session_token and switched_session_token != str(getattr(account, "session_token", "") or "").strip():
                    account.session_token = switched_session_token
                    changed = True
                token_plan = _extract_jwt_plan(access_token) or _extract_jwt_plan(getattr(account, "id_token", None))
            selected_role = str(candidates[0].get("role") or "").strip().lower()
            if selected_workspace_id and selected_workspace_id != str(getattr(account, "workspace_id", "") or "").strip():
                account.workspace_id = selected_workspace_id
                changed = True
            if selected_workspace_id and selected_workspace_id != str(getattr(account, "account_id", "") or "").strip():
                extra = account.extra_data if isinstance(account.extra_data, dict) else {}
                old_account_id = str(getattr(account, "account_id", "") or "").strip()
                if old_account_id and old_account_id != selected_workspace_id:
                    extra["personal_account_id"] = old_account_id
                extra["team_workspace_id"] = selected_workspace_id
                account.extra_data = extra
                account.account_id = selected_workspace_id
                changed = True
            if selected_role in {"owner", "admin", "manager"} and role_tag == RoleTag.NONE.value:
                account.role_tag = RoleTag.PARENT.value
                account.account_label = _normalize_account_label_from_role(RoleTag.PARENT.value)
                changed = True

        detail: Dict[str, Any] = {}
        if refresh_subscription:
            try:
                detail = check_subscription_status_detail(account, proxy_url) or {}
            except Exception as exc:
                detail = {"status": "free", "confidence": "low", "source": f"subscription_error:{exc}"}
        else:
            detail = {
                "status": str(getattr(account, "subscription_type", None) or "free").strip().lower() or "free",
                "confidence": "cached",
                "source": "db.subscription_type",
            }

        extra = account.extra_data if isinstance(account.extra_data, dict) else {}
        extra["runtime_token_plan"] = _extract_jwt_plan(getattr(account, "access_token", None))
        extra["runtime_id_plan"] = _extract_jwt_plan(getattr(account, "id_token", None))
        account.extra_data = extra
        changed = True

        status = _normalize_plan(detail.get("status") or detail.get("subscription_type"))
        if candidates and status == "free" and allow_team_inference:
            status = "team"
            detail = dict(detail)
            detail["source"] = str(detail.get("source") or "workspace_candidates")
            detail["confidence"] = str(detail.get("confidence") or "medium")

        if allow_team_inference and status == "free" and str(getattr(account, "role_tag", "") or "").strip().lower() == RoleTag.CHILD.value:
            extra = account.extra_data if isinstance(account.extra_data, dict) else {}
            runtime_token_plan = _extract_jwt_plan(getattr(account, "access_token", None))
            runtime_id_plan = _extract_jwt_plan(getattr(account, "id_token", None))
            has_team_binding = bool(
                candidates
                or str(extra.get("team_workspace_id") or "").strip()
                or runtime_token_plan == "team"
                or runtime_id_plan == "team"
            )
            if has_team_binding:
                status = "team"
                detail = dict(detail)
                detail["source"] = str(detail.get("source") or "child_team_binding")
                detail["confidence"] = str(detail.get("confidence") or "medium")

        if status in {"plus", "team"}:
            if account.subscription_type != status:
                account.subscription_type = status
                account.subscription_at = utcnow_naive()
                changed = True
        elif str(detail.get("confidence") or "").strip().lower() == "high":
            if getattr(account, "subscription_type", None):
                account.subscription_type = None
                account.subscription_at = None
                changed = True

        if target_email:
            normalized_target = str(target_email or "").strip().lower()
            if normalized_target and normalized_target == str(getattr(account, "email", "") or "").strip().lower():
                if account.role_tag != RoleTag.CHILD.value:
                    account.role_tag = RoleTag.CHILD.value
                    account.account_label = _normalize_account_label_from_role(RoleTag.CHILD.value)
                    changed = True

        if changed:
            db.commit()
            db.refresh(account)

        return {
            "success": True,
            "account_id": account.id,
            "email": account.email,
            "role_tag": account.role_tag,
            "account_label": account.account_label,
            "subscription_type": account.subscription_type or "free",
            "workspace_id": account.workspace_id or account.account_id or "",
            "token_refreshed": refreshed,
            "workspace_candidates": len(candidates),
            "detail": detail,
        }
