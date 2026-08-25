import json

import pytest

import docker_manager


@pytest.fixture(autouse=True)
def isolated_server_registry(tmp_path, monkeypatch):
    """Tests must never depend on a real homelab's personal registry."""
    registry = tmp_path / "servers.json"
    registry.write_text(json.dumps({
        "paper": {
            "display_name": "Paper",
            "container": "minecraft-paper",
            "compose_service": "paper",
            "compose_file": "/tmp/paper-compose.yaml",
            "server_type": "PAPER",
            "supports_paper_metrics": True,
            "warn_before_start": False,
        },
        "pixelmon": {
            "display_name": "Pixelmon",
            "container": "minecraft-pixelmon",
            "compose_service": "pixelmon",
            "compose_file": "/tmp/pixelmon-compose.yaml",
            "server_type": "NEOFORGE",
            "supports_paper_metrics": False,
            "warn_before_start": True,
        },
    }), encoding="utf-8")
    monkeypatch.setattr(docker_manager, "CONFIG_FILE", registry)
