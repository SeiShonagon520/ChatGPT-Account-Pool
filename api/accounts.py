from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.account_exports import AccountExportsService, ExportArtifact
from application.accounts import AccountsService
from domain.accounts import AccountExportSelection, AccountQuery, AccountUpdateCommand

router = APIRouter(prefix="/accounts", tags=["accounts"])
service = AccountsService()
exports_service = AccountExportsService()


class AccountCreateRequest(BaseModel):
    platform: str
    email: str
    password: str
    user_id: Optional[str] = None


class AccountUpdateRequest(BaseModel):
    password: Optional[str] = None
    user_id: Optional[str] = None
    lifecycle_status: Optional[str] = None
    overview: Optional[dict] = None
    credentials: Optional[dict] = None
    provider_accounts: Optional[list[dict]] = None
    provider_resources: Optional[list[dict]] = None
    replace_provider_accounts: bool = False
    replace_provider_resources: bool = False
    primary_token: Optional[str] = None
    cashier_url: Optional[str] = None
    region: Optional[str] = None
    trial_end_time: Optional[int] = None


class ImportRequest(BaseModel):
    platform: str = "chatgpt"
    lines: Optional[list[str]] = None
    text: Optional[str] = None
    auto_check: bool = False
    concurrency: int = 50
    proxy_node: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class BatchExportRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    codex_only: bool = False
    force: bool = False


class Sub2ApiAgentIdentityUploadRequest(BatchExportRequest):
    sub2api_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


def _stream_artifact(artifact: ExportArtifact) -> StreamingResponse:
    if isinstance(artifact.content, io.BytesIO):
        body = artifact.content
    elif isinstance(artifact.content, bytes):
        body = iter([artifact.content])
    else:
        body = iter([artifact.content])
    return StreamingResponse(
        body,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f"attachment; filename={artifact.filename}"},
    )


@router.get("")
def list_accounts(
    platform: str = "",
    email: str = "",
    status: str = "",
    has_refresh_token: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return service.list_accounts(AccountQuery(
        platform=platform,
        status=status,
        email=email,
        has_refresh_token=has_refresh_token,
        page=page,
        page_size=page_size,
    ))


@router.get("/survival-stats")
def get_account_survival_stats(platform: str = "chatgpt"):
    return service.survival_stats(platform)


@router.post("/export/json")
def export_accounts_json(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_json(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/csv")
def export_accounts_csv(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_csv(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/check-codex-readiness")
def check_accounts_codex_readiness(body: BatchExportRequest):
    try:
        return exports_service.check_accounts_codex_readiness(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
                codex_only=body.codex_only,
                force=body.force,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/export/sub2api")
def export_accounts_sub2api(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_sub2api(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
                codex_only=body.codex_only,
                force=body.force,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/sub2api-agent-identity")
def export_accounts_sub2api_agent_identity(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_agent_identity_sub2api(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/upload/sub2api-agent-identity")
def upload_accounts_sub2api_agent_identity(body: Sub2ApiAgentIdentityUploadRequest):
    try:
        return exports_service.upload_chatgpt_agent_identity_to_sub2api(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            ),
            sub2api_url=body.sub2api_url,
            api_key=body.api_key,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/export/cpa")
def export_accounts_cpa(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_cpa(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/any2api")
def export_accounts_any2api(body: BatchExportRequest):
    try:
        artifact = exports_service.export_any2api(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/cockpit")
def export_accounts_cockpit(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_cockpit(
            AccountExportSelection(
                platform=body.platform,
                ids=body.ids,
                select_all=body.select_all,
                status_filter=body.status_filter or "",
                search_filter=body.search_filter or "",
                codex_only=body.codex_only,
                force=body.force,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.get("/tasks/{task_id}/export-recovered-sub2api")
def export_task_recovered_sub2api(task_id: str):
    from application.tasks import get_task
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    result_dict = task.get("result") if isinstance(task.get("result"), dict) else {}
    data_dict = task.get("data") if isinstance(task.get("data"), dict) else {}
    nested_data = result_dict.get("data") if isinstance(result_dict.get("data"), dict) else {}

    recovered_ids = (
        data_dict.get("recovered_account_ids")
        or nested_data.get("recovered_account_ids")
        or result_dict.get("recovered_account_ids")
        or []
    )
    if not recovered_ids:
        raise HTTPException(400, "该任务没有 401 恢复成功的账号")
    try:
        artifact = exports_service.export_chatgpt_sub2api_bundle(
            AccountExportSelection(
                platform="chatgpt",
                ids=[int(aid) for aid in recovered_ids],
                select_all=False,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/import")
def import_accounts(body: ImportRequest):
    lines = list(body.lines or [])
    if body.text:
        lines.extend(body.text.splitlines())
    result = service.import_accounts(body.platform, lines)

    task_info = None
    if body.auto_check and (result.get("created", 0) > 0 or result.get("updated", 0) > 0):
        try:
            from application.account_checks import AccountChecksService
            checks_service = AccountChecksService()
            task_info = checks_service.check_refresh_tokens_async(
                body.platform,
                body.concurrency or 50,
                proxy_node=body.proxy_node or None,
                browser=True,
            )
        except Exception:
            pass

    return {
        **result,
        "task": task_info,
    }


@router.post("/import-file")
async def import_accounts_file(
    file: UploadFile = File(...),
    platform: str = "chatgpt",
    auto_check: bool = False,
    concurrency: int = 50,
    proxy_node: Optional[str] = None,
):
    if not file:
        raise HTTPException(400, "请选择文件")
    payload = await file.read(20 * 1024 * 1024)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = payload.decode("gbk")
        except UnicodeDecodeError:
            text = payload.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    result = service.import_accounts(platform, lines)

    task_info = None
    if auto_check and (result.get("created", 0) > 0 or result.get("updated", 0) > 0):
        try:
            from application.account_checks import AccountChecksService
            checks_service = AccountChecksService()
            task_info = checks_service.check_refresh_tokens_async(
                platform,
                concurrency or 50,
                proxy_node=proxy_node or None,
                browser=True,
            )
        except Exception:
            pass

    return {
        **result,
        "task": task_info,
    }


@router.get("/{account_id:int}")
def get_account(account_id: int):
    item = service.get_account(account_id)
    if not item:
        raise HTTPException(404, "账号不存在")
    return item


@router.patch("/{account_id:int}")
def update_account(account_id: int, body: AccountUpdateRequest):
    item = service.update_account(account_id, AccountUpdateCommand(**body.model_dump()))
    if not item:
        raise HTTPException(404, "账号不存在")
    return item


@router.delete("/{account_id:int}")
def delete_account(account_id: int):
    result = service.delete_account(account_id)
    if not result["ok"]:
        raise HTTPException(404, "账号不存在")
    return result


@router.post("/batch-delete")
def batch_delete_accounts(body: BatchDeleteRequest):
    deleted = 0
    for acc_id in body.ids:
        try:
            res = service.delete_account(acc_id)
            if res.get("ok"):
                deleted += 1
        except Exception:
            pass
    return {"ok": True, "deleted": deleted, "total": len(body.ids)}
