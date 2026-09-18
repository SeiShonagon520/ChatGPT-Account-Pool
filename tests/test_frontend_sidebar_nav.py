from pathlib import Path


APP_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
WELCOME_DIALOG_TSX = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "components"
    / "WelcomeDialog.tsx"
)


def _nav_items_block() -> str:
    source = APP_TSX.read_text(encoding="utf-8")
    start = source.index("const NAV_ITEMS: NavItem[] = [")
    end = source.index("];", start)
    return source[start:end]


def test_sidebar_top_level_nav_includes_registration_tasks():
    block = _nav_items_block()

    assert block.count("path:") == 4
    assert 'path: "/"' not in block
    assert 'path: "/accounts/chatgpt"' in block
    assert 'label: "chatgpt free"' in block
    assert 'path: "/tasks"' in block
    assert 'path: "/microsoft-mailboxes"' in block
    assert 'path: "/settings"' in block
    assert 'labelKey: "nav.settings"' in block


def test_sidebar_hides_accounts_menu_and_other_business_links():
    source = APP_TSX.read_text(encoding="utf-8")

    assert "setAccountsOpen" not in source
    assert "getPlatforms" not in source
    assert "nav.accounts" not in source
    assert "nav.ctfGptPlus" not in source
    assert "nav.gopayGptPlus" not in source
    assert "nav.plusManager" not in source
    assert "nav.tasks" not in source


def test_sidebar_includes_general_mailbox_and_proxy_pool_settings_submenu_items():
    source = APP_TSX.read_text(encoding="utf-8")

    start = source.index("const SETTINGS_NAV_ITEMS:")
    end = source.index("];", start)
    block = source[start:end]

    assert block.count('hash: "') == 4
    assert 'labelKey: "nav.settings.general", hash: "general"' in block
    assert 'labelKey: "nav.settings.mailbox", hash: "mailbox"' in block
    assert 'labelKey: "nav.settings.proxyPool", hash: "proxy-pool"' in block
    assert 'labelKey: "nav.settings.backup", hash: "backup"' in block

    assert "currentTab" in source
    assert "/settings?tab=${item.hash}" in source


def test_app_welcome_dialog_component_preserved_for_upstream():
    source = APP_TSX.read_text(encoding="utf-8")

    # In FreeGPT-Manager user repo, popup is unmounted for clean UX
    assert "<WelcomeDialog />" not in source
    assert WELCOME_DIALOG_TSX.exists()
