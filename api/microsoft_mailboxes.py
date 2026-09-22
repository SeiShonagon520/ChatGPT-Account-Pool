from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool

from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_local_ms_pool_rows
from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository


router = APIRouter(prefix="/microsoft-mailboxes", tags=["microsoft-mailboxes"])
repository = MicrosoftMailboxRepository()

MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_IMPORT_FILES = 100


def _decode_text(payload: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, f"TXT 文件编码无法识别: {filename}")


from pydantic import BaseModel, Field


class BatchTestMailboxesRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)
    concurrency: int = Field(default=10, ge=1, le=30)


@router.get("/stats")
def mailbox_stats():
    return repository.stats()


@router.post("/batch-test")
async def batch_test_mailboxes(body: BatchTestMailboxesRequest | None = None):
    req = body or BatchTestMailboxesRequest()
    concurrency = min(max(req.concurrency, 1), 30)
    records = await run_in_threadpool(
        repository.list_for_testing,
        req.emails if req.emails else None,
    )
    if not records:
        return {
            "ok": True,
            "tested": 0,
            "valid": 0,
            "invalid": 0,
            "results": {},
            "stats": repository.stats(),
        }

    pool = LocalMicrosoftMailboxPool()

    def _test_single(record) -> tuple[str, bool, str]:
        entry = pool._entry_from_record(record)
        if entry.graph_ready:
            try:
                token = pool._graph_access_token(entry)
                if not token:
                    return (record.email, False, "无法获取 Graph Access Token")
                return (record.email, True, "Graph 授权有效")
            except Exception as exc:
                return (record.email, False, str(exc)[:200])
        elif entry.imap_ready:
            try:
                conn = pool._imap_connect(entry)
                try:
                    conn.login(entry.login_account or entry.email, entry.password)
                    return (record.email, True, "IMAP 验证成功")
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception as exc:
                return (record.email, False, f"IMAP 连接失败: {str(exc)[:150]}")
        else:
            return (record.email, False, "未配置 Graph 或 IMAP 凭据")

    from concurrent.futures import ThreadPoolExecutor

    def _run_all():
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            return list(executor.map(_test_single, records))

    raw_results = await run_in_threadpool(_run_all)
    results: dict[str, dict] = {}
    valid_count = 0
    invalid_count = 0
    for email, ok, message in raw_results:
        results[email] = {"ok": ok, "message": message}
        if ok:
            valid_count += 1
        else:
            invalid_count += 1

    return {
        "ok": True,
        "tested": len(records),
        "valid": valid_count,
        "invalid": invalid_count,
        "results": results,
        "stats": repository.stats(),
    }


@router.post("/clear-disabled")
async def clear_disabled_mailboxes():
    deleted = await run_in_threadpool(repository.delete_disabled)
    return {
        "ok": True,
        "deleted": deleted,
        "stats": repository.stats(),
    }


@router.get("/{email}/messages")
async def mailbox_messages(email: str, limit: int = Query(default=20, ge=1, le=50)):
    """Read the latest inbox messages for a Microsoft mailbox (Graph or IMAP).

    Accepts either the parent mailbox address or one of its split sub-addresses
    (``xxx+sub-1@outlook.com``) — sub-addresses resolve to the parent mailbox.
    """
    import urllib.parse

    decoded = urllib.parse.unquote(email.strip())
    pool = LocalMicrosoftMailboxPool()
    parent_key = pool._parent_email_key(decoded)
    record = await run_in_threadpool(repository.get_by_parent_email, parent_key)
    if record is None:
        raise HTTPException(404, f"未找到邮箱 {decoded}")
    entry = pool._entry_from_record(record)
    try:
        if entry.graph_ready:
            messages = await run_in_threadpool(pool._graph_messages, entry, limit=limit)
            source = "graph"
        elif entry.imap_ready:
            messages = await run_in_threadpool(pool._imap_messages, entry, limit=limit)
            source = "imap"
        else:
            raise HTTPException(
                400,
                f"邮箱 {decoded} 没有可用的 Graph token，也没有 IMAP 收件配置",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"读取邮件失败: {str(exc)[:200]}") from exc
    return {
        "email": decoded,
        "parent_email": record.email,
        "source": source,
        "status": record.status,
        "use_count": record.use_count,
        "max_uses": record.max_uses,
        "messages": messages,
    }


@router.get("")
def list_mailboxes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = Query(default=""),
    search: str = Query(default="", max_length=200),
):
    normalized_status = status.strip().lower()
    if normalized_status and normalized_status not in {"available", "exhausted", "disabled"}:
        raise HTTPException(400, "邮箱状态无效")
    return repository.list_page(
        page=page,
        page_size=page_size,
        status=normalized_status,
        search=search,
    )


@router.post("/import")
async def import_mailboxes(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "请选择至少一个 TXT 文件")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(400, f"单次最多导入 {MAX_IMPORT_FILES} 个文件")

    entries_by_email: dict[str, object] = {}
    total_bytes = 0
    nonempty_lines = 0
    parsed_rows = 0
    for upload in files:
        remaining_bytes = MAX_IMPORT_BYTES - total_bytes
        payload = await upload.read(remaining_bytes + 1)
        total_bytes += len(payload)
        if total_bytes > MAX_IMPORT_BYTES:
            raise HTTPException(413, "单次导入文件总大小不能超过 100 MB")
        text = _decode_text(payload, upload.filename or "mailboxes.txt")
        nonempty_lines += sum(
            1
            for line in text.splitlines()
            if line.strip()
            and not line.lstrip().startswith(("#", "//", "'"))
        )
        parsed = parse_local_ms_pool_rows(text)
        parsed_rows += len(parsed)
        for entry in parsed:
            entries_by_email[entry.key] = entry

    if not entries_by_email:
        raise HTTPException(400, "TXT 文件中没有解析到有效的微软邮箱")

    result = await run_in_threadpool(
        repository.import_entries,
        entries_by_email.values(),
        max_uses=6,
    )
    return {
        "ok": True,
        "files": len(files),
        "bytes": total_bytes,
        "lines": nonempty_lines,
        "parsed": parsed_rows,
        "unique": len(entries_by_email),
        "invalid": max(nonempty_lines - parsed_rows, 0),
        "duplicates_in_upload": max(parsed_rows - len(entries_by_email), 0),
        **result,
    }


class MailboxImportTextRequest(BaseModel):
    text: str


@router.post("/import-text")
async def import_mailboxes_text(body: MailboxImportTextRequest):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "请输入邮箱卡密内容")
    parsed = parse_local_ms_pool_rows(text)
    if not parsed:
        raise HTTPException(400, "没有解析到有效的微软邮箱")
    result = await run_in_threadpool(
        repository.import_entries,
        parsed,
        max_uses=6,
    )
    return {
        "ok": True,
        "parsed": len(parsed),
        **result,
    }


@router.post("/{email:path}/test")
async def test_mailbox(email: str):
    import urllib.parse

    decoded = urllib.parse.unquote(email).strip().lower()
    pool = LocalMicrosoftMailboxPool()
    parent_key = pool._parent_email_key(decoded)
    record = await run_in_threadpool(repository.get_by_parent_email, parent_key)
    if not record:
        raise HTTPException(404, f"未找到邮箱 {decoded}")
    entry = pool._entry_from_record(record)
    if not entry.graph_ready:
        return {"ok": False, "email": decoded, "error": "该邮箱未配置 Graph client_id 与 refresh_token"}
    try:
        token = await run_in_threadpool(pool._graph_access_token, entry)
        if not token:
            return {"ok": False, "email": decoded, "error": "无法获取 Graph Access Token"}
        return {"ok": True, "email": decoded, "message": "微软 Graph 授权正常有效"}
    except Exception as exc:
        return {"ok": False, "email": decoded, "error": str(exc)[:200]}


@router.delete("/{email:path}")
def delete_mailbox(email: str):
    import urllib.parse

    decoded = urllib.parse.unquote(email).strip().lower()
    ok = repository.delete_by_email(decoded)
    if not ok:
        raise HTTPException(404, "邮箱不存在或删除失败")
    return {"ok": True, "email": decoded}


class UpdateMailboxStatusRequest(BaseModel):
    status: str


@router.patch("/{email:path}/status")
async def update_mailbox_status(email: str, body: UpdateMailboxStatusRequest):
    import urllib.parse

    decoded = urllib.parse.unquote(email).strip().lower()
    target_status = body.status.strip().lower()
    if target_status not in {"available", "disabled"}:
        raise HTTPException(400, "状态只能为 'available' 或 'disabled'")
    ok = await run_in_threadpool(repository.set_status, decoded, target_status)
    if not ok:
        raise HTTPException(404, "邮箱不存在或更新失败")
    record = await run_in_threadpool(repository.get_by_parent_email, decoded)
    return {
        "ok": True,
        "email": decoded,
        "status": record.status if record else target_status,
        "stats": repository.stats(),
    }

