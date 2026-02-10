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

# 修复 __init__.py 版本问题
RUN printf '"""Package nb."""\ntry:\n    from importlib.metadata import version\n    __version__ = version("nb")\nexcept Exception:\n    __version__ = "1.1.8"\n' > nb/__init__.py

# 修复目录名：page -> pages
RUN if [ -d "nb/web_ui/page" ] && [ ! -d "nb/web_ui/pages" ]; then \
        mv nb/web_ui/page nb/web_ui/pages; \
        echo "Renamed page -> pages"; \
    fi

# 验证目录结构
RUN echo "=== Web UI Structure ===" && \
    ls -la nb/web_ui/ && \
    echo "=== Pages ===" && \
    ls -la nb/web_ui/pages/

# 安装依赖
RUN pip install --no-cache-dir \
    streamlit==1.15.2 \
    pymongo==4.3.3 \
    pydantic==1.10.2 \
    python-dotenv==0.21.0 \
    PyYAML==6.0 \
    requests==2.28.1 \
    typer==0.7.0 \
    Telethon==1.26.0 \
    cryptg==0.4.0 \
    "Pillow>=9.3,<11.0" \
    hachoir==3.1.3 \
    aiohttp==3.8.3 \
    tg-login==0.0.4 \
    watermark.py==0.0.3 \
    pytesseract==0.3.7 \
    rich==12.6.0 \
    verlat==0.1.0

ENV PYTHONPATH=/app
EXPOSE 8501

CMD ["python", "-m", "nb.web_ui.run"]
