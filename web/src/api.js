// API wrapper for the wiki frontend (ROADMAP P1-T4/T8, Phase 2).
// Supports both credential paths: the legacy X-API-Key (superuser / service
// account) and a JWT Bearer token (logged-in wiki user). Both live in
// localStorage.
let _key = localStorage.getItem("wiki_api_key") || "";
let _token = localStorage.getItem("wiki_token") || "";

async function _fetch(url, opts) {
  const headers = { ...(opts.headers || {}) };
  if (_key) headers["X-API-Key"] = _key;
  if (_token) headers["Authorization"] = "Bearer " + _token;
  const resp = await fetch(url, { ...opts, headers });
  if (resp.status === 401) {
    clearToken(); // expired / invalid token — require re-login
  }
  if (!resp.ok) {
    let detail = "";
    try { detail = (await resp.json()).detail || ""; } catch { /* not json */ }
    throw new Error(detail || `${resp.status} ${resp.statusText}`);
  }
  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

const api = {
  get: (url) => _fetch(url, { method: "GET" }),
  post: (url, data) =>
    _fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }),
  put: (url, data) =>
    _fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }),
  del: (url) => _fetch(url, { method: "DELETE" }),
  // multipart upload (attachment); browser sets the boundary Content-Type
  upload: (url, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return _fetch(url, { method: "POST", body: fd });
  },
};

export function setApiKey(k) {
  _key = k;
  localStorage.setItem("wiki_api_key", k);
}
export function getApiKey() {
  return _key;
}
export function setToken(t) {
  _token = t || "";
  if (t) localStorage.setItem("wiki_token", t);
  else localStorage.removeItem("wiki_token");
}
export function clearToken() {
  _token = "";
  localStorage.removeItem("wiki_token");
}
export function getToken() {
  return _token;
}

export default api;
