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
