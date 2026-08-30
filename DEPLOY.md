# 部署指南

两种方式：
- **A. 本地进程**：Python 直接运行 API + Celery，复用已有的 Milvus / Redis。
- **B. Docker Compose**：一条命令启动 Milvus + Redis + API + Worker。

## A. 本地进程部署

```bash
cd <项目目录>
python -m venv .venv && .venv\Scripts\activate   # 可选
pip install -e .
cp .env.example .env
# 编辑 .env：Milvus / Redis 连接、API_KEY
```

启动（两个终端）：

```bash
celery -A tasks.celery_worker worker --loglevel=INFO --concurrency=1
uvicorn main:app --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000`，前端右上角粘贴 `.env` 中的 `API_KEY`。

## B. Docker Compose 部署

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

- 端口：Milvus `19531`、Redis `6380`、API `8000`。
- 数据卷：`milvus_data`、`redis_data`、`./data`、`./uploads`、`./outputs`。
- 日志 / 重启：`docker compose logs -f worker`、`docker compose restart worker`。

## 上线检查

- [ ] `.env` 已设置 `API_KEY`
- [ ] `curl http://localhost:8000/health` 返回 `{"status":"ok",...}`
- [ ] 未带 `X-API-Key` 的修改接口返回 401
- [ ] `data/pipeline.db` 有定时备份

## 常见问题

- **worker 起不来**：Redis 结果损坏时项目会自动清理；重启 worker 或更换 Redis backend。
- **Milvus 连不上**：`health` 返回 `milvus: false`，检查容器和 `.env` 端口。
- **内存不足**：worker 使用 `--concurrency=1`；必要时关闭 `VLM_ENABLED`。