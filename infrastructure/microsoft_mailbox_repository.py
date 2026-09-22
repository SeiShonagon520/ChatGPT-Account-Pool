from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, or_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from core.db import (
    MicrosoftMailboxLeaseModel,
    MicrosoftMailboxModel,
    ProviderSettingModel,
    RegisteredEmailHistoryModel,
    engine,
)
from core.secret_store import decrypt_secret, encrypt_secret


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _email_key(value: object) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class MicrosoftMailboxRecord:
    id: int
    email: str
    password: str
    login_account: str
    imap_host: str
    imap_port: str
    imap_account_type: str
    imap_security: str
    smtp_host: str
    smtp_port: str
    smtp_security: str
    note: str
    proxy_mode: str
    proxy: str
    label: str
    recovery_email: str
    recovery_password: str
    client_id: str
    refresh_token: str
    totp_secret: str
    source_format: str
    use_count: int
    max_uses: int
    status: str
    alias_index: int = 0
    lease_token: str = ""


class MicrosoftMailboxRepository:
    def _record(self, row) -> MicrosoftMailboxRecord:
        getter = row.get if hasattr(row, "get") else lambda key, default=None: getattr(row, key, default)
        return MicrosoftMailboxRecord(
            id=int(getter("id") or 0),
            email=str(getter("email") or ""),
            password=decrypt_secret(getter("password_ciphertext") or ""),
            login_account=str(getter("login_account") or ""),
            imap_host=str(getter("imap_host") or ""),
            imap_port=str(getter("imap_port") or ""),
            imap_account_type=str(getter("imap_account_type") or ""),
            imap_security=str(getter("imap_security") or ""),
            smtp_host=str(getter("smtp_host") or ""),
            smtp_port=str(getter("smtp_port") or ""),
            smtp_security=str(getter("smtp_security") or ""),
            note=str(getter("note") or ""),
            proxy_mode=str(getter("proxy_mode") or ""),
            proxy=str(getter("proxy") or ""),
            label=str(getter("label") or ""),
            recovery_email=str(getter("recovery_email") or ""),
            recovery_password=decrypt_secret(getter("recovery_password_ciphertext") or ""),
            client_id=str(getter("client_id") or ""),
            refresh_token=decrypt_secret(getter("refresh_token_ciphertext") or ""),
            totp_secret=decrypt_secret(getter("totp_secret_ciphertext") or ""),
            source_format=str(getter("source_format") or ""),
            use_count=int(getter("use_count") or 0),
            max_uses=max(int(getter("max_uses") or 6), 1),
            status=str(getter("status") or "available"),
            alias_index=int(getter("lease_alias_index") or getter("alias_index") or 0),
            lease_token=str(getter("lease_token") or ""),
        )

    @staticmethod
    def _payload(entry, *, max_uses: int) -> dict:
        now = _utcnow()
        email = str(getattr(entry, "email", "") or "").strip()
        return {
            "email": email,
            "email_key": _email_key(email),
            "password_ciphertext": encrypt_secret(getattr(entry, "password", "")),
            "login_account": str(getattr(entry, "login_account", "") or email),
            "imap_host": str(getattr(entry, "imap_host", "") or ""),
            "imap_port": str(getattr(entry, "imap_port", "") or ""),
            "imap_account_type": str(getattr(entry, "imap_account_type", "") or ""),
            "imap_security": str(getattr(entry, "imap_security", "") or ""),
            "smtp_host": str(getattr(entry, "smtp_host", "") or ""),
            "smtp_port": str(getattr(entry, "smtp_port", "") or ""),
            "smtp_security": str(getattr(entry, "smtp_security", "") or ""),
            "note": str(getattr(entry, "note", "") or ""),
            "proxy_mode": str(getattr(entry, "proxy_mode", "") or ""),
            "proxy": str(getattr(entry, "proxy", "") or ""),
            "label": str(getattr(entry, "label", "") or ""),
            "recovery_email": str(getattr(entry, "recovery_email", "") or ""),
            "recovery_password_ciphertext": encrypt_secret(
                getattr(entry, "recovery_password", "")
            ),
            "client_id": str(getattr(entry, "client_id", "") or ""),
            "refresh_token_ciphertext": encrypt_secret(
                getattr(entry, "refresh_token", "")
            ),
            "totp_secret_ciphertext": encrypt_secret(getattr(entry, "totp_secret", "")),
            "source_format": str(getattr(entry, "source_format", "") or ""),
            "max_uses": max(int(max_uses or 6), 1),
            "allocation_version": 1,
            "created_at": now,
            "updated_at": now,
        }

    def import_entries(self, entries: Iterable, *, max_uses: int = 6) -> dict:
        unique: dict[str, object] = {}
        for entry in entries:
            key = _email_key(getattr(entry, "email", ""))
            if key:
                unique[key] = entry
        if not unique:
            return {"received": 0, "inserted": 0, "updated": 0, **self.stats()}

        keys = list(unique)
        existing: set[str] = set()
        with Session(engine) as session:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                existing.update(
                    str(value)
                    for value in session.exec(
                        select(MicrosoftMailboxModel.email_key).where(
                            MicrosoftMailboxModel.email_key.in_(chunk)
                        )
                    ).all()
                )

        payloads = [self._payload(entry, max_uses=max_uses) for entry in unique.values()]
        table = MicrosoftMailboxModel.__table__
        statement = sqlite_insert(table).on_conflict_do_update(
            index_elements=[table.c.email_key],
            set_={
                column: getattr(sqlite_insert(table).excluded, column)
                for column in (
                    "email",
                    "password_ciphertext",
                    "login_account",
                    "imap_host",
                    "imap_port",
                    "imap_account_type",
                    "imap_security",
                    "smtp_host",
                    "smtp_port",
                    "smtp_security",
                    "note",
                    "proxy_mode",
                    "proxy",
                    "label",
                    "recovery_email",
                    "recovery_password_ciphertext",
                    "client_id",
                    "refresh_token_ciphertext",
                    "totp_secret_ciphertext",
                    "source_format",
                    "max_uses",
                    "updated_at",
                )
            },
        )
        with engine.begin() as connection:
            connection.execute(statement, payloads)

        stats = self.stats()
        return {
            "received": len(unique),
            "inserted": len(set(keys) - existing),
            "updated": len(existing),
            **stats,
        }

    def reserve(
        self,
        *,
        allow_reuse: bool = False,
        lease_seconds: int = 7200,
    ) -> MicrosoftMailboxRecord:
        if allow_reuse:
            with Session(engine) as session:
                row = session.exec(
                    select(MicrosoftMailboxModel)
                    .where(MicrosoftMailboxModel.status != "disabled")
                    .order_by(MicrosoftMailboxModel.id)
                    .limit(1)
                ).first()
            if row is None:
                raise RuntimeError("本地微软邮箱池为空")
            return self._record(row)

        now = _utcnow()
        expires_at = now + timedelta(seconds=max(int(lease_seconds or 0), 60))
        lease_token = uuid.uuid4().hex
        connection = engine.connect()
        row = None
        try:
            # Serialize slot selection and insertion so concurrent workers
            # cannot lease the same parent/alias slot.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.execute(
                text(
                    """
                    DELETE FROM microsoft_mailbox_leases
                    WHERE status = 'reserved'
                      AND expires_at IS NOT NULL
                      AND expires_at <= :now
                    """
                ),
                {"now": now},
            )
            row = connection.execute(
                text(
                    """
                    WITH RECURSIVE alias_slots(alias_index) AS (
                        SELECT 1
                        UNION ALL
                        SELECT alias_index + 1
                        FROM alias_slots
                        WHERE alias_index < 256
                    )
                    SELECT
                        m.*,
                        alias_slots.alias_index AS lease_alias_index
                    FROM microsoft_mailboxes AS m
                    JOIN alias_slots ON alias_slots.alias_index <= m.max_uses
                    LEFT JOIN microsoft_mailbox_leases AS lease
                      ON lease.mailbox_id = m.id
                     AND lease.alias_index = alias_slots.alias_index
                    WHERE m.status != 'disabled'
                      AND lease.id IS NULL
                    ORDER BY
                        (
                            SELECT COUNT(*)
                            FROM microsoft_mailbox_leases AS occupied
                            WHERE occupied.mailbox_id = m.id
                        ),
                        m.id,
                        alias_slots.alias_index
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is not None:
                connection.execute(
                    text(
                        """
                        INSERT INTO microsoft_mailbox_leases (
                            mailbox_id,
                            alias_index,
                            lease_token,
                            status,
                            expires_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :mailbox_id,
                            :alias_index,
                            :lease_token,
                            'reserved',
                            :expires_at,
                            :now,
                            :now
                        )
                        """
                    ),
                    {
                        "mailbox_id": int(row["id"]),
                        "alias_index": int(row["lease_alias_index"]),
                        "lease_token": lease_token,
                        "expires_at": expires_at,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE microsoft_mailboxes
                        SET last_reserved_at = :now, updated_at = :now
                        WHERE id = :mailbox_id
                        """
                    ),
                    {"now": now, "mailbox_id": int(row["id"])},
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if row is None:
            stats = self.stats()
            raise RuntimeError(
                "本地微软邮箱池已用尽: "
                f"total={stats['total']}, capacity={stats['capacity']}"
            )
        payload = dict(row)
        payload["lease_token"] = lease_token
        return self._record(payload)

    def commit(self, lease_token: str) -> bool:
        token = str(lease_token or "").strip()
        if not token:
            return False
        now = _utcnow()
        connection = engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            lease = connection.execute(
                text(
                    """
                    SELECT mailbox_id, status
                    FROM microsoft_mailbox_leases
                    WHERE lease_token = :lease_token
                    """
                ),
                {"lease_token": token},
            ).mappings().first()
            if lease is None:
                connection.rollback()
                return False
            mailbox_id = int(lease["mailbox_id"])
            if str(lease["status"]) != "committed":
                connection.execute(
                    text(
                        """
                        UPDATE microsoft_mailbox_leases
                        SET status = 'committed', expires_at = NULL, updated_at = :now
                        WHERE lease_token = :lease_token AND status = 'reserved'
                        """
                    ),
                    {"lease_token": token, "now": now},
                )
            self._sync_usage(connection, mailbox_id=mailbox_id, now=now)
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release(self, lease_token: str) -> bool:
        token = str(lease_token or "").strip()
        if not token:
            return False
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    DELETE FROM microsoft_mailbox_leases
                    WHERE lease_token = :lease_token AND status = 'reserved'
                    """
                ),
                {"lease_token": token},
            )
        return bool(result.rowcount)

    @staticmethod
    def _sync_usage(connection, *, mailbox_id: int, now: datetime) -> None:
        connection.execute(
            text(
                """
                UPDATE microsoft_mailboxes
                SET
                    use_count = (
                        SELECT COUNT(*)
                        FROM microsoft_mailbox_leases
                        WHERE mailbox_id = :mailbox_id AND status = 'committed'
                    ),
                    status = CASE
                        WHEN status = 'disabled' THEN 'disabled'
                        WHEN (
                            SELECT COUNT(*)
                            FROM microsoft_mailbox_leases
                            WHERE mailbox_id = :mailbox_id AND status = 'committed'
                        ) >= max_uses THEN 'exhausted'
                        ELSE 'available'
                    END,
                    updated_at = :now
                WHERE id = :mailbox_id
                """
            ),
            {"mailbox_id": int(mailbox_id), "now": now},
        )

    def peek(self) -> MicrosoftMailboxRecord:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DELETE FROM microsoft_mailbox_leases
                    WHERE status = 'reserved'
                      AND expires_at IS NOT NULL
                      AND expires_at <= :now
                    """
                ),
                {"now": _utcnow()},
            )
            row = connection.execute(
                text(
                    """
                    WITH RECURSIVE alias_slots(alias_index) AS (
                        SELECT 1
                        UNION ALL
                        SELECT alias_index + 1
                        FROM alias_slots
                        WHERE alias_index < 256
                    )
                    SELECT
                        m.*,
                        alias_slots.alias_index AS lease_alias_index
                    FROM microsoft_mailboxes AS m
                    JOIN alias_slots ON alias_slots.alias_index <= m.max_uses
                    LEFT JOIN microsoft_mailbox_leases AS lease
                      ON lease.mailbox_id = m.id
                     AND lease.alias_index = alias_slots.alias_index
                    WHERE m.status != 'disabled'
                      AND lease.id IS NULL
                    ORDER BY
                        (
                            SELECT COUNT(*)
                            FROM microsoft_mailbox_leases AS occupied
                            WHERE occupied.mailbox_id = m.id
                        ),
                        m.id,
                        alias_slots.alias_index
                    LIMIT 1
                    """
                )
            ).mappings().first()
        if row is None:
            stats = self.stats()
            raise RuntimeError(
                "本地微软邮箱池已用尽: "
                f"total={stats['total']}, capacity={stats['capacity']}"
            )
        return self._record(row)

    def get_by_parent_email(self, email: str) -> MicrosoftMailboxRecord | None:
        with Session(engine) as session:
            row = session.exec(
                select(MicrosoftMailboxModel).where(
                    MicrosoftMailboxModel.email_key == _email_key(email)
                )
            ).first()
        return self._record(row) if row is not None else None

    def set_status(self, email: str, status: str) -> bool:
        """Update mailbox status between 'available' and 'disabled'."""
        key = _email_key(email)
        target = str(status or "").strip().lower()
        if not key or target not in {"available", "disabled"}:
            return False
        now = _utcnow()
        with Session(engine) as session:
            mailbox = session.exec(
                select(MicrosoftMailboxModel).where(
                    MicrosoftMailboxModel.email_key == key
                )
            ).first()
            if mailbox is None:
                return False
            if target == "disabled":
                mailbox.status = "disabled"
            else:
                mailbox.status = (
                    "exhausted"
                    if int(mailbox.use_count or 0) >= int(mailbox.max_uses or 1)
                    else "available"
                )
            mailbox.updated_at = now
            session.add(mailbox)
            session.commit()
            return True

    def disable(self, email: str) -> bool:
        """Permanently remove a mailbox with unusable credentials from allocation."""
        return self.set_status(email, "disabled")

    def delete_by_email(self, email: str) -> bool:
        key = _email_key(email)
        if not key:
            return False
        statement = text(
            """
            DELETE FROM microsoft_mailboxes
            WHERE email_key = :email_key
            """
        )
        with engine.begin() as connection:
            result = connection.execute(statement, {"email_key": key})
        return bool(result.rowcount)

    def delete_disabled(self) -> int:
        statement = text(
            """
            DELETE FROM microsoft_mailboxes
            WHERE status = 'disabled'
            """
        )
        with engine.begin() as connection:
            result = connection.execute(statement)
        return int(result.rowcount or 0)

    def list_for_testing(self, emails: list[str] | None = None) -> list[MicrosoftMailboxRecord]:
        with Session(engine) as session:
            statement = select(MicrosoftMailboxModel)
            if emails:
                keys = [_email_key(e) for e in emails if _email_key(e)]
                statement = statement.where(MicrosoftMailboxModel.email_key.in_(keys))
            else:
                statement = statement.where(MicrosoftMailboxModel.status != "disabled")
            rows = session.exec(statement.order_by(MicrosoftMailboxModel.id)).all()
            return [self._record(row) for row in rows]

    def stats(self) -> dict:
        now = _utcnow()
        with Session(engine) as session:
            total, capacity, used, available = session.exec(
                select(
                    func.count(MicrosoftMailboxModel.id),
                    func.coalesce(func.sum(MicrosoftMailboxModel.max_uses), 0),
                    func.coalesce(func.sum(MicrosoftMailboxModel.use_count), 0),
                    func.coalesce(
                        func.sum(
                            MicrosoftMailboxModel.max_uses - MicrosoftMailboxModel.use_count
                        ),
                        0,
                    ),
                ).where(MicrosoftMailboxModel.status != "disabled")
            ).one()
            exhausted = session.exec(
                select(func.count(MicrosoftMailboxModel.id)).where(
                    MicrosoftMailboxModel.status == "exhausted"
                )
            ).one()
            disabled = session.exec(
                select(func.count(MicrosoftMailboxModel.id)).where(
                    MicrosoftMailboxModel.status == "disabled"
                )
            ).one()
            reserved = session.exec(
                select(func.count(MicrosoftMailboxLeaseModel.id))
                .join(
                    MicrosoftMailboxModel,
                    MicrosoftMailboxModel.id == MicrosoftMailboxLeaseModel.mailbox_id,
                )
                .where(MicrosoftMailboxModel.status != "disabled")
                .where(MicrosoftMailboxLeaseModel.status == "reserved")
                .where(MicrosoftMailboxLeaseModel.expires_at > now)
            ).one()

        total_int = int(total or 0)
        capacity_int = int(capacity or 0)
        used_int = int(used or 0)
        reserved_int = int(reserved or 0)
        remaining = max(int(available or 0) - reserved_int, 0)
        exhaustion_rate = round((used_int / capacity_int) * 100, 1) if capacity_int > 0 else 0.0

        if total_int == 0 or remaining == 0:
            alert_level = "critical"
        elif remaining <= 10:
            alert_level = "warning"
        else:
            alert_level = "healthy"

        return {
            "total": total_int,
            "capacity": capacity_int,
            "used": used_int,
            "reserved": reserved_int,
            "remaining": remaining,
            "exhausted": int(exhausted or 0),
            "disabled": int(disabled or 0),
            "exhaustion_rate": exhaustion_rate,
            "alert_level": alert_level,
        }

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search: str = "",
    ) -> dict:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 50), 1), 200)
        with Session(engine) as session:
            query = select(MicrosoftMailboxModel)
            count_query = select(func.count(MicrosoftMailboxModel.id))
            if status:
                query = query.where(MicrosoftMailboxModel.status == status)
                count_query = count_query.where(MicrosoftMailboxModel.status == status)
            if search.strip():
                pattern = f"%{search.strip().lower()}%"
                predicate = or_(
                    MicrosoftMailboxModel.email_key.like(pattern),
                    MicrosoftMailboxModel.label.like(pattern),
                )
                query = query.where(predicate)
                count_query = count_query.where(predicate)
            total = int(session.exec(count_query).one() or 0)
            rows = session.exec(
                query.order_by(MicrosoftMailboxModel.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        return {
            "items": [
                {
                    "id": int(row.id or 0),
                    "email": row.email,
                    "use_count": int(row.use_count or 0),
                    "max_uses": int(row.max_uses or 6),
                    "status": row.status,
                    "source_format": row.source_format,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def apply_minimum_usage(self, usage_by_email: dict[str, int]) -> None:
        now = _utcnow()
        with Session(engine) as session:
            for email, count in usage_by_email.items():
                key = _email_key(email)
                if not key:
                    continue
                mailbox = session.exec(
                    select(MicrosoftMailboxModel).where(
                        MicrosoftMailboxModel.email_key == key
                    )
                ).first()
                if mailbox is None:
                    continue
                target = min(max(int(count or 0), 0), int(mailbox.max_uses or 1))
                occupied = {
                    int(value)
                    for value in session.exec(
                        select(MicrosoftMailboxLeaseModel.alias_index).where(
                            MicrosoftMailboxLeaseModel.mailbox_id == mailbox.id
                        )
                    ).all()
                }
                for alias_index in range(1, target + 1):
                    if alias_index in occupied:
                        continue
                    session.add(
                        MicrosoftMailboxLeaseModel(
                            mailbox_id=int(mailbox.id or 0),
                            alias_index=alias_index,
                            lease_token=f"legacy:{mailbox.id}:{alias_index}",
                            status="committed",
                            expires_at=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                mailbox.use_count = max(int(mailbox.use_count or 0), target)
                if mailbox.status != "disabled":
                    mailbox.status = (
                        "exhausted"
                        if mailbox.use_count >= int(mailbox.max_uses or 1)
                        else "available"
                    )
                mailbox.updated_at = now
                session.add(mailbox)
            session.commit()

def _parent_email_key(email: str) -> str:
    local_part, separator, domain = _email_key(email).rpartition("@")
    if not separator:
        return _email_key(email)
    return f"{local_part.split('+', 1)[0]}@{domain}"


def migrate_microsoft_mailbox_usage_leases() -> dict:
    """Reconcile committed usage leases from durable success history.

    Legacy versions incremented ``use_count`` before the browser was even
    started.  Rebuild those counts from durable successful-registration
    history, then use leases for all future attempts.  Reconciliation runs on
    every service start so a crash between saving an account and committing
    its lease is repaired in either direction.  Reserved leases are
    process-local work, so any that survived a restart are safe to release.
    """

    now = _utcnow()
    with Session(engine) as session:
        stale_reservations = session.exec(
            select(MicrosoftMailboxLeaseModel).where(
                MicrosoftMailboxLeaseModel.status == "reserved"
            )
        ).all()
        for lease in stale_reservations:
            session.delete(lease)

        successful_by_parent: Counter[str] = Counter()
        for email in session.exec(select(RegisteredEmailHistoryModel.email)).all():
            parent = _parent_email_key(email)
            if parent:
                successful_by_parent[parent] += 1
        mailboxes = session.exec(select(MicrosoftMailboxModel)).all()
        reclaimed = 0
        confirmed_total = 0
        for mailbox in mailboxes:
            old_count = max(int(mailbox.use_count or 0), 0)
            confirmed = min(
                successful_by_parent.get(_parent_email_key(mailbox.email), 0),
                max(int(mailbox.max_uses or 1), 1),
            )
            reclaimed += max(old_count - confirmed, 0)
            confirmed_total += confirmed

            leases = session.exec(
                select(MicrosoftMailboxLeaseModel).where(
                    MicrosoftMailboxLeaseModel.mailbox_id == mailbox.id
                )
            ).all()
            for lease in leases:
                session.delete(lease)
            session.flush()
            for alias_index in range(1, confirmed + 1):
                session.add(
                    MicrosoftMailboxLeaseModel(
                        mailbox_id=int(mailbox.id or 0),
                        alias_index=alias_index,
                        lease_token=f"reconciled:{mailbox.id}:{alias_index}",
                        status="committed",
                        expires_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            mailbox.use_count = confirmed
            mailbox.allocation_version = 1
            if mailbox.status != "disabled":
                mailbox.status = (
                    "exhausted"
                    if confirmed >= max(int(mailbox.max_uses or 1), 1)
                    else "available"
                )
            mailbox.updated_at = now
            session.add(mailbox)
        session.commit()

    return {
        "mailboxes": len(mailboxes),
        "used": confirmed_total,
        "reclaimed": reclaimed,
        "released_reservations": len(stale_reservations),
    }


def migrate_legacy_microsoft_mailbox_pool() -> dict:
    with Session(engine) as session:
        setting = session.exec(
            select(ProviderSettingModel)
            .where(ProviderSettingModel.provider_type == "mailbox")
            .where(ProviderSettingModel.provider_key == "local_ms_pool")
        ).first()
        if setting is None:
            return {"migrated": 0}
        config = setting.get_config()
        auth = setting.get_auth()

    pool_text = str(auth.get("local_ms_pool_text") or config.get("local_ms_pool_text") or "")
    pool_file = str(config.get("local_ms_pool_file") or auth.get("local_ms_pool_file") or "").strip()
    chunks = [pool_text] if pool_text.strip() else []
    if pool_file:
        path = Path(pool_file).expanduser()
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8-sig"))
    combined = "\n".join(chunks)
    if not combined.strip():
        return {"migrated": 0}

    from core.local_ms_mailbox import parse_local_ms_pool_rows

    entries = parse_local_ms_pool_rows(combined)
    if not entries:
        return {"migrated": 0}

    repository = MicrosoftMailboxRepository()
    result = repository.import_entries(entries, max_uses=6)

    state_path = Path(
        str(config.get("local_ms_pool_state_file") or "").strip()
        or Path(__file__).resolve().parent.parent / "data" / ".local_ms_mailbox_pool_state.json"
    )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {"used": {}}
    usage: Counter[str] = Counter()
    for key in (state.get("used") or {}).keys():
        normalized = str(key or "").strip().lower()
        parent = normalized.split("#sub-", 1)[0]
        if parent:
            usage[parent] += 1
    repository.apply_minimum_usage(dict(usage))

    with Session(engine) as session:
        setting = session.exec(
            select(ProviderSettingModel)
            .where(ProviderSettingModel.provider_type == "mailbox")
            .where(ProviderSettingModel.provider_key == "local_ms_pool")
        ).first()
        if setting is not None:
            config = setting.get_config()
            auth = setting.get_auth()
            for key in (
                "local_ms_pool_text",
                "local_ms_pool_file",
                "local_ms_pool_state_file",
                "local_ms_pool_allow_reuse",
            ):
                config.pop(key, None)
                auth.pop(key, None)
            metadata = setting.get_metadata()
            metadata["database_pool_migrated_at"] = _utcnow().isoformat()
            setting.set_config(config)
            setting.set_auth(auth)
            setting.set_metadata(metadata)
            setting.updated_at = _utcnow()
            session.add(setting)
            session.commit()

    return {"migrated": len(entries), **result, **repository.stats()}
