"""
CPA (Codex Protocol API) 上传功能
"""

import json
import logging
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlMime

from ...database.session import get_db
from ...database.models import Account
from ...config.settings import get_settings
from ..timezone_utils import utcnow_naive
from ...web.services.accounts_service import reconcile_account_runtime_state

logger = logging.getLogger(__name__)


def _normalize_cpa_auth_files_url(api_url: str) -> str:
    """将用户填写的 CPA 地址规范化为 auth-files 接口地址。"""
    normalized = (api_url or "").strip().rstrip("/")
    lower_url = normalized.lower()

    if not normalized:
        return ""

    if lower_url.endswith("/auth-files"):
        return normalized

    if lower_url.endswith("/v0/management") or lower_url.endswith("/management"):
        return f"{normalized}/auth-files"

    if lower_url.endswith("/v0"):
        return f"{normalized}/management/auth-files"

    return f"{normalized}/v0/management/auth-files"


def _build_cpa_headers(api_token: str, content_type: Optional[str] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_token}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _extract_cpa_error(response) -> str:
    error_msg = f"上传失败: HTTP {response.status_code}"
    try:
        error_detail = response.json()
        if isinstance(error_detail, dict):
            error_msg = error_detail.get("message", error_msg)
    except Exception:
        error_msg = f"{error_msg} - {response.text[:200]}"
    return error_msg


def _post_cpa_auth_file_multipart(upload_url: str, filename: str, file_content: bytes, api_token: str):
    mime = CurlMime()
    mime.addpart(
        name="file",
        data=file_content,
        filename=filename,
        content_type="application/json",
    )

    return cffi_requests.post(
        upload_url,
        multipart=mime,
        headers=_build_cpa_headers(api_token),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def _post_cpa_auth_file_raw_json(upload_url: str, filename: str, file_content: bytes, api_token: str):
    raw_upload_url = f"{upload_url}?name={quote(filename)}"
    return cffi_requests.post(
        raw_upload_url,
        data=file_content,
        headers=_build_cpa_headers(api_token, content_type="application/json"),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def generate_token_json(account: Account) -> dict:
    """
    生成 CPA 格式的 Token JSON

    Args:
        account: 账号模型实例

    Returns:
        CPA 格式的 Token 字典
    """
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    subscription_type = str(account.subscription_type or "free").strip().lower() or "free"
    role_tag = str(getattr(account, "role_tag", "") or "").strip().lower() or "none"
    workspace_id = str(account.workspace_id or account.account_id or "").strip()
    account_id = workspace_id or str(account.account_id or "").strip()

    return {
        "type": "codex",
        "email": account.email,
        "expired": account.expires_at.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.expires_at else "",
        "id_token": account.id_token or "",
        "account_id": account_id,
        "workspace_id": workspace_id,
        "access_token": account.access_token or "",
        "last_refresh": account.last_refresh.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.last_refresh else "",
        "refresh_token": account.refresh_token or "",
        "subscription_type": subscription_type,
        "plan_type": subscription_type,
        "role_tag": role_tag,
        "account_label": getattr(account, "account_label", None) or ("mother" if role_tag == "parent" else "child" if role_tag == "child" else "none"),
        "team_role": extra_data.get("team_role") or extra_data.get("workspace_role") or "",
        "auth_mode": extra_data.get("auth_mode") or "",
        "runtime_token_plan": extra_data.get("runtime_token_plan") or "",
        "runtime_id_plan": extra_data.get("runtime_id_plan") or "",
        "disabled": False,
    }


def upload_to_cpa(
    token_data: dict,
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> Tuple[bool, str]:
    """
    上传单个账号到 CPA 管理平台（不走代理）

    Args:
        token_data: Token JSON 数据
        proxy: 保留参数，不使用（CPA 上传始终直连）
        api_url: 指定 CPA API URL（优先于全局配置）
        api_token: 指定 CPA API Token（优先于全局配置）

    Returns:
        (成功标志, 消息或错误信息)
    """
    settings = get_settings()

    # 优先使用传入的参数，否则退回全局配置
    effective_url = api_url or settings.cpa_api_url
    effective_token = api_token or (settings.cpa_api_token.get_secret_value() if settings.cpa_api_token else "")

    # 仅当未指定服务时才检查全局启用开关
    if not api_url and not settings.cpa_enabled:
        return False, "CPA 上传未启用"

    if not effective_url:
        return False, "CPA API URL 未配置"

    if not effective_token:
        return False, "CPA API Token 未配置"

    upload_url = _normalize_cpa_auth_files_url(effective_url)

    filename = f"{token_data['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        response = _post_cpa_auth_file_multipart(
            upload_url,
            filename,
            file_content,
            effective_token,
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        if response.status_code in (404, 405, 415):
            logger.warning("CPA multipart 上传失败，尝试原始 JSON 回退: %s", response.status_code)
            fallback_response = _post_cpa_auth_file_raw_json(
                upload_url,
                filename,
                file_content,
                effective_token,
            )
            if fallback_response.status_code in (200, 201):
                return True, "上传成功"
            response = fallback_response

        return False, _extract_cpa_error(response)

    except Exception as e:
        logger.error(f"CPA 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_cpa(
    account_ids: List[int],
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> dict:
    """
    批量上传账号到 CPA 管理平台

    Args:
        account_ids: 账号 ID 列表
        proxy: 可选的代理 URL
        api_url: 指定 CPA API URL（优先于全局配置）
        api_token: 指定 CPA API Token（优先于全局配置）

    Returns:
        包含成功/失败统计和详情的字典
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": []
    }

    with get_db() as db:
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()

            if not account:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": None,
                    "success": False,
                    "error": "账号不存在"
                })
                continue

            # 检查是否已有 Token
            if not account.access_token:
                results["skipped_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": "缺少 Token"
                })
                continue

            try:
                reconcile_account_runtime_state(account.id)
                db.expire(account)
                db.refresh(account)
            except Exception as exc:
                logger.warning("CPA 批量上传前状态收敛失败: account_id=%s email=%s err=%s", account.id, account.email, exc)

            # 生成 Token JSON
            token_data = generate_token_json(account)

            # 上传
            success, message = upload_to_cpa(token_data, proxy, api_url=api_url, api_token=api_token)

            if success:
                # 更新数据库状态
                account.cpa_uploaded = True
                account.cpa_uploaded_at = utcnow_naive()
                db.commit()

                results["success_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": True,
                    "message": message
                })
            else:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": message
                })

    return results


def list_cpa_auth_files(api_url: str, api_token: str) -> Tuple[bool, Any, str]:
    """鍒楀嚭杩滅 CPA auth-files 娓呭崟銆?"""
    if not api_url:
        return False, None, "API URL 涓嶈兘涓虹┖"

    if not api_token:
        return False, None, "API Token 涓嶈兘涓虹┖"

    list_url = _normalize_cpa_auth_files_url(api_url)
    headers = _build_cpa_headers(api_token)

    try:
        response = cffi_requests.get(
            list_url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )
        if response.status_code != 200:
            return False, None, _extract_cpa_error(response)
        return True, response.json(), "ok"
    except cffi_requests.exceptions.ConnectionError as e:
        return False, None, f"鏃犳硶杩炴帴鍒版湇鍔″櫒: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, None, "杩炴帴瓒呮椂锛岃妫€鏌ョ綉缁滈厤缃?"
    except Exception as e:
        logger.error("鑾峰彇 CPA auth-files 娓呭崟寮傚父: %s", e)
        return False, None, f"鑾峰彇 auth-files 澶辫触: {str(e)}"


def count_ready_cpa_auth_files(payload: Any) -> int:
    """缁熻鍙敤浜庤ˉ璐у垽鏂殑璁よ瘉鏂囦欢鏁伴噺銆?"""
    if isinstance(payload, dict):
        files = payload.get("files", [])
    elif isinstance(payload, list):
        files = payload
    else:
        return 0

    ready_count = 0
    for item in files:
        if not isinstance(item, dict):
            continue

        status = str(item.get("status", "")).strip().lower()
        provider = str(item.get("provider") or item.get("type") or "").strip().lower()
        disabled = bool(item.get("disabled", False))
        unavailable = bool(item.get("unavailable", False))

        if disabled or unavailable:
            continue

        if provider != "codex":
            continue

        if status and status not in {"ready", "active"}:
            continue

        ready_count += 1

    return ready_count


def test_cpa_connection(api_url: str, api_token: str, proxy: str = None) -> Tuple[bool, str]:
    """
    测试 CPA 连接（不走代理）

    Args:
        api_url: CPA API URL
        api_token: CPA API Token
        proxy: 保留参数，不使用（CPA 始终直连）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"

    if not api_token:
        return False, "API Token 不能为空"

    test_url = _normalize_cpa_auth_files_url(api_url)
    headers = _build_cpa_headers(api_token)

    try:
        response = cffi_requests.get(
            test_url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code == 200:
            return True, "CPA 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Token 无效"
        if response.status_code == 403:
            return False, "连接成功，但服务端未启用远程管理或当前 Token 无权限"
        if response.status_code == 404:
            return False, "未找到 CPA auth-files 接口，请检查 API URL 是否填写为根地址、/v0/management 或完整 auth-files 地址"
        if response.status_code == 503:
            return False, "连接成功，但服务端认证管理器不可用"

        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
