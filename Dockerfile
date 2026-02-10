FROM python:3.10

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        apt-utils \
        ffmpeg \
        tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. 安装 poetry
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir "poetry==1.8.3"

# 3. 先只复制依赖定义文件（利用 Docker 缓存）
COPY pyproject.toml poetry.lock* ./

# 4. 禁止 poetry 创建虚拟环境，直接装到系统 Python
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main --no-root

# 5. 复制全部源码
COPY . .

# 6. 安装项目本身，生成入口脚本
RUN poetry install --no-interaction --no-ansi --only main

# 7. 验证
RUN which nb-web && nb-web --help || true

# 8. 暴露端口
EXPOSE 8501

# 9. 启动命令
CMD ["nb-web"]
