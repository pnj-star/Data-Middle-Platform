// Revision history + diff view (ROADMAP P3-T1).
import api from "../api.js";
import { esc } from "../util.js";

let _pageId = null;

export function renderHistory(id) {
  _pageId = id;
  return `
    <div class="card">
      <div class="card-header">
        版本历史
        <button class="btn" style="float:right" onclick="location.hash='#/editor/${esc(id)}'">返回编辑</button>
      </div>
      <div class="form-row" style="margin-bottom:12px;">
        <span class="muted">对比</span>
        <select id="diffFrom" style="flex:1;max-width:220px;"></select>
        <span class="muted">→</span>
        <select id="diffTo" style="flex:1;max-width:220px;"></select>
        <button class="btn btn-primary" id="diffBtn">查看 Diff</button>
      </div>
      <div id="revisionList" class="muted">加载中…</div>
      <div id="diffView" style="margin-top:12px;"></div>
    </div>`;
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/history/")) bindHistory();
});

async function bindHistory() {
  const revs = await api.get(`/api/wiki/pages/${_pageId}/revisions?limit=100`);
  const fromSel = document.getElementById("diffFrom");
  const toSel = document.getElementById("diffTo");
  const opts = revs
    .map((r) => `<option value="${r.revision_id}">#${r.revision_id}${r.note ? " · " + esc(r.note) : ""}</option>`)
    .join("");
  fromSel.innerHTML = opts;
  toSel.innerHTML = opts;
  toSel.value = revs.length ? revs[0].revision_id : "";
  fromSel.value = revs.length > 1 ? revs[1].revision_id : revs[0].revision_id;
  document.getElementById("revisionList").innerHTML =
    `共 ${revs.length} 个修订${revs.length ? `，最新 #${revs[0].revision_id}` : ""}`;

  document.getElementById("diffBtn").onclick = async () => {
    const a = fromSel.value;
    const b = toSel.value;
    if (!a || !b) return;
    try {
      const body = await api.get(`/api/wiki/pages/${_pageId}/revisions/diff?from_rev=${a}&to_rev=${b}`);
      renderDiff(document.getElementById("diffView"), body.lines);
    } catch (err) {
      document.getElementById("diffView").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
    }
  };
}

function renderDiff(el, lines) {
  if (!lines.length) {
    el.innerHTML = '<div class="muted">内容相同，无差异。</div>';
    return;
  }
  el.innerHTML =
    '<div class="diff">' +
    lines
      .map((l) => {
        const cls = l.op === "delete" ? "diff-del" : l.op === "insert" ? "diff-add" : "diff-eq";
        const sign = l.op === "delete" ? "−" : l.op === "insert" ? "+" : " ";
        return `<div class="${cls}"><span class="diff-sign">${sign}</span><pre>${esc(l.text)}</pre></div>`;
      })
      .join("") +
    "</div>";
}
