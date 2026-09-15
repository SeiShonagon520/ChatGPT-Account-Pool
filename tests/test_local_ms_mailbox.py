from core.base_mailbox import MailboxAccount
from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_local_ms_pool_rows


def test_parse_local_ms_pool_rows_accepts_gujumpgate_hotmail_format():
    rows = parse_local_ms_pool_rows(
        "\n".join(
            [
                "account----password----ID----Token",
                "user@example.com----mail-pass----client-id-123----refresh-token-456",
            ]
        )
    )

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "user@example.com"
    assert entry.password == "mail-pass"
    assert entry.login_account == "user@example.com"
    assert entry.client_id == "client-id-123"
    assert entry.refresh_token == "refresh-token-456"
    assert entry.source_format == "gujumpgate_hotmail"
    assert entry.graph_ready is True
    assert entry.imap_ready is False


def test_parse_local_ms_pool_rows_accepts_xinlan_common_format():
    columns = [""] * 19
    columns[0] = "common@outlook.com"
    columns[1] = "mail-password"
    columns[2] = "common@outlook.com"
    columns[3] = "outlook.office365.com"
    columns[4] = "993"
    columns[6] = "ssl"
    columns[16] = "common-client-id"
    columns[17] = "common-refresh-token"

    rows = parse_local_ms_pool_rows("----".join(columns))

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "common@outlook.com"
    assert entry.client_id == "common-client-id"
    assert entry.refresh_token == "common-refresh-token"
    assert entry.source_format == "xinlan_common"
    assert entry.graph_ready is True
    assert entry.imap_ready is True


def test_local_ms_pool_records_gujumpgate_source_metadata(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="user@example.com----mail-pass----client-id-123----refresh-token-456",
        state_file=str(tmp_path / "state.json"),
    )

    account = pool.get_email()
    provider_account = account.extra["provider_account"]
    provider_resource = account.extra["provider_resource"]

    assert provider_account["credentials"]["client_id"] == "client-id-123"
    assert provider_account["credentials"]["refresh_token"] == "refresh-token-456"
    assert provider_account["metadata"]["source"] == "gujumpgate_hotmail"
    assert provider_resource["metadata"]["source"] == "gujumpgate_hotmail"


def test_local_ms_pool_allocates_six_outlook_child_addresses_per_parent(tmp_path):
    import re

    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@outlook.com----mail-pass----client-id----refresh-token",
        state_file=str(tmp_path / "state.json"),
        alias_count=6,
    )

    accounts = [pool.get_email() for _ in range(6)]
    assert pool._repository().stats()["used"] == 0
    assert pool._repository().stats()["reserved"] == 6

    # Every use is an isolated random sub-address (parent+<6-char-tag>@outlook.com)
    # that delivers into the parent inbox.  The bare parent is never handed out:
    # a shared parent inbox lets one worker read another's (or a stale) OTP.
    assert all(re.fullmatch(r"parent\+[a-z0-9]{6}@outlook.com", item.email) for item in accounts)
    assert {item.account_id for item in accounts} == {
        "parent@outlook.com#sub-1",
        *(f"parent@outlook.com#sub-{i}" for i in range(2, 7)),
    }
    assert all(
        item.extra["provider_account"]["credentials"]["email"] == "parent@outlook.com"
        for item in accounts
    )
    assert all(
        item.extra["provider_resource"]["metadata"]["parent_email"] == "parent@outlook.com"
        for item in accounts
    )
    assert all(pool.commit_email(item) for item in accounts)
    assert pool._repository().stats()["used"] == 6

    try:
        pool.get_email()
    except RuntimeError as exc:
        assert "已用尽" in str(exc)
    else:
        raise AssertionError("the seventh use should not be allocated")


def test_local_ms_pool_defaults_to_six_children_and_excludes_exhausted_parent(tmp_path):
    import re

    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@outlook.com----mail-pass----client-id----refresh-token",
        state_file=str(tmp_path / "state.json"),
    )

    accounts = [pool.get_email() for _ in range(6)]
    emails = [account.email for account in accounts]
    # All six uses are isolated +tag sub-addresses; the bare parent is never used.
    assert all(re.fullmatch(r"parent\+[a-z0-9]{6}@outlook.com", email) for email in emails)
    assert all(pool.commit_email(account) for account in accounts)
    try:
        pool.peek_email()
    except RuntimeError as exc:
        assert "已用尽" in str(exc)
    else:
        raise AssertionError("an exhausted Microsoft parent mailbox must leave the pool")


def test_local_ms_pool_failure_releases_parent_capacity(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@outlook.com----mail-pass----client-id----refresh-token",
        state_file=str(tmp_path / "state.json"),
    )

    failed = pool.get_email()
    assert failed.account_id.endswith("#sub-1")
    assert pool.release_email(failed) is True

    retried = pool.get_email()
    assert retried.account_id.endswith("#sub-1")
    assert retried.email != failed.email


def test_child_mailbox_otp_filter_matches_only_the_assigned_recipient():
    account = MailboxAccount(email="parent+reg2@outlook.com")

    assert LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg2@outlook.com"}}]},
        account,
    )
    assert not LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg1@outlook.com"}}]},
        account,
    )


def test_graph_access_token_tries_fallback_endpoint(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, data, proxies=None, timeout=None):
        calls.append((url, data))
        if len(calls) == 1:
            return FakeResponse(400, text='{"error":"invalid_request"}')
        return FakeResponse(200, {"access_token": "access-token-ok"})

    monkeypatch.setattr("core.local_ms_mailbox.requests.post", fake_post)
    pool = LocalMicrosoftMailboxPool()
    account = MailboxAccount(
        email="user@example.com",
        account_id="user@example.com",
        extra={
            "provider_account": {
                "credentials": {
                    "email": "user@example.com",
                    "client_id": "client-id-123",
                    "refresh_token": "refresh-token-456",
                }
            }
        },
    )
    entry = pool._entry_for_account(account)

    assert pool._graph_access_token(entry) == "access-token-ok"
    assert len(calls) == 2
    assert calls[0][0] == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    assert calls[1][0] == "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def test_graph_invalid_grant_disables_parent_mailbox(monkeypatch):
    class FakeResponse:
        status_code = 400
        text = '{"error":"invalid_grant"}'

        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr(
        "core.local_ms_mailbox.requests.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    pool = LocalMicrosoftMailboxPool(
        pool_text="dead@outlook.com----mail-pass----client-id----dead-refresh-token"
    )
    account = pool.get_email()
    entry = pool._entry_for_account(account)

    try:
        pool._graph_access_token(entry)
    except RuntimeError as exc:
        assert "已失效" in str(exc)
    else:
        raise AssertionError("invalid_grant must fail token exchange")

    record = pool._repository().get_by_parent_email("dead@outlook.com")
    assert record is not None
    assert record.status == "disabled"
