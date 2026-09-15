from __future__ import annotations

from unittest.mock import patch

from core.local_ms_mailbox import parse_local_ms_pool_rows
from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository


def _row(index: int) -> str:
    return (
        f"user{index}@outlook.com----password-{index}----"
        f"client-{index}----refresh-token-{index}"
    )


def test_stats_alert_levels():
    repo = MicrosoftMailboxRepository()

    # Empty pool -> critical
    stats = repo.stats()
    assert stats["total"] == 0
    assert stats["alert_level"] == "critical"

    # 1 mailbox (remaining 6 <= 10) -> warning
    repo.import_entries(parse_local_ms_pool_rows(_row(1)), max_uses=6)
    stats = repo.stats()
    assert stats["total"] == 1
    assert stats["remaining"] == 6
    assert stats["alert_level"] == "warning"

    # 3 mailboxes (total remaining 18 > 10) -> healthy
    repo.import_entries(
        parse_local_ms_pool_rows("\n".join(_row(i) for i in (2, 3))),
        max_uses=6,
    )
    stats = repo.stats()
    assert stats["total"] == 3
    assert stats["remaining"] == 18
    assert stats["alert_level"] == "healthy"

    # Exhaust all 18 uses -> critical
    for _ in range(18):
        rec = repo.reserve()
        repo.commit(rec.lease_token)

    stats = repo.stats()
    assert stats["remaining"] == 0
    assert stats["exhausted"] == 3
    assert stats["alert_level"] == "critical"


def test_batch_test_mailboxes(client):
    repo = MicrosoftMailboxRepository()
    repo.import_entries(
        parse_local_ms_pool_rows("\n".join([_row(1), _row(2)])),
        max_uses=6,
    )

    def fake_token(self, entry):
        if "user1" in entry.email:
            return "fake-graph-token-123"
        # Simulate invalid_grant: disable mailbox and raise RuntimeError
        repo.disable(entry.email)
        raise RuntimeError(f"Microsoft refresh_token 已失效，邮箱已禁用: {entry.email}")

    with patch("core.local_ms_mailbox.LocalMicrosoftMailboxPool._graph_access_token", side_effect=fake_token, autospec=True):
        resp = client.post("/api/microsoft-mailboxes/batch-test", json={"concurrency": 2})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["tested"] == 2
    assert data["valid"] == 1
    assert data["invalid"] == 1
    assert data["results"]["user1@outlook.com"]["ok"] is True
    assert data["results"]["user2@outlook.com"]["ok"] is False
    assert data["stats"]["disabled"] == 1


def test_clear_disabled_mailboxes(client):
    repo = MicrosoftMailboxRepository()
    repo.import_entries(
        parse_local_ms_pool_rows("\n".join([_row(1), _row(2)])),
        max_uses=6,
    )

    # Disable user2
    assert repo.disable("user2@outlook.com") is True
    assert repo.stats()["disabled"] == 1

    resp = client.post("/api/microsoft-mailboxes/clear-disabled")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["deleted"] == 1
    assert data["stats"]["disabled"] == 0
    assert data["stats"]["total"] == 1

    # user1 still exists in page list
    list_resp = client.get("/api/microsoft-mailboxes")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == "user1@outlook.com"
