const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
let reviewedServer = null;

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value == null ? "N/A" : String(value);
  return element.innerHTML;
}

function showMessage(text, type = "error") {
  const box = document.getElementById("message");
  box.textContent = text;
  box.className = `message ${type}`;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || "Request failed");
  return data;
}

document.getElementById("create-server-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  const settings = Object.fromEntries(form.entries());
  settings.whitelist = form.has("whitelist");
  try {
    const review = await postJson("/api/servers/review", settings);
    reviewedServer = review;
    const warning = review.memory_warning
      ? `<p class="message error">Warning: projected Minecraft heaps plus the recommended host reserve exceed physical RAM. You may continue, but should normally run servers at different times or reduce allocations.</p>`
      : "";
    const box = document.getElementById("create-review");
    box.innerHTML = `<h2>Review New Server</h2>${warning}<div class="details">
      <div class="row"><span>Name</span><strong>${escapeHtml(review.settings.display_name)}</strong></div>
      <div class="row"><span>Type / version</span><strong>${escapeHtml(review.settings.server_type)} / ${escapeHtml(review.settings.version)}</strong></div>
      <div class="row"><span>Docker image</span><strong>${escapeHtml(review.settings.image)}</strong></div>
      <div class="row"><span>Java RAM</span><strong>${escapeHtml(review.settings.memory)}</strong></div>
      <div class="row"><span>Host RAM / available</span><strong>${review.host_total_gib} / ${review.host_available_gib} GiB</strong></div>
      <div class="row"><span>Recommended host reserve</span><strong>${review.recommended_reserve_gib} GiB</strong></div>
      <div class="row"><span>Projected Minecraft heaps</span><strong>${review.projected_minecraft_gib} GiB</strong></div>
      <div class="row"><span>Data directory</span><strong>${escapeHtml(review.data_directory)}</strong></div>
      <div class="row"><span>Compose file</span><strong>${escapeHtml(review.compose_file)}</strong></div>
      <div class="row"><span>Local endpoint</span><strong>${escapeHtml(review.local_endpoint)}</strong></div>
      <div class="row"><span>Public hostname</span><strong>${escapeHtml(review.settings.public_hostname)}</strong></div>
      <div class="row"><span>Whitelist</span><strong>${review.settings.whitelist ? "Enabled" : "Disabled"}</strong></div>
      <div class="row"><span>Playit</span><strong>${review.playit_status}</strong></div>
    </div><p class="muted">Create starts the new container. World data remains in the displayed data directory. Public access is configured separately in the next stage.</p>
    <div class="actions"><button class="button secondary" onclick="cancelReview()">Cancel</button><button id="confirm-create" class="button primary" onclick="createServer()">Create</button></div>`;
    box.classList.remove("hidden");
    box.scrollIntoView({behavior: "smooth"});
  } catch (error) { showMessage(error.message); }
});

function cancelReview() {
  reviewedServer = null;
  document.getElementById("create-review").classList.add("hidden");
}

async function createServer() {
  if (!reviewedServer) return;
  const button = document.getElementById("confirm-create");
  button.disabled = true;
  button.textContent = "Creating…";
  try {
    const result = await postJson("/api/servers/create", {
      settings: reviewedServer.settings,
      local_port: reviewedServer.local_port,
      confirm_create: true,
    });
    showMessage(result.message, "success");
    setTimeout(() => location.assign("/"), 1500);
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
    button.textContent = "Create";
  }
}
