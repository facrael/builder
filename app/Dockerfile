FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Static check: undefined names (missing imports) fail the BUILD, not the chat.
RUN pip install --no-cache-dir pyflakes && python -m pyflakes /app/*.py
# Runtime smoke: importing modules catches broken top-level wiring too.
# main.py instantiates the Bot at import time -> feed a placeholder token here;
# the real one comes from .env at runtime.
RUN BOT_TOKEN=000:smoke DATABASE_URL=postgresql://x ADMIN_TG_ID=0 python -c "import main; import db; import pdf_engine; import obsidian; print('smoke-ok')"
CMD ["python", "main.py"]