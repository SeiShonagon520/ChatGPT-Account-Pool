"""Safe credential checks used by the account-maintenance tasks.

The checks in this module deliberately distinguish an invalid credential from
an inconclusive HTTP failure.  In particular, a rate limit, Cloudflare page or
temporary network failure must never be used as evidence that an account was
banned.
"""
from __future__ import annotations

import ast
import base64
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests

from .constants import (
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OAUTH_TOKEN_URL,
    OPENAI_AUTH,
)
from .environment_profile import PROTOCOL_CHROME_IMPERSONATE, PROTOCOL_CHROME_VERSION
from .oauth import OAuthStart, generate_oauth_url, submit_callback_url


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_text(response: Any, payload: dict[str, Any]) -> str:
    try:
        text = str(response.text or "")
    except Exception:
        text = ""
    if payload:
        text = f"{text} {json.dumps(payload, ensure_ascii=False)}"
    return text[:1_000]


def _is_cloudflare_challenge_response(response: Any) -> bool:
    """Distinguish an edge challenge from an OpenAI credential rejection."""
    try:
        raw_headers = getattr(response, "headers", {}) or {}
        headers = {
            str(key).strip().lower(): str(value or "").strip().lower()
            for key, value in raw_headers.items()
        }
    except Exception:
        headers = {}
    if headers.get("cf-mitigated") == "challenge":
        return True

    content_type = headers.get("content-type", "")
    try:
        body = str(getattr(response, "text", "") or "").lower()
    except Exception:
        body = ""
    if "text/html" in content_type and (
        "<html" in body
        or "cloudflare" in body
        or "just a moment" in body
        or "challenge-platform" in body
    ):
        return True
    return False


def _decode_access_token_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _access_token_expired_locally(token: str) -> bool:
    payload = _decode_access_token_payload(token)
    try:
        expires_at = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return False
    return bool(expires_at and expires_at <= time.time())


def _chatgpt_account_id_from_access_token(token: str) -> str:
    payload = _decode_access_token_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return ""
    return str(auth.get("chatgpt_account_id") or "").strip()


def _response_detail_code(response: Any) -> str:
    payload = _response_payload(response)
    error = payload.get("error")
    detail = payload.get("detail")
    candidates = [
        detail.get("code") if isinstance(detail, dict) else detail,
        error.get("code") if isinstance(error, dict) else error,
        payload.get("code"),
        error.get("type") if isinstance(error, dict) else None,
        payload.get("type"),
    ]
    return next(
        (str(value).strip() for value in candidates if str(value or "").strip()),
        "",
    )


_BAN_CODES = frozenset({"account_deactivated", "account_suspended", "account_banned"})
_BAN_CODE_PATTERN = re.compile(
    r"(?<![a-z0-9_])(account_(?:deactivated|suspended|banned))(?![a-z0-9_])",
    re.IGNORECASE,
)


class ChatGPTAccountBannedDuringRelogin(RuntimeError):
    """The saved web-session login flow explicitly reported an account ban."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = str(code or "").strip().lower()


def _explicit_ban_code(value: Any) -> str:
    """Return an exact OpenAI ban code from structured auth data only."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in {"banned", "deactivated", "suspended"} and child is True:
                return f"account_{normalized_key}"
            code = _explicit_ban_code(child)
            if code:
                return code
        return ""
    if isinstance(value, (list, tuple)):
        for child in value:
            code = _explicit_ban_code(child)
            if code:
                return code
        return ""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    normalized = text.lower()
    if normalized in _BAN_CODES:
        return normalized
    if text[:1] in {"{", "["}:
        try:
            return _explicit_ban_code(json.loads(text))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    match = _BAN_CODE_PATTERN.search(text)
    return str(match.group(1) or "").lower() if match else ""


def _text_has_explicit_ban_marker(value: Any) -> bool:
    return bool(_explicit_ban_code(value))


def _has_explicit_ban_marker(response: Any, payload: dict[str, Any] | None = None) -> bool:
    resolved = payload if payload is not None else _response_payload(response)
    return bool(_explicit_ban_code(resolved))


def _is_invalid_refresh_response(status_code: int, payload: dict[str, Any], text: str) -> bool:
    """Return true when the saved refresh credential must be renewed."""
    # For account maintenance, OpenAI HTTP 401/403 are stale-credential signals:
    # 401 is unauthorized (session ended / revoked token), 403 is forbidden.
    if status_code in {401, 403}:
        return True
    if status_code != 400:
        return False
    raw_error = payload.get("error")
    error = (
        raw_error
        if isinstance(raw_error, str)
        else str((raw_error or {}).get("code") or "")
    ).strip().lower()
    error_msg = str((raw_error or {}).get("message") or "") if isinstance(raw_error, dict) else ""
    description = str(
        payload.get("error_description")
        or error_msg
        or payload.get("message")
        or text
        or ""
    ).lower()
    return (
        error in {"invalid_grant", "invalid_token", "refresh_token_invalidated", "unauthorized"}
        or any(
            marker in description
            for marker in (
                "session has ended",
                "session_ended",
                "refresh_token_invalidated",
                "invalid_grant",
                "invalid_token",
                "token has been revoked",
            )
        )
        or (
            "refresh" in description
            and any(marker in description for marker in ("invalid", "expired", "revoked"))
        )
    )


def _cookie_map(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if value not in (None, "")}
    text = str(raw or "").strip()
    if not text:
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return {str(key): str(value) for key, value in parsed.items() if value not in (None, "")}
    return {
        name.strip(): value.strip()
        for part in text.split(";")
        if "=" in part
        for name, value in [part.split("=", 1)]
        if name.strip() and value.strip()
    }


_OAUTH_PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
    ),
}
_OAUTH_JSON_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": OPENAI_AUTH,
    "user-agent": _OAUTH_PAGE_HEADERS["user-agent"],
}


MAX_PROTOCOL_LOGIN_CONCURRENCY = 50


def protocol_login_concurrency_limit() -> int:
    raw = os.getenv("CHATGPT_PROTOCOL_LOGIN_CONCURRENCY", "50")
    try:
        return min(max(int(raw), 1), MAX_PROTOCOL_LOGIN_CONCURRENCY)
    except (TypeError, ValueError):
        return MAX_PROTOCOL_LOGIN_CONCURRENCY


_PROTOCOL_LOGIN_SEMAPHORE = threading.BoundedSemaphore(
    protocol_login_concurrency_limit()
)
_PROTOCOL_LOGIN_PROFILE_LOCK = threading.Lock()
_PROTOCOL_LOGIN_PROFILE_POOL: Any | None = None
_ROUTER_ENQUEUE_PATTERN = re.compile(
    r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
    re.DOTALL,
)


def _decode_react_router_payload(payload: Any) -> Any:
    """Decode React Router's flattened/devalue loader serialization.

    Account chooser data is not embedded as ordinary JSON.  Objects encode
    both keys and values as indexes into one top-level list.
    """
    if not isinstance(payload, list):
        return None
    resolved: dict[int, Any] = {}

    def resolve(reference: Any) -> Any:
        if not isinstance(reference, int) or isinstance(reference, bool):
            return reference
        if reference < 0 or reference >= len(payload):
            return None
        if reference in resolved:
            return resolved[reference]
        value = payload[reference]
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            resolved[reference] = output
            for encoded_key, encoded_value in value.items():
                key_text = str(encoded_key)
                if not key_text.startswith("_") or not key_text[1:].isdigit():
                    continue
                key = resolve(int(key_text[1:]))
                if key is not None:
                    output[str(key)] = resolve(encoded_value)
            return output
        if isinstance(value, list):
            output_list: list[Any] = []
            resolved[reference] = output_list
            output_list.extend(resolve(item) for item in value)
            return output_list
        resolved[reference] = value
        return value

    return resolve(0)


def _react_router_payloads_from_html(html: str) -> list[Any]:
    chunks: list[str] = []
    decoded: list[Any] = []
    for match in _ROUTER_ENQUEUE_PATTERN.finditer(str(html or "")):
        try:
            chunk = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(chunk, str):
            continue
        chunks.append(chunk)
        try:
            value = json.loads(chunk)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        decoded.append(_decode_react_router_payload(value) if isinstance(value, list) else value)
    if len(chunks) > 1:
        try:
            combined = json.loads("".join(chunks))
        except (TypeError, ValueError, json.JSONDecodeError):
            combined = None
        if combined is not None:
            decoded.append(
                _decode_react_router_payload(combined)
                if isinstance(combined, list)
                else combined
            )
    return [item for item in decoded if item is not None]


def _walk_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _login_identifier(container: dict[str, Any]) -> str:
    username = container.get("username")
    if isinstance(username, dict):
        username = username.get("value")
    if not isinstance(username, str) or not username.strip():
        username = container.get("email")
    return str(username or "").strip().casefold()


def _extract_unified_session_id(html: str, email: str = "") -> str:
    """Return the chooser's unified session, never the outer OAuth session."""
    wanted = str(email or "").strip().casefold()
    fallback = ""
    for payload in _react_router_payloads_from_html(html):
        for container in _walk_objects(payload):
            sessions = container.get("unified_sessions")
            if not isinstance(sessions, list):
                continue
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                session_id = str(
                    item.get("id") or item.get("session_id") or ""
                ).strip()
                if not session_id:
                    continue
                username = _login_identifier(item)
                if wanted and username == wanted:
                    return session_id
                if not fallback:
                    fallback = session_id
            if fallback and not wanted:
                return fallback
    if fallback and not wanted:
        return fallback

    # Current Auth builds may expose the chosen session directly in a route
    # loader instead of wrapping it in ``unified_sessions``.  Requiring the
    # matching email prevents the outer OAuth client session (also named
    # authsess_*) from being selected by accident.
    if wanted:
        for payload in _react_router_payloads_from_html(html):
            for container in _walk_objects(payload):
                username = _login_identifier(container)
                session_id = str(
                    container.get("session_id") or container.get("id") or ""
                ).strip()
                if username == wanted and session_id.startswith("authsess_"):
                    return session_id
    return ""


def _oauth_callback_target(url: str, oauth_start: OAuthStart) -> bool:
    candidate = urlparse(str(url or "").strip())
    target = urlparse(oauth_start.redirect_uri)
    return bool(
        candidate.scheme == target.scheme
        and candidate.netloc == target.netloc
        and candidate.path.rstrip("/") == target.path.rstrip("/")
    )


def _oauth_callback_error(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(str(url or "")).query)
    error = str((query.get("error") or [""])[0]).strip()
    description = str((query.get("error_description") or [""])[0]).strip()
    return error, description


def _continue_url_from_payload(payload: Any) -> str:
    if not isinstance(payload, (dict, list)):
        return ""
    preferred = ("continue_url", "external_url", "redirect_url", "url")
    for container in _walk_objects(payload):
        for key in preferred:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _requires_phone_verification(payload: Any, continue_url: str = "") -> bool:
    """Detect the Codex OAuth phone gate without starting an SMS flow."""
    path = urlparse(str(continue_url or "").strip()).path.rstrip("/")
    if path.endswith("/add-phone"):
        return True
    if not isinstance(payload, (dict, list)):
        return False
    for container in _walk_objects(payload):
        page = container.get("page")
        if isinstance(page, dict) and str(page.get("type") or "").strip() == "add_phone":
            return True
        if str(container.get("page_type") or "").strip() == "add_phone":
            return True
    return False


def _workspace_and_org_from_payload(payload: Any) -> tuple[str, str, str]:
    workspace_id = ""
    org_id = ""
    project_id = ""
    for container in _walk_objects(payload):
        workspaces = container.get("workspaces")
        if not workspace_id and isinstance(workspaces, list):
            for workspace in workspaces:
                if isinstance(workspace, dict):
                    workspace_id = str(
                        workspace.get("id") or workspace.get("workspace_id") or ""
                    ).strip()
                    if workspace_id:
                        break
        organizations = container.get("organizations") or container.get("orgs")
        if not org_id and isinstance(organizations, list):
            for organization in organizations:
                if not isinstance(organization, dict):
                    continue
                org_id = str(
                    organization.get("id")
                    or organization.get("org_id")
                    or organization.get("organization_id")
                    or ""
                ).strip()
                projects = organization.get("projects")
                if isinstance(projects, list):
                    for project in projects:
                        if isinstance(project, dict):
                            project_id = str(
                                project.get("id") or project.get("project_id") or ""
                            ).strip()
                            if project_id:
                                break
                if org_id:
                    break
    return workspace_id, org_id, project_id


def _oauth_api_headers(
    *,
    referer: str,
    device_id: str,
    sentinel_client=None,
) -> dict[str, str]:
    headers = {
        **_OAUTH_JSON_HEADERS,
        "referer": referer or OPENAI_AUTH,
        "oai-device-id": device_id,
    }
    if sentinel_client is not None:
        headers.update(sentinel_client.build_headers(device_id, "authorize_continue"))
    return headers


def _select_existing_auth_session(
    session,
    *,
    chooser_url: str,
    chooser_html: str,
    email: str,
    device_id: str,
    sentinel_client,
    proxies: dict | None,
) -> dict[str, Any]:
    session_id = _extract_unified_session_id(chooser_html, email)
    if not session_id:
        raise RuntimeError("账号选择页未找到与当前邮箱匹配的登录会话")
    response = session.post(
        f"{OPENAI_AUTH}/api/accounts/session/select",
        json={"session_id": session_id},
        headers=_oauth_api_headers(
            referer=chooser_url,
            device_id=device_id,
            sentinel_client=sentinel_client,
        ),
        allow_redirects=False,
        proxies=proxies,
    )
    payload = _response_payload(response)
    ban_code = _explicit_ban_code(payload)
    if ban_code:
        raise ChatGPTAccountBannedDuringRelogin(
            f"网页登录明确返回 {ban_code}",
            code=ban_code,
        )
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400 or payload.get("error"):
        raise RuntimeError(f"选择已登录账号失败（HTTP {status or '-'}）")
    return {
        "payload": payload,
        "location": str(
            getattr(response, "headers", {}).get("location") or ""
        ).strip(),
    }


def _authorization_code_via_account_selection(
    session,
    *,
    oauth_start: OAuthStart,
    email: str,
    device_id: str,
    sentinel_client,
    proxies: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Complete the current choose-account/workspace flow using HTTP only."""
    current = oauth_start.auth_url
    referer = OPENAI_AUTH
    selection: dict[str, Any] | None = None
    for _ in range(20):
        if cancel_check and cancel_check():
            raise TimeoutError("OAuth 授权已取消或超时")
        response = session.get(
            current,
            headers={**_OAUTH_PAGE_HEADERS, "referer": referer},
            allow_redirects=False,
            proxies=proxies,
        )
        payload = _response_payload(response)
        ban_code = _explicit_ban_code(payload)
        if ban_code:
            raise ChatGPTAccountBannedDuringRelogin(
                f"网页登录明确返回 {ban_code}",
                code=ban_code,
            )
        location = str(
            getattr(response, "headers", {}).get("location") or ""
        ).strip()
        if location:
            target = urljoin(current, location)
            if _oauth_callback_target(target, oauth_start):
                error, description = _oauth_callback_error(target)
                if error:
                    raise RuntimeError(
                        f"Codex OAuth 返回 {error}"
                        f"{': ' + description if description else ''}"
                    )
                return target
            referer, current = current, target
            continue

        final_url = str(getattr(response, "url", "") or current)
        final_path = urlparse(final_url).path.rstrip("/")
        body = str(getattr(response, "text", "") or "")
        if final_path.endswith("/choose-an-account") or "unified_sessions" in body:
            selection = _select_existing_auth_session(
                session,
                chooser_url=final_url,
                chooser_html=body,
                email=email,
                device_id=device_id,
                sentinel_client=sentinel_client,
                proxies=proxies,
            )
            break
        if final_path.endswith("/error"):
            raise RuntimeError("Codex OAuth 进入错误页，当前网页登录会话不可授权")
        if final_path.endswith("/email-verification"):
            raise RuntimeError("Codex OAuth 要求重新验证邮箱，当前会话不能静默换取 RT")
        raise RuntimeError(
            f"Codex OAuth 停在 {final_path or '/'}（HTTP "
            f"{int(getattr(response, 'status_code', 0) or 0) or '-'}）"
        )

    if selection is None:
        raise RuntimeError("Codex OAuth 未进入账号选择页")

    selected_payload = selection["payload"]
    next_url = str(selection.get("location") or "").strip()
    if not next_url:
        next_url = _continue_url_from_payload(selected_payload)
    if next_url:
        next_url = urljoin(OPENAI_AUTH, next_url)
    if _requires_phone_verification(selected_payload, next_url):
        raise RuntimeError(
            "Codex OAuth 要求手机号验证，本次 RT 获取失败（未执行短信接码）"
        )

    dump_response = session.get(
        f"{OPENAI_AUTH}/api/accounts/client_auth_session_dump",
        headers={**_OAUTH_JSON_HEADERS, "referer": next_url or OPENAI_AUTH},
        proxies=proxies,
    )
    dump_payload = _response_payload(dump_response)
    if _requires_phone_verification([selected_payload, dump_payload], next_url):
        raise RuntimeError(
            "Codex OAuth 要求手机号验证，本次 RT 获取失败（未执行短信接码）"
        )
    workspace_id, org_id, project_id = _workspace_and_org_from_payload(
        [selected_payload, dump_payload]
    )
    api_headers = _oauth_api_headers(
        referer=next_url or OPENAI_AUTH,
        device_id=device_id,
    )
    if workspace_id:
        workspace_response = session.post(
            f"{OPENAI_AUTH}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=api_headers,
            allow_redirects=False,
            proxies=proxies,
        )
        workspace_payload = _response_payload(workspace_response)
        location = str(
            getattr(workspace_response, "headers", {}).get("location") or ""
        ).strip()
        next_url = location or _continue_url_from_payload(workspace_payload) or next_url
        _, selected_org, selected_project = _workspace_and_org_from_payload(
            [workspace_payload, dump_payload]
        )
        org_id = org_id or selected_org
        project_id = project_id or selected_project
    if org_id and not next_url:
        organization_payload: dict[str, str] = {"org_id": org_id}
        if project_id:
            organization_payload["project_id"] = project_id
        organization_response = session.post(
            f"{OPENAI_AUTH}/api/accounts/organization/select",
            json=organization_payload,
            headers=api_headers,
            allow_redirects=False,
            proxies=proxies,
        )
        organization_data = _response_payload(organization_response)
        next_url = str(
            getattr(organization_response, "headers", {}).get("location") or ""
        ).strip() or _continue_url_from_payload(organization_data)

    if next_url:
        current = urljoin(OPENAI_AUTH, next_url)
        referer = OPENAI_AUTH
        for _ in range(12):
            if _oauth_callback_target(current, oauth_start):
                return current
            response = session.get(
                current,
                headers={**_OAUTH_PAGE_HEADERS, "referer": referer},
                allow_redirects=False,
                proxies=proxies,
            )
            location = str(
                getattr(response, "headers", {}).get("location") or ""
            ).strip()
            payload = _response_payload(response)
            candidate = location or _continue_url_from_payload(payload)
            if not candidate:
                break
            referer, current = current, urljoin(current, candidate)

    oauth_params = {
        key: values[0]
        for key, values in parse_qs(urlparse(oauth_start.auth_url).query).items()
        if values and str(values[0]).strip()
    }
    final_url = f"{OPENAI_AUTH}/api/oauth/oauth2/auth?{urlencode(oauth_params)}"
    final_response = session.get(
        final_url,
        headers={**_OAUTH_PAGE_HEADERS, "referer": OPENAI_AUTH},
        allow_redirects=False,
        proxies=proxies,
    )
    location = str(
        getattr(final_response, "headers", {}).get("location") or ""
    ).strip()
    if location:
        target = urljoin(final_url, location)
        if _oauth_callback_target(target, oauth_start):
            error, description = _oauth_callback_error(target)
            if error:
                raise RuntimeError(
                    f"Codex OAuth 返回 {error}"
                    f"{': ' + description if description else ''}"
                )
            return target
    raise RuntimeError("已选择登录账号，但 Codex OAuth 未返回 authorization code")


def _authorization_code_from_session(
    session,
    *,
    oauth_start: OAuthStart,
    proxies: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Return the callback URL using an existing signed-in ChatGPT session.

    The redirect to localhost is intentionally not followed.  It is the OAuth
    callback value, not a local service dependency.
    """
    current = oauth_start.auth_url
    for _ in range(15):
        if cancel_check and cancel_check():
            raise TimeoutError("OAuth 授权已取消或超时")
        response = session.get(current, allow_redirects=False, proxies=proxies)
        location = str(getattr(response, "headers", {}).get("location") or "").strip()
        if not location:
            ban_code = _explicit_ban_code(_response_payload(response))
            if ban_code:
                raise ChatGPTAccountBannedDuringRelogin(
                    f"网页登录明确返回 {ban_code}",
                    code=ban_code,
                )
            raise RuntimeError(f"会话未自动授权（HTTP {int(getattr(response, 'status_code', 0) or 0) or '-'}）")
        target = urljoin(current, location)
        if _oauth_callback_target(target, oauth_start):
            error, description = _oauth_callback_error(target)
            if error:
                raise RuntimeError(
                    f"会话未自动授权（{error}"
                    f"{': ' + description if description else ''}）"
                )
            return target
        current = target
    raise RuntimeError("OAuth 授权重定向次数过多")


def mint_chatgpt_refresh_token_from_session(
    cookies: Any,
    *,
    client_id: str = "",
    proxy: str | None = None,
    session=None,
    email: str = "",
    device_id: str = "",
    sentinel_client=None,
    prefer_account_selection: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    authorization_attempts: int = 3,
    retry_delay_seconds: float = 2.0,
) -> dict[str, Any]:
    """Use saved web-session cookies to mint a new Codex OAuth RT.

    This is the re-login/recovery path for records created before the protocol
    registrar started persisting RTs.  A missing or expired web session is an
    inconclusive result, never a ban signal.
    """
    cookie_values = _cookie_map(cookies)
    if not cookie_values:
        return {"state": "missing", "message": "账号未保存可复用的网页登录会话", "tokens": {}}

    resolved_client_id = str(client_id or CODEX_CLIENT_ID).strip() or CODEX_CLIENT_ID
    proxies = {"http": proxy, "https": proxy} if proxy else None
    owns_session = session is None
    session = session or requests.Session(
        impersonate=PROTOCOL_CHROME_IMPERSONATE,
        timeout=30,
    )
    try:
        # A live registration session already has host/path scoped cookies.
        # Re-adding the flattened snapshot creates domainless duplicates and
        # can shadow unified_session_manifest / usc_* on auth.openai.com.
        # Only hydrate a newly-created recovery session from the snapshot.
        if owns_session:
            for name, value in cookie_values.items():
                if hasattr(session.cookies, "set"):
                    session.cookies.set(name, value)
        attempts = min(max(int(authorization_attempts or 1), 1), 5)
        delay = max(float(retry_delay_seconds or 0), 0.0)
        oauth_start: OAuthStart | None = None
        callback_url = ""
        if not prefer_account_selection:
            for attempt in range(attempts):
                oauth_start = generate_oauth_url(
                    redirect_uri=CODEX_REDIRECT_URI,
                    scope=CODEX_SCOPE,
                    client_id=resolved_client_id,
                    prompt="none",
                )
                try:
                    callback_url = _authorization_code_from_session(
                        session,
                        oauth_start=oauth_start,
                        proxies=proxies,
                        cancel_check=cancel_check,
                    )
                    break
                except ChatGPTAccountBannedDuringRelogin:
                    raise
                except RuntimeError as exc:
                    # A newly-created ChatGPT web session may take a few seconds
                    # before Hydra recognizes it for the Codex public client.
                    # During that window /oauth/authorize returns an HTML 200
                    # without a callback.  Retry with a fresh state/PKCE pair; do
                    # not retry explicit bans or a token exchange that may have
                    # already consumed an authorization code.
                    if "会话未自动授权" not in str(exc):
                        raise
                    if attempt + 1 >= attempts:
                        # The silent branch is exhausted. Continue below with the
                        # explicit account-selection protocol and a fresh PKCE.
                        break
                    if cancel_check and cancel_check():
                        raise TimeoutError("OAuth 授权已取消或超时") from exc
                    if delay:
                        time.sleep(min(delay * (attempt + 1), 8.0))
        if oauth_start is None or not callback_url:
            oauth_start = generate_oauth_url(
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                client_id=resolved_client_id,
                prompt="login",
            )
            resolved_device_id = str(device_id or "").strip()
            if not resolved_device_id:
                try:
                    resolved_device_id = str(session.cookies.get("oai-did") or "").strip()
                except Exception:
                    resolved_device_id = ""
            resolved_device_id = resolved_device_id or str(uuid.uuid4())
            owns_sentinel = sentinel_client is None
            if sentinel_client is None:
                from .protocol_register import OpenAISentinelClient

                sentinel_client = OpenAISentinelClient(
                    session,
                    user_agent=_OAUTH_PAGE_HEADERS["user-agent"],
                    proxy=proxy,
                )
            try:
                callback_url = _authorization_code_via_account_selection(
                    session,
                    oauth_start=oauth_start,
                    email=email,
                    device_id=resolved_device_id,
                    sentinel_client=sentinel_client,
                    proxies=proxies,
                    cancel_check=cancel_check,
                )
            finally:
                if owns_sentinel:
                    try:
                        sentinel_client.close()
                    except Exception:
                        pass
        payload = json.loads(
            submit_callback_url(
                callback_url=callback_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=oauth_start.redirect_uri,
                client_id=oauth_start.client_id,
                token_url=OAUTH_TOKEN_URL,
                proxy_url=proxy,
                session=session,
            )
        )
        access_token = str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if access_token and refresh_token:
            tokens = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": resolved_client_id,
            }
            if payload.get("id_token"):
                tokens["id_token"] = str(payload["id_token"])
            return {"state": "valid", "message": "已通过网页登录会话获取新的 RT", "tokens": tokens}
        return {
            "state": "unknown",
            "message": "Codex OAuth 换取 token 成功，但响应未包含 RT",
            "tokens": {},
        }
    except ChatGPTAccountBannedDuringRelogin as exc:
        return {
            "state": "banned",
            "message": str(exc),
            "confirmed_ban_code": exc.code,
            "tokens": {},
        }
    except Exception as exc:
        return {"state": "unknown", "message": f"会话换取 RT 未完成: {exc}", "tokens": {}}
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:
                pass


def refresh_chatgpt_tokens(
    refresh_token: str,
    *,
    client_id: str = "",
    proxy: str | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Exchange a ChatGPT OAuth refresh token without exposing it in logs.

    A successful refresh can rotate both access and refresh tokens.  The
    caller persists the returned values atomically with the check status.
    """
    token = str(refresh_token or "").strip()
    if not token:
        return {"state": "missing", "message": "账号未保存 refresh token", "tokens": {}}

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": str(client_id or CODEX_CLIENT_ID).strip() or CODEX_CLIENT_ID,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            proxies=proxies,
            timeout=max(float(timeout_seconds or 30), 1.0),
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
        )
    except Exception as exc:
        return {"state": "unknown", "message": f"RT 校验网络异常: {exc}", "tokens": {}}

    payload = _response_payload(response)
    status_code = int(getattr(response, "status_code", 0) or 0)
    access_token = str(payload.get("access_token") or "").strip()
    if 200 <= status_code < 300 and access_token:
        tokens = {key: str(payload[key]).strip() for key in ("access_token", "refresh_token", "id_token") if payload.get(key)}
        return {"state": "valid", "message": "RT 有效", "tokens": tokens}

    text = _response_text(response, payload)
    if _is_invalid_refresh_response(status_code, payload, text):
        return {"state": "invalid", "message": "RT 已失效", "tokens": {}}
    return {
        "state": "unknown",
        "message": f"RT 校验未确认（HTTP {status_code or '-'}）",
        "tokens": {},
    }


def _build_protocol_login_otp_callback(
    email: str,
    provider_accounts: list[dict[str, Any]] | None,
    *,
    proxy: str | None = None,
    deadline: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """Reuse the account's original mailbox to read a new protocol-login OTP."""
    normalized_email = str(email or "").strip().lower()
    candidates = [item for item in list(provider_accounts or []) if isinstance(item, dict)]
    provider_account = next(
        (
            item
            for item in candidates
            if str(item.get("provider_type") or "mailbox") == "mailbox"
            and str(item.get("login_identifier") or "").strip().lower() == normalized_email
        ),
        None,
    )
    if provider_account is None:
        provider_account = next(
            (item for item in candidates if str(item.get("provider_type") or "mailbox") == "mailbox"),
            None,
        )
    if provider_account is None:
        return None

    from core.base_mailbox import MailboxAccount

    provider_name = str(provider_account.get("provider_name") or "").strip().lower()
    credentials = dict(provider_account.get("credentials") or {})
    metadata = dict(provider_account.get("metadata") or {})
    mailbox_account = MailboxAccount(
        # A local Microsoft mailbox may use plus-addressing.  Keep the
        # account's exact address here so the provider ignores an OTP sent to
        # a different concurrently registered child address.
        email=str(email).strip(),
        account_id=str(metadata.get("account_id") or ""),
        extra={"provider_account": provider_account},
    )
    if provider_name == "local_ms_pool":
        from core.local_ms_mailbox import LocalMicrosoftMailboxPool

        mailbox = LocalMicrosoftMailboxPool(pool_text="", allow_reuse=True, proxy=proxy)
    elif provider_name == "api_mailbox":
        from core.api_mailbox import ApiMailboxPool

        mailbox = ApiMailboxPool(pool_text="", allow_reuse=True, proxy=proxy)
    elif provider_name == "domain_inbucket":
        from core.inbucket_domain_mailbox import InbucketDomainMailbox

        inbucket_api_url = str(
            os.getenv("CHATGPT_INBUCKET_API_URL")
            or os.getenv("CHATGPT_LOGIN_INBUCKET_API_URL")
            or credentials.get("inbucket_api_url")
            or ""
        ).strip()
        mailbox = InbucketDomainMailbox(
            domain=str(credentials.get("domain") or ""),
            api_url=inbucket_api_url,
        )
    else:
        return None

    before_ids = mailbox.get_current_ids(mailbox_account)

    def wait_for_login_code() -> str:
        remaining = 180.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("协议登录等待验证码前已超过总时限")

        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def read_code() -> None:
            try:
                code = mailbox.wait_for_code(
                    mailbox_account,
                    keyword="",
                    before_ids=before_ids,
                    timeout=max(1, min(180, math.ceil(remaining))),
                )
                result_queue.put((True, code))
            except BaseException as exc:
                result_queue.put((False, exc))

        reader = threading.Thread(
            target=read_code,
            daemon=True,
            name="chatgpt-login-otp",
        )
        reader.start()
        while reader.is_alive():
            if cancel_check and cancel_check():
                raise RuntimeError("协议登录已取消")
            remaining = 180.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("协议登录等待验证码超过总时限")
            reader.join(timeout=min(0.5, remaining))
        succeeded, value = result_queue.get_nowait()
        if succeeded:
            return str(value or "")
        raise value  # type: ignore[misc]

    return wait_for_login_code


def _next_protocol_login_profile():
    from .environment_profile import ProtocolEnvironmentProfile

    return ProtocolEnvironmentProfile.desktop_us_en_firefox_v1()


def _login_with_registration_protocol(
    email: str,
    password: str,
    *,
    otp_callback: Callable[[], str] | None,
    totp_secret: str = "",
    proxy: str | None,
    deadline: float,
    cancel_check: Callable[[], bool],
    log_callback: Callable[[str], None] | None,
    proxy_rotate_callback: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Reuse the registration protocol session to log in an existing account."""
    request_timeout = max(min(deadline - time.monotonic(), 30.0), 1.0)
    from .protocol_register import ChatGPTProtocolRegister

    worker = ChatGPTProtocolRegister(
        proxy=proxy,
        otp_callback=otp_callback,
        totp_secret=totp_secret,
        log_fn=log_callback,
        cancel_check=cancel_check,
        proxy_rotate_callback=proxy_rotate_callback,
        request_timeout=request_timeout,
        profile=_next_protocol_login_profile(),
    )
    return worker.login(email=email, password=password)


def login_chatgpt_with_protocol(
    email: str,
    password: str,
    *,
    provider_accounts: list[dict[str, Any]] | None = None,
    totp_secret: str = "",
    proxy: str | None = None,
    timeout_seconds: float = 240,
    cancel_check: Callable[[], bool] | None = None,
    log_callback: Callable[[str], None] | None = None,
    proxy_rotate_callback: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Obtain fresh credentials through the direct HTTP login protocol."""
    normalized_email = str(email or "").strip()
    normalized_password = str(password or "")
    normalized_totp_secret = str(totp_secret or "").strip()
    if not normalized_email:
        return {
            "state": "missing_mailbox",
            "message": "账号缺少邮箱地址，无法恢复登录",
            "tokens": {},
        }
    if not normalized_password:
        return {"state": "invalid", "message": "账号缺少协议登录所需的邮箱或密码", "tokens": {}}

    timeout_seconds = max(float(timeout_seconds or 240), 0.01)
    deadline = float("inf")

    def user_cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    def stopped() -> bool:
        return user_cancelled() or time.monotonic() >= deadline

    acquired = False
    try:
        while not acquired:
            if user_cancelled():
                return {"state": "cancelled", "message": "协议登录已取消", "tokens": {}}
            acquired = _PROTOCOL_LOGIN_SEMAPHORE.acquire(timeout=0.5)
        deadline = time.monotonic() + timeout_seconds
        otp_callback = None
        # A TOTP-enabled account logs in with password + authenticator code.
        # Only prepare a mailbox reader as a compatibility fallback for old
        # records that do not have a saved secret.
        if not normalized_totp_secret:
            otp_callback = _build_protocol_login_otp_callback(
                normalized_email,
                provider_accounts,
                proxy=proxy,
                deadline=deadline,
                cancel_check=user_cancelled,
            )
        if not normalized_totp_secret and otp_callback is None:
            return {
                "state": "missing_mailbox",
                "message": "账号既没有保存 TOTP，也缺少可复用的验证邮箱，无法协议登录",
                "tokens": {},
            }
        result = _login_with_registration_protocol(
            normalized_email,
            normalized_password,
            otp_callback=otp_callback,
            totp_secret=normalized_totp_secret,
            proxy=proxy,
            deadline=deadline,
            cancel_check=stopped,
            log_callback=log_callback,
            proxy_rotate_callback=proxy_rotate_callback,
        )
    except ChatGPTAccountBannedDuringRelogin as exc:
        return {
            "state": "banned",
            "message": f"协议登录明确确认账号已封禁: {exc}",
            "confirmed_ban_code": exc.code,
            "tokens": {},
        }
    except Exception as exc:
        if user_cancelled():
            return {"state": "cancelled", "message": "协议登录已取消", "tokens": {}}
        return {"state": "invalid", "message": f"协议登录失败: {exc}", "tokens": {}}
    finally:
        if acquired:
            _PROTOCOL_LOGIN_SEMAPHORE.release()

    if time.monotonic() >= deadline:
        return {
            "state": "invalid",
            "message": f"协议登录超过单账号总时限 ({int(timeout_seconds)}s)",
            "tokens": {},
        }

    access_token = str(result.get("access_token") or "").strip()
    if not access_token:
        return {"state": "invalid", "message": "协议登录未返回新的 access token", "tokens": {}}
    tokens = {
        key: value
        for key, value in {
            "access_token": access_token,
            "refresh_token": str(result.get("refresh_token") or "").strip(),
            "id_token": str(result.get("id_token") or "").strip(),
            "client_id": str(result.get("client_id") or "").strip(),
            "session_token": str(result.get("session_token") or "").strip(),
            "cookies": result.get("cookies") or "",
        }.items()
        if value not in (None, "")
    }
    return {"state": "valid", "message": "协议登录已获取新的 access token", "tokens": tokens}


def check_chatgpt_access_token(
    access_token: str,
    *,
    proxy: str | None = None,
    account_id: str = "",
    timeout_seconds: float = 30,
    browser_fetch: Callable[..., dict[str, Any]] | None = None,
    verify_codex: bool = False,
) -> dict[str, str]:
    """Validate an AT via OpenAI API, then optionally inspect workspace state.

    ``api.openai.com/v1/me`` is the authoritative AT check and is always
    requested directly. ``chatgpt.com/backend-api/me`` runs only after that
    succeeds; a transient workspace-check failure never overrides a valid AT.

    When ``browser_fetch`` is supplied (e.g. a camoufox ``page.evaluate``
    wrapper), requests go through a real browser context so Cloudflare sees a
    genuine browser fingerprint instead of a protocol client, avoiding the
    spurious HTTP 403 challenge that curl_cffi hits on ``api.openai.com``.
    The callable receives ``(url, method, headers, body)`` and returns a dict
    with at least ``status`` and ``text`` keys.
    """
    token = str(access_token or "").strip()
    if not token:
        return {"state": "missing", "message": "账号未保存可校验的 access token"}

    token_payload = _decode_access_token_payload(token)
    client_id = str(token_payload.get("client_id") or "")

    if _access_token_expired_locally(token):
        res = {"state": "invalid", "message": "access token JWT exp 已过期"}
        if verify_codex:
            res.update({
                "web_valid": False,
                "codex_valid": False,
                "web_status": "invalid",
                "codex_status": "invalid",
                "client_id": client_id,
            })
        return res

    resolved_account_id = (
        str(account_id or "").strip()
        or _chatgpt_account_id_from_access_token(token)
    )
    request_headers: dict[str, str] = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if resolved_account_id:
        request_headers["ChatGPT-Account-ID"] = resolved_account_id

    deadline = time.monotonic() + max(float(timeout_seconds or 30), 1.0)
    workspace_proxies = {"http": proxy, "https": proxy} if proxy else None

    def classify_response(response: Any, endpoint_name: str) -> dict[str, Any]:
        status_code = int(getattr(response, "status_code", 0) or 0)
        detail_code = _response_detail_code(response)
        detail_suffix = f"，{detail_code}" if detail_code else ""
        if 200 <= status_code < 300:
            return {
                "state": "valid",
                "message": f"HTTP {status_code}（{endpoint_name}）",
                "transient": False,
                "http_status": status_code,
            }
        if endpoint_name.endswith("/codex/responses") and status_code == 400:
            return {
                "state": "valid",
                "message": f"HTTP 200/400（{endpoint_name}，鉴权通过）",
                "transient": False,
                "http_status": status_code,
            }
        if status_code == 401:
            return {
                "state": "invalid",
                "message": f"access token 返回 HTTP 401（{endpoint_name}{detail_suffix}）",
                "transient": False,
                "http_status": status_code,
            }
        if status_code == 402:
            return {
                "state": "invalid",
                "message": f"工作区返回 HTTP 402（{endpoint_name}{detail_suffix}）",
                "transient": False,
                "http_status": status_code,
            }
        if status_code == 403:
            if _is_cloudflare_challenge_response(response):
                return {
                    "state": "unknown",
                    "message": f"Cloudflare/地区上游拦截 HTTP 403（{endpoint_name}）",
                    "transient": True,
                    "http_status": status_code,
                }
            return {
                "state": "invalid",
                "message": f"access token 返回 HTTP 403（{endpoint_name}{detail_suffix}）",
                "transient": False,
                "http_status": status_code,
            }
        if status_code == 429:
            message = f"HTTP 429 限流（{endpoint_name}）"
        elif status_code == 503:
            message = f"HTTP 503 上游服务不可用（{endpoint_name}）"
        else:
            message = f"HTTP {status_code or '-'}（{endpoint_name}{detail_suffix}）"
        return {
            "state": "unknown",
            "message": message,
            "transient": True,
            "http_status": status_code,
        }

    def retry_delay_seconds(response: Any, retry_index: int) -> float:
        try:
            retry_after = float(
                (getattr(response, "headers", {}) or {}).get("retry-after") or 0
            )
        except (TypeError, ValueError):
            retry_after = 0
        if retry_after > 0:
            return min(retry_after, 15.0)
        return 0.8 * (2 ** retry_index)

    def probe(
        endpoint_name: str,
        endpoint_url: str,
        *,
        proxies: dict[str, str] | None,
        max_retries: int,
        method: str = "GET",
        body: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_result: dict[str, Any] = {
            "state": "unknown",
            "message": f"{endpoint_name} 无响应",
            "transient": True,
            "http_status": 0,
        }
        headers = dict(request_headers)
        if extra_headers:
            headers.update(extra_headers)
        for attempt in range(max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if browser_fetch is not None:
                    # Real browser context (camoufox page) — Cloudflare sees a
                    # genuine browser fingerprint instead of a protocol client, avoiding the
                    # spurious HTTP 403 challenge that curl_cffi hits on api.openai.com.
                    result = browser_fetch(
                        endpoint_url,
                        method=method,
                        headers=headers,
                        body=body,
                    )
                    status = int(result.get("status") or 0)
                    text = str(result.get("text") or "")

                    class _BrowserResponse:
                        status_code = status
                        headers = result.get("headers") or {}

                        @property
                        def text(self):
                            return text

                        @property
                        def content(self):
                            return text

                        def json(self):
                            import json as _json

                            try:
                                return _json.loads(text or "")
                            except Exception:
                                return {}

                    response = _BrowserResponse()
                else:
                    if method == "POST":
                        response = requests.post(
                            endpoint_url,
                            headers=headers,
                            data=body,
                            proxies=proxies,
                            timeout=max(min(remaining, 30.0), 1.0),
                            impersonate=PROTOCOL_CHROME_IMPERSONATE,
                        )
                    else:
                        response = requests.get(
                            endpoint_url,
                            headers=headers,
                            proxies=proxies,
                            timeout=max(min(remaining, 30.0), 1.0),
                            impersonate=PROTOCOL_CHROME_IMPERSONATE,
                        )
            except Exception as exc:
                detail = str(exc).replace("\n", " ").strip()
                last_result = {
                    "state": "unknown",
                    "message": f"{endpoint_name} 网络错误：{detail[:180]}",
                    "transient": True,
                    "http_status": 0,
                }
                response = None
            else:
                last_result = classify_response(response, endpoint_name)

            if not last_result.get("transient") or attempt >= max_retries:
                if attempt > 0:
                    last_result["message"] = (
                        f"{last_result.get('message') or endpoint_name}；已自动重试 {attempt} 次"
                    )
                return last_result

            delay = retry_delay_seconds(response, attempt) if response is not None else 0.8 * (2 ** attempt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))

        return last_result

    primary = probe(
        "api.openai.com/v1/me",
        "https://api.openai.com/v1/me",
        proxies=None,
        max_retries=2,
    )
    if primary.get("state") != "valid":
        p_state = str(primary.get("state") or "unknown")
        p_msg = str(primary.get("message") or "AT 验活未确认")
        res = {
            "state": p_state,
            "message": p_msg,
        }
        if verify_codex:
            res.update({
                "web_valid": False,
                "codex_valid": False,
                "web_status": p_state,
                "codex_status": p_state,
                "client_id": client_id,
            })
        return res

    workspace = probe(
        "chatgpt.com/backend-api/me",
        "https://chatgpt.com/backend-api/me",
        proxies=workspace_proxies,
        max_retries=0,
    )
    if workspace.get("state") == "invalid":
        w_msg = str(workspace.get("message") or "工作区不可用")
        res = {
            "state": "invalid",
            "message": w_msg,
        }
        if verify_codex:
            res.update({
                "web_valid": False,
                "codex_valid": False,
                "web_status": "invalid",
                "codex_status": "invalid",
                "client_id": client_id,
            })
        return res

    web_valid = primary.get("state") == "valid" and workspace.get("state") != "invalid"
    web_status = "valid" if web_valid else "unknown"

    if verify_codex:
        codex_res = probe(
            "chatgpt.com/backend-api/codex/responses",
            "https://chatgpt.com/backend-api/codex/responses",
            proxies=workspace_proxies,
            max_retries=0,
            method="POST",
            body=json.dumps({"model": "gpt-5.1-codex", "messages": []}),
            extra_headers={"Content-Type": "application/json"},
        )
        codex_status = str(codex_res.get("state") or "unknown")
        if codex_status == "invalid":
            return {
                "state": "invalid",
                "message": str(codex_res.get("message") or "Codex 接口鉴权失败 (401 Unauthorized)"),
                "web_valid": web_valid,
                "codex_valid": False,
                "web_status": web_status,
                "codex_status": "invalid",
                "client_id": client_id,
            }
        if codex_status == "valid":
            return {
                "state": "valid",
                "message": "access token 可用（Codex 直连与网页鉴权均通过）",
                "web_valid": web_valid,
                "codex_valid": True,
                "web_status": web_status,
                "codex_status": "valid",
                "client_id": client_id,
            }
        return {
            "state": "valid" if web_valid else "unknown",
            "message": f"网页端可用；Codex 接口检查未确认：{str(codex_res.get('message') or '无响应')}",
            "web_valid": web_valid,
            "codex_valid": False,
            "web_status": web_status,
            "codex_status": "unknown",
            "client_id": client_id,
        }

    if workspace.get("state") == "valid":
        return {
            "state": "valid",
            "message": "access token 可用（api.openai.com/v1/me；工作区正常）",
        }
    return {
        "state": "valid",
        "message": (
            "access token 可用（api.openai.com/v1/me）；"
            f"工作区检查未确认：{str(workspace.get('message') or '无响应')}"
        ),
    }
