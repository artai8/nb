FROM python:3.10

ENV VENV_PATH="/venv"
ENV PATH="$VENV_PATH/bin:$PATH"
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

# 2. 创建虚拟环境，安装 poetry
RUN python -m venv "$VENV_PATH" && \
    "$VENV_PATH/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel && \
    "$VENV_PATH/bin/pip" install --no-cache-dir "poetry==1.8.3"

# 3. 先只复制依赖定义文件（利用 Docker 缓存）
COPY pyproject.toml poetry.lock* ./

# 4. 让 poetry 使用 /venv，先只安装依赖
RUN poetry config virtualenvs.create false && \
    poetry config virtualenvs.path "$VENV_PATH" && \
    poetry install --no-interaction --no-ansi --only main --no-root

# 5. 复制全部源码
COPY . .

# 6. 安装项目本身（不带 --no-root，让 poetry 生成入口脚本）
RUN poetry install --no-interaction --no-ansi --only main

# 7. 如果入口脚本不在 /venv/bin，手动查找并软链接
RUN if ! which nb-web > /dev/null 2>&1; then \
        FOUND=$(find / -name "nb-web" -type f 2>/dev/null | head -1); \
        if [ -n "$FOUND" ]; then \
            ln -sf "$FOUND" "$VENV_PATH/bin/nb-web"; \
            ln -sf "$(dirname "$FOUND")/nb" "$VENV_PATH/bin/nb" 2>/dev/null || true; \
        fi; \
    fi

# 8. 验证
RUN which nb-web && echo "✅ nb-web found at $(which nb-web)"

# 9. 暴露端口
EXPOSE 8501

# 10. 启动命令
CMD ["nb-web"]
