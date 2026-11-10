"""Seed and index script for construction bot (ТЗ v2: З-5).

Requirements:
- No default hardcoded ADMIN_TG_ID. Fails if not set.
- No sec_level in manifest.
- Rejects ДСП files.
- Indexes sheets into doc_sheets.
"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path
from config import get_config
from db import Database
from pdf_engine import extract_document_sheets, detect_dsp_marker, extract_gost_stamp_info
from obsidian import sync_document_card

POSSIBLE_DIRS = [
    Path("/data/files"),
    Path(__file__).resolve().parent.parent / "storage" / "files",
    Path(__file__).resolve().parent.parent,
]


def find_file(filename: str) -> Path | None:
    for d in POSSIBLE_DIRS:
        p = d / filename
        if p.exists():
            return p
    return None


async def seed():
    raw_admin = os.getenv("ADMIN_TG_ID")
    if not raw_admin or not raw_admin.strip():
        print("❌ Ошибка: Переменная ADMIN_TG_ID обязательна для запуска seed_sheets.py", file=sys.stderr)
        sys.exit(1)

    try:
        admin_tg_id = int(raw_admin.strip())
        if admin_tg_id <= 0:
            raise ValueError()
    except ValueError:
        print(f"❌ Ошибка: ADMIN_TG_ID должен быть положительным числом, получено '{raw_admin}'", file=sys.stderr)
        sys.exit(1)

    db = Database()
    await db.connect()
    print("✅ Подключение к БД успешно.")

    # Load configuration
    try:
        cfg = get_config()
        await db.sync_config_with_db(cfg)
        print("✅ Синхронизация .env с БД завершена.")
    except Exception as e:
        print(f"⚠️ Предупреждение при синхронизации конфигурации: {e}")

    # Files to index (if present in storage)
    candidate_files = [
        "Э1075_ДСП_ЦТП_КМ10_Площадки_и_опоры.pdf",
        "Э756дспИ2.pdf",
        "Э795дсп.pdf",
        "Э801дсп_КЖ7.pdf",
        "kns.pdf",
    ]

    for fname in candidate_files:
        fpath = find_file(fname)
        if not fpath:
            continue

        # Check DSP marker
        if detect_dsp_marker(str(fpath), filename=fname):
            print(f"🚫 Файл {fname} содержит маркер ДСП — пропуск согласно политике безопасности.")
            continue

        raw_bytes = fpath.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        stamp = extract_gost_stamp_info(str(fpath), filename=fname)
        code = stamp.get("detected_code") or fpath.stem
        section = stamp.get("detected_section") or "КЖ"
        proj_code = stamp.get("suggested_project") or "ЦТП"

        saved_doc = await db.register_new_revision(
            project_code=proj_code,
            doc_type="РД",
            section=section,
            code=code,
            stamp_change_no="0",
            file_path=str(fpath),
            original_filename=fname,
            sha256=sha256,
            uploader_tg_id=admin_tg_id,
        )

        if isinstance(saved_doc, dict) and saved_doc.get("duplicate"):
            print(f"ℹ️ {fname} уже зарегистрирован (ID {saved_doc['id']}).")
            doc_id = saved_doc["id"]
        else:
            print(f"✅ Зарегистрирован {fname} -> {code} (ID {saved_doc['id']})")
            doc_id = saved_doc["id"]
            sync_document_card(saved_doc)

        sheets = extract_document_sheets(str(fpath), filename=fname, cipher=code)
        if sheets:
            await db.index_document_sheets(doc_id, sheets)
            print(f"   Проиндексировано листов: {len(sheets)}")

    print("🏁 Завершено.")


if __name__ == "__main__":
    asyncio.run(seed())
