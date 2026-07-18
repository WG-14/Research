(() => {
  "use strict";

  const uploadInput = document.querySelector("[data-upload-form] input[type=file]");
  const fileName = document.querySelector("[data-file-name]");
  const dropZone = document.querySelector("[data-drop-zone]");
  const filePicker = document.querySelector("[data-file-picker]");
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
    if (filePicker) {
      filePicker.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          uploadInput.click();
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
      if (!response.ok) throw new Error(`status polling failed: ${response.status}`);
      const state = await response.json();
      const stage = detail.querySelector("[data-job-stage]");
      const message = detail.querySelector("[data-job-message]");
      const updated = detail.querySelector("[data-job-updated]");
      const badge = detail.querySelector("[data-job-badge]");
      const icon = detail.querySelector("[data-job-icon]");
      const pollError = detail.querySelector("[data-job-poll-error]");
      if (stage) stage.textContent = state.progress.stage_label;
      if (message) message.textContent = state.progress.message;
      if (updated) updated.textContent = `${new Date(state.updated_at).toLocaleString()} 업데이트`;
      if (badge) {
        badge.textContent = state.status_label;
        badge.className = `badge badge-large badge-${state.status.toLowerCase()}`;
      }
      if (icon) icon.className = `status-icon status-${state.status.toLowerCase()}`;
      if (pollError) pollError.hidden = true;
      if (state.terminal) window.location.reload();
    } catch (_) {
      // A transient polling failure must not change persisted job state.
      const pollError = detail.querySelector("[data-job-poll-error]");
      if (pollError) pollError.hidden = false;
    }
  };
  window.setInterval(poll, 3000);
})();
