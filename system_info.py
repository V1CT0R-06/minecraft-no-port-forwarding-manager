import shutil
import socket
import subprocess
import time

import psutil


_network_samples = {}


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


def get_default_interface():
    """Return the interface used by the host default IPv4 route."""
    try:
        with open("/proc/net/route", encoding="utf-8") as routes:
            for line in routes:
                fields = line.split()
                if len(fields) > 1 and fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass
    return None


def get_network_speed():
    """Measure current traffic without running an external speed test."""
    interface = get_default_interface()
    counters = psutil.net_io_counters(pernic=True)
    current = counters.get(interface) if interface else None
    if current is None:
        interface = "all"
        current = psutil.net_io_counters()

    now = time.monotonic()
    previous = _network_samples.get(interface)
    _network_samples[interface] = (now, current.bytes_recv, current.bytes_sent)
    if not previous or now <= previous[0]:
        return {"interface": interface, "download_mbps": 0.0, "upload_mbps": 0.0}

    seconds = now - previous[0]
    download = max(0, current.bytes_recv - previous[1]) * 8 / seconds / 1_000_000
    upload = max(0, current.bytes_sent - previous[2]) * 8 / seconds / 1_000_000
    return {
        "interface": interface,
        "download_mbps": round(download, 2),
        "upload_mbps": round(upload, 2),
    }


def get_host_status():
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = shutil.disk_usage("/")
    network = get_network_speed()
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
        "network_interface": network["interface"],
        "download_mbps": network["download_mbps"],
        "upload_mbps": network["upload_mbps"],
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
