"""Real-environment full acceptance for Phases 1-4 (run against a live API).

Covers: P4 infra (health/postgres, request-id, metrics), P1 pipeline loop
(upload→convert→import→edit→publish→search), P3 (diff/attachment/comment/links/
trash), P2 (users/spaces/ACL/audit), P4 (scoped key, idempotency).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import httpx

sys.path.insert(0, os.getcwd())
from src.config import config  # noqa: E402

BASE = "http://127.0.0.1:8001"
KEY = config.security.api_key
c = httpx.Client(base_url=BASE, timeout=120, headers={"X-API-Key": KEY} if KEY else None)

ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [OK] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {extra}")


def wait_task(tid, limit=150):
    for _ in range(limit):
        st = c.get(f"/api/tasks/{tid}").json()
        if st["state"] in ("SUCCESS", "FAILURE"):
            return st
        time.sleep(3)
    return {"state": "TIMEOUT"}


print("== P4 基础设施 ==")
r = c.get("/health")
check("health 200", r.status_code == 200, r.text[:80])
check("health 含 postgres=true", r.json().get("postgres") is True, str(r.json()))
check("request-id 头", bool(r.headers.get("x-request-id")))
check("api-version=v1", r.headers.get("x-api-version") == "v1")
r = c.get("/metrics")
check("metrics 200+指标", r.status_code == 200 and "wiki_http_requests" in r.text)

print("== P1 闭环 ==")
md = "# 端到端验收\n\n这是验收文档，讲松茸与云南高原。\n\n## 章节\n内容在这里。"
with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
    f.write(md.encode())
    fpath = f.name
with open(fpath, "rb") as f:
    r = c.post("/api/files/upload", files={"files": ("e2e.md", f, "text/markdown")})
check("上传文件", r.status_code == 200, r.text[:120])
fid = r.json()["files"][0]["id"]
r = c.post(f"/api/convert/{fid}")
st = wait_task(r.json()["task_id"])
check("转换完成", st["state"] == "SUCCESS", str(st))
sid = c.get("/api/wiki/spaces").json()[0]["id"]
r = c.post(f"/api/wiki/import-from-file/{fid}", json={"space_id": sid})
check("导入 wiki 草稿", r.status_code == 200, r.text[:120])
pid = r.json()["id"]
r = c.put(f"/api/wiki/pages/{pid}", json={"content": md + "\n\n## 修订\n补充内容。", "note": "v2"})
check("编辑产生 revision 2", r.json().get("revision_id") == 2, r.text[:80])
r = c.get(f"/api/wiki/pages/{pid}/revisions/diff", params={"from_rev": 1, "to_rev": 2})
check("diff 接口", r.status_code == 200 and len(r.json()["lines"]) > 0)
r = c.post(f"/api/wiki/pages/{pid}/publish")
pub = r.json()
st = wait_task(pub["task_id"])
check("发布任务成功", st["state"] == "SUCCESS", str(st))
pg = c.get(f"/api/wiki/pages/{pid}").json()
check("页面 published", pg["status"] == "published", pg["status"])
r = c.get("/api/wiki/search", params={"q": "松茸 云南"})
check("搜索命中", r.json()["total"] >= 1 and any(x["page_id"] == pid for x in r.json()["results"]), r.text[:120])

print("== P3 特性 ==")
r = c.post(f"/api/wiki/pages/{pid}/attachments", files={"file": ("note.txt", b"attachment data", "text/plain")})
check("附件上传", r.status_code == 200, r.text[:80])
att = r.json()["id"]
r = c.get(f"/api/wiki/attachments/{att}/download")
check("附件下载", r.status_code == 200 and r.content == b"attachment data")
r = c.post(f"/api/wiki/pages/{pid}/comments", json={"content": "验收评论"})
check("评论发表", r.status_code == 200, r.text[:80])
c.post("/api/wiki/pages", json={"space_id": sid, "title": "另一个页"})
c.put(f"/api/wiki/pages/{pid}", json={"content": md + "\n\n引用 [[另一个页]]"})
links = c.get(f"/api/wiki/pages/{pid}/links").json()
check("出链含目标", any(l["target_title"] == "另一个页" for l in links), str(links))
r = c.delete(f"/api/wiki/pages/{pid}")
check("软删进回收站", r.status_code == 200 and r.json().get("trashed") is True)
trash = c.get("/api/wiki/trash").json()
check("回收站含页面", any(t["id"] == pid for t in trash))
check("恢复", c.post(f"/api/wiki/trash/{pid}/restore").status_code == 200)

print("== P2 多用户 / ACL ==")
c2 = httpx.Client(base_url=BASE, timeout=30)
check("注册 alice", c2.post("/api/wiki/auth/register", json={"username": "e2e_alice", "password": "pw123456"}).status_code == 200)
check("注册 bob", c2.post("/api/wiki/auth/register", json={"username": "e2e_bob", "password": "pw123456"}).status_code == 200)
at = c2.post("/api/wiki/auth/login", json={"username": "e2e_alice", "password": "pw123456"}).json()["access_token"]
bt = c2.post("/api/wiki/auth/login", json={"username": "e2e_bob", "password": "pw123456"}).json()["access_token"]
AH, BH = {"Authorization": f"Bearer {at}"}, {"Authorization": f"Bearer {bt}"}
r = c2.post("/api/wiki/spaces", json={"slug": "e2e-team", "name": "验收团队"}, headers=AH)
check("alice 创建空间", r.status_code == 200, r.text[:80])
team = r.json()["id"]
bob_id = c2.get("/api/wiki/auth/me", headers=BH).json()["id"]
check("分配 bob reader", c2.post(f"/api/wiki/spaces/{team}/members", json={"user_id": bob_id, "role": "reader"}, headers=AH).status_code == 200)
check("bob 读 team", c2.get("/api/wiki/pages", params={"space_id": team}, headers=BH).status_code == 200)
check("bob 写 team 403", c2.post("/api/wiki/pages", json={"space_id": team, "title": "x"}, headers=BH).status_code == 403)
check("alice 写 team", c2.post("/api/wiki/pages", json={"space_id": team, "title": "alice 页"}, headers=AH).status_code == 200)
audit = c.get("/api/wiki/audit").json()
check("审计有记录", len(audit) > 0, f"n={len(audit)}")

print("== P4 范围 key + 幂等 ==")
r = c.post("/api/wiki/api-keys", json={"name": "e2e-key", "space_id": team, "role": "reader"})
check("签发 scope key", r.status_code == 200 and r.json()["key"].startswith("sk_"), r.text[:80])
sk = r.json()["key"]
check("scope key 读 team", c2.get("/api/wiki/pages", params={"space_id": team}, headers={"X-API-Key": sk}).status_code == 200)
check("scope key 写 403", c2.post("/api/wiki/pages", json={"space_id": team, "title": "y"}, headers={"X-API-Key": sk}).status_code == 403)
r1 = c.post("/api/wiki/pages", json={"space_id": sid, "title": "幂等验收"}, headers={"Idempotency-Key": "e2e-idem"})
r2 = c.post("/api/wiki/pages", json={"space_id": sid, "title": "幂等验收"}, headers={"Idempotency-Key": "e2e-idem"})
check("幂等同 key 同页", r1.status_code == 200 and r1.json()["id"] == r2.json()["id"])

print(f"\n===== 验收结果: {ok} OK / {fail} FAIL =====")
