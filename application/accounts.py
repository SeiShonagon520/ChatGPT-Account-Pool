from __future__ import annotations

import ast
import csv
import json
import re

from core.datetime_utils import serialize_datetime
from domain.accounts import (
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountUpdateCommand,
)
from infrastructure.accounts_repository import AccountsRepository


IMPORT_LINE_RE = re.compile(
    r'^\s*(?P<email>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'\s+(?P<password>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'(?:\s+(?P<extra>.*))?\s*$'
)


def _decode_import_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
            return decoded if isinstance(decoded, str) else str(decoded)
        except Exception:
            return text[1:-1]
    return text


def _parse_csv_row(raw: str) -> list[str]:
    return next(csv.reader([raw]))


def _extract_jwt_exp(token: str) -> int | None:
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        import base64
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        data = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "page_size": query.page_size,
            "items": [self._serialize_list_item(item) for item in items],
        }

    def survival_stats(self, platform: str) -> dict:
        return self.repository.survival_stats(platform)

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

def parse_account_import_lines(
    lines: list[str],
    default_platform: str = "chatgpt",
) -> tuple[list[AccountImportLine], list[LocalMicrosoftMailboxEntry]]:
    from core.local_ms_mailbox import LocalMicrosoftMailboxEntry

    parsed: list[AccountImportLine] = []
    ms_entries: list[LocalMicrosoftMailboxEntry] = []
    csv_header: list[str] | None = None

    for raw in lines:
        line = (raw or "").strip().strip("\ufeff")
        if not line or line.startswith(("#", "//", "'")):
            continue

        # 1. JSON row
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    email = str(data.get("email") or data.get("username") or data.get("account") or "").strip()
                    if email and "@" in email:
                        pwd = str(data.get("password") or data.get("pwd") or "")
                        extra = dict(data.get("extra") or {})
                        for k in (
                            "credentials",
                            "overview",
                            "provider_accounts",
                            "cashier_url",
                            "totp_secret",
                            "refresh_token",
                            "access_token",
                        ):
                            if k in data and k not in extra:
                                extra[k] = data[k]
                        parsed.append(AccountImportLine(email=email, password=pwd, extra=extra))
                        continue
            except Exception:
                pass

        # 2. CSV header detection (if line has comma and not card separator '----')
        if "----" not in line and csv_header is None and "," in line:
            try:
                header_candidate = [item.strip().lower() for item in _parse_csv_row(line)]
            except Exception:
                header_candidate = []
            if "email" in header_candidate and "password" in header_candidate:
                csv_header = header_candidate
                continue

        # 3. CSV data row
        if csv_header is not None and "----" not in line and "," in line:
            try:
                values = _parse_csv_row(line)
            except Exception:
                values = []
            if values:
                row = {
                    csv_header[index]: values[index]
                    for index in range(min(len(csv_header), len(values)))
                }
                email = str(row.get("email", "") or "").strip()
                password = str(row.get("password", "") or "")
                if email and password and "@" in email and " " not in email:
                    extra = {
                        "overview": {
                            "platform": default_platform,
                            "refresh_token_status": "unknown",
                            "validity_status": "unknown",
                            "lifecycle_status": "registered",
                            "plan_state": "free",
                            "plan_name": "free",
                        }
                    }
                    cashier_url = str(row.get("cashier_url", "") or "").strip()
                    if cashier_url:
                        extra["cashier_url"] = cashier_url
                    parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                    continue

        # 4. Delimited line parsing (----, \t, ,, :, whitespace)
        parts: list[str] = []
        if "----" in line:
            parts = [p.strip() for p in line.split("----")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "，" in line:
            parts = [p.strip() for p in line.split("，")]
        elif "," in line:
            parts = [p.strip() for p in line.split(",")]
        elif ":" in line and not line.startswith("http"):
            parts = [p.strip() for p in line.split(":")]
        else:
            match = IMPORT_LINE_RE.match(line)
            if match:
                parts = [
                    _decode_import_token(match.group("email")),
                    _decode_import_token(match.group("password")),
                ]
                if match.group("extra"):
                    parts.append((match.group("extra") or "").strip())
            else:
                parts = [p.strip() for p in re.split(r"\s+", line) if p.strip()]

        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue

        first = _decode_import_token(parts[0])
        second = _decode_import_token(parts[1])
        if "@" in first and " " not in first:
            email = first
            password = second
        elif "@" in second and " " not in second:
            email = second
            password = first
            parts[0], parts[1] = email, password
        else:
            continue

        extra: dict = {
            "overview": {
                "platform": default_platform,
                "refresh_token_status": "unknown",
                "validity_status": "unknown",
                "lifecycle_status": "registered",
                "plan_state": "free",
                "plan_name": "free",
            },
            "credentials": {},
        }

        # Check for GuJumpgate / Microsoft Hotmail OAuth row (email----password----client_id----refresh_token)
        is_ms_oauth = False
        if len(parts) >= 4:
            c_id = parts[2].strip()
            r_token = parts[3].strip()
            if (len(c_id) >= 16 or "-" in c_id) and len(r_token) >= 20:
                is_ms_oauth = True
                recovery_email = parts[4].strip() if len(parts) > 4 and "@" in parts[4] else ""
                ms_entry = LocalMicrosoftMailboxEntry(
                    email=email,
                    password=password,
                    login_account=email,
                    client_id=c_id,
                    refresh_token=r_token,
                    recovery_email=recovery_email,
                    source_format="gujumpgate_hotmail",
                    raw=line,
                )
                ms_entries.append(ms_entry)
                extra["provider_accounts"] = [
                    {
                        "provider_type": "mailbox",
                        "provider_name": "local_ms_pool",
                        "login_identifier": email,
                        "display_name": email,
                        "credentials": {
                            "email": email,
                            "password": password,
                            "client_id": c_id,
                            "refresh_token": r_token,
                            "login_account": email,
                            "recovery_email": recovery_email,
                        },
                    }
                ]
                extra["credentials"]["auth_type"] = "passwordless"

        if not is_ms_oauth and len(parts) >= 3:
            third = parts[2].strip()
            if third.startswith("{") and third.endswith("}"):
                try:
                    decoded = json.loads(third)
                    if isinstance(decoded, dict):
                        extra.update(decoded)
                except Exception:
                    pass
            elif re.match(r"^[A-Za-z2-7]{16,64}$", third):
                extra["credentials"]["totp_secret"] = third.upper()
                extra["credentials"]["auth_type"] = "password"
            elif third.startswith("eyJ"):
                extra["credentials"]["access_token"] = third
            elif len(third) >= 40 and ("." in third or "-" in third or third.startswith("RT_")):
                extra["credentials"]["refresh_token"] = third
            elif third.startswith("http"):
                extra["cashier_url"] = third

        parsed.append(AccountImportLine(email=email, password=password, extra=extra))

    return parsed, ms_entries


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "page_size": query.page_size,
            "items": [self._serialize_list_item(item) for item in items],
        }

    def survival_stats(self, platform: str) -> dict:
        return self.repository.survival_stats(platform)

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

    def import_accounts(self, platform: str, lines: list[str]) -> dict:
        parsed, ms_entries = parse_account_import_lines(lines, default_platform=platform)
        mailboxes_saved = 0
        if ms_entries:
            try:
                from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository
                ms_repo = MicrosoftMailboxRepository()
                ms_res = ms_repo.import_entries(ms_entries, max_uses=6)
                mailboxes_saved = int(ms_res.get("inserted", 0) + ms_res.get("updated", 0))
            except Exception:
                pass

        result = self.repository.import_lines(platform, parsed)
        if isinstance(result, int):
            created = result
            updated = 0
            failed = 0
            unique = len(parsed)
        else:
            created = result.get("created", 0)
            updated = result.get("updated", 0)
            failed = result.get("failed", 0)
            unique = result.get("unique", len(parsed))

        return {
            "ok": True,
            "received": len(lines),
            "parsed": len(parsed),
            "unique": unique,
            "created": created,
            "updated": updated,
            "failed": failed,
            "mailboxes_saved": mailboxes_saved,
        }

    @staticmethod
    def _serialize_list_item(item: AccountRecord) -> dict:
        overview = dict(item.overview or {})
        has_refresh_token = any(
            credential.get("key") in {"refresh_token", "refreshToken"}
            and bool(str(credential.get("value") or "").strip())
            for credential in item.credentials
        )
        totp_secret = next(
            (
                str(credential.get("value") or "").strip()
                for credential in item.credentials
                if credential.get("key") == "totp_secret"
            ),
            "",
        )

        # Extract Access Token expiration
        at_expires_at = None
        for cred in item.credentials:
            if cred.get("key") in {"access_token", "accessToken"}:
                val = str(cred.get("value") or "").strip()
                at_expires_at = _extract_jwt_exp(val)
                if at_expires_at:
                    break
        if not at_expires_at and overview.get("at_expires_at"):
            try:
                at_expires_at = int(overview["at_expires_at"])
            except Exception:
                pass

        # Check mailbox pool binding
        has_mailbox = False
        mailbox_email = ""
        for pa in item.provider_accounts:
            p_name = str(pa.get("provider_name") or pa.get("provider") or "")
            p_type = str(pa.get("provider_type") or "")
            if p_name == "local_ms_pool" or p_type == "mailbox":
                has_mailbox = True
                mailbox_email = str(pa.get("login_identifier") or pa.get("display_name") or pa.get("email") or "")
                break

        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": item.password,
            "totp_secret": totp_secret,
            "refresh_token_status": str(overview.get("refresh_token_status") or "unknown"),
            "has_refresh_token": has_refresh_token,
            "at_expires_at": at_expires_at,
            "has_mailbox": has_mailbox,
            "mailbox_email": mailbox_email,
            "plan_name": item.plan_name or "free",
            "created_at": serialize_datetime(item.created_at),
        }

    @staticmethod
    def _serialize(item: AccountRecord) -> dict:
        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": item.password,
            "user_id": item.user_id,
            "primary_token": item.primary_token,
            "trial_end_time": item.trial_end_time,
            "cashier_url": item.cashier_url,
            "lifecycle_status": item.lifecycle_status,
            "validity_status": item.validity_status,
            "plan_state": item.plan_state,
            "plan_name": item.plan_name,
            "display_status": item.display_status,
            "overview": item.overview,
            "display_summary": item.display_summary,
            "credentials": item.credentials,
            "provider_accounts": item.provider_accounts,
            "provider_resources": item.provider_resources,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at),
        }
