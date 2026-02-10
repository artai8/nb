FROM python:3.10

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY pyproject.toml poetry.lock* ./
RUN pip install poetry==1.8.3 && \
    poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main --no-root

# 复制代码
COPY . .

# 安装项目
RUN pip install -e .

EXPOSE 8501

# 直接用 Python 模块方式运行（最可靠）
CMD ["python", "-m", "streamlit", "run", "nb/web_ui/0_👋_Hello.py", "--server.port=8501", "--server.address=0.0.0.0"]
