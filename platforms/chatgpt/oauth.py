"""OpenAI/Codex OAuth helpers shared by registration and maintenance flows."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests as cffi_requests

from .constants import (
    CODEX_CLIENT_ID,
    OAUTH_AUTH_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    OPENAI_AUTH,
)


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_callback_url(callback_url: str) -> dict[str, str]:
    candidate = str(callback_url or "").strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}
    if "://" not in candidate:
        candidate = f"http://localhost/{'?' if not candidate.startswith('?') else ''}{candidate}"
    parsed = urlparse(candidate)
    values = parse_qs(parsed.query, keep_blank_values=True)
    fragment = parse_qs(parsed.fragment, keep_blank_values=True)
    for key, items in fragment.items():
        if not values.get(key) or not str(values[key][0] or "").strip():
            values[key] = items

    def first(key: str) -> str:
        items = values.get(key, [""])
        return str(items[0] if items else "").strip()

    return {
        "code": first("code"),
        "state": first("state"),
        "error": first("error"),
        "error_description": first("error_description"),
    }


def _jwt_claims_no_verify(token: str) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    segment = token.split(".")[1]
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str = OAUTH_CLIENT_ID


def generate_oauth_url(
    *,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    scope: str = OAUTH_SCOPE,
    client_id: str = OAUTH_CLIENT_ID,
    prompt: str = "login",
) -> OAuthStart:
    """Build an OAuth URL with state and an S256 PKCE challenge."""
    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_no_pad(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    )
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if prompt:
        params["prompt"] = prompt
    if client_id == CODEX_CLIENT_ID:
        params["id_token_add_organizations"] = "true"
        params["codex_cli_simplified_flow"] = "true"
        base_url = f"{OPENAI_AUTH}/oauth/authorize"
    else:
        params["screen_hint"] = "login_or_signup"
        base_url = OAUTH_AUTH_URL
    return OAuthStart(
        auth_url=f"{base_url}?{urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    client_id: str = OAUTH_CLIENT_ID,
    token_url: str = OAUTH_TOKEN_URL,
    proxy_url: str | None = None,
    session=None,
) -> str:
    """Validate an OAuth callback and exchange its code for the token bundle."""
    callback = _parse_callback_url(callback_url)
    if callback["error"]:
        raise RuntimeError(
            f"oauth error: {callback['error']}: {callback['error_description']}".strip(": ")
        )
    if not callback["code"]:
        raise ValueError("callback url missing ?code=")
    if not callback["state"]:
        raise ValueError("callback url missing ?state=")
    if callback["state"] != expected_state:
        raise ValueError("state mismatch")

    requester = session or cffi_requests
    request_kwargs: dict[str, Any] = {
        "data": {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": callback["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        "headers": {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        "timeout": 30,
    }
    if proxy_url:
        request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    if session is None:
        request_kwargs["impersonate"] = "chrome124"
    response = requester.post(token_url, **request_kwargs)
    if int(getattr(response, "status_code", 0) or 0) != 200:
        raise RuntimeError(
            f"token exchange failed: {getattr(response, 'status_code', 0)}: "
            f"{str(getattr(response, 'text', '') or '')[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("token exchange failed: response is not an object")

    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    claims = _jwt_claims_no_verify(id_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    expires_in = max(_to_int(payload.get("expires_in")), 0)
    now = int(time.time())
    result = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": str(auth_claims.get("chatgpt_account_id") or "").strip(),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "email": str(claims.get("email") or "").strip(),
        "type": "codex",
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_in)),
    }
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
