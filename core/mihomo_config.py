"""Persist and update Mihomo HTTP subscription sources."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "deploy" / "mihomo" / "config.yaml"
DEFAULT_CONFIG_TEMPLATE = ROOT_DIR / "deploy" / "mihomo" / "config.example.yaml"
SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class MihomoConfigError(RuntimeError):
    pass


class MihomoConfigManager:
    def __init__(self, config_path: str | Path | None = None) -> None:
        configured = config_path or os.getenv("MIHOMO_CONFIG_FILE", "") or DEFAULT_CONFIG_PATH
        self.config_path = Path(configured).expanduser()
        self._lock = threading.RLock()

    @staticmethod
    def _safe_document(text: str) -> dict[str, Any]:
        try:
            document = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise MihomoConfigError(f"Mihomo 配置文件不是有效 YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise MihomoConfigError("Mihomo 配置文件根节点必须是对象")
        return document

    def _read(self, *, initialize: bool = False) -> dict[str, Any]:
        if self.config_path.exists() and self.config_path.is_file():
            return self._safe_document(self.config_path.read_text(encoding="utf-8-sig"))
        if not initialize:
            return {}
        if not DEFAULT_CONFIG_TEMPLATE.exists():
            raise MihomoConfigError("未找到 Mihomo 配置模板")
        document = self._safe_document(DEFAULT_CONFIG_TEMPLATE.read_text(encoding="utf-8-sig"))
        # The template contains a non-working placeholder. The first source
        # created in the UI becomes the actual initial provider.
        document["proxy-providers"] = {}
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=4096,
        )
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.config_path)
        except OSError:
            # When config_path is a Docker bind mount (e.g. single-file mount),
            # atomic rename fails with EBUSY (Errno 16). Fall back to writing
            # directly in place to preserve the mount and inode across containers.
            self.config_path.write_text(text, encoding="utf-8")
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _validate_url(url: str) -> str:
        normalized = str(url or "").strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MihomoConfigError("订阅 URL 必须是有效的 http/https 地址")
        return normalized

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not SOURCE_NAME_RE.fullmatch(normalized):
            raise MihomoConfigError("来源名称只能包含字母、数字、点、下划线和连字符")
        return normalized

    @staticmethod
    def _providers(document: dict[str, Any]) -> dict[str, Any]:
        providers = document.get("proxy-providers") or {}
        if not isinstance(providers, dict):
            raise MihomoConfigError("Mihomo proxy-providers 配置无效")
        return dict(providers)

    @staticmethod
    def _sync_registration_groups(document: dict[str, Any], provider_names: list[str]) -> None:
        groups = document.get("proxy-groups") or []
        if not isinstance(groups, list):
            groups = []
        by_name = {
            str(item.get("name") or ""): item
            for item in groups
            if isinstance(item, dict) and item.get("name")
        }

        def ensure_group(name: str, *, filter_value: str = "") -> None:
            group = by_name.get(name)
            if group is None:
                group = {"name": name, "type": "select"}
                groups.append(group)
                by_name[name] = group
            group["type"] = "select"
            group["use"] = list(provider_names)
            group.pop("proxies", None)
            if filter_value:
                group["filter"] = filter_value

        ensure_group("REGISTER-ALL")
        ensure_group(
            "REGISTER-US",
            filter_value=r"(?i)(🇺🇸|美国|美國|United States|\bUS\b|USA)",
        )
        for group in groups:
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "")
            if name.startswith("REGISTER-SLOT-"):
                group["type"] = "select"
                group["use"] = list(provider_names)
                group.pop("proxies", None)
        document["proxy-groups"] = groups

    @staticmethod
    def _sync_rules(document: dict[str, Any], providers: dict[str, Any]) -> None:
        rules = document.get("rules") or []
        if not isinstance(rules, list):
            rules = []
        rule_set = set(rules)
        for config in providers.values():
            if isinstance(config, dict):
                url = str(config.get("url") or "").strip()
                if url:
                    hostname = urlsplit(url).hostname
                    if hostname:
                        rule = f"DOMAIN,{hostname},DIRECT"
                        if rule not in rule_set:
                            match_idx = next(
                                (i for i, r in enumerate(rules) if str(r).startswith("MATCH,")),
                                len(rules),
                            )
                            rules.insert(match_idx, rule)
                            rule_set.add(rule)
        document["rules"] = rules

    def list_sources(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            providers = self._providers(document) if document else {}
        sources: list[dict[str, Any]] = []
        for name, raw_config in providers.items():
            config = raw_config if isinstance(raw_config, dict) else {}
            if str(config.get("type") or "http").lower() != "http":
                continue
            url = str(config.get("url") or "").strip()
            sources.append(
                {
                    "name": str(name),
                    "url": url,
                    "interval": int(config.get("interval") or 3600),
                    "path": str(config.get("path") or ""),
                }
            )
        return sorted(sources, key=lambda item: item["name"].lower())

    def create_source(self, *, name: str, url: str, interval: int = 3600) -> dict[str, Any]:
        normalized_name = self._validate_name(name)
        normalized_url = self._validate_url(url)
        normalized_interval = min(max(int(interval or 3600), 60), 86400)
        with self._lock:
            document = self._read(initialize=True)
            providers = self._providers(document)
            if normalized_name in providers:
                raise MihomoConfigError(f"订阅来源已存在: {normalized_name}")
            providers[normalized_name] = self._source_config(
                normalized_name,
                normalized_url,
                normalized_interval,
            )
            document["proxy-providers"] = providers
            self._sync_registration_groups(document, list(providers))
            self._sync_rules(document, providers)
            self._write(document)
        return next(item for item in self.list_sources() if item["name"] == normalized_name)

    def update_source(
        self,
        source_name: str,
        *,
        name: str,
        url: str,
        interval: int = 3600,
    ) -> dict[str, Any]:
        current_name = self._validate_name(source_name)
        normalized_name = self._validate_name(name)
        normalized_url = self._validate_url(url)
        normalized_interval = min(max(int(interval or 3600), 60), 86400)
        with self._lock:
            document = self._read()
            providers = self._providers(document)
            if current_name not in providers:
                raise MihomoConfigError(f"订阅来源不存在: {current_name}")
            if normalized_name != current_name and normalized_name in providers:
                raise MihomoConfigError(f"订阅来源已存在: {normalized_name}")
            updated: dict[str, Any] = {}
            for provider_name, config in providers.items():
                if provider_name == current_name:
                    updated[normalized_name] = self._source_config(
                        normalized_name,
                        normalized_url,
                        normalized_interval,
                    )
                else:
                    updated[provider_name] = config
            document["proxy-providers"] = updated
            self._sync_registration_groups(document, list(updated))
            self._sync_rules(document, updated)
            self._write(document)
        return next(item for item in self.list_sources() if item["name"] == normalized_name)

    def delete_source(self, source_name: str) -> None:
        normalized_name = self._validate_name(source_name)
        with self._lock:
            document = self._read()
            providers = self._providers(document)
            if normalized_name not in providers:
                raise MihomoConfigError(f"订阅来源不存在: {normalized_name}")
            if len(providers) <= 1:
                raise MihomoConfigError("至少保留一个代理订阅来源")
            del providers[normalized_name]
            document["proxy-providers"] = providers
            self._sync_registration_groups(document, list(providers))
            self._sync_rules(document, providers)
            self._write(document)

    @staticmethod
    def _source_config(name: str, url: str, interval: int) -> dict[str, Any]:
        return {
            "type": "http",
            "url": url,
            "header": {"User-Agent": ["Clash.Meta"]},
            "path": f"./providers/{name}.yaml",
            "interval": interval,
            "health-check": {
                "enable": True,
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "lazy": False,
            },
        }


mihomo_config_manager = MihomoConfigManager()
