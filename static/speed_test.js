const button = document.getElementById("run-speed-test");
const message = document.getElementById("speed-message");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

button.addEventListener("click", async () => {
  button.disabled = true;
  button.textContent = "Testing…";
  message.textContent = "Downloading and uploading test data…";

  try {
    const response = await fetch("/api/speed-test", {
      method: "POST",
      headers: {"X-CSRF-Token": csrfToken},
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Speed test failed");

    document.getElementById("download-speed").textContent = `${data.download_mbps} Mbps`;
    document.getElementById("upload-speed").textContent = `${data.upload_mbps} Mbps`;
    message.textContent = "Test complete. These results are for the server's connection.";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Test again";
  }
});
