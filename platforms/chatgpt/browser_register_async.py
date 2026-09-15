"""ChatGPT 浏览器注册流程 —— async 版（供共享浏览器进程池使用）。

与 ``browser_register.py``（sync 版）的注册状态机完全一致，差别在于：

* 所有 Playwright 操作是 ``await`` 的（async_api 对象）。
* ``time.sleep`` 换成 ``asyncio.sleep``。
* OTP callback（同步阻塞的邮箱轮询）通过 ``asyncio.to_thread`` 丢到线程池，
  避免一个注册的 OTP 等待阻塞整个事件循环里其他 context 的推进。
* 每个 context 用 ``AsyncNewContext`` 生成独立指纹（随机 preset + 独立噪声种子 +
  随机 OS），避免共享同一浏览器进程时多个账号呈现相同设备特征。

选择器常量与纯函数（姓名/年龄/生日/JWT 解析等）复用 sync 版，避免两份漂移。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from camoufox.async_api import AsyncNewContext

from .browser_register import (
    ABOUT_YOU_SUBMIT_SELECTORS,
    AGE_INPUT_SELECTORS,
    BIRTHDAY_INPUT_SELECTORS,
    EMAIL_INPUT_SELECTORS,
    EMAIL_SUBMIT_SELECTORS,
    NAME_INPUT_SELECTORS,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    PASSWORD_REGISTRATION_FALLBACK_SELECTORS,
    PASSWORD_SUBMIT_SELECTORS,
    _SESSION_COOKIE_NAME,
    _build_proxy_config,
    _decode_jwt_payload_no_verify,
    _extract_account_id,
    _generate_age,
    _generate_birthdate,
    _generate_name,
    _is_transient_nav_error,
)
from .constants import CHATGPT_APP
from .mfa import MFA_ACTIVATE_URL, MFA_ENROLL_URL, prepare_totp_activation

# context 级指纹：随机选一个桌面 OS，配合随机 preset 让同浏览器内每个
# context 呈现不同 navigator.platform / screen / canvas / font 噪声。
_CONTEXT_OS_CHOICES = ("windows", "macos")


class BrowserProxyBlockedError(RuntimeError):
    """The current proxy route cannot reach a usable ChatGPT registration page."""


class BrowserProcessCrashedError(RuntimeError):
    """The shared Camoufox process disappeared while a worker was using it."""


_BROWSER_PROCESS_LOST_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "browser disconnected",
    "target closed",
    "connection closed while reading from the driver",
    "playwright connection closed",
)


def is_browser_process_lost_error(exc: BaseException | str) -> bool:
    """Return whether a Playwright error means the browser process is gone."""
    message = str(exc or "").strip().lower()
    return any(marker in message for marker in _BROWSER_PROCESS_LOST_MARKERS)


_HARD_PROXY_BLOCK_MARKERS = (
    "unable to load site",
    "if you are using a vpn",
    "try turning it off",
    "access denied",
    "this website is using a security service",
    "sorry, you have been blocked",
)
_CLOUDFLARE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
    "performing security verification",
    "cloudflare ray id",
)


async def _page_snapshot(page, *, body_limit: int = 600) -> dict[str, Any]:
    try:
        title = str(await page.title() or "").strip()
    except Exception:
        title = ""
    try:
        body = str(await page.locator("body").inner_text(timeout=2500) or "").strip()
    except Exception:
        body = ""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    return {
        "url": url[:500],
        "title": title[:200],
        "body": re.sub(r"\s+", " ", body)[:body_limit],
    }


async def _hard_proxy_block_reason(page) -> str:
    snapshot = await _page_snapshot(page)
    combined = f"{snapshot['title']} {snapshot['body']}".lower()
    marker = next((item for item in _HARD_PROXY_BLOCK_MARKERS if item in combined), "")
    if not marker:
        return ""
    return (
        f"ChatGPT 拒绝当前代理（{marker}）"
        f" title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
    )


async def _is_cloudflare_challenge(page) -> bool:
    """检测页面是否是 Cloudflare 挑战页（"Just a moment..." 等）。"""
    try:
        snapshot = await _page_snapshot(page)
        combined = f"{snapshot['title']} {snapshot['body']}".lower()
        return any(marker in combined for marker in _CLOUDFLARE_MARKERS)
    except Exception:
        return False


async def _wait_cloudflare_pass(page, log, timeout: int = 30) -> bool:
    """等待 Cloudflare 挑战自动通过（camoufox 是真实浏览器，几秒内完成 JS 挑战）。"""
    if not await _is_cloudflare_challenge(page):
        return True
    log("检测到 Cloudflare 挑战，等待自动通过...", level="warning")
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        hard_block = await _hard_proxy_block_reason(page)
        if hard_block:
            log(hard_block, level="warning")
            return False
        if not await _is_cloudflare_challenge(page):
            log("Cloudflare 挑战已通过")
            return True
    log("Cloudflare 挑战超时未通过", level="warning")
    return False


async def _goto_with_retry(page, url: str, *, log, timeout: int = 45000, attempts: int = 1):
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            status = int(getattr(response, "status", 0) or 0)
            hard_block = await _hard_proxy_block_reason(page)
            if hard_block:
                raise BrowserProxyBlockedError(
                    f"HTTP {status or 'unknown'}: {hard_block}"
                )
            if not await _wait_cloudflare_pass(page, log):
                snapshot = await _page_snapshot(page)
                raise BrowserProxyBlockedError(
                    f"HTTP {status or 'unknown'}: Cloudflare 挑战无法通过；"
                    f"title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
                )
            if status in {403, 407, 429} or status >= 500:
                snapshot = await _page_snapshot(page)
                raise BrowserProxyBlockedError(
                    f"ChatGPT 登录页 HTTP {status}; title={snapshot['title']!r} "
                    f"body={snapshot['body'][:240]!r}"
                )
            log(f"ChatGPT 登录页已加载: HTTP {status or 'unknown'}")
            return response
        except BrowserProxyBlockedError:
            raise
        except Exception as exc:
            last_exc = exc
            retryable = _is_transient_nav_error(exc) or "timeout" in str(exc).lower()
            if not retryable:
                raise
            # Playwright's DOMContentLoaded timeout does not necessarily mean
            # the page is unusable.  Under CPU saturation the login form can
            # already be interactive while a late script keeps the lifecycle
            # event pending.  Preserve that usable page instead of throwing
            # away the context and rotating a healthy proxy.
            hard_block = await _hard_proxy_block_reason(page)
            if hard_block:
                raise BrowserProxyBlockedError(hard_block) from exc
            email_selector = await _wait_for_any_selector(
                page,
                EMAIL_INPUT_SELECTORS,
                timeout=2,
            )
            if email_selector:
                log(
                    f"导航等待超时但登录表单已可用: {email_selector}",
                    level="warning",
                )
                return None
            log(f"页面导航瞬时断连({attempt + 1}/{attempts}): {exc}，重试...", level="warning")
            await asyncio.sleep(2)
    raise BrowserProxyBlockedError(
        f"代理导航 ChatGPT 失败: {last_exc or 'unknown navigation error'}"
    ) from last_exc


async def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=8000)
        await locator.click()
        await locator.fill("")
        # 用 fill 而不是 type：async 版 type 逐字输入长字符串会被页面
        # React 重渲染中断（实测长邮箱只输入了前 7 个字符），导致邮箱无效。
        await locator.fill(str(value))
        return True
    except Exception:
        try:
            await page.locator(selector).first.fill(str(value))
            return True
        except Exception:
            return False


async def _wait_for_submit_enabled(page, selectors: list[str], *, timeout: int = 20) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if not await locator.is_visible(timeout=500):
                    continue
                if not await locator.get_attribute("disabled"):
                    return selector
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return None


async def _click_first(page, selectors: list[str], *, timeout: int = 8, click_timeout_ms: int = 5000) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout * 1000)
            await locator.click(timeout=click_timeout_ms)
            return selector
        except Exception:
            continue
    return None


async def _wait_for_any_selector(page, selectors: list[str], *, timeout: int = 30) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                if await page.locator(selector).first.is_visible(timeout=500):
                    return selector
            except Exception:
                continue
        await asyncio.sleep(0.4)
    return None


async def _find_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=500):
                return selector
        except Exception:
            continue
    return None


async def _submit_visible_form(page, input_selector: str) -> bool:
    try:
        await page.locator(input_selector).first.press("Enter")
        return True
    except Exception:
        return False


async def _get_cookies(page) -> dict[str, str]:
    try:
        cookies = await page.context.cookies()
    except Exception:
        return {}
    return {str(c.get("name") or ""): str(c.get("value") or "") for c in cookies}


async def _derive_stage_from_page(page) -> str:
    try:
        current_url = str(page.url or "")
    except Exception:
        current_url = ""

    if await _hard_proxy_block_reason(page):
        return "blocked"

    # Cloudflare 挑战页：URL 可能仍是目标地址，但页面内容是挑战，优先识别
    if await _is_cloudflare_challenge(page):
        return "cloudflare"

    parsed = urlparse(current_url)
    host = parsed.netloc
    path = parsed.path

    cookies = await _get_cookies(page)
    if "chatgpt.com" in host and cookies.get(_SESSION_COOKIE_NAME):
        return "complete"

    if "chatgpt.com" in host:
        if "login" in path or "signup" in path:
            return "entry"
        if path in {"", "/"}:
            return "complete"

    if "auth.openai.com" in host:
        if "about-you" in path:
            return "about_you"
        if "email-verification" in path or "signup" in path or "verify" in path:
            if await _find_visible_selector(page, PASSWORD_INPUT_SELECTORS):
                return "password"
            if await _find_visible_selector(page, OTP_INPUT_SELECTORS):
                return "otp"
            return "email_verification"

    if await _find_visible_selector(page, PASSWORD_INPUT_SELECTORS):
        return "password"
    if await _find_visible_selector(page, OTP_INPUT_SELECTORS):
        return "otp"
    if await _find_visible_selector(page, NAME_INPUT_SELECTORS) or await _find_visible_selector(page, BIRTHDAY_INPUT_SELECTORS):
        return "about_you"
    if await _find_visible_selector(page, EMAIL_INPUT_SELECTORS):
        return "entry"
    return "unknown"


async def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    for selector in BIRTHDAY_INPUT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click()
                await locator.fill(birthdate)
                return True
        except Exception:
            continue
    for selector in BIRTHDAY_INPUT_SELECTORS:
        try:
            set_ok = await page.evaluate(
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


async def _accept_about_you_consents(page, log) -> bool:
    """Accept the current about-you page's required consent group, if present.

    The Korea-localized form renders a select-all checkbox followed by three
    mandatory consent checkboxes.  ``check()`` is intentionally used instead
    of ``click()`` so retries cannot toggle an already accepted group off.
    """
    try:
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()
    except Exception:
        return False

    for index in range(count):
        try:
            checkbox = checkboxes.nth(index)
            if not await checkbox.is_visible(timeout=300):
                continue
            if not await checkbox.is_checked():
                await checkbox.check(timeout=3000)
                log("about_you 已接受必选隐私条款")
            return True
        except Exception:
            continue
    return False


async def _confirm_about_you_birthday(page, log, *, timeout: float = 1.0) -> bool:
    """Confirm the birthday modal shown after submitting about-you."""
    selectors = [
        '[role="dialog"] button:has-text("OK")',
        '[role="dialog"] button:has-text("Confirm")',
        'button:has-text("OK")',
    ]
    deadline = time.time() + max(float(timeout), 0.0)
    first_pass = True
    while first_pass or time.time() <= deadline:
        first_pass = False
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if not await locator.is_visible(timeout=250):
                    continue
                await locator.click(timeout=3000)
                log("about_you 已确认生日")
                return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _page_visible_text(page) -> str:
    try:
        return str(await page.locator("body").inner_text(timeout=3000) or "")
    except Exception:
        return ""


async def _auth_error_text(page) -> str:
    text = await _page_visible_text(page)
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


async def _capture_failure(page, *, reason: str, log) -> None:
    diagnostics_dir = str(
        os.getenv("CHATGPT_BROWSER_DIAGNOSTICS_DIR", "data/browser-diagnostics")
        or ""
    ).strip()
    snapshot = await _page_snapshot(page, body_limit=1200)
    log(
        "浏览器失败诊断: "
        f"url={snapshot['url'][:180]} title={snapshot['title']!r} "
        f"body={snapshot['body'][:400]!r}",
        level="warning",
    )
    if not diagnostics_dir:
        return
    try:
        target_dir = Path(diagnostics_dir)
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        screenshot_path = target_dir / f"{stem}.png"
        metadata_path = target_dir / f"{stem}.json"
        metadata = {**snapshot, "reason": str(reason)[:2000]}
        await asyncio.to_thread(
            metadata_path.write_text,
            json.dumps(metadata, ensure_ascii=False, indent=2),
            "utf-8",
        )
        normalized_reason = str(reason or "").lower()
        skip_screenshot = snapshot["url"] in {"", "about:blank"} or any(
            marker in normalized_reason
            for marker in (
                "page.goto: timeout",
                "代理导航 chatgpt 失败",
                "邮箱提交后入口",
            )
        )
        if skip_screenshot:
            log(f"高频代理失败仅保存元数据: {metadata_path}", level="warning")
            return
        # Full-page screenshots force an expensive layout/paint of the whole
        # document.  A viewport capture contains the visible error state and
        # avoids a second CPU spike when many proxies fail together.
        await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10000)
        log(f"失败截图已保存: {screenshot_path}", level="warning")
    except Exception as exc:
        log(f"保存失败截图失败: {exc}", level="warning")


async def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None,
                         body: str | None = None) -> dict:
    payload = await page.evaluate(
        """(args) => new Promise((resolve) => {
            fetch(args.url, {
                method: args.method,
                headers: args.headers || {},
                body: args.body || undefined,
                credentials: "include",
                redirect: "follow",
            }).then((resp) =>
                resp.text().then((text) =>
                    resolve({ ok: resp.ok, status: resp.status, url: resp.url, text })
                )
            ).catch((err) =>
                resolve({ ok: false, status: 0, url: "", text: String(err) })
            );
        })""",
        {"url": url, "method": method, "headers": headers or {}, "body": body},
    )
    return payload if isinstance(payload, dict) else {"ok": False, "status": 0, "url": "", "text": ""}


async def _browser_mfa_json(
    page,
    url: str,
    access_token: str,
    *,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await _browser_fetch(
        page,
        url,
        method="POST",
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
        },
        body=json.dumps(body, separators=(",", ":")),
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


async def _bind_totp_via_page(page, access_token: str) -> str:
    """Bind TOTP before the registration context and its fingerprint close."""
    enrollment = await _browser_mfa_json(
        page,
        MFA_ENROLL_URL,
        access_token,
        body={"factor_type": "totp"},
    )
    secret, _session_id, activation_body = prepare_totp_activation(enrollment)
    activation = await _browser_mfa_json(
        page,
        MFA_ACTIVATE_URL,
        access_token,
        body=activation_body,
    )
    if not activation.get("success"):
        raise RuntimeError(f"浏览器内 TOTP 激活未确认: {str(activation)[:160]}")
    return secret


async def _fetch_session_via_page(page, log) -> dict:
    session_url = f"{CHATGPT_APP}/api/auth/session"
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            payload = await page.evaluate(
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
            await asyncio.sleep(2)
            continue
        if isinstance(payload, dict) and int(payload.get("status") or 0) == 200:
            try:
                data = json.loads(str(payload.get("text") or ""))
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("accessToken"):
                return data
        await asyncio.sleep(2)
    raise RuntimeError("提取 ChatGPT session 失败：/api/auth/session 未返回 accessToken")


async def _build_session_result(page, session_data: dict, log) -> dict:
    cookies = await _get_cookies(page)
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


async def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    log,
    startup_gate: asyncio.Semaphore | None = None,
    bind_totp_2fa: bool = False,
    is_login: bool = False,
) -> dict:
    action_desc = "登录" if is_login else "注册"
    log(f"开始 ChatGPT 浏览器{action_desc}: {email}")

    async def open_registration_entry() -> bool:
        await _goto_with_retry(page, f"{CHATGPT_APP}/auth/login", log=log)
        await asyncio.sleep(1.5)

        email_selector = await _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            hard_block = await _hard_proxy_block_reason(page)
            snapshot = await _page_snapshot(page)
            raise BrowserProxyBlockedError(
                hard_block
                or (
                    "登录页未找到邮箱输入框，当前代理返回了不可用页面；"
                    f"title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
                )
            )

        await _fill_input_like_user(page, email_selector, email)
        log(f"登录页已填邮箱: {email_selector}")
        submit = await _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=5)
        if not submit:
            await _submit_visible_form(page, email_selector)
            log("登录页已用 Enter 提交邮箱")
        else:
            log(f"登录页已点击: {submit}")

        # Keep the CPU-heavy entry/auth transition inside the startup gate.
        # Once the password/OTP stage appears the worker mostly waits on
        # network and mailbox I/O, so it no longer needs scarce startup slots.
        entry_deadline = time.time() + 45
        while time.time() < entry_deadline:
            if await _derive_stage_from_page(page) != "entry":
                break
            await asyncio.sleep(2)
        if await _derive_stage_from_page(page) == "entry":
            hard_block = await _hard_proxy_block_reason(page)
            snapshot = await _page_snapshot(page)
            raise BrowserProxyBlockedError(
                hard_block
                or (
                    "邮箱提交后入口 45 秒未跳转；"
                    f"title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
                )
            )
        return True

    if startup_gate is None:
        entry_submitted = await open_registration_entry()
    else:
        async with startup_gate:
            entry_submitted = await open_registration_entry()

    seen: dict[str, int] = {}
    password_submitted = False
    password_stage_started_at: float | None = None
    password_submitted_at: float | None = None
    password_submit_retried = False
    password_fallback_requested_at: float | None = None
    password_fallback_attempts = 0
    otp_submitted = False
    otp_submitted_at: float | None = None
    otp_submit_retried = False
    otp_input_selector: str | None = None
    about_you_submitted = False
    email_verification_started_at: float | None = None
    email_verification_retried = False
    for step in range(40):
        stage = await _derive_stage_from_page(page)
        current_url = str(page.url or "")[:120]
        seen[stage] = seen.get(stage, 0) + 1
        log(f"注册推进 step={step + 1} stage={stage} url={current_url} seen={seen[stage]}")
        # OTP and email verification use explicit elapsed-time deadlines below.
        # Count-based limits are too sensitive to scheduler speed at 30-way load.
        if (
            stage not in {"password", "otp", "email_verification", "cloudflare"}
            and seen[stage] > 4
        ):
            if stage in {"entry", "blocked", "unknown"}:
                snapshot = await _page_snapshot(page)
                raise BrowserProxyBlockedError(
                    f"注册入口状态卡住: stage={stage} url={current_url} "
                    f"title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
                )
            raise RuntimeError(f"注册状态卡住: stage={stage} url={current_url}")

        if stage == "blocked":
            raise BrowserProxyBlockedError(
                await _hard_proxy_block_reason(page) or "ChatGPT 拒绝当前代理线路"
            )

        if stage == "cloudflare":
            # Cloudflare 挑战页：等待自动通过，不计入普通卡住判定（放宽到 12 次约 60s）
            if seen[stage] > 12:
                raise BrowserProxyBlockedError(f"Cloudflare 挑战持续未通过: {current_url}")
            if not await _wait_cloudflare_pass(page, log, timeout=30):
                raise BrowserProxyBlockedError(
                    f"Cloudflare 挑战未通过或代理被拒绝: {current_url}"
                )
            continue

        if stage == "complete":
            if not is_login and not password_submitted:
                raise RuntimeError(
                    "注册会话已建立，但 OpenAI 端未完成密码设置；拒绝保存无密码账号"
                )
            log(f"{action_desc}完成：会话已建立")
            result = await _build_session_result(
                page,
                await _fetch_session_via_page(page, log),
                log,
            )
            result["password_registered"] = True
            if bind_totp_2fa:
                log("正在复用注册浏览器会话绑定 TOTP 2FA...")
                try:
                    secret = await _bind_totp_via_page(
                        page,
                        str(result.get("access_token") or ""),
                    )
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
                    clicked = await _click_first(
                        page,
                        PASSWORD_SUBMIT_SELECTORS,
                        timeout=3,
                    )
                    if not clicked:
                        selector = await _find_visible_selector(
                            page,
                            PASSWORD_INPUT_SELECTORS,
                        )
                        if selector:
                            await _submit_visible_form(page, selector)
                    password_submit_retried = True
                    log("密码页未跳转，已重试提交一次", level="warning")
                await asyncio.sleep(2)
                continue
            selector = await _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
            if not selector:
                continue
            await _fill_input_like_user(page, selector, password)
            log("已填注册密码")
            if not await _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=6):
                await _submit_visible_form(page, selector)
                log("密码页已用 Enter 提交")
            password_submitted = True
            password_submitted_at = time.monotonic()
            await asyncio.sleep(2)
            continue

        if stage == "otp":
            if not is_login and not password_submitted:
                if password_fallback_requested_at is not None:
                    if time.monotonic() - password_fallback_requested_at >= 30:
                        raise RuntimeError("已选择密码注册，但 30 秒内未进入密码设置页")
                    await asyncio.sleep(2)
                    continue
                password_fallback_attempts += 1
                clicked = await _click_first(
                    page,
                    PASSWORD_REGISTRATION_FALLBACK_SELECTORS,
                    timeout=8,
                )
                if clicked:
                    password_fallback_requested_at = time.monotonic()
                    log(f"已从无密码 OTP 注册切换到密码注册: {clicked}")
                    await asyncio.sleep(2)
                    continue
                if password_fallback_attempts < 3:
                    await asyncio.sleep(2)
                    continue
                raise RuntimeError(
                    "注册进入邮箱验证码页，但未找到密码设置入口；拒绝创建无密码账号"
                )
            # Navigation can complete between stage detection and logging.
            # Once a code was submitted, never poll/fill it again; wait for
            # the next URL instead of reusing a stale OTP on about-you.
            if otp_submitted:
                submitted_at = (
                    otp_submitted_at
                    if otp_submitted_at is not None
                    else time.monotonic()
                )
                elapsed = time.monotonic() - submitted_at
                if elapsed >= 60:
                    raise RuntimeError(
                        f"验证码提交后 60 秒未跳转: url={current_url}"
                    )
                if elapsed >= 12 and not otp_submit_retried:
                    clicked = await _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=3)
                    if not clicked and otp_input_selector:
                        await _submit_visible_form(page, otp_input_selector)
                    otp_submit_retried = True
                    log("验证码页未跳转，已使用同一验证码重试提交", level="warning")
                await asyncio.sleep(2)
                continue
            if not otp_callback:
                raise RuntimeError("注册需要邮箱验证码但未提供 otp_callback")
            log("等待邮箱验证码...")
            # OTP 轮询是同步阻塞的，丢到线程池避免阻塞事件循环里其他 context
            code = str(await asyncio.to_thread(otp_callback) or "").strip()
            if not code:
                raise RuntimeError("未获取到邮箱验证码")
            selector = await _wait_for_any_selector(page, OTP_INPUT_SELECTORS, timeout=15)
            if not selector:
                continue
            otp_input_selector = selector
            await _fill_input_like_user(page, selector, code)
            log("已填验证码")
            if not await _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=6):
                await _submit_visible_form(page, selector)
                log("验证码页已用 Enter 提交")
            otp_submitted = True
            otp_submitted_at = time.monotonic()
            await asyncio.sleep(3)
            continue

        if stage == "about_you":
            if not about_you_submitted:
                age = _generate_age()
                name = _generate_name()
                birthdate = _generate_birthdate(age)
                name_selector = await _find_visible_selector(page, NAME_INPUT_SELECTORS)
                if name_selector:
                    await _fill_input_like_user(page, name_selector, name)
                    log(f"已填姓名: {name}")
                age_selector = await _find_visible_selector(page, AGE_INPUT_SELECTORS)
                if age_selector:
                    await _fill_input_like_user(page, age_selector, str(age))
                    log(f"已填年龄: {age}")
                await asyncio.sleep(1.0)
                birthday_filled = False
                for sel in BIRTHDAY_INPUT_SELECTORS:
                    try:
                        if await page.locator(sel).first.is_visible(timeout=300):
                            val = str(await page.locator(sel).first.input_value(timeout=1000) or "")
                            birthday_filled = bool(val.strip())
                            break
                    except Exception:
                        continue
                if not birthday_filled:
                    await _sync_hidden_birthday_input(page, birthdate, log)
                    log(f"前端未自动计算生日，已 JS 兜底: {birthdate}")
                await _accept_about_you_consents(page, log)
                submit_selector = await _wait_for_submit_enabled(page, ABOUT_YOU_SUBMIT_SELECTORS, timeout=25)
                if not submit_selector:
                    raise RuntimeError("about_you 提交按钮长时间不可用（表单未通过校验）")
                await _click_first(page, [submit_selector], timeout=8)
                log("about_you 已提交")
                about_you_submitted = True
                await _confirm_about_you_birthday(page, log, timeout=5)
            wait_deadline = time.time() + 60
            while time.time() < wait_deadline:
                if await _derive_stage_from_page(page) != "about_you":
                    break
                await _confirm_about_you_birthday(page, log, timeout=0.5)
                await asyncio.sleep(1)
            if await _derive_stage_from_page(page) == "about_you":
                about_you_submitted = False
                log("about_you 提交后 60s 未跳转，允许重填重试", level="warning")
            await asyncio.sleep(1)
            continue

        if stage == "email_verification":
            now = time.monotonic()
            if email_verification_started_at is None:
                email_verification_started_at = now
                if await _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=3):
                    log("邮箱验证页已点击继续")
            elapsed = now - email_verification_started_at
            if elapsed >= 60:
                raise RuntimeError(
                    f"邮箱验证页 60 秒未跳转: url={current_url}"
                )
            if elapsed >= 12 and not email_verification_retried:
                if await _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=3):
                    log("邮箱验证页未跳转，已重试点击继续", level="warning")
                email_verification_retried = True
            await asyncio.sleep(2)
            continue

        if stage == "entry":
            hard_block = await _hard_proxy_block_reason(page)
            if hard_block:
                raise BrowserProxyBlockedError(hard_block)
            error_text = await _auth_error_text(page)
            if error_text:
                raise RuntimeError(f"注册流程报错: {error_text}")
            if not entry_submitted:
                email_selector = await _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=8)
                if email_selector:
                    await _fill_input_like_user(page, email_selector, email)
                    await _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=5)
                    entry_submitted = True
                    log("已重新提交邮箱")
            # 等待跳转（signin 走代理可能慢，最多 30s）
            deadline = time.time() + 30
            while time.time() < deadline:
                if await _derive_stage_from_page(page) != "entry":
                    break
                await asyncio.sleep(2)
            if await _derive_stage_from_page(page) == "entry":
                snapshot = await _page_snapshot(page)
                raise BrowserProxyBlockedError(
                    "邮箱提交后入口 30 秒未跳转；"
                    f"title={snapshot['title']!r} body={snapshot['body'][:240]!r}"
                )
            continue

        error_text = await _auth_error_text(page)
        if error_text:
            raise RuntimeError(f"注册流程报错: {error_text}")
        await asyncio.sleep(1.5)

    raise RuntimeError("注册状态机超出最大步数")


async def register_in_context(browser, *, email: str, password: str, proxy: str | None,
                              otp_callback: Callable[[], str], log,
                              startup_gate: asyncio.Semaphore | None = None,
                              bind_totp_2fa: bool = False,
                              is_login: bool = False,
                              close_timeout_seconds: float = 15.0,
                              health_state: dict[str, Any] | None = None) -> dict:
    """在共享浏览器进程里开一个独立指纹 context，跑完注册并关闭 context。"""
    health = health_state if health_state is not None else {}
    try:
        context = await AsyncNewContext(
            browser,
            os=random.choice(_CONTEXT_OS_CHOICES),
            proxy=_build_proxy_config(proxy),
            # A smaller desktop viewport lowers paint/compositing cost by roughly
            # 28% while keeping the current registration form in desktop layout.
            viewport={"width": 1024, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
            reduced_motion="reduce",
            service_workers="block",
        )
    except Exception as exc:
        if is_browser_process_lost_error(exc):
            health["recycle_required"] = True
            health["context_started"] = False
            health["reason"] = str(exc)[:300]
            raise BrowserProcessCrashedError(
                f"共享浏览器进程已退出，无法创建 context: {exc}"
            ) from exc
        raise
    health["context_started"] = True
    try:
        try:
            page = await context.new_page()
        except Exception as exc:
            if is_browser_process_lost_error(exc):
                health["recycle_required"] = True
                health["page_started"] = False
                health["reason"] = str(exc)[:300]
                raise BrowserProcessCrashedError(
                    f"共享浏览器进程已退出，无法创建页面: {exc}"
                ) from exc
            raise
        health["page_started"] = True
        try:
            final = await _browser_registration_flow(
                page,
                email,
                password,
                otp_callback,
                log,
                startup_gate=startup_gate,
                bind_totp_2fa=bind_totp_2fa,
                is_login=is_login,
            )
            result = dict(final)
            result.update({
                "email": email,
                "password": password,
                "platform": "chatgpt",
                "_registration_proxy": proxy or "",
            })
            return result
        except Exception as exc:
            if is_browser_process_lost_error(exc):
                health["recycle_required"] = True
                health["reason"] = str(exc)[:300]
            await _capture_failure(page, reason=str(exc), log=log)
            if is_browser_process_lost_error(exc):
                raise BrowserProcessCrashedError(
                    f"共享浏览器进程在注册过程中退出: {exc}"
                ) from exc
            raise
    finally:
        close_task = None
        try:
            close_task = asyncio.create_task(context.close())
            done, _ = await asyncio.wait(
                {close_task},
                timeout=max(float(close_timeout_seconds or 0), 0.1),
            )
            if not done:
                close_task.cancel()
                health["recycle_required"] = True
                health["reason"] = "browser context close timeout"
                log(
                    "浏览器 context 关闭超时，将回收对应 Camoufox 进程",
                    level="warning",
                )
            else:
                close_task.result()
        except Exception as exc:
            if close_task is not None and not close_task.done():
                close_task.cancel()
            health["recycle_required"] = True
            health["reason"] = str(exc)[:300] or "browser context close failed"
            log(
                f"浏览器 context 关闭失败，将回收对应 Camoufox 进程: {exc}",
                level="warning",
            )


__all__ = [
    "BrowserProcessCrashedError",
    "BrowserProxyBlockedError",
    "is_browser_process_lost_error",
    "register_in_context",
]
