"""ChatGPT 浏览器注册流程（Camoufox，有头/无头两用）。

基于 2026-08-06 手动抓包 HAR 的当前注册流程：

    1. chatgpt.com 登录页填写邮箱
       （或浏览器内 POST /api/auth/signin/openai 直入授权链）
    2. auth.openai.com 邮箱验证页 -> POST /api/accounts/user/register  设置密码
    3. POST /api/accounts/email-otp/send + validate                     邮箱 6 位验证码
    4. about-you 页 -> POST /api/accounts/create_account               姓名 + 生日
    5. /api/auth/callback/openai -> 302 回到 chatgpt.com                会话建立
    6. 提取会话（access_token / session_token / cookies）

TOTP 2FA 绑定不在本模块：注册成功拿到 access_token 后由任务后处理
（application/tasks.py 复用 platforms.chatgpt.mfa.bind_totp_2fa）完成。

headless=True 时同样走这套状态机；两种模式都保留（有头用于人工观察/调试）。
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from camoufox.sync_api import Camoufox

from .constants import CHATGPT_APP, OPENAI_AUTH
from .mfa import MFA_ACTIVATE_URL, MFA_ENROLL_URL, prepare_totp_activation

# ---------------------------------------------------------------------------
# 页面元素选择器（同一批选择器在有头/无头下通用）
# ---------------------------------------------------------------------------

EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("创建账号")',
    'button:has-text("注册")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
    'button:has-text("Submit")',
    'button:has-text("Verify")',
    'button:has-text("verify")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("Sign up")',
    'button:has-text("创建账号")',
    'button:has-text("注册")',
    'button:has-text("登录")',
    'button:has-text("验证")',
]

# OpenAI now defaults new email signups to a passwordless OTP flow.  The
# password stored by this project is not registered remotely unless the user
# explicitly chooses this fallback on the OTP page first.
PASSWORD_REGISTRATION_FALLBACK_SELECTORS = [
    'a[href="/create-account/password"]',
    'a[href*="/create-account/password"]',
]

OTP_INPUT_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[inputmode='numeric']",
    "input[type='tel']",
    "input[name*='code' i]",
    "input[id*='code' i]",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[name*='totp' i]",
    "input[id*='totp' i]",
    "input[name*='mfa' i]",
    "input[id*='mfa' i]",
    "input[placeholder*='code' i]",
    "input[aria-label*='code' i]",
]

SIGNUP_ENTRY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("注册")',
    'button:has-text("注册")',
]

NAME_INPUT_SELECTORS = [
    'input[name="name"]',
    'input[name="full_name"]',
    'input[autocomplete="name"]',
    'input[id*="name" i]',
    'input[placeholder*="name" i]',
]

AGE_INPUT_SELECTORS = [
    'input[name="age"]',
    'input[type="number"][name*="age" i]',
    'input[placeholder*="age" i]',
    'input[id*="age" i]',
]

BIRTHDAY_INPUT_SELECTORS = [
    'input[name="birthday"]',
    'input[type="date"]',
    'input[name="birthdate"]',
    'input[name="birth_date"]',
    'input[autocomplete="bday"]',
    'input[id*="birth" i]',
    'input[placeholder*="birth" i]',
]

ABOUT_YOU_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("Create account")',
    'button:has-text("创建账号")',
    'button:has-text("注册")',
]

_FIRST_NAMES = [
    "James", "Oliver", "William", "Lucas", "Henry", "Theodore", "Jack", "Levi",
    "Mateo", "Daniel", "Ethan", "Michael", "Samuel", "Alexander", "Owen",
    "Amelia", "Olivia", "Emma", "Charlotte", "Sophia", "Mia", "Isabella",
    "Ava", "Evelyn", "Luna", "Harper", "Camila", "Sofia", "Ella", "Mila",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
]

# 注册后的会话建立判定：登录成功落在 chatgpt.com 首页且带 session cookie
_SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"


def _is_transient_nav_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_aborted",
            "err_connection_failed",
            "err_timed_out",
            "err_network_changed",
            "err_empty_response",
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
        )
    )


def _goto_with_retry(page, url: str, *, log, timeout: int = 45000, attempts: int = 2):
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as exc:
            last_exc = exc
            if not _is_transient_nav_error(exc):
                raise
            log(f"页面导航瞬时断连({attempt + 1}/{attempts}): {exc}，重试...", level="warning")
            time.sleep(2)
    raise last_exc or RuntimeError("页面导航失败")


def _build_proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    parsed = proxy.split("://", 1)
    server = f"{parsed[0]}://{parsed[1]}" if len(parsed) == 2 else proxy
    return {"server": server}


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    """仿人工输入：聚焦、清空、逐段键入。"""
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=8000)
        locator.click()
        locator.fill("")
        time.sleep(random.uniform(0.2, 0.5))
        locator.type(str(value), delay=random.uniform(40, 90))
        return True
    except Exception:
        try:
            page.locator(selector).first.fill(str(value))
            return True
        except Exception:
            return False


def _wait_for_submit_enabled(page, selectors: list[str], *, timeout: int = 20) -> str | None:
    """等待某个提交按钮变为可用（非 disabled）后返回其选择器。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if not locator.is_visible(timeout=500):
                    continue
                disabled = locator.get_attribute("disabled")
                if not disabled:
                    return selector
            except Exception:
                continue
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 8, click_timeout_ms: int = 5000) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout * 1000)
            locator.click(timeout=click_timeout_ms)
            return selector
        except Exception:
            continue
    return None


def _wait_for_any_selector(page, selectors: list[str], *, timeout: int = 30) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=500):
                    return selector
            except Exception:
                continue
        time.sleep(0.4)
    return None


def _get_cookies(page) -> dict[str, str]:
    try:
        cookies = page.context.cookies()
    except Exception:
        return {}
    return {str(c.get("name") or ""): str(c.get("value") or "") for c in cookies}


def _decode_jwt_payload_no_verify(token: str) -> dict:
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_account_id(access_token: str) -> str:
    return str(_decode_jwt_payload_no_verify(access_token).get("https://api.openai.com/profile", {}).get("id") or "")


def _derive_stage_from_page(page) -> str:
    """按 URL + 可见输入框判定当前注册阶段。

    URL 路径是最高优先信号：about-you 页可能带数字输入框（生日），若先做
    选择器判断会被误判成 otp。密码/验证码输入框仅在同一 URL 场景内作为
    次级信号。
    """
    try:
        current_url = str(page.url or "")
    except Exception:
        current_url = ""
    parsed = urlparse(current_url)
    host = parsed.netloc
    path = parsed.path

    # 已登录：chatgpt.com 且存在 session cookie
    cookies = _get_cookies(page)
    if "chatgpt.com" in host and (
        cookies.get(_SESSION_COOKIE_NAME)
        or cookies.get("next-auth.session-token")
        or cookies.get("__Secure-next-auth.session-token")
    ):
        return "complete"

    if "chatgpt.com" in host:
        if "login" in path or "signup" in path:
            return "entry"
        # A successful password + MFA login currently lands on the ChatGPT
        # home page without exposing the NextAuth cookie to Playwright's
        # cookie list immediately.  Let the session endpoint be the source of
        # truth; _fetch_session_via_page will verify that an access token is
        # actually available before the flow is marked complete.
        if path in {"", "/"} or path.startswith(("/c/", "/g/")):
            return "complete"

    if "auth.openai.com" in host:
        if "about-you" in path:
            return "about_you"
        if (
            "email-verification" in path
            or "signup" in path
            or "verify" in path
            or "mfa" in path
            or "two-factor" in path
            or "2fa" in path
            or "challenge" in path
            or "authenticator" in path
        ):
            if _find_visible_selector(page, PASSWORD_INPUT_SELECTORS):
                return "password"
            if _find_visible_selector(page, OTP_INPUT_SELECTORS):
                return "otp"
            return "otp" if any(m in path for m in ("mfa", "two-factor", "2fa", "authenticator")) else "email_verification"
        # 非识别路径，落到选择器兜底

    # 兜底：看可见输入框类型
    if _find_visible_selector(page, PASSWORD_INPUT_SELECTORS):
        return "password"
    if _find_visible_selector(page, OTP_INPUT_SELECTORS):
        return "otp"
    if _find_visible_selector(page, NAME_INPUT_SELECTORS) or _find_visible_selector(page, BIRTHDAY_INPUT_SELECTORS):
        return "about_you"
    if _find_visible_selector(page, EMAIL_INPUT_SELECTORS):
        return "entry"
    return "unknown"


def _find_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=500):
                return selector
        except Exception:
            continue
    return None


def _submit_visible_form(page, input_selector: str) -> bool:
    """在输入框按 Enter 提交（表单 fallback）。"""
    try:
        page.locator(input_selector).first.press("Enter")
        return True
    except Exception:
        return False


def _generate_name() -> str:
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def _generate_age() -> int:
    """18-35 岁之间，保证成年且不显眼。"""
    return random.randint(18, 35)


def _generate_birthdate(age: int | None = None) -> str:
    """按当前日期 - 年龄 生成生日（YYYY-MM-DD），与年龄保持一致。"""
    from datetime import date
    today = date.today()
    age_value = int(age) if age is not None else _generate_age()
    year = today.year - age_value
    return f"{year:04d}-{today.month:02d}-{today.day:02d}"


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    """about-you 页日期输入可能是隐藏/自定义组件，尝试多种方式填入。"""
    for selector in BIRTHDAY_INPUT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=500):
                locator.click()
                locator.fill(birthdate)
                return True
        except Exception:
            continue
    # 尝试通过 JS 设置原生 date input 的 value
    for selector in BIRTHDAY_INPUT_SELECTORS:
        try:
            set_ok = page.evaluate(
                """(sel, value) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                selector,
                birthdate,
            )
            if set_ok:
                return True
        except Exception:
            continue
    return False


def _page_visible_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=3000) or "")
    except Exception:
        return ""


def _dump_debug(page, prefix: str, log) -> None:
    """保存截图 + 输入框结构到 captures/debug，便于远程诊断卡住的页面。"""
    import os
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data", "captures", "debug"
    )
    os.makedirs(base, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=os.path.join(base, f"{prefix}_{stamp}.png"))
    except Exception:
        pass
    try:
        info = page.evaluate(
            """() => ({
                url: location.href,
                body: (document.body && document.body.innerText || '').slice(0, 1500),
                inputs: Array.from(document.querySelectorAll('input')).map(i => ({
                    type: i.type, name: i.name, id: i.id, placeholder: i.placeholder,
                    inputmode: i.inputmode, value: i.value,
                    visible: !!(i.offsetWidth||i.offsetHeight||i.getClientRects().length)
                })),
                buttons: Array.from(document.querySelectorAll('button')).map(b => ({
                    text: (b.innerText||'').trim().slice(0,50), disabled: b.disabled,
                    type: b.type
                })).filter(b => b.text)
            })"""
        )
        with open(os.path.join(base, f"{prefix}_{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        log(f"调试 dump 已保存: {prefix}_{stamp} url={str(info.get('url'))[:120]}")
    except Exception as exc:
        log(f"调试 dump 失败: {exc}", level="warning")


def _auth_error_text(page) -> str:
    text = _page_visible_text(page)
    for token in (
        "Incorrect",
        "invalid",
        "Invalid",
        "account_deactivated",
        "account_suspended",
        "account_banned",
        "Authentication Error",
        "already registered",
        "already signed up",
        "已有账号",
    ):
        if token in text:
            return token
    return ""


# ---------------------------------------------------------------------------
# 会话提取
# ---------------------------------------------------------------------------


def _fetch_session_via_page(page, log) -> dict:
    """登录成功后从浏览器同源请求 /api/auth/session 提取会话。"""
    session_url = f"{CHATGPT_APP}/api/auth/session"
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            payload = page.evaluate(
                """async (sessionUrl) => {
                    const response = await fetch(sessionUrl, {
                        method: "GET",
                        credentials: "include",
                        headers: { "accept": "application/json" },
                    });
                    return { status: response.status, text: await response.text() };
                }""",
                session_url,
            )
        except Exception as exc:
            log(f"浏览器内请求 session API 失败: {exc}", level="warning")
            time.sleep(2)
            continue
        if isinstance(payload, dict) and int(payload.get("status") or 0) == 200:
            try:
                data = json.loads(str(payload.get("text") or ""))
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("accessToken"):
                return data
        time.sleep(2)
    raise RuntimeError("提取 ChatGPT session 失败：/api/auth/session 未返回 accessToken")


def _build_session_result(page, session_data: dict, log) -> dict:
    cookies = _get_cookies(page)
    access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
    session_token = str(cookies.get(_SESSION_COOKIE_NAME) or "").strip()
    id_token = str(session_data.get("idToken") or "").strip()
    user = session_data.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    account_id = _extract_account_id(access_token) or str(user.get("id") or "")
    cookies_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)

    result: dict[str, Any] = {
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": "",
        "id_token": id_token,
        "session_token": session_token,
        "cookies": cookies_header,
        "profile": {
            "email": str(user.get("email") or ""),
            "name": str(user.get("name") or ""),
        },
    }
    # 从 cookies 里挑 refresh_token 相关的键（部分登录态会有）
    for key, value in cookies.items():
        if "refresh" in key.lower() and value:
            result["refresh_token"] = value
            break
    log(
        f"会话提取: account_id={'yes' if account_id else 'no'} "
        f"access_token={'yes' if access_token else 'no'} "
        f"session_token={'yes' if session_token else 'no'}"
    )
    return result


# ---------------------------------------------------------------------------
# 浏览器内 signin 入口（fallback）
# ---------------------------------------------------------------------------


def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None,
                   body: str | None = None, redirect: str = "manual") -> dict:
    payload = page.evaluate(
        """(args) => new Promise((resolve) => {
            fetch(args.url, {
                method: args.method,
                headers: args.headers || {},
                body: args.body || undefined,
                credentials: "include",
                redirect: args.redirect || "follow",
            }).then((resp) =>
                resp.text().then((text) =>
                    resolve({ ok: resp.ok, status: resp.status, url: resp.url, text })
                )
            ).catch((err) =>
                resolve({ ok: false, status: 0, url: "", text: String(err) })
            );
        })""",
        {"url": url, "method": method, "headers": headers or {}, "body": body, "redirect": redirect},
    )
    return payload if isinstance(payload, dict) else {"ok": False, "status": 0, "url": "", "text": ""}


def _browser_mfa_json(page, url: str, access_token: str, *, body: dict[str, Any]) -> dict[str, Any]:
    response = _browser_fetch(
        page,
        url,
        method="POST",
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
        },
        body=json.dumps(body, separators=(",", ":")),
        redirect="follow",
    )
    status = int(response.get("status") or 0)
    text = str(response.get("text") or "")
    if status < 200 or status >= 300:
        raise RuntimeError(f"浏览器内 TOTP API 返回 HTTP {status}: {text[:160]}")
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"浏览器内 TOTP API 返回非 JSON: {text[:160]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("浏览器内 TOTP API 返回格式无效")
    return payload


def _bind_totp_via_page(page, access_token: str) -> str:
    """Bind TOTP inside the just-created browser session."""
    enrollment = _browser_mfa_json(
        page,
        MFA_ENROLL_URL,
        access_token,
        body={"factor_type": "totp"},
    )
    secret, _session_id, activation_body = prepare_totp_activation(enrollment)
    activation = _browser_mfa_json(
        page,
        MFA_ACTIVATE_URL,
        access_token,
        body=activation_body,
    )
    if not activation.get("success"):
        raise RuntimeError(f"浏览器内 TOTP 激活未确认: {str(activation)[:160]}")
    return secret


def _start_browser_signin_via_fetch(page, email: str, device_id: str, log) -> str:
    """浏览器内 POST /api/auth/signin/openai 直入授权链（与前端行为一致）。"""
    try:
        csrf_resp = _browser_fetch(page, f"{CHATGPT_APP}/api/auth/csrf")
        csrf_token = str((csrf_resp.get("text") and json.loads(csrf_resp["text"]).get("csrfToken")) or "")
    except Exception as exc:
        log(f"获取 CSRF token 失败: {exc}", level="warning")
        csrf_token = ""
    if not csrf_token:
        return ""

    query = urlencode({
        "prompt": "login",
        "ext-oai-did": device_id,
        "auth_session_logging_id": str(uuid.uuid4()),
        "screen_hint": "login_or_signup",
        "login_hint": email,
    })
    body = urlencode({
        "callbackUrl": f"{CHATGPT_APP}/",
        "csrfToken": csrf_token,
        "json": "true",
    })
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        method="POST",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "origin": CHATGPT_APP,
            "content-type": "application/x-www-form-urlencoded",
        },
        redirect="follow",
    )
    if result.get("ok"):
        try:
            data = json.loads(str(result.get("text") or ""))
            return str((data or {}).get("url") or "").strip()
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    log,
    *,
    bind_totp_2fa: bool = False,
) -> dict:
    device_id = str(uuid.uuid4())
    log(f"开始 ChatGPT 浏览器注册: {email}")

    # 1) 进入登录页
    _goto_with_retry(page, f"{CHATGPT_APP}/auth/login", log=log)
    time.sleep(1.5)

    # 尝试直接在登录页填邮箱提交
    entry_submitted = False
    email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
    if email_selector:
        _fill_input_like_user(page, email_selector, email)
        log(f"登录页已填邮箱: {email_selector}")
        submit = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=5)
        if not submit:
            _submit_visible_form(page, email_selector)
            log("登录页已用 Enter 提交邮箱")
        else:
            log(f"登录页已点击: {submit}")
        entry_submitted = True
    else:
        # fallback: 浏览器内 signin
        log("登录页未找到邮箱输入框，改用浏览器内 signin")
        authorize_url = _start_browser_signin_via_fetch(page, email, device_id, log)
        if not authorize_url:
            raise RuntimeError("邮箱页无输入框且浏览器内 signin 失败")
        _goto_with_retry(page, authorize_url, log=log)

    # 2) 等待 auth.openai.com 注册链，推进状态机
    time.sleep(1.5)
    seen: dict[str, int] = {}
    password_submitted = False
    password_stage_started_at: float | None = None
    password_submitted_at: float | None = None
    password_submit_retried = False
    password_fallback_requested_at: float | None = None
    password_fallback_attempts = 0
    otp_submitted = False
    about_you_submitted = False
    for step in range(20):
        stage = _derive_stage_from_page(page)
        current_url = str(page.url or "")[:120]
        seen[stage] = seen.get(stage, 0) + 1
        log(f"注册推进 step={step + 1} stage={stage} url={current_url} seen={seen[stage]}")
        stuck_limit = 10 if stage in {"otp", "email_verification"} else 4
        if stage != "password" and seen[stage] > stuck_limit:
            _dump_debug(page, "stuck", log)
            raise RuntimeError(f"注册状态卡住: stage={stage} url={current_url}")

        if stage == "complete":
            if not password_submitted:
                raise RuntimeError(
                    "注册会话已建立，但 OpenAI 端未完成密码设置；拒绝保存无密码账号"
                )
            log("注册完成：会话已建立")
            result = _build_session_result(page, _fetch_session_via_page(page, log), log)
            result["password_registered"] = True
            if bind_totp_2fa:
                log("正在复用注册浏览器会话绑定 TOTP 2FA...")
                try:
                    secret = _bind_totp_via_page(page, str(result.get("access_token") or ""))
                except Exception as exc:
                    result["totp_2fa"] = {
                        "requested": True,
                        "bound": False,
                        "secret": "",
                        "error": str(exc)[:200],
                    }
                    log(f"浏览器会话内 TOTP 2FA 绑定失败: {exc}", level="warning")
                else:
                    result["totp_2fa"] = {
                        "requested": True,
                        "bound": True,
                        "secret": secret,
                        "error": "",
                    }
                    log("浏览器会话内 TOTP 2FA 绑定并激活成功")
            return result

        if stage == "password":
            now = time.monotonic()
            if password_stage_started_at is None:
                password_stage_started_at = now
            if now - password_stage_started_at >= 60:
                raise RuntimeError(
                    f"密码设置页 60 秒未完成: url={current_url}"
                )
            if password_submitted:
                submitted_at = password_submitted_at or now
                elapsed = now - submitted_at
                if elapsed >= 45:
                    raise RuntimeError(
                        f"密码提交后 45 秒未跳转: url={current_url}"
                    )
                if elapsed >= 12 and not password_submit_retried:
                    clicked = _click_first(
                        page,
                        PASSWORD_SUBMIT_SELECTORS,
                        timeout=3,
                    )
                    if not clicked:
                        selector = _find_visible_selector(
                            page,
                            PASSWORD_INPUT_SELECTORS,
                        )
                        if selector:
                            _submit_visible_form(page, selector)
                    password_submit_retried = True
                    log("密码页未跳转，已重试提交一次", level="warning")
                time.sleep(2)
                continue
            selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
            if not selector:
                continue
            _fill_input_like_user(page, selector, password)
            log("已填注册密码")
            clicked = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=6)
            if not clicked:
                _submit_visible_form(page, selector)
                log("密码页已用 Enter 提交")
            password_submitted = True
            password_submitted_at = time.monotonic()
            time.sleep(2)
            continue

        if stage == "otp":
            if not password_submitted:
                if password_fallback_requested_at is not None:
                    if time.monotonic() - password_fallback_requested_at >= 30:
                        raise RuntimeError("已选择密码注册，但 30 秒内未进入密码设置页")
                    time.sleep(2)
                    continue
                password_fallback_attempts += 1
                clicked = _click_first(
                    page,
                    PASSWORD_REGISTRATION_FALLBACK_SELECTORS,
                    timeout=8,
                )
                if clicked:
                    password_fallback_requested_at = time.monotonic()
                    log(f"已从无密码 OTP 注册切换到密码注册: {clicked}")
                    time.sleep(2)
                    continue
                if password_fallback_attempts < 3:
                    time.sleep(2)
                    continue
                raise RuntimeError(
                    "注册进入邮箱验证码页，但未找到密码设置入口；拒绝创建无密码账号"
                )
            # 已提交过验证码就等待页面推进，不重复取码/填码（避免打断提交）
            if otp_submitted:
                time.sleep(2)
                continue
            if not otp_callback:
                raise RuntimeError("注册需要邮箱验证码但未提供 otp_callback")
            log("等待邮箱验证码...")
            code = str(otp_callback() or "").strip()
            if not code:
                raise RuntimeError("未获取到邮箱验证码")
            selector = _wait_for_any_selector(page, OTP_INPUT_SELECTORS, timeout=15)
            if not selector:
                continue
            _fill_input_like_user(page, selector, code)
            log("已填验证码")
            clicked = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=6)
            if not clicked:
                _submit_visible_form(page, selector)
                log("验证码页已用 Enter 提交")
            otp_submitted = True
            time.sleep(3)
            continue

        if stage == "about_you":
            if not about_you_submitted:
                age = _generate_age()
                name = _generate_name()
                birthdate = _generate_birthdate(age)
                name_selector = _find_visible_selector(page, NAME_INPUT_SELECTORS)
                if name_selector:
                    _fill_input_like_user(page, name_selector, name)
                    log(f"已填姓名: {name}")
                age_selector = _find_visible_selector(page, AGE_INPUT_SELECTORS)
                if age_selector:
                    _fill_input_like_user(page, age_selector, str(age))
                    log(f"已填年龄: {age}")
                # birthday 是隐藏字段，通常由前端按年龄计算；仅当为空时 JS 兜底
                time.sleep(1.0)
                birthday_filled = False
                for sel in BIRTHDAY_INPUT_SELECTORS:
                    try:
                        if page.locator(sel).first.is_visible(timeout=300):
                            val = str(page.locator(sel).first.input_value(timeout=1000) or "")
                            birthday_filled = bool(val.strip())
                            break
                    except Exception:
                        continue
                if not birthday_filled:
                    _sync_hidden_birthday_input(page, birthdate, log)
                    log(f"前端未自动计算生日，已 JS 兜底: {birthdate}")
                # 等待提交按钮变为可用（React 校验完成后），最多 25s
                submit_selector = _wait_for_submit_enabled(page, ABOUT_YOU_SUBMIT_SELECTORS, timeout=25)
                if not submit_selector:
                    _dump_debug(page, "about_you_stuck", log)
                    raise RuntimeError("about_you 提交按钮长时间不可用（表单未通过校验）")
                _click_first(page, [submit_selector], timeout=8)
                log("about_you 已提交")
                about_you_submitted = True
            # 提交后等待页面推进（create_account API 走代理可能 30-60s）
            wait_deadline = time.time() + 60
            while time.time() < wait_deadline:
                if _derive_stage_from_page(page) != "about_you":
                    break
                time.sleep(2)
            if _derive_stage_from_page(page) == "about_you":
                # 60s 仍未跳转：允许重填重试一次
                about_you_submitted = False
                _dump_debug(page, "about_you_stuck", log)
                log("about_you 提交后 60s 未跳转，允许重试", level="warning")
            time.sleep(1)
            continue

        if stage == "email_verification":
            # 邮箱验证页：若页面上有继续/提交按钮则点一下，等后续密码页
            clicked = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=3)
            if clicked:
                log(f"邮箱验证页已点击: {clicked}")
            time.sleep(1.5)
            continue

        if stage == "entry":
            # 已提交过就只等待跳转，绝不重复提交邮箱
            if not entry_submitted:
                email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=8)
                if email_selector:
                    _fill_input_like_user(page, email_selector, email)
                    _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=5)
                    entry_submitted = True
                    log("已重新提交邮箱")
            time.sleep(1.5)
            continue

        # unknown：等待页面就绪或检查错误
        error_text = _auth_error_text(page)
        if error_text:
            raise RuntimeError(f"注册流程报错: {error_text}")
        time.sleep(1.5)

    raise RuntimeError("注册状态机超出最大步数")


class ChatGPTBrowserRegister:
    """Camoufox 有头/无头 ChatGPT 注册执行器。

    ``headless`` 控制窗口形态；流程代码完全一致。
    """

    def __init__(
        self,
        *,
        headless: bool,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        bind_totp_2fa: bool = False,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = bool(headless)
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.bind_totp_2fa = bool(bind_totp_2fa)
        self.log = log_fn

    def run(self, email: str, password: str) -> dict:
        launch_opts: dict[str, Any] = {"headless": self.headless}
        try:
            from camoufox.addons import DefaultAddons

            launch_opts["exclude_addons"] = [DefaultAddons.UBO]
        except Exception:
            pass
        proxy_config = _build_proxy_config(self.proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config
            launch_opts["geoip"] = True

        self.log(f"启动 camoufox 浏览器 (headless={self.headless})")
        with Camoufox(**launch_opts) as browser:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()
            final = _browser_registration_flow(
                page,
                email,
                password,
                self.otp_callback or (lambda: ""),
                self.log,
                bind_totp_2fa=self.bind_totp_2fa,
            )
            result = dict(final)
            result.update({
                "email": email,
                "password": password,
                "platform": "chatgpt",
                "_registration_proxy": self.proxy or "",
            })
            return result


__all__ = ["ChatGPTBrowserRegister"]
