"""Field-level encryption for credentials persisted in the local database."""
from __future__ import annotations

import base64
import hashlib
import os
import threading
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as random_bytes


_PREFIX = "sb1:"
_KEY_LOCK = threading.Lock()
_KEY_CACHE: bytes | None = None
_DEFAULT_KEY_FILE = (
    Path(__file__).resolve().parent.parent / "data" / ".microsoft_mailbox.key"
)


def _configured_key() -> bytes | None:
    value = os.getenv("MICROSOFT_MAILBOX_ENCRYPTION_KEY", "").strip()
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception:
        decoded = b""
    if len(decoded) == SecretBox.KEY_SIZE:
        return decoded
    return hashlib.sha256(value.encode("utf-8")).digest()


def _key_file() -> Path:
    configured = os.getenv("MICROSOFT_MAILBOX_ENCRYPTION_KEY_FILE", "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_KEY_FILE


def _read_key(path: Path) -> bytes:
    try:
        key = base64.urlsafe_b64decode(path.read_bytes().strip())
    except Exception as exc:
        raise RuntimeError(f"微软邮箱加密密钥不可读: {path}") from exc
    if len(key) != SecretBox.KEY_SIZE:
        raise RuntimeError(f"微软邮箱加密密钥长度无效: {path}")
    return key


def _load_key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    with _KEY_LOCK:
        if _KEY_CACHE is not None:
            return _KEY_CACHE
        configured = _configured_key()
        if configured is not None:
            _KEY_CACHE = configured
            return configured

        path = _key_file()
        if path.exists():
            _KEY_CACHE = _read_key(path)
            return _KEY_CACHE

        path.parent.mkdir(parents=True, exist_ok=True)
        key = random_bytes(SecretBox.KEY_SIZE)
        encoded = base64.urlsafe_b64encode(key)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _KEY_CACHE = _read_key(path)
            return _KEY_CACHE
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _KEY_CACHE = key
        return key


def reload_key() -> bytes:
    """Clear cached key and reload from key file or environment."""
    global _KEY_CACHE
    with _KEY_LOCK:
        _KEY_CACHE = None
    return _load_key()


def encrypt_secret(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    encrypted = SecretBox(_load_key()).encrypt(text.encode("utf-8"))
    return _PREFIX + base64.urlsafe_b64encode(bytes(encrypted)).decode("ascii")


def decrypt_secret(value: object) -> str:
    text = str(value or "")
    if not text or not text.startswith(_PREFIX):
        return text
    try:
        payload = base64.urlsafe_b64decode(text[len(_PREFIX) :].encode("ascii"))
        return SecretBox(_load_key()).decrypt(payload).decode("utf-8")
    except (ValueError, CryptoError, UnicodeDecodeError) as exc:
        raise RuntimeError("微软邮箱凭据解密失败，请检查服务器加密密钥") from exc
