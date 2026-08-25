from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import create_app


def make_client():
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-only-secret",
        "USERNAME": "homelab",
        "PASSWORD_HASH": generate_password_hash("correct-password"),
    })
    return app.test_client()


def get_csrf(client):
    with client.session_transaction() as session:
        return session["csrf_token"]


def login(client):
    client.get("/login")
    return client.post("/login", data={"username": "homelab", "password": "correct-password", "csrf_token": get_csrf(client)})


def test_dashboard_requires_login():
    client = make_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/status").status_code == 401


def test_tutorial_requires_login_and_renders_full_setup_guide():
    client = make_client()
    assert client.get("/tutorial").status_code == 302
    login(client)
    response = client.get("/tutorial")
    assert response.status_code == 200
    assert b"Build your own Minecraft homelab" in response.data
    assert b"Install Docker" in response.data
    assert b"Configure Playit" in response.data
    assert b"Back up worlds" in response.data


def test_login_accepts_hash_and_rejects_wrong_password():
    client = make_client()
    client.get("/login")
    assert client.post("/login", data={"username": "homelab", "password": "wrong", "csrf_token": get_csrf(client)}).status_code == 401
    assert client.post("/login", data={"username": "wrong", "password": "correct-password", "csrf_token": get_csrf(client)}).status_code == 401
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200


def test_control_route_needs_csrf_and_uses_registered_server_key():
    client = make_client()
    login(client)
    assert client.post("/api/servers/paper/start", json={}).status_code == 403
    with patch("docker_manager.control_server") as control:
        response = client.post("/api/servers/paper/start", json={}, headers={"X-CSRF-Token": get_csrf(client)})
        assert response.status_code == 200
        control.assert_called_once_with("paper", "start")
    assert client.post("/api/servers/unknown/start", json={}, headers={"X-CSRF-Token": get_csrf(client)}).status_code == 404


def test_invalid_username_never_reaches_rcon():
    client = make_client()
    login(client)
    with patch("minecraft.send_command") as command:
        response = client.post(
            "/api/servers/paper/whitelist/add",
            json={"username": "bad; stop"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert response.status_code == 400
        command.assert_not_called()


def test_pixelmon_start_requires_server_side_memory_confirmation():
    client = make_client()
    login(client)
    gib = 1024 ** 3
    servers = {"pixelmon": {"configured_memory": "3G", "configured_memory_bytes": 3 * gib}}
    host = {"ram_available_gib": 2.5, "ram_total_bytes": 8 * gib}
    with patch("docker_manager.get_servers_status", return_value=(servers, None)), \
         patch("system_info.get_host_status", return_value=host), \
         patch("docker_manager.control_server") as control:
        response = client.post("/api/servers/pixelmon/start", json={}, headers={"X-CSRF-Token": get_csrf(client)})
        assert response.status_code == 409
        assert response.json["configured_memory"] == "3G"
        control.assert_not_called()

        response = client.post(
            "/api/servers/pixelmon/start",
            json={"confirm_memory_warning": True},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert response.status_code == 200
        control.assert_called_once_with("pixelmon", "start")


def test_memory_change_is_reviewed_before_compose_is_modified():
    client = make_client()
    login(client)
    gib = 1024 ** 3
    host = {
        "ram_available_gib": 7.0,
        "ram_total_gib": 16.0,
        "ram_total_bytes": 16 * gib,
    }
    servers = {
        "paper": {"label": "Paper", "state": "running", "configured_memory": "4G", "configured_memory_bytes": 4 * gib},
        "pixelmon": {"label": "Pixelmon", "state": "stopped", "configured_memory": "3G", "configured_memory_bytes": 3 * gib},
    }
    with patch("docker_manager.get_servers_status", return_value=(servers, None)), \
         patch("system_info.get_host_status", return_value=host), \
         patch("docker_manager.change_server_memory") as change:
        response = client.post(
            "/api/servers/paper/memory",
            json={"memory": "6G"},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert response.status_code == 409
        assert response.json["new_memory"] == "6G"
        change.assert_not_called()

        change.return_value = {"changed": True}
        response = client.post(
            "/api/servers/paper/memory",
            json={"memory": "6G", "confirm_recreate": True},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert response.status_code == 200
        change.assert_called_once_with("paper", "6G")


def test_create_server_requires_review_and_does_not_create_during_review():
    client = make_client()
    login(client)
    gib = 1024 ** 3
    host = {"ram_total_gib": 16.0, "ram_available_gib": 8.0, "ram_total_bytes": 16 * gib}
    servers = {"paper": {"state": "running", "configured_memory_bytes": 4 * gib}}
    settings = {
        "server_id": "creative", "display_name": "Creative", "server_type": "PAPER",
        "version": "LATEST", "memory": "2G", "max_players": 20,
        "motd": "Creative server", "whitelist": True,
        "public_hostname": "creative.example.com",
    }
    with patch("system_info.get_host_status", return_value=host), \
         patch("docker_manager.get_servers_status", return_value=(servers, None)), \
         patch("server_manager.allocate_local_port", return_value=25565), \
         patch("server_manager.create_server") as create:
        response = client.post(
            "/api/servers/review", json=settings, headers={"X-CSRF-Token": get_csrf(client)}
        )
        assert response.status_code == 200
        assert response.json["local_endpoint"] == "127.0.0.1:25565"
        create.assert_not_called()

        response = client.post(
            "/api/servers/create",
            json={"settings": settings, "local_port": 25565},
            headers={"X-CSRF-Token": get_csrf(client)},
        )
        assert response.status_code == 400
        create.assert_not_called()


def test_network_page_records_metadata_without_controlling_playit():
    client = make_client()
    login(client)
    server = {
        "label": "Creative", "local_port": 25567,
        "public_hostname": "creative.example.com",
        "playit_status": "needs_configuration", "playit_endpoint": None,
    }
    with patch("docker_manager.get_server", return_value=server), \
         patch("server_manager.local_port_status", return_value="listening"), \
         patch("system_info.get_playit_status", return_value="running"), \
         patch("server_manager.save_playit_endpoint", return_value="example.gl.joinmc.link") as save:
        response = client.get("/servers/creative/network")
        assert response.status_code == 200
        assert b"127.0.0.1:25567" in response.data

        response = client.post(
            "/servers/creative/network",
            data={"playit_endpoint": "example.gl.joinmc.link", "csrf_token": get_csrf(client)},
        )
        assert response.status_code == 200
        save.assert_called_once_with("creative", "example.gl.joinmc.link")
