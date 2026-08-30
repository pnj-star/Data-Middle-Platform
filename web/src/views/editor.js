// Editor view (ROADMAP P1-T4): textarea + marked preview + DOMPurify.
// Saving PUTs the page → backend creates an incremented revision (P1-T2).
import api from "../api.js";
import { esc, toast } from "../util.js";
import DOMPurify from "dompurify";
import { marked } from "marked";

let _ctx = { id: null, space_id: "", parent_page_id: null };

export function renderEditor(id) {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  _ctx = {
    id: id === "new" ? null : id,
    space_id: q.get("space") || "",
    parent_page_id: q.get("parent") || null,
  };
  return `
    <div class="card">
      <input class="editor-title" id="editorTitle" placeholder="页面标题" />
      <div class="editor">
        <textarea id="editorContent" placeholder="Markdown 内容…"></textarea>
        <div class="preview" id="editorPreview"><div class="muted">预览</div></div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center;">
        <button class="btn btn-primary" id="editorSave">保存</button>
        <button class="btn btn-primary" id="editorPublish" style="display:none;">发布</button>
        <button class="btn" id="editorHistory" style="display:none;">历史</button>
        <button class="btn" onclick="location.hash='#/tree'">返回</button>
        <span class="muted" id="editorMeta"></span>
      </div>
      <div id="attachmentPanel" style="margin-top:16px;display:none;">
        <div class="card-header" style="margin-bottom:8px;">附件</div>
        <div id="attachmentList"></div>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <input type="file" id="attachmentInput" style="display:none" />
          <button class="btn btn-sm" id="attachmentUpload">上传附件</button>
        </div>
      </div>
      <div id="linksPanel" style="margin-top:16px;display:none;">
        <div class="card-header" style="margin-bottom:8px;">链接</div>
        <div id="linksView"></div>
      </div>
      <div id="commentsPanel" style="margin-top:16px;display:none;">
        <div class="card-header" style="margin-bottom:8px;">评论</div>
        <div id="commentList"></div>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <input type="text" id="commentInput" placeholder="写下评论…"
            style="flex:1;padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius);" />
          <button class="btn btn-sm btn-primary" id="commentSubmit">发表</button>
        </div>
      </div>
    </div>`;
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/editor")) bindEditor();
});

async function bindEditor() {
  const titleEl = document.getElementById("editorTitle");
  const contentEl = document.getElementById("editorContent");
  const previewEl = document.getElementById("editorPreview");
  const metaEl = document.getElementById("editorMeta");
  const saveBtn = document.getElementById("editorSave");
  const publishBtn = document.getElementById("editorPublish");
  const historyBtn = document.getElementById("editorHistory");

  const updatePreview = () => {
    let html;
    try { html = marked.parse(contentEl.value || ""); } catch { html = ""; }
    previewEl.innerHTML = DOMPurify.sanitize(html);
  };
  contentEl.addEventListener("input", updatePreview);

  if (_ctx.id) {
    try {
      const page = await api.get(`/api/wiki/pages/${_ctx.id}`);
      titleEl.value = page.title;
      contentEl.value = page.content;
      _ctx.space_id = page.space_id;
      const revs = await api.get(`/api/wiki/pages/${_ctx.id}/revisions?limit=1`);
      const revNo = revs.length ? `#${revs[0].revision_id}` : "";
      metaEl.textContent = `${revNo} · ${page.status}`;
      publishBtn.style.display = "";
      publishBtn.onclick = () => doPublish(metaEl);
      historyBtn.style.display = "";
      historyBtn.onclick = () => (location.hash = `#/history/${_ctx.id}`);
      loadAttachments();
      loadLinks();
      loadComments();
    } catch (err) {
      metaEl.textContent = "加载失败：" + err.message;
    }
    updatePreview();
    highlightTarget();
  } else {
    titleEl.value = "";
    contentEl.value = "";
    metaEl.textContent = _ctx.space_id ? "新建草稿" : "新建页面（需要空间，从页面树进入）";
    updatePreview();
  }

  saveBtn.onclick = async () => {
    const title = titleEl.value.trim();
    const content = contentEl.value;
    if (!title) return toast("标题不能为空", true);
    saveBtn.disabled = true;
    try {
      if (_ctx.id) {
        await api.put(`/api/wiki/pages/${_ctx.id}`, { content });
        toast("已保存（产生新修订）");
      } else {
        if (!_ctx.space_id) return toast("缺少目标空间", true);
        const page = await api.post("/api/wiki/pages", {
          space_id: _ctx.space_id,
          parent_page_id: _ctx.parent_page_id,
          title,
          content,
        });
        location.hash = `#/editor/${page.id}`;
        toast("页面已创建");
      }
    } catch (err) {
      toast(esc(err.message), true);
    }
    saveBtn.disabled = false;
  };
}

async function doPublish(metaEl) {
  try {
    const r = await api.post(`/api/wiki/pages/${_ctx.id}/publish`);
    metaEl.textContent = "发布中…";
    toast("已提交发布");
    pollPublish(r.task_id, metaEl);
  } catch (err) {
    toast(esc(err.message), true);
  }
}

async function pollPublish(taskId, metaEl) {
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const st = await api.get(`/api/tasks/${taskId}`);
      if (st.state === "SUCCESS") {
        metaEl.textContent = "已发布";
        toast("发布成功");
        return;
      }
      if (st.state === "FAILURE") {
        metaEl.textContent = "发布失败";
        toast("发布失败，请重试", true);
        return;
      }
    } catch (e) { /* keep polling */ }
  }
  metaEl.textContent = "发布中…请稍后刷新查看状态";
}

// ── Attachments (ROADMAP P3-T3) ───────────────────────────────────

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

async function loadAttachments() {
  const panel = document.getElementById("attachmentPanel");
  const list = document.getElementById("attachmentList");
  if (!_ctx.id || !panel || !list) return;
  try {
    const atts = await api.get(`/api/wiki/pages/${_ctx.id}/attachments`);
    panel.style.display = "";
    list.innerHTML = atts.length
      ? atts
          .map(
            (a) => `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;">
        <span style="flex:1;">${esc(a.original_name)} <span class="muted">(${fmtSize(a.size)})</span></span>
        <a class="btn btn-sm" href="/api/wiki/attachments/${esc(a.id)}/download" download>下载</a>
        <button class="btn btn-sm btn-danger" onclick="window.__delAttachment('${esc(a.id)}')">删除</button>
      </div>`
          )
          .join("")
      : '<div class="muted">暂无附件</div>';

    const input = document.getElementById("attachmentInput");
    const upBtn = document.getElementById("attachmentUpload");
    upBtn.onclick = () => input.click();
    input.onchange = async () => {
      if (!input.files.length) return;
      try {
        await api.upload(`/api/wiki/pages/${_ctx.id}/attachments`, input.files[0]);
        toast("附件已上传");
        input.value = "";
        loadAttachments();
      } catch (err) {
        toast(esc(err.message), true);
      }
    };
  } catch (e) {
    panel.style.display = "none"; // read-only / not permitted
  }
}

window.__delAttachment = async (id) => {
  try {
    await api.del(`/api/wiki/attachments/${id}`);
    toast("已删除");
    loadAttachments();
  } catch (e) {
    toast(e.message, true);
  }
};

// ── Links / backlinks (ROADMAP P3-T4) ─────────────────────────────

async function loadLinks() {
  const panel = document.getElementById("linksPanel");
  const view = document.getElementById("linksView");
  if (!_ctx.id || !panel || !view) return;
  try {
    const [links, backlinks] = await Promise.all([
      api.get(`/api/wiki/pages/${_ctx.id}/links`),
      api.get(`/api/wiki/pages/${_ctx.id}/backlinks`),
    ]);
    panel.style.display = "";
    const back = backlinks.length
      ? backlinks
          .map((x) => `<div><a class="page-link" href="#/editor/${esc(x.page_id)}">${esc(x.title)}</a></div>`)
          .join("")
      : '<div class="muted">无</div>';
    const out = links.length
      ? links
          .map((l) =>
            l.target_page_id
              ? `<div><a class="page-link" href="#/editor/${esc(l.target_page_id)}">${esc(l.target_title)}</a></div>`
              : `<div class="muted">${esc(l.target_title)}（页面未创建）</div>`
          )
          .join("")
      : '<div class="muted">无</div>';
    view.innerHTML =
      `<div class="muted" style="margin-bottom:4px;">反链（引用本页）</div>${back}` +
      `<div class="muted" style="margin:8px 0 4px;">出链（本页引用）</div>${out}`;
  } catch (e) {
    panel.style.display = "none";
  }
}

// ── Comments (ROADMAP P3-T5) ──────────────────────────────────────

async function loadComments() {
  const panel = document.getElementById("commentsPanel");
  const list = document.getElementById("commentList");
  if (!_ctx.id || !panel || !list) return;
  try {
    const comments = await api.get(`/api/wiki/pages/${_ctx.id}/comments`);
    panel.style.display = "";
    list.innerHTML = comments.length
      ? comments
          .map(
            (c) => `
      <div style="padding:6px 0;border-bottom:1px solid var(--border);">
        <div style="display:flex;align-items:center;gap:8px;">
          <strong style="font-size:13px;">${esc(c.username)}</strong>
          <span class="muted">${esc(new Date(c.created_at).toLocaleString())}</span>
          <button class="btn btn-sm" style="margin-left:auto;" onclick="window.__delComment('${esc(c.id)}')">删除</button>
        </div>
        <div style="font-size:14px;margin-top:2px;">${esc(c.content)}</div>
      </div>`
          )
          .join("")
      : '<div class="muted">暂无评论</div>';
    const input = document.getElementById("commentInput");
    const submit = document.getElementById("commentSubmit");
    const post = async () => {
      const text = input.value.trim();
      if (!text) return;
      try {
        await api.post(`/api/wiki/pages/${_ctx.id}/comments`, { content: text });
        input.value = "";
        loadComments();
      } catch (err) {
        toast(esc(err.message), true);
      }
    };
    submit.onclick = post;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") post();
    });
  } catch (e) {
    panel.style.display = "none";
  }
}

window.__delComment = async (id) => {
  try {
    await api.del(`/api/wiki/comments/${id}`);
    toast("已删除");
    loadComments();
  } catch (e) {
    toast(e.message, true);
  }
};

// ── Trace-back highlight (ROADMAP P3-T6) ──────────────────────────

// After the preview renders, jump to and highlight the block matching the `hl`
// query param (set by search results). Falls back to first N chars of the
// target so markdown→HTML rendering differences don't break the locate.
function highlightTarget() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const target = params.get("hl");
  if (!target) return;
  const preview = document.getElementById("editorPreview");
  if (!preview) return;
  const needle = target.slice(0, 40);
  let found = null;
  for (const el of preview.querySelectorAll("p, h1, h2, h3, h4, li, td, blockquote, pre")) {
    if (el.textContent.includes(needle)) {
      found = el;
      break;
    }
  }
  if (found) {
    found.style.backgroundColor = "#fef08a";
    found.scrollIntoView({ block: "center" });
  }
}
