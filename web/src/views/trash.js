// Trash / recycle bin view (ROADMAP P3-T2).
import api from "../api.js";
import { esc, toast } from "../util.js";

export function renderTrash() {
  return '<div class="card" id="trashRoot"><div class="card-header">回收站</div><div class="muted">加载中…</div></div>';
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/trash")) loadTrash();
});

async function loadTrash() {
  const root = document.getElementById("trashRoot");
  if (!root) return;
  try {
    const items = await api.get("/api/wiki/trash?limit=100");
    if (!items.length) {
      root.innerHTML =
        '<div class="card-header">回收站</div><div class="muted">回收站为空。</div>';
      return;
    }
    root.innerHTML =
      `<div class="card-header">回收站（${items.length}）</div>` +
      items
        .map(
          (t) => `
      <div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);">
        <span style="flex:1;">${esc(t.title)}</span>
        <span class="muted">${esc(new Date(t.deleted_at).toLocaleString())}</span>
        <button class="btn btn-sm" onclick="window.__restore('${esc(t.id)}')">恢复</button>
        <button class="btn btn-sm btn-danger" onclick="window.__purge('${esc(t.id)}')">彻底删除</button>
      </div>`
        )
        .join("");
  } catch (err) {
    root.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

window.__restore = async (id) => {
  try {
    await api.post(`/api/wiki/trash/${id}/restore`);
    toast("已恢复");
    loadTrash();
  } catch (e) {
    toast(e.message, true);
  }
};

window.__purge = async (id) => {
  if (!confirm("彻底删除后不可恢复，确定吗？")) return;
  try {
    await api.del(`/api/wiki/trash/${id}`);
    toast("已彻底删除");
    loadTrash();
  } catch (e) {
    toast(e.message, true);
  }
};
