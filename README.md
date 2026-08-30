# Data Middle Platform

文档入库流水线：PDF / Word / PPT / Markdown / 图片 → 转 Markdown → 切片 → 向量化 → 写入 Milvus。
前端提供上传、转换预览/编辑、切片编辑、入库与 Milvus 数据浏览/管理页面。

后端 FastAPI + Celery（Redis broker）异步任务；向量库 Milvus 与 rag 项目共享实例和集合。

## 架构

```
浏览器 ── HTTP ──> FastAPI (main.py)
                    │  POST 任务 → 写 Redis 队列
                    ▼
                  Celery worker (tasks/celery_worker.py)
                    │  转换(MinerU) → 切片 → 嵌入 → 写入
                    ▼
              Milvus (mushroom_knowledge / mushroom_images)
              SQLite (data/pipeline.db 任务状态/文件元数据)
```

- `main.py` — FastAPI 应用 + 全部 HTTP 接口
- `tasks/celery_worker.py` — Celery 任务（convert / chunk / ingest / 全流程 / 重嵌入）
- `src/` — 转换(MinerU)、切片、嵌入(bge)、CLIP、图片处理、Milvus 客户端、去重、SQLite
- `static/index.html` — 单页前端（原生 JS + marked + lucide + DOMPurify）

## 快速开始

```bash
# 1. 依赖（Python 3.11+）
pip install -e .

# 2. 配置
cp .env.example .env
#   填 Milvus / Redis 连接，设置 API_KEY（见下方"安全"）

# 3. 依赖服务：Milvus + Redis
#    参考 docker-compose.yml（或复用 rag 项目已有的容器）

# 4. 启动 API（会自动拉起 Celery worker + MinerU 服务）
uvicorn main:app --host 0.0.0.0 --port 8000
#    MinerU（pdf/docx/pptx GPU 解析）在 MINERU_AUTO_START=true（默认）时也会自动拉起，
#    前提是独立环境 D:\my_env\mineru_env 已装好 mineru[all] + CUDA torch（首次运行自动下载模型）。
#    也可手动运行 scripts\start_mineru_api.bat（日志 mineru_api.log）。
#    启动时自动用当前 Python 解释器拉起 Celery worker（日志写入 celery_worker.log），
#    无需手动开第二个终端。需要手动控制时：
#      celery -A tasks.celery_worker worker --loglevel=INFO
#    已在跑 worker 时会自动跳过；设 AUTO_START_WORKER=false 可关闭自动拉起。

# 5. 打开 http://localhost:8000
```

## 安全

- **API Key**：默认 `API_KEY` 为空，所有接口开放。上线前务必设置：
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  # 填入 .env 的 API_KEY
  ```
  设置后所有 mutating 接口（上传/删除/转换/入库/清理）要求 `X-API-Key` 请求头。
  前端在页面右上角输入框粘贴同一个 Key（保存在浏览器 localStorage），请求会自动携带。

- **XSS 防护**：前端所有用户内容（文件名、错误信息、Markdown、chunk 文本、Milvus 内容）均经过 HTML 转义；
  Markdown 预览经 DOMPurify 清洗。`marked.parse` 不再直接渲染进 DOM。

- **错误信息**：全局异常处理器只向客户端返回通用错误，具体堆栈仅在服务端日志。

## 同名文件处理

Milvus 中每行记录的 `source` 是**文件 UUID**（`files.id`），不是文件名。
因此两个同名但内容不同的 `report.pdf` 各自独立入库，互不覆盖、互不删除；同名且内容完全相同、范围也相同的文件会被新上传去重拦截。
展示时后端从 SQLite 反查文件名返回 `source_name` 字段，前端"源文件"列显示文件名。

旧版本按文件名入库的历史数据：仅当该文件名在库中唯一时才按名清理，避免误删同名兄弟文件的向量。

## 文件去重

新上传文件会按内容 SHA-256 在同一租户、知识库、产品 ID 范围内去重；重复时接口返回 409 和已有文件信息。请求带 `force=true` 时可以建立新的逻辑记录。

物理文件按 `sha256 + 原扩展名` 保存，多条记录可以引用同一份文件；删除时只有最后一个引用被删除后才会清理物理文件。原有的父段落级内容去重保留，并按 `tenant_id / kb_id / product_id` 隔离。

## 健康检查

```bash
curl http://localhost:8000/health
# {"status":"ok","db":true,"milvus":true,"redis":true}
```
任一项异常返回 HTTP 503 与 `status: "degraded"`。

## 卡死任务恢复

Celery worker 设置了软/硬超时（1500s/1800s）。worker 崩溃后，SQLite 中卡在
`converting` / `chunking` / `ingesting` 超过 `RECOVER_STALE_SECONDS`（默认 3600s）
的文件会在下次 **worker 或 API 启动时** 自动重置到可恢复的前序状态
（converting→uploaded，chunking→converted，ingesting→chunked），前端可重新触发对应阶段。
可用 `RECOVER_STALE_SECONDS` 调整阈值。

## API 一览

| 方法 | 路径 | 说明 | 需 API Key |
|---|---|---|---|
| POST | `/api/files/upload` | 上传文件 | ✓ |
| GET | `/api/files` | 文件列表 | |
| GET/DELETE | `/api/files/{id}` | 文件详情 / 删除 | 删除 ✓ |
| POST | `/api/convert/{id}` | 触发转换 | ✓ |
| GET/PUT | `/api/converted/{id}` | 转换结果 / 编辑 Markdown | 编辑 ✓ |
| POST | `/api/chunks/{id}` | 触发切片 | ✓ |
| GET/PUT | `/api/chunks/{id}` | 切片树 / 编辑切片 | 编辑 ✓ |
| POST | `/api/ingest/{id}` | 触发入库 | ✓ |
| POST | `/api/ingest-image/{id}` | 图片入库 | ✓ |
| POST | `/api/ingest-full/{id}` | 一键全流程 | ✓ |
| GET | `/api/tasks/{id}` | 任务状态 | |
| GET | `/api/milvus/stats` | 集合统计 | |
| GET | `/api/milvus/documents` | 检索/浏览（支持分页 page/limit） | |
| DELETE | `/api/milvus/documents/{id}` | 删除单条 | ✓ |
| POST | `/api/milvus/batch-delete` | 批量删除 | ✓ |
| GET | `/api/config` | 前端配置/开关 | |
| GET | `/health` | 健康检查 | |

## 环境变量

见 [.env.example](.env.example)。核心：

- `MILVUS_*` / `REDIS_*` — Milvus 与 Redis 连接
- `API_KEY` — mutating 接口认证
- `EMBEDDER_LOCAL_FILES_ONLY` — 嵌入模型是否仅用本地缓存
- `MAX_FILE_SIZE_MB` / `MAX_IMAGE_SIZE_MB` — 上传大小上限
- `AUTO_START_WORKER` — API 启动时是否自动拉起 Celery worker（默认 true）
- `MINERU_ENABLED` / `MINERU_BASE_URL` / `MINERU_BACKEND` / `MINERU_TIMEOUT_SECONDS` / `MINERU_AUTO_START` / `MINERU_EXECUTABLE` — MinerU 转换后端：pdf/docx/pptx 走 GPU pipeline；启动 API 时自动拉起 `mineru-api`（日志 `mineru_api.log`），也可手动 `scripts/start_mineru_api.bat`
- html/md/txt 由内置轻量转换路径处理（无需外部引擎）；pdf/docx/pptx 无回退引擎，MinerU 不可用时会明确报错
- `RECOVER_STALE_SECONDS` — 卡死任务恢复阈值

## 测试

```bash
python -m pytest tests/ -q
```
- 单元/API 测试：`tests/test_main.py`、`tests/test_db.py`、`tests/test_chunker.py` 等
- 测试对 Milvus / Redis / Celery 打桩，不依赖真实服务

## 与 rag 项目的协作

本项目与 `deep_rag` 共享同一 Milvus 实例与集合（`mushroom_knowledge` / `mushroom_images`）。
请勿 drop/重建这两个集合（会毁掉 rag 的 dense+sparse 混合索引与已有数据）；
清空用本项目 Milvus 页面的"全部删除"，而非删除集合。
