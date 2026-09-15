from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from core.db import (
    MicrosoftMailboxModel,
    ProviderSettingModel,
    engine,
    record_registered_email,
)
from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_local_ms_pool_rows
from infrastructure.microsoft_mailbox_repository import (
    MicrosoftMailboxRepository,
    migrate_legacy_microsoft_mailbox_pool,
    migrate_microsoft_mailbox_usage_leases,
)


def _row(index: int) -> str:
    return (
        f"user{index}@outlook.com----password-{index}----"
        f"client-{index}----refresh-token-{index}"
    )


def test_repository_encrypts_credentials_and_preserves_usage_on_reimport():
    repository = MicrosoftMailboxRepository()
    entry = parse_local_ms_pool_rows(_row(1))[0]

    first = repository.import_entries([entry])
    reserved = repository.reserve()
    assert repository.stats()["used"] == 0
    assert repository.stats()["reserved"] == 1
    assert repository.commit(reserved.lease_token) is True
    second = repository.import_entries([entry])

    assert first["inserted"] == 1
    assert second["updated"] == 1
    assert reserved.use_count == 0
    assert reserved.alias_index == 1
    assert repository.stats()["used"] == 1
    with Session(engine) as session:
        stored = session.exec(select(MicrosoftMailboxModel)).one()
    assert stored.password_ciphertext.startswith("sb1:")
    assert stored.refresh_token_ciphertext.startswith("sb1:")
    assert "password-1" not in stored.password_ciphertext
    assert "refresh-token-1" not in stored.refresh_token_ciphertext


def test_repository_reservations_are_atomic_across_instances():
    repository = MicrosoftMailboxRepository()
    repository.import_entries(parse_local_ms_pool_rows("\n".join(_row(i) for i in range(10))))

    def reserve_one(_index: int):
        repo = MicrosoftMailboxRepository()
        item = repo.reserve()
        assert repo.commit(item.lease_token) is True
        return item.email, item.alias_index

    with ThreadPoolExecutor(max_workers=20) as executor:
        reservations = list(executor.map(reserve_one, range(60)))

    assert len(reservations) == 60
    assert len(set(reservations)) == 60
    assert repository.stats() == {
        "total": 10,
        "capacity": 60,
        "used": 60,
        "reserved": 0,
        "remaining": 0,
        "exhausted": 10,
        "disabled": 0,
        "exhaustion_rate": 100.0,
        "alert_level": "critical",
    }


def test_concurrent_wave_spreads_across_parent_mailboxes_first():
    repository = MicrosoftMailboxRepository()
    repository.import_entries(parse_local_ms_pool_rows("\n".join(_row(i) for i in range(24))))

    reservations = [repository.reserve() for _ in range(24)]

    assert len({item.email for item in reservations}) == 24
    assert {item.alias_index for item in reservations} == {1}


def test_failed_registration_releases_slot_without_consuming_usage():
    repository = MicrosoftMailboxRepository()
    repository.import_entries(parse_local_ms_pool_rows(_row(1)))

    first = repository.reserve()
    assert first.alias_index == 1
    assert repository.release(first.lease_token) is True
    assert repository.stats()["used"] == 0
    assert repository.stats()["reserved"] == 0

    retried = repository.reserve()
    assert retried.alias_index == 1
    assert retried.lease_token != first.lease_token


def test_legacy_eager_usage_is_reconciled_from_success_history():
    repository = MicrosoftMailboxRepository()
    repository.import_entries(parse_local_ms_pool_rows(_row(1)))
    record_registered_email("chatgpt", "user1+first@outlook.com")
    record_registered_email("chatgpt", "user1+second@outlook.com")
    with Session(engine) as session:
        mailbox = session.exec(select(MicrosoftMailboxModel)).one()
        mailbox.use_count = 6
        mailbox.status = "exhausted"
        mailbox.allocation_version = 0
        session.add(mailbox)
        session.commit()

    result = migrate_microsoft_mailbox_usage_leases()

    assert result == {
        "mailboxes": 1,
        "used": 2,
        "reclaimed": 4,
        "released_reservations": 0,
    }
    assert repository.stats()["used"] == 2
    next_slot = repository.reserve()
    assert next_slot.alias_index == 3


def test_legacy_pool_text_and_usage_state_migrate_once(tmp_path):
    state_file = tmp_path / "legacy-state.json"
    state_file.write_text(
        json.dumps(
            {
                "used": {
                    "user1@outlook.com#sub-1": {},
                    "user1@outlook.com#sub-2": {},
                }
            }
        ),
        encoding="utf-8",
    )
    setting = ProviderSettingModel(
        provider_type="mailbox",
        provider_key="local_ms_pool",
        display_name="本地微软邮箱池",
    )
    setting.set_config({"local_ms_pool_state_file": str(state_file)})
    setting.set_auth({"local_ms_pool_text": _row(1)})
    with Session(engine) as session:
        session.add(setting)
        session.commit()

    result = migrate_legacy_microsoft_mailbox_pool()

    assert result["migrated"] == 1
    assert result["used"] == 2
    pool = LocalMicrosoftMailboxPool()
    mailbox = pool.get_email()
    assert mailbox.email.startswith("user1+")
    assert mailbox.email.endswith("@outlook.com")
    assert mailbox.account_id.endswith("#sub-3")
    assert mailbox.extra["provider_resource"]["metadata"]["alias_index"] == 3
    with Session(engine) as session:
        migrated_setting = session.exec(select(ProviderSettingModel)).one()
    assert "local_ms_pool_text" not in migrated_setting.get_auth()
    assert "local_ms_pool_state_file" not in migrated_setting.get_config()


def test_multipart_import_api_deduplicates_and_never_returns_secrets(client):
    response = client.post(
        "/api/microsoft-mailboxes/import",
        files=[
            ("files", ("first.txt", f"{_row(1)}\n{_row(2)}", "text/plain")),
            ("files", ("second.txt", f"{_row(2)}\n{_row(3)}", "text/plain")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["files"] == 2
    assert payload["unique"] == 3
    assert payload["inserted"] == 3
    assert payload["duplicates_in_upload"] == 1
    assert payload["capacity"] == 18

    listed = client.get("/api/microsoft-mailboxes?page_size=10")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 3
    assert set(items[0]) == {
        "id",
        "email",
        "use_count",
        "max_uses",
        "status",
        "source_format",
        "updated_at",
    }
    assert "password" not in listed.text
    assert "refresh-token" not in listed.text
