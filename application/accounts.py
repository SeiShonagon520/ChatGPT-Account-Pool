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


def _extract_jwt_email_and_exp(token: str) -> tuple[str, int | None]:
    if not token or not isinstance(token, str):
        return "", None
    parts = token.split(".")
    if len(parts) < 2:
        return "", None
    try:
        import base64
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        data = json.loads(base64.urlsafe_b64decode(payload_b64))
        if not isinstance(data, dict):
            return "", None
        auth_profile = data.get("https://api.openai.com/profile") or {}
        email = ""
        if isinstance(auth_profile, dict):
            email = str(auth_profile.get("email") or "").strip()
        if not email:
            email = str(data.get("email") or "").strip()
        exp = data.get("exp")
        exp_int = int(exp) if isinstance(exp, (int, float)) else None
        return email, exp_int
    except Exception:
        return "", None


def _parse_json_account(
    data: Any,
    default_platform: str = "chatgpt",
) -> tuple[AccountImportLine | None, LocalMicrosoftMailboxEntry | None]:
    from core.local_ms_mailbox import LocalMicrosoftMailboxEntry

    if not isinstance(data, dict):
        return None, None

    agent_identity = None
    if data.get("auth_mode") == "agentIdentity" or isinstance(data.get("agent_identity"), dict):
        agent_identity = data.get("agent_identity") if isinstance(data.get("agent_identity"), dict) else data

    creds = dict(data.get("credentials") or {})

    # Extract email from multiple possible keys (Sub2API uses "name" for account email)
    candidates = [
        data.get("email"),
        data.get("username"),
        data.get("account"),
        data.get("name"),
        data.get("user"),
        data.get("mail"),
        creds.get("email"),
        creds.get("name"),
    ]
    if agent_identity:
        candidates.append(agent_identity.get("email"))

    email = ""
    for cand in candidates:
        cand_str = str(cand or "").strip()
        if cand_str and "@" in cand_str and " " not in cand_str:
            email = cand_str
            break

    # If email still missing, try decoding JWT access_token or id_token
    jwt_tokens = [
        creds.get("access_token") or creds.get("accessToken"),
        creds.get("id_token") or creds.get("idToken"),
        data.get("access_token") or data.get("accessToken"),
        data.get("id_token") or data.get("idToken"),
    ]
    at_exp = None
    for token in jwt_tokens:
        if token and isinstance(token, str):
            jwt_email, exp = _extract_jwt_email_and_exp(token)
            if not at_exp and exp:
                at_exp = exp
            if not email and jwt_email:
                email = jwt_email

    if not email:
        return None, None

    pwd = str(data.get("password") or data.get("pwd") or data.get("pass") or creds.get("password") or "")
    extra = dict(data.get("extra") or {})

    platform = str(data.get("platform") or creds.get("platform") or default_platform).strip().lower()
    if platform in ("openai", "chatgpt"):
        platform = "chatgpt"

    merged_creds = dict(extra.get("credentials") or {})
    merged_creds.update(creds)

    # 2FA / TOTP detection
    totp = (
        data.get("totp_secret")
        or data.get("2fa")
        or data.get("totp")
        or data.get("secret")
        or creds.get("totp_secret")
        or creds.get("2fa")
        or creds.get("totp")
    )
    if totp:
        totp_clean = str(totp).strip()
        extra["totp_secret"] = totp_clean
        merged_creds["totp_secret"] = totp_clean
        merged_creds.setdefault("auth_type", "password")

    # Tokens and IDs
    for src in (data, creds, agent_identity or {}):
        for k_src, k_dst in (
            ("access_token", "access_token"),
            ("accessToken", "access_token"),
            ("refresh_token", "refresh_token"),
            ("refreshToken", "refresh_token"),
            ("id_token", "id_token"),
            ("idToken", "id_token"),
            ("session_token", "session_token"),
            ("sessionToken", "session_token"),
            ("chatgpt_account_id", "chatgpt_account_id"),
            ("account_id", "account_id"),
            ("accountId", "account_id"),
            ("chatgpt_user_id", "chatgpt_user_id"),
            ("user_id", "user_id"),
            ("client_id", "client_id"),
            ("clientId", "client_id"),
            ("organization_id", "workspace_id"),
            ("workspace_id", "workspace_id"),
            ("phone", "phone"),
            ("mobile", "phone"),
            ("phone_number", "phone"),
            ("cashier_url", "cashier_url"),
            ("mailbox_url", "cashier_url"),
            ("mail_url", "cashier_url"),
            ("query_url", "cashier_url"),
            ("email_query_url", "cashier_url"),
            ("note", "note"),
            ("remarks", "note"),
            ("oai_did", "oai_did"),
            ("cookies", "cookies"),
            ("cookie", "cookies"),
            ("model_mapping", "model_mapping"),
            ("share_token", "share_token"),
            ("api_key", "api_key"),
        ):
            if k_src in src and src[k_src] not in (None, ""):
                val = src[k_src]
                if k_dst not in extra:
                    extra[k_dst] = val
                if k_dst not in merged_creds:
                    merged_creds[k_dst] = val

    if at_exp and "at_expires_at" not in extra:
        extra["at_expires_at"] = at_exp

    if agent_identity:
        extra["agent_identity"] = agent_identity
        merged_creds["agent_identity"] = agent_identity
        if "account_id" in agent_identity and "account_id" not in extra:
            extra["account_id"] = agent_identity["account_id"]

    extra["credentials"] = merged_creds

    if "overview" not in extra:
        extra["overview"] = {
            "platform": platform,
            "refresh_token_status": "valid" if extra.get("refresh_token") else "unknown",
            "validity_status": "valid" if extra.get("access_token") else "unknown",
            "lifecycle_status": "registered",
            "plan_state": "free",
            "plan_name": "free",
        }
        if at_exp:
            extra["overview"]["at_expires_at"] = at_exp

    # Check for Microsoft Mailbox OAuth in JSON
    ms_entry = None
    c_id = str(extra.get("client_id") or merged_creds.get("client_id") or "").strip()
    r_token = str(extra.get("refresh_token") or merged_creds.get("refresh_token") or "").strip()
    if (len(c_id) >= 16 or "-" in c_id) and len(r_token) >= 20 and (
        "outlook" in email.lower() or "hotmail" in email.lower() or "live." in email.lower() or "msn." in email.lower() or data.get("provider_accounts")
    ):
        recovery_email = str(data.get("recovery_email") or creds.get("recovery_email") or "").strip()
        ms_entry = LocalMicrosoftMailboxEntry(
            email=email,
            password=pwd,
            login_account=email,
            client_id=c_id,
            refresh_token=r_token,
            recovery_email=recovery_email,
            source_format="json_ms_oauth",
            raw=json.dumps(data, ensure_ascii=False),
        )
        extra["provider_accounts"] = [
            {
                "provider_type": "mailbox",
                "provider_name": "local_ms_pool",
                "login_identifier": email,
                "display_name": email,
                "credentials": {
                    "email": email,
                    "password": pwd,
                    "client_id": c_id,
                    "refresh_token": r_token,
                    "login_account": email,
                    "recovery_email": recovery_email,
                },
            }
        ]
        extra["credentials"]["auth_type"] = "passwordless"

    return AccountImportLine(email=email, password=pwd, extra=extra), ms_entry


def _extract_items_from_json_root(root_data: Any) -> list[dict]:
    """Unpack various JSON structures (Sub2API bundles, Any2API, dict mapping, envelopes)."""
    if isinstance(root_data, list):
        return [item for item in root_data if isinstance(item, dict)]
    if not isinstance(root_data, dict):
        return []

    # 1. Sub2API bundle format: {"proxies": [...], "accounts": [...]}
    if isinstance(root_data.get("accounts"), list):
        return [item for item in root_data["accounts"] if isinstance(item, dict)]

    # 2. Standard envelope formats: {"data": [...]}, {"items": [...]}, {"list": [...]}, {"results": [...]}
    for key in ("data", "items", "list", "results"):
        if isinstance(root_data.get(key), list):
            return [item for item in root_data[key] if isinstance(item, dict)]

    # 3. Any2API admin.json format: {"providers": {"chatgptConfig": ..., "kiroAccounts": ...}}
    if isinstance(root_data.get("providers"), dict):
        providers = root_data["providers"]
        items = []
        if isinstance(providers.get("chatgptConfig"), dict):
            c_cfg = providers["chatgptConfig"]
            if c_cfg.get("token"):
                items.append({"access_token": c_cfg["token"], "platform": "chatgpt"})
        if isinstance(providers.get("kiroAccounts"), list):
            items.extend([a for a in providers["kiroAccounts"] if isinstance(a, dict)])
        if isinstance(providers.get("grokTokens"), list):
            items.extend([g for g in providers["grokTokens"] if isinstance(g, dict)])
        if items:
            return items

    # 4. Dict mapping: {"user1@email.com": {...}, "user2@email.com": {...}}
    is_dict_map = True
    dict_items = []
    for k, v in root_data.items():
        if "@" in str(k) and isinstance(v, dict):
            entry = dict(v)
            entry.setdefault("email", k)
            dict_items.append(entry)
        else:
            is_dict_map = False
            break
    if is_dict_map and dict_items:
        return dict_items

    # 5. Single account object
    return [root_data]


def parse_account_import_lines(
    lines: list[str],
    default_platform: str = "chatgpt",
) -> tuple[list[AccountImportLine], list[LocalMicrosoftMailboxEntry]]:
    import base64
    from core.local_ms_mailbox import LocalMicrosoftMailboxEntry

    parsed: list[AccountImportLine] = []
    ms_entries: list[LocalMicrosoftMailboxEntry] = []
    csv_header: list[str] | None = None

    full_text = "\n".join(lines).strip().strip("\ufeff")

    # Step 0: Check for subscription URL (e.g. sub://... or https://.../sub... or http://...)
    if full_text.startswith("sub://"):
        try:
            b64_sub = full_text[6:].strip()
            padding = 4 - len(b64_sub) % 4
            if padding != 4:
                b64_sub += "=" * padding
            decoded_text = base64.b64decode(b64_sub).decode("utf-8", errors="ignore").strip()
            if decoded_text:
                return parse_account_import_lines(decoded_text.splitlines(), default_platform=default_platform)
        except Exception:
            pass
    elif len(lines) == 1 and (full_text.startswith("http://") or full_text.startswith("https://")) and " " not in full_text:
        try:
            import requests
            resp = requests.get(full_text, timeout=10, headers={"User-Agent": "FreeGPT-Manager/1.0"})
            if resp.status_code == 200 and resp.text.strip():
                return parse_account_import_lines(resp.text.splitlines(), default_platform=default_platform)
        except Exception:
            pass

    # Step 1: Check if full_text is a Base64 encoded string (common for .sub files)
    if (
        len(full_text) >= 16
        and not full_text.startswith("{")
        and not full_text.startswith("[")
        and "----" not in full_text
        and "|" not in full_text
        and "." not in full_text
    ):
        try:
            compact = "".join(full_text.split())
            padding = 4 - len(compact) % 4
            if padding != 4:
                compact += "=" * padding
            decoded = base64.b64decode(compact).decode("utf-8", errors="ignore").strip()
            if (decoded.startswith("{") or decoded.startswith("[")) or ("@" in decoded and ("----" in decoded or "|" in decoded or "\n" in decoded)):
                return parse_account_import_lines(decoded.splitlines(), default_platform=default_platform)
        except Exception:
            pass

    # Step 2: Check if entire input is JSON (array, Sub2API bundle, object, Any2API)
    if (full_text.startswith("[") and full_text.endswith("]")) or (full_text.startswith("{") and full_text.endswith("}")):
        try:
            root_data = json.loads(full_text)
            items = _extract_items_from_json_root(root_data)
            json_parsed: list[AccountImportLine] = []
            json_ms: list[LocalMicrosoftMailboxEntry] = []
            for item in items:
                parsed_item, ms_entry = _parse_json_account(item, default_platform)
                if parsed_item:
                    json_parsed.append(parsed_item)
                if ms_entry:
                    json_ms.append(ms_entry)
            if json_parsed:
                return json_parsed, json_ms
        except Exception:
            pass

    # Step 3: Process line-by-line
    for raw in lines:
        line = (raw or "").strip().strip("\ufeff")
        if not line or line.startswith(("#", "//", "'")):
            continue

        # 3.1 JSON line
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                items = _extract_items_from_json_root(data)
                for item in items:
                    parsed_item, ms_entry = _parse_json_account(item, default_platform)
                    if parsed_item:
                        parsed.append(parsed_item)
                    if ms_entry:
                        ms_entries.append(ms_entry)
                continue
            except Exception:
                pass

        # 3.2 CSV header detection
        if "----" not in line and "|" not in line and csv_header is None and "," in line:
            try:
                header_candidate = [item.strip().lower() for item in _parse_csv_row(line)]
            except Exception:
                header_candidate = []
            if "email" in header_candidate and ("password" in header_candidate or "access_token" in header_candidate):
                csv_header = header_candidate
                continue

        # 3.3 CSV data row
        if csv_header is not None and "----" not in line and "|" not in line and "," in line:
            try:
                values = _parse_csv_row(line)
            except Exception:
                values = []
            if values:
                row = {
                    csv_header[index]: values[index]
                    for index in range(min(len(csv_header), len(values)))
                }
                parsed_item, ms_entry = _parse_json_account(row, default_platform)
                if parsed_item:
                    parsed.append(parsed_item)
                if ms_entry:
                    ms_entries.append(ms_entry)
                continue

        # 3.4 Standalone JWT Access Token (auto-extract email from payload)
        if line.startswith("eyJ") and line.count(".") == 2 and " " not in line and "----" not in line and "|" not in line:
            email, exp = _extract_jwt_email_and_exp(line)
            if email:
                extra = {
                    "overview": {
                        "platform": default_platform,
                        "refresh_token_status": "unknown",
                        "validity_status": "valid",
                        "lifecycle_status": "registered",
                        "plan_state": "free",
                        "plan_name": "free",
                    },
                    "credentials": {
                        "access_token": line,
                    },
                    "access_token": line,
                }
                if exp:
                    extra["at_expires_at"] = exp
                    extra["overview"]["at_expires_at"] = exp
                parsed.append(AccountImportLine(email=email, password="", extra=extra))
                continue

        # 3.5 Delimited line parsing (----, ---, ||, |, \t, ，, ,, :, ;)
        parts: list[str] = []
        if "----" in line:
            parts = [p.strip() for p in line.split("----")]
        elif "---" in line:
            parts = [p.strip() for p in line.split("---")]
        elif "||" in line:
            parts = [p.strip() for p in line.split("||")]
        elif "|" in line:
            parts = [p.strip() for p in line.split("|")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "，" in line:
            parts = [p.strip() for p in line.split("，")]
        elif "," in line:
            parts = [p.strip() for p in line.split(",")]
        elif ";" in line:
            parts = [p.strip() for p in line.split(";")]
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
        email = ""
        password = ""

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

        # Check if second part is actually a token instead of password
        if password.startswith("eyJ") and password.count(".") == 2:
            extra["credentials"]["access_token"] = password
            extra["access_token"] = password
            _, exp = _extract_jwt_email_and_exp(password)
            if exp:
                extra["at_expires_at"] = exp
            password = ""
        elif (password.startswith("rt.") or password.startswith("RT_") or (len(password) >= 40 and "-" in password)):
            extra["credentials"]["refresh_token"] = password
            extra["refresh_token"] = password
            password = ""

        # Check for GuJumpgate / Microsoft Hotmail OAuth row (email----password----client_id----refresh_token)
        is_ms_oauth = False
        if len(parts) >= 4:
            c_id = parts[2].strip()
            r_token = parts[3].strip()
            if (
                not c_id.startswith("http")
                and not r_token.startswith("http")
                and ("-" in c_id or len(c_id) >= 30)
                and len(r_token) >= 30
                and not re.match(r"^[A-Za-z2-7]{16,32}$", c_id)
            ):
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
            for extra_part in parts[2:]:
                p = extra_part.strip()
                if not p:
                    continue
                if p.startswith("{") and p.endswith("}"):
                    try:
                        decoded = json.loads(p)
                        if isinstance(decoded, dict):
                            extra.update(decoded)
                    except Exception:
                        pass
                elif p.startswith("http://") or p.startswith("https://"):
                    extra["cashier_url"] = p
                    extra["credentials"]["cashier_url"] = p
                elif p.startswith("eyJ") and p.count(".") == 2:
                    extra["credentials"]["access_token"] = p
                    extra["access_token"] = p
                    _, exp = _extract_jwt_email_and_exp(p)
                    if exp:
                        extra["at_expires_at"] = exp
                elif p.startswith("rt.") or p.startswith("RT_") or (len(p) >= 40 and ("." in p or "-" in p)):
                    extra["credentials"]["refresh_token"] = p
                    extra["refresh_token"] = p
                elif re.match(r"^[A-Za-z2-7]{16,64}$", p):
                    extra["credentials"]["totp_secret"] = p.upper()
                    extra["credentials"]["auth_type"] = "password"
                    extra["totp_secret"] = p.upper()
                elif re.match(r"^\+?[0-9]{7,15}$", p):
                    extra["phone"] = p
                    extra["credentials"]["phone"] = p
                elif "@" in p and " " not in p:
                    extra["recovery_email"] = p
                    extra["credentials"]["recovery_email"] = p

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
