const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
let latestStatus = null;

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value == null ? "N/A" : String(value);
  return element.innerHTML;
}

function showMessage(text, type = "success") {
  const box = document.getElementById("message");
  box.textContent = text;
  box.className = `message ${type}`;
  setTimeout(() => box.classList.add("hidden"), 5000);
}

function metric(label, value, percent = null) {
  const bar = percent == null ? "" : `<div class="progress"><span style="width:${Math.min(100, percent)}%"></span></div>`;
  return `<div class="metric"><span class="metric-label">${label}</span><span class="metric-value">${escapeHtml(value)}</span>${bar}</div>`;
}

function renderHost(host, resources) {
  document.getElementById("host-grid").innerHTML = [
    metric("CPU", `${host.cpu_percent}%`, host.cpu_percent),
    metric("RAM", `${host.ram_used_gib} / ${host.ram_total_gib} GiB`, host.ram_percent),
    metric("Available RAM", `${host.ram_available_gib} GiB`),
    metric("Disk", `${host.disk_percent}%`, host.disk_percent),
    metric("Uptime", host.uptime),
    metric("Playit", host.playit === "running" ? "● Running" : host.playit === "stopped" ? "○ Stopped" : "? Unknown"),
  ].join("");
  document.getElementById("host-more-grid").innerHTML = [
    metric("Hostname", host.hostname),
    metric("CPU cores", `${host.physical_cpu_count ?? "N/A"} physical / ${host.logical_cpu_count ?? "N/A"} logical`),
    metric("Recommended host reserve", `${resources.recommended_reserve_gib} GiB`),
    metric("Minecraft configured heaps", `${resources.minecraft_configured_gib} GiB`),
    metric("Running server heaps", `${resources.minecraft_running_gib} GiB`),
    metric("Suggested heap headroom", `${resources.planned_heap_headroom_gib} GiB`),
    metric("Swap", `${host.swap_used_gib} / ${host.swap_total_gib} GiB`, host.swap_percent),
  ].join("");
}

function whitelistHtml(server) {
  const mc = server.minecraft;
  if (!mc || !mc.available) return `<p class="muted">Minecraft information unavailable</p>`;
  const items = mc.whitelist.length
    ? mc.whitelist.map(name => `<div class="whitelist-item"><span>${escapeHtml(name)}</span><button class="button danger" onclick="changeWhitelist('${server.key}','remove','${escapeHtml(name)}')">Remove</button></div>`).join("")
    : `<p class="muted">No players listed</p>`;
  return `${items}<form class="inline-form" onsubmit="addWhitelist(event,'${server.key}')"><input name="username" minlength="3" maxlength="16" pattern="[A-Za-z0-9_]{3,16}" placeholder="Minecraft username" required><button class="button primary">Add Player</button></form>`;
}

function resourcesHtml(server) {
  const resources = latestStatus.resources;
  const host = latestStatus.host;
  return `<details class="panel-section"><summary>Resources</summary>
    <div class="details">
      <div class="row"><span>Host RAM</span><strong>${host.ram_total_gib} GiB total</strong></div>
      <div class="row"><span>Currently available</span><strong>${host.ram_available_gib} GiB</strong></div>
      <div class="row"><span>Recommended host reserve</span><strong>${resources.recommended_reserve_gib} GiB</strong></div>
      <div class="row"><span>Configured Java memory</span><strong>${escapeHtml(server.configured_memory || "N/A")}</strong></div>
      <div class="row"><span>Current container usage</span><strong>${server.current_memory_gib == null ? "N/A" : `${server.current_memory_gib} GiB`}</strong></div>
      <div class="row"><span>Current container CPU</span><strong>${server.cpu_percent == null ? "N/A" : `${server.cpu_percent}%`}</strong></div>
    </div>
    <form class="inline-form" onsubmit="saveMemory(event,'${server.key}')">
      <input name="memory" pattern="[0-9]+([.][0-9]+)?[GgMm]([Ii]?[Bb])?" placeholder="4G or 4096M" required>
      <button class="button primary">Review memory change</button>
    </form>
    <p class="muted">Changing Java memory recreates only this container. Persistent world data remains mounted at /data.</p>
  </details>`;
}

function networkHtml(server) {
  const playit = server.playit_status === "configured" ? "Configured" : server.playit_status === "existing_dns_verified" ? "Existing — DNS verified" : "Action required";
  return `<details class="panel-section"><summary>Network</summary><div class="details">
    <div class="row"><span>Public hostname</span><strong>${escapeHtml(server.public_hostname || "Not configured")}</strong></div>
    <div class="row"><span>Local endpoint</span><strong>${server.local_port ? `127.0.0.1:${server.local_port}` : "Existing Docker network"}</strong></div>
    <div class="row"><span>Playit</span><strong>${playit}</strong></div>
    <div class="row"><span>Playit endpoint</span><strong>${escapeHtml(server.playit_endpoint || "Not recorded")}</strong></div>
  </div><a class="button secondary link-button" href="/servers/${server.key}/network">Open Network Setup</a></details>`;
}

function renderServer(server) {
  const running = server.state === "running";
  const statusClass = running ? "online" : server.state === "stopped" ? "offline" : "unknown";
  const statusText = running ? "● ONLINE" : server.state === "stopped" ? "○ OFFLINE" : "? UNKNOWN";
  const mc = server.minecraft || {};
  const playerText = mc.available && mc.players.online != null ? `${mc.players.online} / ${mc.players.max}` : "N/A";
  const playerNames = mc.available && mc.players.names.length ? mc.players.names.map(name => `<li>${escapeHtml(name)}</li>`).join("") : `<li class="muted">Nobody online</li>`;
  const controls = running
    ? `<button class="button secondary" onclick="controlServer('${server.key}','restart')">Restart</button><button class="button danger" onclick="controlServer('${server.key}','stop')">Stop</button>`
    : server.state === "stopped" ? `<button class="button primary" onclick="controlServer('${server.key}','start')">Start</button>` : "";
  const runningSections = running ? `
    <details class="panel-section"><summary>Players online — ${escapeHtml(mc.players?.online ?? "N/A")}</summary><ul class="name-list">${playerNames}</ul></details>
    <details class="panel-section"><summary>Whitelist</summary><div class="whitelist">${whitelistHtml(server)}</div></details>
    <details class="panel-section"><summary>Minecraft Console</summary><p class="muted">Minecraft commands only. This is not a Linux terminal.</p><form class="inline-form" onsubmit="sendConsole(event,'${server.key}')"><input name="command" maxlength="200" placeholder="say hello" required><button class="button primary">Send Command</button></form><pre id="${server.key}-console-result" class="console-result">No command sent.</pre></details>
    <details class="panel-section"><summary>Recent Logs</summary><button class="button secondary" onclick="loadLogs('${server.key}')">Refresh Logs</button><pre id="${server.key}-logs" class="logs">Select Refresh Logs.</pre></details>` : "";

  document.getElementById(`${server.key}-card`).innerHTML = `
    <div class="server-heading"><div><h2>${escapeHtml(server.label)}</h2><span class="muted">${escapeHtml(server.public_hostname || server.server_type || "Minecraft")}</span></div><span class="status ${statusClass}">${statusText}</span></div>
    ${server.error ? `<p class="message error">${escapeHtml(server.error)}</p>` : ""}
    <div class="server-summary-grid">
      <div><span>Players</span><strong>${playerText}</strong></div>
      <div><span>Java RAM</span><strong>${escapeHtml(server.configured_memory || "N/A")}</strong></div>
      <div><span>RAM now</span><strong>${server.current_memory_gib == null ? "N/A" : `${server.current_memory_gib} GiB`}</strong></div>
      <div><span>CPU</span><strong>${server.cpu_percent == null ? "N/A" : `${server.cpu_percent}%`}</strong></div>
    </div>
    <div class="actions">${controls}</div>
    <details class="panel-section"><summary>Server details</summary><div class="details">
      <div class="row"><span>Docker health</span><strong>${escapeHtml(server.health || "N/A")}</strong></div>
      <div class="row"><span>Local endpoint</span><strong>${server.local_port ? `127.0.0.1:${server.local_port}` : "Docker network only"}</strong></div>
      <div class="row"><span>TPS</span><strong>${mc.tps == null ? "N/A" : mc.tps.toFixed(1)}</strong></div>
      <div class="row"><span>MSPT</span><strong>${mc.mspt == null ? "N/A" : `${mc.mspt.toFixed(1)} ms`}</strong></div>
      <div class="row"><span>Docker CPU limit</span><strong>${server.cpu_limit == null ? "Not configured" : `${server.cpu_limit} CPU`}</strong></div>
      <div class="row"><span>Container uptime</span><strong>${escapeHtml(server.uptime || "N/A")}</strong></div>
      <div class="row"><span>Whitelist configured</span><strong>${server.whitelist_enabled ? "Enabled" : "Disabled"}</strong></div>
    </div></details>${resourcesHtml(server)}${networkHtml(server)}${runningSections}`;
}

function renderServers(servers) {
  const grid = document.getElementById("server-grid");
  const openSections = {};
  grid.querySelectorAll(".server-card").forEach(card => {
    openSections[card.id] = [...card.querySelectorAll("details")]
      .map((section, index) => section.open ? index : -1)
      .filter(index => index >= 0);
  });

  const serverList = Object.values(servers);
  if (!serverList.length) {
    grid.innerHTML = `<article class="card empty-state"><span class="empty-icon">+</span><h2>No servers yet</h2><p class="muted">Create your first persistent Minecraft server, or read the tutorial before you begin.</p><div class="actions"><a class="button primary" href="/servers/new">Create server</a><a class="button secondary" href="/tutorial">Open tutorial</a></div></article>`;
    return;
  }
  grid.innerHTML = serverList.map(server => `<article id="${server.key}-card" class="card server-card is-${server.state}"></article>`).join("");
  serverList.forEach(server => {
    renderServer(server);
    const card = document.getElementById(`${server.key}-card`);
    const sections = card.querySelectorAll("details");
    (openSections[card.id] || []).forEach(index => {
      if (sections[index]) sections[index].open = true;
    });
  });
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (response.status === 401) return location.assign("/login");
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "Status request failed");
    latestStatus = data;
    renderHost(data.host, data.resources);
    renderServers(data.servers);
    document.getElementById("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function controlServer(key, action) {
  const server = latestStatus?.servers[key];
  let requestBody = {};
  if (action === "stop" || action === "restart") {
    if (!confirm(`${action[0].toUpperCase() + action.slice(1)} ${server.label}? Players may be disconnected.`)) return;
  }
  if (server.warn_before_start && action === "start") {
    const warning = `Start ${server.label}?\n\nConfigured Java RAM: ${server.configured_memory || "unknown"}\nCurrently available host RAM: ${latestStatus.host.ram_available_gib} GiB\nRecommended host reserve: ${latestStatus.resources.recommended_reserve_gib} GiB\n\nStarting this server may leave little memory for other Minecraft servers and host services.\n\nStart anyway?`;
    if (!confirm(warning)) return;
    requestBody.confirm_memory_warning = true;
  }
  try {
    const data = await postJson(`/api/servers/${key}/${action}`, requestBody);
    showMessage(data.message);
    setTimeout(refreshStatus, 1200);
  } catch (error) { showMessage(error.message, "error"); }
}

async function saveMemory(event, key) {
  event.preventDefault();
  const memory = event.target.elements.memory.value;
  try {
    const response = await fetch(`/api/servers/${key}/memory`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify({memory}),
    });
    const review = await response.json();
    if (response.status !== 409 || !review.requires_confirmation) {
      throw new Error(review.error || "Unable to review memory change");
    }
    const pressure = review.memory_warning
      ? "\n\nWARNING: The configured Minecraft heaps plus the recommended host reserve would exceed physical RAM."
      : "";
    const explanation = `Change ${review.server} memory?\n\nCurrent: ${review.current_memory}\nNew: ${review.new_memory}\n\nHost RAM: ${review.host_total_gib} GiB\nCurrently available: ${review.host_available_gib} GiB\nRecommended host reserve: ${review.recommended_reserve_gib} GiB\nProjected Minecraft heaps: ${review.projected_minecraft_gib} GiB${pressure}\n\nThis recreates and restarts only this container. Persistent world data remains unchanged.`;
    if (!confirm(explanation)) return;
    const result = await postJson(`/api/servers/${key}/memory`, {memory: review.new_memory, confirm_recreate: true});
    showMessage(result.message);
    setTimeout(refreshStatus, 1500);
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function addWhitelist(event, key) {
  event.preventDefault();
  const input = event.target.elements.username;
  try { const data = await postJson(`/api/servers/${key}/whitelist/add`, {username: input.value}); showMessage(data.message); input.value = ""; await refreshStatus(); }
  catch (error) { showMessage(error.message, "error"); }
}

async function changeWhitelist(key, action, username) {
  if (action === "remove" && !confirm(`Remove ${username} from the whitelist?`)) return;
  try { const data = await postJson(`/api/servers/${key}/whitelist/${action}`, {username}); showMessage(data.message); await refreshStatus(); }
  catch (error) { showMessage(error.message, "error"); }
}

async function sendConsole(event, key) {
  event.preventDefault();
  const input = event.target.elements.command;
  const output = document.getElementById(`${key}-console-result`);
  try { const data = await postJson(`/api/servers/${key}/console`, {command: input.value}); output.textContent = data.result || "Command completed with no output."; input.value = ""; }
  catch (error) { output.textContent = `Error: ${error.message}`; }
}

async function loadLogs(key) {
  const output = document.getElementById(`${key}-logs`);
  output.textContent = "Loading…";
  try { const response = await fetch(`/api/servers/${key}/logs`, {cache: "no-store"}); const data = await response.json(); if (!response.ok) throw new Error(data.error); output.textContent = data.logs || "No logs returned."; }
  catch (error) { output.textContent = `Error: ${error.message}`; }
}

refreshStatus();
setInterval(refreshStatus, 8000);
