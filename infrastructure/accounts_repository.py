from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from core.account_display import build_account_display_summary
from core.db import (
    AccountCredentialModel,
    AccountModel,
    AccountOverviewModel,
    RegisteredEmailHistoryModel,
    engine,
)
from core.account_graph import (
    load_account_graphs,
    matches_status_filter,
    patch_account_graph,
    purge_account_graph,
    sync_account_graph,
)
from core.platform_accounts import resolve_primary_token
from domain.accounts import (
    AccountExportSelection,
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountUpdateCommand,
)


def _build_summary_updates(
    overview: dict | None,
    *,
    cashier_url: str | None = None,
    region: str | None = None,
    trial_end_time: int | None = None,
) -> dict | None:
    summary = dict(overview or {})
    if cashier_url is not None:
        summary["cashier_url"] = cashier_url
    if region is not None:
        summary["region"] = region
    if trial_end_time is not None:
        summary["trial_end_time"] = int(trial_end_time or 0)
    return summary or None


def _build_credential_updates(
    credentials: dict | None,
) -> dict | None:
    return dict(credentials or {}) or None


def _to_record(model: AccountModel, graph: dict | None = None) -> AccountRecord:
    graph = graph or {}
    overview = graph.get("overview") or {}
    lifecycle_status = graph.get("lifecycle_status") or "registered"
    validity_status = graph.get("validity_status") or "unknown"
    plan_state = graph.get("plan_state") or "unknown"
    plan_name = graph.get("plan_name") or ""
    display_status = graph.get("display_status") or "registered"
    provider_resources = list(graph.get("provider_resources") or [])
    return AccountRecord(
        id=int(model.id or 0),
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        primary_token=resolve_primary_token(model, graph),
        trial_end_time=int(overview.get("trial_end_time") or 0),
        cashier_url=str(overview.get("cashier_url") or ""),
        lifecycle_status=lifecycle_status,
        validity_status=validity_status,
        plan_state=plan_state,
        plan_name=plan_name,
        display_status=display_status,
        overview=overview,
        display_summary=build_account_display_summary(
            platform=model.platform,
            email=model.email,
            lifecycle_status=lifecycle_status,
            validity_status=validity_status,
            plan_state=plan_state,
            plan_name=plan_name,
            display_status=display_status,
            overview=overview,
            provider_resources=provider_resources,
        ),
        credentials=list(graph.get("credentials") or []),
        provider_accounts=list(graph.get("provider_accounts") or []),
        provider_resources=provider_resources,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AccountsRepository:
    @staticmethod
    def _load_records(session: Session, models: list[AccountModel]) -> list[AccountRecord]:
        account_ids = [int(model.id or 0) for model in models if model.id]
        graphs = load_account_graphs(session, account_ids)
        missing = [model for model in models if int(model.id or 0) not in graphs]
        if missing:
            for model in missing:
                sync_account_graph(session, model)
            session.commit()
            graphs = load_account_graphs(session, account_ids)
        return [_to_record(model, graphs.get(int(model.id or 0), {})) for model in models]

    def list(self, query: AccountQuery) -> tuple[int, list[AccountRecord]]:
        page = max(query.page, 1)
        page_size = min(max(query.page_size, 1), 100)
        with Session(engine) as session:
            statement = select(AccountModel)
            count_statement = select(func.count()).select_from(AccountModel)
            if query.platform:
                statement = statement.where(AccountModel.platform == query.platform)
                count_statement = count_statement.where(AccountModel.platform == query.platform)
            if query.email:
                statement = statement.where(AccountModel.email.contains(query.email))
                count_statement = count_statement.where(AccountModel.email.contains(query.email))
            if query.has_refresh_token is not None:
                refresh_token_account_ids = select(AccountCredentialModel.account_id).where(
                    AccountCredentialModel.scope == "platform",
                    AccountCredentialModel.key.in_(("refresh_token", "refreshToken")),
                    AccountCredentialModel.value != "",
                )
                refresh_token_condition = AccountModel.id.in_(refresh_token_account_ids)
                if query.has_refresh_token:
                    statement = statement.where(refresh_token_condition)
                    count_statement = count_statement.where(refresh_token_condition)
                else:
                    statement = statement.where(~refresh_token_condition)
                    count_statement = count_statement.where(~refresh_token_condition)
            if query.status:
                norm_status = query.status.strip().lower()
                if norm_status == "valid":
                    st_ids = select(AccountOverviewModel.account_id).where(
                        func.lower(func.coalesce(func.json_extract(AccountOverviewModel.summary_json, "$.refresh_token_status"), "")) == "valid"
                    )
                    statement = statement.where(AccountModel.id.in_(st_ids))
                    count_statement = count_statement.where(AccountModel.id.in_(st_ids))
                elif norm_status == "invalid":
                    st_ids = select(AccountOverviewModel.account_id).where(
                        func.lower(func.coalesce(func.json_extract(AccountOverviewModel.summary_json, "$.refresh_token_status"), "")) == "invalid"
                    )
                    statement = statement.where(AccountModel.id.in_(st_ids))
                    count_statement = count_statement.where(AccountModel.id.in_(st_ids))
                elif norm_status == "has_mailbox":
                    from core.db import ProviderAccountModel
                    st_ids = select(ProviderAccountModel.account_id).where(
                        ProviderAccountModel.provider_name == "local_ms_pool"
                    )
                    statement = statement.where(AccountModel.id.in_(st_ids))
                    count_statement = count_statement.where(AccountModel.id.in_(st_ids))
                elif norm_status == "has_rt":
                    rt_ids = select(AccountCredentialModel.account_id).where(
                        AccountCredentialModel.scope == "platform",
                        AccountCredentialModel.key.in_(("refresh_token", "refreshToken")),
                        AccountCredentialModel.value != "",
                    )
                    statement = statement.where(AccountModel.id.in_(rt_ids))
                    count_statement = count_statement.where(AccountModel.id.in_(rt_ids))
            total = int(session.exec(count_statement).one() or 0)
            statement = (
                statement
                .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            models = session.exec(statement).all()
            records = self._load_records(session, models)
        return total, records

    def survival_stats(self, platform: str) -> dict[str, int | float | str]:
        normalized_platform = str(platform or "").strip().lower()
        with Session(engine) as session:
            historical_statement = select(func.count()).select_from(RegisteredEmailHistoryModel)
            alive_statement = (
                select(func.count())
                .select_from(AccountOverviewModel)
                .join(AccountModel, AccountModel.id == AccountOverviewModel.account_id)
                .where(
                    func.lower(
                        func.coalesce(
                            func.json_extract(
                                AccountOverviewModel.summary_json,
                                "$.refresh_token_status",
                            ),
                            "",
                        )
                    ) == "valid"
                )
            )
            if normalized_platform:
                historical_statement = historical_statement.where(
                    RegisteredEmailHistoryModel.platform == normalized_platform
                )
                alive_statement = alive_statement.where(
                    AccountModel.platform == normalized_platform
                )
            historical_count = int(session.exec(historical_statement).one() or 0)
            alive_count = int(session.exec(alive_statement).one() or 0)
        survival_rate = round(alive_count / historical_count * 100, 2) if historical_count else 0.0
        return {
            "platform": normalized_platform,
            "alive_accounts": alive_count,
            "historical_registered_emails": historical_count,
            "survival_rate": survival_rate,
        }

    def get(self, account_id: int) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            records = self._load_records(session, [model])
            return records[0] if records else None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        with Session(engine) as session:
            statement = select(AccountModel)
            if selection.platform:
                statement = statement.where(AccountModel.platform == selection.platform)
            if selection.search_filter:
                statement = statement.where(AccountModel.email.contains(selection.search_filter))
            if not selection.select_all and selection.ids:
                statement = statement.where(AccountModel.id.in_(selection.ids))
            statement = statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            models = session.exec(statement).all()
            records = self._load_records(session, models)
        if selection.status_filter:
            records = [item for item in records if matches_status_filter({
                "display_status": item.display_status,
                "lifecycle_status": item.lifecycle_status,
                "plan_state": item.plan_state,
                "validity_status": item.validity_status,
            }, selection.status_filter)]
        return records

    def update(self, account_id: int, command: AccountUpdateCommand) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            if command.password is not None:
                model.password = command.password
            if command.user_id is not None:
                model.user_id = command.user_id
            model.updated_at = datetime.now(timezone.utc)
            session.add(model)
            session.commit()
            session.refresh(model)
            patch_account_graph(
                session,
                model,
                lifecycle_status=command.lifecycle_status,
                primary_token=command.primary_token,
                cashier_url=command.cashier_url,
                region=command.region,
                trial_end_time=command.trial_end_time,
                summary_updates=_build_summary_updates(
                    command.overview,
                    cashier_url=command.cashier_url,
                    region=command.region,
                    trial_end_time=command.trial_end_time,
                ),
                credential_updates=_build_credential_updates(command.credentials),
                provider_accounts=command.provider_accounts,
                provider_resources=command.provider_resources,
                replace_provider_accounts=command.replace_provider_accounts,
                replace_provider_resources=command.replace_provider_resources,
            )
            session.commit()
            return self._load_records(session, [model])[0]

    def delete(self, account_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return False
            purge_account_graph(session, account_id)
            session.delete(model)
            session.commit()
            return True

    def import_lines(self, platform: str, lines: list[AccountImportLine]) -> dict[str, int]:
        if not lines:
            return {"received": 0, "unique": 0, "created": 0, "updated": 0, "failed": 0}

        unique_lines: dict[str, AccountImportLine] = {}
        for line in lines:
            key = line.email.strip().lower()
            if key:
                unique_lines[key] = line

        created = 0
        updated = 0
        failed = 0

        with Session(engine) as session:
            keys = list(unique_lines.keys())
            existing_map: dict[str, AccountModel] = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                chunk_models = session.exec(
                    select(AccountModel).where(
                        AccountModel.platform == platform,
                        AccountModel.email.in_(chunk),
                    )
                ).all()
                for m in chunk_models:
                    existing_map[m.email.strip().lower()] = m

            for email_key, line in unique_lines.items():
                try:
                    if email_key in existing_map:
                        model = existing_map[email_key]
                        if line.password:
                            model.password = line.password
                        model.updated_at = datetime.now(timezone.utc)
                        session.add(model)
                        session.commit()
                        session.refresh(model)
                        updated += 1
                    else:
                        model = AccountModel(
                            platform=platform,
                            email=line.email,
                            password=line.password,
                        )
                        session.add(model)
                        session.commit()
                        session.refresh(model)
                        existing_map[email_key] = model
                        created += 1
                        try:
                            from core.db import record_registered_email_history
                            record_registered_email_history(platform, line.email)
                        except Exception:
                            pass

                    extra = dict(line.extra or {})
                    summary_updates = dict(extra.get("overview") or extra.get("summary") or {})
                    for key in ("trial_end_time", "cashier_url", "region", "remote_email", "checked_at"):
                        if key in extra and key not in summary_updates:
                            summary_updates[key] = extra[key]
                    legacy_extra = {
                        key: value
                        for key, value in extra.items()
                        if key not in {
                            "overview",
                            "summary",
                            "primary_token",
                            "token",
                            "lifecycle_status",
                            "status",
                            "cashier_url",
                            "trial_end_time",
                            "region",
                            "remote_email",
                            "checked_at",
                            "credentials",
                            "provider_accounts",
                            "provider_resources",
                        }
                        and value not in (None, "", [], {})
                    }
                    if legacy_extra:
                        summary_updates["legacy_extra"] = legacy_extra
                    credential_updates = dict(extra.get("credentials") or {})
                    for key in (
                        "access_token",
                        "refresh_token",
                        "session_token",
                        "id_token",
                        "accessToken",
                        "refreshToken",
                        "sessionToken",
                        "idToken",
                        "cookies",
                        "cookie",
                        "api_key",
                        "wos_session",
                        "sso",
                        "sso_rw",
                        "totp_secret",
                        "auth_type",
                    ):
                        if key in extra and key not in credential_updates:
                            credential_updates[key] = extra[key]
                    primary_token = extra.get("primary_token")
                    if primary_token in (None, ""):
                        primary_token = extra.get("token")
                    patch_account_graph(
                        session,
                        model,
                        lifecycle_status=str(extra.get("lifecycle_status") or extra.get("status") or "registered"),
                        primary_token=str(primary_token or "") or None,
                        cashier_url=str(extra.get("cashier_url") or "") or None,
                        summary_updates=summary_updates or None,
                        credential_updates=credential_updates or None,
                        provider_accounts=list(extra.get("provider_accounts") or []) or None,
                        provider_resources=list(extra.get("provider_resources") or []) or None,
                        replace_provider_accounts=bool(extra.get("provider_accounts")),
                        replace_provider_resources=bool(extra.get("provider_resources")),
                    )
                    session.commit()
                except Exception:
                    failed += 1

        return {
            "received": len(lines),
            "unique": len(unique_lines),
            "created": created,
            "updated": updated,
            "failed": failed,
        }
