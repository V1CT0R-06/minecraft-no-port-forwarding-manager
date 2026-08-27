import hmac
import hashlib
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for


def load_env_file(path):
    """Load simple KEY=value settings without another dependency."""
    if not os.access(path, os.R_OK):
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(Path(__file__).with_name(".env"))

# These modules read a few machine-specific paths from the environment, so the
# ignored local .env file must be loaded before importing them in development.
import docker_manager
import minecraft
import network_manager
import server_manager
import system_info


def create_app(test_config=None):
    username = os.getenv("PANEL_USERNAME", "admin")
    password = os.getenv("PANEL_PASSWORD", "")
    # Changing the login also invalidates old sessions. No extra secret setting
    # is needed during setup.
    secret_key = hashlib.sha256(
        f"minecraft-panel:{username}:{password}".encode()
    ).hexdigest()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        USERNAME=username,
        PASSWORD=password,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("logged_in"):
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "Authentication required"}), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(24)
        return session["csrf_token"]

    def check_csrf():
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        return bool(expected and hmac.compare_digest(supplied, expected))

    def csrf_required(view):
        """Protect API routes that change data."""
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not check_csrf():
                return api_error("Invalid CSRF token", 403)
            return view(*args, **kwargs)
        return wrapped

    def json_body():
        return request.get_json(silent=True) or {}

    def api_error(message, status=400):
        return jsonify({"ok": False, "error": message}), status

    @app.context_processor
    def template_values():
        return {
            "csrf_token": csrf_token,
            "panel_title": "No-Port Minecraft Manager",
            "dns_zone": network_manager.DNS_ZONE,
        }

    @app.get("/login")
    def login():
        if session.get("logged_in"):
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        if not check_csrf():
            return render_template("login.html", error="Your login form expired. Please try again."), 400
        expected_password = app.config.get("PASSWORD", "")
        expected_username = app.config.get("USERNAME", "")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        username_matches = hmac.compare_digest(username, expected_username)
        password_matches = bool(expected_password) and hmac.compare_digest(password, expected_password)
        if username_matches and password_matches:
            session.clear()
            session["logged_in"] = True
            csrf_token()
            return redirect(url_for("index"))
        return render_template("login.html", error="Incorrect password"), 401

    @app.post("/logout")
    @login_required
    def logout():
        if not check_csrf():
            return "Invalid CSRF token", 400
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        return render_template("index.html")

    @app.get("/speed-test")
    @login_required
    def speed_test_page():
        return render_template("speed_test.html")

    @app.post("/api/speed-test")
    @login_required
    @csrf_required
    def internet_speed_test():
        try:
            return jsonify({"ok": True, **system_info.run_internet_speed_test()})
        except RuntimeError as exc:
            return api_error(str(exc), 503)

    @app.get("/tutorial")
    @login_required
    def tutorial():
        return render_template(
            "tutorial.html",
            project_path=Path(__file__).parent,
            dns_zone=network_manager.DNS_ZONE,
            server_root=server_manager.SERVER_ROOT,
            shared_env_file=server_manager.SHARED_ENV_FILE,
        )

    @app.get("/servers/new")
    @login_required
    def new_server():
        host = system_info.get_host_status()
        servers, _ = docker_manager.get_servers_status()
        resources = system_info.get_resource_summary(host, servers)
        try:
            local_port = server_manager.allocate_local_port()
            port_error = None
        except Exception as exc:
            local_port = None
            port_error = str(exc)
        return render_template(
            "create_server.html",
            host=host,
            resources=resources,
            local_port=local_port,
            port_error=port_error,
            server_types=sorted(server_manager.ALLOWED_TYPES),
        )

    @app.get("/removed")
    @login_required
    def removed_servers():
        return render_template(
            "removed.html",
            archives=server_manager.list_removed_servers(),
        )

    @app.post("/removed/<archive_name>/delete")
    @login_required
    def delete_removed_server(archive_name):
        if not check_csrf():
            return "Invalid CSRF token", 403
        if request.form.get("confirmation", "").strip() != archive_name:
            return render_template(
                "removed.html",
                archives=server_manager.list_removed_servers(),
                error=f"Type {archive_name} exactly to delete it",
            ), 400
        try:
            server_manager.delete_removed_server(archive_name)
        except (ValueError, OSError) as exc:
            return render_template(
                "removed.html",
                archives=server_manager.list_removed_servers(),
                error=str(exc),
            ), 400
        return redirect(url_for("removed_servers", deleted=archive_name))

    @app.route("/servers/<server_key>/network", methods=["GET", "POST"])
    @login_required
    def server_network(server_key):
        try:
            server = docker_manager.get_server(server_key)
        except ValueError:
            return "Unknown server", 404
        error = None
        saved = False
        if request.method == "POST":
            if not check_csrf():
                return "Invalid CSRF token", 403
            try:
                server_manager.save_playit_endpoint(server_key, request.form.get("playit_endpoint"))
                server = docker_manager.get_server(server_key)
                saved = True
            except ValueError as exc:
                error = str(exc)
        local_port = server.get("local_port")
        local_endpoint = f"127.0.0.1:{local_port}" if local_port else None
        try:
            dns = network_manager.build_dns_guidance(server)
        except ValueError as exc:
            dns = {"records": [], "configured": False, "missing_reason": str(exc), "actual": {}, "cloudflare": False}
        return render_template(
            "server_network.html",
            server_key=server_key,
            server=server,
            local_endpoint=local_endpoint,
            local_status=server_manager.local_port_status(server),
            playit_status=system_info.get_playit_status(),
            error=error,
            saved=saved,
            dns=dns,
        )

    @app.post("/api/servers/review")
    @login_required
    @csrf_required
    def review_server():
        try:
            settings = server_manager.validate_server_settings(json_body())
            servers, docker_error = docker_manager.get_servers_status()
            if docker_error:
                return api_error("Docker information is unavailable", 503)
            if settings["server_id"] in servers:
                return api_error("That server name is already registered")
            host = system_info.get_host_status()
            resources = system_info.get_resource_summary(host, servers)
            requested_bytes = docker_manager.parse_memory(settings["memory"])
            projected = resources["minecraft_configured_bytes"] + requested_bytes
            local_port = server_manager.allocate_local_port()
            server_directory = server_manager.SERVER_ROOT / settings["server_id"]
            return jsonify({
                "ok": True,
                "settings": settings,
                "host_total_gib": host["ram_total_gib"],
                "host_available_gib": host["ram_available_gib"],
                "minecraft_configured_gib": resources["minecraft_configured_gib"],
                "recommended_reserve_gib": resources["recommended_reserve_gib"],
                "suggested_maximum_gib": resources["planned_heap_headroom_gib"],
                "projected_minecraft_gib": system_info.bytes_to_gib(projected),
                "memory_warning": projected + resources["recommended_reserve_bytes"] > host["ram_total_bytes"],
                "local_port": local_port,
                "local_endpoint": f"127.0.0.1:{local_port}",
                "server_directory": str(server_directory),
                "data_directory": str(server_directory / "data"),
                "compose_file": str(server_directory / "compose.yaml"),
                "playit_status": "Needs configuration",
            })
        except ValueError as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.post("/api/servers/create")
    @login_required
    @csrf_required
    def create_server():
        body = json_body()
        if body.get("confirm_create") is not True:
            return api_error("Review and confirm the server before creating it")
        try:
            result = server_manager.create_server(body.get("settings") or {}, body.get("local_port"))
            return jsonify({
                "ok": True,
                "message": f"{result['display_name']} was created and is starting",
                "server_id": result["server_id"],
            })
        except (TypeError, ValueError) as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.get("/api/status")
    @login_required
    def status():
        host = system_info.get_host_status()
        servers, docker_error = docker_manager.get_servers_status()
        for key, server in servers.items():
            if server.get("state") == "running":
                server["minecraft"] = minecraft.get_minecraft_status(key)
        resources = system_info.get_resource_summary(host, servers)
        return jsonify({
            "ok": True,
            "host": host,
            "resources": resources,
            "servers": servers,
            "docker_error": docker_error,
        })

    @app.post("/api/servers/<server_key>/start", defaults={"action": "start"})
    @app.post("/api/servers/<server_key>/stop", defaults={"action": "stop"})
    @app.post("/api/servers/<server_key>/restart", defaults={"action": "restart"})
    @login_required
    @csrf_required
    def server_action(server_key, action):
        try:
            server = docker_manager.get_server(server_key)
            if action == "start" and server.get("warn_before_start") and json_body().get("confirm_memory_warning") is not True:
                host = system_info.get_host_status()
                servers, _ = docker_manager.get_servers_status()
                resources = system_info.get_resource_summary(host, servers)
                return jsonify({
                    "ok": False,
                    "requires_confirmation": True,
                    "server": server["label"],
                    "configured_memory": servers[server_key].get("configured_memory", "Unknown"),
                    "available_memory_gib": host["ram_available_gib"],
                    "recommended_reserve_gib": resources["recommended_reserve_gib"],
                    "error": f"Starting {server['label']} may create significant memory pressure",
                }), 409
            docker_manager.control_server(server_key, action)
            return jsonify({"ok": True, "message": f"{server['label']} {action} requested"})
        except ValueError as exc:
            return api_error(str(exc), 404)
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.post("/api/servers/<server_key>/memory")
    @login_required
    @csrf_required
    def server_memory(server_key):
        body = json_body()
        try:
            new_memory = docker_manager.normalize_memory(body.get("memory", ""))
            new_bytes = docker_manager.parse_memory(new_memory)
            host = system_info.get_host_status()
            servers, docker_error = docker_manager.get_servers_status()
            if docker_error or server_key not in servers:
                return api_error("Docker information is unavailable", 503)
            server = servers[server_key]
            current_bytes = server.get("configured_memory_bytes") or 0
            resources = system_info.get_resource_summary(host, servers)
            projected_total = resources["minecraft_configured_bytes"] - current_bytes + new_bytes
            reserve = resources["recommended_reserve_bytes"]
            warning = projected_total + reserve > host["ram_total_bytes"]

            if body.get("confirm_recreate") is not True:
                return jsonify({
                    "ok": False,
                    "requires_confirmation": True,
                    "server": server.get("label", server_key.title()),
                    "current_memory": server.get("configured_memory", "Unknown"),
                    "new_memory": new_memory,
                    "host_total_gib": host["ram_total_gib"],
                    "host_available_gib": host["ram_available_gib"],
                    "recommended_reserve_gib": resources["recommended_reserve_gib"],
                    "projected_minecraft_gib": system_info.bytes_to_gib(projected_total),
                    "memory_warning": warning,
                    "error": "This change requires the selected container to be recreated",
                }), 409

            result = docker_manager.change_server_memory(server_key, new_memory)
            message = (
                f"{server.get('label', server_key.title())} already uses {new_memory}"
                if not result["changed"]
                else f"{server.get('label', server_key.title())} memory changed to {new_memory}"
            )
            return jsonify({"ok": True, "message": message})
        except ValueError as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.post("/api/servers/<server_key>/remove")
    @login_required
    @csrf_required
    def server_remove(server_key):
        if json_body().get("confirmation") != server_key:
            return api_error("Type the exact server ID to confirm removal")
        try:
            archive = server_manager.remove_server(server_key)
            message = (
                f"Server removed. Its files were archived at {archive}"
                if archive else
                "Server removed from Docker and the panel. Its world and Compose files were kept."
            )
            return jsonify({
                "ok": True,
                "message": message,
            })
        except ValueError as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.post("/api/servers/<server_key>/whitelist/add", defaults={"action": "add"})
    @app.post("/api/servers/<server_key>/whitelist/remove", defaults={"action": "remove"})
    @login_required
    @csrf_required
    def server_whitelist(server_key, action):
        username = json_body().get("username", "")
        try:
            command = minecraft.add_to_whitelist if action == "add" else minecraft.remove_from_whitelist
            message = command(server_key, username)
            return jsonify({"ok": True, "message": message})
        except ValueError as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.post("/api/servers/<server_key>/console")
    @login_required
    @csrf_required
    def server_console(server_key):
        command = json_body().get("command", "")
        try:
            return jsonify({"ok": True, "result": minecraft.send_command(server_key, command)})
        except ValueError as exc:
            return api_error(str(exc))
        except Exception as exc:
            return api_error(str(exc), 500)

    @app.get("/api/servers/<server_key>/logs")
    @login_required
    def server_logs(server_key):
        try:
            return jsonify({"ok": True, "logs": docker_manager.get_recent_logs(server_key)})
        except Exception as exc:
            return api_error(str(exc), 500)

    return app


app = create_app()


if __name__ == "__main__":
    if not app.config.get("PASSWORD"):
        raise SystemExit("Set PANEL_USERNAME and PANEL_PASSWORD in .env first. See README.md.")
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False,
    )
