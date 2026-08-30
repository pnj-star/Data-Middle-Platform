// Login / register view (Phase 2): JWT login so real users can use the wiki
// instead of pasting an X-API-Key.
import api, { setToken } from "../api.js";
import { esc, toast } from "../util.js";

export function renderLogin() {
  return `
    <div class="card" style="max-width:420px;margin:40px auto;">
      <div class="card-header" style="display:flex;gap:16px;">
        <a href="#/login" class="auth-tab" id="tabLogin">登录</a>
        <a href="#/register" class="auth-tab" id="tabRegister">注册</a>
      </div>
      <div class="form-row" style="margin-top:14px;">
        <input id="authUsername" type="text" placeholder="用户名" autocomplete="username" />
      </div>
      <div class="form-row" style="margin-top:8px;">
        <input id="authPassword" type="password" placeholder="密码" autocomplete="current-password" />
      </div>
      <div class="form-row" style="margin-top:14px;">
        <button class="btn btn-primary" id="authSubmit">登录</button>
      </div>
      <div class="muted" id="authMsg" style="margin-top:8px;"></div>
    </div>`;
}

window.addEventListener("wiki:rendered", (e) => {
  if (e.detail === "#/login" || e.detail === "#/register") bindAuth();
});

async function bindAuth() {
  const isRegister = location.hash === "#/register";
  const submit = document.getElementById("authSubmit");
  const msg = document.getElementById("authMsg");
  const tabLogin = document.getElementById("tabLogin");
  const tabRegister = document.getElementById("tabRegister");
  if (tabLogin) tabLogin.classList.toggle("active", !isRegister);
  if (tabRegister) tabRegister.classList.toggle("active", isRegister);
  submit.textContent = isRegister ? "注册" : "登录";

  submit.onclick = async () => {
    const username = document.getElementById("authUsername").value.trim();
    const password = document.getElementById("authPassword").value;
    if (!username || !password) return toast("请输入用户名和密码", true);
    submit.disabled = true;
    msg.textContent = isRegister ? "注册中…" : "登录中…";
    try {
      if (isRegister) {
        const r = await api.post("/api/wiki/auth/register", { username, password });
        // auto-login right after registration
        const lr = await api.post("/api/wiki/auth/login", { username, password });
        setToken(lr.access_token);
        toast(`已注册并登录：${r.username}`);
      } else {
        const r = await api.post("/api/wiki/auth/login", { username, password });
        setToken(r.access_token);
        toast("登录成功");
      }
      location.hash = "#/tree";
    } catch (err) {
      msg.textContent = "";
      toast(esc(err.message), true);
    }
    submit.disabled = false;
  };
}
