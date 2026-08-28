"""Paper version upgrades and full server-data backups.

This module deliberately uses plain files and standard-library tools. Backups
live in the panel state directory, outside Minecraft data and outside Git.
"""

import hashlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

import docker_manager
import minecraft


PAPER_API = "https://fill.papermc.io/v3/projects/paper"
USER_AGENT = "minecraft-no-port-forwarding-manager/1.0"
BACKUP_ROOT = Path(os.getenv("PANEL_BACKUP_ROOT", docker_manager.STATE_DIRECTORY / "backups"))
UPGRADEABLE_TYPES = {"PAPER"}
MODDED_TYPES = {"FABRIC", "FORGE", "NEOFORGE"}
_paper_cache = {"time": 0, "versions": None}


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def version_key(version):
    if not re.fullmatch(r"\d+(?:\.\d+){1,2}", version or ""):
        raise ValueError("Choose a normal released Minecraft version")
    return tuple(int(part) for part in version.split("."))


def paper_request(path=""):
    request = urllib.request.Request(
        PAPER_API + path,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError("Paper version information is temporarily unavailable") from exc


def get_paper_versions():
    """Return released versions that currently have a stable Paper build."""
    if _paper_cache["versions"] and time.monotonic() - _paper_cache["time"] < 600:
        return _paper_cache["versions"]
    project = paper_request()
    candidates = []
    for group in project.get("versions", {}).values():
        for version in group:
            if re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
                candidates.append(version)

    available = []
    for version in sorted(set(candidates), key=version_key, reverse=True):
        builds = paper_request(f"/versions/{version}/builds")
        stable = next((build for build in builds if build.get("channel") == "STABLE"), None)
        if stable:
            available.append({"version": version, "build": int(stable["id"])})
        # The page only needs recent upgrade choices, not the whole Paper history.
        if len(available) == 12:
            break
    _paper_cache.update(time=time.monotonic(), versions=available)
    return available


def read_server_properties(data_path):
    values = {}
    try:
        for line in (Path(data_path) / "server.properties").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    except OSError:
        pass
    return values


def find_plugins(data_path):
    plugins = []
    try:
        for jar in sorted((Path(data_path) / "plugins").glob("*.jar")):
            plugins.append({"name": jar.name, "compatibility": "Compatibility unknown"})
    except OSError:
        pass
    return plugins


def get_version_info(server_key, include_available=True):
    server = docker_manager.get_server(server_key)
    status = docker_manager.get_one_server_status(docker_manager.get_client(), server_key, server)
    server_type = str(status.get("server_type") or "UNKNOWN").upper()
    data_path = Path(server.get("data_path") or "")
    backups = list_backups(server_key)
    result = {
        **status,
        "server_type": server_type,
        "upgrade_supported": server_type in UPGRADEABLE_TYPES,
        "modded": server_type in MODDED_TYPES,
        "plugins": find_plugins(data_path),
        "last_backup": backups[0] if backups else None,
        "available_versions": [],
        "version_error": None,
    }
    if include_available and result["upgrade_supported"]:
        try:
            current_key = version_key(status.get("minecraft_version"))
            result["available_versions"] = [
                item for item in get_paper_versions() if version_key(item["version"]) > current_key
            ]
        except (ValueError, RuntimeError) as exc:
            result["version_error"] = str(exc)
    return result


def backup_directory(server_key):
    if not docker_manager.SERVER_ID_PATTERN.fullmatch(server_key):
        raise ValueError("Invalid server ID")
    return BACKUP_ROOT / server_key


@contextmanager
def server_operation(server_key):
    """Prevent two Gunicorn workers changing the same server together."""
    root = backup_directory(server_key)
    root.mkdir(parents=True, exist_ok=True)
    lock_file = (root / ".operation.lock").open("w")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another backup or version operation is already running") from exc
        yield
    finally:
        lock_file.close()


def list_backups(server_key):
    items = []
    root = backup_directory(server_key)
    try:
        manifests = sorted(root.glob("*.json"), reverse=True)
    except OSError:
        return []
    for path in manifests:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            archive = root / item["archive_name"]
            if not archive.is_file():
                continue
            item["id"] = path.stem
            item["path"] = str(archive)
            item["size"] = archive.stat().st_size
            item["size_text"] = human_size(item["size"])
            items.append(item)
        except (OSError, ValueError, KeyError):
            continue
    return items


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(archive, checksum=None):
    if not archive.is_file() or archive.stat().st_size == 0:
        raise RuntimeError("Backup archive is missing or empty")
    if checksum and sha256_file(archive) != checksum:
        raise RuntimeError("Backup checksum verification failed")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            if not names or not any(name.endswith("level.dat") for name in names):
                raise RuntimeError("Backup does not contain a Minecraft world")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError("Backup archive verification failed") from exc
    return True


def data_mount_source(container):
    mount = next(
        (item for item in container.attrs.get("Mounts", []) if item.get("Destination") == "/data"),
        None,
    )
    if not mount or mount.get("Type") != "bind" or not mount.get("RW"):
        raise RuntimeError("The server must use a writable persistent bind mount at /data")
    return Path(mount["Source"]).resolve()


def helper_command(client, container, command):
    """Run one fixed maintenance command with the registered /data mount."""
    helper = client.containers.create(
        image=container.image.id,
        entrypoint=["sh", "-c"],
        command=command,
        network_disabled=True,
        volumes_from=[container.name],
        labels={"minecraft-panel": "maintenance"},
    )
    try:
        helper.start()
        result = helper.wait(timeout=120)
        output = helper.logs().decode("utf-8", errors="replace").strip()
        if result.get("StatusCode") != 0:
            raise RuntimeError(f"Docker data maintenance failed: {output}")
        return output
    finally:
        helper.remove(force=True)


def container_data_size(client, container):
    container.reload()
    if container.attrs.get("State", {}).get("Running"):
        result = container.exec_run(["du", "-sb", "/data"])
        if result.exit_code != 0:
            raise RuntimeError("Could not measure the Minecraft data directory")
        output = result.output.decode("utf-8", errors="replace")
    else:
        output = helper_command(client, container, "du -sb /data")
    match = re.search(r"(?m)^(\d+)(?:\s|$)", output)
    if not match:
        raise RuntimeError("Could not measure the Minecraft data directory")
    return int(match.group(1))


def archive_container_data(container, destination):
    stream, _ = container.get_archive("/data")
    import gzip
    with gzip.open(destination, "wb") as output:
        for chunk in stream:
            output.write(chunk)


def clear_container_data(client, container):
    helper_command(
        client,
        container,
        "find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
    )


def _create_backup(server_key, purpose="manual", leave_stopped=False):
    server = docker_manager.get_server(server_key)
    configured_data_path = Path(server.get("data_path") or "").resolve()
    if not server.get("data_path"):
        raise RuntimeError("The persistent Minecraft data directory could not be determined")
    root = backup_directory(server_key).resolve()
    if root == configured_data_path or root.is_relative_to(configured_data_path):
        raise RuntimeError("The backup directory must be outside live Minecraft data")

    root.mkdir(parents=True, exist_ok=True)
    client = docker_manager.get_client()
    container = docker_manager.get_container(client, server_key)
    container.reload()
    mounted_data_path = data_mount_source(container)
    if mounted_data_path != configured_data_path:
        raise RuntimeError("The registered data path does not match the container's /data mount")
    data_size = container_data_size(client, container)
    if data_size <= 0:
        raise RuntimeError("The Minecraft data directory is empty")
    free = shutil.disk_usage(root).free
    required = int(data_size * 1.15) + 100 * 1024 * 1024
    if free < required:
        raise RuntimeError(
            f"Not enough backup space: {human_size(required)} recommended, {human_size(free)} available"
        )
    was_running = bool(container.attrs.get("State", {}).get("Running"))
    if was_running:
        # A failed save aborts before the server is stopped or any config changes.
        minecraft.send_command(server_key, "save-off")
        try:
            minecraft.send_command(server_key, "save-all flush")
        except Exception:
            minecraft.send_command(server_key, "save-on")
            raise
        try:
            container.stop(timeout=120)
        except Exception:
            minecraft.send_command(server_key, "save-on")
            raise

    stamp = utc_stamp()
    backup_id = f"{server_key}-{stamp}"
    partial = root / f"{backup_id}.tar.gz.partial"
    archive = root / f"{backup_id}.tar.gz"
    compose_copy = root / f"{backup_id}.compose.yaml"
    manifest_path = root / f"{backup_id}.json"
    try:
        archive_container_data(container, partial)
        partial.replace(archive)
        checksum = sha256_file(archive)
        verify_backup(archive, checksum)

        compose_path = Path(server.get("compose_file") or "")
        if not compose_path.is_file():
            raise RuntimeError("The persistent Compose configuration could not be determined")
        shutil.copy2(compose_path, compose_copy)

        status = docker_manager.get_one_server_status(client, server_key, server)
        metadata = {
            "server_id": server_key,
            "server_name": server["label"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": purpose,
            "minecraft_version": status.get("minecraft_version", "Unknown"),
            "paper_build": status.get("paper_build"),
            "archive_name": archive.name,
            "compose_name": compose_copy.name,
            "sha256": checksum,
            "data_size": data_size,
            "archive_size": archive.stat().st_size,
        }
        manifest_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    except Exception:
        partial.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        compose_copy.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        if was_running:
            container.start()
        raise

    if was_running and not leave_stopped:
        container.start()
    return {**metadata, "id": backup_id, "path": str(archive), "size_text": human_size(archive.stat().st_size)}


def create_backup(server_key, purpose="manual", leave_stopped=False):
    with server_operation(server_key):
        return _create_backup(server_key, purpose, leave_stopped)


def replace_compose_version(compose_text, service_name, version, build):
    lines = compose_text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.fullmatch(rf"  {re.escape(service_name)}:\s*\n?", line)), None)
    if start is None:
        raise ValueError("The Compose service was not found")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_.-]+:\s*(?:#.*)?$", lines[i].rstrip("\n"))), len(lines))

    found = set()
    replacements = {"VERSION": version, "PAPER_BUILD": str(build)}
    for key, value in replacements.items():
        for index in range(start + 1, end):
            match = re.match(rf'^(\s+{key}:\s*)(["\']?)[^"\'\n]+(["\']?)(\s*(?:#.*)?\n?)$', lines[index])
            if match:
                quote = match.group(2) or match.group(3) or '"'
                lines[index] = f"{match.group(1)}{quote}{value}{quote}{match.group(4)}"
                found.add(key)
                break
    if found != set(replacements):
        raise ValueError("VERSION and PAPER_BUILD must both exist in the selected Compose service")
    return "".join(lines)


def validate_upgrade(server_key, target_version):
    info = get_version_info(server_key, include_available=False)
    if not info["upgrade_supported"]:
        raise ValueError("Normal version upgrades are only available for Paper servers")
    current = info.get("minecraft_version")
    if version_key(target_version) <= version_key(current):
        raise ValueError("The target must be newer than the current Minecraft version")
    available = {item["version"]: item["build"] for item in get_paper_versions()}
    if target_version not in available:
        raise ValueError("That Minecraft version has no stable Paper build")
    if not str(info.get("java_version", "")).startswith("25") and version_key(target_version) >= (26, 1):
        raise ValueError("This target requires Java 25")
    return info, available[target_version]


def wait_for_upgrade(server_key, target_version, timeout=240):
    deadline = time.monotonic() + timeout
    last_error = "Minecraft did not become ready"
    while time.monotonic() < deadline:
        try:
            status = docker_manager.get_one_server_status(docker_manager.get_client(), server_key)
            if status.get("state") == "running" and status.get("health") in {"healthy", "not configured"}:
                response = minecraft.send_command(server_key, "version")
                if target_version in response or status.get("minecraft_version") == target_version:
                    return status, response
        except Exception as exc:
            last_error = str(exc)
        time.sleep(5)
    raise RuntimeError(last_error)


def _upgrade_paper(server_key, target_version):
    info, target_build = validate_upgrade(server_key, target_version)
    server = docker_manager.get_server(server_key)
    compose_path = Path(server["compose_file"])
    original = compose_path.read_text(encoding="utf-8")
    # Validate the persistent edit before taking the server offline.
    updated = replace_compose_version(original, server["compose_service"], target_version, target_build)
    backup = _create_backup(server_key, purpose="pre-upgrade", leave_stopped=True)

    try:
        with compose_path.open("r+", encoding="utf-8") as handle:
            handle.write(updated)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d", "--no-deps", "--force-recreate", server["compose_service"]],
            check=True, capture_output=True, text=True, timeout=300,
        )
        status, version_response = wait_for_upgrade(server_key, target_version)
        logs = docker_manager.get_recent_logs(server_key, 100)
        fatal_pattern = r"(?i)(fatal|failed to start the minecraft server|exception in server tick loop)"
        if re.search(fatal_pattern, logs):
            raise RuntimeError("A fatal startup error was found in recent logs")
        mc = minecraft.get_minecraft_status(server_key)
        return {
            "ok": True,
            "server": server["label"],
            "original_version": info["minecraft_version"],
            "target_version": target_version,
            "paper_build": target_build,
            "backup": backup,
            "container_running": status.get("state") == "running",
            "health": status.get("health"),
            "rcon": True,
            "running_version": status.get("minecraft_version"),
            "world_loaded": True,
            "tps": mc.get("tps"),
            "mspt": mc.get("mspt"),
            "version_response": version_response,
        }
    except Exception as exc:
        try:
            logs = docker_manager.get_recent_logs(server_key, 80)
        except Exception:
            logs = "Logs unavailable"
        raise UpgradeFailed(str(exc), info["minecraft_version"], target_version, backup, logs) from exc


def upgrade_paper(server_key, target_version):
    with server_operation(server_key):
        return _upgrade_paper(server_key, target_version)


class UpgradeFailed(RuntimeError):
    def __init__(self, message, original_version, target_version, backup, logs):
        super().__init__(message)
        self.original_version = original_version
        self.target_version = target_version
        self.backup = backup
        self.logs = logs


def _restore_backup(server_key, backup_id, confirmation):
    expected = f"RESTORE {server_key} {backup_id}"
    if confirmation != expected:
        raise ValueError(f"Type {expected} exactly")
    backup = next((item for item in list_backups(server_key) if item["id"] == backup_id), None)
    if not backup:
        raise ValueError("Backup not found")
    archive = Path(backup["path"])
    verify_backup(archive, backup.get("sha256"))

    server = docker_manager.get_server(server_key)
    compose_path = Path(server["compose_file"]).resolve()
    compose_backup = backup_directory(server_key) / backup["compose_name"]
    if not compose_backup.is_file():
        raise RuntimeError("The backup's original Compose file is missing")

    container = docker_manager.get_container(docker_manager.get_client(), server_key)
    container.reload()
    if container.attrs.get("State", {}).get("Running"):
        container.stop(timeout=120)

    # Preserve the current state before replacing it. This archive is never
    # used as the requested rollback backup and does not overwrite that backup.
    failed_state = _create_backup(server_key, purpose="pre-restore", leave_stopped=True)
    clear_container_data(docker_manager.get_client(), container)
    import gzip
    with gzip.open(archive, "rb") as source:
        if not container.put_archive("/", source):
            raise RuntimeError("Docker could not restore the backup archive")
    shutil.copy2(compose_backup, compose_path)
    subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "up", "-d", "--no-deps", "--force-recreate", server["compose_service"]],
        check=True, capture_output=True, text=True, timeout=300,
    )
    status, response = wait_for_upgrade(server_key, backup["minecraft_version"])
    return {"restored": backup, "preserved_failed_state": failed_state, "status": status, "version_response": response}


def restore_backup(server_key, backup_id, confirmation):
    with server_operation(server_key):
        return _restore_backup(server_key, backup_id, confirmation)
