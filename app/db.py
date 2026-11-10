"""Database access layer and ACL enforcement for construction bot (ТЗ v2).

Enforces:
- Direct access control by user_projects (synchronized from .env whitelist).
- No roles, no L0/L1/L2 clearance levels.
- System admin (ADMIN_TG_ID) does not bypass project ACL.
- Append-only audit logging.
- FTS + pg_trgm sheet search with homoglyph normalization.
"""
from __future__ import annotations
import asyncpg
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


@dataclass
class PendingUpload:
    file_id: str
    tg_id: int
    project_code: str
    doc_type: str
    section: str
    code: str
    stamp_change_no: str
    temp_path: str
    original_filename: str
    sha256: str


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self.apply_migrations()

    async def apply_migrations(self):
        """Applies schema migrations, dropping obsolete v1 concepts."""
        async with self.pool.acquire() as conn:
            # 1. Extensions
            await conn.execute("""
                CREATE EXTENSION IF NOT EXISTS "pg_trgm";
                CREATE EXTENSION IF NOT EXISTS "pgcrypto";
            """)

            # 2. Schema migrations table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INT PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            applied = await conn.fetch("SELECT version FROM schema_migrations")
            applied_versions = {r["version"] for r in applied}

            # Migration 1: Baseline v2 schema
            if 1 not in applied_versions:
                async with conn.transaction():
                    # Users: drop obsolete columns if present
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            id SERIAL PRIMARY KEY,
                            tg_id BIGINT UNIQUE NOT NULL,
                            full_name VARCHAR(255) NOT NULL,
                            phone VARCHAR(20),
                            position VARCHAR(255),
                            is_active BOOLEAN DEFAULT TRUE,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                        ALTER TABLE users DROP COLUMN IF EXISTS role;
                        ALTER TABLE users DROP COLUMN IF EXISTS max_clearance;
                    """)

                    # Identities
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS identities (
                            id SERIAL PRIMARY KEY,
                            channel VARCHAR(32) NOT NULL DEFAULT 'telegram',
                            external_id VARCHAR(64) NOT NULL,
                            user_id INT REFERENCES users(id) ON DELETE CASCADE,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(channel, external_id)
                        );
                    """)

                    # Projects
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS projects (
                            id SERIAL PRIMARY KEY,
                            code VARCHAR(50) UNIQUE NOT NULL,
                            name VARCHAR(255) NOT NULL,
                            cloud_ok BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                        ALTER TABLE projects ADD COLUMN IF NOT EXISTS cloud_ok BOOLEAN DEFAULT FALSE;
                    """)

                    # User Projects
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS user_projects (
                            user_id INT REFERENCES users(id) ON DELETE CASCADE,
                            project_id INT REFERENCES projects(id) ON DELETE CASCADE,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            PRIMARY KEY (user_id, project_id)
                        );
                        ALTER TABLE user_projects DROP COLUMN IF EXISTS project_role;
                        ALTER TABLE user_projects DROP COLUMN IF EXISTS allowed_sections;
                    """)

                    # Documents: quarantine former L1/L2 and drop security_level
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS documents (
                            id SERIAL PRIMARY KEY,
                            project_id INT REFERENCES projects(id),
                            doc_type VARCHAR(50) NOT NULL,
                            section VARCHAR(50) NOT NULL,
                            code VARCHAR(150) NOT NULL,
                            stamp_change_no VARCHAR(20) DEFAULT '0',
                            revision INT NOT NULL DEFAULT 1,
                            status VARCHAR(50) NOT NULL DEFAULT 'active',
                            file_path TEXT NOT NULL,
                            original_filename TEXT,
                            sha256 VARCHAR(64) NOT NULL,
                            uploaded_by INT REFERENCES users(id),
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                        ALTER TABLE documents ADD COLUMN IF NOT EXISTS stamp_change_no VARCHAR(20) DEFAULT '0';
                        ALTER TABLE documents ADD COLUMN IF NOT EXISTS original_filename TEXT;
                    """)

                    # Check if security_level column exists in documents
                    col_exists = await conn.fetchval("""
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name = 'documents' AND column_name = 'security_level'
                    """)
                    if col_exists:
                        # Move all L1/L2 documents to quarantine (З-18)
                        await conn.execute("""
                            UPDATE documents 
                            SET status = 'quarantine' 
                            WHERE security_level IN ('L1', 'L2') AND status = 'active';
                            ALTER TABLE documents DROP COLUMN security_level;
                        """)

                    # Drop old invite_codes table (З-2)
                    await conn.execute("DROP TABLE IF EXISTS invite_codes CASCADE;")

                    # Pending uploads
                    await conn.execute("""
                        DROP TABLE IF EXISTS pending_uploads;
                        CREATE TABLE pending_uploads (
                            file_id VARCHAR(100) PRIMARY KEY,
                            tg_id BIGINT NOT NULL,
                            project_code VARCHAR(50) NOT NULL,
                            doc_type VARCHAR(50) NOT NULL,
                            section VARCHAR(50) NOT NULL,
                            code VARCHAR(150) NOT NULL,
                            stamp_change_no VARCHAR(20) DEFAULT '0',
                            temp_path TEXT NOT NULL,
                            original_filename TEXT,
                            sha256 VARCHAR(64) NOT NULL,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)

                    # Doc sheets
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS doc_sheets (
                            id SERIAL PRIMARY KEY,
                            doc_id INT REFERENCES documents(id) ON DELETE CASCADE,
                            page_index INT NOT NULL,
                            sheet_number INT,
                            title TEXT NOT NULL,
                            tags TEXT[] DEFAULT '{}',
                            text_content TEXT DEFAULT '',
                            codes TEXT[] DEFAULT '{}',
                            tsv tsvector,
                            is_landscape BOOLEAN DEFAULT TRUE,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE (doc_id, page_index)
                        );
                        ALTER TABLE doc_sheets ADD COLUMN IF NOT EXISTS codes TEXT[] DEFAULT '{}';
                    """)

                    # Audit log
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS audit_log (
                            id BIGSERIAL PRIMARY KEY,
                            user_tg_id BIGINT,
                            action VARCHAR(100) NOT NULL,
                            doc_id INT,
                            details JSONB,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)

                    # View visible_documents
                    await conn.execute("""
                        CREATE OR REPLACE VIEW visible_documents AS
                        SELECT d.*, p.code as project_code, p.name as project_name, p.cloud_ok as project_cloud_ok, up.user_id as allowed_user_id
                        FROM documents d
                        JOIN projects p ON d.project_id = p.id
                        JOIN user_projects up ON p.id = up.project_id
                        WHERE d.status != 'quarantine';
                    """)

                    # Record migration
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (1, 'v2_baseline_acl')"
                    )
                    logger.info("Database migration 1 (v2_baseline_acl) applied successfully.")

    async def sync_config_with_db(self, config: AppConfig):
        """Synchronizes database state strictly with .env config on startup (З-2).

        - Upserts projects from config (code, name, cloud_ok).
        - Upserts users and identities for all IDs in config.
        - Synchronizes user_projects bindings.
        - Deactivates users removed from all whitelists.
        - Logs differences to audit_log.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Sync projects
                for pcode, pcfg in config.projects.items():
                    await conn.execute("""
                        INSERT INTO projects (code, name, cloud_ok)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (code) DO UPDATE SET
                            name = EXCLUDED.name,
                            cloud_ok = EXCLUDED.cloud_ok
                    """, pcfg.code, pcfg.name, pcfg.cloud_ok)

                # Fetch all current DB projects
                db_projs = await conn.fetch("SELECT id, code FROM projects")
                proj_map = {r["code"]: r["id"] for r in db_projs}

                # 2. Collect all active TG IDs from config (project users + admin)
                all_config_tg_ids: set[int] = set()
                for pcfg in config.projects.values():
                    all_config_tg_ids.update(pcfg.users)
                if config.admin_tg_id:
                    all_config_tg_ids.add(config.admin_tg_id)

                # Ensure users and identities exist in DB
                for tg_id in all_config_tg_ids:
                    user_row = await conn.fetchrow("""
                        INSERT INTO users (tg_id, full_name, is_active)
                        VALUES ($1, $2, TRUE)
                        ON CONFLICT (tg_id) DO UPDATE SET is_active = TRUE
                        RETURNING id
                    """, tg_id, f"Пользователь {tg_id}")

                    uid = user_row["id"]
                    await conn.execute("""
                        INSERT INTO identities (channel, external_id, user_id)
                        VALUES ('telegram', $1, $2)
                        ON CONFLICT (channel, external_id) DO UPDATE SET user_id = EXCLUDED.user_id
                    """, str(tg_id), uid)

                # 3. Synchronize user_projects
                # Build desired links: set of (user_id, project_id)
                db_users = await conn.fetch("SELECT id, tg_id FROM users")
                tg_to_user_id = {r["tg_id"]: r["id"] for r in db_users}

                desired_links: set[tuple[int, int]] = set()
                for pcode, pcfg in config.projects.items():
                    pid = proj_map.get(pcode)
                    if not pid:
                        continue
                    for tg_id in pcfg.users:
                        uid = tg_to_user_id.get(tg_id)
                        if uid:
                            desired_links.add((uid, pid))

                current_links_rows = await conn.fetch("SELECT user_id, project_id FROM user_projects")
                current_links = {(r["user_id"], r["project_id"]) for r in current_links_rows}

                added_links = desired_links - current_links
                removed_links = current_links - desired_links

                for uid, pid in added_links:
                    await conn.execute(
                        "INSERT INTO user_projects (user_id, project_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        uid, pid
                    )

                for uid, pid in removed_links:
                    await conn.execute(
                        "DELETE FROM user_projects WHERE user_id = $1 AND project_id = $2",
                        uid, pid
                    )

                # 4. Deactivate users who are not in any project and not admin
                active_users_in_projects = {uid for uid, _ in desired_links}
                if config.admin_tg_id and config.admin_tg_id in tg_to_user_id:
                    active_users_in_projects.add(tg_to_user_id[config.admin_tg_id])

                _ = await conn.execute("""
                    UPDATE users SET is_active = FALSE 
                    WHERE id != ALL($1::int[]) AND is_active = TRUE
                """, list(active_users_in_projects))

                # 5. Clean up pending_uploads for removed users
                await conn.execute("""
                    DELETE FROM pending_uploads 
                    WHERE tg_id != ALL($1::bigint[])
                """, list(all_config_tg_ids))

                # 6. Audit log difference
                diff_summary = {
                    "added_links_count": len(added_links),
                    "removed_links_count": len(removed_links),
                    "active_users_count": len(all_config_tg_ids),
                }
                await conn.execute("""
                    INSERT INTO audit_log (user_tg_id, action, details)
                    VALUES ($1, 'CONFIG_SYNC', $2)
                """, config.admin_tg_id, json.dumps(diff_summary))

    async def log_audit(self, user_tg_id: int | None, action: str, doc_id: int | None = None, details: dict | None = None):
        """Append-only audit logger."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (user_tg_id, action, doc_id, details) VALUES ($1, $2, $3, $4)",
                user_tg_id, action, doc_id, json.dumps(details or {}, ensure_ascii=False)
            )

    async def get_user_by_tg_id(self, tg_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE tg_id = $1 AND is_active = TRUE", tg_id
            )
            return dict(row) if row else None

    async def get_user_allowed_project_codes(self, tg_id: int) -> list[str]:
        """Returns project codes accessible to the given user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.code 
                FROM projects p
                JOIN user_projects up ON up.project_id = p.id
                JOIN users u ON u.id = up.user_id
                WHERE u.tg_id = $1 AND u.is_active = TRUE
                ORDER BY p.code
            """, tg_id)
            return [r["code"] for r in rows]

    async def can_access(self, tg_id: int, project_code: str) -> bool:
        """Single ACL check (З-4): user must belong to project in user_projects."""
        if not project_code:
            return False
        codes = await self.get_user_allowed_project_codes(tg_id)
        return project_code.upper() in codes

    async def find_active_doc(
        self,
        tg_id: int,
        project_code: str | None = None,
        doc_type: str | None = None,
        section: str | None = None,
        code: str | None = None,
    ) -> dict | None:
        """Finds active document respecting user's allowed projects (never skips ACL)."""
        allowed_projects = await self.get_user_allowed_project_codes(tg_id)
        if not allowed_projects:
            return None

        if project_code:
            if project_code.upper() not in allowed_projects:
                return None
            search_projects = [project_code.upper()]
        else:
            search_projects = allowed_projects

        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, p.name as project_name, p.cloud_ok as project_cloud_ok
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                WHERE d.status = 'active'
                  AND p.code = ANY($1::varchar[])
                  AND ($2::varchar IS NULL OR d.doc_type ILIKE $2)
                  AND ($3::varchar IS NULL OR d.section ILIKE $3)
                  AND ($4::varchar IS NULL OR d.code ILIKE '%' || $4 || '%')
                ORDER BY d.revision DESC, d.created_at DESC
                LIMIT 1
            """
            row = await conn.fetchrow(query, search_projects, doc_type, section, code)
            return dict(row) if row else None

    async def list_active_docs(
        self,
        tg_id: int,
        project_code: str | None = None,
        doc_type: str | None = None,
        section: str | None = None,
        code: str | None = None,
    ) -> list[dict]:
        """Returns all active documents matching filters within user's allowed projects."""
        allowed_projects = await self.get_user_allowed_project_codes(tg_id)
        if not allowed_projects:
            return []

        if project_code:
            if project_code.upper() not in allowed_projects:
                return []
            search_projects = [project_code.upper()]
        else:
            search_projects = allowed_projects

        async with self.pool.acquire() as conn:
            query = """
                SELECT d.*, p.code as project_code, p.name as project_name, p.cloud_ok as project_cloud_ok,
                       u.full_name as uploader_name
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                LEFT JOIN users u ON d.uploaded_by = u.id
                WHERE d.status = 'active'
                  AND p.code = ANY($1::varchar[])
                  AND ($2::varchar IS NULL OR d.doc_type ILIKE $2)
                  AND ($3::varchar IS NULL OR d.section ILIKE $3)
                  AND ($4::varchar IS NULL OR d.code ILIKE '%' || $4 || '%')
                ORDER BY p.code ASC, d.section ASC, d.code ASC, d.revision DESC
            """
            rows = await conn.fetch(query, search_projects, doc_type, section, code)
            return [dict(r) for r in rows]

    async def list_available_docs_summary(self, tg_id: int) -> list[dict]:
        """Returns active documents summary strictly for the user's allowed projects."""
        allowed_projects = await self.get_user_allowed_project_codes(tg_id)
        if not allowed_projects:
            return []

        async with self.pool.acquire() as conn:
            query = """
                SELECT p.code as project_code, d.code as doc_code, d.section, d.revision, d.stamp_change_no
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                WHERE d.status = 'active'
                  AND p.code = ANY($1::varchar[])
                ORDER BY p.code, d.section, d.code
            """
            rows = await conn.fetch(query, allowed_projects)
            return [dict(r) for r in rows]

    async def register_new_revision(
        self,
        project_code: str,
        doc_type: str,
        section: str,
        code: str,
        stamp_change_no: str,
        file_path: str,
        original_filename: str,
        sha256: str,
        uploader_tg_id: int,
    ) -> dict:
        """Atomically registers a new revision of a document.

        - Checks deduplication by sha256 within the project.
        - Updates previous active version of this code to 'superseded'.
        - Inserts new document as 'active'.
        """
        async with self.pool.acquire() as conn:
            p_row = await conn.fetchrow("SELECT id, code FROM projects WHERE code = $1", project_code.upper())
            if not p_row:
                raise ValueError(f"Проект '{project_code}' не найден в системе.")

            proj_id = p_row["id"]

            # Deduplication within project (З-6)
            dup = await conn.fetchrow("""
                SELECT d.*, p.code AS project_code FROM documents d
                JOIN projects p ON p.id = d.project_id
                WHERE d.project_id = $1 AND d.sha256 = $2
            """, proj_id, sha256)
            if dup:
                res = dict(dup)
                res["duplicate"] = True
                return res

            user_row = await conn.fetchrow("SELECT id FROM users WHERE tg_id = $1", uploader_tg_id)
            user_id = user_row["id"] if user_row else None

            async with conn.transaction():
                old_doc = await conn.fetchrow("""
                    SELECT id, revision FROM documents
                    WHERE project_id = $1 AND code = $2 AND status = 'active'
                    FOR UPDATE
                """, proj_id, code)

                new_rev = (old_doc["revision"] + 1) if old_doc else 1

                if old_doc:
                    await conn.execute(
                        "UPDATE documents SET status = 'superseded' WHERE id = $1",
                        old_doc["id"]
                    )

                new_doc = await conn.fetchrow("""
                    INSERT INTO documents 
                    (project_id, doc_type, section, code, stamp_change_no, revision, status, file_path, original_filename, sha256, uploaded_by)
                    VALUES ($1, $2, $3, $4, $5, $6, 'active', $7, $8, $9, $10)
                    RETURNING *
                """, proj_id, doc_type, section, code, stamp_change_no, new_rev, file_path, original_filename, sha256, user_id)

                res = dict(new_doc)
                res["project_code"] = p_row["code"]
                return res

    async def update_document_metadata(
        self,
        doc_id: int,
        new_project_code: str,
        new_section: str,
        new_code: str,
        new_stamp_change_no: str,
        user_tg_id: int,
    ) -> dict:
        """Updates document card metadata and records audit log before/after (З-7)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                old_row = await conn.fetchrow("""
                    SELECT d.*, p.code as project_code 
                    FROM documents d 
                    JOIN projects p ON d.project_id = p.id
                    WHERE d.id = $1
                    FOR UPDATE
                """, doc_id)
                if not old_row:
                    raise ValueError("Документ не найден.")

                new_p_row = await conn.fetchrow("SELECT id, code FROM projects WHERE code = $1", new_project_code.upper())
                if not new_p_row:
                    raise ValueError(f"Проект {new_project_code} не существует.")

                updated = await conn.fetchrow("""
                    UPDATE documents
                    SET project_id = $1, section = $2, code = $3, stamp_change_no = $4
                    WHERE id = $5
                    RETURNING *
                """, new_p_row["id"], new_section, new_code, new_stamp_change_no, doc_id)

                details = {
                    "before": {
                        "project_code": old_row["project_code"],
                        "section": old_row["section"],
                        "code": old_row["code"],
                        "stamp_change_no": old_row["stamp_change_no"],
                    },
                    "after": {
                        "project_code": new_p_row["code"],
                        "section": new_section,
                        "code": new_code,
                        "stamp_change_no": new_stamp_change_no,
                    }
                }
                await conn.execute("""
                    INSERT INTO audit_log (user_tg_id, action, doc_id, details)
                    VALUES ($1, 'EDIT_CARD', $2, $3)
                """, user_tg_id, doc_id, json.dumps(details, ensure_ascii=False))

                res = dict(updated)
                res["project_code"] = new_p_row["code"]
                return res

    async def index_document_sheets(self, doc_id: int, sheets: list[dict]):
        """Saves page sheet registry with weighted tsvector and normalized codes (З-17)."""
        async with self.pool.acquire() as conn:
            for s in sheets:
                tags = s.get("tags") or []
                text_content = s.get("text_content") or ""
                title = s.get("title") or ""
                codes = s.get("codes") or []
                sheet_number = s.get("sheet_number")
                page_index = s["page_index"]
                is_landscape = s.get("is_landscape", True)

                await conn.execute("""
                    INSERT INTO doc_sheets (
                        doc_id, page_index, sheet_number, title, tags, text_content, codes, tsv, is_landscape
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7,
                        setweight(to_tsvector('russian', coalesce($4, '')), 'A') ||
                        setweight(to_tsvector('russian', array_to_string($5::text[], ' ')), 'B') ||
                        setweight(to_tsvector('russian', coalesce($6, '')), 'C'),
                        $8
                    )
                    ON CONFLICT (doc_id, page_index) DO UPDATE SET
                        sheet_number = EXCLUDED.sheet_number,
                        title = EXCLUDED.title,
                        tags = EXCLUDED.tags,
                        text_content = EXCLUDED.text_content,
                        codes = EXCLUDED.codes,
                        tsv = setweight(to_tsvector('russian', coalesce(EXCLUDED.title, '')), 'A') ||
                              setweight(to_tsvector('russian', array_to_string(EXCLUDED.tags, ' ')), 'B') ||
                              setweight(to_tsvector('russian', coalesce(EXCLUDED.text_content, '')), 'C'),
                        is_landscape = EXCLUDED.is_landscape
                """, doc_id, page_index, sheet_number, title, tags, text_content, codes, is_landscape)

    async def search_sheets_fts(
        self,
        tg_id: int,
        query: str,
        project_code: str | None = None,
        section: str | None = None
    ) -> dict | None:
        """Fast FTS + trigram sheet search strictly limited by user's allowed projects."""
        allowed_projects = await self.get_user_allowed_project_codes(tg_id)
        if not allowed_projects:
            return None

        if project_code:
            if project_code.upper() not in allowed_projects:
                return None
            search_projects = [project_code.upper()]
        else:
            search_projects = allowed_projects

        stop_words = {
            'дай', 'где', 'как', 'что', 'это', 'какая', 'какой', 'скажи',
            'покажи', 'по', 'нему', 'для', 'или', 'на', 'есть', 'ли', 'мне',
            'пожалуйста', 'скинь', 'выдай', 'нужен', 'нужна', 'нужны', 'чертеж', 'чертежи'
        }
        synonyms = {
            'крыша': 'кровля', 'крышу': 'кровля', 'крыши': 'кровля', 'крышей': 'кровля',
            'кровле': 'кровля', 'кровлю': 'кровля', 'кровли': 'кровля',
            'фундаменты': 'фундамент', 'ростверки': 'ростверк',
            'фасады': 'фасад', 'перекрытия': 'перекрытие', 'сваи': 'свая',
        }
        raw_tokens = [w for w in re.findall(r'[а-яa-z0-9]{2,}', query, re.I) if w.lower() not in stop_words]
        tokens = [synonyms.get(w.lower(), w.lower()) for w in raw_tokens]
        if not tokens:
            return None

        clean_q = " ".join(tokens)
        query_tokens = tokens

        async with self.pool.acquire() as conn:
            sql = """
                SELECT * FROM (
                    SELECT ds.*, d.code as doc_code, d.revision, d.stamp_change_no, d.section, d.file_path,
                           p.code as project_code, p.cloud_ok as project_cloud_ok,
                           ts_rank(ds.tsv, plainto_tsquery('russian', $1)) as rank,
                           ts_rank(to_tsvector('russian', coalesce(ds.title, '')), plainto_tsquery('russian', $1)) as title_rank,
                           similarity(ds.title, $1) as sim
                    FROM doc_sheets ds
                    JOIN documents d ON ds.doc_id = d.id
                    JOIN projects p ON d.project_id = p.id
                    WHERE d.status = 'active'
                      AND p.code = ANY($2::varchar[])
                      AND ($3::varchar IS NULL OR d.section ILIKE $3)
                      AND (
                          ds.tsv @@ plainto_tsquery('russian', $1)
                          OR ds.title ILIKE '%' || $1 || '%'
                          OR ds.tags && $4::text[]
                          OR similarity(ds.title, $1) > 0.15
                      )
                ) t
                ORDER BY (title_rank * 5 + rank + sim) DESC, sheet_number ASC
                LIMIT 1
            """
            row = await conn.fetchrow(sql, clean_q, search_projects, section, query_tokens)
            return dict(row) if row else None

    async def get_project_sheets_map(self, tg_id: int, project_code: str | None = None) -> str:
        """Compact project map per user (post-ACL)."""
        docs = await self.list_active_docs(tg_id, project_code=project_code)
        if not docs:
            return "В реестре нет доступных активных документов по вашим правам."

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
                ch_no = d.get("stamp_change_no") or "0"
                lines.append(
                    f"  📄 Комплект [{d['code']}] (Раздел {d['section']}, Ревизия {d['revision']}, Изм. {ch_no}):"
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

    async def save_pending_upload(self, upload: PendingUpload):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pending_uploads 
                (file_id, tg_id, project_code, doc_type, section, code, stamp_change_no, temp_path, original_filename, sha256)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (file_id) DO UPDATE SET
                    project_code = EXCLUDED.project_code,
                    doc_type = EXCLUDED.doc_type,
                    section = EXCLUDED.section,
                    code = EXCLUDED.code,
                    stamp_change_no = EXCLUDED.stamp_change_no,
                    temp_path = EXCLUDED.temp_path,
                    original_filename = EXCLUDED.original_filename,
                    sha256 = EXCLUDED.sha256,
                    created_at = NOW()
            """, upload.file_id, upload.tg_id, upload.project_code, upload.doc_type,
                upload.section, upload.code, upload.stamp_change_no, upload.temp_path,
                upload.original_filename, upload.sha256)

    async def pop_pending_upload(self, file_id_or_tg_id: str | int) -> PendingUpload | None:
        async with self.pool.acquire() as conn:
            if isinstance(file_id_or_tg_id, int) or str(file_id_or_tg_id).isdigit():
                row = await conn.fetchrow(
                    "DELETE FROM pending_uploads WHERE tg_id = $1 RETURNING *", int(file_id_or_tg_id)
                )
            else:
                row = await conn.fetchrow(
                    "DELETE FROM pending_uploads WHERE file_id = $1 RETURNING *", str(file_id_or_tg_id)
                )
            if not row:
                return None
            return PendingUpload(
                file_id=row["file_id"],
                tg_id=row["tg_id"],
                project_code=row["project_code"],
                doc_type=row["doc_type"],
                section=row["section"],
                code=row["code"],
                stamp_change_no=row["stamp_change_no"],
                temp_path=row["temp_path"],
                original_filename=row["original_filename"],
                sha256=row["sha256"],
            )

    async def get_document_by_id(self, doc_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT d.*, p.code as project_code, p.name as project_name, p.cloud_ok as project_cloud_ok
                FROM documents d
                JOIN projects p ON d.project_id = p.id
                WHERE d.id = $1
            """, doc_id)
            return dict(row) if row else None

    async def get_sheet_by_page(self, doc_id: int, page_index: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT ds.*, d.code as doc_code, d.revision, d.stamp_change_no, d.file_path,
                       p.code as project_code, p.cloud_ok as project_cloud_ok
                FROM doc_sheets ds
                JOIN documents d ON ds.doc_id = d.id
                JOIN projects p ON d.project_id = p.id
                WHERE ds.doc_id = $1 AND ds.page_index = $2
            """, doc_id, page_index)
            return dict(row) if row else None

    async def quarantine_document(self, doc_id: int, user_tg_id: int, reason: str = "quarantined"):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE documents SET status = 'quarantine' WHERE id = $1", doc_id)
            await self.log_audit(user_tg_id, "QUARANTINE_DOC", doc_id, {"reason": reason})

    async def activate_quarantined_document(self, doc_id: int, user_tg_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE documents SET status = 'active' WHERE id = $1", doc_id)
            await self.log_audit(user_tg_id, "ACTIVATE_DOC", doc_id)
