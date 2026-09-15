from __future__ import annotations

import time

import pytest

from core.base_platform import Account
from core.db import record_registered_email, save_account


def _create_account(*, email: str, extra: dict | None = None) -> int:
    model = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra=extra or {},
        )
    )
    return int(model.id or 0)


def test_account_list_is_server_paginated_and_redacts_credentials(client):
    for index in range(3):
        _create_account(
            email=f"page-{index}@example.com",
            extra={"access_token": f"access-{index}", "refresh_token": f"refresh-{index}"},
        )

    first = client.get("/api/accounts", params={"platform": "chatgpt", "page": 1, "page_size": 2})
    second = client.get("/api/accounts", params={"platform": "chatgpt", "page": 2, "page_size": 2})

    assert first.status_code == 200
    assert first.json()["total"] == 3
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 1
    row = first.json()["items"][0]
    assert set(row) == {
        "id",
        "platform",
        "email",
        "password",
        "totp_secret",
        "refresh_token_status",
        "has_refresh_token",
        "created_at",
        "at_expires_at",
        "has_mailbox",
        "mailbox_email",
        "plan_name",
    }
    assert row["has_refresh_token"] is True
    assert "password" in row  # mailbox password is shown by design
    assert "credentials" not in row


def test_dashboard_stats_endpoint_is_removed(client):
    response = client.get("/api/accounts/stats")

    assert response.status_code == 404


def test_survival_stats_keep_deleted_registered_emails_in_history(client):
    alive_id = _create_account(
        email="alive@example.com",
        extra={"account_overview": {"refresh_token_status": "valid"}},
    )
    dead_id = _create_account(
        email="dead@example.com",
        extra={"account_overview": {"refresh_token_status": "invalid"}},
    )
    for email in ("alive@example.com", "dead@example.com", "deleted@example.com"):
        record_registered_email("chatgpt", email)
    record_registered_email("chatgpt", "ALIVE@example.com")

    first = client.get("/api/accounts/survival-stats", params={"platform": "chatgpt"})
    assert first.status_code == 200
    assert first.json() == {
        "platform": "chatgpt",
        "alive_accounts": 1,
        "historical_registered_emails": 3,
        "survival_rate": 33.33,
    }

    assert client.delete(f"/api/accounts/{dead_id}").status_code == 200
    assert client.delete(f"/api/accounts/{alive_id}").status_code == 200
    second = client.get("/api/accounts/survival-stats", params={"platform": "chatgpt"})
    assert second.json()["alive_accounts"] == 0
    assert second.json()["historical_registered_emails"] == 3
    assert second.json()["survival_rate"] == 0.0


def test_survival_stats_backfill_successful_registration_events(client):
    from application.tasks import append_task_event, create_task
    from core.db import init_db

    task = create_task(task_type="register", platform="chatgpt", payload={})
    append_task_event(task["task_id"], "注册成功: past@example.com")
    init_db()
    response = client.get("/api/accounts/survival-stats", params={"platform": "chatgpt"})

    assert response.status_code == 200
    assert response.json()["historical_registered_emails"] == 1


def test_task_event_tail_returns_the_latest_events_in_order():
    from application.tasks import append_task_event, create_task
    from application.tasks_query import TasksQueryService

    task = create_task(task_type="test", platform="chatgpt", payload={})
    for index in range(5):
        append_task_event(task["task_id"], f"event-{index}")

    events = TasksQueryService().list_events(task["task_id"], tail=3)["items"]

    assert [event["message"] for event in events] == ["event-2", "event-3", "event-4"]


def test_maintenance_endpoints_create_limited_background_tasks(client):
    refresh = client.post(
        "/api/accounts/check-refresh-tokens",
        json={"platform": "chatgpt", "concurrency": 5, "browser": False},
    )

    assert refresh.status_code == 200
    assert refresh.json()["type"] == "refresh_token_check"
    assert refresh.json()["result"]["data"] is None
    assert client.post("/api/accounts/check-refresh-tokens", json={"concurrency": 201}).status_code == 422
    assert client.post("/api/accounts/verify-bans", json={"concurrency": 2}).status_code == 405
    for task_id in (refresh.json()["task_id"],):
        for _ in range(30):
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["terminal"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"task {task_id} did not finish")


def test_refresh_check_task_defaults_to_browser_first_mode():
    from application import tasks as tasks_module
    from sqlmodel import Session
    from core.db import TaskModel, engine

    task = tasks_module.create_refresh_token_check_task(concurrency=1)
    with Session(engine) as session:
        saved = session.get(TaskModel, task["task_id"])
        assert saved is not None
        assert saved.get_payload()["browser"] is True


def test_refresh_check_always_checks_access_token_even_when_rt_exists(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="refresh@example.com",
        extra={"access_token": "old-access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda token, **_kwargs: {
            "state": "valid",
            "message": f"{token} works",
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.refresh_chatgpt_tokens",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RT refresh must not run")),
    )

    result = _run_single_refresh_token_check(account_id)
    saved = AccountsService().get_account(account_id)

    assert result["state"] == "valid"
    assert result["login_required"] is False
    assert saved is not None
    assert saved["overview"]["refresh_token_status"] == "valid"
    assert saved["overview"]["refresh_token_check_method"] == "access_token"
    credentials = {item["key"]: item["value"] for item in saved["credentials"]}
    assert credentials["access_token"] == "old-access"
    assert credentials["refresh_token"] == "old-refresh"


def test_refresh_check_checks_access_token_without_rt(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="access-only@example.com",
        extra={"access_token": "access-only"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {"state": "valid", "message": "access token works"},
    )

    result = _run_single_refresh_token_check(account_id)
    saved = AccountsService().get_account(account_id)

    assert result["state"] == "valid"
    assert saved is not None
    assert saved["overview"]["refresh_token_status"] == "valid"
    assert saved["overview"]["refresh_token_check_method"] == "access_token"


def test_new_registration_access_token_check_is_persisted_as_valid(monkeypatch):
    from application.accounts import AccountsService
    from application.tasks import _check_newly_registered_chatgpt_account

    account = Account(
        platform="chatgpt",
        email="fresh@example.com",
        password="TestPass123!",
        extra={
            "access_token": "fresh-access",
            "account_id": "acct-123",
            "cookies": {"oai-did": "device-123", "session": "saved-cookie"},
        },
    )
    captured = {}
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda token, **kwargs: captured.update(kwargs) or {
            "state": "valid",
            "message": f"{token} works",
        },
    )

    result = _check_newly_registered_chatgpt_account(account)
    saved_model = save_account(account)
    saved = AccountsService().get_account(int(saved_model.id or 0))

    assert result["state"] == "valid"
    assert saved is not None
    assert saved["overview"]["refresh_token_status"] == "valid"
    assert saved["overview"]["refresh_token_check_method"] == "access_token"
    assert saved["overview"]["refresh_token_check_message"] == "fresh-access works"
    assert saved["overview"]["refresh_token_checked_at"]
    assert captured["account_id"] == "acct-123"


def test_access_token_check_treats_403_as_invalid(monkeypatch):
    from platforms.chatgpt import credential_checks

    calls = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response(403 if url.endswith("/me") else 200)

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)

    result = credential_checks.check_chatgpt_access_token("fresh-access")

    assert result == {
        "state": "invalid",
        "message": "access token 返回 HTTP 403（api.openai.com/v1/me）",
    }
    assert calls == ["https://api.openai.com/v1/me"]


def test_access_token_check_treats_cloudflare_403_as_inconclusive(monkeypatch):
    from platforms.chatgpt import credential_checks

    calls = []

    class Response:
        status_code = 403
        text = "<html><title>Just a moment...</title></html>"
        headers = {
            "content-type": "text/html; charset=UTF-8",
            "cf-mitigated": "challenge",
        }

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)
    monkeypatch.setattr(credential_checks.time, "sleep", lambda _seconds: None)

    result = credential_checks.check_chatgpt_access_token(
        "fresh-access",
        timeout_seconds=30,
    )

    assert result["state"] == "unknown"
    assert "Cloudflare/地区上游拦截" in result["message"]
    assert calls == [
        "https://api.openai.com/v1/me",
        "https://api.openai.com/v1/me",
        "https://api.openai.com/v1/me",
    ]


def test_access_token_check_uses_protocol_chrome_fingerprint(monkeypatch):
    from platforms.chatgpt import credential_checks
    from platforms.chatgpt.environment_profile import PROTOCOL_CHROME_IMPERSONATE

    calls = []

    class Response:
        status_code = 200

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)

    result = credential_checks.check_chatgpt_access_token(
        "fresh-access",
        proxy="http://mihomo:7890",
        account_id="acct-123",
    )

    assert result["state"] == "valid"
    assert [item[0] for item in calls] == [
        "https://api.openai.com/v1/me",
        "https://chatgpt.com/backend-api/me",
    ]
    assert all(
        kwargs["impersonate"] == PROTOCOL_CHROME_IMPERSONATE
        for _, kwargs in calls
    )
    assert calls[0][1]["proxies"] is None
    assert calls[1][1]["proxies"] == {
        "http": "http://mihomo:7890",
        "https": "http://mihomo:7890",
    }
    assert calls[0][1]["headers"]["ChatGPT-Account-ID"] == "acct-123"


def test_access_token_check_keeps_api_alive_when_workspace_check_is_blocked(monkeypatch):
    from platforms.chatgpt import credential_checks

    calls = []

    class Response:
        def __init__(self, status_code, *, html=False):
            self.status_code = status_code
            self.text = "<html>Just a moment...</html>" if html else "{}"
            self.headers = (
                {"content-type": "text/html", "cf-mitigated": "challenge"}
                if html
                else {"content-type": "application/json"}
            )

        @staticmethod
        def json():
            return {}

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response(200) if "api.openai.com" in url else Response(403, html=True)

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)

    result = credential_checks.check_chatgpt_access_token("fresh-access")

    assert result["state"] == "valid"
    assert "工作区检查未确认" in result["message"]
    assert calls == [
        "https://api.openai.com/v1/me",
        "https://chatgpt.com/backend-api/me",
    ]


def test_access_token_check_rejects_deactivated_workspace(monkeypatch):
    from platforms.chatgpt import credential_checks

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = ""
            self.headers = {"content-type": "application/json"}

        def json(self):
            return self._payload

    monkeypatch.setattr(
        credential_checks.requests,
        "get",
        lambda url, **_kwargs: (
            Response(200, {})
            if "api.openai.com" in url
            else Response(402, {"detail": {"code": "deactivated_workspace"}})
        ),
    )

    result = credential_checks.check_chatgpt_access_token("fresh-access")

    assert result == {
        "state": "invalid",
        "message": (
            "工作区返回 HTTP 402（chatgpt.com/backend-api/me，"
            "deactivated_workspace）"
        ),
    }


def test_access_token_check_rejects_locally_expired_jwt(monkeypatch):
    import base64
    import json

    from platforms.chatgpt import credential_checks

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = f"{encode({'alg': 'none'})}.{encode({'exp': 1})}.signature"
    monkeypatch.setattr(
        credential_checks.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired JWT must not make a network request")
        ),
    )

    assert credential_checks.check_chatgpt_access_token(token) == {
        "state": "invalid",
        "message": "access token JWT exp 已过期",
    }


def test_refresh_token_check_treats_http_403_as_invalid(monkeypatch):
    from platforms.chatgpt import credential_checks

    class Response:
        status_code = 403
        text = "forbidden"

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(
        credential_checks.requests,
        "post",
        lambda *_args, **_kwargs: Response(),
    )

    result = credential_checks.refresh_chatgpt_tokens("stale-refresh")

    assert result == {"state": "invalid", "message": "RT 已失效", "tokens": {}}


def test_access_token_check_stops_immediately_on_401(monkeypatch):
    from platforms.chatgpt import credential_checks

    calls = []

    class Response:
        status_code = 401

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)

    result = credential_checks.check_chatgpt_access_token("dead-access")

    assert result == {
        "state": "invalid",
        "message": "access token 返回 HTTP 401（api.openai.com/v1/me）",
    }
    assert calls == ["https://api.openai.com/v1/me"]


def test_refresh_check_recovers_invalid_access_token_with_protocol_login(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="recover@example.com",
        extra={"access_token": "old-access", "refresh_token": "old-refresh"},
    )
    check_results = iter(
        [
            {
                "state": "invalid",
                "message": "access token 返回 HTTP 401（me）",
            },
            {"state": "valid", "message": "new access token works"},
        ]
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: next(check_results),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        lambda *_args, **_kwargs: {
            "state": "valid",
            "message": "new access token issued",
            "tokens": {"access_token": "new-access", "refresh_token": "new-refresh"},
        },
    )

    result = _run_single_refresh_token_check(account_id)
    saved = AccountsService().get_account(account_id)

    assert result["state"] == "valid"
    assert result["login_required"] is True
    assert result["login_attempted"] is True
    assert result["login_succeeded"] is True
    assert result["recovery_state"] == "valid"
    assert saved is not None
    assert saved["overview"]["refresh_token_status"] == "valid"
    assert saved["overview"]["refresh_token_check_method"] == "protocol_login_verified"
    credentials = {item["key"]: item["value"] for item in saved["credentials"]}
    assert credentials["access_token"] == "new-access"
    assert credentials["refresh_token"] == "new-refresh"


def test_refresh_check_uses_account_id_for_api_check_and_proxy_for_login(monkeypatch):
    from application.tasks import _run_single_refresh_token_check

    account_id = _create_account(
        email="proxy-login@example.com",
        extra={
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "account_id": "acct-123",
            "cookies": {"oai-did": "device-123", "session": "saved-cookie"},
        },
    )
    calls: dict[str, object] = {}

    def check_access_token(*_args, **kwargs):
        calls["check_proxy"] = kwargs.get("proxy")
        calls["check_account_id"] = kwargs.get("account_id")
        return {"state": "invalid", "message": "access token 返回 HTTP 401（me）"}

    def login_with_protocol(*_args, **kwargs):
        calls["login_proxy"] = kwargs.get("proxy")
        return {"state": "invalid", "message": "temporary login failure", "tokens": {}}

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        check_access_token,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        login_with_protocol,
    )

    result = _run_single_refresh_token_check(
        account_id,
        login_proxy="http://127.0.0.1:17890",
        check_proxy="http://127.0.0.1:17890",
    )

    assert result["login_attempted"] is True
    assert calls["check_proxy"] == "http://127.0.0.1:17890"
    assert calls["check_account_id"] == "acct-123"
    assert calls["login_proxy"] == "http://127.0.0.1:17890"


def test_refresh_check_passes_saved_totp_secret_to_protocol_login(monkeypatch):
    from application.tasks import _run_single_refresh_token_check

    account_id = _create_account(
        email="totp-recovery@example.com",
        extra={"access_token": "old-access", "totp_secret": "SAVEDSECRET"},
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda token, **_kwargs: (
            {"state": "invalid", "message": "HTTP 401"}
            if token == "old-access"
            else {"state": "valid", "message": "new token works"}
        ),
    )

    def login(*_args, **kwargs):
        calls.update(kwargs)
        return {"state": "valid", "message": "recovered", "tokens": {"access_token": "new-access"}}

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        login,
    )

    result = _run_single_refresh_token_check(account_id)

    assert result["login_succeeded"] is True
    assert calls["totp_secret"] == "SAVEDSECRET"


def test_refresh_check_deletes_after_relogin_reports_ban(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="banned@example.com",
        extra={"access_token": "access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401（me）",
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        lambda *_args, **_kwargs: {
            "state": "banned",
            "message": "account_deactivated",
            "confirmed_ban_code": "account_deactivated",
            "tokens": {},
        },
    )

    result = _run_single_refresh_token_check(account_id)

    assert result["state"] == "deleted"
    assert AccountsService().get_account(account_id) is None


def test_refresh_check_keeps_unconfirmed_ban_result(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="unconfirmed-ban@example.com",
        extra={"access_token": "access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401（me）",
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        lambda *_args, **_kwargs: {
            "state": "banned",
            "message": "generic login failure",
            "tokens": {},
        },
    )

    result = _run_single_refresh_token_check(account_id)

    assert result["state"] == "invalid"
    assert result["recovery_state"] == "unconfirmed_ban"
    assert AccountsService().get_account(account_id) is not None


def test_refresh_check_deletes_when_relogin_has_no_reusable_mailbox(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="missing-mailbox@example.com",
        extra={"access_token": "access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401（me）",
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        lambda *_args, **_kwargs: {
            "state": "missing_mailbox",
            "message": "账号缺少可复用的验证邮箱，无法协议登录",
            "tokens": {},
        },
    )

    result = _run_single_refresh_token_check(account_id)

    assert result["state"] == "deleted"
    assert AccountsService().get_account(account_id) is None


def test_refresh_check_keeps_account_when_relogin_cannot_issue_credentials(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="unknown@example.com",
        extra={"access_token": "access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401（me）",
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.login_chatgpt_with_protocol",
        lambda *_args, **_kwargs: {"state": "unknown", "message": "session expired", "tokens": {}},
    )
    result = _run_single_refresh_token_check(account_id)
    saved = AccountsService().get_account(account_id)

    assert result["state"] == "invalid"
    assert saved is not None
    assert saved["overview"]["relogin_status"] == "failed"
    assert "session expired" in saved["overview"]["refresh_token_check_message"]


def test_refresh_check_cancel_does_not_delete_invalid_account(monkeypatch):
    from application.tasks import _run_single_refresh_token_check
    from application.accounts import AccountsService

    account_id = _create_account(
        email="cancelled@example.com",
        extra={"access_token": "access", "refresh_token": "old-refresh"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401（me）",
        },
    )

    result = _run_single_refresh_token_check(
        account_id,
        cancel_check=lambda: True,
    )

    assert result["state"] == "unknown"
    assert AccountsService().get_account(account_id) is not None


def test_refresh_login_route_falls_back_to_mihomo_when_direct_is_blocked(monkeypatch):
    import application.tasks as tasks_module

    calls = []
    logs = []

    def probe(proxy):
        calls.append(proxy)
        return (False, "HTTP 403") if proxy is None else (True, "HTTP 200")

    monkeypatch.setattr(tasks_module, "_probe_chatgpt_login_route", probe)
    monkeypatch.setenv("MIHOMO_PROXY_URL", "http://mihomo:7890")
    logger = type(
        "LoggerStub",
        (),
        {"log": lambda _self, message, **_kwargs: logs.append(message)},
    )()

    proxy = tasks_module._resolve_refresh_login_proxy("", logger=logger)

    assert proxy == "http://mihomo:7890"
    assert calls == ["http://mihomo:7890"]
    assert any("Mihomo 节点预检通过" in message for message in logs)


def test_refresh_login_route_accepts_http_403_as_transport_reachable(monkeypatch):
    import application.tasks as tasks_module
    from curl_cffi import requests as curl_requests

    class Response:
        status_code = 403

    monkeypatch.delenv("MIHOMO_PROXY_URL", raising=False)
    monkeypatch.setattr(curl_requests, "get", lambda *_args, **_kwargs: Response())
    logs = []
    logger = type(
        "LoggerStub",
        (),
        {"log": lambda _self, message, **_kwargs: logs.append(message)},
    )()

    proxy = tasks_module._resolve_refresh_login_proxy("", logger=logger)

    assert proxy is None
    assert any("直连预检通过（HTTP 403）" in message for message in logs)


def test_refresh_background_task_continues_after_homepage_http_403(client, monkeypatch):
    from curl_cffi import requests as curl_requests

    _create_account(
        email="homepage-403@example.com",
        extra={"access_token": "still-valid"},
    )

    class Response:
        status_code = 403

    monkeypatch.delenv("MIHOMO_PROXY_URL", raising=False)
    monkeypatch.setattr(curl_requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {"state": "valid", "message": "AT 正常"},
    )

    created = client.post(
        "/api/accounts/check-refresh-tokens",
        json={"platform": "chatgpt", "concurrency": 1, "browser": False},
    )
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    for _ in range(30):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["terminal"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"task {task_id} did not finish")

    assert task["status"] == "succeeded"
    assert task["progress"] == "1/1"
    assert task["data"]["valid"] == 1
    assert task["data"].get("route_error") is None


def test_refresh_login_route_fails_before_accounts_when_all_routes_are_blocked(monkeypatch):
    import application.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "_probe_chatgpt_login_route",
        lambda proxy: (
            False,
            "ConnectionError: direct route unavailable"
            if proxy is None
            else "ConnectionError: proxy route unavailable",
        ),
    )
    monkeypatch.setenv("MIHOMO_PROXY_URL", "http://mihomo:7890")
    logger = type("LoggerStub", (), {"log": lambda *_args, **_kwargs: None})()

    with pytest.raises(RuntimeError, match="Mihomo 不可用"):
        tasks_module._resolve_refresh_login_proxy("", logger=logger)


def test_refresh_task_fails_when_every_recovery_login_fails(monkeypatch):
    import application.tasks as tasks_module

    class LoggerStub:
        def __init__(self):
            self.finished = ""
            self.error = ""

        def log(self, _message, **_kwargs):
            pass

        def set_progress(self, _current, _total=None):
            pass

        def set_counts(self, **_kwargs):
            pass

        def set_result_data(self, _data):
            pass

        def is_cancel_requested(self):
            return False

        def finish(self, status, *, error=""):
            self.finished = status
            self.error = error

    monkeypatch.setattr(tasks_module, "_account_ids_for_platform", lambda _platform: [1, 2])
    monkeypatch.setattr(
        tasks_module,
        "_resolve_refresh_login_proxy",
        lambda _node, *, logger: "http://mihomo:7890",
    )
    monkeypatch.setattr(
        tasks_module,
        "_run_single_refresh_token_check",
        lambda account_id, **_kwargs: {
            "account_id": account_id,
            "state": "invalid",
            "login_required": True,
            "login_attempted": True,
            "login_succeeded": False,
            "recovery_state": "invalid",
        },
    )
    logger = LoggerStub()

    tasks_module._execute_refresh_token_check_task(
        {"platform": "chatgpt", "concurrency": 2},
        logger,
    )

    assert logger.finished == tasks_module.TASK_STATUS_FAILED
    assert logger.error == "需要恢复登录的 2 个账号全部登录失败"


def test_refresh_task_logs_heartbeat_while_tail_is_waiting(monkeypatch):
    import application.tasks as tasks_module

    class LoggerStub:
        def __init__(self):
            self.messages: list[str] = []
            self.finished = ""
            self.result_data: dict = {}

        def log(self, message, **_kwargs):
            self.messages.append(message)

        def set_progress(self, _current, _total=None):
            pass

        def set_counts(self, **_kwargs):
            pass

        def set_result_data(self, data):
            self.result_data = data

        def is_cancel_requested(self):
            return False

        def finish(self, status, **_kwargs):
            self.finished = status

    monkeypatch.setattr(tasks_module, "REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setenv("CHATGPT_PROTOCOL_LOGIN_CONCURRENCY", "50")
    monkeypatch.setattr(tasks_module, "_account_ids_for_platform", lambda _platform: [1, 2])
    monkeypatch.setattr(
        tasks_module,
        "_resolve_refresh_login_proxy",
        lambda _node, *, logger: None,
    )

    def slow_check(account_id, **_kwargs):
        time.sleep(0.04)
        return {"account_id": account_id, "state": "valid", "message": "ok"}

    monkeypatch.setattr(tasks_module, "_run_single_refresh_token_check", slow_check)
    logger = LoggerStub()

    tasks_module._execute_refresh_token_check_task(
        {"platform": "chatgpt", "concurrency": 2},
        logger,
    )

    assert any("处理中 2" in message for message in logger.messages)
    assert any("401 验活 2/2" in message for message in logger.messages)
    assert any(
        "AT 检查并发 2，协议登录并发 2" in message
        for message in logger.messages
    )
    assert logger.result_data["login_concurrency"] == 2
    assert logger.finished == tasks_module.TASK_STATUS_SUCCEEDED


def test_browser_refresh_phase_uses_parallel_at_checks_then_protocol_relogin(monkeypatch):
    import threading
    import application.tasks as tasks_module

    class BrowserPoolStub:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.browser_fetch = lambda *_args, **_kwargs: {
                "status": 200,
                "text": "{}",
                "headers": {},
            }
            self.__class__.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    monkeypatch.setattr(
        "platforms.chatgpt.browser_verify.BrowserFetchPool",
        BrowserPoolStub,
    )
    monkeypatch.setattr(tasks_module, "_account_ids_for_platform", lambda _platform: list(range(1, 9)))
    monkeypatch.setattr(
        tasks_module,
        "_resolve_refresh_login_proxy",
        lambda _node, *, logger: None,
    )

    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_check(account_id, **kwargs):
        nonlocal active, max_active
        if kwargs.get("force_recovery"):
            return {
                "account_id": account_id,
                "state": "valid",
                "login_attempted": True,
                "login_succeeded": True,
                "recovery_state": "valid",
            }
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with active_lock:
            active -= 1
        if account_id % 2:
            return {
                "account_id": account_id,
                "state": "invalid",
                "login_required": True,
            }
        return {"account_id": account_id, "state": "valid"}

    monkeypatch.setattr(tasks_module, "_run_single_refresh_token_check", fake_check)

    class LoggerStub:
        def __init__(self):
            self.messages = []
            self.result_data = {}
            self.finished = ""

        def log(self, message, **_kwargs):
            self.messages.append(message)

        def set_progress(self, *_args, **_kwargs):
            pass

        def set_counts(self, **_kwargs):
            pass

        def set_result_data(self, data):
            self.result_data = data

        def is_cancel_requested(self):
            return False

        def finish(self, status, **_kwargs):
            self.finished = status

    logger = LoggerStub()
    tasks_module._execute_refresh_token_check_task(
        {"platform": "chatgpt", "concurrency": 4, "browser": True},
        logger,
    )

    assert BrowserPoolStub.instances[0].kwargs["concurrency"] == 4
    assert BrowserPoolStub.instances[0].closed is True
    assert max_active >= 2
    assert logger.result_data["browser"] is True
    assert logger.result_data["login_attempted"] == 4
    assert logger.result_data["login_succeeded"] == 4
    assert logger.finished == tasks_module.TASK_STATUS_SUCCEEDED


def test_refresh_task_finishes_cancel_only_after_inflight_worker_stops(monkeypatch):
    import threading

    import application.tasks as tasks_module

    started = threading.Event()
    stopped = threading.Event()

    class LoggerStub:
        def __init__(self):
            self.cancel_requested = False
            self.finished = ""

        def log(self, _message, **_kwargs):
            pass

        def set_progress(self, _current, _total=None):
            pass

        def set_counts(self, **_kwargs):
            pass

        def set_result_data(self, _data):
            pass

        def is_cancel_requested(self):
            return self.cancel_requested

        def finish(self, status, **_kwargs):
            assert stopped.is_set()
            self.finished = status

    def blocking_check(_account_id, *, cancel_check, **_kwargs):
        started.set()
        while not cancel_check():
            time.sleep(0.005)
        stopped.set()
        return {"state": "unknown", "recovery_state": "cancelled"}

    monkeypatch.setattr(tasks_module, "_account_ids_for_platform", lambda _platform: [1])
    monkeypatch.setattr(
        tasks_module,
        "_resolve_refresh_login_proxy",
        lambda _node, *, logger: None,
    )
    monkeypatch.setattr(tasks_module, "_run_single_refresh_token_check", blocking_check)
    logger = LoggerStub()
    worker = threading.Thread(
        target=tasks_module._execute_refresh_token_check_task,
        args=({"platform": "chatgpt", "concurrency": 1}, logger),
    )
    worker.start()
    assert started.wait(timeout=1)
    logger.cancel_requested = True
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert logger.finished == tasks_module.TASK_STATUS_CANCELLED


def test_execute_task_records_unhandled_worker_failure(monkeypatch):
    from application import tasks as tasks_module

    task = tasks_module.create_refresh_token_check_task(concurrency=1)
    monkeypatch.setattr(
        tasks_module,
        "_execute_refresh_token_check_task",
        lambda _payload, _logger: (_ for _ in ()).throw(RuntimeError("worker exploded")),
    )

    tasks_module.execute_task(task["task_id"])
    saved = tasks_module.get_task(task["task_id"])

    assert saved is not None
    assert saved["status"] == tasks_module.TASK_STATUS_FAILED
    assert saved["error"] == "worker exploded"


def test_cancelled_task_remains_cancelled_for_inflight_workers():
    from application import tasks as tasks_module

    task = tasks_module.create_refresh_token_check_task(concurrency=1)
    logger = tasks_module.TaskLogger(task["task_id"])
    logger.mark_running()
    tasks_module.request_cancel(task["task_id"])

    assert logger.is_cancel_requested() is True
    logger.finish(tasks_module.TASK_STATUS_CANCELLED, error="任务已取消")
    assert logger.is_cancel_requested() is True


def test_web_session_relogin_reports_only_explicit_ban_markers_as_banned():
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        def set(self, *_args, **_kwargs):
            pass

    class Response:
        status_code = 200
        headers: dict[str, str] = {}
        text = '{"error":"account_deactivated"}'

        @staticmethod
        def json():
            return {"error": "account_deactivated"}

    class SessionStub:
        cookies = Cookies()

        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    result = mint_chatgpt_refresh_token_from_session(
        "session=active",
        session=SessionStub(),
    )

    assert result["state"] == "banned"


def test_protocol_login_returns_fresh_access_token_without_web_session(monkeypatch):
    from platforms.chatgpt.credential_checks import login_chatgpt_with_protocol

    calls: dict[str, object] = {}

    def registration_login(email, password, **kwargs):
        calls.update({"email": email, "password": password, **kwargs})
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "session_token": "new-session",
        }

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._login_with_registration_protocol",
        registration_login,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )

    result = login_chatgpt_with_protocol(
        "user@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
        proxy="http://127.0.0.1:8080",
    )

    assert result["state"] == "valid"
    assert result["tokens"]["access_token"] == "new-access"
    assert result["tokens"]["refresh_token"] == "new-refresh"
    assert calls["email"] == "user@example.com"
    assert calls["password"] == "password"
    assert calls["proxy"] == "http://127.0.0.1:8080"
    assert callable(calls["otp_callback"])
    assert calls["log_callback"] is None


def test_protocol_login_concurrency_is_independent_from_sentinel_workers(monkeypatch):
    from platforms.chatgpt.credential_checks import (
        protocol_login_concurrency_limit,
    )

    monkeypatch.delenv("CHATGPT_PROTOCOL_LOGIN_CONCURRENCY", raising=False)
    monkeypatch.setenv("CHATGPT_SENTINEL_VM_WORKERS", "3")
    assert protocol_login_concurrency_limit() == 50

    monkeypatch.setenv("CHATGPT_PROTOCOL_LOGIN_CONCURRENCY", "12")
    assert protocol_login_concurrency_limit() == 12

    monkeypatch.setenv("CHATGPT_PROTOCOL_LOGIN_CONCURRENCY", "80")
    assert protocol_login_concurrency_limit() == 50


def test_protocol_login_queue_wait_does_not_consume_login_deadline(monkeypatch):
    from platforms.chatgpt import credential_checks

    class DelayedSemaphore:
        released = False

        def acquire(self, **_kwargs):
            time.sleep(0.15)
            return True

        def release(self):
            self.released = True

    semaphore = DelayedSemaphore()
    observed: dict[str, float] = {}

    def registration_login(*_args, **kwargs):
        observed["remaining"] = kwargs["deadline"] - time.monotonic()
        return {"access_token": "fresh-after-queue"}

    monkeypatch.setattr(
        credential_checks,
        "_PROTOCOL_LOGIN_SEMAPHORE",
        semaphore,
    )
    monkeypatch.setattr(
        credential_checks,
        "_build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )
    monkeypatch.setattr(
        credential_checks,
        "_login_with_registration_protocol",
        registration_login,
    )

    result = credential_checks.login_chatgpt_with_protocol(
        "queued@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
        timeout_seconds=0.1,
    )

    assert result["state"] == "valid"
    assert observed["remaining"] > 0
    assert semaphore.released is True


def test_protocol_login_reuses_registration_protocol_worker(monkeypatch):
    from platforms.chatgpt import credential_checks
    from platforms.chatgpt import protocol_register

    calls: dict[str, object] = {}
    profile = object()

    class Worker:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def login(self, *, email, password):
            calls["login"] = {"email": email, "password": password}
            return {
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "session_token": "fresh-session",
            }

    monkeypatch.setattr(protocol_register, "ChatGPTProtocolRegister", Worker)
    monkeypatch.setattr(
        credential_checks,
        "_next_protocol_login_profile",
        lambda: profile,
    )
    monkeypatch.setattr(
        credential_checks,
        "_build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )

    result = credential_checks.login_chatgpt_with_protocol(
        "user@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
        proxy="http://127.0.0.1:8080",
        timeout_seconds=30,
    )

    assert result["state"] == "valid"
    assert result["tokens"]["access_token"] == "fresh-access"
    assert calls["login"] == {
        "email": "user@example.com",
        "password": "password",
    }
    init = calls["init"]
    assert init["proxy"] == "http://127.0.0.1:8080"
    assert callable(init["otp_callback"])
    assert callable(init["cancel_check"])
    assert init["profile"] is profile
    assert 0 < init["request_timeout"] <= 30


def test_ban_detection_ignores_generic_html_text():
    from platforms.chatgpt.credential_checks import _has_explicit_ban_marker

    class Response:
        text = "<html>account deactivated is not an API error code</html>"

        @staticmethod
        def json():
            raise ValueError("not json")

    assert _has_explicit_ban_marker(Response(), {}) is False
    assert _has_explicit_ban_marker(Response(), {"error": "account_deactivated"}) is True


def test_registration_protocol_preserves_structured_otp_ban():
    from platforms.chatgpt.credential_checks import ChatGPTAccountBannedDuringRelogin
    from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

    class Response:
        status_code = 400
        text = '{"error":{"code":"account_deactivated"}}'

        @staticmethod
        def json():
            return {"error": {"code": "account_deactivated"}}

    class Session:
        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

    worker = ChatGPTProtocolRegister(session=Session())

    with pytest.raises(ChatGPTAccountBannedDuringRelogin) as raised:
        worker._validate_otp("123456")

    assert raised.value.code == "account_deactivated"


def test_protocol_login_reports_only_explicit_login_ban_as_banned(monkeypatch):
    from platforms.chatgpt.credential_checks import (
        ChatGPTAccountBannedDuringRelogin,
        login_chatgpt_with_protocol,
    )

    def banned(*_args, **_kwargs):
        raise ChatGPTAccountBannedDuringRelogin(
            "OTP 校验明确返回 account_deactivated",
            code="account_deactivated",
        )

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._login_with_registration_protocol",
        banned,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )

    result = login_chatgpt_with_protocol(
        "banned@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
    )

    assert result["state"] == "banned"
    assert result["confirmed_ban_code"] == "account_deactivated"


def test_protocol_login_keeps_ordinary_login_failure_as_invalid(monkeypatch):
    from platforms.chatgpt.credential_checks import login_chatgpt_with_protocol

    def failed(*_args, **_kwargs):
        raise RuntimeError("incorrect password")

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._login_with_registration_protocol",
        failed,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )

    result = login_chatgpt_with_protocol(
        "retry@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
    )

    assert result["state"] == "invalid"


def test_protocol_login_reports_missing_mailbox_when_email_is_empty():
    from platforms.chatgpt.credential_checks import login_chatgpt_with_protocol

    result = login_chatgpt_with_protocol(
        "  ",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "domain_inbucket"}],
    )

    assert result["state"] == "missing_mailbox"
    assert result["tokens"] == {}


def test_protocol_login_uses_local_inbucket_override(monkeypatch):
    from platforms.chatgpt.credential_checks import _build_protocol_login_otp_callback

    captured = {}

    class Mailbox:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @staticmethod
        def get_current_ids(_account):
            return set()

    # The implementation prefers the general INBUCKET override first, then the
    # login-specific one, then the provider credentials.  Pin both so the test
    # is independent of the host .env.
    monkeypatch.setenv(
        "CHATGPT_INBUCKET_API_URL",
        "",
    )
    monkeypatch.setenv(
        "CHATGPT_LOGIN_INBUCKET_API_URL",
        "http://127.0.0.1:19000/api/v1",
    )
    monkeypatch.setattr(
        "core.inbucket_domain_mailbox.InbucketDomainMailbox",
        Mailbox,
    )

    callback = _build_protocol_login_otp_callback(
        "user@example.com",
        [
            {
                "provider_type": "mailbox",
                "provider_name": "domain_inbucket",
                "login_identifier": "user@example.com",
                "credentials": {
                    "domain": "example.com",
                    "inbucket_api_url": "http://inbucket:9000/api/v1",
                },
            }
        ],
    )

    assert callable(callback)
    # General override is empty -> login-specific override is used.
    assert captured["api_url"] == "http://127.0.0.1:19000/api/v1"


def test_protocol_login_prefers_general_inbucket_override(monkeypatch):
    from platforms.chatgpt.credential_checks import _build_protocol_login_otp_callback

    captured = {}

    class Mailbox:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @staticmethod
        def get_current_ids(_account):
            return set()

    monkeypatch.setenv(
        "CHATGPT_INBUCKET_API_URL",
        "http://127.0.0.1:19000/api/v1",
    )
    monkeypatch.setenv(
        "CHATGPT_LOGIN_INBUCKET_API_URL",
        "http://127.0.0.1:29000/api/v1",
    )
    monkeypatch.setattr(
        "core.inbucket_domain_mailbox.InbucketDomainMailbox",
        Mailbox,
    )

    callback = _build_protocol_login_otp_callback(
        "user@example.com",
        [
            {
                "provider_type": "mailbox",
                "provider_name": "domain_inbucket",
                "login_identifier": "user@example.com",
                "credentials": {
                    "domain": "example.com",
                    "inbucket_api_url": "http://inbucket:9000/api/v1",
                },
            }
        ],
    )

    assert callable(callback)
    assert captured["api_url"] == "http://127.0.0.1:19000/api/v1"


def test_protocol_login_enforces_total_deadline(monkeypatch):
    from platforms.chatgpt.credential_checks import login_chatgpt_with_protocol

    def slow_login(*_args, **_kwargs):
        time.sleep(0.03)
        return {"access_token": "late-access"}

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._login_with_registration_protocol",
        slow_login,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks._build_protocol_login_otp_callback",
        lambda *_args, **_kwargs: (lambda: "123456"),
    )

    result = login_chatgpt_with_protocol(
        "late@example.com",
        "password",
        provider_accounts=[{"provider_type": "mailbox", "provider_name": "api_mailbox"}],
        timeout_seconds=0.01,
    )

    assert result["state"] == "invalid"
    assert "总时限" in result["message"]
