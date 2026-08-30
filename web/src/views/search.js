// Search view (ROADMAP P1-T7): semantic search over published pages,
// results link back to the page editor (溯源, D3).
import api from "../api.js";
import { esc, toast } from "../util.js";

export function renderSearch() {
  return `
    <div class="card">
      <div class="form-row">
        <select id="searchSpace"><option value="">全部空间</option></select>
        <input id="searchInput" type="text" placeholder="搜索已发布页面…（语义检索）" />
        <button class="btn btn-primary" id="searchBtn">搜索</button>
      </div>
    </div>
    <div class="card" id="searchResults">
      <div class="muted">输入问题或关键词，检索已发布的页面内容。</div>
    </div>`;
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/search")) bindSearch();
});

async function bindSearch() {
  const input = document.getElementById("searchInput");
  const btn = document.getElementById("searchBtn");
  const results = document.getElementById("searchResults");
  const spaceSel = document.getElementById("searchSpace");

  // populate the space filter (multi-space comes with Phase 2 ACL)
  try {
    const spaces = await api.get("/api/wiki/spaces");
    if (spaces.length) {
      spaceSel.innerHTML =
        '<option value="">全部空间</option>' +
        spaces.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
    }
  } catch (e) { /* wiki not reachable */ }

  const doSearch = async () => {
    const q = input.value.trim();
    if (!q) return toast("请输入搜索内容", true);
    const spaceId = spaceSel.value;
    const url =
      `/api/wiki/search?q=${encodeURIComponent(q)}` +
      (spaceId ? `&space_id=${encodeURIComponent(spaceId)}` : "");
    btn.disabled = true;
    try {
      const body = await api.get(url);
      if (!body.results.length) {
        results.innerHTML = '<div class="muted">没有命中。</div>';
        return;
      }
      results.innerHTML =
        body.results
          .map((r) => {
            // trace-back anchor (P3-T6): point at the parent section or the
            // chunk content so the editor can highlight it on open
            const hl = encodeURIComponent(r.parent_title || r.content.slice(0, 40));
            return `
      <div style="padding:8px 0;border-bottom:1px solid var(--border);">
        <a class="page-link" href="#/editor/${esc(r.page_id)}?hl=${hl}">${esc(r.page_title)}</a>
        <span class="muted"> · rev ${r.revision_id}${r.parent_title ? " · " + esc(r.parent_title) : ""}</span>
        <div class="muted" style="margin-top:4px;">${esc(snippet(r.content, q))}</div>
      </div>`;
          })
          .join("");
    } catch (err) {
      toast(err.message, true);
    }
    btn.disabled = false;
  };

  btn.onclick = doSearch;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
}

function snippet(text, q, n = 120) {
  const t = text || "";
  const i = t.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return t.slice(0, n);
  const start = Math.max(0, i - 30);
  return (start > 0 ? "…" : "") + t.slice(start, start + n);
}
