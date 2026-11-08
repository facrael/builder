"""
Скрипт первичной миграции и индексации 5 файлов комплектов РД в таблицу doc_sheets.
Запуск внутри контейнера:
  docker exec builder-bot-1 python seed_sheets.py
Или локально при настроенном DATABASE_URL.
"""
import asyncio
import os
import hashlib
from pathlib import Path
from db import Database
from pdf_engine import extract_document_sheets
from obsidian import sync_document_card

DOCS_MANIFEST = [
    {
        "filename": "Э1075_ДСП_ЦТП_КМ10.pdf",
        "code": "ЦКДИ.2981.КМ10",
        "project_code": "ЦТП",
        "section": "КМ",
        "doc_type": "РД",
        "sec_level": "L1",
        "revision": 1,
    },
    {
        "filename": "Э756дспИ2.pdf",
        "code": "ЦКДИ.2981.АР5",
        "project_code": "ЦТП",
        "section": "АР",
        "doc_type": "РД",
        "sec_level": "L0",
        "revision": 2,
    },
    {
        "filename": "Э795дсп.pdf",
        "code": "ЦКДИ.2981.КЖ",
        "project_code": "ЦТП",
        "section": "КЖ",
        "doc_type": "РД",
        "sec_level": "L0",
        "revision": 1,
    },
    {
        "filename": "Э801дсп_КЖ7.pdf",
        "code": "ЦКДИ.2981.КЖ7",
        "project_code": "ЦТП",
        "section": "КЖ",
        "doc_type": "РД",
        "sec_level": "L0",
        "revision": 1,
    },
    {
        "filename": "kns.pdf",
        "code": "ЦКДИ.2981.АР3",
        "project_code": "КНС",
        "section": "АР",
        "doc_type": "РД",
        "sec_level": "L0",
        "revision": 1,
    },
]

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
    db = Database()
    await db.connect()
    print("✅ Подключение к БД успешно.")

    # Обеспечиваем админа
    admin_id = int(os.getenv("ADMIN_TG_ID", "353745073"))
    user = await db.ensure_whitelist_user(admin_id, "Главный Инженер (Admin)")

    for item in DOCS_MANIFEST:
        fpath = find_file(item["filename"])
        if not fpath:
            print(f"⚠️ Файл {item['filename']} не найден, пропускаем.")
            continue

        raw_bytes = fpath.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        # Регистрируем документ
        saved_doc = await db.register_new_revision(
            project_code=item["project_code"],
            doc_type=item["doc_type"],
            section=item["section"],
            code=item["code"],
            sec_level=item["sec_level"],
            file_path=str(fpath),
            sha256=sha256,
            user_id=user["id"]
        )

        doc_id = saved_doc["id"]
        print(f"📄 Документ зарегистрирован: ID {doc_id} | {item['code']} ({item['project_code']})")

        # Извлекаем и индексируем листы в doc_sheets
        sheets = extract_document_sheets(str(fpath), filename=item["filename"], cipher=item["code"])
        if sheets:
            await db.index_document_sheets(doc_id, sheets)
            print(f"   📐 Проиндексировано листов: {len(sheets)}")

        # Синхронизируем карточку в Obsidian
        card = sync_document_card(saved_doc)
        print(f"   📝 Карточка Obsidian: {card}")

    # Проверка карты проекта
    print("\n" + "=" * 60)
    project_map = await db.get_project_sheets_map(user)
    print(project_map)
    print("=" * 60)
    print("🎉 Миграция и индексация 5 файлов успешно завершена!")

if __name__ == "__main__":
    asyncio.run(seed())
