"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import (
    AccountModel,
    TaskEventModel,
    TaskModel,
    engine,
    record_registered_email,
    save_account,
)
from core.platform_accounts import build_platform_account
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime

TASK_TYPE_REGISTER = "register"
TASK_TYPE_REFRESH_TOKEN_CHECK = "refresh_token_check"
TASK_TYPE_PLATFORM_ACTION = "platform_action"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

# A registration task may have up to two hundred live protocol workers.
# Unlimited tasks keep this pool full until the user explicitly stops the
# task.  The pulse path does not cap waves at this number: it is bounded by
# the healthy-node pool and the Mihomo slot count instead.
MAX_REGISTER_CONCURRENCY = 200

# Pulse-registration marker: the worker never received the OTP email.  The
# pulse controller requeues the account index and (after the consecutive
# threshold) pauses that node instead of counting a hard failure.
_NO_EMAIL_MARKER = "__no_email__"
# Fatal marker: the mailbox pool is exhausted, so no further account can ever
# be created.  Both controllers terminate the task instead of spinning out
# thousands of pool-exhaustion failures.
_POOL_EXHAUSTED_MARKER = "__pool_exhausted__"
_BROWSER_INFRA_FAILURE_MARKERS = (
    "共享浏览器进程已退出",
    "共享浏览器进程在注册过程中退出",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser disconnected",
    "target closed",
)
_BROWSER_FATAL_FAILURE_MARKERS = (
    "共享浏览器事件循环超过",
    "共享浏览器池无可用进程",
)
# The executor keeps only ``concurrency`` futures in flight, so this remains
# bounded even when a task checks tens of thousands of accounts.
MAX_REFRESH_TOKEN_CHECK_CONCURRENCY = 200
DEFAULT_REFRESH_TOKEN_CHECK_CONCURRENCY = 100
REFRESH_TOKEN_CHECK_ACCOUNT_TIMEOUT_SECONDS = 240
REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS = 10
POST_REGISTER_RECHECK_DELAY_SECONDS = 120
MAX_TASK_ACCOUNT_SUMMARIES = 200
MAX_TASK_ERROR_DETAILS = 100
CONFIRMED_CHATGPT_BAN_CODES = {
    "account_deactivated",
    "account_suspended",
    "account_banned",
}


def _is_browser_infrastructure_failure(value: Any) -> bool:
    message = str(value or "").strip().lower()
    return any(marker.lower() in message for marker in _BROWSER_INFRA_FAILURE_MARKERS)


def _is_browser_runtime_fatal_failure(value: Any) -> bool:
    message = str(value or "").strip().lower()
    return any(marker.lower() in message for marker in _BROWSER_FATAL_FAILURE_MARKERS)

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()
_credential_check_write_lock = threading.Lock()

# Sub2API Admin keys are intentionally volatile.  The browser submits them
# when creating a task, but they are never put in task payloads, the SQLite
# database, result data, or task logs.  The shared lock also makes attaching
# the config atomic with task creation, before a worker may claim the task.
_register_sub2api_upload_configs: dict[str, dict[str, str]] = {}
_register_sub2api_upload_configs_guard = threading.RLock()


def register_sub2api_upload_configs_guard() -> threading.RLock:
    return _register_sub2api_upload_configs_guard


def set_register_sub2api_upload_config(
    task_id: str,
    *,
    sub2api_url: str,
    api_key: str,
) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs[task_id] = {
            "sub2api_url": str(sub2api_url or "").strip(),
            "api_key": str(api_key or "").strip(),
        }


def get_register_sub2api_upload_config(task_id: str) -> dict[str, str] | None:
    with _register_sub2api_upload_configs_guard:
        config = _register_sub2api_upload_configs.get(task_id)
        return dict(config) if config else None


def clear_register_sub2api_upload_config(task_id: str) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs.pop(task_id, None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type == TASK_TYPE_PLATFORM_ACTION:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    if task_type == TASK_TYPE_REFRESH_TOKEN_CHECK:
        return [
            f"account:{int(account_id)}"
            for account_id in list(payload.get("account_ids") or [])
            if int(account_id or 0) > 0
        ]
    return []


def _task_not_before_has_passed(payload: dict[str, Any], *, now: datetime | None = None) -> bool:
    value = str(payload.get("not_before") or "").strip()
    if not value:
        return True
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # A malformed defer marker must not strand a task forever.
        return True
    current = now or _utcnow()
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return current >= deadline


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    payload = task.get_payload()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    unlimited = task.type == TASK_TYPE_REGISTER and int(payload.get("count", 1) or 0) == 0
    if progress_total > 0 and not unlimited:
        # Older browser-check tasks counted both verification phases and may
        # have persisted values such as 111/58. Keep their raw audit record,
        # but never expose an impossible finite progress value to the UI.
        progress_current = min(progress_current, progress_total)
    progress_label = (
        f"{progress_current}/∞"
        if unlimited
        else (f"{progress_current}/{progress_total}" if progress_total else "0/0")
    )
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": progress_label,
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": progress_label,
            "unlimited": unlimited,
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 0), 0)
    executor_type = str(payload.get("executor_type") or "protocol") or "protocol"
    if executor_type not in ("protocol", "headless", "headed"):
        executor_type = "protocol"
    extra = dict(payload.get("extra") or {})
    # Every automated ChatGPT registration must leave a remotely configured
    # password and an activated TOTP factor. There is no opt-out: otherwise an
    # API client could still create passwordless or incomplete accounts.
    extra["bind_totp_2fa"] = True
    payload = {
        **payload,
        "platform": "chatgpt",
        "count": count,
        "executor_type": executor_type,
        "extra": extra,
    }
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload=payload,
        progress_total=count,
    )


def _bounded_concurrency(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    return min(max(int(value or default), 1), maximum)


def create_refresh_token_check_task(
    platform: str = "chatgpt",
    concurrency: int | None = None,
    *,
    schedule_date: str = "",
    proxy_node: str | None = None,
    browser: bool = True,
    account_ids: list[int] | None = None,
    not_before: str = "",
    schedule_source: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": platform,
        "concurrency": _bounded_concurrency(
            concurrency,
            default=DEFAULT_REFRESH_TOKEN_CHECK_CONCURRENCY,
            maximum=MAX_REFRESH_TOKEN_CHECK_CONCURRENCY,
        ),
        "browser": bool(browser),
    }
    if str(proxy_node or "").strip():
        payload["proxy_node"] = str(proxy_node).strip()
    normalized_account_ids = sorted(
        {
            int(account_id)
            for account_id in list(account_ids or [])
            if int(account_id or 0) > 0
        }
    )
    if normalized_account_ids:
        payload["account_ids"] = normalized_account_ids
    if str(not_before or "").strip():
        payload["not_before"] = str(not_before).strip()
    if schedule_date:
        payload.update(
            {
                "schedule_source": "daily_401_check",
                "schedule_date": schedule_date,
            }
        )
    elif str(schedule_source or "").strip():
        payload["schedule_source"] = str(schedule_source).strip()
    return create_task(
        task_type=TASK_TYPE_REFRESH_TOKEN_CHECK,
        platform=platform,
        payload=payload,
        progress_total=len(normalized_account_ids),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(*, task_type: str = "", limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
    limit = min(max(int(limit or 100), 1), 500)
    with Session(engine) as session:
        statement = select(TaskModel)
        if task_type:
            statement = statement.where(TaskModel.type == task_type)
        if active_only:
            statement = statement.where(TaskModel.status.not_in(TERMINAL_TASK_STATUSES))
        tasks = session.exec(
            statement.order_by(TaskModel.created_at.desc()).limit(limit)
        ).all()
    return [serialize_task(task) for task in tasks]


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        q = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id > since)
            .order_by(TaskEventModel.id)
            .limit(limit)
        )
        items = session.exec(q).all()
    return [serialize_event(item) for item in items]


def list_latest_task_events(task_id: str, *, limit: int = 300) -> list[dict[str, Any]]:
    """Return the newest task events in chronological order.

    A page refresh must show the current tail immediately; replaying an entire
    long-running registration task before reaching the latest log is both slow
    and not useful in the fixed-height live-log panel.
    """
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        items = session.exec(
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .order_by(TaskEventModel.id.desc())
            .limit(limit)
        ).all()
    return [serialize_event(item) for item in reversed(items)]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
    else:
        task.status = TASK_STATUS_CANCEL_REQUESTED


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            if not _task_not_before_has_passed(payload):
                continue
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            if platform and running_platform_counts.get(platform, 0) >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self._event_lock = threading.Lock()
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        with self._event_lock:
            append_task_event(
                self.task_id,
                message,
                event_type=event_type,
                level=level,
                detail=merged_detail or None,
            )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        try:
            print(f"{prefix} {message}")
        except (BrokenPipeError, OSError, ValueError):
            # A detached/background server can lose its parent console. Task
            # persistence must not fail just because stdout is unavailable.
            pass

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        _mutate_task(self.task_id, _update)
        self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(
                task
                and task.status
                in {
                    TASK_STATUS_CANCEL_REQUESTED,
                    TASK_STATUS_CANCELLED,
                    TASK_STATUS_INTERRUPTED,
                }
            )

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            if len(errors) < MAX_TASK_ERROR_DETAILS:
                errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_counts(self, *, success: int, error: int) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count = max(int(success), 0)
            task.error_count = max(int(error), 0)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _build_platform_instance(
    platform_name: str,
    payload: dict[str, Any],
    logger: TaskLogger,
    resolved_proxy: str | None = None,
    shared_mailbox=None,
    proxy_rotate_callback: Callable[[], str | None] | None = None,
    otp_timeout_seconds: int | None = None,
    otp_received_callback: Callable[[], None] | None = None,
):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "headless") or "headless")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    if otp_timeout_seconds is not None:
        # The plugin's OtpSpec reads ``extra["otp_timeout"]`` (see
        # platforms/chatgpt/plugin.py), so the pulse ban probe can shorten the
        # OTP wait without touching the shared task payload.
        extra["otp_timeout"] = int(otp_timeout_seconds)
    if otp_received_callback is not None:
        # Runtime-only signal used by pulse probes.  It lets the controller
        # distinguish a failure before OAI sent mail (keep the node paused)
        # from a later registration failure after the OTP was actually read.
        extra["_otp_received_callback"] = otp_received_callback
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=resolved_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    if hasattr(platform, "set_cancel_checker"):
        platform.set_cancel_checker(logger.is_cancel_requested)
    if hasattr(platform, "set_proxy_rotate_callback"):
        platform.set_proxy_rotate_callback(proxy_rotate_callback)
    return platform


def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}
            if hasattr(plugin, "get_last_check_overview"):
                summary_updates.update(plugin.get_last_check_overview() or {})
            lifecycle_status = None
            if valid:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_REFRESH_TOKEN_CHECK: _execute_refresh_token_check_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    try:
        handler(payload, logger)
    except Exception as exc:
        error = str(exc).strip() or exc.__class__.__name__
        logger.log(f"任务执行异常: {error}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=error)
    finally:
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)
            # 释放共享浏览器进程池（无头批量注册），避免任务结束后浏览器
            # 进程仍常驻占内存。
            try:
                from platforms.chatgpt.browser_pool import shutdown_shared_pool

                shutdown_shared_pool()
            except Exception:
                pass


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
) -> str | None:
    normalized_explicit_proxy = str(explicit_proxy or "").strip() or None
    if str(platform_name or "").strip().lower() == "chatgpt":
        # ChatGPT 只使用本次任务显式传入的动态 IP；留空时固定本地直连，
        # 不从全局代理池回退。
        return normalized_explicit_proxy
    return normalized_explicit_proxy or proxy_getter()


def _registration_concurrency(requested: Any, count: int) -> int:
    limit = MAX_REGISTER_CONCURRENCY if int(count or 0) == 0 else min(
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )
    return min(max(int(requested or 1), 1), limit)


def _access_token_for_account(account: Any) -> str:
    extra = getattr(account, "extra", {}) or {}
    return str(
        getattr(account, "token", "")
        or extra.get("access_token", "")
        or extra.get("accessToken", "")
        or ""
    ).strip()


def _cookie_header_value(value: Any) -> str:
    """Normalize exported browser/protocol cookies for an HTTP Cookie header."""
    if isinstance(value, dict):
        return "; ".join(
            f"{str(name).strip()}={str(cookie).strip()}"
            for name, cookie in value.items()
            if str(name).strip()
        )
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _persist_totp_secret(account_id: int, secret: str) -> None:
    """Save a TOTP 2FA secret to the account's credentials store."""
    if not secret:
        return
    from application.accounts import AccountsService
    from domain.accounts import AccountUpdateCommand

    AccountsService().update_account(
        int(account_id),
        AccountUpdateCommand(credentials={"totp_secret": str(secret).strip()}),
    )


def _bind_registered_account_totp(
    account: Any,
    account_id: int | None = None,
    *,
    proxy: str | None = None,
) -> str:
    """Bind TOTP through the authenticated API and persist its secret."""
    from curl_cffi import requests as _cffi_requests
    from platforms.chatgpt.environment_profile import PROTOCOL_CHROME_IMPERSONATE
    from platforms.chatgpt.mfa import bind_totp_2fa

    access_token = _access_token_for_account(account)
    if not access_token:
        raise RuntimeError("注册结果缺少 access token，无法绑定 TOTP 2FA")

    account_extra = dict(getattr(account, "extra", {}) or {})
    session_kwargs: dict[str, Any] = {
        "impersonate": PROTOCOL_CHROME_IMPERSONATE,
        "timeout": 30,
    }
    cookies = _cookie_header_value(account_extra.get("cookies"))
    if cookies:
        session_kwargs["headers"] = {"Cookie": cookies}
    mfa_session = _cffi_requests.Session(**session_kwargs)
    try:
        if proxy:
            mfa_session.proxies = {"http": proxy, "https": proxy}
        result = bind_totp_2fa(mfa_session, access_token)
    finally:
        close = getattr(mfa_session, "close", None)
        if callable(close):
            close()

    if not result.get("activated"):
        raise RuntimeError(
            f"TOTP 激活未确认：{str(result.get('result') or result)[:160]}"
        )
    secret = str(result.get("secret") or "").strip()
    if not secret:
        raise RuntimeError("TOTP 已激活但接口未返回 secret")
    if int(account_id or 0) > 0:
        _persist_totp_secret(int(account_id), secret)
    return secret


def _check_newly_registered_chatgpt_account(
    account: Any,
    *,
    proxy: str | None = None,
) -> dict[str, str]:
    """Immediately run the saved 401 check against a registration's AT.

    Registration results already contain the access token.  Persist the check
    result in the account overview so a newly saved account does not start out
    as "unconfirmed" merely because the maintenance task has not run yet.
    """
    from platforms.chatgpt.credential_checks import check_chatgpt_access_token

    extra = dict(getattr(account, "extra", {}) or {})
    result = check_chatgpt_access_token(
        _access_token_for_account(account),
        proxy=proxy,
        account_id=str(
            extra.get("chatgpt_account_id")
            or extra.get("account_id")
            or getattr(account, "user_id", "")
            or ""
        ),
    )
    state = str(result.get("state") or "unknown")
    message = str(result.get("message") or "")
    overview = dict(extra.get("account_overview") or {})
    overview.update(
        {
            "refresh_token_status": state,
            "valid": state == "valid",
            "refresh_token_checked_at": _utcnow_iso(),
            "refresh_token_check_message": message,
            "refresh_token_check_method": "access_token",
            "relogin_status": "unavailable" if state == "invalid" else "",
        }
    )
    extra["account_overview"] = overview
    account.extra = extra
    return {"state": state, "message": message}


def _run_unlimited_registration(
    do_one: Callable[[int], dict[str, Any] | str],
    *,
    concurrency: int,
    logger: TaskLogger,
    registered_account_ids: list[int] | None = None,
) -> None:
    """Keep at most ``concurrency`` protocol registrations in flight."""
    success = 0
    failure_count = 0
    first_error = ""
    completed = 0
    next_index = 0
    fatal_error = ""
    browser_infra_failures_since_success = 0
    browser_infra_failure_limit = max(int(concurrency) * 2, 8)
    registered_accounts: list[dict[str, Any]] = []

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending = {
                pool.submit(do_one, index)
                for index in range(concurrency)
            }
            next_index = concurrency
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    completed += 1
                    if isinstance(result, dict):
                        success += 1
                        browser_infra_failures_since_success = 0
                        if len(registered_accounts) < MAX_TASK_ACCOUNT_SUMMARIES:
                            registered_accounts.append(result)
                    elif result == _POOL_EXHAUSTED_MARKER:
                        # Mailbox pool exhausted: stop the whole task, not one
                        # account.  The executor drains in-flight futures below.
                        if not fatal_error:
                            fatal_error = "本地微软邮箱池已用尽，注册任务终止"
                    elif result != "__cancel_requested__":
                        failure_count += 1
                        first_error = first_error or str(result)
                        if _is_browser_runtime_fatal_failure(result):
                            fatal_error = f"共享浏览器池失去响应，任务终止: {result}"
                        elif _is_browser_infrastructure_failure(result):
                            browser_infra_failures_since_success += 1
                            if (
                                browser_infra_failures_since_success
                                >= browser_infra_failure_limit
                            ):
                                fatal_error = (
                                    "共享浏览器连续崩溃，已触发熔断，任务终止："
                                    f"连续 {browser_infra_failures_since_success} 次失败"
                                )
                                logger.log(fatal_error, level="error")
                    logger.set_progress(completed, 0)
                    if (
                        not logger.is_cancel_requested()
                        and not fatal_error
                    ):
                        pending.add(pool.submit(do_one, next_index))
                        next_index += 1
                if fatal_error:
                    break
    except Exception as exc:
        logger.log(f"Registration runtime error: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    validation_ids = sorted(
        {
            int(account_id)
            for account_id in list(registered_account_ids or [])
            if int(account_id or 0) > 0
        }
    )
    logger.set_result_data({
        "success": success,
        "fail": failure_count,
        "unlimited": True,
        "account_ids": [item["account_id"] for item in registered_accounts],
        "accounts": registered_accounts,
    })
    if fatal_error:
        logger.finish(TASK_STATUS_FAILED, error=fatal_error)
        return
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    _schedule_post_registration_recheck(logger, validation_ids)
    logger.finish(TASK_STATUS_SUCCEEDED, error=first_error if success == 0 else "")


def _run_pulse_registration(
    do_one: Callable[..., dict[str, Any] | str],
    *,
    registration_allocator,
    count: int,
    logger: TaskLogger,
    payload: dict[str, Any],
    registered_account_ids: list[int] | None = None,
) -> None:
    """Run a register task in synchronized pulses with per-node ban probes.

    Each wave respects the requested ``concurrency`` and the available healthy
    node/slot count.  The controller is responsible for sizing waves, pausing
    banned nodes, applying worker results as they finish, and running probes.
    """
    from application.registration_pulse import PulseConfig, PulseRegistration

    config = PulseConfig.from_payload(payload)
    controller = PulseRegistration(
        do_one=do_one,
        allocator=registration_allocator,
        config=config,
        logger=logger,
        count=count,
    )
    controller.run()
    validation_ids = sorted(
        {
            int(account_id)
            for account_id in list(registered_account_ids or [])
            if int(account_id or 0) > 0
        }
    )
    _schedule_post_registration_recheck(logger, validation_ids)


def _upload_registered_chatgpt_account_to_sub2api(
    account_id: int,
    *,
    sub2api_url: str,
    api_key: str,
) -> dict:
    """Send exactly one newly saved Agent Identity to Sub2API."""
    from application.account_exports import AccountExportsService
    from domain.accounts import AccountExportSelection

    return AccountExportsService().upload_chatgpt_agent_identity_to_sub2api(
        AccountExportSelection(
            platform="chatgpt",
            ids=[int(account_id)],
            select_all=False,
        ),
        sub2api_url=sub2api_url,
        api_key=api_key,
    )


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    count = max(int(payload.get("count", 1) or 0), 0)
    concurrency = _registration_concurrency(payload.get("concurrency", 1), count)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    proxy_node = str(payload.get("proxy_node") or "").strip()
    use_proxy_pool = bool(payload.get("proxy_pool"))
    proxy_api_url = str(payload.get("proxy_api_url") or "").strip() or None
    extra = dict(payload.get("extra") or {})
    # Re-assert the invariant at execution time as well, so queued/legacy API
    # payloads cannot bypass mandatory TOTP activation.
    extra["bind_totp_2fa"] = True
    payload = {**payload, "extra": extra}
    task_id = str(getattr(logger, "task_id", "") or "")

    # Manual camoufox HAR-capture mode: open a real browser, the operator
    # registers by hand, and the HAR is saved for template extraction.
    if bool(payload.get("har_capture")):
        from platforms.chatgpt.har_capture import default_capture_path, open_capture_browser

        har_path = str(payload.get("har_path") or "").strip() or default_capture_path(
            f"register-{task_id}"
        )
        proxy = str(payload.get("proxy") or "").strip() or None
        # For 2FA binding capture: after registration the operator navigates to
        # the OpenAI security settings and binds TOTP 2FA manually, so do NOT
        # auto-open Codex OAuth (which would take them down the RT path instead).
        capture_2fa = bool(payload.get("har_capture_2fa"))
        followup_url = ""
        if not capture_2fa:
            try:
                from platforms.chatgpt.constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE
                from platforms.chatgpt.oauth import generate_oauth_url

                followup_url = generate_oauth_url(
                    redirect_uri=CODEX_REDIRECT_URI,
                    scope=CODEX_SCOPE,
                    client_id=CODEX_CLIENT_ID,
                ).auth_url
            except Exception:
                followup_url = ""
        logger.log(
            "抓包模式已开启：打开 camoufox 浏览器，请在页面内手动完成注册"
            + ("并绑定 2FA（两步验证）" if capture_2fa else "")
        )
        if followup_url:
            logger.log("ChatGPT 注册会话建立后会自动打开 Codex OAuth 授权以获取 RT")
        try:
            result = open_capture_browser(
                har_path=har_path,
                proxy=proxy,
                followup_url=followup_url,
                log=lambda message, **kw: logger.log(message, **kw),
            )
        except Exception as exc:
            logger.log(f"抓包浏览器异常: {exc}", level="error")
            logger.finish(TASK_STATUS_FAILED, error=f"抓包浏览器异常: {exc}")
            return
        logger.log(f"HAR 已保存: {result.get('har_path')}")
        logger.set_result_data({"capture": "har", "har_path": result.get("har_path")})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    sub2api_upload_enabled = bool(extra.get("auto_upload_sub2api_agent_identity"))
    sub2api_upload_config = (
        get_register_sub2api_upload_config(task_id)
        if sub2api_upload_enabled and task_id
        else None
    )
    logger.set_progress(0, count)
    registration_allocator = None
    # Rotating-residential-proxy mode: every worker draws a fresh IP from the
    # extract API and the platform swaps to a new IP on Cloudflare challenges.
    dynamic_proxy = None
    if proxy_api_url:
        from core.dynamic_proxy import DynamicProxyManager

        dynamic_proxy = DynamicProxyManager(proxy_api_url)
        logger.log(
            f"动态 IP 模式已启用: 每个 worker 从提取 API 获取轮换住宅 IP "
            f"(挑战时自动更换新 IP)"
        )
    if proxy_node or use_proxy_pool:
        try:
            from core.mihomo_client import mihomo_client

            registration_allocator = mihomo_client.create_registration_allocator(
                preferred_node=proxy_node or None,
                preflight=True,
            )
            preflight_results = dict(
                getattr(registration_allocator, "preflight_results", {}) or {}
            )
            rejected_nodes = [
                node
                for node, result in preflight_results.items()
                if not bool(result.get("eligible"))
            ]
            logger.log(
                f"Mihomo multi-exit pool ready: "
                f"{registration_allocator.node_count} nodes, "
                f"{registration_allocator.slot_count} worker slots; "
                f"preferred={proxy_node or 'auto'}; "
                f"ChatGPT preflight rejected={len(rejected_nodes)}"
            )
            if rejected_nodes:
                logger.log(
                    "ChatGPT 预检已排除节点: "
                    + ", ".join(rejected_nodes[:30]),
                    level="warning",
                )
        except Exception as exc:
            logger.log(f"代理节点启用失败: {exc}", level="error")
            logger.finish(TASK_STATUS_FAILED, error=f"代理节点启用失败: {exc}")
            return
    resolved_proxy = (
        (dynamic_proxy.get_proxy() if dynamic_proxy else None)
        or _resolve_registration_proxy_for_platform(
            platform_name,
            explicit_proxy=explicit_proxy,
            proxy_getter=proxy_pool.get_next,
        )
    )
    if sub2api_upload_enabled and not sub2api_upload_config:
        logger.log(
            "已启用 Sub2API 自动上传，但当前任务缺少临时上传凭据；注册账号不会上传",
            level="warning",
        )
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            shared_mailbox = create_mailbox(
                provider=extra.get("mail_provider", ""),
                extra=extra,
                proxy=resolved_proxy,
            )
            test_connection = getattr(shared_mailbox, "test_connection", None)
            if callable(test_connection):
                logger.log("正在检查邮箱服务连接...")
                test_connection()
                logger.log("邮箱服务连接正常")
    except Exception as exc:
        logger.log(f"邮箱服务不可用，注册任务未启动: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱服务不可用: {exc}")
        return

    upload_lock = threading.Lock()
    account_save_lock = threading.Lock()
    registered_account_ids: list[int] = []
    uploaded_count = 0
    upload_failures: list[str] = []

    def _do_one(
        index: int,
        *,
        forced_node: str | None = None,
        otp_timeout_seconds: int | None = None,
        retry_otp_once: bool = True,
        report_no_email: bool = True,
        worker_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | str:
        nonlocal uploaded_count
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        if worker_context is not None:
            worker_context["otp_received"] = False

        def _mark_otp_received() -> None:
            if worker_context is not None:
                worker_context["otp_received"] = True

        logger.set_subtask(f"worker_{index + 1}", f"Worker {index + 1}")
        lease = None
        worker_proxy = resolved_proxy
        platform = None
        try:
            logger.log(f"开始注册第 {index + 1}/{count} 个账号")
            # Dynamic IP mode: draw a fresh rotating-residential IP for this worker.
            if dynamic_proxy is not None and forced_node is None:
                fresh = dynamic_proxy.get_proxy()
                if fresh:
                    worker_proxy = fresh
                if worker_context is not None:
                    worker_context["node"] = "dynamic-ip"
                    worker_context["index"] = index
            if worker_proxy:
                logger.log(f"使用代理: {worker_proxy}")
            if registration_allocator is not None:
                from core.mihomo_client import MihomoNodeError

                try:
                    lease = (
                        registration_allocator.acquire_node(forced_node)
                        if forced_node
                        else registration_allocator.acquire()
                    )
                except MihomoNodeError:
                    registration_allocator.refresh_nodes()
                    lease = (
                        registration_allocator.acquire_node(forced_node)
                        if forced_node
                        else registration_allocator.acquire()
                    )
                worker_proxy = lease.proxy
                logger.log(
                    f"worker {index + 1} fixed Mihomo slot {lease.slot:02d} "
                    f"node={lease.node} proxy={worker_proxy}"
                )
                if worker_context is not None:
                    worker_context["node"] = lease.node
                    worker_context["index"] = index
            elif worker_proxy:
                logger.log(f"proxy={worker_proxy}")
            account = None
            # Pulse mode never retries the OTP inside one worker: a banned IP
            # would just burn a second mailbox.  Recovery happens across waves
            # and through the node probe instead.
            registration_attempts = (
                1 if (email is None and not retry_otp_once) else (2 if email is None else 1)
            )
            for registration_attempt in range(registration_attempts):
                platform = _build_platform_instance(
                    platform_name,
                    payload,
                    logger,
                    resolved_proxy=worker_proxy,
                    shared_mailbox=shared_mailbox,
                    proxy_rotate_callback=(
                        None
                        if forced_node
                        else (
                            (dynamic_proxy.get_proxy if dynamic_proxy is not None else None)
                            or (lease.rotate if lease is not None else None)
                        )
                    ),
                    otp_timeout_seconds=otp_timeout_seconds,
                    otp_received_callback=(
                        _mark_otp_received if worker_context is not None else None
                    ),
                )
                try:
                    account = platform.register(email=email, password=password)
                    break
                except TimeoutError as exc:
                    retryable_otp_timeout = "等待验证码超时" in str(exc)
                    can_retry = (
                        retryable_otp_timeout
                        and registration_attempt + 1 < registration_attempts
                        and not logger.is_cancel_requested()
                    )
                    if not can_retry:
                        raise
                    logger.log(
                        "邮箱验证码等待超时，正在更换新邮箱重试一次",
                        level="warning",
                    )
            if account is None:
                raise RuntimeError("协议注册未返回账号结果")
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            if not _access_token_for_account(account):
                raise RuntimeError("注册未返回访问令牌，账号未入库")
            logger.log(f"正在校验 {account.email} 的 access token 是否返回 401")
            access_token_check = _check_newly_registered_chatgpt_account(
                account,
                proxy=worker_proxy,
            )
            access_token_state = str(access_token_check.get("state") or "unknown")
            access_token_message = str(access_token_check.get("message") or "")
            if access_token_state == "invalid":
                raise RuntimeError(f"注册账号 401 验活失败：{access_token_message or 'access token 已失效'}")
            if access_token_state == "valid":
                logger.log(f"{account.email} 的 access token 401 验活正常")
            else:
                logger.log(
                    f"{account.email} 的 access token 验活未确认，注册结果仍保存："
                    f"{access_token_message or '未知响应'}",
                    level="warning",
                )
            account_extra = dict(getattr(account, "extra", {}) or {})
            account.extra = account_extra
            password_confirmed = bool(
                account_extra.pop("_registration_password_confirmed", False)
            )
            if not password_confirmed:
                raise RuntimeError(
                    "注册结果未确认 OpenAI 端密码已设置，账号未保存"
                )
            registration_proxy = str(
                account_extra.pop("_registration_proxy", "") or ""
            ).strip()
            browser_totp_error = str(
                account_extra.pop("_registration_totp_error", "") or ""
            ).strip()
            totp_result: dict[str, Any] = {
                "requested": bool(extra.get("bind_totp_2fa")),
                "bound": False,
                "error": "",
            }
            if totp_result["requested"]:
                totp_secret = str(account_extra.get("totp_secret") or "").strip()
                if totp_secret:
                    totp_result["bound"] = True
                    logger.log(
                        f"{account.email} 已在注册浏览器会话内完成 TOTP 2FA 绑定"
                    )
                else:
                    if browser_totp_error:
                        logger.log(
                            "浏览器会话内 TOTP 绑定未完成，正在使用同一登录态协议重试："
                            f"{browser_totp_error}",
                            level="warning",
                        )
                    else:
                        logger.log(f"正在为 {account.email} 绑定 TOTP 2FA...")
                    try:
                        totp_secret = _bind_registered_account_totp(
                            account,
                            proxy=registration_proxy or worker_proxy,
                        )
                    except Exception as mfa_exc:
                        totp_result["error"] = str(mfa_exc)[:200]
                        raise RuntimeError(
                            f"{account.email} TOTP 2FA 绑定失败，账号未保存："
                            f"{totp_result['error']}"
                        ) from mfa_exc
                    account_extra["totp_secret"] = totp_secret
                    totp_result["bound"] = True
                    logger.log(f"{account.email} TOTP 2FA 绑定成功，secret 将随账号保存")
            with account_save_lock:
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                saved_account = save_account(account)
                record_registered_email(account.platform, account.email)
                if int(saved_account.id or 0) > 0:
                    registered_account_ids.append(int(saved_account.id))
            identity_committer = getattr(platform, "commit_registration_identity", None)
            if callable(identity_committer):
                identity_committer()
            saved_account_id = int(saved_account.id)
            if resolved_proxy and registration_allocator is None:
                proxy_pool.report_success(resolved_proxy)
            if sub2api_upload_config:
                logger.log(f"正在上传 {account.email} 的 Agent Identity 到 Sub2API")
                try:
                    _upload_registered_chatgpt_account_to_sub2api(
                        saved_account_id,
                        sub2api_url=sub2api_upload_config["sub2api_url"],
                        api_key=sub2api_upload_config["api_key"],
                    )
                except Exception as upload_exc:
                    upload_error = str(upload_exc)
                    with upload_lock:
                        upload_failures.append(upload_error)
                    logger.log(
                        f"{account.email} 的 Agent Identity 上传失败：{upload_error}",
                        level="error",
                    )
                else:
                    with upload_lock:
                        uploaded_count += 1
                    logger.log(f"{account.email} 的 Agent Identity 已上传到 Sub2API")
            logger.record_success()
            logger.log(f"注册成功: {account.email}")
            registration_result: dict[str, Any] = {
                "account_id": saved_account_id,
                "email": account.email,
            }
            if totp_result["requested"]:
                registration_result["totp_2fa"] = totp_result
            return registration_result
        except Exception as exc:
            # Proxy rotation (e.g. Cloudflare) changes the node behind this
            # worker's slot; keep the pulse controller's node attribution
            # pointing at the node the failure actually happened on.
            if worker_context is not None and lease is not None:
                worker_context["node"] = lease.node
            error = str(exc)
            if "本地微软邮箱池已用尽" in error:
                # The pool has no more addresses: no further account can ever be
                # created, so tell the controller to stop the whole task instead
                # of spinning out one pool-exhaustion failure per worker.
                logger.log(f"本地微软邮箱池已用尽，注册任务终止: {error[:120]}", level="error")
                return _POOL_EXHAUSTED_MARKER
            if (
                not report_no_email
                and isinstance(exc, TimeoutError)
                and "等待验证码超时" in str(exc)
            ):
                # The node IP is at fault, not the proxy: surface a dedicated
                # marker so the pulse controller can requeue and pause the node.
                logger.log(f"注册等待验证码超时（将在下一波重试）: {exc}", level="warning")
                return _NO_EMAIL_MARKER
            if resolved_proxy and registration_allocator is None:
                proxy_pool.report_fail(resolved_proxy)
            logger.record_error(error)
            logger.log(f"注册失败: {error}", level="error")
            return error
        finally:
            identity_releaser = getattr(platform, "release_registration_identity", None)
            if callable(identity_releaser):
                try:
                    identity_releaser()
                except Exception:
                    pass
            if lease is not None:
                lease.release()
            logger.clear_subtask()

    pulse_enabled = bool(payload.get("pulse", True)) and registration_allocator is not None
    if pulse_enabled:
        _run_pulse_registration(
            _do_one,
            registration_allocator=registration_allocator,
            count=count,
            logger=logger,
            payload=payload,
            registered_account_ids=registered_account_ids,
        )
        return

    if count == 0:
        _run_unlimited_registration(
            _do_one,
            concurrency=concurrency,
            logger=logger,
            registered_account_ids=registered_account_ids,
        )
        return

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending = {pool.submit(_do_one, index) for index in range(count)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    completed += 1
                    if isinstance(result, dict):
                        success += 1
                        if len(registered_accounts) < MAX_TASK_ACCOUNT_SUMMARIES:
                            registered_accounts.append(result)
                    elif result != "__cancel_requested__":
                        errors.append(str(result))
                    logger.set_progress(completed, count)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in registered_accounts],
        "accounts": registered_accounts,
        "auto_upload_sub2api_agent_identity": sub2api_upload_enabled,
    }
    if sub2api_upload_enabled:
        result_data["sub2api_agent_identity_upload"] = {
            "submitted": uploaded_count,
            "failed": len(upload_failures),
            "errors": upload_failures,
        }
    logger.set_result_data(result_data)
    logger.log(f"完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    _schedule_post_registration_recheck(logger, registered_account_ids)
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
    )
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _account_ids_for_platform(
    platform: str,
    account_ids: list[int] | None = None,
) -> list[int]:
    with Session(engine) as session:
        statement = select(AccountModel.id).order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        if platform:
            statement = statement.where(AccountModel.platform == platform)
        normalized_ids = sorted(
            {
                int(account_id)
                for account_id in list(account_ids or [])
                if int(account_id or 0) > 0
            }
        )
        if normalized_ids:
            statement = statement.where(AccountModel.id.in_(normalized_ids))
        return [int(account_id) for account_id in session.exec(statement).all() if account_id]


def _schedule_post_registration_recheck(
    logger: Any,
    account_ids: list[int],
) -> dict[str, Any] | None:
    """Queue a durable delayed browser check for newly saved accounts."""
    normalized_ids = sorted(
        {
            int(account_id)
            for account_id in list(account_ids or [])
            if int(account_id or 0) > 0
        }
    )
    task_id = str(getattr(logger, "task_id", "") or "").strip()
    if not normalized_ids or not task_id or bool(logger.is_cancel_requested()):
        return None
    try:
        delay = float(
            os.getenv(
                "POST_REGISTER_RECHECK_DELAY_SECONDS",
                str(POST_REGISTER_RECHECK_DELAY_SECONDS),
            )
        )
    except (TypeError, ValueError):
        delay = float(POST_REGISTER_RECHECK_DELAY_SECONDS)
    delay = min(max(delay, 0.0), 3600.0)
    not_before = datetime.fromtimestamp(
        time.time() + delay,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    task = create_refresh_token_check_task(
        platform="chatgpt",
        concurrency=min(DEFAULT_REFRESH_TOKEN_CHECK_CONCURRENCY, len(normalized_ids)),
        browser=True,
        account_ids=normalized_ids,
        not_before=not_before,
        schedule_source="post_registration_401_check",
    )
    logger.log(
        f"已安排注册后延迟浏览器复验：{len(normalized_ids)} 个账号，"
        f"约 {int(delay)} 秒后执行（任务 {task['task_id']}）",
        event_type="progress",
    )
    try:
        from services.task_runtime import task_runtime

        task_runtime.wake_up()
    except Exception:
        pass
    return task


def _update_credential_check_status(
    account_id: int,
    *,
    summary_updates: dict[str, Any],
    credential_updates: dict[str, Any] | None = None,
) -> tuple[str, str]:
    # SQLite has one writer.  Network work runs in parallel, while graph
    # writes pass through this short critical section to avoid lock storms.
    with _credential_check_write_lock:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                raise ValueError("账号不存在")
            email = model.email
            platform = model.platform
            patch_account_graph(
                session,
                model,
                summary_updates=summary_updates,
                credential_updates=credential_updates,
            )
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
    return platform, email


def _delete_account_after_failed_relogin(
    account_id: int,
    *,
    email: str,
    message: str,
) -> dict[str, Any]:
    """Delete an account only after protocol login explicitly confirms a ban."""
    with _credential_check_write_lock:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return {
                    "account_id": account_id,
                    "email": email,
                    "state": "gone",
                    "message": "账号已不存在",
                }
            from core.account_graph import purge_account_graph

            current_email = model.email
            purge_account_graph(session, account_id)
            session.delete(model)
            session.commit()
    return {
        "account_id": account_id,
        "email": current_email,
        "state": "deleted",
        "message": message,
    }


def _run_single_refresh_token_check(
    account_id: int,
    *,
    timeout_seconds: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    event_callback: Callable[[str], None] | None = None,
    login_proxy: str | None = None,
    login_proxy_rotate_callback: Callable[[], str | None] | None = None,
    check_proxy: str | None = None,
    browser_fetch: Callable[..., dict[str, Any]] | None = None,
    browser_login: Callable[..., dict[str, Any]] | None = None,
    verify_only: bool = False,
    force_recovery: bool = False,
) -> dict[str, Any]:
    from platforms.chatgpt.credential_checks import (
        check_chatgpt_access_token,
        login_chatgpt_with_protocol,
    )

    timeout_seconds = max(
        float(timeout_seconds or REFRESH_TOKEN_CHECK_ACCOUNT_TIMEOUT_SECONDS),
        1.0,
    )
    deadline = time.monotonic() + timeout_seconds
    is_cancelled = cancel_check or (lambda: False)

    def remaining(maximum: float) -> float:
        return max(min(deadline - time.monotonic(), maximum), 0.0)

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        account = build_platform_account(session, model)
    extra = dict(account.extra or {})
    access_token = str(
        extra.get("access_token")
        or extra.get("accessToken")
        or account.token
        or ""
    )
    account_id_for_check = str(
        extra.get("chatgpt_account_id")
        or extra.get("account_id")
        or account.user_id
        or ""
    )
    check_method = "access_token"
    relogin_status = ""
    login_required = False
    login_attempted = False
    login_succeeded = False
    recovery_state = ""
    has_codex_token = bool(extra.get("refresh_token") or getattr(account, "refresh_token", None)) or (
        str(extra.get("client_id") or "") == "app_EMoamEEZ73f0CkXaXp7hrann"
    )
    result = (
        {
            "state": "invalid",
            "message": "浏览器第一阶段已确认 access token 返回 401，直接进入恢复登录",
        }
        if force_recovery
        else check_chatgpt_access_token(
            access_token,
            proxy=check_proxy,
            account_id=account_id_for_check,
            timeout_seconds=max(remaining(30), 1.0),
            browser_fetch=browser_fetch,
            verify_codex=has_codex_token,
        )
    )
    state = str(result.get("state") or "unknown")
    initial_at_valid = (state == "valid")
    has_refresh_token = bool(
        str(extra.get("refresh_token") or extra.get("refreshToken") or getattr(account, "refresh_token", None) or "").strip()
    )
    has_login_credentials = bool(account.password) or bool(extra.get("totp_secret")) or bool(extra.get("provider_accounts"))
    missing_codex_rt = (not has_refresh_token) and has_login_credentials

    # This task is a saved-AT liveness check. A stored RT must never bypass
    # the AT check: an explicit HTTP 401/403 always enters protocol login.
    if verify_only:
        codex_status = str(result.get("codex_status") or ("valid" if result.get("codex_valid") else ("invalid" if state == "invalid" else "unknown")))
        web_status = str(result.get("web_status") or ("valid" if result.get("web_valid") else ("invalid" if state == "invalid" else "unknown")))
        _update_credential_check_status(
            account_id,
            summary_updates={
                "refresh_token_status": state,
                "valid": state == "valid",
                "codex_status": codex_status,
                "web_status": web_status,
                "refresh_token_checked_at": _utcnow_iso(),
                "refresh_token_check_message": str(result.get("message") or ""),
                "refresh_token_check_method": "browser_access_token",
                "relogin_status": "",
            },
        )
        login_needed = (state in {"invalid", "missing"}) or missing_codex_rt
        return {
            "account_id": account_id,
            "email": account.email,
            "state": state,
            "message": str(result.get("message") or ""),
            "login_required": login_needed,
            "login_attempted": False,
            "login_succeeded": False,
            "recovery_state": "",
        }

    needs_recovery = (state in {"invalid", "missing"}) or (missing_codex_rt and not verify_only)

    if needs_recovery:
        login_required = True
        if event_callback:
            if state in {"invalid", "missing"}:
                event_callback(
                    f"{account.email}: {str(result.get('message') or 'AT 已失效')}，开始恢复"
                )
            else:
                event_callback(
                    f"{account.email}: 网页会话正常但缺少 Codex Refresh Token，自动启动补齐换证流程"
                )
        if is_cancelled():
            return {
                "account_id": account_id,
                "email": account.email,
                "state": "unknown",
                "message": "任务已取消，未执行协议登录",
                "login_required": True,
                "login_attempted": False,
                "login_succeeded": False,
                "recovery_state": "cancelled",
            }

        # 优先通道：如果账号保存了 Refresh Token，先尝试直接用 RT 刷新换取新 AT（规避网页端人机验证）
        recovery = None
        refresh_token = str(
            extra.get("refresh_token")
            or extra.get("refreshToken")
            or ""
        ).strip()
        client_id = str(
            extra.get("client_id")
            or extra.get("clientId")
            or ""
        ).strip()

        if refresh_token:
            login_attempted = True
            if event_callback:
                event_callback(f"{account.email}: 检测到保存的 Refresh Token，优先尝试 RT 刷新换取新 AT...")
            from platforms.chatgpt.credential_checks import refresh_chatgpt_tokens
            rt_proxy = login_proxy
            rt_result = refresh_chatgpt_tokens(
                refresh_token,
                client_id=client_id,
                proxy=rt_proxy,
                timeout_seconds=min(remaining(30), 20.0),
            )
            # 遭遇 429 限流时尝试轮换干净节点重试
            if rt_result.get("state") == "unknown" and any(m in str(rt_result.get("message") or "").lower() for m in ("429", "rate_limit", "rate limit")):
                for _ in range(2):
                    if callable(login_proxy_rotate_callback):
                        new_proxy = login_proxy_rotate_callback()
                        if new_proxy:
                            rt_proxy = new_proxy
                            rt_result = refresh_chatgpt_tokens(
                                refresh_token,
                                client_id=client_id,
                                proxy=rt_proxy,
                                timeout_seconds=min(remaining(30), 20.0),
                            )
                            if rt_result.get("state") == "valid":
                                break

            if rt_result.get("state") == "valid" and rt_result.get("tokens", {}).get("access_token"):
                if event_callback:
                    event_callback(f"{account.email}: RT 刷新成功，直接换取到新 AT（已规避网页人机挑战）")
                recovery = rt_result
                recovery["recovery_method"] = "refresh_token"
            else:
                rt_msg = str(rt_result.get("message") or "RT 刷新未通过")
                if event_callback:
                    event_callback(f"{account.email}: RT 刷新未通过 ({rt_msg})，回退到密码与 2FA 协议登录")

        if recovery is None:
            login_timeout = remaining(REFRESH_TOKEN_CHECK_ACCOUNT_TIMEOUT_SECONDS)
            if login_timeout <= 0:
                recovery = {
                    "state": "invalid",
                    "message": f"协议登录超过单账号总时限 ({int(timeout_seconds)}s)",
                    "tokens": {},
                }
            else:
                login_attempted = True
                recovery = login_chatgpt_with_protocol(
                    account.email,
                    account.password,
                    provider_accounts=list(extra.get("provider_accounts") or []),
                    totp_secret=str(extra.get("totp_secret") or "").strip(),
                    proxy=login_proxy,
                    timeout_seconds=login_timeout,
                    cancel_check=is_cancelled,
                    log_callback=event_callback,
                    proxy_rotate_callback=login_proxy_rotate_callback,
                )
            recovery_message = str(recovery.get("message") or "")
            recovery_lower = recovery_message.lower()
            protocol_lacks_rt = (
                recovery.get("state") == "valid"
                and not bool(recovery.get("tokens", {}).get("refresh_token"))
            )
            if (
                (recovery.get("state") not in {"valid", "banned", "missing_mailbox"} or protocol_lacks_rt)
                and not is_cancelled()
                and (callable(browser_login) or str(extra.get("totp_secret") or "").strip())
            ):
                active_browser_login = browser_login
                standalone_pool = None
                if not callable(active_browser_login):
                    try:
                        from platforms.chatgpt.browser_verify import BrowserFetchPool

                        standalone_pool = BrowserFetchPool(
                            headless=True,
                            proxy=login_proxy or None,
                            concurrency=1,
                        )
                        standalone_pool.__enter__()
                        active_browser_login = getattr(standalone_pool, "browser_login", None)
                    except Exception as pool_err:
                        if event_callback:
                            event_callback(f"无法初始化 Camoufox 降级登录: {pool_err}")
                if callable(active_browser_login):
                    if event_callback:
                        reason = "协议登录缺少 Refresh Token" if protocol_lacks_rt else f"协议登录未成功（{recovery_message}）"
                        event_callback(
                            f"{account.email}: {reason}，切换 Camoufox 执行密码 + 邮箱/TOTP 登录"
                        )
                    try:
                        browser_recovery = active_browser_login(
                            account.email,
                            account.password,
                            str(extra.get("totp_secret") or "").strip(),
                            proxy=login_proxy,
                            log=event_callback,
                            provider_accounts=list(extra.get("provider_accounts") or []),
                        )
                        browser_message = str(browser_recovery.get("message") or "")
                        browser_lower = browser_message.lower()
                        if any(
                            marker in browser_lower
                            for marker in (
                                "account_deactivated",
                                "account_suspended",
                                "account_banned",
                            )
                        ):
                            recovery = {
                                "state": "banned",
                                "message": browser_message,
                                "confirmed_ban_code": next(
                                    marker
                                    for marker in (
                                        "account_deactivated",
                                        "account_suspended",
                                        "account_banned",
                                    )
                                    if marker in browser_lower
                                ),
                                "tokens": {},
                            }
                        elif browser_recovery.get("state") == "valid":
                            recovery = browser_recovery
                            recovery["recovery_method"] = "browser_login"
                            recovery["message"] = (
                                browser_message
                                or "browser login issued a fresh access token"
                            )
                        else:
                            recovery = browser_recovery
                            if recovery_message and recovery_message != browser_message:
                                recovery["message"] = (
                                    f"协议登录失败（{recovery_message}）；"
                                    f"浏览器登录失败（{browser_message}）"
                                )
                    finally:
                        if standalone_pool is not None:
                            try:
                                standalone_pool.__exit__(None, None, None)
                            except Exception:
                                pass
        recovery.setdefault("message", "401 recovery login did not issue fresh credentials")
        recovery_state = str(recovery.get("state") or "unknown")
        if recovery.get("state") == "valid":
            fresh_tokens = dict(recovery.get("tokens") or {})
            fresh_access_token = str(
                fresh_tokens.get("access_token")
                or fresh_tokens.get("accessToken")
                or ""
            ).strip()
            if fresh_access_token:
                # Protocol login success is not an AT liveness result. OpenAI
                # may invalidate the newly issued token before it is used.
                has_codex_token = bool(fresh_tokens.get("refresh_token")) or (
                    str(fresh_tokens.get("client_id") or "") == "app_EMoamEEZ73f0CkXaXp7hrann"
                )
                fresh_check = check_chatgpt_access_token(
                    fresh_access_token,
                    proxy=check_proxy,
                    account_id="",
                    timeout_seconds=max(remaining(30), 1.0),
                    browser_fetch=browser_fetch,
                    verify_codex=has_codex_token,
                )
                fresh_state = str(fresh_check.get("state") or "unknown")
                is_browser_token = (
                    recovery.get("recovery_method") == "browser_login"
                    or "browser login" in str(recovery.get("message") or "").lower()
                )
                if (fresh_state == "unknown" or fresh_check.get("web_valid")) and is_browser_token:
                    fresh_state = "valid"
                    recovery["message"] = f"{str(recovery.get('message') or '')}（浏览器会话已确认为有效）"

                if fresh_state != "valid":
                    recovery_state = f"fresh_at_{fresh_state or 'unknown'}"
                    recovery["state"] = (
                        fresh_state
                        if fresh_state in {"invalid", "unknown", "missing"}
                        else "invalid"
                    )
                    recovery["message"] = (
                        f"{str(recovery.get('message') or '')}；"
                        f"新 AT 二次验活未通过：{str(fresh_check.get('message') or '未确认')}"
                    ).strip("；")
                    recovery["tokens"] = {}
            else:
                recovery_state = "fresh_at_missing"
                recovery["state"] = "invalid"
                recovery["message"] = (
                    f"{str(recovery.get('message') or '')}；协议登录未返回可验活的新 AT"
                ).strip("；")
                recovery["tokens"] = {}
        if recovery.get("state") == "valid":
            result = recovery
            state = "valid"
            check_method = (
                "refresh_token_verified"
                if recovery.get("recovery_method") == "refresh_token"
                else "protocol_login_verified"
            )
            relogin_status = "recovered"
            login_succeeded = True
            if event_callback:
                method_name = "RT 刷新" if recovery.get("recovery_method") == "refresh_token" else "协议登录"
                event_callback(f"{account.email}: {method_name}成功，已获取并保存新 AT")
        elif recovery.get("state") == "cancelled" or is_cancelled():
            return {
                "account_id": account_id,
                "email": account.email,
                "state": "unknown",
                "message": str(recovery.get("message") or "任务已取消"),
                "login_required": True,
                "login_attempted": login_attempted,
                "login_succeeded": False,
                "recovery_state": "cancelled",
            }
        elif recovery_state.startswith("fresh_at_"):
            # A protocol login can succeed while the newly issued AT is
            # already rejected or cannot be checked through the network.  Do
            # not persist that token, and keep transport uncertainty separate
            # from a confirmed 401 credential failure.
            fresh_state = recovery_state.removeprefix("fresh_at_")
            state = "unknown" if fresh_state == "unknown" else "invalid"
            check_method = "protocol_login"
            relogin_status = "failed"
            if event_callback:
                event_callback(
                    f"{account.email}: 新 AT 二次验活未确认，账号保留为隔离状态："
                    f"{str(recovery.get('message') or '未确认')}"
                )
            result = {
                **result,
                "state": state,
                "message": (
                    f"{str(result.get('message') or '')}；"
                    f"{str(recovery.get('message') or '新 AT 二次验活未确认')}"
                ).strip("；"),
            }
        elif recovery.get("state") in {"banned", "missing_mailbox"}:
            confirmed_ban_code = str(
                recovery.get("confirmed_ban_code") or ""
            ).strip().lower()
            if (
                recovery.get("state") == "banned"
                and confirmed_ban_code not in CONFIRMED_CHATGPT_BAN_CODES
            ):
                state = "invalid"
                check_method = "protocol_login"
                relogin_status = "failed"
                recovery_state = "unconfirmed_ban"
                if event_callback:
                    event_callback(
                        f"{account.email}: 登录返回封禁状态但缺少明确封禁代码，账号保留："
                        f"{str(recovery.get('message') or '未确认')}"
                    )
                result = {
                    **result,
                    "state": state,
                    "message": (
                        f"{str(result.get('message') or '')}；"
                        "协议登录未提供可审计的封禁代码，账号保留"
                    ).strip("；"),
                }
            else:
                if is_cancelled():
                    return {
                        "account_id": account_id,
                        "email": account.email,
                        "state": "unknown",
                        "message": "任务已取消，未删除账号",
                        "login_required": True,
                        "login_attempted": login_attempted,
                        "login_succeeded": False,
                        "recovery_state": "cancelled",
                    }
                reason = (
                    f"确认封禁 {confirmed_ban_code}"
                    if recovery.get("state") == "banned"
                    else "缺少可复用邮箱"
                )
                evidence = str(recovery.get("message") or "").strip()
                if event_callback:
                    event_callback(
                        f"{account.email}: 协议登录结果为{reason}，删除账号"
                        f"{('：' + evidence) if evidence else ''}"
                    )
                deleted = _delete_account_after_failed_relogin(
                    account_id,
                    email=account.email,
                    message=str(
                        evidence
                        or (
                            "账号缺少可复用邮箱，无法恢复"
                            if recovery.get("state") == "missing_mailbox"
                            else f"重新登录时明确返回 {confirmed_ban_code}"
                        )
                    ),
                )
                return {
                    **deleted,
                    "login_required": True,
                    "login_attempted": login_attempted,
                    "login_succeeded": False,
                    "recovery_state": recovery_state,
                    "confirmed_ban_code": confirmed_ban_code,
                }
        else:
            # A timeout, mailbox failure, network error, or incomplete login
            # does not prove that the account is banned. Keep the account and
            # persist the failed recovery attempt for a later retry.
            rec_msg = str(recovery.get("message") or "未确认")
            relogin_status = "failed"
            if initial_at_valid:
                state = "valid"
                check_method = "browser_access_token"
                if any(m in rec_msg.lower() for m in ("rate_limit", "rate limit", "too_many_requests", "429")):
                    recovery_state = "rate_limited"
                    if event_callback:
                        event_callback(
                            f"{account.email}: 换取 Codex RT 遭遇频控限流 (429)，原网页会话仍有效，保留稍后重试"
                        )
                elif event_callback:
                    event_callback(
                        f"{account.email}: 原网页会话有效，但换取 Codex RT 未成功（{rec_msg}），账号保留"
                    )
                result = {
                    **result,
                    "state": state,
                    "message": (
                        f"{str(result.get('message') or '')}；换取 Codex RT 未成功："
                        f"{rec_msg}"
                    ).strip("；"),
                }
            else:
                state = "invalid"
                check_method = "protocol_login"
                if any(m in rec_msg.lower() for m in ("rate_limit", "rate limit", "too_many_requests", "429")):
                    recovery_state = "rate_limited"
                    if event_callback:
                        event_callback(
                            f"{account.email}: 登录遭遇频控限流 (rate_limit_exceeded)，已轮换节点重试仍受限，账号保留供稍后重试"
                        )
                elif event_callback:
                    event_callback(
                        f"{account.email}: 协议登录未成功，账号保留："
                        f"{rec_msg}"
                    )
                result = {
                    **result,
                    "state": state,
                    "message": (
                        f"{str(result.get('message') or '')}；协议重新登录失败："
                        f"{rec_msg}"
                    ).strip("；"),
                }
    if is_cancelled():
        return {
            "account_id": account_id,
            "email": account.email,
            "state": "unknown",
            "message": "任务已取消，未写入验活结果",
        }
    tokens = dict(result.get("tokens") or {})
    final_codex_status = str(
        result.get("codex_status")
        or (
            "valid"
            if state == "valid" and bool(tokens.get("refresh_token") or extra.get("refresh_token"))
            else ("invalid" if state == "invalid" else "unknown")
        )
    )
    final_web_status = str(
        result.get("web_status")
        or ("valid" if state == "valid" else ("invalid" if state == "invalid" else "unknown"))
    )
    _update_credential_check_status(
        account_id,
        summary_updates={
            "refresh_token_status": state,
            "valid": state == "valid",
            "codex_status": final_codex_status,
            "web_status": final_web_status,
            "refresh_token_checked_at": _utcnow_iso(),
            "refresh_token_check_message": str(result.get("message") or ""),
            "refresh_token_check_method": check_method,
            "relogin_status": relogin_status,
        },
        credential_updates=tokens or None,
    )
    return {
        "account_id": account_id,
        "email": account.email,
        "state": state,
        "message": result.get("message", ""),
        "login_required": login_required,
        "login_attempted": login_attempted,
        "login_succeeded": login_succeeded,
        "recovery_state": recovery_state,
    }


def _probe_chatgpt_login_route(
    proxy: str | None,
    *args: Any,
    timeout: float = 15.0,
    **kwargs: Any,
) -> tuple[bool, str]:
    from curl_cffi import requests as curl_requests

    from platforms.chatgpt.constants import CHATGPT_APP
    from platforms.chatgpt.environment_profile import PROTOCOL_CHROME_IMPERSONATE

    request_kwargs: dict[str, Any] = {
        "allow_redirects": True,
        "impersonate": PROTOCOL_CHROME_IMPERSONATE,
        "timeout": max(float(timeout), 1.0),
    }
    normalized_proxy = str(proxy or "").strip()
    if normalized_proxy:
        request_kwargs["proxies"] = {
            "http": normalized_proxy,
            "https": normalized_proxy,
        }
    try:
        response = curl_requests.get(CHATGPT_APP, **request_kwargs)
    except Exception as exc:
        detail = str(exc).replace("\n", " ").strip()
        return False, f"{exc.__class__.__name__}: {detail[:180]}"
    status_code = int(getattr(response, "status_code", 0) or 0)
    # This is a transport preflight, not an authentication/Cloudflare check.
    # Any real HTTP response proves that the route reached ChatGPT.  In
    # particular, the homepage commonly returns 403 to this lightweight
    # request while the API token check and a full protocol-login session can
    # still proceed.  Treating that 403 as an unreachable route made the whole
    # 401 maintenance task fail before it checked its first account.
    if status_code > 0:
        return True, f"HTTP {status_code}"
    return False, "未收到 HTTP 响应"


def _resolve_refresh_login_proxy(
    proxy_node: str,
    *,
    logger: TaskLogger,
) -> str | None:
    normalized_node = str(proxy_node or "").strip()
    if normalized_node:
        from core.mihomo_client import mihomo_client

        proxy = mihomo_client.activate_node(normalized_node)
        reachable, detail = _probe_chatgpt_login_route(proxy)
        if not reachable:
            raise RuntimeError(
                f"协议登录代理节点不可访问 ChatGPT：{normalized_node}（{detail}）"
            )
        logger.log(
            f"协议登录/工作区代理节点：{normalized_node}，预检通过（{detail}）；"
            "AT 主验活仍直连 api.openai.com/v1/me",
            event_type="progress",
        )
        return proxy

    fallback_proxy = str(os.getenv("MIHOMO_PROXY_URL") or "").strip()
    if fallback_proxy:
        fallback_detail = ""
        fallback_reachable, fallback_detail = _probe_chatgpt_login_route(fallback_proxy)
        if fallback_reachable:
            logger.log(
                f"协议登录/工作区线路：当前 Mihomo 节点预检通过（{fallback_detail}）；"
                "AT 主验活直连 api.openai.com/v1/me",
                event_type="progress",
            )
            return fallback_proxy

        # The selector may still point at a node whose controller health is
        # already false.  Rotate the selector through enabled healthy nodes
        # and probe each real ChatGPT route before falling back to direct.
        try:
            from core.mihomo_client import mihomo_client

            attempted_nodes: set[str] = set()
            candidates = mihomo_client.healthy_node_candidates(refresh=True)
            for candidate in candidates:
                candidate_name = str(candidate.get("name") or "").strip()
                if not candidate_name or candidate_name in attempted_nodes:
                    continue
                attempted_nodes.add(candidate_name)
                try:
                    candidate_proxy = mihomo_client.activate_node(candidate_name)
                except Exception as exc:
                    fallback_detail = f"{candidate_name}: {exc}"
                    continue
                candidate_reachable, candidate_detail = _probe_chatgpt_login_route(
                    candidate_proxy
                )
                if candidate_reachable:
                    logger.log(
                        f"当前 Mihomo 节点不可用，已自动切换到健康节点："
                        f"{candidate_name}（{candidate_detail}）；"
                        "AT 主验活直连 api.openai.com/v1/me",
                        level="warning",
                        event_type="progress",
                    )
                    return candidate_proxy
                fallback_detail = f"{candidate_name}: {candidate_detail}"
        except Exception as exc:
            fallback_detail = f"{fallback_detail or '当前节点不可达'}；健康节点选择失败: {exc}"

        # Mihomo may be in the middle of a provider reload.  Give the selected
        # route two short retries after the controller-based switch attempt.
        for attempt in range(2):
            time.sleep(1.0)
            fallback_reachable, retry_detail = _probe_chatgpt_login_route(fallback_proxy)
            fallback_detail = retry_detail or fallback_detail
            if fallback_reachable:
                logger.log(
                    f"协议登录/工作区线路：Mihomo 重试预检通过（{fallback_detail}）；"
                    "AT 主验活直连 api.openai.com/v1/me",
                    event_type="progress",
                )
                return fallback_proxy

    direct_reachable, direct_detail = _probe_chatgpt_login_route(None)
    if direct_reachable:
        if fallback_proxy and "403" in str(direct_detail):
            logger.log(
                f"直连预检返回 {direct_detail}，疑似 Cloudflare challenge；"
                f"Mihomo 预检暂时失败（{fallback_detail}），仍强制使用 Mihomo 重试协议登录",
                level="warning",
                event_type="progress",
            )
            return fallback_proxy
        logger.log(
            f"协议登录/工作区线路：直连预检通过（{direct_detail}）；"
            "AT 主验活直连 api.openai.com/v1/me",
            event_type="progress",
        )
        return None

    if not fallback_proxy:
        raise RuntimeError(
            f"协议登录直连不可用（{direct_detail}），且未配置 MIHOMO_PROXY_URL"
        )
    raise RuntimeError(
        f"协议登录 Mihomo 不可用（{fallback_detail}），直连也不可用（{direct_detail}）"
    )


def _execute_refresh_token_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from platforms.chatgpt.credential_checks import (
        protocol_login_concurrency_limit,
    )

    platform = str(payload.get("platform") or "chatgpt")
    browser_mode = bool(payload.get("browser"))
    concurrency = _bounded_concurrency(
        payload.get("concurrency"),
        default=DEFAULT_REFRESH_TOKEN_CHECK_CONCURRENCY,
        maximum=MAX_REFRESH_TOKEN_CHECK_CONCURRENCY,
    )
    login_concurrency = min(concurrency, protocol_login_concurrency_limit())
    concurrency_data = {
        "concurrency": concurrency,
        "login_concurrency": login_concurrency,
        "browser": browser_mode,
    }
    requested_account_ids = payload.get("account_ids")
    # Keep the one-argument call shape for existing integrations and test
    # doubles that provide a platform-only account lookup.
    account_ids = (
        _account_ids_for_platform(platform, requested_account_ids)
        if requested_account_ids
        else _account_ids_for_platform(platform)
    )
    total = len(account_ids)
    logger.set_progress(0, total)
    results = {
        "valid": 0,
        "invalid": 0,
        "missing": 0,
        "unknown": 0,
        "deleted": 0,
        "login_required": 0,
        "login_attempted": 0,
        "login_succeeded": 0,
        "login_failed": 0,
        "banned": 0,
        "missing_mailbox": 0,
    }
    recovered_account_ids: list[int] = []
    recovered_emails: list[str] = []
    if not account_ids:
        logger.set_result_data({**results, **concurrency_data, "recovered_account_ids": [], "recovered_emails": []})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    completed = 0
    successes = 0
    errors = 0
    cancelled = False
    progress_interval = max(total // 200, 25)
    account_timeout = REFRESH_TOKEN_CHECK_ACCOUNT_TIMEOUT_SECONDS
    proxy_node = str(payload.get("proxy_node") or "").strip()
    try:
        login_proxy = _resolve_refresh_login_proxy(proxy_node, logger=logger)
    except Exception as exc:
        error = str(exc).strip() or exc.__class__.__name__
        logger.set_result_data({**results, **concurrency_data, "route_error": error})
        logger.log(error, level="error", event_type="progress")
        logger.finish(TASK_STATUS_FAILED, error=error)
        return
    refresh_allocator = None
    if login_proxy:
        try:
            from core.mihomo_client import mihomo_client

            logger.log("正在对所有代理节点进行网络连通性测速预检（Ping）...", event_type="progress")
            mihomo_client.refresh_group_delay(url="http://www.gstatic.com/generate_204", timeout_ms=8000)
            refresh_allocator = mihomo_client.create_registration_allocator(
                preferred_node=proxy_node or None,
                preflight=True,
            )
            logger.log(
                f"401 恢复登录启用独立 Mihomo slot："
                f"{refresh_allocator.node_count} 个节点预检连通，挑战时自动轮换",
                event_type="progress",
            )
        except Exception as exc:
            logger.log(
                f"401 恢复登录使用本地代理：{login_proxy}",
                event_type="progress",
            )
    logger.log(
        f"401 验活配置：总计 {total}，AT 检查并发 {concurrency}，"
        f"协议登录并发 {login_concurrency}，"
        f"单账号最长 {account_timeout}s；AT 主验活直连 api.openai.com/v1/me，"
        "通过后再检查 ChatGPT 工作区",
        event_type="progress",
    )

    def run_account(
        account_id: int,
        verify_only: bool = False,
        force_recovery: bool = False,
    ) -> dict[str, Any]:
        lease = None
        account_proxy = login_proxy
        rotate_callback = None
        if refresh_allocator is not None and not verify_only:
            try:
                lease = refresh_allocator.acquire()
                account_proxy = lease.proxy
                rotate_callback = lease.rotate
                # 验活/恢复前先 ping 探测当前代理节点的联通性
                for probe_attempt in range(3):
                    try:
                        reachable, probe_detail = _probe_chatgpt_login_route(account_proxy, timeout=5.0)
                    except TypeError:
                        reachable, probe_detail = _probe_chatgpt_login_route(account_proxy)
                    if reachable:
                        break
                    failed_node = lease.node
                    logger.log(
                        f"账号 {account_id} 代理节点 {failed_node} 联通性异常（{probe_detail}），正在自动轮换可用节点...",
                        level="warning",
                        event_type="progress",
                        detail={"account_id": account_id},
                    )
                    lease.rotate()
                    account_proxy = lease.proxy
                logger.log(
                    f"账号 {account_id} 恢复登录使用 Mihomo slot {lease.slot:02d}，"
                    f"节点 {lease.node}（联通性预检正常）",
                    event_type="progress",
                    detail={"account_id": account_id},
                )
            except Exception as exc:
                logger.log(
                    f"账号 {account_id} 无法分配独立 Mihomo slot，使用当前代理组：{exc}",
                    level="warning",
                    event_type="progress",
                    detail={"account_id": account_id},
                )
        try:
            return _run_single_refresh_token_check(
                account_id,
                timeout_seconds=account_timeout,
                cancel_check=logger.is_cancel_requested,
                login_proxy=account_proxy,
                login_proxy_rotate_callback=rotate_callback,
                check_proxy=account_proxy,
                browser_fetch=browser_fetch,
                browser_login=browser_login,
                verify_only=verify_only,
                force_recovery=force_recovery,
                event_callback=lambda message: logger.log(
                    message,
                    event_type="progress",
                    detail={"account_id": account_id},
                ),
            )
        finally:
            if lease is not None:
                lease.release()

    # Browser verification: open one camoufox page shared by all workers and
    # run the /v1/me fetch inside it, so Cloudflare sees a real browser instead
    # of a protocol client (which spuriously returns HTTP 403).
    browser_fetch = None
    browser_login = None
    _browser_session = None
    if bool(payload.get("browser")):
        logger.log(
            f"浏览器验活已开启：Camoufox 请求池并发 {concurrency}，执行 AT 校验（规避 403）",
            event_type="progress",
        )
        try:
            from platforms.chatgpt.browser_verify import BrowserFetchPool

            _browser_session = BrowserFetchPool(
                headless=True,
                proxy=login_proxy or None,
                concurrency=concurrency,
            )
            _browser_session.__enter__()
            browser_fetch = getattr(_browser_session, "browser_fetch", None)
            # Keep browser-first verification compatible with older pool
            # implementations and test doubles that do not provide the
            # optional password+TOTP fallback callback.
            browser_login = getattr(_browser_session, "browser_login", None)
        except Exception as exc:
            logger.log(f"浏览器验活初始化失败，回退协议直连: {exc}", level="warning", event_type="progress")
            browser_fetch = None

    # Phase 1: verify every AT through Camoufox with the requested concurrency.
    # Phase 2: relogin only the accounts that are explicitly invalid/missing,
    # concurrently with the protocol (curl_cffi) login.
    # If browser startup failed, use the ordinary concurrent protocol path
    # rather than accidentally falling back to a serial browser-mode loop.
    browser_mode = browser_mode and browser_fetch is not None
    if browser_mode:
        last_progress_log = time.monotonic()
        login_ids: list[int] = []
        phase1_results = {"valid": 0, "invalid": 0, "missing": 0, "unknown": 0}
        completed = 0
        account_iter = iter(account_ids)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pending = {
                pool.submit(run_account, account_id, True): account_id
                for account_id in (
                    next(account_iter, None)
                    for _ in range(min(concurrency, total))
                )
                if account_id is not None
            }
            last_progress_log = time.monotonic()
            while pending:
                done, _ = wait(
                    set(pending),
                    timeout=REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    account_id = pending.pop(future)
                    completed += 1
                    try:
                        result = future.result()
                        state = str(result.get("state") or "unknown")
                        phase1_results[state if state in phase1_results else "unknown"] += 1
                        if result.get("login_required"):
                            results["login_required"] += 1
                            login_ids.append(account_id)
                        if state == "valid":
                            successes += 1
                    except Exception:
                        phase1_results["unknown"] += 1
                        errors += 1
                    next_account_id = next(account_iter, None)
                    if next_account_id is not None and not logger.is_cancel_requested():
                        pending[pool.submit(run_account, next_account_id, True)] = next_account_id
                if (
                    completed % progress_interval == 0
                    or completed == total
                    or (
                        pending
                        and time.monotonic() - last_progress_log
                        >= REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS
                    )
                ):
                    logger.set_progress(completed, total)
                    logger.set_counts(success=successes, error=errors)
                    logger.log(
                        f"401 浏览器验活 {completed}/{total}：正常 {phase1_results['valid']}，"
                        f"失效 {phase1_results['invalid']}，已删除 0，"
                        f"未确认 {phase1_results['unknown'] + phase1_results['missing']}，"
                        f"需登录 {results['login_required']}，处理中 {len(pending)}",
                        event_type="progress",
                    )
                    last_progress_log = time.monotonic()
                if logger.is_cancel_requested():
                    for future in pending:
                        future.cancel()
                    cancelled = True
                    break
            results["valid"] = phase1_results["valid"]
            if completed % progress_interval == 0 or completed == total:
                logger.set_progress(completed, total)
                logger.set_counts(success=successes, error=errors)
                logger.log(
                    f"401 浏览器验活 {completed}/{total}：正常 {results['valid']}，"
                    f"失效 {phase1_results['invalid']}，已删除 {phase1_results.get('deleted', 0)}，"
                    f"未确认 {phase1_results['unknown'] + phase1_results['missing']}；"
                    f"需登录 {results['login_required']}",
                    event_type="progress",
                )
        # Invalid ATs are pending recovery; only final recovery results should
        # enter the terminal counters. First-pass valid/unknown results are
        # already final and are copied through now.
        results["valid"] = phase1_results["valid"]
        results["unknown"] = phase1_results["unknown"]
        # A missing AT is also recoverable through protocol login.  Do not
        # leave it in the terminal counter when the second phase succeeds.
        results["missing"] = 0
        # Phase 2: concurrent protocol relogin for accounts that returned 401/403.
        if login_ids and not logger.is_cancel_requested():
            logger.log(
                f"浏览器验活完成，对 {len(login_ids)} 个失效账号并发协议登录获取新 AT",
                event_type="progress",
            )
            def _relogin(account_id: int) -> dict[str, Any]:
                return run_account(account_id, force_recovery=True)

            with ThreadPoolExecutor(max_workers=login_concurrency) as pool:
                futures = [pool.submit(_relogin, aid) for aid in login_ids]
                recovery_completed = 0
                for future in futures:
                    if logger.is_cancel_requested():
                        cancelled = True
                        break
                    try:
                        result = future.result()
                        state = str(result.get("state") or "unknown")
                        results[state if state in results else "unknown"] += 1
                        if result.get("login_attempted"):
                            results["login_attempted"] += 1
                        if result.get("login_attempted") and not result.get("login_succeeded"):
                            recovery_state = str(result.get("recovery_state") or "")
                            if recovery_state not in {"banned", "missing_mailbox", "cancelled"}:
                                results["login_failed"] += 1
                        if result.get("login_succeeded"):
                            results["login_succeeded"] += 1
                            rec_id = result.get("account_id")
                            if rec_id and rec_id not in recovered_account_ids and state == "valid":
                                recovered_account_ids.append(rec_id)
                                if result.get("email"):
                                    recovered_emails.append(str(result.get("email")))
                        recovery_state = str(result.get("recovery_state") or "")
                        if recovery_state == "banned":
                            results["banned"] += 1
                        elif recovery_state == "missing_mailbox":
                            results["missing_mailbox"] += 1
                        if state == "valid":
                            successes += 1
                    except Exception as exc:
                        results["unknown"] += 1
                        errors += 1
                    recovery_completed += 1
                    if (
                        recovery_completed % max(len(login_ids) // 20, 1) == 0
                        or recovery_completed == len(login_ids)
                    ):
                        logger.set_progress(total, total)
                        logger.set_counts(success=successes, error=errors)
                        logger.log(
                            f"401 重登录 {recovery_completed}/{len(login_ids)}：正常 {results['valid']}，"
                            f"失效 {results['invalid']}，已删除 {results['deleted']}，"
                            f"未确认 {results['unknown'] + results['missing']}；"
                            f"登录成功 {results['login_succeeded']}，"
                            f"确认封禁 {results['banned']}",
                            event_type="progress",
                        )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            account_iter = iter(account_ids)
            pending = {
                pool.submit(run_account, account_id)
                for account_id in (next(account_iter, None) for _ in range(min(concurrency, total)))
                if account_id is not None
            }
            last_progress_log = time.monotonic()

            def log_progress(*, heartbeat: bool = False) -> None:
                nonlocal last_progress_log
                logger.set_progress(completed, total)
                logger.set_counts(success=successes, error=errors)
                suffix = f"，处理中 {len(pending)}" if heartbeat and pending else ""
                logger.log(
                    f"401 验活 {completed}/{total}：正常 {results['valid']}，"
                    f"失效 {results['invalid']}，已删除 {results['deleted']}，"
                    f"未确认 {results['unknown'] + results['missing']}；"
                    f"需登录 {results['login_required']}，已尝试 {results['login_attempted']}，"
                    f"登录成功 {results['login_succeeded']}，登录失败 {results['login_failed']}，"
                    f"确认封禁 {results['banned']}，缺邮箱 {results['missing_mailbox']}"
                    f"{suffix}",
                    event_type="progress",
                )
                last_progress_log = time.monotonic()

            while pending:
                done, pending = wait(
                    pending,
                    timeout=REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    completed += 1
                    try:
                        result = future.result()
                        state = str(result.get("state") or "unknown")
                        results[state if state in results else "unknown"] += 1
                        if result.get("login_required"):
                            results["login_required"] += 1
                        if result.get("login_attempted"):
                            results["login_attempted"] += 1
                        if result.get("login_succeeded"):
                            results["login_succeeded"] += 1
                            rec_id = result.get("account_id")
                            if rec_id and rec_id not in recovered_account_ids and state == "valid":
                                recovered_account_ids.append(rec_id)
                                if result.get("email"):
                                    recovered_emails.append(str(result.get("email")))
                        recovery_state = str(result.get("recovery_state") or "")
                        if result.get("login_attempted") and not result.get("login_succeeded"):
                            if recovery_state not in {"banned", "missing_mailbox", "cancelled"}:
                                results["login_failed"] += 1
                        if recovery_state == "banned":
                            results["banned"] += 1
                        elif recovery_state == "missing_mailbox":
                            results["missing_mailbox"] += 1
                        if state == "valid":
                            successes += 1
                    except Exception as exc:
                        results["unknown"] += 1
                        errors += 1
                    if completed % progress_interval == 0 or completed == total:
                        log_progress()
                    next_account_id = next(account_iter, None)
                    if next_account_id is not None and not logger.is_cancel_requested():
                        pending.add(pool.submit(run_account, next_account_id))
                if (
                    pending
                    and time.monotonic() - last_progress_log >= REFRESH_TOKEN_CHECK_HEARTBEAT_SECONDS
                ):
                    log_progress(heartbeat=True)
                if logger.is_cancel_requested():
                    for future in pending:
                        future.cancel()
                    cancelled = True
                    break
    # Release the shared camoufox browser session (if one was opened).
    try:
        if _browser_session is not None:
            _browser_session.__exit__(None, None, None)
    except Exception:
        pass
    if cancelled:
        logger.set_result_data({
            **results,
            **concurrency_data,
            "recovered_account_ids": recovered_account_ids,
            "recovered_emails": recovered_emails,
            "recovered_count": len(recovered_account_ids),
        })
        logger.set_progress(completed, total)
        logger.set_counts(success=successes, error=errors)
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    unresolved = int(
        results["invalid"]
        + results["unknown"]
        + results["missing"]
    )
    logger.set_result_data({
        **results,
        **concurrency_data,
        "unresolved": unresolved,
        "recovered_account_ids": recovered_account_ids,
        "recovered_emails": recovered_emails,
        "recovered_count": len(recovered_account_ids),
    })
    if recovered_account_ids:
        logger.log(
            f"🎉 401 恢复成功：已解救并刷新 {len(recovered_account_ids)} 个账号 AT: "
            f"{', '.join(recovered_emails)}",
            event_type="progress",
        )
    logger.set_progress(completed, total)
    logger.set_counts(success=successes, error=errors + unresolved)
    all_recovery_logins_failed = (
        results["login_attempted"] > 0
        and results["login_succeeded"] == 0
        and results["deleted"] == 0
        and results["login_failed"] >= results["login_attempted"]
    )
    if all_recovery_logins_failed:
        error = f"需要恢复登录的 {results['login_attempted']} 个账号全部登录失败"
        logger.finish(TASK_STATUS_FAILED, error=error)
        return
    if unresolved:
        logger.finish(
            TASK_STATUS_FAILED,
            error=f"仍有 {unresolved} 个账号未确认有效，已保留为隔离状态",
        )
        return
    logger.finish(TASK_STATUS_FAILED if errors else TASK_STATUS_SUCCEEDED)
