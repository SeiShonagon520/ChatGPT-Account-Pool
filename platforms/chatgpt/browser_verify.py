"""Browser-based credential verification for accounts.

curl_cffi protocol requests get flagged by Cloudflare as "not a real browser"
(HTTP 403 challenge) even on clean IPs.  Verifying an access token through a
real camoufox browser context fixes that: the ``fetch()`` runs inside a genuine
browser page, so Cloudflare sees a real browser fingerprint and origin.

This module exposes ``BrowserFetchSession`` — a small context manager that
opens one camoufox page and yields a ``browser_fetch`` callable compatible with
``check_chatgpt_access_token(..., browser_fetch=...)``.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional

from camoufox.async_api import AsyncCamoufox

# BrowserFetchFn: ``(url, method, headers, body) -> {"status": int, "text": str, "headers": dict}``
BrowserFetchFn = Callable[..., dict[str, Any]]


async def _maybe_await(value: Any) -> Any:
    """Return async Playwright values while keeping fakes/test doubles usable."""
    return await value if inspect.isawaitable(value) else value

_FETCH_JS = """
async ({ url, method, headers, body, timeoutMs }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
  try {
    const resp = await fetch(url, {
      method,
      headers: headers || {},
      body: body === null ? undefined : body,
      redirect: 'manual',
      signal: controller.signal,
    });
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });
    let text = '';
    try { text = await resp.text(); } catch {}
    return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text };
  } catch (e) {
    return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


def make_browser_fetch(page: Any, *, timeout_ms: int = 30000) -> BrowserFetchFn:
    """Return a ``browser_fetch`` callable bound to a live browser context.

    Uses playwright's ``context.request`` (the browser network stack) instead
    of ``page.evaluate(fetch)``.  ``page.evaluate(fetch)`` is blocked by CORS
    from an ``about:blank`` origin ("NetworkError when attempting to fetch
    resource"), while ``context.request`` performs the request through the real
    browser engine with the camoufox TLS/HTTP2 fingerprint and no CORS origin.

    Playwright's page/context is not thread-safe, so concurrent calls are
    serialised on a lock (callers should still force concurrency=1 in browser
    mode for throughput).
    """
    import threading

    _lock = threading.Lock()

    def _browser_fetch(
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        with _lock:
            try:
                request = page.context.request
                resp = request.get(
                    url,
                    headers=headers or {},
                    timeout=timeout_ms,
                )
                status = int(resp.status)
                resp_headers = {k: v for k, v in resp.headers.items()}
                try:
                    text = resp.text()
                except Exception:
                    text = ""
                return {"status": status, "text": text, "headers": resp_headers}
            except Exception:
                return {"status": 0, "text": "browser request failed", "headers": {}}

    return _browser_fetch


class BrowserFetchPool:
    """Run browser-backed credential checks concurrently on one async browser.

    The old implementation shared a *sync* Playwright page between worker
    threads and protected it with a lock, which made browser verification
    effectively serial.  This pool owns one AsyncCamoufox browser and its
    request context on a dedicated asyncio loop.  Every worker thread submits
    an async request to that loop, so up to ``concurrency`` requests can be in
    flight without crossing Playwright threads or serialising on a lock.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        timeout_ms: int = 30000,
        concurrency: int = 100,
    ):
        self._headless = bool(headless)
        self._proxy = proxy
        self._timeout_ms = max(int(timeout_ms or 30000), 1000)
        self._concurrency = max(int(concurrency or 1), 1)
        self._manager: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._login_semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._closed = False
        self.browser_fetch: BrowserFetchFn = self._browser_fetch

        self._thread = threading.Thread(
            target=self._run_loop,
            name="chatgpt-browser-verify",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=60):
            self.close()
            raise RuntimeError("浏览器验活进程启动超时")
        if self._init_error is not None:
            error = self._init_error
            self.close()
            raise RuntimeError(f"浏览器验活初始化失败: {error}") from error

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_init())
        except BaseException as exc:  # noqa: BLE001 - propagate to constructor
            self._init_error = exc
            self._ready.set()
            loop.close()
            self._loop = None
            return

        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._async_close())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _async_init(self) -> None:
        launch_opts: dict[str, Any] = {
            "headless": self._headless,
            "enable_cache": False,
        }
        if self._proxy:
            launch_opts["proxy"] = {"server": self._proxy}
        self._manager = AsyncCamoufox(**launch_opts)
        self._browser = await self._manager.__aenter__()
        # APIRequestContext is safe to drive concurrently from one asyncio
        # loop.  No page navigation is needed for /v1/me and this avoids the
        # sync Playwright page/thread-safety limitation entirely.
        self._context = await self._browser.new_context()
        # Login fallback is intentionally bounded much lower than AT checks:
        # each login owns a full page/context and performs several navigations.
        self._login_semaphore = asyncio.Semaphore(5)

    async def _login_async(
        self,
        email: str,
        password: str,
        totp_secret: str = "",
        proxy: str | None = None,
        log: Callable[[str], None] | None = None,
        otp_callback: Callable[[], str] | None = None,
        provider_accounts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        browser = self._browser
        semaphore = self._login_semaphore
        if browser is None or semaphore is None:
            return {"state": "invalid", "message": "browser login pool is not ready"}
        from platforms.chatgpt.browser_register_async import register_in_context
        from platforms.chatgpt.mfa import totp_code

        async with semaphore:
            try:
                # Task progress callbacks accept only the message, while the
                # browser flow also supplies structured keywords such as
                # ``level="warning"``.  Adapt the callback here so diagnostics
                # cannot mask the actual login result with a TypeError.
                def log_callback(message: str, **_kwargs: Any) -> None:
                    if log is not None:
                        log(message)

                resolved_otp_cb = otp_callback
                if resolved_otp_cb is None:
                    if totp_secret:
                        resolved_otp_cb = lambda: totp_code(totp_secret)
                    elif provider_accounts:
                        from platforms.chatgpt.credential_checks import _build_protocol_login_otp_callback

                        resolved_otp_cb = _build_protocol_login_otp_callback(
                            email,
                            provider_accounts,
                            proxy=proxy,
                        )
                if resolved_otp_cb is None:
                    resolved_otp_cb = lambda: ""

                result = await register_in_context(
                    browser,
                    email=email,
                    password=password,
                    proxy=proxy,
                    otp_callback=resolved_otp_cb,
                    log=log_callback,
                    bind_totp_2fa=False,
                    is_login=True,
                )
                return {
                    "state": "valid" if str(result.get("access_token") or "").strip() else "invalid",
                    "message": "browser login issued a fresh access token",
                    "tokens": {
                        key: value
                        for key, value in {
                            "access_token": result.get("access_token"),
                            "refresh_token": result.get("refresh_token"),
                            "id_token": result.get("id_token"),
                            "session_token": result.get("session_token"),
                            "cookies": result.get("cookies"),
                        }.items()
                        if value not in (None, "")
                    },
                }
            except Exception as exc:
                return {"state": "invalid", "message": str(exc)[:300], "tokens": {}}

    def browser_login(
        self,
        email: str,
        password: str,
        totp_secret: str = "",
        *,
        proxy: str | None = None,
        log: Callable[[str], None] | None = None,
        timeout_seconds: float = 120.0,
        otp_callback: Callable[[], str] | None = None,
        provider_accounts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Use the shared Camoufox browser for a password + TOTP or email OTP login.

        This is a bounded fallback for protocol logins that are blocked by an
        auth-edge Cloudflare challenge.  It deliberately reuses the browser
        process already opened for browser-first 401 checks.
        """
        loop = self._loop
        if self._closed or loop is None or not loop.is_running():
            return {"state": "invalid", "message": "browser login pool is closed", "tokens": {}}
        future = asyncio.run_coroutine_threadsafe(
            self._login_async(
                email,
                password,
                totp_secret,
                proxy=proxy,
                log=log,
                otp_callback=otp_callback,
                provider_accounts=provider_accounts,
            ),
            loop,
        )
        try:
            return future.result(timeout=max(float(timeout_seconds or 120.0), 10.0))
        except FutureTimeoutError:
            future.cancel()
            return {"state": "invalid", "message": "browser login timeout", "tokens": {}}
        except Exception as exc:
            return {"state": "invalid", "message": str(exc)[:300], "tokens": {}}

    async def _async_close(self) -> None:
        context, browser, manager = self._context, self._browser, self._manager
        self._context = None
        self._browser = None
        self._manager = None
        if context is not None:
            try:
                await _maybe_await(context.close())
            except Exception:
                pass
        if manager is not None:
            try:
                await _maybe_await(manager.__aexit__(None, None, None))
                return
            except Exception:
                pass
        if browser is not None:
            try:
                await _maybe_await(browser.close())
            except Exception:
                pass

    async def _fetch_async(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        context = self._context
        if context is None:
            return {"status": 0, "text": "browser request failed", "headers": {}}
        request = context.request
        method = str(method or "GET").upper()
        options: dict[str, Any] = {
            "headers": headers or {},
            "timeout": self._timeout_ms,
        }
        if body is not None:
            options["data"] = body
        try:
            response = await request.fetch(url, method=method, **options)
            status = int(getattr(response, "status", 0) or 0)
            raw_headers = await _maybe_await(getattr(response, "headers", {}) or {})
            response_headers = {str(key): str(value) for key, value in dict(raw_headers).items()}
            text = await _maybe_await(response.text())
            return {
                "status": status,
                "text": str(text or ""),
                "headers": response_headers,
            }
        except Exception as exc:
            return {
                "status": 0,
                "text": f"browser request failed: {str(exc)[:180]}",
                "headers": {},
            }

    def _browser_fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        loop = self._loop
        if self._closed or loop is None or not loop.is_running():
            return {"status": 0, "text": "browser request failed", "headers": {}}
        future = asyncio.run_coroutine_threadsafe(
            self._fetch_async(url, method=method, headers=headers, body=body),
            loop,
        )
        try:
            return future.result(timeout=(self._timeout_ms / 1000.0) + 5.0)
        except FutureTimeoutError:
            future.cancel()
            return {"status": 0, "text": "browser request timeout", "headers": {}}
        except Exception as exc:
            return {
                "status": 0,
                "text": f"browser request failed: {str(exc)[:180]}",
                "headers": {},
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=30)

    def __enter__(self) -> "BrowserFetchPool":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class BrowserFetchSession:
    """Open a camoufox page and provide a ``browser_fetch`` for verification.

    Usage::

        with BrowserFetchSession() as session:
            result = check_chatgpt_access_token(token, browser_fetch=session.browser_fetch)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        timeout_ms: int = 30000,
    ):
        self._headless = headless
        self._proxy = proxy
        self._timeout_ms = timeout_ms
        self._manager = None
        self._browser = None
        self._page = None
        self.browser_fetch: Optional[BrowserFetchFn] = None

    def __enter__(self) -> "BrowserFetchSession":
        from camoufox.sync_api import Camoufox

        launch_opts: dict[str, Any] = {"headless": self._headless}
        if self._proxy:
            launch_opts["proxy"] = {"server": self._proxy}
        self._manager = Camoufox(**launch_opts)
        try:
            self._browser = self._manager.__enter__()
            self._page = self._browser.new_page()
            # Do NOT navigate to an external origin: Google can challenge/redirect
            # headless browsers and leave the page mid-load.  about:blank is a
            # stable, same-origin-clean context for page.evaluate(fetch).
            try:
                self._page.goto("about:blank", wait_until="load", timeout=15000)
            except Exception:
                pass
            # Wait until the JS runtime is ready before issuing any fetch.
            try:
                self._page.evaluate("1 + 1")
            except Exception:
                pass
            self.browser_fetch = make_browser_fetch(self._page, timeout_ms=self._timeout_ms)
            return self
        except Exception:
            try:
                self._manager.__exit__(None, None, None)
            finally:
                self._manager = None
                self._browser = None
                self._page = None
                self.browser_fetch = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._manager is not None:
                self._manager.__exit__(exc_type, exc, tb)
        except Exception:
            try:
                if self._browser is not None:
                    self._browser.close()
            except Exception:
                pass
        self._manager = None
        self._browser = None
        self._page = None
        self.browser_fetch = None
