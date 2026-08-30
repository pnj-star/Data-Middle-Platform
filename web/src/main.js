// Wiki frontend entry (ROADMAP D5/D18): hash-routed, vanilla JS modules.
import "./styles.css";
import api, { clearToken, getApiKey, getToken, setApiKey } from "./api.js";
import { esc } from "./util.js";
import { renderTree } from "./views/tree.js";
import { renderEditor } from "./views/editor.js";
import { renderSearch } from "./views/search.js";
import { renderAdmin } from "./views/admin.js";
import { renderLogin } from "./views/login.js";
import { renderHistory } from "./views/history.js";
import { renderTrash } from "./views/trash.js";

// 每个路由的页面标题（eyebrow + 标题），渲染在共享 page-header 里
const PAGE_META = {
  tree: { eyebrow: "KNOWLEDGE BASE", title: "页面树" },
  search: { eyebrow: "KNOWLEDGE BASE", title: "搜索" },
  trash: { eyebrow: "KNOWLEDGE BASE", title: "回收站" },
  admin: { eyebrow: "IMPORT", title: "从文件导入" },
  login: { eyebrow: "ACCOUNT", title: "登录" },
  register: { eyebrow: "ACCOUNT", title: "注册" },
  editor: { eyebrow: "PAGE", title: "编辑页面" },
  history: { eyebrow: "PAGE", title: "历史记录" },
};

function routeKey(hash) {
  if (hash.startsWith("#/editor")) return "editor";
  if (hash.startsWith("#/history/")) return "history";
  if (hash.startsWith("#/login")) return "login";
  if (hash.startsWith("#/register")) return "register";
  if (hash.startsWith("#/tree")) return "tree";
  if (hash.startsWith("#/search")) return "search";
  if (hash.startsWith("#/trash")) return "trash";
  if (hash.startsWith("#/admin")) return "admin";
  return "tree";
}

function sidebar() {
  const hash = location.hash || "#/tree";
  const key = routeKey(hash);
  const active = (k) => (k === key ? " sidebar__nav-item--active" : "");
  return `
    <aside class="sidebar">
      <div class="sidebar__top">
        <div class="sidebar__brand">
          <span class="sidebar__mark">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
          </span>
          <span>Wiki</span>
        </div>
        <div class="sidebar__tag">KNOWLEDGE BASE</div>
        <nav class="sidebar__nav">
          <a class="sidebar__nav-item${active("tree")}" href="#/tree">
            <svg class="sidebar__nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>
            页面树
          </a>
          <a class="sidebar__nav-item${active("search")}" href="#/search">
            <svg class="sidebar__nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            搜索
          </a>
          <a class="sidebar__nav-item${active("trash")}" href="#/trash">
            <svg class="sidebar__nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            回收站
          </a>
          <a class="sidebar__nav-item${active("admin")}" href="#/admin">
            <svg class="sidebar__nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8"/><path d="M12 11v4"/><path d="M12 7h.01"/></svg>
            从文件导入
          </a>
          <a class="sidebar__nav-item" href="/">
            <svg class="sidebar__nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>
            文档流水线 ↗
          </a>
        </nav>
      </div>
      <div class="sidebar__bottom">
        <div class="sidebar__label">API Key</div>
        <input class="key-input" id="apiKeyInput" type="password"
          placeholder="粘贴 X-API-Key（服务账号，可选）" value="${getApiKey()}"
          title="粘贴与后端一致的 X-API-Key（服务账号，可选）">
        <div class="sidebar__status" id="authArea"></div>
      </div>
    </aside>
  `;
}

async function refreshAuth() {
  const area = document.getElementById("authArea");
  if (!area) return;
  if (getToken()) {
    try {
      const me = await api.get("/api/wiki/auth/me");
      area.innerHTML =
        `<span class="muted" style="margin-right:8px;">${esc(me.username)}</span>` +
        `<button class="btn btn-sm" onclick="window.__logout()">登出</button>`;
    } catch {
      clearToken();
      area.innerHTML = `<a href="#/login" class="auth-tab">登录</a>`;
    }
  } else {
    area.innerHTML = `<a href="#/login" class="auth-tab">登录</a>`;
  }
}

window.__logout = () => {
  clearToken();
  location.hash = "#/login";
};

function render() {
  const hash = location.hash || "#/tree";
  let body;
  if (hash.startsWith("#/editor")) {
    const id = hash.split("/")[2] || "new";
    body = renderEditor(id);
  } else if (hash.startsWith("#/history/")) {
    body = renderHistory(hash.split("/")[2]);
  } else if (hash === "#/login" || hash === "#/register") {
    body = renderLogin();
  } else if (hash.startsWith("#/tree")) {
    body = renderTree();
  } else if (hash.startsWith("#/search")) {
    body = renderSearch();
  } else if (hash.startsWith("#/trash")) {
    body = renderTrash();
  } else if (hash.startsWith("#/admin")) {
    body = renderAdmin();
  } else {
    location.hash = "#/tree";
    return;
  }

  const meta = PAGE_META[routeKey(hash)];
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="app-shell">
      ${sidebar()}
      <main class="app-main">
        <header class="page-header">
          <div>
            <span class="eyebrow">${meta.eyebrow}</span>
            <h1 class="page-header__title">${meta.title}</h1>
          </div>
        </header>
        <div id="view">${body}</div>
      </main>
    </div>
  `;

  const keyInput = document.getElementById("apiKeyInput");
  if (keyInput) keyInput.addEventListener("change", (e) => setApiKey(e.target.value.trim()));

  window.dispatchEvent(new CustomEvent("wiki:rendered", { detail: hash }));
  refreshAuth();
}

window.addEventListener("hashchange", render);
render();
