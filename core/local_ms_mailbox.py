"""Local Microsoft mailbox pool provider.

The importer accepts GuJumpgate Hotmail rows (email/password/client_id/
refresh_token) and the older Xinlan/BH Mailer "common" account rows.
Microsoft accounts with Client Id + refresh token are read through Microsoft
Graph; rows without OAuth material fall back to IMAP only when inbound server
fields are present and usable.
"""

from __future__ import annotations

import csv
import email as email_lib
import imaplib
import re
import ssl
import time
from dataclasses import dataclass
from email.header import decode_header
from email.utils import getaddresses
from pathlib import Path

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
DEFAULT_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".local_ms_mailbox_pool_state.json"
MAX_OUTLOOK_SUBADDRESS_COUNT = 6


@dataclass(frozen=True)
class LocalMicrosoftMailboxEntry:
    email: str
    password: str = ""
    login_account: str = ""
    imap_host: str = ""
    imap_port: str = ""
    imap_account_type: str = ""
    imap_security: str = ""
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_security: str = ""
    note: str = ""
    proxy_mode: str = ""
    proxy: str = ""
    label: str = ""
    recovery_email: str = ""
    recovery_password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    totp_secret: str = ""
    source_format: str = "xinlan_common"
    raw: str = ""

    @property
    def key(self) -> str:
        return self.email.strip().lower()

    @property
    def graph_ready(self) -> bool:
        return bool(self.client_id and self.refresh_token)

    @property
    def imap_ready(self) -> bool:
        return bool(self.imap_host and (self.login_account or self.email) and self.password)

    def credentials(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "login_account": self.login_account,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_account_type": self.imap_account_type,
            "imap_security": self.imap_security,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "recovery_email": self.recovery_email,
            "recovery_password": self.recovery_password,
            "totp_secret": self.totp_secret,
        }


@dataclass(frozen=True)
class LocalMicrosoftMailboxCandidate:
    """A reservation from a parent Outlook mailbox, optionally via plus addressing."""

    entry: LocalMicrosoftMailboxEntry
    email: str
    key: str
    alias_index: int = 0


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _bounded_alias_count(value: object) -> int:
    try:
        return min(max(int(value or 1), 1), MAX_OUTLOOK_SUBADDRESS_COUNT)
    except (TypeError, ValueError):
        return 1


def _safe_text(value: object) -> str:
    return str(value or "").strip().strip("\ufeff")


def _csv_split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter, quotechar='"', skipinitialspace=True))
    except Exception:
        return line.split(delimiter)


def split_local_ms_pool_line(line: str) -> list[str]:
    text = str(line or "").strip().strip("\ufeff")
    if not text:
        return []
    if "----" in text:
        return [item.strip() for item in text.split("----")]
    if "\t" in text:
        return [item.strip() for item in text.split("\t")]
    if "，" in text:
        return [item.strip() for item in _csv_split(text, "，")]
    if "," in text:
        return [item.strip() for item in _csv_split(text, ",")]
    return [item.strip() for item in re.split(r"\s+", text) if item.strip()]


def split_xinlan_common_line(line: str) -> list[str]:
    return split_local_ms_pool_line(line)


def _is_gujumpgate_hotmail_header(parts: list[str]) -> bool:
    normalized = [str(part or "").strip().lower() for part in parts[:4]]
    if len(normalized) < 4:
        return False
    return (
        normalized[0] in {"account", "email", "mail", "账号", "郵箱", "邮箱"}
        and normalized[1] in {"password", "pass", "pwd", "密码", "密碼"}
        and normalized[2] in {"id", "clientid", "client_id", "client id"}
        and normalized[3] in {"token", "refreshtoken", "refresh_token", "refresh token"}
    )


def _looks_like_gujumpgate_hotmail_row(parts: list[str]) -> bool:
    # 卡密导出末尾可能多一个空的 ``----`` 分隔符（变成 5 列），去掉末尾
    # 空元素后按 4 列判断：邮箱 / 密码 / client_id / refresh_token。
    trimmed = [p for p in parts if _safe_text(p)]
    return (
        len(trimmed) == 4
        and "@" in _safe_text(parts[0])
        and bool(_safe_text(parts[2]))
        and bool(_safe_text(parts[3]))
    )


def parse_local_ms_pool_rows(text: str) -> list[LocalMicrosoftMailboxEntry]:
    entries: list[LocalMicrosoftMailboxEntry] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith("'"):
            continue
        parts = split_local_ms_pool_line(line)
        if not parts:
            continue
        if _is_gujumpgate_hotmail_header(parts):
            continue
        if _looks_like_gujumpgate_hotmail_row(parts):
            email = _safe_text(parts[0])
            entry = LocalMicrosoftMailboxEntry(
                email=email,
                password=_safe_text(parts[1]),
                login_account=email,
                client_id=_safe_text(parts[2]),
                refresh_token=_safe_text(parts[3]),
                source_format="gujumpgate_hotmail",
                raw=line,
            )
            if entry.key in seen:
                continue
            seen.add(entry.key)
            entries.append(entry)
            continue
        padded = parts + [""] * max(0, 19 - len(parts))
        email = _safe_text(padded[0])
        if "@" not in email:
            continue
        entry = LocalMicrosoftMailboxEntry(
            email=email,
            password=_safe_text(padded[1]),
            login_account=_safe_text(padded[2]) or email,
            imap_host=_safe_text(padded[3]),
            imap_port=_safe_text(padded[4]),
            imap_account_type=_safe_text(padded[5]),
            imap_security=_safe_text(padded[6]),
            smtp_host=_safe_text(padded[7]),
            smtp_port=_safe_text(padded[8]),
            smtp_security=_safe_text(padded[9]),
            note=_safe_text(padded[10]),
            proxy_mode=_safe_text(padded[11]),
            proxy=_safe_text(padded[12]),
            label=_safe_text(padded[13]),
            recovery_email=_safe_text(padded[14]),
            recovery_password=_safe_text(padded[15]),
            client_id=_safe_text(padded[16]),
            refresh_token=_safe_text(padded[17]),
            totp_secret=_safe_text(padded[18]),
            source_format="xinlan_common",
            raw=line,
        )
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def parse_xinlan_common_rows(text: str) -> list[LocalMicrosoftMailboxEntry]:
    return parse_local_ms_pool_rows(text)


class LocalMicrosoftMailboxPool(BaseMailbox):
    """Use existing Outlook/Hotmail/Live accounts from a local text pool."""

    def __init__(
        self,
        *,
        pool_text: str = "",
        pool_file: str = "",
        state_file: str = "",
        graph_scope: str = "",
        allow_reuse: bool = False,
        alias_count: int = 6,
        proxy: str = None,
    ):
        self.pool_text = str(pool_text or "")
        self.pool_file = str(pool_file or "").strip()
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.graph_scope = str(graph_scope or DEFAULT_GRAPH_SCOPE).strip()
        self.allow_reuse = bool(allow_reuse)
        self.alias_count = _bounded_alias_count(alias_count)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._legacy_source_imported = False

    @classmethod
    def from_config(cls, config: dict) -> "LocalMicrosoftMailboxPool":
        return cls(
            pool_text=config.get("local_ms_pool_text", ""),
            pool_file=config.get("local_ms_pool_file", ""),
            state_file=config.get("local_ms_pool_state_file", ""),
            graph_scope=config.get("local_ms_graph_scope", ""),
            allow_reuse=_truthy(config.get("local_ms_pool_allow_reuse")),
            alias_count=config.get("local_ms_pool_alias_count", 6),
            proxy=config.get("proxy") or None,
        )

    def _load_pool_text(self) -> str:
        chunks: list[str] = []
        if self.pool_text.strip():
            chunks.append(self.pool_text)
        if self.pool_file:
            path = Path(self.pool_file).expanduser()
            if not path.exists():
                raise RuntimeError(f"本地微软邮箱池文件不存在: {path}")
            chunks.append(path.read_text(encoding="utf-8-sig"))
        combined = "\n".join(chunks)
        if not combined.strip():
            raise RuntimeError("本地微软邮箱池为空，请粘贴 Hotmail 四列格式或配置文件路径")
        return combined

    def _entries(self) -> list[LocalMicrosoftMailboxEntry]:
        entries = parse_local_ms_pool_rows(self._load_pool_text())
        if not entries:
            raise RuntimeError("本地微软邮箱池未解析到有效邮箱")
        return entries

    def _candidate_email(self, entry: LocalMicrosoftMailboxEntry) -> str:
        # Every registration gets its own random sub-address (local+tag@domain)
        # that delivers into the parent inbox.  The bare parent address is
        # never handed out: a parent inbox receives every sibling's mail, so a
        # worker on the parent reads another (or a stale) OTP and fails with
        # wrong_email_otp_code.  The tag is a random string (<= 6 chars) instead
        # of a predictable regN sequence, which OpenAI flags across siblings.
        local_part, separator, domain = entry.email.strip().rpartition("@")
        if not separator or not local_part or not domain:
            raise RuntimeError(f"Outlook 邮箱格式无效，无法生成子邮箱: {entry.email}")
        import random
        import string

        parent_local_part = local_part.split("+", 1)[0]
        tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{parent_local_part}+{tag}@{domain}"

    @staticmethod
    def _entry_from_record(record) -> LocalMicrosoftMailboxEntry:
        return LocalMicrosoftMailboxEntry(
            email=record.email,
            password=record.password,
            login_account=record.login_account,
            imap_host=record.imap_host,
            imap_port=record.imap_port,
            imap_account_type=record.imap_account_type,
            imap_security=record.imap_security,
            smtp_host=record.smtp_host,
            smtp_port=record.smtp_port,
            smtp_security=record.smtp_security,
            note=record.note,
            proxy_mode=record.proxy_mode,
            proxy=record.proxy,
            label=record.label,
            recovery_email=record.recovery_email,
            recovery_password=record.recovery_password,
            client_id=record.client_id,
            refresh_token=record.refresh_token,
            totp_secret=record.totp_secret,
            source_format=record.source_format,
        )

    def _repository(self):
        from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository

        return MicrosoftMailboxRepository()

    def _ensure_legacy_source_imported(self) -> None:
        if self._legacy_source_imported:
            return
        self._legacy_source_imported = True
        if not self.pool_text.strip() and not self.pool_file:
            return
        self._repository().import_entries(self._entries(), max_uses=self.alias_count)

    def _candidate_from_record(self, record, *, reserved: bool) -> LocalMicrosoftMailboxCandidate:
        entry = self._entry_from_record(record)
        alias_index = int(getattr(record, "alias_index", 0) or 0)
        if not alias_index:
            alias_index = int(record.use_count or 0)
            if not reserved and not self.allow_reuse:
                alias_index += 1
        if self.allow_reuse:
            alias_index = 1
        alias_index = min(max(alias_index, 1), int(record.max_uses or self.alias_count))
        email = self._candidate_email(entry)
        key = entry.key if int(record.max_uses or 1) <= 1 else f"{entry.key}#sub-{alias_index}"
        return LocalMicrosoftMailboxCandidate(
            entry=entry,
            email=email,
            key=key,
            alias_index=alias_index if int(record.max_uses or 1) > 1 else 0,
        )

    def peek_email(self) -> str:
        self._ensure_legacy_source_imported()
        record = self._repository().peek()
        return self._candidate_from_record(record, reserved=False).email

    def get_email(self) -> MailboxAccount:
        self._ensure_legacy_source_imported()
        record = self._repository().reserve(allow_reuse=self.allow_reuse)
        try:
            candidate = self._candidate_from_record(record, reserved=True)
        except BaseException:
            if getattr(record, "lease_token", ""):
                self._repository().release(record.lease_token)
            raise

        entry = candidate.entry
        credentials = entry.credentials()
        credentials = {key: value for key, value in credentials.items() if value}
        return MailboxAccount(
            email=candidate.email,
            account_id=candidate.key,
            extra={
                "_mailbox_lease_token": str(getattr(record, "lease_token", "") or ""),
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "local_ms_pool",
                    "login_identifier": entry.login_account or entry.email,
                    "display_name": candidate.email,
                    "credentials": credentials,
                    "metadata": {
                        "source": entry.source_format,
                        "source_format": entry.source_format,
                        "has_graph_refresh_token": bool(entry.graph_ready),
                        "has_imap_config": bool(entry.imap_ready),
                        "parent_email": entry.email,
                        "alias_index": candidate.alias_index,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "local_ms_pool",
                    "resource_type": "mailbox",
                    "resource_identifier": candidate.key,
                    "handle": candidate.email,
                    "display_name": candidate.email,
                    "metadata": {
                        "email": candidate.email,
                        "parent_email": entry.email,
                        "alias_index": candidate.alias_index,
                        "source": entry.source_format,
                        "reserved": not self.allow_reuse,
                    },
                },
            },
        )

    @staticmethod
    def _lease_token(account: MailboxAccount) -> str:
        return str((getattr(account, "extra", {}) or {}).get("_mailbox_lease_token") or "")

    def commit_email(self, account: MailboxAccount) -> bool:
        token = self._lease_token(account)
        if not token:
            return False
        committed = self._repository().commit(token)
        if committed:
            account.extra = dict(account.extra or {})
            account.extra.pop("_mailbox_lease_token", None)
            resource = account.extra.get("provider_resource")
            if isinstance(resource, dict):
                metadata = dict(resource.get("metadata") or {})
                metadata["reserved"] = False
                resource["metadata"] = metadata
        return committed

    def release_email(self, account: MailboxAccount) -> bool:
        token = self._lease_token(account)
        if not token:
            return False
        released = self._repository().release(token)
        account.extra = dict(account.extra or {})
        account.extra.pop("_mailbox_lease_token", None)
        return released

    def _entry_for_account(self, account: MailboxAccount) -> LocalMicrosoftMailboxEntry:
        account_email = str(getattr(account, "email", "") or "").strip().lower()
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        metadata = dict(provider_account.get("metadata") or {})
        if credentials:
            return LocalMicrosoftMailboxEntry(
                email=str(credentials.get("email") or account.email or ""),
                password=str(credentials.get("password") or ""),
                login_account=str(credentials.get("login_account") or account.email or ""),
                imap_host=str(credentials.get("imap_host") or ""),
                imap_port=str(credentials.get("imap_port") or ""),
                imap_account_type=str(credentials.get("imap_account_type") or ""),
                imap_security=str(credentials.get("imap_security") or ""),
                client_id=str(credentials.get("client_id") or ""),
                refresh_token=str(credentials.get("refresh_token") or ""),
                recovery_email=str(credentials.get("recovery_email") or ""),
                recovery_password=str(credentials.get("recovery_password") or ""),
                totp_secret=str(credentials.get("totp_secret") or ""),
                source_format=str(metadata.get("source") or metadata.get("source_format") or ""),
            )

        account_parent_key = self._parent_email_key(account_email)
        record = self._repository().get_by_parent_email(account_parent_key)
        if record is not None:
            return self._entry_from_record(record)
        for entry in self._entries():
            if entry.key == account_email or self._parent_email_key(entry.email) == account_parent_key:
                return entry
        raise RuntimeError(f"本地微软邮箱池未找到账号: {getattr(account, 'email', '')}")

    @staticmethod
    def _parent_email_key(email: str) -> str:
        local_part, separator, domain = str(email or "").strip().lower().rpartition("@")
        if not separator:
            return str(email or "").strip().lower()
        return f"{local_part.split('+', 1)[0]}@{domain}"

    @staticmethod
    def _decode_mime(value: str) -> str:
        parts = []
        for raw, charset in decode_header(value or ""):
            if isinstance(raw, bytes):
                parts.append(raw.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(str(raw or ""))
        return "".join(parts)

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(mail.get("id") or mail.get("internetMessageId") or mail.get("receivedDateTime") or "")

    @staticmethod
    def _message_text(mail: dict) -> str:
        body = mail.get("body") or {}
        return " ".join(
            str(value or "")
            for value in (
                mail.get("subject"),
                mail.get("bodyPreview"),
                body.get("content") if isinstance(body, dict) else "",
            )
        )

    @staticmethod
    def _message_recipients(mail: dict) -> set[str]:
        recipients: set[str] = set()
        raw_recipients = mail.get("toRecipients")
        if isinstance(raw_recipients, list):
            for recipient in raw_recipients:
                if not isinstance(recipient, dict):
                    continue
                address = recipient.get("emailAddress")
                if isinstance(address, dict):
                    value = str(address.get("address") or "").strip().lower()
                    if value:
                        recipients.add(value)
        raw_to = str(mail.get("to") or "").strip()
        if raw_to:
            recipients.update(
                address.strip().lower()
                for _, address in getaddresses([raw_to])
                if address.strip()
            )
        return recipients

    @classmethod
    def _message_is_for_account(cls, mail: dict, account: MailboxAccount) -> bool:
        target = str(getattr(account, "email", "") or "").strip().lower()
        # Parent mailboxes are not shared.  Child addresses are, so only accept
        # a message explicitly sent to that child and never steal another
        # concurrent registration's OTP.
        if not target or "+" not in target.partition("@")[0]:
            return True
        recipients = cls._message_recipients(mail)
        return target in recipients

    def _graph_access_token(self, entry: LocalMicrosoftMailboxEntry) -> str:
        if not entry.graph_ready:
            raise RuntimeError(f"微软邮箱缺少 Client Id 或刷新令牌: {entry.email}")
        errors: list[str] = []
        invalid_grant = False
        strategies = [
            ("entra-consumers-default", GRAPH_CONSUMERS_TOKEN_URL, {"scope": "https://graph.microsoft.com/.default offline_access"}),
            ("entra-common-default", GRAPH_TOKEN_URL, {"scope": "https://graph.microsoft.com/.default offline_access"}),
            ("entra-consumers-delegated", GRAPH_CONSUMERS_TOKEN_URL, {"scope": self.graph_scope}),
            ("entra-common-delegated", GRAPH_TOKEN_URL, {"scope": self.graph_scope}),
        ]
        for name, url, extra_data in strategies:
            data = {
                "client_id": entry.client_id,
                "grant_type": "refresh_token",
                "refresh_token": entry.refresh_token,
            }
            data.update({key: value for key, value in extra_data.items() if value})
            try:
                response = requests.post(
                    url,
                    data=data,
                    proxies=self.proxy,
                    timeout=25,
                )
            except Exception as exc:
                errors.append(f"{name}: request failed: {str(exc)[:200]}")
                continue
            if response.status_code != 200:
                try:
                    error_payload = response.json() or {}
                except Exception:
                    error_payload = {}
                if "default" in name and str(error_payload.get("error") or "").strip().lower() == "invalid_grant":
                    invalid_grant = True
                errors.append(f"{name}: HTTP {response.status_code} {response.text[:200]}")
                continue
            payload = response.json() or {}
            token = str(payload.get("access_token") or "").strip()
            if token:
                return token
            errors.append(f"{name}: missing access_token")
        details = " | ".join(errors) if errors else "no token strategies attempted"
        if invalid_grant:
            # invalid_grant is terminal for this refresh token.  Leaving the
            # row available burns another alias and repeats the same failure
            # in every future task, so remove the parent mailbox immediately.
            try:
                self._repository().disable(entry.email)
            except Exception:
                pass
            raise RuntimeError(
                f"Microsoft refresh_token 已失效，邮箱已禁用: {entry.email}; {details}"
            )
        raise RuntimeError(f"Microsoft refresh_token 换 access_token 失败: {details}")

    def _graph_messages(self, entry: LocalMicrosoftMailboxEntry, *, limit: int = 25) -> list[dict]:
        token = self._graph_access_token(entry)
        response = requests.get(
            GRAPH_MESSAGES_URL,
            headers={"authorization": f"Bearer {token}", "accept": "application/json"},
            params={
                "$top": str(max(int(limit or 25), 1)),
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,bodyPreview,receivedDateTime,from,toRecipients,body",
            },
            proxies=self.proxy,
            timeout=25,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Microsoft Graph 读取邮件失败: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json() or {}
        return list(payload.get("value") or [])

    def _imap_connect(self, entry: LocalMicrosoftMailboxEntry):
        host = entry.imap_host.strip()
        port = int(entry.imap_port or 993)
        security = entry.imap_security.lower()
        if port == 993 or "ssl" in security:
            return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        conn = imaplib.IMAP4(host, port)
        if "tls" in security:
            conn.starttls(ssl_context=ssl.create_default_context())
        return conn

    def _imap_messages(self, entry: LocalMicrosoftMailboxEntry, *, limit: int = 25) -> list[dict]:
        if not entry.imap_ready:
            raise RuntimeError(f"微软邮箱没有可用的 Graph token，也没有 IMAP 收件配置: {entry.email}")
        conn = self._imap_connect(entry)
        messages: list[dict] = []
        try:
            conn.login(entry.login_account or entry.email, entry.password)
            conn.select("INBOX", readonly=True)
            _, msg_nums = conn.search(None, "ALL")
            ids = msg_nums[0].split() if msg_nums and msg_nums[0] else []
            for mid in reversed(ids[-max(int(limit or 25), 1):]):
                _, data = conn.fetch(mid, "(RFC822)")
                if not data or not data[0]:
                    continue
                msg = email_lib.message_from_bytes(data[0][1])
                subject = self._decode_mime(str(msg.get("Subject", "") or ""))
                parts: list[str] = []
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() not in ("text/plain", "text/html"):
                            continue
                        payload = part.get_payload(decode=True)
                        if payload:
                            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
                messages.append({
                    "id": str(msg.get("Message-ID") or mid.decode("ascii", errors="ignore")),
                    "subject": subject,
                    "bodyPreview": " ".join(parts),
                    "to": str(msg.get("To", "") or ""),
                })
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return messages

    def _messages(self, account: MailboxAccount) -> list[dict]:
        entry = self._entry_for_account(account)
        if entry.graph_ready:
            return self._graph_messages(entry)
        return self._imap_messages(entry)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                self._message_id(mail)
                for mail in self._messages(account)
                if self._message_is_for_account(mail, account) and self._message_id(mail)
            }
        except Exception:
            return set()

    @staticmethod
    def _clean_search_text(text: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", cleaned)
        return cleaned

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        seen = set(before_ids or [])
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        start = time.time()
        while time.time() - start < timeout:
            for mail in self._messages(account):
                if not self._message_is_for_account(mail, account):
                    continue
                mid = self._message_id(mail)
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                text = self._clean_search_text(self._message_text(mail))
                if keyword and keyword.lower() not in text.lower():
                    continue
                match = pattern.search(text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            time.sleep(5)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            for mail in self._messages(account):
                if not self._message_is_for_account(mail, account):
                    continue
                mid = self._message_id(mail)
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                link = _extract_verification_link(self._message_text(mail), keyword)
                if link:
                    return link
            time.sleep(5)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")
