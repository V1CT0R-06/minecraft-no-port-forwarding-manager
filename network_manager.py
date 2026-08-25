"""Read-only DNS inspection and human-readable record guidance."""

import ipaddress
import os
import subprocess


DNS_ZONE = os.getenv("MINECRAFT_DNS_ZONE", "example.com").lower().rstrip(".")


def dig_short(name, record_type):
    """Query DNS without a shell. An empty list means absent or unavailable."""
    try:
        result = subprocess.run(
            ["dig", "+short", name, record_type],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip().rstrip(".") for line in result.stdout.splitlines() if line.strip()]


def parse_srv(lines):
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) != 4 or not all(item.isdigit() for item in parts[:3]):
        return None
    return {
        "priority": int(parts[0]),
        "weight": int(parts[1]),
        "port": int(parts[2]),
        "target": parts[3].rstrip("."),
    }


def inspect_hostname(hostname):
    return {
        "cname": next(iter(dig_short(hostname, "CNAME")), None),
        "srv": parse_srv(dig_short(f"_minecraft._tcp.{hostname}", "SRV")),
    }


def relative_record_name(hostname):
    suffix = f".{DNS_ZONE}"
    if hostname == DNS_ZONE:
        return "@"
    if not hostname.endswith(suffix):
        raise ValueError(f"Public hostname must be inside {DNS_ZONE}")
    return hostname[:-len(suffix)]


def split_endpoint(endpoint):
    if not endpoint:
        return None, None
    host, separator, port_text = endpoint.rpartition(":")
    if separator and port_text.isdigit():
        return host, int(port_text)
    return endpoint, None


def build_dns_guidance(server):
    hostname = (server.get("public_hostname") or "").lower().rstrip(".")
    record_name = relative_record_name(hostname)
    endpoint_host, endpoint_port = split_endpoint(server.get("playit_endpoint"))
    actual = inspect_hostname(hostname)
    nameservers = dig_short(DNS_ZONE, "NS")
    cloudflare = any(name.endswith("ns.cloudflare.com") for name in nameservers)
    records = []
    missing_reason = None

    if endpoint_host:
        try:
            ipaddress.ip_address(endpoint_host)
            records.append({"type": "A", "name": record_name, "target": endpoint_host, "proxy": "DNS only"})
            if endpoint_port:
                records.append({
                    "type": "SRV", "name": f"_minecraft._tcp.{record_name}",
                    "priority": 1, "weight": 1, "port": endpoint_port, "target": hostname,
                })
        except ValueError:
            records.append({"type": "CNAME", "name": record_name, "target": endpoint_host, "proxy": "DNS only"})
            source_srv = parse_srv(dig_short(f"_minecraft._tcp.{endpoint_host}", "SRV"))
            srv_port = endpoint_port or (source_srv and source_srv["port"])
            srv_target = endpoint_host if endpoint_port else (source_srv and source_srv["target"])
            if srv_port and srv_target:
                records.append({
                    "type": "SRV", "name": f"_minecraft._tcp.{record_name}",
                    "priority": 1, "weight": 1, "port": srv_port, "target": srv_target,
                })
            elif not endpoint_port:
                missing_reason = "The saved Playit hostname has no discoverable Minecraft SRV record. Copy its host and port from Playit."
    else:
        missing_reason = "Save the Playit public endpoint before generating DNS records."

    expected_cname = next((record["target"] for record in records if record["type"] == "CNAME"), None)
    expected_a = next((record["target"] for record in records if record["type"] == "A"), None)
    expected_srv = next((record for record in records if record["type"] == "SRV"), None)
    cname_matches = expected_cname is None or actual["cname"] == expected_cname
    if expected_a:
        cname_matches = expected_a in dig_short(hostname, "A")
    srv_matches = expected_srv is None or (
        actual["srv"] is not None
        and actual["srv"]["port"] == expected_srv["port"]
        and actual["srv"]["target"] == expected_srv["target"]
    )
    return {
        "zone": DNS_ZONE,
        "cloudflare": cloudflare,
        "nameservers": nameservers,
        "actual": actual,
        "records": records,
        "configured": bool(records and cname_matches and srv_matches),
        "missing_reason": missing_reason,
    }
