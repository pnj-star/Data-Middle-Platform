# File Pipeline Channel — API + Celery worker 镜像
FROM python:3.11-slim

WORKDIR /app

# 文档转换/OCR 等依赖的 C 库
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

COPY . .

# 默认以 API 启动；worker 用 CMD 覆盖：
#   docker run ... image celery -A tasks.celery_worker worker --loglevel=INFO
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
