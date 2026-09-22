from __future__ import annotations

import yaml

from core.mihomo_config import MihomoConfigManager


def test_mihomo_source_manager_creates_updates_and_syncs_registration_groups(tmp_path):
    config_path = tmp_path / "config.yaml"
    manager = MihomoConfigManager(config_path)

    first = manager.create_source(
        name="primary-subscription",
        url="https://proxy.example/first?token=abc",
        interval=1800,
    )
    manager.create_source(
        name="backup-subscription",
        url="https://proxy.example/backup",
        interval=3600,
    )
    updated = manager.update_source(
        "backup-subscription",
        name="secondary-subscription",
        url="https://proxy.example/second",
        interval=7200,
    )

    assert first["name"] == "primary-subscription"
    assert updated["name"] == "secondary-subscription"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert list(document["proxy-providers"]) == [
        "primary-subscription",
        "secondary-subscription",
    ]
    register_groups = [
        item
        for item in document["proxy-groups"]
        if item["name"] == "REGISTER-ALL" or item["name"].startswith("REGISTER-SLOT-")
    ]
    assert register_groups
    assert all(
        item["use"] == ["primary-subscription", "secondary-subscription"]
        for item in register_groups
    )

    manager.delete_source("secondary-subscription")
    assert [item["name"] for item in manager.list_sources()] == ["primary-subscription"]


def test_proxy_source_api_persists_and_reloads(client, monkeypatch, tmp_path):
    manager = MihomoConfigManager(tmp_path / "config.yaml")
    reloads = []
    monkeypatch.setattr("api.proxy_nodes.mihomo_config_manager", manager)
    monkeypatch.setattr("api.proxy_nodes.mihomo_client.reload_config", lambda: reloads.append(True))
    monkeypatch.setattr(
        "api.proxy_nodes.mihomo_client.list_proxy_providers",
        lambda: {
            "primary": {
                "updatedAt": "2026-08-06T10:00:00Z",
                "proxies": [{"name": "Node A"}, {"name": "Node B"}],
            }
        },
    )

    created = client.post(
        "/api/proxy-nodes/sources",
        json={"name": "primary", "url": "https://proxy.example/sub", "interval": 1800},
    )
    listed = client.get("/api/proxy-nodes/sources")

    assert created.status_code == 200
    assert created.json()["reloaded"] is True
    assert reloads == [True]
    assert listed.json()["sources"][0]["node_count"] == 2
    assert listed.json()["sources"][0]["runtime_available"] is True


def test_proxy_node_can_be_disabled_through_api(client, monkeypatch, tmp_path):
    node = {
        "name": "Node A",
        "type": "VLESS",
        "alive": True,
        "delay": 30,
        "last_test": "",
        "udp": True,
        "selected": True,
    }
    monkeypatch.setattr("api.proxy_nodes.mihomo_client.node_state_file", tmp_path / "nodes.json")
    monkeypatch.setattr(
        "api.proxy_nodes.mihomo_client.list_nodes",
        lambda **_kwargs: {"available": True, "nodes": [dict(node)], "selected": "Node A"},
    )
    monkeypatch.setattr("api.proxy_nodes.mihomo_client.list_proxy_providers", lambda: {})

    response = client.put("/api/proxy-nodes/nodes/Node%20A", json={"enabled": False})
    listed = client.get("/api/proxy-nodes")

    assert response.status_code == 200
    assert listed.json()["nodes"][0]["enabled"] is False


def test_mihomo_config_manager_write_handles_ebusy_on_docker_bind_mount(monkeypatch, tmp_path):
    import errno
    from pathlib import Path

    config_path = tmp_path / "config.yaml"
    manager = MihomoConfigManager(config_path)

    # First write to create initial file
    manager.create_source(
        name="initial-subscription",
        url="https://proxy.example/first",
        interval=1800,
    )

    # Now simulate Docker bind-mount where Path.replace fails with EBUSY (Errno 16)
    original_replace = Path.replace

    def fake_replace(self, target):
        if str(self).endswith(".tmp"):
            raise OSError(errno.EBUSY, "Device or resource busy")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)

    # Adding another source should succeed via the fallback
    manager.create_source(
        name="second-subscription",
        url="https://proxy.example/second",
        interval=3600,
    )

    sources = manager.list_sources()
    assert [s["name"] for s in sources] == ["initial-subscription", "second-subscription"]
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "second-subscription" in document["proxy-providers"]
    assert not (tmp_path / "config.yaml.tmp").exists()

