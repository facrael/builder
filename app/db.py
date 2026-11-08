import asyncpg
import os
import re
import json
import secrets
from dataclasses import dataclass

DATABASE_URL = os.getenv("DATABASE_URL")
CLEARANCE_ORDER = {"L0": 0, "L1": 1, "L2": 2}


@dataclass
class PendingUpload:
    """Черновик загрузки, хранится в БД вместо in-memory dict."""
    project_code: str
    doc_type: str
    section: str
    code: str
    sec_level: str
    file_path: str
    sha256: str


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.pool.acquire() as conn:
            # Создаём расширения и таблицы
            await conn.execute("""
                CREATE EXTENSION IF NOT EXISTS "pg_trgm";
                CREATE EXTENSION IF NOT EXISTS "pgcrypto";

                CREATE TABLE IF NOT EXISTS pending_uploads (
                    tg_id BIGINT PRIMARY KEY,
                    project_code VARCHAR(50) NOT NULL,
                    doc_type VARCHAR(50) NOT NULL,
                    section VARCHAR(50) NOT NULL,
                    code VARCHAR(150) NOT NULL,
                    sec_level VARCHAR(10) NOT NULL DEFAULT 'L0',
                    file_path TEXT NOT NULL,
                    sha256 VARCHAR(64) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS doc_sheets (
                    id SERIAL PRIMARY KEY,
                    doc_id INT REFERENCES documents(id) ON DELETE CASCADE,
                    page_index INT NOT NULL,
                    sheet_number INT,
                    title TEXT NOT NULL,
                    tags TEXT[] DEFAULT '{}',
                    text_content TEXT DEFAULT '',
                    tsv tsvector,
                    is_landscape BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (doc_id, page_index)
                );

                CREATE INDEX IF NOT EXISTS idx_doc_sheets_lookup ON doc_sheets(doc_id, page_index);
                CREATE INDEX IF NOT EXISTS idx_doc_sheets_tsv ON doc_sheets USING gin(tsv);
                CREATE INDEX IF NOT EXISTS idx_doc_sheets_title_trgm ON doc_sheets USING gin(title gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS idx_documents_code_trgm ON documents USING gin(code gin_trgm_ops);
            """)

    async def ensure_whitelist_user(self, tg_id: int, full_name: str = "Инженер") -> dict:
        """Регистрирует/обновляет пользователя из белого списка с полным доступом (admin, L2)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO users (tg_id, full_name, role, max_clearance, is_active)
                VALUES ($1, $2, 'admin', 'L2', TRUE)
                ON CONFLICT (tg_id) DO UPDATE SET 
                    role = 'admin',
                    max_clearance = 'L2',
                    is_active = TRUE
                RETURNING *
            """, tg_id, full_name or "Инженер")
            user = dict(row)
            # Привязываем ко всем имеющимся объектам с полным доступом
            await conn.execute("""
                INSERT INTO user_projects (user_id, project_id, project_role, allowed_sections)
                SELECT $1, p.id, 'admin', '{}' FROM projects p
                ON CONFLICT (user_id, project_id) DO UPDATE SET
                    project_role = 'admin',
                    allowed_sections = '{}'
            """, user["id"])
            return user

    async def ensure_admin(self, tg_id: int):
        return await self.ensure_whitelist_user(tg_id, "Главный Инженер (Admin)")

    async def get_user(self, tg_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE tg_id = $1 AND is_active = TRUE", tg_id
            )
            return dict(row) if row else None

    async def create_invite(
        self, created_by_id: int, project_code: str, role: str,
        clearance: str = "L0", allowed_sections: list[str] = None
    ) -> str:
        """Создает разовый инвайт-код для привязки сотрудника по его TG ID."""
        code = f"INV-{secrets.token_hex(3).upper()}"
        allowed_sections = allowed_sections or []
        async with self.pool.acquire() as conn:
            proj = await conn.fetchrow("SELECT id FROM projects WHERE code = $1", project_code)
            if not proj:
                raise ValueError(f"Объект {project_code} не найден")
            
            await conn.execute("""
                INSERT INTO invite_codes (code, target_role, max_clearance, project_id, allowed_sections, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, code, role, clearance, proj["id"], allowed_sections, created_by_id)
            return code

    async def register_by_invite(self, tg_id: int, full_name: str, code: str) -> dict | None:
        """Активирует инвайт: создает профиль пользователя и привязывает к объекту."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                invite = await conn.fetchrow("""
                    SELECT * FROM invite_codes 
                    WHERE code = $1 AND used_by IS NULL AND expires_at > NOW()
                    FOR UPDATE
                """, code.strip())
                if not invite:
                    return None
                
                # Создаем пользователя
                user_row = await conn.fetchrow("""
                    INSERT INTO users (tg_id, full_name, role, max_clearance)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (tg_id) DO UPDATE SET 
                        role = EXCLUDED.role,
                        max_clearance = EXCLUDED.max_clearance,
                        is_active = TRUE
                    RETURNING *
                """, tg_id, full_name, invite["target_role"], invite["max_clearance"])
                
                user = dict(user_row)
                # Привязываем к проекту
                await conn.execute("""
                    INSERT INTO user_projects (user_id, project_id, project_role, allowed_sections)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, project_id) DO UPDATE SET
                        project_role = EXCLUDED.project_role,
                        allowed_sections = EXCLUDED.allowed_sections
                """, user["id"], invite["project_id"], invite["target_role"], invite["allowed_sections"])
                
                # Помечаем инвайт использованным
                await conn.execute(
                    "UPDATE invite_codes SET used_by = $1 WHERE id = $2", user["id"], invite["id"]
                )
                return user

    async def log_audit(self, user_id: int, action: str, doc_id: int | None, details: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (user_id, action, doc_id, details) VALUES ($1, $2, $3, $4)",
                user_id, action, doc_id, json.dumps(details, ensure_ascii=False)
            )

    async def get_or_create_project(self, code: str, name: str = None, user_id: int = None) -> dict:
        """Получает или автоматически регистрирует новый строительный объект."""
        clean_code = code.strip().upper()
        clean_name = name or f"Объект {clean_code}"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO projects (code, name) 
                VALUES ($1, $2) 
                ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code 
                RETURNING *
            """, clean_code, clean_name)
            proj = dict(row)
            
            # Если передан user_id (например ПТО/Админ), связываем его с новым проектом
            if user_id:
                await conn.execute("""
                    INSERT INTO user_projects (user_id, project_id, project_role, allowed_sections)
                    VALUES ($1, $2, 'pto', '{}')
                    ON CONFLICT (user_id, project_id) DO NOTHING
                """, user_id, proj["id"])
            return proj

    async def register_new_revision(
        self, project_code: str, doc_type: str, section: str,
        code: str, sec_level: str, file_path: str, sha256: str, user_id: int
    ) -> dict:
        """
        ГАРАНТИЯ АКТУАЛЬНОСТИ:
        Деактивирует старую ревизию ('superseded') и записывает новую ('active').
        Все внутри одной транзакции. Благодаря индексу idx_one_active_per_code 
        не может существовать двух 'active' версий одного шифра.
        """
        async with self.pool.acquire() as conn:
            # Тот же файл уже в реестре (уникальный sha256)? Не падаем по unique constraint.
            dup = await conn.fetchrow("""
                SELECT d.*, p.code AS project_code FROM documents d
                JOIN projects p ON p.id = d.project_id
                WHERE d.sha256 = $1
            """, sha256)
            if dup:
                res = dict(dup)
                res["duplicate"] = True
                return res

            async with conn.transaction():
                proj = await conn.fetchrow("SELECT id, code FROM projects WHERE code = $1", project_code)
                if not proj:
                    proj = await conn.fetchrow(
                        "INSERT INTO projects (code, name) VALUES ($1, $2) RETURNING *",
                        project_code, f"Объект {project_code}"
                    )
                    # Привязываем загрузившего пользователя к проекту
                    await conn.execute("""
                        INSERT INTO user_projects (user_id, project_id, project_role, allowed_sections)
                        VALUES ($1, $2, 'pto', '{}')
                        ON CONFLICT (user_id, project_id) DO NOTHING
                    """, user_id, proj["id"])

                # Ищем текущую активную ревизию этого шифра
                old_doc = await conn.fetchrow("""
                    SELECT id, revision FROM documents
                    WHERE project_id = $1 AND code = $2 AND status = 'active'
                    FOR UPDATE
                """, proj["id"], code)

                new_rev = (old_doc["revision"] + 1) if old_doc else 1

                if old_doc:
                    await conn.execute(
                        "UPDATE documents SET status = 'superseded' WHERE id = $1",
                        old_doc["id"]
                    )

                new_doc = await conn.fetchrow("""
                    INSERT INTO documents 
                    (project_id, doc_type, section, code, revision, status, security_level, file_path, sha256, uploaded_by)
                    VALUES ($1, $2, $3, $4, $5, 'active', $6, $7, $8, $9)
                    RETURNING *
                """, proj["id"], doc_type, section, code, new_rev, sec_level, file_path, sha256, user_id)

                res = dict(new_doc)
                res["project_code"] = proj["code"]
                return res

    async def find_active_doc_with_acl(
        self, user: dict, doc_type: str = None, section: str = None,
        query_code: str = None, project_code: str = None
    ) -> dict | None:
        """
        ФИЛЬТР ПРАВ И СТРОГАЯ ВЫДАЧА ТОЛЬКО status = 'active':
        Для белого списка / admin — полный доступ ко всем объектам и грифам (L0..L2).
        Для остальных — проверка допуска по грифу и связки с проектом.
        """
        is_admin = (user.get("role") == "admin")
        if is_admin:
            allowed_levels = ["L0", "L1", "L2"]
        else:
            allowed_levels = [
                lvl for lvl, rank in CLEARANCE_ORDER.items()
                if rank <= CLEARANCE_ORDER.get(user.get("max_clearance", "L0"), 0)
            ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, up.allowed_sections
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                LEFT JOIN user_projects up ON up.project_id = p.id AND up.user_id = $1
                WHERE d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($7::boolean OR (up.user_id IS NOT NULL AND (cardinality(up.allowed_sections) = 0 OR d.section = ANY(up.allowed_sections))))
                  AND ($3::varchar IS NULL OR d.doc_type ILIKE $3)
                  AND ($4::varchar IS NULL OR d.section ILIKE $4)
                  AND ($5::varchar IS NULL OR d.code ILIKE '%' || $5 || '%')
                  AND ($6::varchar IS NULL OR p.code ILIKE $6)
                ORDER BY d.revision DESC, d.created_at DESC
                LIMIT 1
            """
            row = await conn.fetchrow(
                query, user["id"], allowed_levels, doc_type, section, query_code, project_code, is_admin
            )
            return dict(row) if row else None

    async def list_active_docs_with_acl(
        self, user: dict, doc_type: str = None, section: str = None,
        query_code: str = None, project_code: str = None
    ) -> list[dict]:
        """Возвращает ВСЕ активные документы с учетом ACL (или все для admin)."""
        is_admin = (user.get("role") == "admin")
        if is_admin:
            allowed_levels = ["L0", "L1", "L2"]
        else:
            allowed_levels = [
                lvl for lvl, rank in CLEARANCE_ORDER.items()
                if rank <= CLEARANCE_ORDER.get(user.get("max_clearance", "L0"), 0)
            ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, up.allowed_sections
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                LEFT JOIN user_projects up ON up.project_id = p.id AND up.user_id = $1
                WHERE d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($7::boolean OR (up.user_id IS NOT NULL AND (cardinality(up.allowed_sections) = 0 OR d.section = ANY(up.allowed_sections))))
                  AND ($3::varchar IS NULL OR d.doc_type ILIKE $3)
                  AND ($4::varchar IS NULL OR d.section ILIKE $4)
                  AND ($5::varchar IS NULL OR d.code ILIKE '%' || $5 || '%')
                  AND ($6::varchar IS NULL OR p.code ILIKE $6)
                ORDER BY d.revision DESC, d.created_at DESC
            """
            rows = await conn.fetch(
                query, user["id"], allowed_levels, doc_type, section, query_code, project_code, is_admin
            )
            return [dict(r) for r in rows]

    async def list_available_docs_summary(self, user: dict) -> list[dict]:
        """Возвращает список доступных пользователю активных документов по проектам."""
        is_admin = (user.get("role") == "admin")
        if is_admin:
            allowed_levels = ["L0", "L1", "L2"]
        else:
            allowed_levels = [
                lvl for lvl, rank in CLEARANCE_ORDER.items()
                if rank <= CLEARANCE_ORDER.get(user.get("max_clearance", "L0"), 0)
            ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT p.code as project_code, d.code as doc_code, d.section, d.revision
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                LEFT JOIN user_projects up ON up.project_id = p.id AND up.user_id = $1
                WHERE d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($3::boolean OR up.user_id IS NOT NULL)
                ORDER BY p.code, d.section, d.code
            """
            rows = await conn.fetch(query, user["id"], allowed_levels, is_admin)
            return [dict(r) for r in rows]

    async def index_document_sheets(self, doc_id: int, sheets: list[dict]):
        """
        Сохраняет/обновляет постраничный реестр листов чертежей в doc_sheets.
        Генерирует tsvector полнотекстового поиска с весами:
        'A' - заголовок листа (title)
        'B' - теги листа (tags)
        'C' - текстовый контент листа (text_content)
        """
        async with self.pool.acquire() as conn:
            for s in sheets:
                tags = s.get("tags") or []
                text_content = s.get("text_content") or ""
                title = s.get("title") or ""
                sheet_number = s.get("sheet_number")
                page_index = s["page_index"]
                is_landscape = s.get("is_landscape", True)

                await conn.execute("""
                    INSERT INTO doc_sheets (
                        doc_id, page_index, sheet_number, title, tags, text_content, tsv, is_landscape
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6,
                        setweight(to_tsvector('russian', coalesce($4, '')), 'A') ||
                        setweight(to_tsvector('russian', array_to_string($5::text[], ' ')), 'B') ||
                        setweight(to_tsvector('russian', coalesce($6, '')), 'C'),
                        $7
                    )
                    ON CONFLICT (doc_id, page_index) DO UPDATE SET
                        sheet_number = EXCLUDED.sheet_number,
                        title = EXCLUDED.title,
                        tags = EXCLUDED.tags,
                        text_content = EXCLUDED.text_content,
                        tsv = setweight(to_tsvector('russian', coalesce(EXCLUDED.title, '')), 'A') ||
                              setweight(to_tsvector('russian', array_to_string(EXCLUDED.tags, ' ')), 'B') ||
                              setweight(to_tsvector('russian', coalesce(EXCLUDED.text_content, '')), 'C'),
                        is_landscape = EXCLUDED.is_landscape
                """, doc_id, page_index, sheet_number, title, tags, text_content, is_landscape)

    async def search_sheets_fts(
        self, user: dict, query: str, project_code: str = None, section: str = None
    ) -> dict | None:
        """
        Мгновенный поиск листа через полнотекстовый индекс tsvector + pg_trgm.
        Возвращает самый релевантный лист за 1-2 мс без вызова нейросетей.
        """
        stop_words = {
            'дай', 'где', 'как', 'что', 'это', 'какая', 'какой', 'скажи', 
            'покажи', 'по', 'нему', 'для', 'или', 'на', 'есть', 'ли', 'мне',
            'пожалуйста', 'скинь', 'выдай', 'нужен', 'нужна', 'нужны', 'чертеж', 'чертежи'
        }
        # Синонимы строительных терминов: крыша -> кровля и т.п.
        synonyms = {
            'крыша': 'кровля', 'крышу': 'кровля', 'крыши': 'кровля', 'крышей': 'кровля',
            'кровле': 'кровля', 'кровлю': 'кровля', 'кровли': 'кровля',
            'фундаменты': 'фундамент', 'ростверки': 'ростверк',
            'фасады': 'фасад', 'перекрытия': 'перекрытие', 'сваи': 'свая',
        }
        raw_tokens = [w for w in re.findall(r'[а-яa-z0-9]{2,}', query, re.I) if w.lower() not in stop_words]
        tokens = []
        for w in raw_tokens:
            wl = w.lower()
            tokens.append(synonyms.get(wl, wl))
        if not tokens:
            return None
        
        clean_q = " ".join(tokens)
        query_tokens = [w.lower() for w in tokens]

        is_admin = (user.get("role") == "admin")
        allowed_levels = ["L0", "L1", "L2"] if is_admin else [
            lvl for lvl, rank in CLEARANCE_ORDER.items()
            if rank <= CLEARANCE_ORDER.get(user.get("max_clearance", "L0"), 0)
        ]

        async with self.pool.acquire() as conn:
            sql = """
                SELECT ds.*, d.code as doc_code, d.revision, d.section, d.file_path, d.security_level,
                       p.code as project_code,
                       ts_rank(ds.tsv, plainto_tsquery('russian', $1)) as rank,
                       ts_rank(to_tsvector('russian', coalesce(ds.title, '')), plainto_tsquery('russian', $1)) as title_rank,
                       similarity(ds.title, $1) as sim
                FROM doc_sheets ds
                JOIN documents d ON ds.doc_id = d.id
                JOIN projects p ON d.project_id = p.id
                WHERE d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($3::varchar IS NULL OR p.code ILIKE $3)
                  AND ($4::varchar IS NULL OR d.section ILIKE $4)
                  AND (
                      ds.tsv @@ plainto_tsquery('russian', $1)
                      OR ds.title ILIKE '%' || $1 || '%'
                      OR ds.tags && $5::text[]
                      OR similarity(ds.title, $1) > 0.15
                  )
                ORDER BY (title_rank * 5 + rank + sim) DESC, ds.sheet_number ASC
                LIMIT 1
            """
            row = await conn.fetchrow(sql, clean_q, allowed_levels, project_code, section, query_tokens)
            return dict(row) if row else None

    async def get_project_sheets_map(self, user: dict, project_code: str = None) -> str:
        """
        Формирует структурированную текстовую карту доступных комплектов и листов (per-user, post-ACL).
        Занимает всего 1-3 тыс. токенов и передается в LLM как компактная карта проекта.
        """
        docs = await self.list_active_docs_with_acl(user, project_code=project_code)
        if not docs:
            return "В реестре нет доступных активных документов по данному запросу."

        doc_ids = [d["id"] for d in docs]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT doc_id, page_index, sheet_number, title, tags
                FROM doc_sheets
                WHERE doc_id = ANY($1::int[])
                ORDER BY doc_id, page_index ASC
            """, doc_ids)

        sheets_by_doc = {}
        for r in rows:
            sheets_by_doc.setdefault(r["doc_id"], []).append(dict(r))

        by_proj = {}
        for d in docs:
            by_proj.setdefault(d["project_code"], []).append(d)

        lines = ["=== КАРТА ПРОЕКТА (РЕЕСТР ДОСТУПНЫХ ЛИСТОВ) ==="]
        for p_code, p_docs in by_proj.items():
            lines.append(f"\n🏗️ Объект: {p_code}")
            for d in p_docs:
                d_sheets = sheets_by_doc.get(d["id"], [])
                sec_lvl = d.get("security_level", "L0")
                lines.append(
                    f"  📄 Комплект [{d['code']}] (Раздел {d['section']}, Изм. {d['revision']}, Гриф {sec_lvl}):"
                )
                if not d_sheets:
                    lines.append("     (состав листов не индексирован)")
                else:
                    for s in d_sheets:
                        sh_num = s["sheet_number"]
                        sh_str = f"Лист {sh_num}" if sh_num else f"Стр. {s['page_index'] + 1}"
                        p_idx = s["page_index"] + 1
                        tags_str = f" [{', '.join(s['tags'])}]" if s.get("tags") else ""
                        lines.append(f"     • {sh_str} (PDF-стр. {p_idx}): {s['title']}{tags_str}")

        return "\n".join(lines)

    async def save_pending_upload(self, tg_id: int, upload: PendingUpload):
        """Сохраняет черновик загрузки в БД (UPSERT)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pending_uploads (tg_id, project_code, doc_type, section, code, sec_level, file_path, sha256)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (tg_id) DO UPDATE SET
                    project_code = EXCLUDED.project_code,
                    doc_type = EXCLUDED.doc_type,
                    section = EXCLUDED.section,
                    code = EXCLUDED.code,
                    sec_level = EXCLUDED.sec_level,
                    file_path = EXCLUDED.file_path,
                    sha256 = EXCLUDED.sha256,
                    created_at = NOW()
            """, tg_id, upload.project_code, upload.doc_type, upload.section,
                upload.code, upload.sec_level, upload.file_path, upload.sha256)

    async def pop_pending_upload(self, tg_id: int) -> PendingUpload | None:
        """Забирает и удаляет черновик загрузки из БД."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM pending_uploads WHERE tg_id = $1 RETURNING *", tg_id
            )
            if not row:
                return None
            return PendingUpload(
                project_code=row["project_code"],
                doc_type=row["doc_type"],
                section=row["section"],
                code=row["code"],
                sec_level=row["sec_level"],
                file_path=row["file_path"],
                sha256=row["sha256"],
            )
