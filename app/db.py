import asyncpg
import os
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
            # Создаём таблицу pending_uploads если её нет
            await conn.execute("""
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
                )
            """)

    async def ensure_admin(self, tg_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (tg_id, full_name, role, max_clearance)
                VALUES ($1, 'Главный Инженер (Admin)', 'admin', 'L2')
                ON CONFLICT (tg_id) DO UPDATE SET max_clearance = 'L2', role = 'admin'
            """, tg_id)
            # Привязываем админа ко всем объектам без ограничения по разделам
            await conn.execute("""
                INSERT INTO user_projects (user_id, project_id, project_role, allowed_sections)
                SELECT u.id, p.id, 'admin', '{}' FROM users u CROSS JOIN projects p
                WHERE u.tg_id = $1
                ON CONFLICT (user_id, project_id) DO NOTHING
            """, tg_id)

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
        ЖЕСТКИЙ ФИЛЬТР ПРАВ И СТРОГАЯ ВЫДАЧА ТОЛЬКО status = 'active':
        Проверяет привязку к объекту, допуск по грифу, секции и возвращает 
        ИСКЛЮЧИТЕЛЬНО последнюю активную ревизию с сортировкой по revision DESC.
        """
        allowed_levels = [
            lvl for lvl, rank in CLEARANCE_ORDER.items()
            if rank <= CLEARANCE_ORDER.get(user["max_clearance"], 0)
        ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, up.allowed_sections
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                JOIN user_projects up ON up.project_id = p.id
                WHERE up.user_id = $1
                  AND d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($3::varchar IS NULL OR d.doc_type ILIKE $3)
                  AND ($4::varchar IS NULL OR d.section ILIKE $4)
                  AND ($5::varchar IS NULL OR d.code ILIKE '%' || $5 || '%')
                  AND ($6::varchar IS NULL OR p.code ILIKE $6)
                  AND (cardinality(up.allowed_sections) = 0 OR d.section = ANY(up.allowed_sections))
                ORDER BY d.revision DESC, d.created_at DESC
                LIMIT 1
            """
            row = await conn.fetchrow(
                query, user["id"], allowed_levels, doc_type, section, query_code, project_code
            )
            return dict(row) if row else None

    async def list_active_docs_with_acl(
        self, user: dict, doc_type: str = None, section: str = None,
        query_code: str = None, project_code: str = None
    ) -> list[dict]:
        """Как find_active_doc_with_acl, но возвращает ВСЕ активные документы (для мульти-комплектного контекста)."""
        allowed_levels = [
            lvl for lvl, rank in CLEARANCE_ORDER.items()
            if rank <= CLEARANCE_ORDER.get(user["max_clearance"], 0)
        ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, up.allowed_sections
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                JOIN user_projects up ON up.project_id = p.id
                WHERE up.user_id = $1
                  AND d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                  AND ($3::varchar IS NULL OR d.doc_type ILIKE $3)
                  AND ($4::varchar IS NULL OR d.section ILIKE $4)
                  AND ($5::varchar IS NULL OR d.code ILIKE '%' || $5 || '%')
                  AND ($6::varchar IS NULL OR p.code ILIKE $6)
                  AND (cardinality(up.allowed_sections) = 0 OR d.section = ANY(up.allowed_sections))
                ORDER BY d.revision DESC, d.created_at DESC
            """
            rows = await conn.fetch(
                query, user["id"], allowed_levels, doc_type, section, query_code, project_code
            )
            return [dict(r) for r in rows]

    async def list_available_docs_summary(self, user: dict) -> list[dict]:
        """Возвращает список доступных пользователю активных документов по проектам."""
        allowed_levels = [
            lvl for lvl, rank in CLEARANCE_ORDER.items()
            if rank <= CLEARANCE_ORDER.get(user["max_clearance"], 0)
        ]
        async with self.pool.acquire() as conn:
            query = """
                SELECT p.code as project_code, d.code as doc_code, d.section, d.revision
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                JOIN user_projects up ON up.project_id = p.id
                WHERE up.user_id = $1
                  AND d.status = 'active'
                  AND d.security_level = ANY($2::varchar[])
                ORDER BY p.code, d.section, d.code
            """
            rows = await conn.fetch(query, user["id"], allowed_levels)
            return [dict(r) for r in rows]

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
