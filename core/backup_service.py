"""FreeGPT-Manager 数据库一键加密快照与跨机迁移服务"""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from core.db import DATABASE_URL, engine, init_db
from core.secret_store import _key_file, reload_key
from core.version import __version__

MAGIC_HEADER = b"FGM_BAK_V1"  # 10 bytes
SALT_SIZE = 16  # 16 bytes
PBKDF2_ITERATIONS = 100_000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db_file_path() -> Path:
    """从 DATABASE_URL 解析出真实的本地 SQLite 文件绝对路径。"""
    prefix = "sqlite:///"
    if DATABASE_URL.startswith(prefix):
        raw_path = DATABASE_URL[len(prefix):]
        # 在 Windows 上如果是 sqlite:////app/data/... 或 sqlite:///C:/...
        # 兼容绝对路径与相对路径
        path = Path(raw_path).resolve()
        return path
    # 默认兜底
    return (Path(__file__).resolve().parent.parent / "data" / "account_manager.db").resolve()


def _get_snapshots_dir() -> Path:
    """本地快照保存目录。"""
    snapshots_dir = Path(__file__).resolve().parent.parent / "data" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return snapshots_dir


def _derive_key(password: str, salt: bytes) -> bytes:
    """使用 PBKDF2-HMAC-SHA256 派生 32 字节高强度密钥。"""
    if not password:
        raise ValueError("备份密码不能为空")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=SecretBox.KEY_SIZE,
    )


def _gather_manifest_from_sqlite(db_path: Path) -> dict[str, Any]:
    """从一个 SQLite 文件中读取账号与基础配置统计信息。"""
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "app_name": "FreeGPT-Manager",
        "app_version": __version__,
        "export_time": _utcnow_iso(),
        "total_accounts": 0,
        "accounts_by_platform": {},
        "total_microsoft_mailboxes": 0,
        "total_proxies": 0,
    }
    if not db_path.exists():
        return manifest

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        # 统计账号总数与各平台分布
        try:
            cursor.execute("SELECT platform, COUNT(*) FROM accounts GROUP BY platform")
            rows = cursor.fetchall()
            total_acc = 0
            by_plat = {}
            for plat, count in rows:
                by_plat[str(plat)] = int(count)
                total_acc += int(count)
            manifest["total_accounts"] = total_acc
            manifest["accounts_by_platform"] = by_plat
        except sqlite3.OperationalError:
            pass

        # 统计微软母体邮箱数量
        try:
            cursor.execute("SELECT COUNT(*) FROM microsoft_mailboxes")
            row = cursor.fetchone()
            if row:
                manifest["total_microsoft_mailboxes"] = int(row[0])
        except sqlite3.OperationalError:
            pass

        # 统计代理数量
        try:
            cursor.execute("SELECT COUNT(*) FROM proxies")
            row = cursor.fetchone()
            if row:
                manifest["total_proxies"] = int(row[0])
        except sqlite3.OperationalError:
            pass
    except Exception as exc:
        manifest["gather_warning"] = str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return manifest


def _dump_live_sqlite_to_bytes(source_db_path: Path) -> bytes:
    """使用 SQLite 官方备份 API 进行点对点事务安全快照。"""
    if not source_db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {source_db_path}")

    temp_backup = source_db_path.parent / f"_tmp_dump_{secrets.token_hex(6)}.db"
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{source_db_path}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(temp_backup))
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            try:
                dst_conn.close()
            except Exception:
                pass
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass

    try:
        return temp_backup.read_bytes()
    finally:
        if temp_backup.exists():
            try:
                temp_backup.unlink()
            except OSError:
                pass


def create_encrypted_backup(password: str) -> tuple[bytes, str, dict[str, Any]]:
    """创建加密备份包 (.fgmbak)。

    返回: (encrypted_file_bytes, suggested_filename, manifest)
    """
    if not password or len(password.strip()) < 4:
        raise ValueError("为了数据安全，备份密码至少需要 4 位字符")

    db_path = _get_db_file_path()
    if not db_path.exists():
        raise FileNotFoundError("未找到当前数据库文件，无法执行备份")

    # 1. 获取 SQLite 事务快照二进制与元数据
    db_bytes = _dump_live_sqlite_to_bytes(db_path)
    manifest = _gather_manifest_from_sqlite(db_path)

    # 2. 读取凭据主密钥（跨机迁移必需）
    key_path = _key_file()
    key_bytes: bytes | None = None
    if key_path.exists():
        try:
            key_bytes = key_path.read_bytes()
            manifest["includes_secret_key"] = True
        except Exception:
            manifest["includes_secret_key"] = False
    else:
        manifest["includes_secret_key"] = False

    # 3. 打包成 ZIP
    import zipfile

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account_manager.db", db_bytes)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if key_bytes is not None:
            zf.writestr("secret_key.bin", key_bytes)

    raw_zip_bytes = zip_buffer.getvalue()

    # 4. 加密 ZIP
    salt = secrets.token_bytes(SALT_SIZE)
    derived_key = _derive_key(password, salt)
    encrypted_payload = SecretBox(derived_key).encrypt(raw_zip_bytes)

    # 5. 组装最终文件: [MAGIC (10B)] + [SALT (16B)] + [ENCRYPTED_PAYLOAD]
    final_data = MAGIC_HEADER + salt + bytes(encrypted_payload)

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"FreeGPT_Manager_Backup_{timestamp_str}.fgmbak"

    return final_data, filename, manifest


def _decrypt_package_to_zip(file_bytes: bytes, password: str) -> tuple[bytes, dict[str, Any]]:
    """验证并解密文件字节，返回 (raw_zip_bytes, manifest)。"""
    if len(file_bytes) < len(MAGIC_HEADER) + SALT_SIZE + 40:
        raise ValueError("无效的备份文件：文件尺寸过小或已损坏")

    if not file_bytes.startswith(MAGIC_HEADER):
        raise ValueError("文件格式不匹配：不是 FreeGPT-Manager 的有效加密备份包")

    offset = len(MAGIC_HEADER)
    salt = file_bytes[offset: offset + SALT_SIZE]
    offset += SALT_SIZE
    encrypted_payload = file_bytes[offset:]

    derived_key = _derive_key(password, salt)
    try:
        decrypted_zip = SecretBox(derived_key).decrypt(encrypted_payload)
    except (CryptoError, ValueError) as exc:
        raise ValueError("解密失败：备份密码错误或文件内容已被篡改") from exc

    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(decrypted_zip), "r") as zf:
            file_list = zf.namelist()
            if "account_manager.db" not in file_list or "manifest.json" not in file_list:
                raise ValueError("备份文件内容损坏：缺少核心数据库或元数据清单")
            manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
            return decrypted_zip, manifest_data
    except zipfile.BadZipFile as exc:
        raise ValueError("备份解密数据损坏：无法解析 ZIP 归档") from exc


def inspect_encrypted_backup(file_bytes: bytes, password: str) -> dict[str, Any]:
    """在内存中解密并仅读取 manifest 信息，不写入磁盘，用于恢复前的安全预检。"""
    _, manifest = _decrypt_package_to_zip(file_bytes, password)
    return manifest


def restore_encrypted_backup(file_bytes: bytes, password: str) -> dict[str, Any]:
    """全自动安全恢复：先做本地防误删备份，再解密、验证并覆写数据库与凭据密钥。"""
    import zipfile

    # 1. 解密并提取元数据
    raw_zip, manifest = _decrypt_package_to_zip(file_bytes, password)

    # 2. 内存解包并做 SQLite 完整性检查
    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
        restored_db_bytes = zf.read("account_manager.db")
        restored_key_bytes = zf.read("secret_key.bin") if "secret_key.bin" in zf.namelist() else None

    # 将解压后的 db 写入临时文件，执行 PRAGMA integrity_check
    db_path = _get_db_file_path()
    temp_verify = db_path.parent / f"_verify_restore_{secrets.token_hex(6)}.db"
    try:
        temp_verify.write_bytes(restored_db_bytes)
        conn = None
        try:
            conn = sqlite3.connect(str(temp_verify))
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            check_result = cursor.fetchone()
            if not check_result or check_result[0] != "ok":
                raise ValueError(f"备份数据库完整性校验未通过: {check_result}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    finally:
        if temp_verify.exists():
            try:
                temp_verify.unlink()
            except OSError:
                pass

    # 3. 自动生成一份「恢复前安全救命快照」
    pre_snapshot = None
    if db_path.exists():
        try:
            pre_snapshot = create_local_snapshot(label="auto_pre_restore")
        except Exception as exc:
            print(f"[Backup] 预备份生成警告 (继续恢复): {exc}")

    # 4. 释放现有数据库连接池
    engine.dispose()

    # 5. 写入数据库与清理 WAL
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(restored_db_bytes)

    # 清理遗留的 SQLite 临时日志文件
    for ext in ("-wal", "-shm", "-journal"):
        wal_file = Path(str(db_path) + ext)
        if wal_file.exists():
            try:
                wal_file.unlink()
            except OSError:
                pass

    # 6. 恢复凭据密钥文件
    if restored_key_bytes:
        target_key_file = _key_file()
        target_key_file.parent.mkdir(parents=True, exist_ok=True)
        target_key_file.write_bytes(restored_key_bytes)
        try:
            target_key_file.chmod(0o600)
        except OSError:
            pass
        reload_key()

    # 7. 重新跑 init_db 以确保 schema 迁移和初始化连接池
    init_db()

    return {
        "success": True,
        "message": "数据库与凭据密钥已成功恢复！",
        "restored_manifest": manifest,
        "pre_restore_snapshot": pre_snapshot,
        "restored_at": _utcnow_iso(),
    }


def create_local_snapshot(label: str = "manual") -> dict[str, Any]:
    """在本地 data/snapshots/ 创建一个带时间戳的 SQLite 物理快照。"""
    db_path = _get_db_file_path()
    if not db_path.exists():
        raise FileNotFoundError("数据库文件不存在，无法创建快照")

    snapshots_dir = _get_snapshots_dir()
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_label = "".join(c for c in label if c.isalnum() or c in ("-", "_")).strip() or "snap"
    snapshot_filename = f"snapshot_{now_str}_{clean_label}.db"
    snapshot_path = snapshots_dir / snapshot_filename

    # 使用 SQLite 事务安全备份写入快照
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(snapshot_path))
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            try:
                dst_conn.close()
            except Exception:
                pass
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass

    stat = snapshot_path.stat()
    manifest = _gather_manifest_from_sqlite(snapshot_path)

    # 写入伴生元数据文件 (.json)
    meta_path = snapshots_dir / f"{snapshot_filename}.meta.json"
    meta_data = {
        "filename": snapshot_filename,
        "label": clean_label,
        "size_bytes": stat.st_size,
        "created_at": _utcnow_iso(),
        "total_accounts": manifest.get("total_accounts", 0),
        "total_microsoft_mailboxes": manifest.get("total_microsoft_mailboxes", 0),
        "manifest": manifest,
    }
    meta_path.write_text(json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")

    return meta_data


def list_local_snapshots() -> list[dict[str, Any]]:
    """列出本地现存的所有快照。"""
    snapshots_dir = _get_snapshots_dir()
    results = []

    for file in snapshots_dir.glob("snapshot_*.db"):
        stat = file.stat()
        meta_file = snapshots_dir / f"{file.name}.meta.json"
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                results.append(data)
                continue
            except Exception:
                pass

        # 如果没有 meta，直接读取基础信息
        results.append({
            "filename": file.name,
            "label": "unlabeled",
            "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "total_accounts": -1,
            "total_microsoft_mailboxes": -1,
        })

    # 按创建时间倒序排列
    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return results


def restore_local_snapshot(filename: str) -> dict[str, Any]:
    """从本地快照文件恢复当前数据库。"""
    snapshots_dir = _get_snapshots_dir()
    # 防路径穿越
    safe_name = Path(filename).name
    snapshot_path = (snapshots_dir / safe_name).resolve()
    if not snapshot_path.exists() or not snapshot_path.is_file():
        raise FileNotFoundError(f"未找到指定的快照文件: {safe_name}")

    db_path = _get_db_file_path()

    # 1. 恢复前先自动做一份当前库的安全快照
    pre_snap = None
    if db_path.exists():
        try:
            pre_snap = create_local_snapshot(label="before_rollback")
        except Exception as exc:
            print(f"[Backup] 本地快照回滚前置备份提醒: {exc}")

    # 2. 释放连接
    engine.dispose()

    # 3. 复制覆写
    shutil.copy2(str(snapshot_path), str(db_path))

    # 4. 清理 WAL 缓存
    for ext in ("-wal", "-shm", "-journal"):
        wal_file = Path(str(db_path) + ext)
        if wal_file.exists():
            try:
                wal_file.unlink()
            except OSError:
                pass

    # 5. 重启并加载 schema
    init_db()

    manifest = _gather_manifest_from_sqlite(db_path)
    return {
        "success": True,
        "message": f"已成功回滚至快照: {safe_name}",
        "pre_restore_snapshot": pre_snap,
        "current_stats": manifest,
        "restored_at": _utcnow_iso(),
    }


def delete_local_snapshot(filename: str) -> bool:
    """删除指定的本地快照及其元数据。"""
    import gc
    gc.collect()

    snapshots_dir = _get_snapshots_dir()
    safe_name = Path(filename).name
    target = snapshots_dir / safe_name
    meta = snapshots_dir / f"{safe_name}.meta.json"

    deleted = False
    if target.exists() and target.is_file():
        try:
            target.unlink()
            deleted = True
        except PermissionError:
            gc.collect()
            target.unlink()
            deleted = True

    if meta.exists() and meta.is_file():
        try:
            meta.unlink()
        except OSError:
            pass

    return deleted
