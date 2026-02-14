FROM python:3.11-slim
WORKDIR /app

# 1. 安装系统依赖 (新增 build-essential 和 python3-dev 用于编译)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    procps \
    build-essential \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 生成版本信息
RUN printf '"""Package nb."""\ntry:\n    from importlib.metadata import version\n    __version__ = version("nb")\nexcept Exception:\n    __version__ = "2.0.0"\n' > nb/__init__.py

# 清理 Web UI 目录结构
RUN if [ -d "nb/web_ui/page" ] && [ ! -d "nb/web_ui/pages" ]; then mv nb/web_ui/page nb/web_ui/pages; fi
RUN find nb/web_ui/pages/ -mindepth 1 ! -name "*.py" -exec rm -rf {} + 2>/dev/null || true

# 2. 升级 pip 核心工具 (极大提高构建成功率)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 3. 安装项目依赖
# ⚠️ 已移除 'cryptg' 以避免 Rust 编译错误
RUN pip install --no-cache-dir \
    "altair>=5.2.0" \
    "streamlit>=1.33.0" \
    "pymongo>=4.6.3" \
    "pydantic>=2.7.0" \
    "python-dotenv>=1.0.1" \
    "PyYAML>=6.0.1,<7.0" \
    "requests>=2.31.0" \
    "typer>=0.12.3" \
    "Telethon>=1.34.0" \
    "Pillow>=10.3.0" \
    "hachoir>=3.3.0" \
    "aiohttp>=3.9.5" \
    "tg-login>=0.0.4" \
    "watermark.py>=0.0.3" \
    "pytesseract>=0.3.10" \
    "rich>=13.7.1" \
    "verlat>=0.1.0" \
    "watchdog>=4.0.0"

# 生成可执行命令
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.cli import app\nif __name__ == "__main__":\n    app()\n' > /usr/local/bin/nb && chmod +x /usr/local/bin/nb
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.web_ui.run import main\nif __name__ == "__main__":\n    main()\n' > /usr/local/bin/nb-web && chmod +x /usr/local/bin/nb-web

# 健康检查
RUN python -c "import streamlit; print(f'Streamlit {streamlit.__version__} OK')" && \
    python -c "import altair; print(f'Altair {altair.__version__} OK')" && \
    python -c "from nb.cli import app; print('nb CLI OK')"

RUN nb --version || echo "nb command created"

ENV PYTHONPATH=/app
EXPOSE 8501
CMD ["python", "-m", "nb.web_ui.run"]
