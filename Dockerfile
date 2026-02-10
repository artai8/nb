FROM python:3.10

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1. 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. 升级 pip 并安装 poetry
RUN pip install --upgrade pip && \
    pip install poetry==1.8.3

# 3. 复制项目配置文件
COPY pyproject.toml poetry.lock* ./

# 4. 配置 poetry 不创建虚拟环境（直接装到系统 Python）
RUN poetry config virtualenvs.create false

# 5. 安装依赖（不包含项目本身）
RUN poetry install --no-interaction --no-ansi --only main --no-root

# 6. 复制整个项目源码
COPY . .

# 7. 关键步骤：安装项目本身为 Python 包
RUN pip install -e . || poetry install --no-interaction --no-ansi --only main

# 8. 验证安装
RUN python -c "import nb; print('nb module installed successfully')" && \
    python -c "from nb.web_ui.run import main; print('nb.web_ui.run.main found')"

# 9. 创建启动脚本（作为备份方案）
RUN echo '#!/usr/bin/env python3\n\
import sys\n\
import os\n\
sys.path.insert(0, "/app")\n\
from nb.web_ui.run import main\n\
if __name__ == "__main__":\n\
    main()' > /usr/local/bin/nb-web-backup && \
    chmod +x /usr/local/bin/nb-web-backup

EXPOSE 8501

# 10. 使用 Python 模块方式启动（最可靠）
CMD ["python", "-m", "nb.web_ui.run"]
