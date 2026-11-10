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
# Provide valid minimal dummy env for config validation during build smoke test.
RUN BOT_TOKEN=000:smoke DATABASE_URL=postgresql://x ADMIN_TG_ID=1 PROJECTS=OBJ1 PROJECT_OBJ1_CODE=ЦТП PROJECT_OBJ1_NAME=Test PROJECT_OBJ1_USERS=1 python -c "import config; import db; import pdf_engine; import obsidian; import main; print('smoke-ok')"
CMD ["python", "main.py"]
