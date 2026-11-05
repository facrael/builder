from pathlib import Path
from datetime import datetime, timezone

VAULT_ROOT = Path("/data/obsidian_vault")

def sync_document_card(doc_record: dict):
    """
    Создает карточку документа в папке соответствующего грифа:
    /data/obsidian_vault/L0/_registry/123-24-КР1_rev2.md
    """
    sec_level = doc_record["security_level"]
    reg_dir = VAULT_ROOT / sec_level / "_registry"
    reg_dir.mkdir(parents=True, exist_ok=True)

    safe_code = doc_record["code"].replace("/", "_")
    filename = f"{safe_code}_rev{doc_record['revision']}.md"
    filepath = reg_dir / filename

    content = f"""---
id: {doc_record['id']}
project: "{doc_record['project_code']}"
type: "{doc_record['doc_type']}"
section: "{doc_record['section']}"
code: "{doc_record['code']}"
revision: {doc_record['revision']}
status: "{doc_record['status']}"
security_level: "{sec_level}"
sha256: "{doc_record['sha256']}"
updated_at: "{datetime.now(timezone.utc).isoformat()}"
---

# {doc_record['code']} (Изм. {doc_record['revision']})

- **Объект:** [[{doc_record['project_code']}]]
- **Раздел:** `{doc_record['section']}` ({doc_record['doc_type']})
- **Статус:** **{doc_record['status'].upper()}**
- **Гриф:** `{sec_level}`

## Связанные замечания и протоколы
> Инженеры ПТО могут ссылаться на этот документ в своих заметках через `[[{safe_code}_rev{doc_record['revision']}]]`.
"""
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)