"""Creation helpers for new, panel-managed Minecraft servers.

Existing imported servers are never moved through this module. New servers use
one readable Compose project and one persistent data directory each.
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

import docker_manager
from docker.errors import NotFound


SERVER_ROOT = Path(os.getenv("MINECRAFT_SERVER_ROOT", "/srv/minecraft/servers"))
SHARED_ENV_FILE = Path(os.getenv("MINECRAFT_SHARED_ENV_FILE", "/etc/minecraft-panel/minecraft.env"))
ALLOWED_TYPES = {"VANILLA", "PAPER", "FABRIC", "FORGE", "NEOFORGE"}
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
VERSION_PATTERN = re.compile(r"^(?:LATEST|\d+(?:\.\d+){1,2})$", re.IGNORECASE)
PLAYIT_ENDPOINT_PATTERN = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")


def select_image(minecraft_version):
    """Choose a stable itzg Java variant from the requested Minecraft version."""
    version = minecraft_version.upper()
    if version == "LATEST" or int(version.split(".")[0]) >= 26:
        return "itzg/minecraft-server:stable"
    parts = [int(part) for part in version.split(".")]
    minor = parts[1]
    patch = parts[2] if len(parts) > 2 else 0
    if minor > 20 or (minor == 20 and patch >= 5):
        return "itzg/minecraft-server:stable-java21"
    if minor >= 18:
        return "itzg/minecraft-server:stable-java17"
    if minor == 17:
        return "itzg/minecraft-server:java16"
    return "itzg/minecraft-server:stable-java8"


def validate_server_settings(values):
    server_id = str(values.get("server_id", "")).strip().lower()
    if not docker_manager.SERVER_ID_PATTERN.fullmatch(server_id):
        raise ValueError("Server name must use 1–32 lowercase letters, numbers, hyphens, or underscores")
    display_name = str(values.get("display_name", "")).strip()
    if not 1 <= len(display_name) <= 40:
        raise ValueError("Display name must be 1–40 characters")
    server_type = str(values.get("server_type", "")).strip().upper()
    if server_type not in ALLOWED_TYPES:
        raise ValueError("Choose Vanilla, Paper, Fabric, Forge, or NeoForge")
    version = str(values.get("version", "")).strip().upper()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Minecraft version must look like 1.21.1, 26.1, or LATEST")
    memory = docker_manager.normalize_memory(str(values.get("memory", "")))
    try:
        max_players = int(values.get("max_players", 20))
    except (TypeError, ValueError) as exc:
        raise ValueError("Max players must be a whole number") from exc
    if not 1 <= max_players <= 1000:
        raise ValueError("Max players must be between 1 and 1000")
    motd = str(values.get("motd", "")).strip()
    if not 1 <= len(motd) <= 120 or "\n" in motd or "\r" in motd:
        raise ValueError("MOTD must be 1–120 characters on one line")
    hostname = str(values.get("public_hostname", "")).strip().lower().rstrip(".")
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        raise ValueError("Enter a complete hostname such as creative.example.com")
    return {
        "server_id": server_id,
        "display_name": display_name,
        "server_type": server_type,
        "version": version,
        "memory": memory,
        "max_players": max_players,
        "motd": motd,
        "whitelist": values.get("whitelist") in {True, "true", "on", "1", 1},
        "public_hostname": hostname,
        "image": select_image(version),
    }


def docker_assigned_ports():
    ports = set()
    client = docker_manager.get_client()
    for container in client.containers.list(all=True):
        bindings = container.attrs.get("HostConfig", {}).get("PortBindings") or {}
        for entries in bindings.values():
            for entry in entries or []:
                if entry.get("HostPort", "").isdigit():
                    ports.add(int(entry["HostPort"]))
    return ports


def container_exists(container_name):
    try:
        docker_manager.get_client().containers.get(container_name)
        return True
    except NotFound:
        return False


def port_is_available(port):
    """Check both Docker assignments and the host TCP listener table."""
    if port in docker_assigned_ports():
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_local_port(servers=None, start=25565, end=25999):
    registered = servers if servers is not None else docker_manager.load_servers()
    assigned = {item.get("local_port") for item in registered.values() if item.get("local_port")}
    assigned.update(docker_assigned_ports())
    for port in range(start, end + 1):
        if port not in assigned:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            finally:
                sock.close()
            return port
    raise RuntimeError("No free local Minecraft port was found")


def render_compose(settings, local_port, data_directory):
    """Render deliberately small YAML; JSON strings are valid YAML strings."""
    quote = json.dumps
    environment = {
        "EULA": "TRUE",
        "TZ": "Europe/Lisbon",
        "TYPE": settings["server_type"],
        "VERSION": settings["version"],
        "MEMORY": settings["memory"],
        "MAX_PLAYERS": str(settings["max_players"]),
        "MOTD": settings["motd"],
        "ONLINE_MODE": "TRUE",
        "ENABLE_RCON": "TRUE",
        "ENABLE_WHITELIST": "TRUE" if settings["whitelist"] else "FALSE",
    }
    env_lines = "\n".join(f"      {key}: {quote(value)}" for key, value in environment.items())
    return (
        f"name: minecraft-{settings['server_id']}\n\n"
        "services:\n"
        "  server:\n"
        f"    image: {settings['image']}\n"
        f"    container_name: minecraft-{settings['server_id']}\n"
        "    restart: unless-stopped\n"
        "    tty: true\n"
        "    stdin_open: true\n"
        "    stop_grace_period: 2m\n"
        f"    env_file:\n      - {SHARED_ENV_FILE}\n"
        f"    environment:\n{env_lines}\n"
        f"    ports:\n      - \"127.0.0.1:{local_port}:25565\"\n"
        f"    volumes:\n      - {data_directory}:/data\n"
    )


def save_registry(raw_registry):
    path = docker_manager.CONFIG_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw_registry, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate_playit_endpoint(value):
    endpoint = str(value or "").strip().lower().rstrip(".")
    if not PLAYIT_ENDPOINT_PATTERN.fullmatch(endpoint) or ".." in endpoint:
        raise ValueError("Enter the Playit hostname or IP, optionally followed by :port")
    if ":" in endpoint:
        port = int(endpoint.rsplit(":", 1)[1])
        if not 1 <= port <= 65535:
            raise ValueError("The Playit endpoint port must be between 1 and 65535")
    return endpoint


def save_playit_endpoint(server_id, endpoint):
    """Save panel metadata only; this never calls or changes Playit itself."""
    endpoint = validate_playit_endpoint(endpoint)
    lock_path = docker_manager.STATE_DIRECTORY / "create-server.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_registry = (
            json.loads(docker_manager.CONFIG_FILE.read_text(encoding="utf-8"))
            if docker_manager.CONFIG_FILE.exists()
            else {}
        )
        if server_id not in raw_registry:
            raise ValueError("Unknown server")
        raw_registry[server_id]["playit_status"] = "configured"
        raw_registry[server_id]["playit_endpoint"] = endpoint
        save_registry(raw_registry)
    return endpoint


def local_port_status(server):
    port = server.get("local_port")
    if not port:
        return "not_applicable"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return "listening" if sock.connect_ex(("127.0.0.1", int(port))) == 0 else "not_listening"
    finally:
        sock.close()


def create_server(values, expected_port):
    """Create one new Compose project, keeping partial data if startup fails."""
    settings = validate_server_settings(values)
    lock_path = docker_manager.STATE_DIRECTORY / "create-server.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_registry = json.loads(docker_manager.CONFIG_FILE.read_text(encoding="utf-8"))
        if settings["server_id"] in raw_registry:
            raise ValueError("That server name is already registered")
        container_name = f"minecraft-{settings['server_id']}"
        if any(item.get("container") == container_name for item in raw_registry.values()):
            raise ValueError("That Docker container name is already registered")
        if container_exists(container_name):
            raise ValueError("A Docker container with that name already exists")
        local_port = int(expected_port)
        if local_port != allocate_local_port(docker_manager.load_servers()):
            raise ValueError("The reviewed local port is no longer available; review the server again")

        server_directory = SERVER_ROOT / settings["server_id"]
        data_directory = server_directory / "data"
        compose_file = server_directory / "compose.yaml"
        if server_directory.exists():
            raise ValueError("That server directory already exists")
        data_directory.mkdir(parents=True)
        compose_file.write_text(render_compose(settings, local_port, data_directory), encoding="utf-8")

        metadata = {
            "display_name": settings["display_name"],
            "container": container_name,
            "compose_service": "server",
            "compose_file": str(compose_file),
            "data_path": str(data_directory),
            "server_type": settings["server_type"],
            "minecraft_version": settings["version"],
            "public_hostname": settings["public_hostname"],
            "local_port": local_port,
            "supports_paper_metrics": settings["server_type"] == "PAPER",
            "warn_before_start": False,
            "playit_status": "needs_configuration",
            "managed": True,
        }
        raw_registry[settings["server_id"]] = metadata
        save_registry(raw_registry)
        try:
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d"],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raw_registry.pop(settings["server_id"], None)
            save_registry(raw_registry)
            raise RuntimeError(
                f"Docker Compose could not start the server. Its files were kept at {server_directory}: {exc}"
            ) from exc
    return {**metadata, "server_id": settings["server_id"]}


def remove_server(server_id):
    """Remove a panel-created server and archive its files for recovery."""
    lock_path = docker_manager.STATE_DIRECTORY / "create-server.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = json.loads(docker_manager.CONFIG_FILE.read_text(encoding="utf-8"))
        server = registry.get(server_id)
        if not server or not server.get("managed"):
            raise ValueError("Only servers created by this panel can be removed")

        compose_file = Path(server["compose_file"]).resolve()
        server_directory = compose_file.parent
        server_root = SERVER_ROOT.resolve()
        if server_directory.parent != server_root:
            raise ValueError("The server directory is outside the managed server folder")

        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

        archive_root = server_root / ".removed"
        archive_root.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = archive_root / f"{server_id}-{timestamp}"
        shutil.move(str(server_directory), archive)
        try:
            registry.pop(server_id)
            save_registry(registry)
        except Exception:
            shutil.move(str(archive), server_directory)
            raise
        return archive
