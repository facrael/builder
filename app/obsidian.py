from pathlib import Path
from datetime import datetime, timezone
import re

VAULT_ROOT = Path("/data/obsidian_vault")


def sync_document_card(doc_record: dict) -> str:
    """
    Создает карточку документа в папке объекта (ТЗ v2: З-16):
    /data/obsidian_vault/<project_code>/_registry/<code_revN>.md
    """
    project_code = doc_record.get("project_code") or "DEFAULT"
    reg_dir = VAULT_ROOT / project_code / "_registry"
    reg_dir.mkdir(parents=True, exist_ok=True)

    safe_code = re.sub(r'[/\\?%*:|"<>]', '_', doc_record["code"])
    filename = f"{safe_code}_rev{doc_record['revision']}.md"
    filepath = reg_dir / filename

    stamp_change_no = doc_record.get("stamp_change_no") or "0"

    content = f"""---
id: {doc_record['id']}
project: "{project_code}"
type: "{doc_record['doc_type']}"
section: "{doc_record['section']}"
code: "{doc_record['code']}"
stamp_change_no: "{stamp_change_no}"
revision: {doc_record['revision']}
status: "{doc_record['status']}"
sha256: "{doc_record['sha256']}"
updated_at: "{datetime.now(timezone.utc).isoformat()}"
---

# {doc_record['code']} (Изм. {stamp_change_no}, Выпуск {doc_record['revision']})

- **Объект:** [[{project_code}]]
- **Раздел:** `{doc_record['section']}` ({doc_record['doc_type']})
- **Статус:** **{doc_record['status'].upper()}**
- **Изм. по штампу:** `{stamp_change_no}`
- **Порядковый выпуск:** `{doc_record['revision']}`

## Связанные замечания и протоколы
> Инженеры ПТО могут ссылаться на этот документ в своих заметках через `[[{safe_code}_rev{doc_record['revision']}]]`.
"""
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)
