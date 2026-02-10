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

# 4. 让 poetry 使用 /venv，先只安装依赖（不安装项目本身）
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main --no-root

# 5. 复制全部源码
COPY . .

# 6. 用 /venv/bin/pip 安装项目本身，确保入口脚本生成在 /venv/bin/
RUN "$VENV_PATH/bin/pip" install --no-cache-dir --no-deps .

# 7. 构建时验证 nb-web 命令存在
RUN which nb-web

# 8. 暴露端口
EXPOSE 8501

# 9. 启动命令
CMD ["nb-web"]
