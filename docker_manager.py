import re
import os
import json
import shutil
import subprocess
from pathlib import Path
from time import strftime
from datetime import datetime, timezone

import docker
from docker.errors import APIError, DockerException, NotFound

from system_info import bytes_to_gib, format_duration


CONFIG_FILE = Path(os.getenv("PANEL_SERVERS_FILE", Path(__file__).with_name("config") / "servers.json"))
STATE_DIRECTORY = Path(os.getenv("STATE_DIRECTORY", "/var/lib/minecraft-panel"))
SERVER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def load_servers(config_file=None):
    """Load the human-readable registry used as the Docker safety allowlist."""
    path = Path(config_file or CONFIG_FILE)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("The server registry must be a JSON object")

    servers = {}
    container_names = set()
    for server_id, settings in data.items():
        if not SERVER_ID_PATTERN.fullmatch(server_id) or not isinstance(settings, dict):
            raise ValueError(f"Invalid server registry entry: {server_id}")
        container = settings.get("container", "")
        compose_service = settings.get("compose_service", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValueError(f"Invalid container name for {server_id}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", compose_service):
            raise ValueError(f"Invalid Compose service for {server_id}")
        if container in container_names:
            raise ValueError(f"Container {container!r} is registered more than once")
        container_names.add(container)
        servers[server_id] = {
            **settings,
            "name": container,
            "label": settings.get("display_name", server_id.title()),
        }
    return servers


def get_server(server_key):
    """Return one registered server or reject the browser-supplied ID."""
    servers = load_servers()
    if server_key not in servers:
        raise ValueError("Unknown server")
    return servers[server_key]


def get_client():
    return docker.from_env(timeout=4)


def get_container(client, server_key):
    server = get_server(server_key)
    return client.containers.get(server["container"])


def get_environment(container):
    environment = {}
    for item in container.attrs.get("Config", {}).get("Env", []):
        key, _, value = item.partition("=")
        environment[key] = value
    return environment


def get_configured_memory(container):
    env = get_environment(container)
    # MAX_MEMORY is the clearest maximum when present. MEMORY is the common
    # itzg image setting; INIT_MEMORY alone is only the starting heap.
    for key in ("MAX_MEMORY", "MEMORY", "INIT_MEMORY"):
        if env.get(key):
            return env[key]
    return "Not configured"


def memory_to_gib(value):
    """Parse common Java memory strings such as 3G, 4096M, or 2.5G."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GMK])(?:I?B)?\s*", value.upper())
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    return number * {"G": 1, "M": 1 / 1024, "K": 1 / (1024 ** 2)}[unit]


def parse_memory(value):
    """Normalize an itzg/Java memory value to bytes for comparisons."""
    gib = memory_to_gib(value)
    return None if gib is None else int(gib * 1024 ** 3)


def format_memory(bytes_value):
    """Format bytes as an easy-to-read GiB value."""
    if bytes_value is None:
        return "N/A"
    gib = bytes_value / (1024 ** 3)
    return f"{gib:.0f} GiB" if gib.is_integer() else f"{gib:.1f} GiB"


def normalize_memory(value):
    """Validate memory input and return a compact value accepted by itzg."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GM])(?:I?B)?\s*", value.upper())
    if not match:
        raise ValueError("Use a memory value such as 2G, 3G, or 4096M")
    number = float(match.group(1))
    if number <= 0:
        raise ValueError("Memory must be greater than zero")
    number_text = str(int(number)) if number.is_integer() else str(number)
    return f"{number_text}{match.group(2)}"


def replace_compose_memory(compose_text, service_name, new_memory):
    """Replace the heap variable only inside one Compose service block."""
    lines = compose_text.splitlines(keepends=True)
    service_start = next(
        (index for index, line in enumerate(lines) if re.fullmatch(rf"  {re.escape(service_name)}:\s*\n?", line)),
        None,
    )
    if service_start is None:
        raise ValueError(f"Compose service {service_name!r} was not found")
    service_end = next(
        (index for index in range(service_start + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_.-]+:\s*(?:#.*)?$", lines[index].rstrip("\n"))),
        len(lines),
    )
    preferred_keys = ("MAX_MEMORY", "MEMORY", "INIT_MEMORY")
    for key in preferred_keys:
        for index in range(service_start + 1, service_end):
            match = re.match(rf'^(\s+{key}:\s*)(["\']?)[^"\'\n]+(["\']?)(\s*(?:#.*)?\n?)$', lines[index])
            if match:
                quote = match.group(2) or match.group(3)
                lines[index] = f"{match.group(1)}{quote}{new_memory}{quote}{match.group(4)}"
                return "".join(lines), key
    raise ValueError(f"No Java memory setting was found for Compose service {service_name!r}")


def change_server_memory(server_key, new_memory, compose_file=None, state_directory=None, run_command=True):
    """Persist a heap change and recreate only the selected Compose service."""
    server = get_server(server_key)
    normalized = normalize_memory(new_memory)
    compose_path = Path(compose_file or server["compose_file"])
    backup_root = Path(state_directory or STATE_DIRECTORY) / "compose-backups"
    original = compose_path.read_text(encoding="utf-8")
    updated, variable = replace_compose_memory(original, server["compose_service"], normalized)
    if updated == original:
        return {"memory": normalized, "variable": variable, "changed": False, "backup": None}

    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"compose-{strftime('%Y%m%d-%H%M%S')}.yaml"
    shutil.copy2(compose_path, backup_path)
    with compose_path.open("r+", encoding="utf-8") as compose_handle:
        compose_handle.write(updated)
        compose_handle.truncate()
        compose_handle.flush()
        os.fsync(compose_handle.fileno())

    if run_command:
        client = get_client()
        was_running = bool(get_container(client, server_key).attrs.get("State", {}).get("Running"))
        action = "up" if was_running else "create"
        command = ["docker", "compose", "-f", str(compose_path), action]
        if was_running:
            command.extend(["-d", "--no-deps"])
        command.extend(["--force-recreate", server["compose_service"]])
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        except (subprocess.SubprocessError, OSError) as exc:
            with compose_path.open("r+", encoding="utf-8") as compose_handle:
                compose_handle.write(original)
                compose_handle.truncate()
            raise RuntimeError(f"Docker Compose failed; the Compose file was restored: {exc}") from exc

    return {"memory": normalized, "variable": variable, "changed": True, "backup": str(backup_path)}


def get_cpu_limit(container):
    """Read a basic Docker CPU limit without changing container resources."""
    host_config = container.attrs.get("HostConfig", {})
    nano_cpus = host_config.get("NanoCpus") or 0
    if nano_cpus:
        return round(nano_cpus / 1_000_000_000, 2)
    quota = host_config.get("CpuQuota") or 0
    period = host_config.get("CpuPeriod") or 0
    if quota > 0 and period > 0:
        return round(quota / period, 2)
    return None


def container_uptime(container):
    started = container.attrs.get("State", {}).get("StartedAt")
    if not started or started.startswith("0001-"):
        return "N/A"
    started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return format_duration((datetime.now(timezone.utc) - started_at).total_seconds())


def get_one_server_status(client, server_key, details=None):
    details = details or get_server(server_key)
    try:
        container = get_container(client, server_key)
        container.reload()
    except NotFound:
        return {"key": server_key, "label": details["label"], "state": "missing", "error": "Container not found"}

    state = container.attrs.get("State", {})
    running = state.get("Running", False)
    health = (state.get("Health") or {}).get("Status", "not configured")
    configured_memory = get_configured_memory(container)
    result = {
        "key": server_key,
        "label": details["label"],
        "public_hostname": details.get("public_hostname"),
        "server_type": details.get("server_type", "Unknown"),
        "data_path": details.get("data_path"),
        "local_port": details.get("local_port"),
        "managed": bool(details.get("managed")),
        "playit_status": details.get("playit_status", "needs_configuration"),
        "playit_endpoint": details.get("playit_endpoint"),
        "warn_before_start": bool(details.get("warn_before_start")),
        "state": "running" if running else "stopped",
        "health": health,
        "configured_memory": configured_memory,
        "configured_memory_gib": memory_to_gib(configured_memory),
        "configured_memory_bytes": parse_memory(configured_memory),
        "cpu_limit": get_cpu_limit(container),
        "whitelist_enabled": get_environment(container).get("ENABLE_WHITELIST", "").lower() == "true",
        "current_memory_gib": None,
        "cpu_percent": None,
        "uptime": container_uptime(container) if running else "N/A",
    }

    if running:
        stats = container.stats(stream=False)
        memory_usage = stats.get("memory_stats", {}).get("usage", 0)
        # Cache is reclaimable memory and should not be presented as active use.
        cache = stats.get("memory_stats", {}).get("stats", {}).get("inactive_file", 0)
        result["current_memory_gib"] = bytes_to_gib(max(0, memory_usage - cache))

        cpu_delta = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        system_delta = stats.get("cpu_stats", {}).get("system_cpu_usage", 0) - stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
        cpu_count = stats.get("cpu_stats", {}).get("online_cpus") or 1
        if system_delta > 0 and cpu_delta >= 0:
            result["cpu_percent"] = round(cpu_delta / system_delta * cpu_count * 100, 1)
    return result


def get_servers_status():
    servers = load_servers()
    try:
        client = get_client()
        return {key: get_one_server_status(client, key, details) for key, details in servers.items()}, None
    except (DockerException, APIError) as exc:
        offline = {
            key: {"key": key, "label": item["label"], "state": "unknown", "error": "Docker unavailable"}
            for key, item in servers.items()
        }
        return offline, str(exc)


def control_server(server_key, action):
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported action")
    client = get_client()
    container = get_container(client, server_key)
    if action == "start":
        container.start()
    elif action == "stop":
        container.stop(timeout=60)
    else:
        container.restart(timeout=60)


def get_recent_logs(server_key, lines=80):
    client = get_client()
    container = get_container(client, server_key)
    return container.logs(tail=min(lines, 100), timestamps=True).decode("utf-8", errors="replace")
