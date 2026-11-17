(function () {
  var form = document.getElementById("upload-form");
  if (!form || !window.fetch || !window.FormData) return;

  var chunkBytes = Number(form.getAttribute("data-chunk-bytes")) || 8 * 1024 * 1024;
  var progress = document.getElementById("upload-progress");
  var fill = document.getElementById("upload-progress-fill");
  var track = document.getElementById("upload-progress-track");
  var label = document.getElementById("upload-progress-label");
  var errorBox = document.getElementById("upload-error");
  var button = form.querySelector('button[type="submit"]');
  var fileInput = form.querySelector('input[type="file"][name="files"]');

  form.addEventListener("submit", function (event) {
    if (!fileInput || !fileInput.files) return;
    event.preventDefault();
    startUpload();
  });

  function field(name) {
    var radios = form.querySelectorAll('input[name="' + name + '"]');
    if (radios.length && radios[0].type === "radio") {
      for (var i = 0; i < radios.length; i++) {
        if (radios[i].checked) return radios[i].value;
      }
      return "";
    }
    var el = form.querySelector('[name="' + name + '"]');
    return el && "value" in el ? el.value : "";
  }

  function csrf() {
    return field("csrf");
  }

  function showError(message) {
    if (!errorBox) return;
    errorBox.hidden = false;
    errorBox.textContent = message || "上載未完成，請稍後再試。";
  }

  function clearError() {
    if (!errorBox) return;
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function setProgress(sent, total, text) {
    if (progress) progress.hidden = false;
    var pct = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;
    if (fill) fill.style.width = pct + "%";
    if (track) {
      track.setAttribute("aria-valuenow", String(pct));
      track.setAttribute("aria-valuetext", text);
    }
    if (label) label.textContent = text;
  }

  function formatSize(bytes) {
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    if (bytes >= 1024 * 1024) return Math.max(1, Math.round(bytes / (1024 * 1024))) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (err) {
      return {};
    }
  }

  async function postJson(url, payload) {
    var response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": csrf(),
      },
      body: JSON.stringify(payload),
    });
    var data = await readJson(response);
    if (!response.ok) throw new Error(data.error || "上載未完成，請稍後再試。");
    return data;
  }

  async function startUpload() {
    clearError();
    var files = Array.prototype.slice.call(fileInput.files || []);
    if (!files.length) {
      showError("請至少選擇一個檔案。");
      return;
    }
    var total = 0;
    files.forEach(function (file) {
      total += file.size;
    });
    if (button) {
      button.disabled = true;
      button.textContent = "上載中…";
    }
    setProgress(0, total, "正在準備分段上載…");
    try {
      var signature = files
        .map(function (file) {
          return file.name + ":" + file.size + ":" + file.lastModified;
        })
        .join("|");
      var session = await reuseOrStart(signature, files);
      if (session.chunk_bytes) chunkBytes = session.chunk_bytes;
      var sent = 0;
      for (var i = 0; i < files.length; i++) {
        var known = {};
        var already = (session.files[i] && session.files[i].received) || [];
        already.forEach(function (index) {
          known[index] = true;
        });
        var size = files[i].size;
        var count = size > 0 ? Math.ceil(size / chunkBytes) : 0;
        for (var index = 0; index < count; index++) {
          var start = index * chunkBytes;
          var end = Math.min(size, start + chunkBytes);
          if (known[index]) {
            sent += end - start;
            setProgress(sent, total, "已送出 " + formatSize(sent) + "／共 " + formatSize(total));
            continue;
          }
          await sendChunk(session.id, i, index, files[i].slice(start, end));
          sent += end - start;
          setProgress(
            sent,
            total,
            "正在上載「" + files[i].name + "」 · 已送出 " + formatSize(sent) + "／共 " + formatSize(total)
          );
        }
      }
      var done = await postJson("/upload/" + session.id + "/complete", { csrf: csrf() });
      window.sessionStorage.removeItem("tg-chunk-upload");
      window.location.assign(done.redirect || "/");
    } catch (err) {
      showError(err && err.message ? err.message : "上載未完成，請稍後再試。");
      if (button) {
        button.disabled = false;
        button.textContent = "提交上載";
      }
    }
  }

  async function reuseOrStart(signature, files) {
    var saved = null;
    try {
      saved = JSON.parse(window.sessionStorage.getItem("tg-chunk-upload") || "null");
    } catch (err) {
      saved = null;
    }
    if (saved && saved.signature === signature && saved.id) {
      var status = await fetch("/upload/" + saved.id, {
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-CSRF-Token": csrf() },
      });
      if (status.ok) {
        var data = await readJson(status);
        if (data && data.id) return data;
      }
    }
    var created = await postJson("/upload/start", {
      csrf: csrf(),
      purpose: field("purpose"),
      record_mode: field("record_mode"),
      record_day: field("record_day"),
      record_date_start: field("record_date_start"),
      record_date_end: field("record_date_end"),
      note: field("note"),
      files: files.map(function (file) {
        return { name: file.name, size: file.size };
      }),
    });
    window.sessionStorage.setItem(
      "tg-chunk-upload",
      JSON.stringify({ id: created.id, signature: signature })
    );
    return created;
  }

  async function sendChunk(id, fileIndex, index, blob) {
    var lastError = null;
    for (var attempt = 0; attempt < 3; attempt++) {
      try {
        var body = new FormData();
        body.append("csrf", csrf());
        body.append("file_index", String(fileIndex));
        body.append("index", String(index));
        body.append("chunk", blob, "chunk");
        var response = await fetch("/upload/" + id + "/chunk", {
          method: "POST",
          credentials: "same-origin",
          headers: { Accept: "application/json", "X-CSRF-Token": csrf() },
          body: body,
        });
        var data = await readJson(response);
        if (!response.ok) throw new Error(data.error || "這一段未送出，請再試。");
        return data;
      } catch (err) {
        lastError = err;
      }
    }
    throw lastError || new Error("這一段未送出，請再試。");
  }
})();
