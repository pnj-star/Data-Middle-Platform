// Import view (ROADMAP P1-T3): converted pipeline file → wiki page draft.
// The import entry now lives in the Wiki module (not the pipeline console).
import api from "../api.js";
import { esc, toast } from "../util.js";

export function renderAdmin() {
  return `
    <div class="card">
      <div class="card-header">选择已转换的文本文件，导入为页面草稿</div>
      <div class="form-row">
        <label>文件:</label>
        <select id="importFileSelect" style="flex:1;max-width:380px;"><option value="">-- 加载中… --</option></select>
      </div>
      <div class="form-row">
        <label>目标空间:</label>
        <select id="importSpaceSelect" style="flex:1;max-width:220px;"><option value="">-- 加载中… --</option></select>
      </div>
      <div class="form-row">
        <button class="btn btn-primary" id="importBtn" onclick="window.__wikiImport()" disabled>导入为 Wiki 草稿</button>
        <span class="muted" id="importStatus"></span>
      </div>
      <div id="importResult" style="margin-top:4px;"></div>
    </div>
  `;
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail.startsWith("#/admin")) initImport();
});

async function initImport() {
  const fileSel = document.getElementById("importFileSelect");
  const spaceSel = document.getElementById("importSpaceSelect");
  if (!fileSel || !spaceSel) return;

  // 只有已转换的文本文件才能导入（后端 /import-from-file 会 409 无转换）
  try {
    const r = await api.get("/api/files");
    const importable = ["converted", "chunking", "chunked", "ingesting", "done"];
    const text = (r.files || []).filter((f) => f.type === "text" && importable.includes(f.status));
    if (!text.length) {
      fileSel.innerHTML =
        '<option value="">-- 暂无已转换的文本文件，请先在「文档流水线」上传并转换 --</option>';
    } else {
      fileSel.innerHTML =
        '<option value="">-- 选择文件 --</option>' +
        text
          .map((f) => `<option value="${esc(f.id)}">${esc(f.name)} (${esc(f.status)})</option>`)
          .join("");
    }
  } catch (err) {
    fileSel.innerHTML = `<option value="">加载失败: ${esc(err.message)}</option>`;
  }

  try {
    const spaces = await api.get("/api/wiki/spaces");
    spaceSel.innerHTML =
      '<option value="">-- 选择空间 --</option>' +
      spaces
        .map((s) => `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.slug)})</option>`)
        .join("");
  } catch (err) {
    spaceSel.innerHTML = `<option value="">加载失败: ${esc(err.message)}</option>`;
  }

  updateImportBtn();
  [fileSel, spaceSel].forEach((s) => s.addEventListener("change", updateImportBtn));
}

function updateImportBtn() {
  const btn = document.getElementById("importBtn");
  const fileSel = document.getElementById("importFileSelect");
  const spaceSel = document.getElementById("importSpaceSelect");
  if (btn && fileSel && spaceSel) btn.disabled = !fileSel.value || !spaceSel.value;
}

window.__wikiImport = async () => {
  const fileSel = document.getElementById("importFileSelect");
  const spaceSel = document.getElementById("importSpaceSelect");
  const status = document.getElementById("importStatus");
  const result = document.getElementById("importResult");
  const btn = document.getElementById("importBtn");
  if (!fileSel || !spaceSel || !fileSel.value || !spaceSel.value) {
    toast("请选择文件和目标空间", true);
    return;
  }
  btn.disabled = true;
  if (status) status.textContent = "正在导入…";
  try {
    const page = await api.post("/api/wiki/import-from-file/" + fileSel.value, {
      space_id: spaceSel.value,
    });
    toast("已导入为 Wiki 草稿");
    if (result) {
      result.innerHTML = `
        <div class="card" style="border-color:var(--accent-soft);">
          <div class="card-header">导入成功</div>
          <div class="form-row">
            <span>草稿：<a href="#/editor/${esc(page.id)}"><strong>${esc(page.title)}</strong></a></span>
            <a class="btn btn-sm btn-primary" href="#/editor/${esc(page.id)}">去编辑 →</a>
          </div>
        </div>`;
    }
  } catch (err) {
    toast("导入失败: " + (err.message || err), true);
    if (result) result.innerHTML = `<div class="muted">导入失败：${esc(err.message || err)}</div>`;
  } finally {
    btn.disabled = false;
    if (status) status.textContent = "";
  }
};
