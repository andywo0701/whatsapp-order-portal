(function (root) {
  // 2MB stays under a ~100s proxy limit at about 50KB/s. Each piece aborts around 90s and retries.
  // Three pieces in flight use more than one connection without saturating the browser's per-host limit.
  var DEFAULT_CHUNK_BYTES = 2 * 1024 * 1024;
  var CHUNK_TIMEOUT_MS = 90 * 1000;
  var CHUNK_ATTEMPTS = 3;
  var PARALLEL_CHUNKS = 3;
  var SESSION_KEY = "tg-chunk-upload";
  var TIMEOUT_MESSAGE = "這一段傳送逾時。請再按「提交上載」，已收到的部分會自動略過。";
  var CHUNK_FAIL = "這一段未送出，請再試。";

  function formatSize(bytes) {
    var size = bytes > 0 ? bytes : 0;
    if (size >= 1024 * 1024 * 1024) return (size / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    if (size >= 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + " MB";
    if (size >= 1024) return Math.round(size / 1024) + " KB";
    return size + " B";
  }

  function sentLabel(sent, total) {
    return "已送出 " + formatSize(sent) + "／共 " + formatSize(total);
  }

  function uploadingLabel(name, sent, total) {
    return "正在上載「" + name + "」 · " + sentLabel(sent, total);
  }

  function bytesUploaded(blobSize, event) {
    if (!event || !event.lengthComputable || !event.total || !(blobSize > 0)) return 0;
    var ratio = event.loaded / event.total;
    if (!(ratio > 0)) return 0;
    if (ratio > 1) ratio = 1;
    return Math.min(blobSize, Math.round(blobSize * ratio));
  }

  function planPieces(size, chunkBytes, received) {
    var pieces = [];
    if (!(size > 0) || !(chunkBytes > 0)) return pieces;
    var known = {};
    var list = received || [];
    for (var i = 0; i < list.length; i++) known[list[i]] = true;
    var count = Math.ceil(size / chunkBytes);
    for (var index = 0; index < count; index++) {
      var start = index * chunkBytes;
      var end = Math.min(size, start + chunkBytes);
      pieces.push({
        index: index,
        start: start,
        end: end,
        length: end - start,
        skip: known[index] === true,
      });
    }
    return pieces;
  }

  function fileSignature(files) {
    return files
      .map(function (file) {
        return file.name + ":" + file.size + ":" + file.lastModified;
      })
      .join("|");
  }

  function attach(doc, env) {
    env = env || root;
    var form = doc.getElementById("upload-form");
    if (!form || !env.fetch || !env.FormData) return;

    var chunkBytes = Number(form.getAttribute("data-chunk-bytes"));
    if (!(chunkBytes > 0)) chunkBytes = DEFAULT_CHUNK_BYTES;
    var progress = doc.getElementById("upload-progress");
    var fill = doc.getElementById("upload-progress-fill");
    var track = doc.getElementById("upload-progress-track");
    var label = doc.getElementById("upload-progress-label");
    var errorBox = doc.getElementById("upload-error");
    var button = form.querySelector('button[type="submit"]');
    var fileInput = form.querySelector('input[type="file"][name="files"]');
    var idleLabel = button && button.textContent ? String(button.textContent).replace(/^\s+|\s+$/g, "") : "";
    if (!idleLabel) idleLabel = "提交上載";
    var storage = env.sessionStorage;

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

    function releaseButton() {
      if (!button) return;
      button.disabled = false;
      button.textContent = idleLabel;
    }

    async function readJson(response) {
      try {
        return await response.json();
      } catch (err) {
        return {};
      }
    }

    async function postJson(url, payload) {
      var response = await env.fetch(url, {
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

    function readSaved() {
      if (!storage) return null;
      try {
        return JSON.parse(storage.getItem(SESSION_KEY) || "null");
      } catch (err) {
        return null;
      }
    }

    function writeSaved(id, signature) {
      if (!storage) return;
      storage.setItem(SESSION_KEY, JSON.stringify({ id: id, signature: signature }));
    }

    async function startUpload() {
      clearError();
      var files = Array.prototype.slice.call(fileInput.files || []);
      if (!files.length) {
        showError("請至少選擇一個檔案。");
        releaseButton();
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
        var signature = fileSignature(files);
        var session = await reuseOrStart(signature, files);
        if (session.chunk_bytes) chunkBytes = Number(session.chunk_bytes);
        var sent = 0;
        for (var i = 0; i < files.length; i++) {
          var already = (session.files[i] && session.files[i].received) || [];
          var fileName = files[i].name;
          var pieces = planPieces(files[i].size, chunkBytes, already);
          var pending = [];
          for (var p = 0; p < pieces.length; p++) {
            if (pieces[p].skip) sent += pieces[p].length;
            else pending.push(pieces[p]);
          }
          if (sent > 0) setProgress(sent, total, sentLabel(sent, total));
          if (!pending.length) continue;
          await uploadPending(session.id, i, files[i], fileName, pending, sent, total);
          for (var q = 0; q < pending.length; q++) sent += pending[q].length;
          setProgress(sent, total, uploadingLabel(fileName, sent, total));
        }
        var done = await postJson("/upload/" + session.id + "/complete", { csrf: csrf() });
        if (storage) storage.removeItem(SESSION_KEY);
        env.location.assign(done.redirect || "/");
      } catch (err) {
        showError(err && err.message ? err.message : "上載未完成，請稍後再試。");
        releaseButton();
      }
    }

    async function uploadPending(id, fileIndex, file, fileName, pending, baseSent, totalBytes) {
      var confirmed = 0;
      var inflight = {};
      var next = 0;
      var failed = null;

      function extra() {
        var sum = 0;
        for (var key in inflight) sum += inflight[key];
        return sum;
      }

      function report(text) {
        var amount = baseSent + confirmed + extra();
        setProgress(amount, totalBytes, text || uploadingLabel(fileName, amount, totalBytes));
      }

      function take() {
        if (failed || next >= pending.length) return null;
        var piece = pending[next];
        next += 1;
        return piece;
      }

      async function runPiece(piece) {
        var index = piece.index;
        inflight[index] = 0;
        try {
          await sendChunk(
            id,
            fileIndex,
            index,
            file.slice(piece.start, piece.end),
            function (pieceSent) {
              inflight[index] = pieceSent;
              report();
            },
            function () {
              inflight[index] = 0;
              var amount = baseSent + confirmed + extra();
              setProgress(amount, totalBytes, "正在再試 · " + sentLabel(amount, totalBytes));
            }
          );
          delete inflight[index];
          confirmed += piece.length;
          report();
        } catch (err) {
          delete inflight[index];
          if (!failed) failed = err || new Error(CHUNK_FAIL);
          throw failed;
        }
      }

      async function worker() {
        for (;;) {
          var piece = take();
          if (!piece) return;
          await runPiece(piece);
        }
      }

      report();
      var workers = [];
      var count = pending.length < PARALLEL_CHUNKS ? pending.length : PARALLEL_CHUNKS;
      for (var w = 0; w < count; w++) workers.push(worker());
      var settled = await Promise.all(
        workers.map(function (job) {
          return job.then(
            function () {
              return null;
            },
            function (err) {
              return err || new Error(CHUNK_FAIL);
            }
          );
        })
      );
      for (var s = 0; s < settled.length; s++) {
        if (settled[s]) throw settled[s];
      }
    }

    async function reuseOrStart(signature, files) {
      var saved = readSaved();
      if (saved && saved.signature === signature && saved.id) {
        var status = await env.fetch("/upload/" + saved.id, {
          credentials: "same-origin",
          headers: { Accept: "application/json", "X-CSRF-Token": csrf() },
        });
        if (status.ok) {
          var data = await readJson(status);
          // An older session cut the file into larger pieces. Start fresh so the next
          // pieces match the current limit and can still finish inside the request budget.
          if (data && data.id && Number(data.chunk_bytes) === chunkBytes) return data;
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
      writeSaved(created.id, signature);
      return created;
    }

    function postChunkOnce(id, fileIndex, index, blob, onPiece) {
      return new Promise(function (resolve, reject) {
        var xhr = new env.XMLHttpRequest();
        var settled = false;
        function finish(err, data) {
          if (settled) return;
          settled = true;
          if (err) reject(err);
          else resolve(data);
        }
        xhr.open("POST", "/upload/" + id + "/chunk");
        xhr.timeout = CHUNK_TIMEOUT_MS;
        xhr.withCredentials = true;
        xhr.setRequestHeader("Accept", "application/json");
        xhr.setRequestHeader("X-CSRF-Token", csrf());
        if (xhr.upload) {
          xhr.upload.onprogress = function (event) {
            if (settled || typeof onPiece !== "function") return;
            onPiece(bytesUploaded(blob && blob.size, event));
          };
        }
        xhr.onload = function () {
          var data = {};
          try {
            data = JSON.parse(xhr.responseText || "{}");
          } catch (err) {
            data = {};
          }
          if (xhr.status >= 200 && xhr.status < 300) finish(null, data);
          else finish(new Error((data && data.error) || CHUNK_FAIL));
        };
        xhr.onerror = function () {
          finish(new Error(CHUNK_FAIL));
        };
        xhr.ontimeout = function () {
          finish(new Error(TIMEOUT_MESSAGE));
        };
        xhr.onabort = function () {
          finish(new Error(CHUNK_FAIL));
        };
        var body = new env.FormData();
        body.append("csrf", csrf());
        body.append("file_index", String(fileIndex));
        body.append("index", String(index));
        body.append("chunk", blob, "chunk");
        xhr.send(body);
      });
    }

    async function sendChunk(id, fileIndex, index, blob, onPiece, onRetry) {
      var lastError = null;
      for (var attempt = 0; attempt < CHUNK_ATTEMPTS; attempt++) {
        if (attempt > 0 && typeof onRetry === "function") onRetry(attempt);
        try {
          return await postChunkOnce(id, fileIndex, index, blob, onPiece);
        } catch (err) {
          lastError = err;
        }
      }
      throw lastError || new Error(CHUNK_FAIL);
    }
  }

  var api = {
    DEFAULT_CHUNK_BYTES: DEFAULT_CHUNK_BYTES,
    CHUNK_TIMEOUT_MS: CHUNK_TIMEOUT_MS,
    CHUNK_ATTEMPTS: CHUNK_ATTEMPTS,
    PARALLEL_CHUNKS: PARALLEL_CHUNKS,
    SESSION_KEY: SESSION_KEY,
    TIMEOUT_MESSAGE: TIMEOUT_MESSAGE,
    formatSize: formatSize,
    sentLabel: sentLabel,
    uploadingLabel: uploadingLabel,
    bytesUploaded: bytesUploaded,
    planPieces: planPieces,
    fileSignature: fileSignature,
    attach: attach,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root.document && typeof root.document.getElementById === "function") {
    attach(root.document, root);
  }
})(typeof window !== "undefined" ? window : globalThis);
