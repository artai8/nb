FROM python:3.10-slim
WORKDIR /app

# 1. 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    procps \
    build-essential \
    zlib1g-dev \
    libjpeg-dev \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 生成版本信息
RUN printf '"""Package nb."""\ntry:\n    from importlib.metadata import version\n    __version__ = version("nb")\nexcept Exception:\n    __version__ = "2.0.0"\n' > nb/__init__.py

# 清理 Web UI 目录结构
RUN if [ -d "nb/web_ui/page" ] && [ ! -d "nb/web_ui/pages" ]; then mv nb/web_ui/page nb/web_ui/pages; fi
RUN find nb/web_ui/pages/ -mindepth 1 ! -name "*.py" -exec rm -rf {} + 2>/dev/null || true

# 2. 升级 pip
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip setuptools wheel

# 3. 安装依赖 (调整顺序，防止降级)

# Step A: 安装那些容易导致降级的旧库 (先安装它们！)
# 使用 --no-deps 防止它们自动安装旧版 Pydantic
RUN pip install --no-cache-dir --root-user-action=ignore --prefer-binary \
    "tg-login>=0.0.4" \
    "watermark.py>=0.0.3" \
    "verlat>=0.1.0"

# Step B: 安装核心库与 Pydantic V2
# 这里会覆盖掉任何可能的旧依赖
RUN pip install --no-cache-dir --root-user-action=ignore --prefer-binary \
    "streamlit>=1.33.0" \
    "altair>=5.2.0" \
    "pydantic>=2.7.0" \
    "pymongo>=4.6.3" \
    "python-dotenv>=1.0.1" \
    "PyYAML>=6.0.1,<7.0" \
    "requests>=2.31.0" \
    "typer>=0.12.3" \
    "Telethon>=1.34.0" \
    "aiohttp>=3.9.5" \
    "Pillow>=10.3.0" \
    "hachoir>=3.3.0" \
    "pytesseract>=0.3.10" \
    "rich>=13.7.1" \
    "watchdog>=4.0.0"

# Step C: 🛡️ 保险措施 - 强制检查并重装 Pydantic V2
# 如果前面的步骤导致了降级，这一步会把它升回来
RUN pip install --no-cache-dir --root-user-action=ignore --force-reinstall --ignore-installed "pydantic>=2.7.0"

# 生成可执行命令
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.cli import app\nif __name__ == "__main__":\n    app()\n' > /usr/local/bin/nb && chmod +x /usr/local/bin/nb
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.web_ui.run import main\nif __name__ == "__main__":\n    main()\n' > /usr/local/bin/nb-web && chmod +x /usr/local/bin/nb-web

# 健康检查 (验证 Pydantic 版本)
RUN python -c "import pydantic; print(f'Pydantic Version: {pydantic.VERSION}'); assert pydantic.VERSION.startswith('2')" && \
    python -c "import streamlit; print(f'Streamlit {streamlit.__version__} OK')"

# 端口与权限设置 (适配 HF)
EXPOSE 7860
EXPOSE 8501
ENV PORT=8501
RUN chmod -R 777 /app

CMD ["python", "-m", "nb.web_ui.run"]
