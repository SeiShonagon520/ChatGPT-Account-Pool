"""Unit tests for account import parsing, upsert logic, and API endpoints."""
from __future__ import annotations

import io
import pytest
from application.accounts import parse_account_import_lines, AccountsService
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository
from sqlmodel import Session, select
from core.db import AccountModel, engine


def test_parse_account_import_lines_ms_oauth():
    lines = [
        "test1@outlook.com----Pass123!----00000003-0000-0ff1-ce00-000000000000----M.C544_BAY.0.U.-Cv3t2TestRefreshTokenLongEnough----recovery1@gmail.com",
        "  # comment line",
        "",
        "test2@hotmail.com\tPass456!\t9e5f94bc-e8a4-4e73-b8be-63364c29d753\tM.C544_BAY.0.U.-CsecondTokenLongEnough",
    ]
    parsed, ms_entries = parse_account_import_lines(lines)

    assert len(parsed) == 2
    assert len(ms_entries) == 2

    # Verify first account
    assert parsed[0].email == "test1@outlook.com"
    assert parsed[0].password == "Pass123!"
    assert parsed[0].extra["credentials"]["auth_type"] == "passwordless"
    assert parsed[0].extra["provider_accounts"][0]["provider_name"] == "local_ms_pool"
    assert parsed[0].extra["provider_accounts"][0]["credentials"]["client_id"] == "00000003-0000-0ff1-ce00-000000000000"
    assert parsed[0].extra["provider_accounts"][0]["credentials"]["recovery_email"] == "recovery1@gmail.com"

    # Verify ms_entries
    assert ms_entries[0].email == "test1@outlook.com"
    assert ms_entries[0].client_id == "00000003-0000-0ff1-ce00-000000000000"
    assert ms_entries[1].email == "test2@hotmail.com"


def test_parse_account_import_lines_2fa_and_simple():
    lines = [
        "user2fa@gmail.com----SecretPass123----JBSWY3DPEHPK3PXP",
        "simple@example.com----MyPassword999",
        '{"email": "json@test.com", "password": "jsonpass", "totp_secret": "ABCDEF2345678901"}',
    ]
    parsed, ms_entries = parse_account_import_lines(lines)

    assert len(ms_entries) == 0
    assert len(parsed) == 3

    assert parsed[0].email == "user2fa@gmail.com"
    assert parsed[0].extra["credentials"]["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert parsed[0].extra["credentials"]["auth_type"] == "password"

    assert parsed[1].email == "simple@example.com"
    assert parsed[1].password == "MyPassword999"

    assert parsed[2].email == "json@test.com"
    assert parsed[2].password == "jsonpass"


def test_parse_account_import_lines_json_with_2fa_and_tokens():
    raw_json = (
        '{"phone":"5427937208","email":"JeremyReesek35410U@outlook.com","password":"Jn9MbxSzkLB#siSH",'
        '"2fa":"BPOVLXNLZ4V6DSR6OO6LKNLE6UGIF5SY","chatgpt_account_id":"5869e4e4-3bae-4b58-9d54-8d483e28b81a",'
        '"access_token":"token-abc","refresh_token":"rt-xyz","id_token":"id-123"}'
    )
    parsed, ms_entries = parse_account_import_lines([raw_json])
    assert len(parsed) == 1
    assert len(ms_entries) == 0
    acc = parsed[0]
    assert acc.email == "JeremyReesek35410U@outlook.com"
    assert acc.password == "Jn9MbxSzkLB#siSH"
    assert acc.extra["totp_secret"] == "BPOVLXNLZ4V6DSR6OO6LKNLE6UGIF5SY"
    assert acc.extra["credentials"]["totp_secret"] == "BPOVLXNLZ4V6DSR6OO6LKNLE6UGIF5SY"
    assert acc.extra["credentials"]["auth_type"] == "password"
    assert acc.extra["refresh_token"] == "rt-xyz"
    assert acc.extra["access_token"] == "token-abc"
    assert acc.extra["id_token"] == "id-123"
    assert acc.extra["chatgpt_account_id"] == "5869e4e4-3bae-4b58-9d54-8d483e28b81a"
    assert acc.extra["phone"] == "5427937208"


def test_parse_account_import_lines_multiline_json():
    multiline = [
        "{",
        '  "email": "multiline@test.com",',
        '  "password": "pass",',
        '  "2fa": "JBSWY3DPEHPK3PXP"',
        "}",
    ]
    parsed, ms_entries = parse_account_import_lines(multiline)
    assert len(parsed) == 1
    assert parsed[0].email == "multiline@test.com"
    assert parsed[0].extra["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_import_accounts_service_upsert(client):
    service = AccountsService()
    unique_email = f"upsert_test_{id(client)}@outlook.com"

    # First import
    lines = [
        f"{unique_email}----InitialPass----00000003-0000-0ff1-ce00-000000000000----M.C544_BAY.0.U.-CinitToken1234567890",
    ]
    res1 = service.import_accounts("chatgpt", lines)
    assert res1["ok"] is True
    assert res1["created"] == 1
    assert res1["updated"] == 0
    assert res1["mailboxes_saved"] >= 1

    # Second import: update password and token
    lines_updated = [
        f"{unique_email}----UpdatedPass----00000003-0000-0ff1-ce00-000000000000----M.C544_BAY.0.U.-CupdatedToken1234567890",
    ]
    res2 = service.import_accounts("chatgpt", lines_updated)
    assert res2["ok"] is True
    assert res2["created"] == 0
    assert res2["updated"] == 1

    # Verify in DB that only 1 record exists and password is updated
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel).where(
                AccountModel.platform == "chatgpt",
                AccountModel.email == unique_email,
            )
        ).all()
        assert len(models) == 1
        assert models[0].password == "UpdatedPass"


def test_api_import_endpoints(client):
    test_email = "api_import_test@outlook.com"
    resp = client.post(
        "/api/accounts/import",
        json={
            "platform": "chatgpt",
            "lines": [f"{test_email}----ApiPass123----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C544_BAY.0.U.-CapiToken1234567890"],
            "auto_check": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["parsed"] == 1

    # Test /import-file
    file_content = f"file_import_test@outlook.com----FilePass123\n".encode("utf-8")
    resp_file = client.post(
        "/api/accounts/import-file",
        files={"file": ("accounts.txt", io.BytesIO(file_content), "text/plain")},
        data={"platform": "chatgpt"},
    )
    assert resp_file.status_code == 200
    data_file = resp_file.json()
    assert data_file["ok"] is True
    assert data_file["parsed"] == 1


def test_parse_sub2api_bundle_and_agent_identity():
    # 1. Sub2API bundle format
    sub2api_bundle = {
        "proxies": [],
        "accounts": [
            {
                "name": "sub2_user1@outlook.com",
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "access_token": "token_sub1",
                    "refresh_token": "rt_sub1",
                    "client_id": "cid_sub1",
                    "chatgpt_account_id": "acc_sub1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                },
            },
            {
                "name": "sub2_user2@gmail.com",
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "access_token": "token_sub2",
                    "refresh_token": "rt_sub2",
                },
            },
        ],
    }
    import json
    lines = [json.dumps(sub2api_bundle)]
    parsed, ms_entries = parse_account_import_lines(lines)
    assert len(parsed) == 2
    assert parsed[0].email == "sub2_user1@outlook.com"
    assert parsed[0].extra["access_token"] == "token_sub1"
    assert parsed[0].extra["refresh_token"] == "rt_sub1"
    assert parsed[0].extra["client_id"] == "cid_sub1"
    assert parsed[0].extra["chatgpt_account_id"] == "acc_sub1"
    assert parsed[0].extra["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert parsed[1].email == "sub2_user2@gmail.com"

    # 2. Sub2API Agent Identity format
    agent_id_data = {
        "auth_mode": "agentIdentity",
        "agent_identity": {
            "email": "agent_user@openai.com",
            "account_id": "agent-acc-999",
            "certificate": "cert-data-here",
        },
    }
    parsed_agent, _ = parse_account_import_lines([json.dumps(agent_id_data)])
    assert len(parsed_agent) == 1
    assert parsed_agent[0].email == "agent_user@openai.com"
    assert parsed_agent[0].extra["account_id"] == "agent-acc-999"
    assert parsed_agent[0].extra["agent_identity"]["certificate"] == "cert-data-here"


def test_parse_base64_sub():
    import base64
    import json
    content = [
        "b64user1@test.com----Pass1----TOTP111111111111",
        "b64user2@test.com----Pass2----TOTP222222222222",
    ]
    raw_text = "\n".join(content)
    b64_str = base64.b64encode(raw_text.encode("utf-8")).decode("utf-8")

    parsed, _ = parse_account_import_lines([b64_str])
    assert len(parsed) == 2
    assert parsed[0].email == "b64user1@test.com"
    assert parsed[1].email == "b64user2@test.com"

    # Also test sub:// scheme
    sub_uri = "sub://" + b64_str
    parsed_sub, _ = parse_account_import_lines([sub_uri])
    assert len(parsed_sub) == 2
    assert parsed_sub[0].email == "b64user1@test.com"


def test_parse_pipe_delimiter():
    lines = [
        "pipe1@test.com|Pass123|JBSWY3DPEHPK3PXP",
        "pipe2@test.com||Pass456||JBSWY3DPEHPK3PXP",
    ]
    parsed, _ = parse_account_import_lines(lines)
    assert len(parsed) == 2
    assert parsed[0].email == "pipe1@test.com"
    assert parsed[0].password == "Pass123"
    assert parsed[0].extra["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert parsed[1].email == "pipe2@test.com"
    assert parsed[1].password == "Pass456"


def test_parse_jwt_token():
    import base64
    import json
    # Construct a dummy JWT with email payload
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("utf-8").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "https://api.openai.com/profile": {"email": "jwt_extracted@test.com"},
            "exp": 1800000000,
        }).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    dummy_jwt = f"{header}.{payload}.signature"

    # Test standalone JWT line
    parsed, _ = parse_account_import_lines([dummy_jwt])
    assert len(parsed) == 1
    assert parsed[0].email == "jwt_extracted@test.com"
    assert parsed[0].extra["access_token"] == dummy_jwt
    assert parsed[0].extra["at_expires_at"] == 1800000000


def test_api_import_json_and_sub_files(client):
    import json
    import base64
    # Test uploading a JSON file containing Sub2API accounts
    sub2api_data = {
        "accounts": [
            {
                "name": "api_file_sub@test.com",
                "credentials": {
                    "access_token": "at_123",
                    "refresh_token": "rt_123",
                },
            }
        ]
    }
    json_bytes = json.dumps(sub2api_data).encode("utf-8")
    resp = client.post(
        "/api/accounts/import-file",
        files={"file": ("accounts.json", io.BytesIO(json_bytes), "application/json")},
        data={"platform": "chatgpt"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["parsed"] == 1

    # Test uploading a .sub file containing Base64 content
    sub_content = base64.b64encode(b"file_sub@test.com----Pass123").decode("utf-8").encode("utf-8")
    resp_sub = client.post(
        "/api/accounts/import-file",
        files={"file": ("subscription.sub", io.BytesIO(sub_content), "application/octet-stream")},
        data={"platform": "chatgpt"},
    )
    assert resp_sub.status_code == 200
    data_sub = resp_sub.json()
    assert data_sub["ok"] is True
    assert data_sub["parsed"] == 1

