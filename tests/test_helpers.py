import json

import docker_manager
import minecraft
import network_manager
import system_info
import server_manager
from unittest.mock import Mock, patch


def test_java_memory_parser():
    assert docker_manager.memory_to_gib("4G") == 4
    assert docker_manager.memory_to_gib("3072M") == 3
    assert docker_manager.memory_to_gib("not set") is None
    assert docker_manager.parse_memory("4096M") == 4 * 1024 ** 3
    assert docker_manager.format_memory(3 * 1024 ** 3) == "3 GiB"
    assert docker_manager.normalize_memory(" 4096mb ") == "4096M"


def test_compose_memory_change_only_updates_selected_service(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  paper:\n"
        "    environment:\n"
        "      MEMORY: \"4G\"\n"
        "  pixelmon:\n"
        "    environment:\n"
        "      MEMORY: \"3G\"\n",
        encoding="utf-8",
    )
    result = docker_manager.change_server_memory(
        "paper", "6144M", compose_file=compose, state_directory=tmp_path, run_command=False
    )
    changed = compose.read_text(encoding="utf-8")
    assert 'MEMORY: "6144M"' in changed
    assert 'MEMORY: "3G"' in changed
    assert result["changed"] is True
    assert list((tmp_path / "compose-backups").glob("*.yaml"))


def test_stopped_server_memory_recreation_does_not_start_it(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  paper:\n    environment:\n      MEMORY: \"4G\"\n", encoding="utf-8")
    stopped_container = Mock(attrs={"State": {"Running": False}})
    with patch("docker_manager.get_client", return_value=object()), \
         patch("docker_manager.get_container", return_value=stopped_container), \
         patch("docker_manager.subprocess.run") as run:
        docker_manager.change_server_memory("paper", "5G", compose_file=compose, state_directory=tmp_path)
    command = run.call_args.args[0]
    assert "create" in command
    assert "up" not in command
    assert "--no-deps" not in command


def test_host_reserve_scales_with_physical_ram():
    gib = 1024 ** 3
    assert system_info.recommended_host_reserve(4 * gib) == 1 * gib
    assert system_info.recommended_host_reserve(16 * gib) == int(16 * gib * 0.15)
    assert system_info.recommended_host_reserve(32 * gib) == int(32 * gib * 0.15)


def test_resource_summary_includes_all_registered_servers():
    gib = 1024 ** 3
    host = {"ram_total_bytes": 16 * gib}
    servers = {
        "paper": {"state": "running", "configured_memory_bytes": 4 * gib},
        "pixelmon": {"state": "stopped", "configured_memory_bytes": 3 * gib},
        "creative": {"state": "running", "configured_memory_bytes": 2 * gib},
    }
    summary = system_info.get_resource_summary(host, servers)
    assert summary["minecraft_configured_bytes"] == 9 * gib
    assert summary["minecraft_running_bytes"] == 6 * gib
    assert summary["planned_heap_headroom_bytes"] > 0


def test_container_allowlist_rejects_unknown_name():
    try:
        docker_manager.get_container(object(), "anything-else")
    except ValueError as exc:
        assert str(exc) == "Unknown server"
    else:
        raise AssertionError("Unknown server key should be rejected")


def test_server_registry_can_load_additional_servers(tmp_path):
    registry = tmp_path / "servers.json"
    registry.write_text(
        '{"creative": {"display_name": "Creative", "container": "minecraft-creative", '
        '"compose_service": "creative", "compose_file": "/srv/creative/compose.yaml"}}',
        encoding="utf-8",
    )
    servers = docker_manager.load_servers(registry)
    assert servers["creative"]["label"] == "Creative"
    assert servers["creative"]["container"] == "minecraft-creative"


def test_empty_registry_supports_a_clean_first_run(tmp_path):
    registry = tmp_path / "servers.json"
    registry.write_text("{}\n", encoding="utf-8")
    assert docker_manager.load_servers(registry) == {}


def test_new_server_validation_and_java_image_selection():
    settings = server_manager.validate_server_settings({
        "server_id": "creative",
        "display_name": "Creative",
        "server_type": "paper",
        "version": "1.21.1",
        "memory": "2048M",
        "max_players": "20",
        "motd": "Creative server",
        "whitelist": True,
        "public_hostname": "creative.example.com",
    })
    assert settings["server_type"] == "PAPER"
    assert settings["memory"] == "2048M"
    assert settings["image"] == "itzg/minecraft-server:stable-java21"
    assert server_manager.select_image("LATEST") == "itzg/minecraft-server:stable"
    assert server_manager.select_image("1.20.1") == "itzg/minecraft-server:stable-java17"


def test_local_port_allocator_skips_registry_docker_and_listener(monkeypatch):
    monkeypatch.setattr(server_manager, "docker_assigned_ports", lambda: {25566})
    monkeypatch.setattr(server_manager.socket, "socket", Mock())
    servers = {"existing": {"local_port": 25565}}
    assert server_manager.allocate_local_port(servers, start=25565, end=25568) == 25567


def test_create_server_uses_persistent_files_and_registry_without_real_docker(tmp_path, monkeypatch):
    registry = tmp_path / "servers.json"
    registry.write_text(
        '{"paper": {"container": "minecraft-paper", "compose_service": "paper", '
        '"compose_file": "/tmp/paper.yaml"}}\n',
        encoding="utf-8",
    )
    server_root = tmp_path / "servers"
    state = tmp_path / "state"
    monkeypatch.setattr(docker_manager, "CONFIG_FILE", registry)
    monkeypatch.setattr(docker_manager, "STATE_DIRECTORY", state)
    monkeypatch.setattr(server_manager, "SERVER_ROOT", server_root)
    monkeypatch.setattr(server_manager, "allocate_local_port", lambda servers=None: 25565)
    monkeypatch.setattr(server_manager, "container_exists", lambda name: False)
    with patch("server_manager.subprocess.run") as run:
        result = server_manager.create_server({
            "server_id": "creative",
            "display_name": "Creative",
            "server_type": "PAPER",
            "version": "LATEST",
            "memory": "2G",
            "max_players": 20,
            "motd": "Creative server",
            "whitelist": True,
            "public_hostname": "creative.example.com",
        }, 25565)
    compose = (server_root / "creative" / "compose.yaml").read_text(encoding="utf-8")
    saved = docker_manager.load_servers(registry)
    assert (server_root / "creative" / "data").is_dir()
    assert "127.0.0.1:25565:25565" in compose
    assert "RCON_PASSWORD:" not in compose
    assert saved["creative"]["data_path"] == str(server_root / "creative" / "data")
    assert result["local_port"] == 25565
    assert run.call_args.args[0][-2:] == ["up", "-d"]


def test_playit_endpoint_validation_and_metadata_save(tmp_path, monkeypatch):
    registry = tmp_path / "servers.json"
    registry.write_text(
        '{"creative": {"container": "minecraft-creative", "compose_service": "server", '
        '"compose_file": "/tmp/creative.yaml"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(docker_manager, "CONFIG_FILE", registry)
    monkeypatch.setattr(docker_manager, "STATE_DIRECTORY", tmp_path / "state")
    endpoint = server_manager.save_playit_endpoint("creative", "Example.GL.JoinMC.Link")
    saved = docker_manager.load_servers(registry)["creative"]
    assert endpoint == "example.gl.joinmc.link"
    assert saved["playit_status"] == "configured"
    assert saved["playit_endpoint"] == endpoint
    try:
        server_manager.validate_playit_endpoint("https://playit.gg/not-an-endpoint")
    except ValueError:
        pass
    else:
        raise AssertionError("URLs must not be accepted as Playit endpoints")


def test_remove_managed_server_archives_files(tmp_path, monkeypatch):
    server_root = tmp_path / "servers"
    server_directory = server_root / "test"
    server_directory.mkdir(parents=True)
    compose = server_directory / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (server_directory / "data").mkdir()
    registry = tmp_path / "servers.json"
    registry.write_text(
        '{"test": {"managed": true, "compose_service": "server", "compose_file": "' + str(compose) + '"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_manager, "SERVER_ROOT", server_root)
    monkeypatch.setattr(docker_manager, "CONFIG_FILE", registry)
    monkeypatch.setattr(docker_manager, "STATE_DIRECTORY", tmp_path / "state")

    with patch("server_manager.subprocess.run") as run:
        archive = server_manager.remove_server("test")

    assert archive.parent == server_root / ".removed"
    assert (archive / "compose.yaml").exists()
    assert not server_directory.exists()
    assert docker_manager.load_servers(registry) == {}
    assert run.call_args.args[0][-1] == "down"


def test_remove_imported_server_keeps_its_files(tmp_path, monkeypatch):
    data = tmp_path / "paper-data"
    data.mkdir()
    (data / "world.dat").write_text("keep", encoding="utf-8")
    registry = tmp_path / "servers.json"
    registry.write_text(
        '{"paper": {"managed": false, "container": "minecraft-paper", '
        '"data_path": "' + str(data) + '"}}\n', encoding="utf-8",
    )
    monkeypatch.setattr(docker_manager, "CONFIG_FILE", registry)
    monkeypatch.setattr(docker_manager, "STATE_DIRECTORY", tmp_path / "state")
    container = Mock(attrs={"State": {"Running": True}})
    client = Mock()
    client.containers.get.return_value = container
    monkeypatch.setattr(docker_manager, "get_client", lambda: client)

    assert server_manager.remove_server("paper") is None
    container.stop.assert_called_once_with(timeout=60)
    container.remove.assert_called_once()
    assert (data / "world.dat").read_text(encoding="utf-8") == "keep"
    assert json.loads(registry.read_text(encoding="utf-8")) == {}


def test_minecraft_and_pixelmon_version_detection(tmp_path):
    data = tmp_path / "data"
    mods = data / "mods"
    mods.mkdir(parents=True)
    (data / "minecraft_server.26.2.jar").touch()
    (mods / "Pixelmon-1.21.1-9.3.16-universal.jar").touch()
    details = {"data_path": str(data)}

    assert docker_manager.get_minecraft_version(details, {"VERSION": "LATEST"}) == "26.2"
    assert docker_manager.get_minecraft_version(details, {"VERSION": "1.21.1"}) == "1.21.1"
    assert docker_manager.get_mod_info(details) == ("Pixelmon", "9.3.16")


def test_removed_server_archives_can_be_listed_and_deleted(tmp_path, monkeypatch):
    server_root = tmp_path / "servers"
    archive = server_root / ".removed" / "test-20260825-120000"
    archive.mkdir(parents=True)
    (archive / "world.dat").write_bytes(b"world")
    monkeypatch.setattr(server_manager, "SERVER_ROOT", server_root)

    listed = server_manager.list_removed_servers()
    assert listed[0]["name"] == archive.name
    assert listed[0]["size"] == "5 B"
    server_manager.delete_removed_server(archive.name)
    assert not archive.exists()


def test_removed_server_delete_rejects_arbitrary_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(server_manager, "SERVER_ROOT", tmp_path / "servers")
    for name in ("../paper", "paper", "/srv/minecraft/paper"):
        try:
            server_manager.delete_removed_server(name)
        except ValueError:
            pass
        else:
            raise AssertionError("Arbitrary paths must not be deletable")


def test_dns_guidance_matches_playit_cname_and_srv(monkeypatch):
    monkeypatch.setattr(network_manager, "DNS_ZONE", "example.com")
    answers = {
        ("example.com", "NS"): ["keenan.ns.cloudflare.com"],
        ("creative.example.com", "CNAME"): ["example.gl.joinmc.link"],
        ("_minecraft._tcp.creative.example.com", "SRV"): ["1 1 30123 example.gl.at.ply.gg"],
        ("_minecraft._tcp.example.gl.joinmc.link", "SRV"): ["1 1 30123 example.gl.at.ply.gg"],
    }
    monkeypatch.setattr(network_manager, "dig_short", lambda name, record_type: answers.get((name, record_type), []))
    guidance = network_manager.build_dns_guidance({
        "public_hostname": "creative.example.com",
        "playit_endpoint": "example.gl.joinmc.link",
    })
    assert guidance["cloudflare"] is True
    assert guidance["configured"] is True
    assert guidance["records"] == [
        {"type": "CNAME", "name": "creative", "target": "example.gl.joinmc.link", "proxy": "DNS only"},
        {"type": "SRV", "name": "_minecraft._tcp.creative", "priority": 1, "weight": 1,
         "port": 30123, "target": "example.gl.at.ply.gg"},
    ]


def test_username_validation():
    assert minecraft.validate_username("player_123")
    assert minecraft.validate_username("V1CT0R2921")
    assert not minecraft.validate_username("ab")
    assert not minecraft.validate_username("name; stop")
    assert not minecraft.validate_username("a" * 17)


def test_player_and_whitelist_parsing():
    players = minecraft.parse_players("There are 2 of a max of 20 players online: Alice, Bob")
    assert players == {"online": 2, "max": 20, "names": ["Alice", "Bob"]}
    assert minecraft.parse_whitelist("There are 2 whitelisted player(s): Alice, Bob") == ["Alice", "Bob"]


def test_paper_metric_parsing():
    assert minecraft.parse_tps("TPS from last 1m, 5m, 15m: 20.0, 19.9, 20.0") == 20.0
    assert minecraft.parse_mspt("Server tick times (avg/min/max) from last 5s, 10s, 1m:\n◴ 18.7/1.0/40.0") == 18.7


def test_minecraft_color_codes_are_removed_before_parsing():
    response = minecraft.clean_response("§6TPS from last 1m, 5m, 15m: §a20.0§r, §a20.0§r, §a20.0")
    assert minecraft.parse_tps(response) == 20.0
