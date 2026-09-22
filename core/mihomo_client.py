"""Small client for the private Mihomo controller used by registrations."""
from __future__ import annotations

import os
import re
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests


DEFAULT_CONTROLLER_URL = "http://mihomo:9090"
DEFAULT_PROXY_URL = "http://mihomo:7890"
DEFAULT_PROXY_GROUP = "REGISTER-ALL"
DEFAULT_SLOT_GROUP_PREFIX = "REGISTER-SLOT-"
DEFAULT_SLOT_PORT_BASE = 7900
DEFAULT_SLOT_COUNT = 50
DEFAULT_DELAY_TEST_URL = "http://www.gstatic.com/generate_204"
DEFAULT_NODE_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".mihomo_node_state.json"
DEFAULT_CHATGPT_PREFLIGHT_URL = "https://chatgpt.com/auth/login"

_CHATGPT_HARD_BLOCK_MARKERS = (
    "unable to load site",
    "if you are using a vpn",
    "try turning it off",
    "sorry, you have been blocked",
    "access denied",
)
_CHATGPT_CHALLENGE_MARKERS = (
    "enable javascript and cookies to continue",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "cloudflare ray id",
)

# Some Clash subscriptions expose plan metadata as entries in the provider's
# proxy list. They are not dialable proxies even though Mihomo reports them as
# VLESS/Trojan entries, so never surface or allocate them as worker nodes.
_SUBSCRIPTION_METADATA_PREFIXES = (
    "\u5269\u4f59\u6d41\u91cf",
    "\u8ddd\u79bb\u4e0b\u6b21\u91cd\u7f6e\u5269\u4f59",
    "\u5957\u9910\u5230\u671f",
)
_SUBSCRIPTION_METADATA_RE = re.compile(
    r"^(?:" + "|".join(map(re.escape, _SUBSCRIPTION_METADATA_PREFIXES)) + r")"
)


def _is_usable_proxy_name(name: str) -> bool:
    normalized = str(name or "").strip()
    return bool(normalized) and not _SUBSCRIPTION_METADATA_RE.match(normalized)


class MihomoError(RuntimeError):
    pass


class MihomoUnavailableError(MihomoError):
    pass


class MihomoNodeError(MihomoError):
    pass


@dataclass
class MihomoProxyLease:
    """A sticky listener/node assignment for one protocol worker."""

    allocator: "MihomoRegistrationAllocator"
    slot: int
    node: str
    proxy: str
    attempted_nodes: set[str] = field(default_factory=set)
    released: bool = False

    def rotate(self) -> str:
        if self.released:
            raise MihomoNodeError("proxy slot has already been released")
        self.node = self.allocator.rotate(self)
        return self.proxy

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.allocator.release(self)


class MihomoRegistrationAllocator:
    """Lease independent Mihomo listeners to concurrent registrations."""

    def __init__(
        self,
        client: "MihomoClient",
        preferred_node: str | None = None,
        *,
        preflight: bool = False,
    ):
        self.client = client
        self._lock = threading.RLock()
        self._free_slots = set(range(1, client.slot_count + 1))
        self._active: dict[int, MihomoProxyLease] = {}
        self._node_counts: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        # Nodes that returned a registration-specific hard failure are kept
        # out for the rest of this task, rather than becoming eligible again
        # after the short network cooldown.
        self._failed_nodes: set[str] = set()
        self._cooldown_seconds = max(client.node_cooldown_seconds, 5.0)
        # Per-node OAI IP-ban state.  A node is only usable for registration
        # while it is absent from ``_banned``; the pulse controller moves a
        # node here after consecutive no-email strikes and removes it again
        # when a probe confirms OAI accepts the IP once more.
        self._banned: set[str] = set()
        self._no_email_counts: dict[str, int] = {}
        info = client.list_nodes(refresh=preflight)
        self.nodes = [
            str(item.get("name") or "").strip()
            for item in info.get("nodes", [])
            if str(item.get("name") or "").strip()
            and item.get("alive") is not False
            and client.is_node_enabled(str(item.get("name") or ""))
        ]
        self.preflight_results: dict[str, dict[str, Any]] = {}
        self._preflight_rejected: set[str] = set()
        if preflight and self.nodes:
            self.preflight_results = client.preflight_registration_nodes(self.nodes)
            self._preflight_rejected = {
                node
                for node in self.nodes
                if not bool(self.preflight_results.get(node, {}).get("eligible"))
            }
            eligible = [
                node
                for node in self.nodes
                if bool(self.preflight_results.get(node, {}).get("eligible"))
            ]
            if eligible:
                self.nodes = eligible
            else:
                # If preflight rejected all nodes due to transient probe errors,
                # retain candidate nodes that are not confirmed hard VPN blocks.
                soft_candidates = [
                    node
                    for node in self.nodes
                    if self.preflight_results.get(node, {}).get("classification") != "vpn_block"
                ]
                if soft_candidates:
                    self.nodes = soft_candidates
        if not self.nodes:
            rejected = ", ".join(
                f"{node}: {result.get('detail') or result.get('classification')}"
                for node, result in self.preflight_results.items()
            )
            suffix = f" ({rejected[:800]})" if rejected else ""
            raise MihomoNodeError(f"no ChatGPT-eligible Mihomo proxy nodes{suffix}")
        preferred = str(preferred_node or "").strip()
        if preferred:
            if preferred not in self.nodes:
                raise MihomoNodeError(f"proxy node is unavailable: {preferred}")
            self._cursor = self.nodes.index(preferred)
        else:
            self._cursor = 0

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def slot_count(self) -> int:
        return self.client.slot_count

    def _pick_node_locked(self, *, exclude: set[str] | None = None) -> str:
        excluded = set(exclude or ())
        now = time.monotonic()
        candidates = [
            node
            for node in self.nodes
            if node not in excluded
            and node not in self._banned
            and node not in self._failed_nodes
            and self._blocked_until.get(node, 0.0) <= now
        ]
        if not candidates:
            candidates = [
                node
                for node in self.nodes
                if node not in excluded
                and node not in self._banned
                and node not in self._failed_nodes
            ]
        if not candidates:
            # Continuous mode never fills ``_banned``, so this only fires when
            # the pulse controller has paused every usable node.
            raise MihomoNodeError("所有节点已封禁，没有可用的注册节点")
        selected = min(
            candidates,
            key=lambda node: (
                self._node_counts.get(node, 0),
                (self.nodes.index(node) - self._cursor) % len(self.nodes),
            ),
        )
        self._cursor = (self.nodes.index(selected) + 1) % len(self.nodes)
        return selected

    def acquire(self) -> MihomoProxyLease:
        with self._lock:
            if not self._free_slots:
                raise MihomoNodeError("Mihomo registration slots are full")
            excluded: set[str] = set()
            while True:
                node = self._pick_node_locked(exclude=excluded)
                try:
                    return self._acquire_slot_locked(node)
                except MihomoNodeError:
                    # A node may go offline after task preflight.  Do not make
                    # a worker fail merely because its first allocation raced
                    # with that health transition.
                    self._failed_nodes.add(node)
                    excluded.add(node)

    def acquire_node(self, node: str) -> MihomoProxyLease:
        """Lease a slot pinned to a SPECIFIC node, even if it is banned.

        Used by the pulse controller's ban probe: the probe must stay on the
        banned IP to test whether OAI delivers verification mail for it again.
        """
        normalized = str(node or "").strip()
        with self._lock:
            if normalized not in self.nodes:
                raise MihomoNodeError(f"代理节点不可用: {normalized or '(空)'}")
            return self._acquire_slot_locked(normalized)

    def _acquire_slot_locked(self, node: str) -> MihomoProxyLease:
        if not self._free_slots:
            raise MihomoNodeError("Mihomo registration slots are full")
        slot = min(self._free_slots)
        self._free_slots.remove(slot)
        lease = MihomoProxyLease(
            allocator=self,
            slot=slot,
            node=node,
            proxy=self.client.slot_proxy_url(slot),
            attempted_nodes={node},
        )
        self._active[slot] = lease
        self._node_counts[node] = self._node_counts.get(node, 0) + 1
        try:
            self.client.activate_slot_node(slot, node)
        except Exception:
            self.release(lease)
            raise
        return lease

    def rotate(self, lease: MihomoProxyLease) -> str:
        with self._lock:
            if lease.slot not in self._active:
                raise MihomoNodeError("proxy slot has already been released")
            old_node = lease.node
            # Browser navigation timeouts are not proof that the exit node is
            # permanently bad.  Under 24-30 way CPU saturation a healthy node
            # can time out once and succeed for another worker seconds later.
            # Keep it on cooldown, but do not remove it for the whole task;
            # otherwise a burst of transient failures can exhaust every node.
            self._blocked_until[old_node] = time.monotonic() + self._cooldown_seconds
            self._node_counts[old_node] = max(self._node_counts.get(old_node, 0) - 1, 0)
            # Never send the same registration worker back through a node it
            # already timed out on.  Other workers may reuse that node after
            # cooldown, but one worker must make forward progress through the
            # pool instead of bouncing between the same few cooling exits.
            lease.attempted_nodes.add(old_node)
            excluded = set(lease.attempted_nodes)
            last_error: MihomoNodeError | None = None
            while True:
                try:
                    next_node = self._pick_node_locked(exclude=excluded)
                except MihomoNodeError as exc:
                    if last_error is not None:
                        raise MihomoNodeError(
                            f"没有可激活的注册节点；最后错误: {last_error}"
                        ) from last_error
                    raise exc
                self._node_counts[next_node] = self._node_counts.get(next_node, 0) + 1
                lease.attempted_nodes.add(next_node)
                try:
                    self.client.activate_slot_node(lease.slot, next_node)
                    return next_node
                except MihomoNodeError as exc:
                    self._node_counts[next_node] = max(
                        self._node_counts.get(next_node, 0) - 1,
                        0,
                    )
                    self._failed_nodes.add(next_node)
                    excluded.add(next_node)
                    last_error = exc

    def release(self, lease: MihomoProxyLease) -> None:
        with self._lock:
            active = self._active.pop(lease.slot, None)
            if not active:
                return
            self._free_slots.add(lease.slot)
            self._node_counts[active.node] = max(
                self._node_counts.get(active.node, 0) - 1,
                0,
            )

    # --- Per-node OAI IP-ban state (pulse registration) ---------------------

    def mark_blocked(self, node: str) -> None:
        with self._lock:
            self._banned.add(str(node).strip())

    def unblock(self, node: str) -> None:
        with self._lock:
            self._banned.discard(str(node).strip())
            self._no_email_counts.pop(str(node).strip(), None)

    def banned_nodes(self) -> list[str]:
        with self._lock:
            return sorted(self._banned)

    def healthy_nodes(self) -> list[str]:
        with self._lock:
            return [
                node
                for node in self.nodes
                if node not in self._banned and node not in self._failed_nodes
            ]

    def all_blocked(self) -> bool:
        with self._lock:
            return bool(self.nodes) and not any(
                node not in self._banned and node not in self._failed_nodes
                for node in self.nodes
            )

    def record_no_email(self, node: str) -> int:
        """Record one consecutive no-email strike and return the new count."""
        with self._lock:
            count = self._no_email_counts.get(str(node).strip(), 0) + 1
            self._no_email_counts[str(node).strip()] = count
            return count

    def record_success(self, node: str) -> None:
        with self._lock:
            self._no_email_counts.pop(str(node).strip(), None)

    def refresh_nodes(self) -> list[str]:
        """Rebuild ``self.nodes`` from Mihomo, dropping stale banned state."""
        with self._lock:
            info = self.client.list_nodes()
            fresh = [
                str(item.get("name") or "").strip()
                for item in info.get("nodes", [])
                if str(item.get("name") or "").strip()
                and item.get("alive") is not False
                and self.client.is_node_enabled(str(item.get("name") or ""))
                and str(item.get("name") or "").strip() not in self._preflight_rejected
            ]
            if not fresh:
                return list(self.nodes)
            removed = set(self.nodes) - set(fresh)
            self.nodes = fresh
            if removed:
                self._banned -= removed
                self._failed_nodes -= removed
                for node in removed:
                    self._no_email_counts.pop(node, None)
            if self._cursor >= len(self.nodes):
                self._cursor = 0
            return list(self.nodes)


class MihomoClient:
    def __init__(
        self,
        *,
        controller_url: str | None = None,
        proxy_url: str | None = None,
        group: str | None = None,
        secret: str | None = None,
        timeout_seconds: float | None = None,
        node_state_file: str | Path | None = None,
    ) -> None:
        self.controller_url = str(
            controller_url or os.getenv("MIHOMO_CONTROLLER_URL", DEFAULT_CONTROLLER_URL)
        ).rstrip("/")
        self.proxy_url = str(
            proxy_url or os.getenv("MIHOMO_PROXY_URL", DEFAULT_PROXY_URL)
        ).strip()
        self.group = str(
            group or os.getenv("MIHOMO_PROXY_GROUP", DEFAULT_PROXY_GROUP)
        ).strip()
        self.slot_group_prefix = str(
            os.getenv("MIHOMO_SLOT_GROUP_PREFIX", DEFAULT_SLOT_GROUP_PREFIX)
        ).strip()
        try:
            slot_count = int(os.getenv("MIHOMO_SLOT_COUNT", str(DEFAULT_SLOT_COUNT)))
        except (TypeError, ValueError):
            slot_count = DEFAULT_SLOT_COUNT
        self.slot_count = min(max(slot_count, 1), 200)
        try:
            slot_port_base = int(
                os.getenv("MIHOMO_SLOT_PORT_BASE", str(DEFAULT_SLOT_PORT_BASE))
            )
        except (TypeError, ValueError):
            slot_port_base = DEFAULT_SLOT_PORT_BASE
        self.slot_port_base = min(max(slot_port_base, 1024), 65000 - self.slot_count)
        try:
            self.node_cooldown_seconds = float(
                os.getenv("MIHOMO_NODE_COOLDOWN_SECONDS", "120")
            )
        except (TypeError, ValueError):
            self.node_cooldown_seconds = 120.0
        self.secret = str(
            secret if secret is not None else os.getenv("MIHOMO_CONTROLLER_SECRET", "")
        ).strip()
        try:
            configured_timeout = float(
                timeout_seconds
                if timeout_seconds is not None
                else os.getenv("MIHOMO_CONTROLLER_TIMEOUT_SECONDS", "8")
            )
        except (TypeError, ValueError):
            configured_timeout = 8.0
        self.timeout_seconds = min(max(configured_timeout, 1.0), 30.0)
        self.node_state_file = Path(
            node_state_file or os.getenv("MIHOMO_NODE_STATE_FILE", "") or DEFAULT_NODE_STATE_FILE
        )
        self._node_state_lock = threading.RLock()
        self.session = requests.Session()
        # The controller is an internal Docker endpoint. Host HTTP_PROXY values
        # must never redirect these calls outside the compose network.
        self.session.trust_env = False

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        try:
            response = self.session.request(
                method,
                f"{self.controller_url}{path}",
                headers=headers,
                timeout=timeout or self.timeout_seconds,
                **kwargs,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MihomoUnavailableError(f"Mihomo 控制器不可用: {exc}") from exc
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise MihomoUnavailableError("Mihomo 控制器返回了无效 JSON") from exc
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _latest_history(info: dict[str, Any]) -> dict[str, Any]:
        history = info.get("history") or []
        if not isinstance(history, list):
            return {}
        for item in reversed(history):
            if isinstance(item, dict):
                return item
        return {}

    def refresh_group_delay(
        self,
        *,
        url: str | None = None,
        timeout_ms: int = 5000,
    ) -> dict[str, int]:
        group = quote(self.group, safe="")
        test_url = url or os.getenv("MIHOMO_DELAY_TEST_URL", DEFAULT_DELAY_TEST_URL)
        try:
            res = self._request(
                "GET",
                f"/group/{group}/delay",
                timeout=max(int(timeout_ms / 1000) + 5, 10),
                params={
                    "timeout": timeout_ms,
                    "url": test_url,
                },
            )
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    def list_proxy_providers(self) -> dict[str, Any]:
        providers = self._request("GET", "/providers/proxies").get("providers") or {}
        return providers if isinstance(providers, dict) else {}

    def refresh_proxy_provider(self, provider_name: str) -> None:
        normalized = str(provider_name or "").strip()
        if not normalized:
            raise MihomoNodeError("未指定代理订阅来源")
        self._request("PUT", f"/providers/proxies/{quote(normalized, safe='')}")

    def reload_config(self) -> None:
        reload_path = str(
            os.getenv("MIHOMO_CONFIG_RELOAD_PATH", "/root/.config/mihomo/config.yaml")
        ).strip()
        self._request(
            "PUT",
            "/configs",
            params={"force": "true"},
            json={"path": reload_path},
        )

    def _node_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.node_state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"disabled": []}
        except Exception:
            return {"disabled": []}

    def _disabled_nodes(self) -> set[str]:
        with self._node_state_lock:
            return {
                str(item).strip()
                for item in self._node_state().get("disabled", [])
                if str(item).strip()
            }

    def is_node_enabled(self, node_name: str) -> bool:
        return str(node_name or "").strip() not in self._disabled_nodes()

    def set_node_enabled(self, node_name: str, enabled: bool) -> None:
        normalized = str(node_name or "").strip()
        if not normalized:
            raise MihomoNodeError("未指定代理节点")
        known_nodes = {
            str(item.get("name") or "").strip()
            for item in self.list_nodes().get("nodes", [])
        }
        if normalized not in known_nodes:
            raise MihomoNodeError(f"代理节点不存在: {normalized}")
        with self._node_state_lock:
            disabled = self._disabled_nodes()
            if enabled:
                disabled.discard(normalized)
            else:
                disabled.add(normalized)
            self.node_state_file.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2)
            temporary = self.node_state_file.with_suffix(self.node_state_file.suffix + ".tmp")
            try:
                temporary.write_text(data, encoding="utf-8")
                temporary.replace(self.node_state_file)
            except OSError:
                self.node_state_file.write_text(data, encoding="utf-8")
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass

    def list_nodes(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            try:
                self.refresh_group_delay()
            except MihomoError:
                # Existing health history is still useful if an on-demand
                # delay test happens to fail.
                pass

        group_name = quote(self.group, safe="")
        group_info = self._request("GET", f"/proxies/{group_name}")
        all_info = self._request("GET", "/proxies").get("proxies") or {}
        names = group_info.get("all") or []
        selected = str(group_info.get("now") or "")
        usable_names = [
            str(raw_name or "").strip()
            for raw_name in names
            if _is_usable_proxy_name(str(raw_name or "").strip())
        ]
        provider_info: dict[str, Any] = {}
        if any(
            not isinstance(all_info.get(str(name)), dict)
            or not all_info.get(str(name), {}).get("type")
            for name in usable_names
        ):
            try:
                providers = self.list_proxy_providers()
                if isinstance(providers, dict):
                    for provider_name, provider in providers.items():
                        if not isinstance(provider, dict):
                            continue
                        for proxy in provider.get("proxies") or []:
                            if not isinstance(proxy, dict):
                                continue
                            proxy_name = str(proxy.get("name") or "").strip()
                            if proxy_name:
                                provider_info[proxy_name] = proxy
            except MihomoError:
                # Older controller versions may not expose provider details.
                pass
        disabled_nodes = self._disabled_nodes()
        nodes: list[dict[str, Any]] = []
        for raw_name in names:
            name = str(raw_name or "").strip()
            if (
                not _is_usable_proxy_name(name)
                or name.upper() in {"DIRECT", "REJECT", "PASS", "COMPATIBLE"}
            ):
                continue
            info = all_info.get(name) if isinstance(all_info, dict) else {}
            if not isinstance(info, dict) or not info.get("type"):
                info = provider_info.get(name) or info
            info = info if isinstance(info, dict) else {}
            latest = self._latest_history(info)
            try:
                delay = max(int(latest.get("delay") or info.get("delay") or 0), 0)
            except (TypeError, ValueError):
                delay = 0
            alive_value = info.get("alive")
            alive = alive_value if isinstance(alive_value, bool) else (delay > 0 if latest else None)
            nodes.append(
                {
                    "name": name,
                    "type": str(info.get("type") or "unknown"),
                    "alive": alive,
                    "delay": delay or None,
                    "last_test": str(latest.get("time") or ""),
                    "udp": bool(info.get("udp", False)),
                    "selected": name == selected,
                }
            )

        nodes.sort(
            key=lambda item: (
                item["name"] in disabled_nodes,
                item["alive"] is False,
                item["delay"] is None,
                int(item["delay"] or 999999),
                item["name"].lower(),
            )
        )
        return {
            "available": True,
            "group": self.group,
            "selected": selected,
            "nodes": nodes,
            "error": "",
        }

    def probe_registration_node(self, node_name: str, *, slot: int) -> dict[str, Any]:
        """Check one node against the actual ChatGPT login page.

        Mihomo's generic delay test only proves basic Internet access.  This
        probe catches the much more common failure where Cloudflare rejects
        the exit IP with the VPN-specific "Unable to load site" page.
        """
        normalized = str(node_name or "").strip()
        if not normalized:
            return {
                "eligible": False,
                "classification": "invalid_node",
                "status": 0,
                "detail": "empty node name",
            }
        group_name = quote(self.slot_group_name(slot), safe="")
        try:
            self._request(
                "PUT",
                f"/proxies/{group_name}",
                json={"name": normalized},
            )
        except Exception as exc:
            return {
                "eligible": False,
                "classification": "controller_error",
                "status": 0,
                "detail": str(exc)[:240],
            }

        proxy = self.slot_proxy_url(slot)
        try:
            timeout = float(os.getenv("MIHOMO_CHATGPT_PREFLIGHT_TIMEOUT_SECONDS", "12"))
        except (TypeError, ValueError):
            timeout = 12.0
        session = None
        try:
            target_url = os.getenv("MIHOMO_CHATGPT_PREFLIGHT_URL", DEFAULT_CHATGPT_PREFLIGHT_URL)
            response = None
            try:
                from curl_cffi import requests as curl_requests

                response = curl_requests.get(
                    target_url,
                    headers={
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/136.0.0.0 Safari/537.36"
                        ),
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                    proxies={"http": proxy, "https": proxy},
                    timeout=(min(timeout, 6.0), timeout),
                    impersonate="chrome136",
                    allow_redirects=True,
                )
            except (ImportError, Exception):
                # Fallback to standard requests if curl_cffi fails or is unavailable
                session = requests.Session()
                session.trust_env = False
                response = session.get(
                    target_url,
                    headers={
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/136.0.0.0 Safari/537.36"
                        ),
                        "accept": "text/html,application/xhtml+xml",
                    },
                    proxies={"http": proxy, "https": proxy},
                    timeout=(min(timeout, 6.0), timeout),
                    allow_redirects=True,
                )
            status = int(getattr(response, "status_code", 0) or 0)
            body = re.sub(r"\s+", " ", str(getattr(response, "text", "") or "")).lower()[:6000]
            hard_marker = next(
                (marker for marker in _CHATGPT_HARD_BLOCK_MARKERS if marker in body),
                "",
            )
            challenge_marker = next(
                (marker for marker in _CHATGPT_CHALLENGE_MARKERS if marker in body),
                "",
            )
            if hard_marker:
                return {
                    "eligible": False,
                    "classification": "vpn_block",
                    "status": status,
                    "detail": hard_marker,
                }
            if challenge_marker or status == 403:
                # A real Camoufox context may solve this.  Keep it eligible,
                # but let the browser retry/rotation path decide at runtime.
                return {
                    "eligible": True,
                    "classification": "cloudflare_challenge",
                    "status": status,
                    "detail": challenge_marker or f"HTTP {status}",
                }
            eligible = status == 200
            return {
                "eligible": eligible,
                "classification": "ok" if eligible else "http_error",
                "status": status,
                "detail": f"HTTP {status}",
            }
        except Exception as exc:
            return {
                "eligible": False,
                "classification": "network_error",
                "status": 0,
                "detail": str(exc)[:240],
            }
        finally:
            if session is not None:
                session.close()

    def preflight_registration_nodes(
        self, node_names: list[str]
    ) -> dict[str, dict[str, Any]]:
        normalized = [str(item or "").strip() for item in node_names if str(item or "").strip()]
        if not normalized:
            return {}
        try:
            configured_workers = int(os.getenv("MIHOMO_CHATGPT_PREFLIGHT_WORKERS", "12"))
        except (TypeError, ValueError):
            configured_workers = 12
        workers = min(max(configured_workers, 1), self.slot_count)
        results: dict[str, dict[str, Any]] = {}
        # Probe in batches so no two requests ever switch the same listener at
        # the same time when a subscription contains more nodes than slots.
        for start in range(0, len(normalized), self.slot_count):
            batch = normalized[start : start + self.slot_count]
            with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
                futures = {
                    executor.submit(self.probe_registration_node, node, slot=index + 1): node
                    for index, node in enumerate(batch)
                }
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        results[node] = future.result()
                    except Exception as exc:
                        results[node] = {
                            "eligible": False,
                            "classification": "probe_error",
                            "status": 0,
                            "detail": str(exc)[:240],
                        }
        return results

    def validate_node(self, node_name: str) -> dict[str, Any]:
        normalized = str(node_name or "").strip()
        if not normalized:
            raise MihomoNodeError("未选择代理节点")
        node = next(
            (item for item in self.list_nodes().get("nodes", []) if item["name"] == normalized),
            None,
        )
        if not node:
            raise MihomoNodeError(f"代理节点不存在或不属于当前节点组: {normalized}")
        if not self.is_node_enabled(normalized):
            raise MihomoNodeError(f"代理节点已停用: {normalized}")
        if node.get("alive") is False:
            raise MihomoNodeError(f"代理节点当前不可用: {normalized}")
        return node

    def healthy_node_candidates(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Return enabled Mihomo nodes that are not marked offline.

        The selector's ``now`` value can remain pinned to a dead node after a
        provider refresh.  Callers that use the default route must therefore
        choose from the health table instead of blindly reusing ``now``.
        ``alive=None`` is retained as a last-resort candidate because older
        controllers do not report a delay until the first probe completes.
        """
        info = self.list_nodes(refresh=refresh)
        nodes = [
            item
            for item in list(info.get("nodes") or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and item.get("alive") is not False
            and self.is_node_enabled(str(item.get("name") or ""))
        ]
        selected = str(info.get("selected") or "").strip()
        nodes.sort(
            key=lambda item: (
                0 if str(item.get("name") or "") == selected and item.get("alive") is True else 1,
                0 if item.get("alive") is True else 1,
                item.get("delay") is None,
                int(item.get("delay") or 999999),
                str(item.get("name") or "").lower(),
            )
        )
        return nodes

    def activate_healthy_node(
        self,
        *,
        exclude: set[str] | None = None,
        refresh: bool = True,
    ) -> tuple[str, str]:
        """Activate the best currently healthy node and return ``(name, url)``."""
        excluded = {str(item or "").strip() for item in (exclude or set())}
        last_error: Exception | None = None
        for item in self.healthy_node_candidates(refresh=refresh):
            node_name = str(item.get("name") or "").strip()
            if not node_name or node_name in excluded:
                continue
            try:
                return node_name, self.activate_node(node_name)
            except Exception as exc:
                last_error = exc
                excluded.add(node_name)
        if last_error is not None:
            raise MihomoNodeError(f"没有可激活的健康代理节点: {last_error}") from last_error
        raise MihomoNodeError("当前 Mihomo 节点组没有可用的健康节点")

    def activate_node(self, node_name: str) -> str:
        node = self.validate_node(node_name)
        group_name = quote(self.group, safe="")
        self._request(
            "PUT",
            f"/proxies/{group_name}",
            json={"name": node["name"]},
        )
        return self.proxy_url

    def slot_group_name(self, slot: int) -> str:
        slot = int(slot)
        if slot < 1 or slot > self.slot_count:
            raise MihomoNodeError(f"proxy slot out of range: {slot}")
        return f"{self.slot_group_prefix}{slot:02d}"

    def slot_proxy_url(self, slot: int) -> str:
        parsed = urlsplit(self.proxy_url)
        hostname = parsed.hostname or "mihomo"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.username:
            netloc = f"{parsed.username}:{parsed.password or ''}@{netloc}"
        netloc = f"{netloc}:{self.slot_port_base + int(slot)}"
        return urlunsplit((parsed.scheme or "http", netloc, "", "", ""))

    def activate_slot_node(self, slot: int, node_name: str) -> str:
        node = self.validate_node(node_name)
        group_name = quote(self.slot_group_name(slot), safe="")
        self._request(
            "PUT",
            f"/proxies/{group_name}",
            json={"name": node["name"]},
        )
        return self.slot_proxy_url(slot)

    def create_registration_allocator(
        self,
        preferred_node: str | None = None,
        *,
        preflight: bool = False,
    ) -> MihomoRegistrationAllocator:
        return MihomoRegistrationAllocator(
            self,
            preferred_node=preferred_node,
            preflight=preflight,
        )


mihomo_client = MihomoClient()
