"""ChatGPT email registration through the OpenAI web protocol.

All network operations are direct HTTP. Sentinel JavaScript challenges run in
a bounded Node V8 pool and never start a browser executable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import CurlECode, CurlError, requests

from .constants import (
    CHATGPT_APP,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    SENTINEL_BASE,
    SENTINEL_FRAME_URL,
    SENTINEL_REQ_URL,
    SENTINEL_SDK_URL,
)
from .environment_profile import (
    FingerprintPool,
    PROTOCOL_CHROME_IMPERSONATE,
    PROTOCOL_CHROME_VERSION,
    ProtocolEnvironmentProfile,
    _browser_family,
)
from .sentinel_vm import get_sentinel_sdk, get_sentinel_vm_pool
from .oauth import OAuthStart, generate_oauth_url, submit_callback_url

_logger = logging.getLogger(__name__)


_OAUTH_INIT_MAX_ATTEMPTS = 6
_OAUTH_INIT_RETRY_BASE_SECONDS = 0.75
_OAUTH_INIT_RETRY_MAX_SECONDS = 8.0
_TRANSIENT_CURL_CODES = frozenset(
    {
        int(CurlECode.COULDNT_RESOLVE_PROXY),
        int(CurlECode.COULDNT_RESOLVE_HOST),
        int(CurlECode.COULDNT_CONNECT),
        int(CurlECode.HTTP2),
        int(CurlECode.PARTIAL_FILE),
        int(CurlECode.OPERATION_TIMEDOUT),
        int(CurlECode.SSL_CONNECT_ERROR),
        int(CurlECode.GOT_NOTHING),
        int(CurlECode.SEND_ERROR),
        int(CurlECode.RECV_ERROR),
        int(CurlECode.HTTP2_STREAM),
        int(CurlECode.HTTP3),
        int(CurlECode.QUIC_CONNECT_ERROR),
        int(CurlECode.PROXY),
    }
)


FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Daniel", "Matthew", "Anthony", "Mary", "Linda",
    "Jennifer", "Sarah", "Jessica", "Elizabeth",
)
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Martin",
    "Lee", "White",
)


def _random_profile() -> tuple[str, str]:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    age = random.randint(24, 36)
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    return name, birthdate


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _authorization_page_type(payload: dict) -> str:
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    return str(page.get("type") or payload.get("page_type") or "").strip()


def _authorization_continue_url(payload: dict) -> str:
    for key in ("continue_url", "external_url", "redirect_url", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _oauth_callback_target(url: str, oauth_start: OAuthStart) -> bool:
    candidate = urlparse(str(url or "").strip())
    target = urlparse(oauth_start.redirect_uri)
    return bool(
        candidate.scheme == target.scheme
        and candidate.netloc == target.netloc
        and candidate.path.rstrip("/") == target.path.rstrip("/")
    )


def _password_registration_step(page_type: str, continue_url: str = "") -> bool:
    normalized = str(page_type or "").strip().lower()
    target = str(continue_url or "").strip().lower()
    return normalized in {"password", "login_password", "create_account_password"} or "create-account/password" in target or "log-in/password" in target


def _email_otp_step(page_type: str, continue_url: str = "") -> bool:
    normalized = str(page_type or "").strip().lower()
    target = str(continue_url or "").strip().lower()
    return normalized in {"email_otp_send", "email_otp_verification"} or any(
        marker in target for marker in ("email-verification", "email-otp")
    )


def _response_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _response_error(response, payload: dict | None = None) -> str:
    data = payload or _response_json(response)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message and code not in message:
            return f"{code}: {message}"
        if message or code:
            return message or code
    if isinstance(error, str) and error:
        return error
    text = str(getattr(response, "text", "") or "").strip()
    status = int(getattr(response, "status_code", 0) or 0)
    # A number of auth form failures are returned as an empty HTTP 5xx body.
    # Keep the diagnostic useful without ever including request data (which
    # could contain passwords, TOTP values, cookies, or tokens).
    if text:
        return text[:300]
    details: list[str] = [f"HTTP {status}"]
    url = str(getattr(response, "url", "") or "").strip()
    if url:
        details.append(f"url={url[:180]}")
    headers = getattr(response, "headers", {}) or {}
    for name in ("x-request-id", "cf-ray", "content-type", "server"):
        value = str(headers.get(name) or headers.get(name.title()) or "").strip()
        if value:
            details.append(f"{name}={value[:120]}")
    return "; ".join(details)


class ChatGPTCloudflareChallengeError(RuntimeError):
    """The web edge returned a challenge instead of an OAuth response."""

    def __init__(self, stage: str, response) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        self.status_code = status
        self.stage = str(stage or "web request")
        super().__init__(
            f"Cloudflare challenge during {self.stage} (HTTP {status or '-'})"
        )


class ChatGPTRateLimitError(RuntimeError):
    """The auth edge rejected a bootstrap transaction due to rate limiting."""

    def __init__(self, code: str = "rate_limit_exceeded") -> None:
        self.stage = "OpenAI OAuth rate limit"
        self.code = str(code or "rate_limit_exceeded")
        super().__init__(f"OpenAI OAuth initialization failed: {self.code}")


class DirectCodexRegistrationUnavailable(RuntimeError):
    """The direct Codex registration bootstrap could not enter a signup state."""


def _is_cloudflare_challenge_response(response) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = {
        str(key).strip().lower(): str(value or "").strip().lower()
        for key, value in (getattr(response, "headers", {}) or {}).items()
    }
    content_type = headers.get("content-type", "")
    if status in {403, 429, 500, 502, 503, 504} and (
        headers.get("server") == "cloudflare" or "cf-ray" in headers
    ):
        return True
    try:
        body = str(getattr(response, "text", "") or "").lower()
    except Exception:
        body = ""
    if headers.get("cf-mitigated") == "challenge":
        return True
    if "text/html" not in content_type:
        return False
    if any(
        marker in body
        for marker in (
            "<title>just a moment",
            "cf-chl-",
            "cf_chl_",
            "verify you are human",
            "checking your browser",
            "enable javascript and cookies",
            "unable to load site",
            "using a vpn, try turning it off",
        )
    ):
        return True
    # Cloudflare's current blocked/error page may omit its brand and can be
    # returned as an empty 5xx body from the edge.  Treat those responses as
    # a challenge so the caller can rotate the route instead of recording a
    # false password failure.
    return False


def _authorization_error_from_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.path.rstrip("/") != "/error":
        return ""
    query = parse_qs(parsed.query)
    direct_error = str((query.get("error") or [""])[0]).strip()
    direct_description = str(
        (query.get("error_description") or [""])[0]
    ).strip()
    encoded_payload = str((query.get("payload") or [""])[0]).strip()
    payload: dict = {}
    if encoded_payload:
        try:
            padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            candidate = json.loads(decoded.decode("utf-8"))
            if isinstance(candidate, dict):
                payload = candidate
        except Exception:
            payload = {}
    code = str(
        payload.get("errorCode")
        or payload.get("error_code")
        or payload.get("error")
        or direct_error
        or "authorization_error"
    ).strip()
    description = str(
        payload.get("message")
        or payload.get("errorDescription")
        or payload.get("error_description")
        or direct_description
        or ""
    ).strip()
    return f"{code}: {description}" if description and description != code else code


def _raise_if_explicit_account_ban(payload: dict, *, stage: str) -> None:
    if not payload:
        return
    # Keep the deletion signal tied to structured OpenAI auth data. Generic
    # HTML, Cloudflare pages and transport failures must remain inconclusive.
    from .credential_checks import (
        ChatGPTAccountBannedDuringRelogin,
        _explicit_ban_code,
    )

    code = _explicit_ban_code(payload)
    if code:
        raise ChatGPTAccountBannedDuringRelogin(
            f"{stage}明确返回 {code}",
            code=code,
        )


class _SentinelTokenGenerator:
    """Generate the requirements/enforcement PoW used by OpenAI Sentinel.

    All environment fields are read from a ``ProtocolEnvironmentProfile``
    so that the Python proof and the Node V8 SDK see the same fingerprint.
    """

    def __init__(
        self,
        user_agent: str,
        sdk_url: str = SENTINEL_SDK_URL,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        self.user_agent = user_agent
        self.sdk_url = sdk_url
        self.sid = str(uuid.uuid4())
        self._profile = profile

    @staticmethod
    def _fnv1a32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return f"{value & 0xFFFFFFFF:08x}"

    @staticmethod
    def _encode(value) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    @property
    def _screen(self) -> str:
        if self._profile:
            return f"{self._profile.screen_width}x{self._profile.screen_height}"
        return "1920x1080"

    @property
    def _language(self) -> str:
        return self._profile.language if self._profile else "en-US"

    @property
    def _languages(self) -> str:
        if self._profile:
            return ",".join(self._profile.languages)
        return "en-US,en"

    @property
    def _hardware_concurrency(self) -> int:
        return self._profile.hardware_concurrency if self._profile else 8

    def _now_in_profile_tz(self) -> datetime:
        if self._profile:
            import zoneinfo
            try:
                return datetime.now(zoneinfo.ZoneInfo(self._profile.timezone))
            except Exception:
                pass
        return datetime.now().astimezone()

    def _fingerprint(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            self._screen,
            time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                time.gmtime(),
            ),
            4294705152,
            random.random(),
            self.user_agent,
            self.sdk_url,
            None,
            None,
            self._language,
            self._languages,
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice((4, 8, 12, 16)),
            int(time.time() * 1000 - perf_now),
        ]

    def _reference_fingerprint(self) -> list:
        """25-field fingerprint used by the current Sentinel SDK.

        All environment fields match the ``ProtocolEnvironmentProfile``
        so the Python proof and the V8 SDK expose identical values.
        """
        now = self._now_in_profile_tz()
        perf_now = round(
            time.time() * 1000 - 1_000_000 + random.uniform(1000, 5000), 1
        )
        time_origin = round(time.time() * 1000 - 50_000, 1)
        return [
            3000,
            str(now),
            4294705152,
            0,
            self.user_agent,
            self.sdk_url,
            None,
            self._language,
            self._languages,
            0,
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            self._hardware_concurrency,
            time_origin,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    def _solve_reference_pow(self, seed: str, difficulty: str, data: list) -> str:
        started = time.perf_counter()
        target = str(difficulty or "0")
        for nonce in range(500_000):
            data[3] = nonce
            data[9] = round((time.perf_counter() - started) * 1000)
            encoded = self._encode(data)
            digest = self._fnv1a32(str(seed or "") + encoded)
            if digest[: len(target)] <= target:
                return encoded + "~S"
        return self._encode("e")

    def requirements(self) -> str:
        config = self._reference_fingerprint()
        config[3] = 1
        config[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._solve_reference_pow(
            str(random.random()), "0", config
        )

    def enforcement(self, seed: str, difficulty: str) -> str:
        return "gAAAAAB" + self._solve_reference_pow(
            seed, difficulty, self._reference_fingerprint()
        )


class OpenAISentinelClient:
    def __init__(
        self,
        session,
        *,
        user_agent: str,
        proxy: str | None = None,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        del proxy
        self.session = session
        self.user_agent = user_agent
        self._profile = profile

    @staticmethod
    def _looks_like_vm_error(value: str) -> bool:
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            return False
        lowered = decoded.lower()
        return "syntaxerror" in lowered or "typeerror" in lowered or "error:" in lowered

    def build_headers(self, device_id: str, flow: str) -> dict[str, str]:
        sdk = get_sentinel_sdk(self.session)
        generator = _SentinelTokenGenerator(
            self.user_agent, sdk.url, profile=self._profile
        )
        proof = generator.requirements()
        response = self.session.post(
            SENTINEL_REQ_URL,
            data=json.dumps({"p": proof, "id": device_id, "flow": flow}),
            headers={
                "accept": "*/*",
                "content-type": "text/plain;charset=UTF-8",
                "origin": SENTINEL_BASE,
                "referer": SENTINEL_FRAME_URL,
            },
        )
        chat_req = _response_json(response)
        challenge = str(chat_req.get("token") or "").strip()
        if getattr(response, "status_code", 0) >= 400 or not challenge:
            raise RuntimeError(
                f"Sentinel challenge 获取失败: {_response_error(response, chat_req)}"
            )

        turnstile = chat_req.get("turnstile") or {}
        observer = chat_req.get("so") or {}
        vm: dict = {"t": "", "so": ""}
        if (
            turnstile.get("dx")
            or observer.get("collector_dx")
            or observer.get("snapshot_dx")
        ):
            vm_challenge = dict(chat_req)
            vm_challenge["_python_proof"] = proof
            profile = self._profile
            vm = get_sentinel_vm_pool().execute(
                challenge=vm_challenge,
                sdk=str(sdk.path.resolve()),
                script_src=sdk.url,
                user_agent=self.user_agent,
                flow=flow,
                device_id=device_id,
                page_url=f"{OPENAI_AUTH}/about-you",
                width=profile.screen_width if profile else 1920,
                height=profile.screen_height if profile else 1080,
                cores=profile.hardware_concurrency if profile else 8,
                language=profile.language if profile else "en-US",
                languages=",".join(profile.languages) if profile else "en-US,en",
                no_cookie=profile.no_cookie if profile else True,
            )
        if turnstile.get("required") and not vm.get("t"):
            raise RuntimeError(
                "Sentinel protocol VM did not generate a Turnstile token"
            )

        so_value = str(vm.get("so") or "")
        if so_value and self._looks_like_vm_error(so_value):
            so_value = ""
        pow_info = chat_req.get("proofofwork") or {}
        if pow_info.get("required") and pow_info.get("seed"):
            enforcement = generator.enforcement(
                str(pow_info.get("seed") or ""),
                str(pow_info.get("difficulty") or "0"),
            )
        else:
            enforcement = proof
        token = {
            "p": str(vm.get("p") or enforcement),
            "t": str(vm.get("t") or ""),
            "c": challenge,
            "id": device_id,
            "flow": flow,
        }
        headers = {
            "openai-sentinel-token": json.dumps(token, separators=(",", ":"))
        }
        if so_value:
            headers["openai-sentinel-so-token"] = json.dumps(
                {
                    "so": so_value,
                    "c": challenge,
                    "id": device_id,
                    "flow": flow,
                },
                separators=(",", ":"),
            )
        return headers

    def build_header(self, device_id: str, flow: str) -> str:
        return self.build_headers(device_id, flow)["openai-sentinel-token"]

    def close(self) -> None:
        pass


class ChatGPTProtocolRegister:
    """Synchronous worker compatible with ``ProtocolMailboxAdapter``.

    Accepts a ``ProtocolEnvironmentProfile`` that MUST be internally
    consistent — the startup validation in
    ``ProtocolEnvironmentProfile.validate()`` enforces that before any
    network traffic leaves the process.
    """

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        totp_secret: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        proxy_rotate_callback: Callable[[], str | None] | None = None,
        impersonate: str = PROTOCOL_CHROME_IMPERSONATE,
        session=None,
        request_timeout: float = 60,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        # --- Profile validation --------------------------------------------------
        # If the caller supplied a profile, use its fields instead of the
        # class-level defaults.  The profile MUST be internally consistent.
        if profile is not None:
            profile.validate()
            self.user_agent = profile.user_agent
            impersonate = profile.impersonate
        self._profile = profile

        self.proxy = str(proxy or "").strip() or None
        self.otp_callback = otp_callback
        # A saved TOTP secret is used for recovery login after the password
        # form.  Keep it on the worker so callers can pass it at construction
        # time without ever putting the secret in logs.
        self.totp_secret = str(totp_secret or "").strip()
        self.log = log_fn or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.proxy_rotate_callback = proxy_rotate_callback
        self._session_factory: Callable[[], object] | None = None
        if session is None:
            request_timeout = max(float(request_timeout or 60), 1.0)

            def create_session():
                kwargs = {
                    "impersonate": impersonate,
                    "timeout": request_timeout,
                }
                if self.proxy:
                    kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
                return requests.Session(**kwargs)

            self._session_factory = create_session
            session = self._session_factory()
        self.session = session
        self.sentinel = OpenAISentinelClient(
            session,
            user_agent=self.user_agent,
            proxy=self.proxy,
            profile=self._profile,
        )
        self.device_id = str(uuid.uuid4())

        # --- Diagnostic log (non-sensitive summary only) -------------------------
        if self._profile:
            self.log(
                f"环境 profile: {self._profile.name}, "
                f"family={_browser_family(self._profile.user_agent)}, "
                f"imp={self._profile.impersonate}, "
                f"lang={self._profile.language}, "
                f"tz={self._profile.timezone}, "
                f"screen={self._profile.screen_width}x{self._profile.screen_height}"
            )

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise RuntimeError("任务已取消")

    def _common_headers(self, referer: str) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": OPENAI_AUTH,
            "referer": referer,
            "user-agent": self.user_agent,
        }

    def _follow_authorize_chain(self, location: str):
        current = str(location or "").strip()
        for _ in range(15):
            if not current:
                return None
            self._check_cancelled()
            response = self.session.get(urljoin(OPENAI_AUTH, current), allow_redirects=False)
            if _is_cloudflare_challenge_response(response):
                raise ChatGPTCloudflareChallengeError("OAuth redirect", response)
            payload = _response_json(response)
            _raise_if_explicit_account_ban(payload, stage="OpenAI OAuth 重定向")
            if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
                raise RuntimeError(
                    f"OpenAI OAuth 重定向失败: {_response_error(response, payload)}"
                )
            current = str(response.headers.get("location") or "").strip()
            if not current:
                return response
        raise RuntimeError("OpenAI 授权重定向次数过多")

    @staticmethod
    def _is_transient_curl_error(exc: CurlError) -> bool:
        try:
            return int(getattr(exc, "code", 0) or 0) in _TRANSIENT_CURL_CODES
        except (TypeError, ValueError):
            return False

    def _rotate_proxy_after_challenge(self) -> bool:
        return self._rotate_proxy("Cloudflare challenge detected")

    def _rotate_proxy(self, reason: str) -> bool:
        callback = self.proxy_rotate_callback
        if not callable(callback):
            return False
        try:
            rotated = callback()
        except Exception as exc:
            self.log(f"{reason} 后切换代理失败: {exc}")
            return False
        if rotated:
            self.proxy = str(rotated).strip() or self.proxy
        self.log(f"{reason}; switched proxy and rebuilt session")
        return True

    def _replace_owned_session(self) -> None:
        if self._session_factory is None:
            raise RuntimeError("Cannot replace an externally managed HTTP session")
        try:
            self.sentinel.close()
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._session_factory()
        self.sentinel = OpenAISentinelClient(
            self.session,
            user_agent=self.user_agent,
            proxy=self.proxy,
            profile=self._profile,
        )

    def _wait_before_oauth_retry(self, delay_seconds: float) -> None:
        deadline = time.monotonic() + max(float(delay_seconds), 0.0)
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))

    def _initialize_signup(self, email: str, *, registration: bool = False):
        for attempt in range(1, _OAUTH_INIT_MAX_ATTEMPTS + 1):
            try:
                return self._initialize_signup_once(email, registration=registration)
            except (ChatGPTCloudflareChallengeError, ChatGPTRateLimitError) as exc:
                can_retry = (
                    attempt < _OAUTH_INIT_MAX_ATTEMPTS
                    and (callable(self.proxy_rotate_callback) or self._session_factory is not None)
                )
                if not can_retry:
                    raise
                if callable(self.proxy_rotate_callback) and not self._rotate_proxy_after_challenge():
                    raise
                retry_base = (
                    5.0
                    if "rate limit" in str(exc.stage).lower()
                    else _OAUTH_INIT_RETRY_BASE_SECONDS
                )
                retry_cap = (
                    30.0
                    if "rate limit" in str(exc.stage).lower()
                    else _OAUTH_INIT_RETRY_MAX_SECONDS
                )
                delay = min(retry_base * (2 ** (attempt - 1)), retry_cap)
                reason = (
                    "OAuth rate limit"
                    if "rate limit" in str(exc.stage).lower()
                    else "Cloudflare challenge"
                )
                retry_target = "on a new proxy" if callable(self.proxy_rotate_callback) else "after delay"
                self.log(
                    f"{reason} at {exc.stage}; retrying OAuth "
                    f"{retry_target} in {delay:.1f}s "
                    f"({attempt + 1}/{_OAUTH_INIT_MAX_ATTEMPTS})"
                )
                self._wait_before_oauth_retry(delay)
                self._replace_owned_session()
            except CurlError as exc:
                can_retry = (
                    self._session_factory is not None
                    and self._is_transient_curl_error(exc)
                    and attempt < _OAUTH_INIT_MAX_ATTEMPTS
                )
                if not can_retry:
                    raise
                base_delay = min(
                    _OAUTH_INIT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    _OAUTH_INIT_RETRY_MAX_SECONDS,
                )
                delay = base_delay * random.uniform(0.8, 1.2)
                error_code = int(getattr(exc, "code", 0) or 0)
                if callable(self.proxy_rotate_callback):
                    self._rotate_proxy(f"OAuth initialization curl({error_code})")
                self.log(
                    f"OAuth initialization hit transient curl({error_code}); "
                    f"retrying in {delay:.1f}s "
                    f"({attempt + 1}/{_OAUTH_INIT_MAX_ATTEMPTS})"
                )
                self._wait_before_oauth_retry(delay)
                self._replace_owned_session()
        raise RuntimeError("OAuth initialization retry loop exited unexpectedly")

    def _initialize_signup_once(self, email: str, *, registration: bool = False):
        self.log("初始化 ChatGPT 协议 OAuth 会话...")
        response = self.session.get(CHATGPT_APP, allow_redirects=True)
        self._check_cancelled()
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("ChatGPT homepage", response)
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(f"ChatGPT 首页访问失败: {_response_error(response)}")
        # NextAuth binds the authorization transaction to the ``oai-did``
        # cookie created by the ChatGPT homepage.  Using the constructor's
        # provisional UUID in ``ext-oai-did`` while the cookie carries a
        # different value lets the flow reach OTP delivery, but OTP validation
        # is then rejected with ``invalid_state``.
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass
        csrf_response = self.session.get(f"{CHATGPT_APP}/api/auth/csrf")
        self._check_cancelled()
        if _is_cloudflare_challenge_response(csrf_response):
            raise ChatGPTCloudflareChallengeError("ChatGPT CSRF", csrf_response)
        csrf_payload = _response_json(csrf_response)
        _raise_if_explicit_account_ban(csrf_payload, stage="ChatGPT OAuth CSRF 获取")
        csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
        if getattr(csrf_response, "status_code", 0) != 200 or not csrf_token:
            raise RuntimeError(f"CSRF 获取失败: {_response_error(csrf_response, csrf_payload)}")

        if registration:
            # The ChatGPT NextAuth registration bootstrap currently expects the
            # plain OpenAI sign-in transaction.  Adding ``screen_hint=signup``
            # here produces an OTP page that sends mail successfully but whose
            # validation endpoint rejects the transaction as ``invalid_state``.
            query_params = {
                "prompt": "login",
                "ext-oai-did": self.device_id,
            }
        else:
            query_params = {
                "prompt": "login",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                # Recovery is only for an existing account.  Asking for the
                # explicit login screen prevents the generic transaction from
                # preferring passwordless email OTP before password + MFA.
                "screen_hint": "login",
                "login_hint": email,
            }
        query = urlencode(query_params)
        signin_response = self.session.post(
            f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
            data=urlencode(
                {
                    "callbackUrl": f"{CHATGPT_APP}/",
                    "csrfToken": csrf_token,
                    "json": "true",
                }
            ),
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_APP,
                "referer": f"{CHATGPT_APP}/",
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        self._check_cancelled()
        if _is_cloudflare_challenge_response(signin_response):
            raise ChatGPTCloudflareChallengeError("ChatGPT OAuth sign-in", signin_response)
        signin_payload = _response_json(signin_response)
        _raise_if_explicit_account_ban(signin_payload, stage="ChatGPT OAuth 初始化")
        location = str(
            signin_payload.get("url")
            or signin_response.headers.get("location")
            or ""
        ).strip()
        if getattr(signin_response, "status_code", 0) >= 400 or not location:
            raise RuntimeError(f"OpenAI 注册授权初始化失败: {_response_error(signin_response, signin_payload)}")
        final_response = self._follow_authorize_chain(location)
        final_url = str(getattr(final_response, "url", "") or "").strip()
        authorization_error = _authorization_error_from_url(final_url)
        if authorization_error:
            if any(
                marker in authorization_error.lower()
                for marker in ("rate_limit", "rate limit", "too_many_requests")
            ):
                raise ChatGPTRateLimitError(authorization_error)
            raise RuntimeError(
                f"OpenAI OAuth initialization failed: {authorization_error}"
            )
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass
        return final_response

    def _initialize_codex_registration(self, email: str) -> OAuthStart:
        """Start registration inside the Codex PKCE transaction that will issue RT."""
        for attempt in range(1, _OAUTH_INIT_MAX_ATTEMPTS + 1):
            try:
                return self._initialize_codex_registration_once(email)
            except ChatGPTCloudflareChallengeError as exc:
                can_retry = (
                    self._session_factory is not None
                    and callable(self.proxy_rotate_callback)
                    and attempt < _OAUTH_INIT_MAX_ATTEMPTS
                )
                if not can_retry or not self._rotate_proxy_after_challenge():
                    raise
                delay = min(
                    _OAUTH_INIT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    _OAUTH_INIT_RETRY_MAX_SECONDS,
                )
                self.log(
                    f"Cloudflare challenge at {exc.stage}; retrying direct Codex OAuth "
                    f"on a new proxy in {delay:.1f}s "
                    f"({attempt + 1}/{_OAUTH_INIT_MAX_ATTEMPTS})"
                )
                self._wait_before_oauth_retry(delay)
                self._replace_owned_session()
            except CurlError as exc:
                can_retry = (
                    self._session_factory is not None
                    and self._is_transient_curl_error(exc)
                    and attempt < _OAUTH_INIT_MAX_ATTEMPTS
                )
                if not can_retry:
                    raise
                base_delay = min(
                    _OAUTH_INIT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    _OAUTH_INIT_RETRY_MAX_SECONDS,
                )
                delay = base_delay * random.uniform(0.8, 1.2)
                error_code = int(getattr(exc, "code", 0) or 0)
                if callable(self.proxy_rotate_callback):
                    self._rotate_proxy(f"Direct Codex OAuth curl({error_code})")
                self.log(
                    f"Direct Codex OAuth initialization hit transient curl({error_code}); "
                    f"retrying in {delay:.1f}s "
                    f"({attempt + 1}/{_OAUTH_INIT_MAX_ATTEMPTS})"
                )
                self._wait_before_oauth_retry(delay)
                self._replace_owned_session()
        raise RuntimeError("Direct Codex OAuth initialization retry loop exited unexpectedly")

    def _initialize_codex_registration_once(self, email: str) -> OAuthStart:
        self._check_cancelled()
        oauth_start = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
            prompt="login",
        )
        self.log(f"初始化 Codex OAuth 注册事务: {email}")
        response = self.session.get(oauth_start.auth_url, allow_redirects=True)
        self._check_cancelled()
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("Codex OAuth registration", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="Codex OAuth 注册初始化")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(
                f"Codex OAuth 注册初始化失败: {_response_error(response, payload)}"
            )
        final_url = str(getattr(response, "url", "") or oauth_start.auth_url).strip()
        authorization_error = _authorization_error_from_url(final_url)
        if authorization_error:
            raise RuntimeError(
                f"Codex OAuth registration initialization failed: {authorization_error}"
            )
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass
        return oauth_start

    def _validate_otp(self, code: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/email-verification")
        headers.update(
            {
                "oai-device-id": self.device_id,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            }
        )
        response = self.session.post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            json={"code": code},
            headers=headers,
        )
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 登录验证码校验")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"邮箱验证码校验失败: {_response_error(response, payload)}")
        return payload

    def _send_otp(self, referer: str = "") -> dict:
        resolved_referer = urljoin(OPENAI_AUTH, referer or "/email-verification")
        response = self.session.get(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers={
                "accept": "application/json, text/plain, */*",
                "origin": OPENAI_AUTH,
                "referer": resolved_referer,
                "oai-device-id": self.device_id,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "user-agent": self.user_agent,
            },
            allow_redirects=True,
        )
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 邮箱验证码发送")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"发送邮箱验证码失败: {_response_error(response, payload)}")
        self.log("邮箱验证码已发送")
        return payload

    def _submit_signup_email(self, email: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account")
        headers["oai-device-id"] = self.device_id
        headers.update(self.sentinel.build_headers(
            self.device_id,
            "authorize_continue",
        ))
        response = self.session.post(
            OPENAI_API_ENDPOINTS["signup"],
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": "signup",
            },
            headers=headers,
        )
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI signup", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI signup email submission")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(
                f"OpenAI signup email submission failed: {_response_error(response, payload)}"
            )
        page_type = _authorization_page_type(payload)
        if page_type not in {
            "password",
            "create_account_password",
            "email_otp_send",
            "email_otp_verification",
        }:
            raise RuntimeError(
                f"OpenAI signup returned unexpected authorization step: {page_type or 'unknown'}"
            )
        self.log(f"OpenAI signup email submitted: next={page_type}")
        return payload

    def _submit_login_email(self, email: str) -> dict:
        """Ask the auth transaction for the existing-account login method.

        The HTML email-verification page is only a generic shell.  The
        ``authorize/continue`` response carries the transaction-bound
        ``continue_url`` for the password form; navigating to
        ``/log-in/password`` without that state returns HTTP 400.
        """
        headers = self._common_headers(f"{OPENAI_AUTH}/email-verification")
        headers["oai-device-id"] = self.device_id
        headers.update(self.sentinel.build_headers(self.device_id, "authorize_continue"))
        response = self.session.post(
            OPENAI_API_ENDPOINTS["signup"],
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": "login",
            },
            headers=headers,
        )
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI login method", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 登录方式选择")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(
                f"OpenAI 登录方式选择失败: {_response_error(response, payload)}"
            )
        page_type = _authorization_page_type(payload)
        if page_type not in {
            "password",
            "login_password",
            "email_otp_send",
            "email_otp_verification",
        }:
            raise RuntimeError(
                f"OpenAI 登录返回未知验证步骤: {page_type or 'unknown'}"
            )
        self.log(f"OpenAI login method selected: next={page_type}")
        return payload

    def _register_password(self, email: str, password: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account/password")
        headers.update(self.sentinel.build_headers(
            self.device_id,
            "username_password_create",
        ))
        response = self.session.post(
            OPENAI_API_ENDPOINTS["register"],
            json={"password": password, "username": email},
            headers=headers,
        )
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 密码设置")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"设置 ChatGPT 密码失败: {_response_error(response, payload)}")
        return payload

    def _login_password(self, password: str, page_response) -> dict:
        """Submit the password to the login form selected by the auth protocol."""
        page_text = str(getattr(page_response, "text", "") or "")
        action_match = re.search(
            r"<form[^>]+action=[\"']([^\"']+)",
            page_text,
            flags=re.IGNORECASE,
        )
        if not action_match:
            raise RuntimeError("ChatGPT protocol login page did not expose a password form")
        form_action = urljoin(OPENAI_AUTH, action_match.group(1))
        hidden_fields: dict[str, str] = {}
        for tag in re.findall(r"<input[^>]*>", page_text, flags=re.IGNORECASE):
            if not re.search(r"type=[\"']hidden[\"']", tag, flags=re.IGNORECASE):
                continue
            name_match = re.search(r"name=[\"']([^\"']+)", tag, flags=re.IGNORECASE)
            value_match = re.search(r"value=[\"']([^\"']*)", tag, flags=re.IGNORECASE)
            if name_match:
                hidden_fields[name_match.group(1)] = value_match.group(1) if value_match else ""
        hidden_fields["password"] = password
        response = self.session.post(
            form_action,
            data=urlencode(hidden_fields),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": OPENAI_AUTH,
                "referer": str(getattr(page_response, "url", "") or f"{OPENAI_AUTH}/log-in/password"),
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI password login", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 密码登录")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"ChatGPT protocol login failed: {_response_error(response, payload)}")
        return {
            "continue_url": str(response.headers.get("location") or ""),
            "response": response,
            "payload": payload,
        }

    @staticmethod
    def _has_password_form(page_response) -> bool:
        text = str(getattr(page_response, "text", "") or "")
        return bool(
            re.search(r"<form\b", text, flags=re.IGNORECASE)
            and re.search(
                r"(?:type=[\"']password[\"']|name=[\"']password[\"'])",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _load_login_password_page(self, page_response, *, continue_url: str = ""):
        """Load the password page for a login transaction.

        OpenAI currently lands the generic login transaction on an email OTP
        page even for accounts that have a password.  The password form is a
        sibling auth step in the same transaction, so navigating to it keeps
        the transaction/device cookies intact and avoids consuming an email
        code before the password + TOTP path is attempted.
        """
        if self._has_password_form(page_response):
            return page_response
        referer = str(getattr(page_response, "url", "") or "").strip()
        target = str(continue_url or "/log-in/password").strip()
        response = self.session.get(
            urljoin(OPENAI_AUTH, target),
            headers={
                "referer": referer or f"{OPENAI_AUTH}/email-verification",
                "user-agent": self.user_agent,
            },
            allow_redirects=True,
        )
        self._check_cancelled()
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI password login", response)
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(
                f"OpenAI 密码登录页面获取失败: {_response_error(response)}"
            )
        if not self._has_password_form(response):
            raise RuntimeError("OpenAI 登录未提供密码表单，拒绝回退到邮箱验证码")
        return response

    @staticmethod
    def _looks_like_totp_challenge(page_response) -> bool:
        text = str(getattr(page_response, "text", "") or "").lower()
        url = str(getattr(page_response, "url", "") or "").lower()
        if any(marker in url for marker in ("/mfa", "/two-factor", "/2fa", "/authenticator")):
            return True
        if any(
            marker in text
            for marker in (
                "one-time password",
                "two-factor",
                "two factor",
                "authenticator app",
                "verification code",
                "mfa",
                "totp",
            )
        ) and re.search(r"(?:autocomplete=[\"']one-time-code|name=[\"'](?:code|otp|totp|mfa)[\"'])", text):
            return True
        return False

    @staticmethod
    def _looks_like_email_otp_challenge(page_response) -> bool:
        if page_response is None:
            return False
        text = str(getattr(page_response, "text", "") or "").lower()
        url = str(getattr(page_response, "url", "") or "").lower()
        if any(marker in url for marker in ("/email-verification", "/email-otp")):
            return True
        if any(
            marker in text
            for marker in (
                "email-verification",
                "check your email",
                "sent a code to",
                "enter the code we sent",
            )
        ) and (
            re.search(r"(?:name=[\"'](?:code|otp)[\"']|autocomplete=[\"']one-time-code)", text)
            or "/email-verification" in text
        ):
            return True
        return False

    def _login_totp(self, secret: str, page_response) -> dict:
        """Submit a saved authenticator code on the post-password MFA form."""
        normalized_secret = str(secret or "").strip()
        if not normalized_secret:
            raise RuntimeError("ChatGPT protocol login requires a saved TOTP secret")
        page_text = str(getattr(page_response, "text", "") or "")
        action_match = re.search(
            r"<form[^>]+action=[\"']([^\"']+)",
            page_text,
            flags=re.IGNORECASE,
        )
        if not action_match:
            raise RuntimeError("ChatGPT protocol login MFA page did not expose a form")
        form_action = urljoin(OPENAI_AUTH, action_match.group(1))
        fields: dict[str, str] = {}
        visible_names: list[str] = []
        for tag in re.findall(r"<input[^>]*>", page_text, flags=re.IGNORECASE):
            name_match = re.search(r"name=[\"']([^\"']+)", tag, flags=re.IGNORECASE)
            if not name_match:
                continue
            name = name_match.group(1)
            value_match = re.search(r"value=[\"']([^\"']*)", tag, flags=re.IGNORECASE)
            value = value_match.group(1) if value_match else ""
            if re.search(r"type=[\"']hidden[\"']", tag, flags=re.IGNORECASE):
                fields[name] = value
            else:
                visible_names.append(name)
        code_name = next(
            (
                name
                for name in visible_names
                if name.lower() in {"code", "otp", "totp", "totp_code", "mfa_code", "token"}
            ),
            visible_names[0] if visible_names else "code",
        )
        from .mfa import totp_code

        fields[code_name] = totp_code(normalized_secret)
        response = self.session.post(
            form_action,
            data=urlencode(fields),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": OPENAI_AUTH,
                "referer": str(getattr(page_response, "url", "") or f"{OPENAI_AUTH}/mfa"),
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI TOTP login", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI TOTP 登录")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"ChatGPT TOTP 登录失败: {_response_error(response, payload)}")
        return {
            "continue_url": str(response.headers.get("location") or ""),
            "response": response,
            "payload": payload,
        }

    def _create_account(self, name: str, birthdate: str) -> dict:
        """Submit the create-account request with a fresh Sentinel proof.

        ``registration_disallowed`` is treated as a policy-level rejection:
        the same account / session / profile is NOT retried immediately,
        because the rejection condition almost never changes within seconds.
        """
        self._check_cancelled()
        headers = self._common_headers(f"{OPENAI_AUTH}/about-you")
        headers.update(
            self.sentinel.build_headers(self.device_id, "oauth_create_account")
        )
        response = self.session.post(
            OPENAI_API_ENDPOINTS["create_account"],
            json={"name": name, "birthdate": birthdate},
            headers=headers,
        )
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 创建账号")
        if getattr(response, "status_code", 0) < 400 and not payload.get("error"):
            return payload
        last_error = _response_error(response, payload)
        if "registration_disallowed" in last_error:
            self.log(
                f"registration_disallowed (policy rejection) — "
                f"不立即重试同一 session"
            )
        raise RuntimeError(f"创建 ChatGPT 账号失败: {last_error}")

    def _visit_auth_step(self, continue_url: str, *, referer: str) -> object | None:
        target = str(continue_url or "").strip()
        if not target:
            return None
        response = self.session.get(
            urljoin(OPENAI_AUTH, target),
            headers={
                "referer": urljoin(OPENAI_AUTH, referer),
                "user-agent": self.user_agent,
            },
            allow_redirects=True,
        )
        if _is_cloudflare_challenge_response(response):
            raise ChatGPTCloudflareChallengeError("OpenAI registration step", response)
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="OpenAI 注册页面跳转")
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"OpenAI 注册页面跳转失败: {_response_error(response, payload)}")
        return response

    def _follow_codex_callback(
        self,
        oauth_start: OAuthStart,
        continue_url: str,
    ) -> str:
        current = urljoin(OPENAI_AUTH, str(continue_url or "").strip())
        if not current:
            return ""
        for _ in range(15):
            self._check_cancelled()
            if _oauth_callback_target(current, oauth_start):
                return current
            response = self.session.get(
                current,
                headers={"referer": OPENAI_AUTH, "user-agent": self.user_agent},
                allow_redirects=False,
            )
            if _is_cloudflare_challenge_response(response):
                raise ChatGPTCloudflareChallengeError("Codex OAuth callback", response)
            payload = _response_json(response)
            _raise_if_explicit_account_ban(payload, stage="Codex OAuth 授权回调")
            if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
                raise RuntimeError(
                    f"Codex OAuth 授权跳转失败: {_response_error(response, payload)}"
                )
            candidate = str(
                getattr(response, "headers", {}).get("location")
                or _authorization_continue_url(payload)
                or ""
            ).strip()
            if not candidate:
                return ""
            current = urljoin(current, candidate)
        raise RuntimeError("Codex OAuth 授权重定向次数过多")

    def _complete_codex_registration_oauth(
        self,
        oauth_start: OAuthStart,
        *,
        email: str,
        continue_url: str = "",
    ) -> dict:
        callback_url = ""
        if continue_url:
            callback_url = self._follow_codex_callback(oauth_start, continue_url)
        if not callback_url:
            from platforms.chatgpt.credential_checks import (
                _authorization_code_from_session,
                _authorization_code_via_account_selection,
            )

            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            try:
                callback_url = _authorization_code_from_session(
                    self.session,
                    oauth_start=oauth_start,
                    proxies=proxies,
                    cancel_check=self.cancel_check,
                )
            except RuntimeError as silent_error:
                self.log(
                    f"Codex OAuth 直接回调未完成，继续账号/工作区选择: {silent_error}"
                )
                callback_url = _authorization_code_via_account_selection(
                    self.session,
                    oauth_start=oauth_start,
                    email=email,
                    device_id=self.device_id,
                    sentinel_client=self.sentinel,
                    proxies=proxies,
                    cancel_check=self.cancel_check,
                )
        tokens = json.loads(
            submit_callback_url(
                callback_url=callback_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=oauth_start.redirect_uri,
                client_id=oauth_start.client_id,
                proxy_url=self.proxy,
                session=self.session,
            )
        )
        if not str(tokens.get("access_token") or "").strip():
            raise RuntimeError("Codex OAuth token 响应缺少 access_token")
        if not str(tokens.get("refresh_token") or "").strip():
            raise RuntimeError("Codex OAuth token 响应缺少 refresh_token")
        tokens["client_id"] = oauth_start.client_id
        self.log("同一注册事务已获取 Codex OAuth refresh token")
        return tokens

    def _codex_registration_result(
        self,
        email: str,
        password: str,
        tokens: dict,
    ) -> dict:
        access_token = str(tokens.get("access_token") or "").strip()
        id_token = str(tokens.get("id_token") or "").strip()
        claims = _decode_jwt_payload(id_token) or _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        account_id = str(
            tokens.get("account_id")
            or auth_claims.get("chatgpt_account_id")
            or ""
        ).strip()
        workspace_id = str(
            auth_claims.get("organization_id")
            or auth_claims.get("chatgpt_account_id")
            or account_id
        ).strip()
        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = {}
        session_token = ""
        try:
            session_token = str(
                self.session.cookies.get("__Secure-next-auth.session-token") or ""
            ).strip()
        except Exception:
            pass
        return {
            "email": str(tokens.get("email") or email).strip() or email,
            "password": password,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "session_token": session_token,
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": id_token,
            "client_id": str(tokens.get("client_id") or CODEX_CLIENT_ID).strip(),
            "cookies": cookies,
            "profile": {"email": str(tokens.get("email") or email).strip()},
            "expires_at": str(tokens.get("expired") or "").strip(),
        }

    def _session_result(self, email: str, password: str) -> dict:
        self._check_cancelled()
        response = self.session.get(f"{CHATGPT_APP}/api/auth/session")
        self._check_cancelled()
        payload = _response_json(response)
        _raise_if_explicit_account_ban(payload, stage="ChatGPT 会话获取")
        access_token = str(payload.get("accessToken") or "").strip()
        if getattr(response, "status_code", 0) != 200 or not access_token:
            raise RuntimeError(f"注册完成但获取 ChatGPT session 失败: {_response_error(response, payload)}")
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        claims = _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        account_id = str(
            auth_claims.get("chatgpt_account_id")
            or account.get("id")
            or ""
        )
        workspace_id = str(auth_claims.get("organization_id") or account_id)
        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = {}
        oauth_tokens: dict = {}
        try:
            from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

            recovered = mint_chatgpt_refresh_token_from_session(
                cookies,
                proxy=self.proxy,
                session=self.session,
                email=email,
                device_id=self.device_id,
                sentinel_client=self.sentinel,
                # A freshly-created ChatGPT session can authorize the Codex
                # public client silently after a short propagation delay.  The
                # explicit account chooser currently routes new accounts to an
                # ``add_phone`` gate, so try the session-backed ``prompt=none``
                # path before falling back to account selection.
                prefer_account_selection=False,
                cancel_check=self.cancel_check,
            )
            if recovered.get("state") == "valid":
                oauth_tokens = dict(recovered.get("tokens") or {})
                self.log("已获取 OAuth refresh token")
            else:
                oauth_message = str(recovered.get("message") or "").strip()
                if "add_phone" in oauth_message or "手机号验证" in oauth_message:
                    self.log("本次 OAuth 命中 add_phone，按正常无 RT 账号保存")
                else:
                    self.log(f"未获取 OAuth refresh token: {oauth_message}")
        except Exception as exc:
            self.log(f"获取 OAuth refresh token 失败: {exc}")
        return {
            "email": email,
            "password": password,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": str(oauth_tokens.get("access_token") or access_token),
            "session_token": str(payload.get("sessionToken") or ""),
            "refresh_token": str(oauth_tokens.get("refresh_token") or ""),
            "id_token": str(oauth_tokens.get("id_token") or ""),
            "client_id": str(oauth_tokens.get("client_id") or ""),
            "cookies": cookies,
            "profile": account,
            "expires_at": payload.get("expires") or "",
        }

    def _finalize_registration_result(self, result: dict) -> dict:
        """Bind TOTP before the registration session is closed.

        The registration session carries the exact cookies, device identity,
        TLS fingerprint and proxy route that created the account. Reusing it
        for MFA avoids intermittent 403 responses from a fresh follow-up
        session and makes protocol registration obey the same password+2FA
        invariant as browser registration.
        """
        from platforms.chatgpt.mfa import bind_totp_2fa

        access_token = str(result.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("协议注册结果缺少 access token，无法绑定 TOTP 2FA")
        try:
            totp = bind_totp_2fa(self.session, access_token)
        except Exception as exc:
            raise RuntimeError(f"协议注册 TOTP 2FA 绑定失败: {exc}") from exc
        secret = str(totp.get("secret") or "").strip()
        if not bool(totp.get("activated")) or not secret:
            raise RuntimeError(
                "协议注册 TOTP 2FA 激活未确认，拒绝保存账号: "
                f"{str(totp.get('result') or totp)[:160]}"
            )
        result["password_registered"] = True
        result["totp_2fa"] = {
            "requested": True,
            "bound": True,
            "secret": secret,
            "error": "",
        }
        if self.proxy:
            result["_registration_proxy"] = self.proxy
        self.log("协议注册会话内 TOTP 2FA 绑定并激活成功")
        return result

    def login(
        self,
        *,
        email: str,
        password: str,
        totp_secret: str | None = None,
    ) -> dict:
        """Log in through the same OAuth bootstrap used by registration."""
        if not str(email or "").strip() or not str(password or ""):
            raise RuntimeError("ChatGPT protocol login requires email and password")
        saved_totp_secret = str(totp_secret or self.totp_secret or "").strip()
        self._check_cancelled()
        self.log(f"开始复用 ChatGPT 注册协议登录: {email}")
        try:
            login_page = self._initialize_signup(email)
            self._check_cancelled()
            login_authorization = None
            if saved_totp_secret:
                login_authorization = self._submit_login_email(email)
            else:
                try:
                    login_authorization = self._submit_login_email(email)
                except Exception as email_exc:
                    if not callable(self.otp_callback):
                        raise
                    self.log(f"登录邮箱选择步骤异常 ({email_exc})，尝试直接校验 OTP 回调兼容路径")

            if login_authorization is not None:
                login_continue_url = _authorization_continue_url(login_authorization)
                login_page_type = _authorization_page_type(login_authorization)
                if _password_registration_step(login_page_type, login_continue_url):
                    login_page = self._load_login_password_page(
                        login_page,
                        continue_url=login_continue_url,
                    )
                    self.log("已进入密码登录步骤")
                    login_result = self._login_password(password, login_page)
                    self._check_cancelled()
                    continue_url = str(login_result.get("continue_url") or "").strip()
                    next_page = login_result.get("response")
                    if continue_url:
                        next_page = self._follow_authorize_chain(continue_url)
                    if saved_totp_secret:
                        if not self._looks_like_totp_challenge(next_page):
                            raise RuntimeError(
                                "密码已提交，但 OpenAI 未进入 TOTP 验证步骤，拒绝改走邮箱验证码"
                            )
                        totp_result = self._login_totp(saved_totp_secret, next_page)
                        self._check_cancelled()
                        continue_url = str(totp_result.get("continue_url") or "").strip()
                        if continue_url:
                            self._follow_authorize_chain(continue_url)
                        self.log("已使用保存的 TOTP 完成双重验证")
                    elif self._looks_like_totp_challenge(next_page):
                        raise RuntimeError("密码已提交，但 OpenAI 要求 TOTP 双重验证，且该账号未保存 TOTP 密匙")
                    elif self._looks_like_email_otp_challenge(next_page):
                        self.log("密码提交后 OpenAI 要求邮箱验证码，调用 OTP 回调...")
                        if not callable(self.otp_callback):
                            raise RuntimeError("OpenAI 要求邮箱验证码，但未提供 OTP 回调")
                        code = str(self.otp_callback() or "").strip()
                        self._check_cancelled()
                        if not code:
                            raise RuntimeError("ChatGPT protocol login did not receive an email code")
                        validation = self._validate_otp(code)
                        self._check_cancelled()
                        continue_url = str(validation.get("continue_url") or "").strip()
                        if continue_url:
                            self._follow_authorize_chain(continue_url)
                        self.log("已完成邮箱验证码二次验证")
                elif _email_otp_step(login_page_type, login_continue_url):
                    self.log("OpenAI 要求邮箱验证码登录，开始收取邮件...")
                    if not callable(self.otp_callback):
                        raise RuntimeError(
                            "OpenAI 要求邮箱验证码，但未提供 OTP 回调"
                        )
                    code = str(self.otp_callback() or "").strip()
                    self._check_cancelled()
                    if not code:
                        raise RuntimeError("ChatGPT protocol login did not receive an email code")
                    validation = self._validate_otp(code)
                    self._check_cancelled()
                    continue_url = str(validation.get("continue_url") or "").strip()
                    if continue_url:
                        login_page = self._follow_authorize_chain(continue_url)
                    self.log("邮箱验证码校验通过")
                    if self._has_password_form(login_page):
                        self.log("继续提交密码")
                        login_result = self._login_password(password, login_page)
                        self._check_cancelled()
                        continue_url = str(login_result.get("continue_url") or "").strip()
                        if continue_url:
                            self._follow_authorize_chain(continue_url)
                else:
                    raise RuntimeError(
                        f"OpenAI 登录返回未知验证步骤: {login_page_type or 'unknown'}"
                    )
            else:
                # Compatibility path for test doubles where signup was not mocked
                if not callable(self.otp_callback):
                    raise RuntimeError(
                        "ChatGPT protocol login requires a saved TOTP secret or OTP callback"
                    )
                code = str(self.otp_callback() or "").strip()
                self._check_cancelled()
                if not code:
                    raise RuntimeError("ChatGPT protocol login did not receive an email code")
                validation = self._validate_otp(code)
                self._check_cancelled()
                continue_url = str(validation.get("continue_url") or "").strip()
                if continue_url:
                    login_page = self._follow_authorize_chain(continue_url)
                login_result = self._login_password(password, login_page)
                self._check_cancelled()
                continue_url = str(login_result.get("continue_url") or "").strip()
                if continue_url:
                    self._follow_authorize_chain(continue_url)

            result = self._session_result(email, password)
            self.log("ChatGPT protocol login completed and issued a session token")
            return result
        except ChatGPTCloudflareChallengeError as exc:
            retries = int(getattr(self, "_login_cloudflare_retries", 0) or 0)
            if retries >= _OAUTH_INIT_MAX_ATTEMPTS - 1:
                raise
            if callable(self.proxy_rotate_callback) and not self._rotate_proxy_after_challenge():
                raise
            self._login_cloudflare_retries = retries + 1
            delay = min(
                _OAUTH_INIT_RETRY_BASE_SECONDS * (2 ** retries),
                _OAUTH_INIT_RETRY_MAX_SECONDS,
            )
            retry_target = "on a new proxy" if callable(self.proxy_rotate_callback) else "after delay"
            self.log(
                f"Cloudflare challenge at {exc.stage}; retrying password login "
                f"{retry_target} in {delay:.1f}s "
                f"({retries + 2}/{_OAUTH_INIT_MAX_ATTEMPTS})"
            )
            self._wait_before_oauth_retry(delay)
            self._replace_owned_session()
            return self.login(
                email=email,
                password=password,
                totp_secret=saved_totp_secret,
            )
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass
    def _run_legacy_web_registration(self, *, email: str, password: str) -> dict:
        """Create the account through ChatGPT NextAuth, then mint Codex tokens."""
        self.log(f"开始 ChatGPT Web 协议注册: {email}")
        try:
            self._initialize_signup(email, registration=True)
            authorization = self._submit_signup_email(email)
            created = self._create_account_from_authorization(
                email=email,
                password=password,
                authorization=authorization,
            )
            callback_url = _authorization_continue_url(created)
            if callback_url:
                self.session.get(
                    urljoin(OPENAI_AUTH, callback_url),
                    headers={"user-agent": self.user_agent},
                    allow_redirects=True,
                )
            result = self._finalize_registration_result(
                self._session_result(email, password)
            )
            self.log("ChatGPT Web 兼容注册完成")
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass

    def _create_account_from_authorization(
        self,
        *,
        email: str,
        password: str,
        authorization: dict,
    ) -> dict:
        page_type = _authorization_page_type(authorization)
        continue_url = _authorization_continue_url(authorization)
        password_registered = False
        otp_completed = False
        # ``authorize/continue`` and ``user/register`` currently dispatch the
        # OTP when their response enters the email-verification state. Calling
        # ``email-otp/send`` again invalidates the transaction and validation
        # fails with ``invalid_state`` even though the second email arrives.
        otp_already_dispatched = _email_otp_step(page_type, continue_url)

        for _ in range(6):
            self._check_cancelled()
            if _password_registration_step(page_type, continue_url):
                self._visit_auth_step(
                    continue_url or "/create-account/password",
                    referer="/create-account",
                )
                password_result = self._register_password(email, password)
                self._direct_registration_mutated = True
                password_registered = True
                page_type = _authorization_page_type(password_result)
                continue_url = _authorization_continue_url(password_result)
                otp_already_dispatched = _email_otp_step(page_type, continue_url)
                self.log(f"ChatGPT 密码创建成功: next={page_type or continue_url or 'unknown'}")
                continue

            if _email_otp_step(page_type, continue_url):
                if not password_registered:
                    # OpenAI can default a new signup to the passwordless OTP
                    # branch. Require the remote password API to accept our
                    # password before consuming an email verification code.
                    self._visit_auth_step(
                        "/create-account/password",
                        referer=continue_url or "/create-account",
                    )
                    password_result = self._register_password(email, password)
                    self._direct_registration_mutated = True
                    password_registered = True
                    page_type = _authorization_page_type(password_result)
                    continue_url = _authorization_continue_url(password_result)
                    otp_already_dispatched = _email_otp_step(page_type, continue_url)
                    self.log(
                        "已从无密码 OTP 注册切换到密码注册: "
                        f"next={page_type or continue_url or 'unknown'}"
                    )
                    continue
                if otp_completed:
                    raise RuntimeError("邮箱验证码校验后仍停留在 OTP 页面")
                self._visit_auth_step(
                    continue_url or "/email-verification",
                    referer="/create-account/password" if password_registered else "/create-account",
                )
                if otp_already_dispatched:
                    self.log("授权状态已自动发送邮箱验证码，跳过重复发码")
                    otp_already_dispatched = False
                else:
                    self._send_otp(continue_url or "/email-verification")
                code = str(self.otp_callback() or "").strip()
                self._check_cancelled()
                if not code:
                    raise RuntimeError("未收到邮箱验证码")
                validation = self._validate_otp(code)
                otp_completed = True
                page_type = _authorization_page_type(validation)
                continue_url = _authorization_continue_url(validation)
                self.log(f"邮箱验证码校验通过: next={page_type or continue_url or 'unknown'}")
                continue

            normalized_page = str(page_type or "").strip().lower()
            normalized_url = str(continue_url or "").strip().lower()
            if normalized_page == "add_phone" or normalized_url.endswith("/add-phone"):
                # ``add_phone`` is also used as a UI hint in sessions where
                # the account-creation API still permits completion.  Let the
                # server-side create_account call make the authoritative
                # decision instead of rejecting the transaction prematurely.
                self.log(
                    "OpenAI 注册返回 add_phone，继续调用 create_account "
                    "由服务端确认是否强制手机号验证"
                )
                break
            if (
                normalized_page in {"about_you", "create_account"}
                or "about-you" in normalized_url
                or (not normalized_page and not normalized_url and password_registered and otp_completed)
            ):
                break
            raise RuntimeError(
                f"OpenAI 注册进入未知步骤: {page_type or continue_url or 'unknown'}"
            )
        else:
            raise RuntimeError("OpenAI 注册状态切换次数过多")

        if not otp_completed:
            raise RuntimeError("OpenAI 注册流程未完成邮箱验证码校验")
        if not password_registered:
            raise RuntimeError("OpenAI 注册流程未确认远端密码设置，拒绝创建无密码账号")

        if continue_url:
            self._visit_auth_step(continue_url, referer="/email-verification")
        name, birthdate = _random_profile()
        created = self._create_account(name, birthdate)
        self.log("ChatGPT 账号资料创建成功")
        return created

    def _run_codex_registration(self, *, email: str, password: str) -> dict:
        oauth_start: OAuthStart | None = None
        authorization: dict | None = None
        for bootstrap_attempt in range(2):
            try:
                oauth_start = self._initialize_codex_registration(email)
                authorization = self._submit_signup_email(email)
                break
            except ChatGPTCloudflareChallengeError as exc:
                can_retry = (
                    bootstrap_attempt < 1
                    and callable(self.proxy_rotate_callback)
                    and self._session_factory is not None
                    and self._rotate_proxy_after_challenge()
                )
                if not can_retry:
                    raise DirectCodexRegistrationUnavailable(str(exc)) from exc
                self._wait_before_oauth_retry(0.8)
                self._replace_owned_session()
            except Exception as exc:
                if self.cancel_check() or exc.__class__.__name__ == "ChatGPTAccountBannedDuringRelogin":
                    raise
                raise DirectCodexRegistrationUnavailable(str(exc)) from exc
        if oauth_start is None or authorization is None:
            raise DirectCodexRegistrationUnavailable(
                "Codex OAuth registration bootstrap did not complete"
            )

        created = self._create_account_from_authorization(
            email=email,
            password=password,
            authorization=authorization,
        )
        self.log("ChatGPT 账号资料创建成功，继续同一 Codex OAuth 事务")
        tokens = self._complete_codex_registration_oauth(
            oauth_start,
            email=email,
            continue_url=_authorization_continue_url(created),
        )
        return self._finalize_registration_result(
            self._codex_registration_result(email, password, tokens)
        )


    def run(self, *, email: str, password: str) -> dict:
        if not str(email or "").strip():
            raise RuntimeError("协议注册缺少邮箱")
        if not callable(self.otp_callback):
            raise RuntimeError("协议注册缺少邮箱验证码回调")
        self._check_cancelled()
        self._direct_registration_mutated = False
        self.log(f"开始 ChatGPT Web 协议注册并获取 Codex OAuth token: {email}")
        try:
            # Account creation and Codex token issuance are deliberately split
            # into two OAuth transactions.  Creating an account directly inside
            # the Codex PKCE transaction currently enters the mandatory
            # ``add_phone`` step after email OTP validation, while ChatGPT Web's
            # registration transaction can finish account creation and establish
            # the session needed by ``_session_result``.  That session is then
            # exchanged through a fresh Codex OAuth transaction for AT/RT/IDT.
            result = self._run_legacy_web_registration(email=email, password=password)
            if str(result.get("refresh_token") or "").strip():
                self.log("ChatGPT 协议注册完成并获取 Codex OAuth token")
            else:
                self.log("ChatGPT 协议注册完成，本账号为正常无 RT 状态")
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass
