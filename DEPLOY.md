# 部署指南

两种部署方式：
- **A. 本地进程**（默认，与开发一致）：直接用 Python 跑 API + Celery，依赖已存在的 Milvus / Redis。
- **B. Docker Compose**（独立部署）：一条命令起 Milvus + Redis + API + Worker 四个容器。

## A. 本地进程部署

前置：可用的 Milvus 与 Redis（可复用 rag 项目的 test-* 容器，Milvus 19531 / Redis 6380；重启后需 `docker start`）。

```bash
cd "D:\my_project\Data Middle Platform"
python -m venv .venv && .venv\Scripts\activate   # 可选
pip install -e .

cp .env.example .env
# 编辑 .env：MILVUS_HOST/PORT、REDIS_HOST/PORT、API_KEY（务必设置）

# 终端 1：worker
celery -A tasks.celery_worker worker --loglevel=INFO --concurrency=1

# 终端 2：API
uvicorn main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。前端页面右上角输入框粘贴 `.env` 里的 `API_KEY`。

### Windows 开机自启（可选）

- **Worker**：`任务计划程序` 新建任务，启动程序为
  `cmd /c cd /d "D:\my_project\Data Middle Platform" && celery -A tasks.celery_worker worker --loglevel=INFO`
- **API**：同理，命令为 `uvicorn main:app --host 0.0.0.0 --port 8000`
- 勾选"不管用户是否登录都要运行"，否则会话退出后任务会停。

## B. Docker Compose 部署

```bash
cp .env.example .env
# 编辑 .env 的 API_KEY（必须）；MILVUS_HOST/REDIS_HOST 保持默认会被 compose 覆盖为服务名

docker compose up -d --build
docker compose ps
```

- 端口：Milvus 19531 → 容器 19530，Redis 6380 → 容器 6379，API 8000。
- 若本机已跑 rag 的 Milvus/Redis 容器占用 19531/6380，先 `docker stop` 它们，或改端口。
- 数据卷：`milvus_data`（向量）、`redis_data`（任务队列）、`./data`（SQLite）、`./uploads`、`./outputs`。

### 查看日志 / 重启

```bash
docker compose logs -f worker
docker compose restart worker        # worker 重启会自动恢复卡死任务
```

## 上线检查清单

- [ ] `.env` 已设置 `API_KEY`，前端右上角已粘贴
- [ ] `curl http://localhost:8000/health` 返回 `{"status":"ok",...}`（db/milvus/redis 全 true）
- [ ] 无鉴权接口确认：`curl -X POST http://localhost:8000/api/convert/xxx` 返回 401
- [ ] 两个同名文件先后上传 + 入库，互相不删除（Milvus 页按 `source_name` 区分）
- [ ] 重启 worker 后，曾卡在 converting/chunking/ingesting 的文件被重置
- [ ] 上传一个含 `<script>` 的文件名，页面不执行脚本（转义生效）
- [ ] 数据库 `data/pipeline.db` 有定时备份策略

## 常见问题

- **worker 起不来 / CRITICAL Unrecoverable error**：Redis 结果损坏（celery/celery#7878）。
  本项目已 monkey-patch `_store_result` 自动清损坏 key；根治可换 Redis Streams 或独立结果 backend。
- **Milvus 连不上**：`health` 接口返回 `milvus: false`；检查容器是否启动、`.env` 端口。
- **模型加载失败**：`EMBEDDER_LOCAL_FILES_ONLY=true` 时模型需在本地
  `models/` 缓存（或 HF 缓存目录）；首次可在有网环境先跑一次让它下载。
- **内存不足**：MinerU / bge / CLIP 较吃内存，worker 用 `--concurrency=1`；
  若 OOM 关掉 `VLM_ENABLED`（跳过图片描述 LLM）。
