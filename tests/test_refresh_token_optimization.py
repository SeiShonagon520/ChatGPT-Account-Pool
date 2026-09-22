from __future__ import annotations

import pytest
from platforms.chatgpt import credential_checks


def test_is_invalid_refresh_response_detects_status_401():
    # Standard OpenAI 401 response when session is ended
    payload = {
        "error": {
            "message": "Your session has ended. Please log in again.",
            "type": "invalid_request_error",
            "code": "refresh_token_invalidated",
        }
    }
    assert credential_checks._is_invalid_refresh_response(401, payload, "Your session has ended") is True


def test_is_invalid_refresh_response_detects_status_403():
    assert credential_checks._is_invalid_refresh_response(403, {}, "forbidden") is True


def test_is_invalid_refresh_response_detects_status_400_with_session_ended():
    payload = {
        "error": {
            "message": "Your session has ended. Please log in again.",
            "type": "invalid_request_error",
            "code": "refresh_token_invalidated",
        }
    }
    assert credential_checks._is_invalid_refresh_response(400, payload, "") is True


def test_is_invalid_refresh_response_detects_status_400_with_invalid_grant():
    payload = {
        "error": "invalid_grant",
        "error_description": "refresh token is expired",
    }
    assert credential_checks._is_invalid_refresh_response(400, payload, "") is True


def test_is_invalid_refresh_response_ignores_200_and_500():
    assert credential_checks._is_invalid_refresh_response(200, {"access_token": "abc"}, "") is False
    assert credential_checks._is_invalid_refresh_response(503, {}, "service unavailable") is False


def test_refresh_chatgpt_tokens_returns_invalid_on_401(monkeypatch):
    class Response:
        status_code = 401
        text = '{"error":{"message":"Your session has ended. Please log in again.","code":"refresh_token_invalidated"}}'

        @staticmethod
        def json():
            return {
                "error": {
                    "message": "Your session has ended. Please log in again.",
                    "code": "refresh_token_invalidated",
                }
            }

    monkeypatch.setattr(credential_checks.requests, "post", lambda *_args, **_kwargs: Response())

    result = credential_checks.refresh_chatgpt_tokens("stale-refresh")
    assert result["state"] == "invalid"
    assert "RT 已失效" in result["message"]


def test_check_chatgpt_access_token_verify_codex_detects_401(monkeypatch):
    class Response:
        def __init__(self, status_code, body=""):
            self.status_code = status_code
            self.text = body
            self.headers = {"content-type": "application/json"}

        def json(self):
            import json
            try:
                return json.loads(self.text)
            except Exception:
                return {}

    def fake_get(url, **_kwargs):
        if "api.openai.com/v1/me" in url:
            return Response(200, '{"id": "user-123"}')
        if "chatgpt.com/backend-api/me" in url:
            return Response(200, '{"object": "user"}')
        return Response(404)

    def fake_post(url, **_kwargs):
        if "codex/responses" in url:
            return Response(401, '{"detail": "Unauthorized"}')
        return Response(404)

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)
    monkeypatch.setattr(credential_checks.requests, "post", fake_post)

    # Without verify_codex, it returns valid because /v1/me and backend-api/me are 200
    res_without = credential_checks.check_chatgpt_access_token("token-abc", verify_codex=False)
    assert res_without["state"] == "valid"

    # With verify_codex, it catches the 401 Unauthorized from codex/responses!
    res_with = credential_checks.check_chatgpt_access_token("token-abc", verify_codex=True)
    assert res_with["state"] == "invalid"
    assert "401" in res_with["message"]


def test_check_chatgpt_access_token_verify_codex_accepts_400_as_authorized(monkeypatch):
    class Response:
        def __init__(self, status_code, body=""):
            self.status_code = status_code
            self.text = body
            self.headers = {"content-type": "application/json"}

        def json(self):
            import json
            try:
                return json.loads(self.text)
            except Exception:
                return {}

    def fake_get(url, **_kwargs):
        if "api.openai.com/v1/me" in url:
            return Response(200, '{"id": "user-123"}')
        if "chatgpt.com/backend-api/me" in url:
            return Response(200, '{"object": "user"}')
        return Response(404)

    def fake_post(url, **_kwargs):
        if "codex/responses" in url:
            # 400 with model unconfigured proves authentication succeeded!
            return Response(400, '{"detail": "The model is not supported"}')
        return Response(404)

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)
    monkeypatch.setattr(credential_checks.requests, "post", fake_post)

    res = credential_checks.check_chatgpt_access_token("token-abc", verify_codex=True)
    assert res["state"] == "valid"
    assert res["web_valid"] is True
    assert res["codex_valid"] is True
    assert res["web_status"] == "valid"
    assert res["codex_status"] == "valid"


def test_check_chatgpt_access_token_dual_status_on_codex_failure(monkeypatch):
    class Response:
        def __init__(self, status_code, body=""):
            self.status_code = status_code
            self.text = body
            self.headers = {"content-type": "application/json"}

        def json(self):
            import json
            try:
                return json.loads(self.text)
            except Exception:
                return {}

    def fake_get(url, **_kwargs):
        if "api.openai.com/v1/me" in url or "chatgpt.com/backend-api/me" in url:
            return Response(200, '{"id": "user-123"}')
        return Response(404)

    def fake_post(url, **_kwargs):
        if "codex/responses" in url:
            return Response(401, '{"detail": "Unauthorized"}')
        return Response(404)

    monkeypatch.setattr(credential_checks.requests, "get", fake_get)
    monkeypatch.setattr(credential_checks.requests, "post", fake_post)

    res = credential_checks.check_chatgpt_access_token("token-abc", verify_codex=True)
    assert res["state"] == "invalid"
    assert res["web_valid"] is True
    assert res["codex_valid"] is False
    assert res["web_status"] == "valid"
    assert res["codex_status"] == "invalid"


def test_matches_status_filter_codex_valid():
    from core.account_graph import matches_status_filter

    ready_graph = {"overview": {"codex_status": "valid"}}
    invalid_graph = {"overview": {"codex_status": "invalid"}}
    unknown_graph = {"overview": {}}

    assert matches_status_filter(ready_graph, "codex_valid") is True
    assert matches_status_filter(invalid_graph, "codex_valid") is False
    assert matches_status_filter(unknown_graph, "codex_valid") is False


def test_check_accounts_codex_readiness():
    from application.account_exports import AccountExportsService
    from domain.accounts import AccountExportSelection, AccountRecord

    class DummyRepo:
        def select_for_export(self, selection):
            return [
                AccountRecord(
                    id=1,
                    platform="chatgpt",
                    email="ready@test.com",
                    password="p1",
                    credentials=[
                        {"scope": "platform", "key": "access_token", "value": "at-1"},
                        {"scope": "platform", "key": "refresh_token", "value": "rt-1"},
                        {"scope": "platform", "key": "client_id", "value": "app_EMoamEEZ73f0CkXaXp7hrann"},
                    ],
                    overview={"codex_status": "valid"},
                ),
                AccountRecord(
                    id=2,
                    platform="chatgpt",
                    email="no-rt@test.com",
                    password="p2",
                    credentials=[
                        {"scope": "platform", "key": "access_token", "value": "at-2"},
                    ],
                ),
                AccountRecord(
                    id=3,
                    platform="chatgpt",
                    email="codex-invalid@test.com",
                    password="p3",
                    credentials=[
                        {"scope": "platform", "key": "access_token", "value": "at-3"},
                        {"scope": "platform", "key": "refresh_token", "value": "rt-3"},
                    ],
                    overview={"codex_status": "invalid"},
                ),
            ]

    service = AccountExportsService(repository=DummyRepo())
    result = service.check_accounts_codex_readiness(
        AccountExportSelection(platform="chatgpt", ids=[], select_all=True)
    )

    assert result["total_count"] == 3
    assert result["ready_count"] == 1
    assert result["unready_count"] == 2
    assert result["ready_accounts"][0]["email"] == "ready@test.com"

    unready_emails = [acc["email"] for acc in result["unready_accounts"]]
    assert "no-rt@test.com" in unready_emails
    assert "codex-invalid@test.com" in unready_emails

