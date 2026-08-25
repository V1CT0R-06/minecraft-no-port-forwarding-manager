import re

from mcrcon import MCRcon

from docker_manager import get_client, get_container, get_environment, get_server


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MINECRAFT_COLOR_PATTERN = re.compile(r"§[0-9A-FK-OR]", re.IGNORECASE)


def clean_response(text):
    text = ANSI_PATTERN.sub("", text)
    return MINECRAFT_COLOR_PATTERN.sub("", text).strip()


def validate_username(username):
    return bool(USERNAME_PATTERN.fullmatch(username or ""))


def get_rcon_settings(server_key):
    client = get_client()
    container = get_container(client, server_key)
    container.reload()
    if not container.attrs.get("State", {}).get("Running"):
        raise ConnectionError("Server is offline")
    env = get_environment(container)
    password = env.get("RCON_PASSWORD")
    if not password:
        raise ConnectionError("RCON password is not configured")
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    address = next((item.get("IPAddress") for item in networks.values() if item.get("IPAddress")), None)
    if not address:
        raise ConnectionError("Container network address is unavailable")
    return address, int(env.get("RCON_PORT", "25575")), password


def send_command(server_key, command):
    if not command or len(command) > 200 or "\n" in command or "\r" in command or "\x00" in command:
        raise ValueError("Enter one Minecraft command of 1–200 characters")
    host, port, password = get_rcon_settings(server_key)
    with MCRcon(host, password, port=port, timeout=3) as rcon:
        return clean_response(rcon.command(command.lstrip("/")))


def parse_players(response):
    match = re.search(r"There are (\d+) of a max of (\d+) players online:\s*(.*)", response, re.I | re.S)
    if not match:
        return {"online": None, "max": None, "names": []}
    names = [name.strip() for name in match.group(3).split(",") if name.strip()]
    return {"online": int(match.group(1)), "max": int(match.group(2)), "names": names}


def parse_whitelist(response):
    match = re.search(r"There are \d+ whitelisted player\(s\):\s*(.*)", response, re.I | re.S)
    if not match or not match.group(1).strip():
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def parse_tps(response):
    match = re.search(r"TPS from last 1m, 5m, 15m:\s*([0-9.]+)", response)
    return float(match.group(1)) if match else None


def parse_mspt(response):
    match = re.search(r"from last 5s.*?\n?\s*[^0-9]*([0-9.]+)\s*/", response, re.I | re.S)
    return float(match.group(1)) if match else None


def get_minecraft_status(server_key):
    result = {"available": False, "players": {"online": None, "max": None, "names": []}, "tps": None, "mspt": None, "whitelist": []}
    try:
        result["players"] = parse_players(send_command(server_key, "list"))
        result["whitelist"] = parse_whitelist(send_command(server_key, "whitelist list"))
        if get_server(server_key).get("supports_paper_metrics", False):
            result["tps"] = parse_tps(send_command(server_key, "tps"))
            result["mspt"] = parse_mspt(send_command(server_key, "mspt"))
        result["available"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def add_to_whitelist(server_key, username):
    if not validate_username(username):
        raise ValueError("Minecraft usernames must be 3–16 letters, numbers, or underscores")
    return send_command(server_key, f"whitelist add {username}")


def remove_from_whitelist(server_key, username):
    if not validate_username(username):
        raise ValueError("Minecraft usernames must be 3–16 letters, numbers, or underscores")
    return send_command(server_key, f"whitelist remove {username}")
