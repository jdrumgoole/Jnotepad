// Jnotepad front end. Vanilla JS, no build step.
// All user content is inserted with textContent (never innerHTML).
"use strict";

const TOKEN_KEY = "jnotepad.token";
const PAGE = 50;
const $ = (sel, root = document) => root.querySelector(sel);

const state = {
  token: null,
  user: null,
  offset: 0,
  total: 0,
  pendingFiles: [],   // images queued in the capture box
  blobUrls: new Map(), // image id -> object URL cache
  editing: null,
};

// ---------- storage ----------
function loadToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
function saveToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

// ---------- API ----------
class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(d => d.msg?.replace(/^Value error, /, "") ?? JSON.stringify(d)).join("; ");
  return JSON.stringify(detail);
}

async function api(path, { method = "GET", json, form, auth = true } = {}) {
  const headers = {};
  if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form;
  const res = await fetch(path, { method, headers, body });
  if (res.status === 401 && auth) { logoutLocal(); throw new ApiError(401, "Session expired — please log in again."); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = detailText((await res.json()).detail); } catch { /* not json */ }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.blob();
}

// ---------- auth view ----------
let registering = false;

function showAuth() {
  $("#auth-view").hidden = false;
  $("#app-view").hidden = true;
  $("#userbox").hidden = true;
}

function setAuthMode(reg) {
  registering = reg;
  $("#auth-title").textContent = reg ? "Create account" : "Log in";
  $("#auth-submit").textContent = reg ? "Register" : "Log in";
  $("#auth-toggle").textContent = reg ? "Have an account? Log in" : "No account? Register";
  $("#auth-form").password.autocomplete = reg ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
}

async function onAuthSubmit(ev) {
  ev.preventDefault();
  const f = ev.target;
  const creds = { username: f.username.value.trim(), password: f.password.value };
  $("#auth-error").textContent = "";
  $("#auth-submit").disabled = true;
  try {
    if (registering) await api("/api/session/register", { method: "POST", json: creds, auth: false });
    const tok = await api("/api/session/login", { method: "POST", json: creds, auth: false });
    state.token = tok.access_token;
    saveToken(state.token);
    f.reset();
    await startApp();
  } catch (e) {
    $("#auth-error").textContent = e.message;
  } finally {
    $("#auth-submit").disabled = false;
  }
}

function logoutLocal() {
  state.token = null; state.user = null;
  saveToken(null);
  for (const u of state.blobUrls.values()) URL.revokeObjectURL(u);
  state.blobUrls.clear();
  $("#items").replaceChildren();
  showAuth();
}

async function logout() {
  try { await api("/api/session/logout", { method: "POST" }); } catch { /* already invalid */ }
  logoutLocal();
}

// ---------- app ----------
async function startApp() {
  state.user = await api("/api/session/me");
  $("#whoami").textContent = state.user.username;
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#userbox").hidden = false;
  await Promise.all([refresh(), refreshTags()]);
  $("#cap-text").focus();
}

function parseTags(s) {
  return s.split(/[,\s]+/).map(t => t.replace(/^#/, "").trim()).filter(Boolean);
}

const URL_ONLY = /^https?:\/\/\S+$/i;

// ---------- capture ----------
function queueFiles(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    state.pendingFiles.push(f);
  }
  renderPending();
}

function renderPending() {
  const box = $("#cap-previews");
  box.replaceChildren();
  state.pendingFiles.forEach((f, i) => {
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.onload = () => URL.revokeObjectURL(img.src);
    img.alt = f.name;
    const x = document.createElement("button");
    x.className = "thumb-x"; x.textContent = "×"; x.title = "Remove";
    x.onclick = () => { state.pendingFiles.splice(i, 1); renderPending(); };
    wrap.append(img, x);
    box.append(wrap);
  });
}

async function uploadImages(itemId, files) {
  let item = null;
  for (const f of files) {
    const form = new FormData();
    form.append("file", f, f.name || "pasted.png");
    item = await api(`/api/items/${itemId}/images`, { method: "POST", form });
  }
  return item;
}

async function saveCapture() {
  let title = $("#cap-title").value.trim();
  let text = $("#cap-text").value.trim();
  let url = $("#cap-url").value.trim();
  const tags = parseTags($("#cap-tags").value);
  // A bare URL typed into the note box is a link capture.
  if (!url && URL_ONLY.test(text)) { url = text; text = ""; }
  if (!title && !text && !url && state.pendingFiles.length === 0) {
    status("Nothing to save."); return;
  }
  $("#cap-save").disabled = true;
  status("Saving…");
  try {
    const item = await api("/api/items", { method: "POST", json: { title, text, url: url || null, tags } });
    if (state.pendingFiles.length) {
      try { await uploadImages(item.id, state.pendingFiles); }
      catch (e) { status(`Saved, but an image failed: ${e.message}`); }
    }
    for (const id of ["#cap-title", "#cap-text", "#cap-url", "#cap-tags"]) $(id).value = "";
    state.pendingFiles = []; renderPending();
    if (!$("#cap-status").textContent.startsWith("Saved, but")) status("Saved.");
    await Promise.all([refresh(), refreshTags()]);
    $("#cap-text").focus();
  } catch (e) {
    status(e.message);
  } finally {
    $("#cap-save").disabled = false;
  }
}

let statusTimer;
function status(msg) {
  $("#cap-status").textContent = msg;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { $("#cap-status").textContent = ""; }, 5000);
}

// ---------- list ----------
function query(offset) {
  const p = new URLSearchParams({ limit: PAGE, offset });
  const q = $("#f-q").value.trim(); if (q) p.set("q", q);
  const k = $("#f-kind").value; if (k) p.set("kind", k);
  const t = $("#f-tag").value; if (t) p.set("tag", t);
  return `/api/items?${p}`;
}

async function refresh() {
  state.offset = 0;
  const page = await api(query(0));
  $("#items").replaceChildren(...page.items.map(renderItem));
  state.offset = page.items.length; state.total = page.total;
  updateSummary();
}

async function loadMore() {
  const page = await api(query(state.offset));
  $("#items").append(...page.items.map(renderItem));
  state.offset += page.items.length; state.total = page.total;
  updateSummary();
}

function updateSummary() {
  $("#list-summary").textContent = state.total === 0 ? "Nothing captured yet." :
    `${state.total} item${state.total === 1 ? "" : "s"}`;
  $("#more").hidden = state.offset >= state.total;
}

async function refreshTags() {
  const tags = await api("/api/tags");
  const sel = $("#f-tag"), cur = sel.value;
  sel.replaceChildren(new Option("All tags", ""), ...tags.map(t => new Option(`#${t}`, t)));
  sel.value = tags.includes(cur) ? cur : "";
}

async function imageUrl(id) {
  if (state.blobUrls.has(id)) return state.blobUrls.get(id);
  const blob = await api(`/api/images/${id}`);
  const u = URL.createObjectURL(blob);
  state.blobUrls.set(id, u);
  return u;
}

function renderImages(box, item, { removable = false } = {}) {
  box.replaceChildren();
  for (const meta of item.images) {
    const wrap = document.createElement("div");
    wrap.className = "thumb";
    const img = document.createElement("img");
    img.alt = meta.filename;
    img.loading = "lazy";
    imageUrl(meta.id).then(u => { img.src = u; }).catch(() => { img.alt = "(failed to load)"; });
    img.onclick = () => openLightbox(meta.id);
    wrap.append(img);
    if (removable) {
      const x = document.createElement("button");
      x.type = "button"; x.className = "thumb-x"; x.textContent = "×"; x.title = "Remove image";
      x.onclick = async () => {
        if (!confirm("Remove this image?")) return;
        const updated = await api(`/api/items/${item.id}/images/${meta.id}`, { method: "DELETE" });
        state.editing = updated;
        renderImages(box, updated, { removable: true });
        replaceCard(updated);
      };
      wrap.append(x);
    }
    box.append(wrap);
  }
}

async function openLightbox(id) {
  const dlg = $("#lightbox");
  $("img", dlg).src = await imageUrl(id);
  dlg.showModal();
}

function renderItem(item) {
  const node = $("#item-tpl").content.firstElementChild.cloneNode(true);
  node.dataset.id = item.id;
  $(".badge", node).textContent = item.kind;
  $(".badge", node).classList.add(`k-${item.kind}`);
  $(".item-title", node).textContent = item.title || (item.url ? hostOf(item.url) : "");
  const t = $("time", node);
  t.dateTime = item.updated_at;
  t.textContent = new Date(item.updated_at).toLocaleString();
  const a = $(".item-url", node);
  if (item.url) { a.href = item.url; a.textContent = item.url; } else a.remove();
  const p = $(".item-text", node);
  if (item.text) p.textContent = item.text; else p.remove();
  renderImages($(".item-images", node), item);
  const tags = $(".item-tags", node);
  for (const tag of item.tags) {
    const b = document.createElement("button");
    b.className = "tag"; b.textContent = `#${tag}`;
    b.onclick = () => { $("#f-tag").value = tag; refresh(); };
    tags.append(b);
  }
  $(".act-edit", node).onclick = () => openEdit(item.id);
  $(".act-delete", node).onclick = async () => {
    if (!confirm("Delete this item?")) return;
    await api(`/api/items/${item.id}`, { method: "DELETE" });
    for (const m of item.images) { URL.revokeObjectURL(state.blobUrls.get(m.id)); state.blobUrls.delete(m.id); }
    node.remove(); state.total -= 1; state.offset -= 1; updateSummary(); refreshTags();
  };
  $(".act-addimg", node).onchange = async (ev) => {
    try { replaceCard(await uploadImages(item.id, [...ev.target.files])); }
    catch (e) { alert(e.message); }
  };
  return node;
}

function replaceCard(item) {
  const old = document.querySelector(`.item[data-id="${CSS.escape(item.id)}"]`);
  if (old) old.replaceWith(renderItem(item));
}

function hostOf(u) {
  try { return new URL(u).host; } catch { return u; }
}

// ---------- edit ----------
async function openEdit(id) {
  const item = await api(`/api/items/${id}`);
  state.editing = item;
  const f = $("#edit-form");
  f.title.value = item.title; f.text.value = item.text;
  f.url.value = item.url || ""; f.tags.value = item.tags.join(", ");
  $("#edit-error").textContent = "";
  renderImages($("#edit-images"), item, { removable: true });
  $("#edit-dialog").showModal();
}

async function onEditClose() {
  const dlg = $("#edit-dialog");
  if (dlg.returnValue !== "save" || !state.editing) return;
  const f = $("#edit-form");
  try {
    const updated = await api(`/api/items/${state.editing.id}`, {
      method: "PATCH",
      json: { title: f.title.value.trim(), text: f.text.value, url: f.url.value.trim() || null,
              tags: parseTags(f.tags.value) },
    });
    replaceCard(updated);
    refreshTags();
  } catch (e) {
    $("#edit-error").textContent = e.message;
    dlg.showModal();
  }
}

// ---------- password ----------
async function onPwClose() {
  const dlg = $("#pw-dialog");
  const f = $("#pw-form");
  if (dlg.returnValue !== "save") { f.reset(); return; }
  try {
    await api("/api/session/password", { method: "POST", json: {
      current_password: f.current_password.value, new_password: f.new_password.value } });
    f.reset();
    alert("Password changed. Please log in again.");
    logoutLocal();
  } catch (e) {
    if (e.status === 401) return;
    $("#pw-error").textContent = e.message;
    dlg.showModal();
  }
}

// ---------- wiring ----------
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function wire() {
  $("#auth-form").addEventListener("submit", onAuthSubmit);
  $("#auth-toggle").onclick = () => setAuthMode(!registering);
  $("#btn-logout").onclick = logout;
  $("#btn-password").onclick = () => { $("#pw-error").textContent = ""; $("#pw-dialog").showModal(); };
  $("#pw-dialog").addEventListener("close", onPwClose);
  $("#edit-dialog").addEventListener("close", onEditClose);

  $("#cap-save").onclick = saveCapture;
  $("#cap-file").onchange = (ev) => { queueFiles(ev.target.files); ev.target.value = ""; };
  const cap = $("#capture");
  cap.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); saveCapture(); }
  });
  // Paste an image from the clipboard anywhere in the capture box.
  cap.addEventListener("paste", (ev) => {
    const files = [...(ev.clipboardData?.files || [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { ev.preventDefault(); queueFiles(files); }
  });
  cap.addEventListener("dragover", (ev) => { ev.preventDefault(); cap.classList.add("drop"); });
  cap.addEventListener("dragleave", () => cap.classList.remove("drop"));
  cap.addEventListener("drop", (ev) => {
    ev.preventDefault(); cap.classList.remove("drop");
    const files = [...(ev.dataTransfer?.files || [])];
    if (files.length) { queueFiles(files); return; }
    const uri = ev.dataTransfer?.getData("text/uri-list") || ev.dataTransfer?.getData("text/plain");
    if (uri && URL_ONLY.test(uri.trim())) $("#cap-url").value = uri.trim();
  });

  const reload = debounce(() => refresh().catch(e => status(e.message)), 250);
  $("#f-q").addEventListener("input", reload);
  $("#f-kind").addEventListener("change", reload);
  $("#f-tag").addEventListener("change", reload);
  $("#more").onclick = () => loadMore().catch(e => status(e.message));
}

async function boot() {
  wire();
  setAuthMode(false);
  state.token = loadToken();
  if (!state.token) { showAuth(); return; }
  try { await startApp(); } catch { logoutLocal(); }
}

document.addEventListener("DOMContentLoaded", boot);
