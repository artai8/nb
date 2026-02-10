FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr && \
    rm -rf /var/lib/apt/lists/*

# 复制项目
COPY . .

# 修复 __init__.py 的版本问题
RUN sed -i 's/__version__ = version(__package__)/__version__ = "1.1.8"/' nb/__init__.py

# 安装 Python 依赖
RUN pip install --no-cache-dir \
    requests \
    typer \
    python-dotenv \
    pydantic==1.10.2 \
    Telethon==1.26.0 \
    cryptg \
    Pillow \
    hachoir \
    aiohttp \
    tg-login \
    watermark.py \
    pytesseract \
    rich \
    verlat \
    streamlit \
    PyYAML \
    pymongo

# 设置 Python 路径
ENV PYTHONPATH=/app

EXPOSE 8501

# 直接运行
CMD ["python", "-c", "from nb.web_ui.run import main; main()"]
