"""API endpoints for FreeGPT-Manager database backup, encrypted snapshots, and migration."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.backup_service import (
    _get_snapshots_dir,
    create_encrypted_backup,
    create_local_snapshot,
    delete_local_snapshot,
    inspect_encrypted_backup,
    list_local_snapshots,
    restore_encrypted_backup,
    restore_local_snapshot,
)

router = APIRouter(prefix="/backup", tags=["backup"])


class ExportBackupRequest(BaseModel):
    password: str


class CreateSnapshotRequest(BaseModel):
    label: Optional[str] = "manual"


@router.post("/export")
def export_backup(req: ExportBackupRequest):
    """创建并下载加密的数据库与密钥全量备份包 (.fgmbak)。"""
    password = (req.password or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="备份密码不能为空")

    try:
        data, filename, manifest = create_encrypted_backup(password)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成加密备份失败: {exc}")

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition, X-Total-Accounts",
            "X-Total-Accounts": str(manifest.get("total_accounts", 0)),
        },
    )


@router.post("/inspect")
async def inspect_backup(
    file: UploadFile = File(...),
    password: str = Form(...),
):
    """安全预检：在内存中解密读取备份包元数据，不执行恢复。"""
    try:
        content = await file.read()
        manifest = inspect_encrypted_backup(content, password.strip())
        return {"success": True, "manifest": manifest}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预检解析异常: {exc}")


@router.post("/restore")
async def restore_backup(
    file: UploadFile = File(...),
    password: str = Form(...),
):
    """解密并恢复数据库与凭据密钥（自动保留恢复前本地快照）。"""
    try:
        content = await file.read()
        result = restore_encrypted_backup(content, password.strip())
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"恢复数据库异常: {exc}")


@router.get("/snapshots")
def get_snapshots():
    """获取本地快照列表。"""
    try:
        snapshots = list_local_snapshots()
        return {"snapshots": snapshots}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取快照列表失败: {exc}")


@router.post("/snapshots")
def create_snapshot(req: CreateSnapshotRequest):
    """即时创建一份本地 SQLite 快照。"""
    try:
        snapshot = create_local_snapshot(label=req.label or "manual")
        return {"success": True, "snapshot": snapshot}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建快照失败: {exc}")


@router.post("/snapshots/{filename}/restore")
def rollback_snapshot(filename: str):
    """从指定的本地快照回滚数据库。"""
    try:
        result = restore_local_snapshot(filename)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"回滚快照失败: {exc}")


@router.delete("/snapshots/{filename}")
def remove_snapshot(filename: str):
    """删除指定的本地快照。"""
    try:
        deleted = delete_local_snapshot(filename)
        if not deleted:
            raise HTTPException(status_code=404, detail="快照不存在")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除快照失败: {exc}")


@router.get("/snapshots/{filename}/download")
def download_snapshot(filename: str):
    """下载本地快照文件。"""
    snapshots_dir = _get_snapshots_dir()
    safe_name = Path(filename).name
    target = snapshots_dir / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="快照文件不存在")

    return FileResponse(
        str(target),
        filename=safe_name,
        media_type="application/octet-stream",
    )
