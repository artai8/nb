FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y ffmpeg tesseract-ocr && \
    rm -rf /var/lib/apt/lists/*

# 复制项目
COPY . .

# 手动安装所有依赖（从 pyproject.toml 提取）
RUN pip install --no-cache-dir \
    requests==2.28.1 \
    typer==0.7.0 \
    python-dotenv==0.21.0 \
    pydantic==1.10.2 \
    Telethon==1.26.0 \
    cryptg==0.4.0 \
    Pillow==9.3.0 \
    hachoir==3.1.3 \
    aiohttp==3.8.3 \
    tg-login==0.0.4 \
    watermark.py==0.0.3 \
    pytesseract==0.3.7 \
    rich==12.6.0 \
    verlat==0.1.0 \
    streamlit==1.15.2 \
    PyYAML==6.0 \
    pymongo==4.3.3

# 设置 Python 路径
ENV PYTHONPATH=/app

EXPOSE 8501

# 直接调用 run.py 的 main 函数
CMD ["python", "-c", "import sys; sys.path.insert(0,'/app'); from nb.web_ui.run import main; main()"]
