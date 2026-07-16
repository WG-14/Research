(() => {
  "use strict";

  const uploadInput = document.querySelector("[data-upload-form] input[type=file]");
  const fileName = document.querySelector("[data-file-name]");
  const dropZone = document.querySelector("[data-drop-zone]");
  if (uploadInput && fileName) {
    const updateName = () => {
      fileName.textContent = uploadInput.files?.[0]?.name || "선택된 파일 없음";
    };
    uploadInput.addEventListener("change", updateName);
    if (dropZone) {
      ["dragenter", "dragover"].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropZone.classList.add("is-dragging");
      }));
      ["dragleave", "drop"].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropZone.classList.remove("is-dragging");
      }));
      dropZone.addEventListener("drop", (event) => {
        if (event.dataTransfer?.files?.length) {
          uploadInput.files = event.dataTransfer.files;
          updateName();
        }
      });
    }
  }

  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });

  const detail = document.querySelector("[data-job-status-url]");
  if (!detail || detail.dataset.terminal === "true") return;
  const statusUrl = detail.dataset.jobStatusUrl;
  const poll = async () => {
    try {
      const response = await fetch(statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!response.ok) return;
      const state = await response.json();
      const stage = detail.querySelector("[data-job-stage]");
      const message = detail.querySelector("[data-job-message]");
      const updated = detail.querySelector("[data-job-updated]");
      const badge = detail.querySelector("[data-job-badge]");
      if (stage) stage.textContent = state.stage;
      if (message) message.textContent = state.message;
      if (updated) updated.textContent = `${state.updated_at} 업데이트`;
      if (badge) {
        badge.textContent = state.status_label;
        badge.className = `badge badge-large badge-${state.status.toLowerCase()}`;
      }
      if (state.terminal) window.location.reload();
    } catch (_) {
      // A transient polling failure must not change persisted job state.
    }
  };
  window.setInterval(poll, 3000);
})();
