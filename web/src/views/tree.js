// Page tree view (ROADMAP P1-T4/T8): spaces → nested page tree.
import api from "../api.js";
import { esc } from "../util.js";

let _spaces = [];

export function renderTree() {
  return '<div class="card" id="treeRoot"><div class="card-header">页面树</div><div class="muted">加载中…</div></div>';
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/tree")) loadTree();
});

async function loadTree() {
  const root = document.getElementById("treeRoot");
  if (!root) return;
  try {
    _spaces = await api.get("/api/wiki/spaces");
    if (!_spaces.length) {
      root.innerHTML = '<div class="muted">还没有空间，请先在入库后台或 API 创建。</div>';
      return;
    }
    const space = _spaces[0];
    const pages = await api.get(`/api/wiki/pages?space_id=${space.id}`);
    root.innerHTML = `
      <div class="card-header">
        空间：${esc(space.name)}
        <button class="btn btn-primary" style="float:right"
          onclick="location.hash='#/editor/new?space=${space.id}'">+ 新建页面</button>
      </div>
      <div class="tree">${renderNodes(pages)}</div>
    `;
  } catch (err) {
    root.innerHTML = `<div class="muted">加载失败：${esc(err.message)}</div>`;
  }
}

function renderNodes(nodes) {
  if (!nodes.length) return '<div class="muted">暂无页面</div>';
  return (
    "<ul>" +
    nodes
      .map(
        (n) => `
      <li>
        <a class="page-link" href="#/editor/${esc(n.id)}">${esc(n.title)}</a>
        <span class="page-status">${esc(n.status)}${n.has_current_revision ? "" : " · 未发布"}</span>
        ${n.children.length ? renderNodes(n.children) : ""}
      </li>`
      )
      .join("") +
    "</ul>"
  );
}
