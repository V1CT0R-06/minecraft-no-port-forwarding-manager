import shutil
import socket
import subprocess
import time

import psutil


def bytes_to_gib(value):
    """Convert bytes to GiB and keep one decimal place for the dashboard."""
    return round(value / (1024 ** 3), 1)


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_playit_status():
    """systemctl status checks are read-only and do not require sudo."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "playit.service"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    if result.stdout.strip() == "active":
        return "running"
    if result.stdout.strip() in {"inactive", "failed", "deactivating"}:
        return "stopped"
    return "unknown"


def get_host_status():
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = shutil.disk_usage("/")
    return {
        "hostname": socket.gethostname(),
        "uptime": format_duration(time.time() - psutil.boot_time()),
        "cpu_percent": round(psutil.cpu_percent(interval=0.15), 1),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "ram_total_bytes": memory.total,
        "ram_available_bytes": memory.available,
        "ram_used_gib": bytes_to_gib(memory.used),
        "ram_available_gib": bytes_to_gib(memory.available),
        "ram_total_gib": bytes_to_gib(memory.total),
        "ram_percent": round(memory.percent, 1),
        "swap_used_gib": bytes_to_gib(swap.used),
        "swap_total_gib": bytes_to_gib(swap.total),
        "swap_percent": round(swap.percent, 1),
        "disk_used_gib": bytes_to_gib(disk.used),
        "disk_total_gib": bytes_to_gib(disk.total),
        "disk_percent": round(disk.used / disk.total * 100, 1),
        "playit": get_playit_status(),
    }


def recommended_host_reserve(total_ram_bytes):
    """Suggest host headroom: 15% of RAM, with a practical 1 GiB minimum.

    This is a warning guideline, not a hard limit. It scales automatically when
    physical RAM changes and leaves room for Ubuntu, Docker, and other services.
    """
    one_gib = 1024 ** 3
    return max(one_gib, int(total_ram_bytes * 0.15))


def get_resource_summary(host, servers):
    """Combine live host memory with the configured heaps of known servers."""
    total = host["ram_total_bytes"]
    reserve = recommended_host_reserve(total)
    configured = sum(server.get("configured_memory_bytes") or 0 for server in servers.values())
    running = sum(
        server.get("configured_memory_bytes") or 0
        for server in servers.values()
        if server.get("state") == "running"
    )
    headroom = max(0, total - reserve - configured)
    return {
        "recommended_reserve_bytes": reserve,
        "recommended_reserve_gib": bytes_to_gib(reserve),
        "minecraft_configured_bytes": configured,
        "minecraft_configured_gib": bytes_to_gib(configured),
        "minecraft_running_bytes": running,
        "minecraft_running_gib": bytes_to_gib(running),
        "planned_heap_headroom_bytes": headroom,
        "planned_heap_headroom_gib": bytes_to_gib(headroom),
        "configured_exceeds_recommendation": configured + reserve > total,
    }
