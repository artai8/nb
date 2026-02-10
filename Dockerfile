FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        gcc \
        python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY pyproject.toml poetry.lock* ./

# 安装 Poetry 并导出依赖到 requirements.txt
RUN pip install poetry==1.8.3 && \
    poetry export -f requirements.txt --output requirements.txt --without-hashes --only main

# 用 pip 安装依赖（避免 Poetry 的脚本安装问题）
RUN pip install -r requirements.txt

# 复制源代码
COPY nb/ ./nb/

# 手动创建入口脚本
RUN echo '#!/usr/bin/env python\n\
from nb.web_ui.run import main\n\
if __name__ == "__main__":\n\
    main()' > /usr/local/bin/nb-web && \
    chmod +x /usr/local/bin/nb-web

# 同样创建 nb 命令
RUN echo '#!/usr/bin/env python\n\
from nb.cli import app\n\
import typer\n\
if __name__ == "__main__":\n\
    typer.run(app)' > /usr/local/bin/nb && \
    chmod +x /usr/local/bin/nb

EXPOSE 8501

CMD ["nb-web"]
