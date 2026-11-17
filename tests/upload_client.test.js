const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const upload = require("../static/upload.js");

const CHUNK = 2 * 1024 * 1024;

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    async json() {
      return body;
    },
  };
}

function memoryStorage(initial) {
  const data = Object.assign({}, initial || {});
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      data[key] = String(value);
    },
    removeItem(key) {
      delete data[key];
    },
  };
}

function fakeFile(name, size) {
  const slices = [];
  return {
    name: name,
    size: size,
    lastModified: 1700000000000,
    slices: slices,
    slice(start, end) {
      slices.push([start, end]);
      return { size: end - start, start: start, end: end };
    },
  };
}

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function mount(options) {
  const labels = [];
  const widths = [];
  const xhrs = [];
  const label = {};
  Object.defineProperty(label, "textContent", {
    get() {
      return this._text || "";
    },
    set(value) {
      this._text = String(value);
      labels.push(this._text);
    },
  });
  const fill = { style: {} };
  Object.defineProperty(fill.style, "width", {
    get() {
      return this._width || "";
    },
    set(value) {
      this._width = String(value);
      widths.push(this._width);
    },
  });
  const track = {
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
  const errorBox = { hidden: true, textContent: "" };
  const progress = { hidden: true };
  const button = { disabled: false, textContent: "提交上載" };
  const file = options.file || fakeFile("WhatsApp Chat - sample.zip", 2 * CHUNK);
  const fileInput = { type: "file", name: "files", files: [file] };
  const inputs = [
    { type: "hidden", name: "csrf", value: "csrf-test" },
    { type: "radio", name: "purpose", value: "auto", checked: true },
    { type: "radio", name: "purpose", value: "orders", checked: false },
    { type: "radio", name: "record_mode", value: "", checked: true },
    { type: "radio", name: "record_mode", value: "day", checked: false },
    { type: "date", name: "record_day", value: "" },
    { type: "date", name: "record_date_start", value: "" },
    { type: "date", name: "record_date_end", value: "" },
    { type: "textarea", name: "note", value: "" },
    fileInput,
  ];
  const form = {
    getAttribute(name) {
      if (name === "data-chunk-bytes") return String(options.chunkBytes || CHUNK);
      return null;
    },
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return button;
      if (selector === 'input[type="file"][name="files"]') return fileInput;
      const named = /\[name="([^"]+)"\]/.exec(selector);
      if (!named) return null;
      return inputs.find((item) => item.name === named[1]) || null;
    },
    querySelectorAll(selector) {
      const named = /\[name="([^"]+)"\]/.exec(selector);
      if (!named) return [];
      return inputs.filter((item) => item.name === named[1]);
    },
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
    listeners: {},
  };
  const nodes = {
    "upload-form": form,
    "upload-progress": progress,
    "upload-progress-fill": fill,
    "upload-progress-track": track,
    "upload-progress-label": label,
    "upload-error": errorBox,
  };
  const storage = memoryStorage(options.saved || {});
  const fetches = [];
  const assigned = [];
  const events = [];
  let active = 0;
  function FakeFormData() {
    this.entries = {};
  }
  FakeFormData.prototype.append = function append(name, value) {
    this.entries[name] = value;
  };
  function FakeXHR() {
    this.upload = {};
    this.headers = {};
    this.timeout = 0;
    this.withCredentials = false;
    this.status = 0;
    this.responseText = "";
    this.method = "";
    this.url = "";
  }
  FakeXHR.prototype.open = function open(method, url) {
    this.method = method;
    this.url = url;
  };
  FakeXHR.prototype.setRequestHeader = function setRequestHeader(name, value) {
    this.headers[name] = value;
  };
  FakeXHR.prototype.send = function send(body) {
    const xhr = this;
    xhr.body = body;
    xhr.done = false;
    active += 1;
    xhr.activeAtSend = active;
    xhrs.push(xhr);
    events.push("send");
    const mode = typeof options.xhr === "function" ? options.xhr(xhrs.length, xhr) : options.xhr;
    function succeed() {
      if (xhr.done) return;
      xhr.done = true;
      if (xhr.upload.onprogress) {
        xhr.upload.onprogress({ lengthComputable: true, loaded: 1, total: 2 });
      }
      xhr.status = 200;
      xhr.responseText = "{}";
      active -= 1;
      events.push("load");
      if (xhr.onload) xhr.onload();
    }
    function timeOut() {
      if (xhr.done) return;
      xhr.done = true;
      active -= 1;
      events.push("timeout");
      if (xhr.ontimeout) xhr.ontimeout();
    }
    xhr.finish = succeed;
    if (mode === "hold") return;
    Promise.resolve().then(() => {
      if (mode === "timeout") timeOut();
      else succeed();
    });
  };
  const serverChunk = options.serverChunkBytes || options.chunkBytes || CHUNK;
  const uploadId = options.uploadId || "abc123";
  async function fakeFetch(url, request) {
    fetches.push({ url: url, request: request });
    if (url === "/upload/start") {
      return jsonResponse(200, {
        id: uploadId,
        chunk_bytes: serverChunk,
        files: [{ received: [] }],
      });
    }
    if (String(url).endsWith("/complete")) {
      return jsonResponse(200, { ok: true, id: "sub1", redirect: "/submission/sub1?notice=submitted" });
    }
    if (!request || request.method === undefined) {
      return jsonResponse(
        200,
        options.status || { id: uploadId, chunk_bytes: serverChunk, files: [{ received: [] }] }
      );
    }
    return jsonResponse(404, { error: "找不到這次上載。" });
  }
  upload.attach(
    { getElementById: (id) => nodes[id] || null },
    {
      fetch: fakeFetch,
      FormData: FakeFormData,
      XMLHttpRequest: FakeXHR,
      sessionStorage: storage,
      location: {
        assign(url) {
          assigned.push(url);
        },
      },
    }
  );
  return {
    form: form,
    file: file,
    labels: labels,
    widths: widths,
    xhrs: xhrs,
    events: events,
    fetches: fetches,
    storage: storage,
    button: button,
    errorBox: errorBox,
    assigned: assigned,
    async begin() {
      form.listeners.submit({
        preventDefault() {},
      });
      await new Promise((resolve) => setImmediate(resolve));
    },
    async submit() {
      let prevented = false;
      form.listeners.submit({
        preventDefault() {
          prevented = true;
        },
      });
      await settle();
      return prevented;
    },
  };
}

test("chunk size is at most 2MB and each piece times out before the proxy", () => {
  assert.equal(upload.DEFAULT_CHUNK_BYTES, CHUNK);
  assert.ok(upload.DEFAULT_CHUNK_BYTES <= 2 * 1024 * 1024);
  assert.equal(upload.CHUNK_TIMEOUT_MS, 90 * 1000);
  assert.ok(upload.CHUNK_TIMEOUT_MS < 100 * 1000);
  assert.equal(upload.CHUNK_ATTEMPTS, 3);
  assert.equal(upload.PARALLEL_CHUNKS, 3);
  const source = fs.readFileSync(path.join(__dirname, "../static/upload.js"), "utf8");
  assert.equal(source.includes("readAsArrayBuffer"), false);
  assert.equal(source.includes(".arrayBuffer("), false);
  assert.equal(source.includes("upload.onprogress"), true);
});

test("in-flight progress is a fraction of the piece, not only the finished piece", () => {
  const blob = CHUNK;
  assert.equal(upload.bytesUploaded(blob, { lengthComputable: true, loaded: 1, total: 2 }), blob / 2);
  assert.equal(upload.bytesUploaded(blob, { lengthComputable: false, loaded: 1, total: 2 }), 0);
  assert.equal(upload.bytesUploaded(blob, { lengthComputable: true, loaded: 9, total: 4 }), blob);
  const pieces = upload.planPieces(5 * 1024 * 1024, CHUNK, [0]);
  assert.deepEqual(
    pieces.map((piece) => [piece.index, piece.length, piece.skip]),
    [
      [0, CHUNK, true],
      [1, CHUNK, false],
      [2, 1024 * 1024, false],
    ]
  );
});

test("the bar and label move while a piece is still uploading", async () => {
  const page = mount({ xhr: "ok" });
  const prevented = await page.submit();
  assert.equal(prevented, true);
  assert.ok(page.widths.includes("25%"));
  assert.ok(page.widths.includes("75%"));
  const mid = page.labels.findIndex((text) => text.includes("正在上載") && text.includes("1.0 MB"));
  const after = page.labels.findIndex((text, index) => index > mid && text.includes("2.0 MB"));
  assert.ok(mid > 0);
  assert.ok(after > mid);
  assert.equal(page.assigned[0], "/submission/sub1?notice=submitted");
  assert.equal(page.storage.getItem(upload.SESSION_KEY), null);
  assert.equal(page.xhrs[0].timeout, upload.CHUNK_TIMEOUT_MS);
  assert.equal(page.xhrs[0].withCredentials, true);
  assert.equal(page.xhrs[0].headers["X-CSRF-Token"], "csrf-test");
  assert.equal(page.xhrs[0].body.entries.csrf, "csrf-test");
  assert.equal(page.fetches[0].request.credentials, "same-origin");
  assert.equal(page.fetches[0].request.headers["X-CSRF-Token"], "csrf-test");
});

test("resume skips pieces the server already received", async () => {
  const file = fakeFile("WhatsApp Chat - sample.zip", 5 * 1024 * 1024);
  const signature = upload.fileSignature([file]);
  const page = mount({
    file: file,
    saved: {
      [upload.SESSION_KEY]: JSON.stringify({ id: "abc123", signature: signature }),
    },
    status: { id: "abc123", chunk_bytes: CHUNK, files: [{ received: [0] }] },
    xhr: "ok",
  });
  await page.submit();
  assert.deepEqual(
    page.xhrs.map((xhr) => xhr.body.entries.index),
    ["1", "2"]
  );
  assert.deepEqual(file.slices, [
    [CHUNK, CHUNK * 2],
    [CHUNK * 2, 5 * 1024 * 1024],
  ]);
  assert.ok(page.labels.includes("已送出 2.0 MB／共 5.0 MB"));
  assert.ok(page.labels.some((text) => text.includes("正在上載") && text.includes("4.5 MB")));
  assert.ok(page.widths.includes("90%"));
  assert.equal(
    page.fetches.some((call) => call.url === "/upload/start"),
    false
  );
  assert.equal(page.fetches[0].url, "/upload/abc123");
  assert.equal(page.fetches[0].request.credentials, "same-origin");
});

test("a timed out piece retries, then restores the button with a Traditional Chinese error", async () => {
  const page = mount({
    file: fakeFile("notes.txt", 100),
    xhr: "timeout",
  });
  await page.submit();
  assert.equal(page.xhrs.length, upload.CHUNK_ATTEMPTS);
  page.xhrs.forEach((xhr) => {
    assert.equal(xhr.timeout, 90 * 1000);
    assert.equal(xhr.method, "POST");
    assert.equal(xhr.url, "/upload/abc123/chunk");
  });
  assert.equal(page.button.disabled, false);
  assert.equal(page.button.textContent, "提交上載");
  assert.equal(page.errorBox.hidden, false);
  assert.equal(page.errorBox.textContent, upload.TIMEOUT_MESSAGE);
  assert.match(page.errorBox.textContent, /請再按「提交上載」/);
  assert.ok(page.labels.some((text) => text.includes("正在再試")));
  const saved = JSON.parse(page.storage.getItem(upload.SESSION_KEY));
  assert.equal(saved.id, "abc123");
  assert.equal(
    page.fetches.some((call) => call.url === "/upload/abc123/complete"),
    false
  );
});

test("retries still finish the piece when a later attempt succeeds", async () => {
  const page = mount({
    file: fakeFile("notes.txt", 100),
    xhr(attempt) {
      return attempt < 3 ? "timeout" : "ok";
    },
  });
  await page.submit();
  assert.equal(page.xhrs.length, 3);
  assert.equal(page.assigned[0], "/submission/sub1?notice=submitted");
  assert.equal(page.errorBox.hidden, true);
  assert.equal(page.storage.getItem(upload.SESSION_KEY), null);
});

test("at most three pieces are in flight, and their progress adds up", async () => {
  const page = mount({
    file: fakeFile("WhatsApp Chat - sample.zip", 4 * CHUNK),
    xhr: "hold",
  });
  await page.begin();
  assert.equal(page.xhrs.length, upload.PARALLEL_CHUNKS);
  assert.equal(Math.max(...page.xhrs.map((xhr) => xhr.activeAtSend)), upload.PARALLEL_CHUNKS);
  page.xhrs[0].upload.onprogress({ lengthComputable: true, loaded: 1, total: 2 });
  page.xhrs[1].upload.onprogress({ lengthComputable: true, loaded: 1, total: 2 });
  const latest = page.labels[page.labels.length - 1];
  assert.match(latest, /正在上載/);
  assert.match(latest, /2\.0 MB/);
  assert.match(latest, /8\.0 MB/);
  assert.equal(page.widths[page.widths.length - 1], "25%");
  for (let turn = 0; turn < 8; turn += 1) {
    page.xhrs.filter((xhr) => !xhr.done).forEach((xhr) => xhr.finish());
    await new Promise((resolve) => setImmediate(resolve));
    if (page.assigned.length) break;
  }
  assert.deepEqual(
    page.xhrs.map((xhr) => xhr.body.entries.index),
    ["0", "1", "2", "3"]
  );
  const fourthSend = page.events
    .map((event, index) => (event === "send" ? index : -1))
    .filter((index) => index >= 0)[3];
  const loadsBeforeFourth = page.events.slice(0, fourthSend).filter((event) => event === "load").length;
  assert.ok(loadsBeforeFourth >= 1);
  assert.equal(page.assigned[0], "/submission/sub1?notice=submitted");
});

test("an older 8MB session is not resumed, so the next pieces stay at 2MB", async () => {
  const file = fakeFile("WhatsApp Chat - sample.zip", 5 * 1024 * 1024);
  const signature = upload.fileSignature([file]);
  const page = mount({
    file: file,
    saved: {
      [upload.SESSION_KEY]: JSON.stringify({ id: "old8", signature: signature }),
    },
    uploadId: "new2",
    status: { id: "old8", chunk_bytes: 8 * 1024 * 1024, files: [{ received: [0] }] },
    serverChunkBytes: CHUNK,
    xhr: "ok",
  });
  await page.submit();
  assert.equal(page.fetches[0].url, "/upload/old8");
  assert.ok(page.fetches.some((call) => call.url === "/upload/start"));
  assert.deepEqual(
    page.xhrs.map((xhr) => xhr.body.entries.index),
    ["0", "1", "2"]
  );
  page.xhrs.forEach((xhr) => {
    assert.equal(xhr.url, "/upload/new2/chunk");
    assert.ok(xhr.body.entries.chunk.size <= CHUNK);
  });
  assert.equal(page.storage.getItem(upload.SESSION_KEY), null);
});
