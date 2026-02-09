FROM python:3.10

# 虚拟环境路径放在 /venv，并加入 PATH
ENV VENV_PATH="/venv"
ENV PATH="$VENV_PATH/bin:$PATH"
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. 安装系统依赖：apt-utils / ffmpeg / tesseract-ocr
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        apt-utils \
        ffmpeg \
        tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. 创建虚拟环境，并在虚拟环境里安装 pip / setuptools / wheel / poetry
RUN python -m venv "$VENV_PATH" && \
    "$VENV_PATH/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel && \
    "$VENV_PATH/bin/pip" install --no-cache-dir "poetry==1.8.3"

# 3. 先只复制依赖定义文件，加快构建缓存利用
COPY pyproject.toml poetry.lock* ./

# 4. 让 poetry 使用我们自己的 /venv，不再创建额外虚拟环境，
#    并先安装所有依赖但不安装本项目本身（no-root，用于依赖缓存）
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main --no-root

# 5. 再复制全部源码
COPY . .

# 6. 安装当前项目本身（这样才会有 nb-web 这个命令行入口）
RUN poetry install --no-interaction --no-ansi --only main

# 7. 暴露端口
EXPOSE 8501

# 8. 启动命令（你原来就是 nb-web，这里保持不变）
CMD ["nb-web"]
