"""Tests for new account features: AT exp, status filter, batch delete, mailbox endpoints."""
from __future__ import annotations

import base64
import json
import time
from application.accounts import _extract_jwt_exp, AccountsService
from domain.accounts import AccountQuery
from infrastructure.accounts_repository import AccountsRepository


def test_extract_jwt_exp():
    now = int(time.time())
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": now + 3600}).encode()).decode().rstrip("=")
    fake_jwt = f"{header}.{payload}.sig"

    assert _extract_jwt_exp(fake_jwt) == now + 3600
    assert _extract_jwt_exp("invalid.token") is None
    assert _extract_jwt_exp("") is None


def test_status_filter_and_batch_delete(client):
    service = AccountsService()
    unique1 = f"filter_test_1_{id(client)}@outlook.com"
    unique2 = f"filter_test_2_{id(client)}@outlook.com"

    service.import_accounts("chatgpt", [
        f"{unique1}----Pass1----00000003-0000-0ff1-ce00-000000000000----M.C544_BAY.0.U.-CtestToken1234567890",
        f"{unique2}----Pass2",
    ])

    # Filter by has_mailbox
    resp_mb = client.get(f"/api/accounts?status=has_mailbox&email={unique1}")
    assert resp_mb.status_code == 200
    items = resp_mb.json()["items"]
    assert len(items) == 1
    assert items[0]["has_mailbox"] is True
    assert items[0]["mailbox_email"] == unique1
    acc1_id = items[0]["id"]

    resp_no_mb = client.get(f"/api/accounts?status=has_mailbox&email={unique2}")
    assert resp_no_mb.status_code == 200
    assert len(resp_no_mb.json()["items"]) == 0

    # Get acc2_id
    resp_acc2 = client.get(f"/api/accounts?email={unique2}")
    acc2_id = resp_acc2.json()["items"][0]["id"]

    # Batch delete both
    resp_del = client.post("/api/accounts/batch-delete", json={"ids": [acc1_id, acc2_id]})
    assert resp_del.status_code == 200
    assert resp_del.json()["deleted"] == 2


def test_mailbox_import_text_and_delete(client):
    unique_mb = f"mb_test_{id(client)}@outlook.com"
    resp = client.post(
        "/api/microsoft-mailboxes/import-text",
        json={"text": f"{unique_mb}----Pass123----00000003-0000-0ff1-ce00-000000000000----M.C544_BAY.0.U.-CtestToken1234567890"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Check listed
    resp_list = client.get(f"/api/microsoft-mailboxes?search={unique_mb}")
    assert resp_list.status_code == 200
    assert len(resp_list.json()["items"]) == 1

    # Delete
    resp_del = client.delete(f"/api/microsoft-mailboxes/{unique_mb}")
    assert resp_del.status_code == 200
    assert resp_del.json()["ok"] is True


def test_export_recovered_sub2api_bundle(client):
    import json
    from sqlmodel import Session
    from core.db import engine, TaskModel
    from application.tasks import TASK_TYPE_REFRESH_TOKEN_CHECK, TASK_STATUS_SUCCEEDED

    service = AccountsService()
    unique = f"recovered_test_{id(client)}@outlook.com"
    service.import_accounts("chatgpt", [f"{unique}----Pass123"])

    resp_acc = client.get(f"/api/accounts?email={unique}")
    acc_id = resp_acc.json()["items"][0]["id"]

    # Create a fake task with recovered_account_ids
    task_id = f"test-rec-{id(client)}"
    with Session(engine) as session:
        task = TaskModel(
            id=task_id,
            type=TASK_TYPE_REFRESH_TOKEN_CHECK,
            platform="chatgpt",
            status=TASK_STATUS_SUCCEEDED,
            payload_json=json.dumps({"platform": "chatgpt"}),
            result_json=json.dumps({
                "data": {
                    "recovered_account_ids": [acc_id],
                    "recovered_emails": [unique],
                    "recovered_count": 1,
                }
            }),
        )
        session.add(task)
        session.commit()



    # Call endpoint
    export_resp = client.get(f"/api/accounts/tasks/{task_id}/export-recovered-sub2api")
    assert export_resp.status_code == 200, f"Export failed: {export_resp.status_code} {export_resp.text}"
    assert export_resp.headers["content-type"].startswith("application/json")
    assert "_sub2api.json" in export_resp.headers["content-disposition"]

    data = json.loads(export_resp.content.decode("utf-8"))
    assert "accounts" in data
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["name"] == unique
