CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- =====================================================
-- ПОЛЬЗОВАТЕЛИ (ТЗ v2: без role и max_clearance)
-- =====================================================

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    position VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON COLUMN users.tg_id IS 'Telegram user ID — внешний идентификатор сотрудника';

-- =====================================================
-- КАНАЛЫ И ИДЕНТИФИКАТОРЫ (З-20)
-- =====================================================

CREATE TABLE IF NOT EXISTS identities (
    id SERIAL PRIMARY KEY,
    channel VARCHAR(32) NOT NULL DEFAULT 'telegram',
    external_id VARCHAR(64) NOT NULL,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(channel, external_id)
);

-- =====================================================
-- ОБЪЕКТЫ (ТЗ v2: cloud_ok на уровне объекта)
-- =====================================================

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    cloud_ok BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- ПРИВЯЗКА СОТРУДНИКОВ К ОБЪЕКТАМ (БЕЛЫЕ СПИСКИ)
-- ТЗ v2: равные права у всех участников объекта
-- =====================================================

CREATE TABLE IF NOT EXISTS user_projects (
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, project_id)
);

-- =====================================================
-- РЕЕСТР ДОКУМЕНТОВ (источник истины)
-- ТЗ v2: без security_level; добавлен stamp_change_no,
-- original_filename, статус quarantine
-- =====================================================

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

CREATE INDEX IF NOT EXISTS idx_docs_lookup ON documents(project_id, doc_type, section, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_per_code ON documents(project_id, code) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_sha256_proj ON documents(project_id, sha256);
CREATE INDEX IF NOT EXISTS idx_documents_code_trgm ON documents USING gin(code gin_trgm_ops);

-- =====================================================
-- ПОСТРАНИЧНЫЙ РЕЕСТР ЛИСТОВ ЧЕРТЕЖЕЙ (FTS + pg_trgm)
-- =====================================================

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

CREATE INDEX IF NOT EXISTS idx_doc_sheets_lookup ON doc_sheets(doc_id, page_index);
CREATE INDEX IF NOT EXISTS idx_doc_sheets_tsv ON doc_sheets USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_doc_sheets_title_trgm ON doc_sheets USING gin(title gin_trgm_ops);

-- =====================================================
-- ЧЕРНОВИКИ ЗАГРУЗОК (pending_uploads)
-- =====================================================

CREATE TABLE IF NOT EXISTS pending_uploads (
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

-- =====================================================
-- ЖУРНАЛ АУДИТА (Append-Only)
-- =====================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_tg_id BIGINT,
    action VARCHAR(100) NOT NULL,
    doc_id INT,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- МИГРАЦИИ СХЕМЫ
-- =====================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- SQL ПРЕДСТАВЛЕНИЕ: ВИДИМЫЕ ДОКУМЕНТЫ (З-4)
-- =====================================================

CREATE OR REPLACE VIEW visible_documents AS
SELECT d.*, p.code as project_code, p.name as project_name, p.cloud_ok as project_cloud_ok, up.user_id as allowed_user_id
FROM documents d
JOIN projects p ON d.project_id = p.id
JOIN user_projects up ON p.id = up.project_id
WHERE d.status != 'quarantine';
