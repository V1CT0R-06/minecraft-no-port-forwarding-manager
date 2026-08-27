# Minecraft No-Port-Forwarding Manager

A small tool for creating and managing Minecraft servers without router port forwarding.

Licensed under the MIT License. You may use, change, and share it.

Project: https://github.com/V1CT0R-06/minecraft-no-port-forwarding-manager

It uses only:

- Python 3 and Flask;
- Jinja HTML templates;
- one normal CSS file;
- minimal vanilla JavaScript;
- Docker's Python SDK, `psutil`, and a small RCON library.

There is no Node.js, npm, frontend framework, database, Redis, WebSocket service, or build step.

## What it does

- monitors host CPU, RAM, swap, disk, uptime, and Playit;
- includes an on-demand Internet download and upload speed test;
- discovers configured Java memory and actual Docker resource use;
- starts, stops, and restarts registered Minecraft containers;
- displays players, Paper TPS/MSPT, logs, and RCON results;
- manages whitelists without editing `whitelist.json`;
- safely reviews and applies Java memory changes;
- creates persistent Vanilla, Paper, Fabric, Forge, and NeoForge servers;
- removes panel-created servers while archiving their files for recovery;
- removes imported containers while preserving their worlds and Compose files;
- lists recoverable archives and can permanently delete a selected archive;
- displays Minecraft, server software, and detected Pixelmon versions;
- guides Playit tunnel setup;
- generates and verifies DNS-only CNAME and Minecraft SRV records;
- works with any number of servers registered in a simple JSON file.

## Start here

After installing the panel, sign in and open **Tutorial** in the top navigation. It contains a complete beginner-oriented guide covering:

1. architecture and requirements;
2. official Docker installation;
3. panel and systemd deployment;
4. login setup;
5. importing or creating servers;
6. Playit and domain setup;
7. whitelist, console, and logs;
8. memory changes and backups;
9. updates, troubleshooting, and safe sharing.

## Quick development setup

```bash
git clone https://github.com/V1CT0R-06/minecraft-no-port-forwarding-manager.git minecraft-panel
cd minecraft-panel
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
cp config/servers.example.json config/servers.json
.venv/bin/pytest
.venv/bin/python app.py
```

The panel can also start without `config/servers.json`; it will show an empty dashboard and create the registry when the first server is created.

Open `.env` and choose your login:

```text
PANEL_USERNAME=admin
PANEL_PASSWORD=choose-a-long-password
```

The `.env` file is ignored by Git. Do not publish it.

## Configuration

Personal runtime settings are deliberately ignored by Git:

- `.env` contains login and machine-specific settings;
- `config/servers.json` contains the local server registry;
- `/etc/minecraft-panel/minecraft.env` contains the shared Minecraft RCON secret;
- `/srv/minecraft/servers/` contains panel-created Compose projects and worlds.

Share the example files instead:

- `.env.example`;
- `config/servers.example.json`;
- `deploy/minecraft-panel.service.example`.

Before publishing, inspect Git history as well as the current files. Deleting a private runtime file in a later commit does not remove it from old commits. The safest public release is a fresh repository created from a sanitized working tree.

Important environment variables:

| Variable | Purpose |
|---|---|
| `PANEL_USERNAME` | Single administrator username |
| `PANEL_PASSWORD` | Single administrator password |
| `MINECRAFT_DNS_ZONE` | Your DNS zone, such as `example.com` |
| `MINECRAFT_SERVER_ROOT` | Parent directory for new servers |
| `MINECRAFT_SHARED_ENV_FILE` | Secret environment file used by new Minecraft containers |

## Code map

- `app.py` — Flask setup, login, CSRF protection, pages, and API routes.
- `docker_manager.py` — registered-container inspection and control.
- `minecraft.py` — RCON commands and response parsing.
- `server_manager.py` — validation, port allocation, Compose generation, and server creation.
- `system_info.py` — live host resource and Playit status.
- `network_manager.py` — read-only DNS inspection and record guidance.
- `templates/` — server-rendered HTML pages.
- `static/style.css` — the entire visual design.
- `static/*.js` — small browser-side refresh and form helpers.
- `tests/` — tests that use mocks and temporary directories instead of live worlds.

The Python code favors plain functions over class hierarchies.

## Security model

Docker access is effectively root access. The production service runs as a dedicated non-login account and receives the Docker supplementary group. The application accepts only server IDs registered in `config/servers.json`; it never accepts arbitrary Docker container names.

The panel:

- does not expose a Linux shell;
- does not insert browser input into shell commands;
- does not expose Docker over TCP;
- does not store RCON, Playit, Cloudflare, or login secrets in JSON;
- does not modify Playit tunnels or Cloudflare records automatically;
- should remain private on LAN, NetBird, or another trusted VPN.

Authentication is still required on a private network.

## Persistent server layout

Panel-created servers remain understandable without the panel:

```text
/srv/minecraft/servers/
└── creative/
    ├── compose.yaml
    └── data/
```

The host-only port binding looks like `127.0.0.1:25565:25565`. Playit on the same host can reach it, but it is not published to the LAN or router.

## Public connection path

```text
Player
  → server.example.com
  → DNS-only CNAME and Minecraft SRV
  → Playit public relay
  → Playit agent on the homelab
  → 127.0.0.1:unique-port
  → Docker port mapping
  → Minecraft container:25565
```

The DHCP LAN address does not matter to friends. NetBird is separate private access. Router port forwarding and Cloudflare Tunnel are not used.

## Running and updating

Development:

```bash
.venv/bin/python app.py
```

Production:

```bash
sudo systemctl status minecraft-panel
sudo systemctl restart minecraft-panel
sudo journalctl -u minecraft-panel -n 100
```

Before restarting after an update:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```

## Tests

```bash
.venv/bin/pytest
```

Tests cover authentication, CSRF, the JSON allowlist, resource calculations, memory parsing, Compose editing, server creation, RCON parsing, Playit metadata, and DNS guidance without controlling live Minecraft containers.
