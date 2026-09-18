"""Tests for FreeGPT-Manager encrypted backup, snapshot, and migration service."""
from __future__ import annotations

import io
import pytest
from sqlmodel import Session

from core.db import AccountModel, engine
from core.backup_service import (
    create_encrypted_backup,
    inspect_encrypted_backup,
    restore_encrypted_backup,
    create_local_snapshot,
    list_local_snapshots,
    delete_local_snapshot,
)


def test_create_and_inspect_encrypted_backup():
    # Insert test account
    with Session(engine) as session:
        acc = AccountModel(platform="chatgpt", email="backup_test@example.com", password="pass")
        session.add(acc)
        session.commit()

    password = "MySecurePassword123!"
    data, filename, manifest = create_encrypted_backup(password)

    assert data.startswith(b"FGM_BAK_V1")
    assert filename.startswith("FreeGPT_Manager_Backup_")
    assert filename.endswith(".fgmbak")
    assert manifest["total_accounts"] >= 1
    assert "chatgpt" in manifest["accounts_by_platform"]

    # Test inspect with wrong password
    with pytest.raises(ValueError, match="解密失败"):
        inspect_encrypted_backup(data, "WrongPassword!")

    # Test inspect with correct password
    inspected_manifest = inspect_encrypted_backup(data, password)
    assert inspected_manifest["total_accounts"] == manifest["total_accounts"]
    assert inspected_manifest["app_name"] == "FreeGPT-Manager"


def test_restore_encrypted_backup():
    password = "TestRestorePassword2026!"

    with Session(engine) as session:
        acc1 = AccountModel(platform="chatgpt", email="acc1@example.com", password="p1")
        acc2 = AccountModel(platform="chatgpt", email="acc2@example.com", password="p2")
        session.add(acc1)
        session.add(acc2)
        session.commit()

    # Create backup containing 2 accounts
    backup_bytes, _, _ = create_encrypted_backup(password)

    # Now simulate data deletion
    with Session(engine) as session:
        all_accs = session.query(AccountModel).all()
        for a in all_accs:
            session.delete(a)
        session.commit()

    with Session(engine) as session:
        assert len(session.query(AccountModel).all()) == 0

    # Restore from backup
    restore_res = restore_encrypted_backup(backup_bytes, password)
    assert restore_res["success"] is True
    assert restore_res["restored_manifest"]["total_accounts"] == 2

    # Verify accounts are back
    with Session(engine) as session:
        restored_accounts = session.query(AccountModel).all()
        emails = {a.email for a in restored_accounts}
        assert "acc1@example.com" in emails
        assert "acc2@example.com" in emails


def test_local_snapshots_lifecycle():
    snap = create_local_snapshot(label="test_snap")
    filename = snap["filename"]
    assert filename.startswith("snapshot_")

    all_snaps = list_local_snapshots()
    filenames = [s["filename"] for s in all_snaps]
    assert filename in filenames

    # Test deleting snapshot
    deleted = delete_local_snapshot(filename)
    assert deleted is True

    remaining = [s["filename"] for s in list_local_snapshots()]
    assert filename not in remaining


def test_backup_api_endpoints(client):
    # Test export endpoint
    res = client.post("/api/backup/export", json={"password": "ApiPassword123!"})
    assert res.status_code == 200
    content = res.content
    assert content.startswith(b"FGM_BAK_V1")

    # Test inspect endpoint with wrong password
    files = {"file": ("backup.fgmbak", io.BytesIO(content), "application/octet-stream")}
    res_inspect_bad = client.post("/api/backup/inspect", files=files, data={"password": "bad"})
    assert res_inspect_bad.status_code == 400
    assert "解密失败" in res_inspect_bad.json()["detail"]

    # Test inspect endpoint with correct password
    files = {"file": ("backup.fgmbak", io.BytesIO(content), "application/octet-stream")}
    res_inspect_ok = client.post("/api/backup/inspect", files=files, data={"password": "ApiPassword123!"})
    assert res_inspect_ok.status_code == 200
    assert res_inspect_ok.json()["success"] is True

    # Test snapshots API
    res_snap_create = client.post("/api/backup/snapshots", json={"label": "api_test"})
    assert res_snap_create.status_code == 200
    snap_name = res_snap_create.json()["snapshot"]["filename"]

    res_snap_list = client.get("/api/backup/snapshots")
    assert res_snap_list.status_code == 200
    snap_names = [s["filename"] for s in res_snap_list.json()["snapshots"]]
    assert snap_name in snap_names

    # Clean up snapshot
    res_del = client.delete(f"/api/backup/snapshots/{snap_name}")
    assert res_del.status_code == 200
