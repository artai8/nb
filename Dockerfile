FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tesseract-ocr procps && \
    rm -rf /var/lib/apt/lists/*
COPY . .
RUN printf '"""Package nb."""\ntry:\n    from importlib.metadata import version\n    __version__ = version("nb")\nexcept Exception:\n    __version__ = "1.1.8"\n' > nb/__init__.py
RUN if [ -d "nb/web_ui/page" ] && [ ! -d "nb/web_ui/pages" ]; then mv nb/web_ui/page nb/web_ui/pages; fi
RUN find nb/web_ui/pages/ -mindepth 1 ! -name "*.py" -exec rm -rf {} + 2>/dev/null || true
RUN pip install --no-cache-dir \
    altair==4.2.2 streamlit==1.15.2 pymongo==4.3.3 pydantic==1.10.2 \
    python-dotenv==0.21.0 "PyYAML>=6.0,<7.0" requests==2.28.1 typer==0.7.0 \
    Telethon==1.42.0 cryptg==0.4.0 "Pillow>=9.3,<11.0" hachoir==3.1.3 \
    aiohttp==3.8.3 tg-login==0.0.4 watermark.py==0.0.3 pytesseract==0.3.7 \
    rich==12.6.0 verlat==0.1.0
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.cli import app\nif __name__ == "__main__":\n    app()\n' > /usr/local/bin/nb && chmod +x /usr/local/bin/nb
RUN printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/app")\nfrom nb.web_ui.run import main\nif __name__ == "__main__":\n    main()\n' > /usr/local/bin/nb-web && chmod +x /usr/local/bin/nb-web
RUN python -c "import streamlit; print(f'Streamlit {streamlit.__version__} OK')" && \
    python -c "import altair; print(f'Altair {altair.__version__} OK')" && \
    python -c "from nb.cli import app; print('nb CLI OK')"
RUN nb --version || echo "nb command created"
ENV PYTHONPATH=/app
EXPOSE 8501
CMD ["python", "-m", "nb.web_ui.run"]
