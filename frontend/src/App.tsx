import {
  BrowserRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { useEffect, useState } from "react";
import { getAuthToken, setAuthToken, API, cn } from "@/lib/utils";
import { I18nProvider, useI18n } from "@/lib/i18n-context";
import { PrivacyProvider, usePrivacy } from "@/lib/privacy-context";
import type { TranslationKey } from "@/lib/i18n";
import Accounts from "@/pages/Accounts";
import Tasks from "@/pages/Tasks";
import SettingsPage from "@/pages/SettingsPage";
import MicrosoftMailboxes from "@/pages/MicrosoftMailboxes";
import UpdateBanner from "@/components/UpdateBanner";
import {
  Moon,
  Settings as SettingsIcon,
  Sun,
  Monitor,
  Languages,
  Users,
  ListTodo,
  PanelLeftClose,
  PanelLeft,
  Inbox,
  Shield,
  ShieldAlert,
} from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Sidebar                                                            */
/* ------------------------------------------------------------------ */

type NavItem = {
  path: string;
  labelKey?: TranslationKey;
  label?: string;
  icon: any;
  exact?: boolean;
};

const SETTINGS_NAV_ITEMS: { labelKey: TranslationKey; hash: string }[] = [
  { labelKey: "nav.settings.general", hash: "general" },
  { labelKey: "nav.settings.mailbox", hash: "mailbox" },
  { labelKey: "nav.settings.proxyPool", hash: "proxy-pool" },
  { labelKey: "nav.settings.backup", hash: "backup" },
];

const NAV_ITEMS: NavItem[] = [
  { path: "/accounts/chatgpt", label: "chatgpt free", icon: Users },
  { path: "/tasks", label: "任务", icon: ListTodo },
  { path: "/microsoft-mailboxes", label: "微软邮箱", icon: Inbox },
  { path: "/settings", labelKey: "nav.settings", icon: SettingsIcon },
];

function Sidebar({
  theme,
  toggleTheme,
  collapsed,
  setCollapsed,
}: {
  theme: string;
  toggleTheme: () => void;
  collapsed: boolean;
  setCollapsed: (v: boolean) => void;
}) {
  const { t, toggleLanguage } = useI18n();
  const { privacyMode, togglePrivacyMode } = usePrivacy();
  const location = useLocation();
  const isSettings = location.pathname === "/settings";
  const currentTab = new URLSearchParams(location.search).get("tab") || "general";

  const navLinkClass = (active: boolean) =>
    cn(
      "group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors",
      active
        ? "bg-[var(--accent-soft)] text-[var(--text-primary)]"
        : "text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]",
      collapsed && "justify-center px-0",
    );

  const iconClass = (active: boolean) =>
    cn(
      "h-[18px] w-[18px] shrink-0",
      active
        ? "text-[var(--accent)]"
        : "text-[var(--text-muted)] group-hover:text-[var(--text-secondary)]",
    );

  return (
    <aside
      className={cn(
        "flex h-screen flex-col border-r border-[var(--border)] bg-[var(--bg-surface)] transition-[width] duration-200",
        collapsed ? "w-16" : "w-[220px]",
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex h-12 shrink-0 items-center border-b border-[var(--border)] px-3",
          collapsed && "justify-center",
        )}
      >
        {!collapsed && (
          <div className="flex items-center gap-2.5 min-w-0 flex-1">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white">
              C
            </div>
            <div className="flex flex-col min-w-0 flex-1">
              <span className="truncate text-sm font-semibold text-[var(--text-primary)] leading-tight">
                ChatGPT-Account-Pool
              </span>
              <span className="text-[10px] font-mono text-emerald-400 font-medium leading-tight">
                v2.0.0 · 最新版
              </span>
            </div>
          </div>
        )}
        {collapsed && (
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white" title="ChatGPT-Account-Pool v2.0.0">
            C
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-0.5">
        {NAV_ITEMS.map(({ path, labelKey, label: itemLabel, icon: Icon, exact }) => {
          const active = exact
            ? location.pathname === path
            : location.pathname.startsWith(path);
          const label = itemLabel || (labelKey ? t(labelKey) : path);
          return (
            <NavLink
              key={path}
              to={path}
              end={exact}
              className={navLinkClass(active)}
              title={collapsed ? label : undefined}
            >
              <Icon className={iconClass(active)} />
              {!collapsed && <span>{label}</span>}
            </NavLink>
          );
        })}
        {!collapsed && isSettings && (
          <div className="ml-[21px] mt-0.5 space-y-px border-l border-[var(--border)] pl-3">
            {SETTINGS_NAV_ITEMS.map((item) => {
              const active = currentTab === item.hash;
              return (
                <NavLink
                  key={item.hash}
                  to={`/settings?tab=${item.hash}`}
                  className={cn(
                    "relative block rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
                    active
                      ? "text-[var(--accent)] font-medium bg-[var(--accent-soft)]"
                      : "text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]",
                  )}
                >
                  {active && (
                    <span className="absolute -left-[13.5px] top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-[var(--accent)]" />
                  )}
                  {t(item.labelKey)}
                </NavLink>
              );
            })}
          </div>
        )}
      </nav>

      {/* Footer */}
      <div className="shrink-0 border-t border-[var(--border)] px-2 py-1.5 flex items-center gap-1">
        <button
          onClick={toggleTheme}
          className={cn(
            "flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]",
          )}
          title={
            theme === "light"
              ? t("sidebar.theme.toDark")
              : theme === "dark"
                ? t("sidebar.theme.toLight")
                : t("sidebar.theme.followSystem")
          }
        >
          {theme === "light" ? (
            <Moon className="h-4 w-4" />
          ) : theme === "system" ? (
            <Monitor className="h-4 w-4" />
          ) : (
            <Sun className="h-4 w-4" />
          )}
        </button>
        {!collapsed && (
          <span className="flex-1 text-[12px] text-[var(--text-muted)]">
            {theme === "light"
              ? t("sidebar.theme.light")
              : theme === "dark"
                ? t("sidebar.theme.dark")
                : t("sidebar.theme.system")}
          </span>
        )}
        <button
          onClick={toggleLanguage}
          className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          title={t("sidebar.languageToggle")}
        >
          <Languages className="h-4 w-4" />
        </button>
        <button
          onClick={togglePrivacyMode}
          className={cn(
            "flex items-center justify-center rounded-md p-2 transition-colors cursor-pointer",
            privacyMode
              ? "text-emerald-400 bg-emerald-500/10 hover:bg-emerald-500/20"
              : "text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          )}
          title={privacyMode ? "隐私防窥模式：已开启（敏感信息已脱敏）" : "隐私防窥模式：已关闭（敏感信息公开）"}
        >
          {privacyMode ? <Shield className="h-4 w-4" /> : <ShieldAlert className="h-4 w-4" />}
        </button>
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
        >
          {collapsed ? (
            <PanelLeft className="h-4 w-4" />
          ) : (
            <PanelLeftClose className="h-4 w-4" />
          )}
        </button>
      </div>
    </aside>
  );
}

/* ------------------------------------------------------------------ */
/*  Shell                                                              */
/* ------------------------------------------------------------------ */

function Shell({
  theme,
  setTheme,
  toggleTheme,
}: {
  theme: string;
  setTheme: (t: string) => void;
  toggleTheme: () => void;
}) {
  const { privacyMode, togglePrivacyMode } = usePrivacy();
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true",
  );
  const [narrowViewport, setNarrowViewport] = useState(
    () => window.matchMedia("(max-width: 767px)").matches,
  );

  useEffect(() => {
    localStorage.setItem("sidebar-collapsed", String(collapsed));
  }, [collapsed]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const update = () => setNarrowViewport(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  const sidebarCollapsed = collapsed || narrowViewport;

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--bg-base)]">
      <Sidebar
        theme={theme}
        toggleTheme={toggleTheme}
        collapsed={sidebarCollapsed}
        setCollapsed={setCollapsed}
      />
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-6 py-6 lg:px-8">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex-1 min-w-0">
              <UpdateBanner />
            </div>
            <button
              type="button"
              onClick={togglePrivacyMode}
              className={cn(
                "shrink-0 inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-all cursor-pointer shadow-sm",
                privacyMode
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                  : "border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:border-zinc-500"
              )}
              title={privacyMode ? "点击关闭防窥模式（显示所有明文）" : "点击开启防窥模式（敏感信息自动打码）"}
            >
              {privacyMode ? (
                <Shield className="h-3.5 w-3.5 text-emerald-400" />
              ) : (
                <ShieldAlert className="h-3.5 w-3.5 text-zinc-400" />
              )}
              <span>{privacyMode ? "防窥保护：已开启" : "防窥保护：已关闭"}</span>
            </button>
          </div>
          <Routes>
            <Route path="/" element={<Navigate to="/accounts/chatgpt" replace />} />
            <Route path="/accounts/chatgpt" element={<Accounts />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/microsoft-mailboxes" element={<MicrosoftMailboxes />} />
            <Route
              path="/settings"
              element={<SettingsPage theme={theme} setTheme={setTheme} />}
            />
          </Routes>
        </div>
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Login                                                              */
/* ------------------------------------------------------------------ */

function LoginScreen({ onLogin }: { onLogin: (token: string) => void }) {
  const { t } = useI18n();
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(API + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (data.ok) {
        setAuthToken(data.token || "");
        onLogin(data.token || "");
      } else {
        setError(data.error || t("login.passwordError"));
      }
    } catch {
      setError(t("login.requestFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-[var(--bg-base)]">
      <form
        onSubmit={submit}
        className="w-80 space-y-4 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-6"
      >
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--accent)] text-sm font-bold text-white">
            C
          </div>
          <h1 className="text-base font-semibold text-[var(--text-primary)]">
            ChatGPT-Account-Pool
          </h1>
        </div>
        <p className="text-sm text-[var(--text-muted)]">{t("login.prompt")}</p>
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder={t("login.passwordPlaceholder")}
          autoFocus
          className="control-surface w-full"
        />
        {error && <p className="text-xs text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={loading || !pw}
          className="w-full rounded-lg bg-[var(--accent)] px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-[var(--accent-hover)] disabled:opacity-50"
        >
          {loading ? t("login.checking") : t("login.submit")}
        </button>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  App root                                                           */
/* ------------------------------------------------------------------ */

function AppContent() {
  const { t } = useI18n();
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "dark",
  );
  const [authState, setAuthState] = useState<
    "loading" | "open" | "locked" | "authed"
  >("loading");

  useEffect(() => {
    const applyTheme = () => {
      let effective = theme;
      if (theme === "system") {
        effective = window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
      }
      document.documentElement.classList.toggle("light", effective === "light");
    };
    applyTheme();
    localStorage.setItem("theme", theme);
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => {
      if (theme === "system") applyTheme();
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  useEffect(() => {
    fetch(API + "/auth/check")
      .then((r) => r.json())
      .then((data) => {
        if (!data.required) setAuthState("open");
        else if (getAuthToken()) setAuthState("authed");
        else setAuthState("locked");
      })
      .catch(() => setAuthState("open"));
  }, []);

  const toggleTheme = () =>
    setTheme((c) =>
      c === "dark" ? "light" : c === "light" ? "system" : "dark",
    );

  if (authState === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--bg-base)] text-[var(--text-muted)] text-sm">
        {t("app.loading")}
      </div>
    );
  }
  if (authState === "locked") {
    return <LoginScreen onLogin={() => setAuthState("authed")} />;
  }

  return (
    <BrowserRouter>
      <Shell theme={theme} setTheme={setTheme} toggleTheme={toggleTheme} />
    </BrowserRouter>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <PrivacyProvider>
        <AppContent />
      </PrivacyProvider>
    </I18nProvider>
  );
}
