CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- =====================================================
-- ПОЛЬЗОВАТЕЛИ
-- =====================================================

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT UNIQUE NOT NULL,               -- Telegram user ID — основной идентификатор
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(20),                           -- контактный телефон
    position VARCHAR(255),                       -- должность: 'Главный инженер проекта'
    role VARCHAR(50) NOT NULL,                   -- глобальная роль: 'admin','gip','pto','engineer','foreman','viewer'
    max_clearance VARCHAR(10) NOT NULL DEFAULT 'L0',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON COLUMN users.tg_id IS 'Telegram user ID — единственный внешний идентификатор сотрудника. Узнать: переслать сообщение @userinfobot';

-- =====================================================
-- ОБЪЕКТЫ
-- =====================================================

CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL
);

-- =====================================================
-- ПРИВЯЗКА СОТРУДНИКОВ К ОБЪЕКТАМ (с ролью НА объекте)
-- =====================================================

CREATE TABLE user_projects (
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    project_role VARCHAR(50) NOT NULL DEFAULT 'viewer',   -- роль именно на ЭТОМ объекте
    allowed_sections VARCHAR(50)[] DEFAULT '{}',           -- пустой = ВСЕ разделы
    PRIMARY KEY (user_id, project_id)
);

COMMENT ON COLUMN user_projects.allowed_sections IS 'Пустой массив {} = доступ ко всем разделам. Заполненный = только перечисленные, напр. {КР,АР}';

-- =====================================================
-- РЕЕСТР ДОКУМЕНТОВ (источник истины)
-- Ключевое правило: на каждую связку (project_id, code) может быть
-- ровно ОДНА запись со status='active'. Остальные — 'superseded'.
-- =====================================================

CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    project_id INT REFERENCES projects(id),
    doc_type VARCHAR(50) NOT NULL,
    section VARCHAR(50) NOT NULL,
    code VARCHAR(150) NOT NULL,
    revision INT NOT NULL DEFAULT 1,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    security_level VARCHAR(10) NOT NULL DEFAULT 'L0',
    file_path TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    uploaded_by INT REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Быстрый поиск по (объект, тип, раздел, статус)
CREATE INDEX idx_docs_lookup ON documents(project_id, doc_type, section, status);

-- Уникальный индекс: только ОДНА активная версия на каждый шифр внутри объекта
CREATE UNIQUE INDEX idx_one_active_per_code
    ON documents(project_id, code) WHERE status = 'active';

-- Защита от загрузки дубликатов (тот же файл)
CREATE UNIQUE INDEX idx_docs_sha256 ON documents(sha256);

-- =====================================================
-- ПОСТРАНИЧНЫЙ РЕЕСТР ЛИСТОВ ЧЕРТЕЖЕЙ (FTS + pg_trgm)
-- =====================================================

CREATE TABLE IF NOT EXISTS doc_sheets (
    id SERIAL PRIMARY KEY,
    doc_id INT REFERENCES documents(id) ON DELETE CASCADE,
    page_index INT NOT NULL,                  -- 0-based индекс страницы PDF (0, 1, 2...)
    sheet_number INT,                         -- номер листа по ГОСТ (1, 2, 3...)
    title TEXT NOT NULL,                      -- наименование листа из штампа/ведомости
    tags TEXT[] DEFAULT '{}',                 -- теги ['опалубка', 'плита', 'фасад', 'кровля'...]
    text_content TEXT DEFAULT '',             -- извлечённый текст листа
    tsv tsvector,                             -- полнотекстовый индекс (russian)
    is_landscape BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (doc_id, page_index)
);

CREATE INDEX IF NOT EXISTS idx_doc_sheets_lookup ON doc_sheets(doc_id, page_index);
CREATE INDEX IF NOT EXISTS idx_doc_sheets_tsv ON doc_sheets USING gin(tsv);
CREATE INDEX IF NOT EXISTS idx_doc_sheets_title_trgm ON doc_sheets USING gin(title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_documents_code_trgm ON documents USING gin(code gin_trgm_ops);

-- =====================================================
-- ИНВАЙТ-КОДЫ (регистрация сотрудников)
-- =====================================================

CREATE TABLE invite_codes (
    id SERIAL PRIMARY KEY,
    code VARCHAR(20) UNIQUE NOT NULL,
    target_role VARCHAR(50) NOT NULL DEFAULT 'viewer',
    max_clearance VARCHAR(10) NOT NULL DEFAULT 'L0',
    project_id INT REFERENCES projects(id),
    allowed_sections VARCHAR(50)[] DEFAULT '{}',
    created_by INT REFERENCES users(id),
    used_by INT REFERENCES users(id),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- ЖУРНАЛ АУДИТА
-- =====================================================

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    doc_id INT REFERENCES documents(id),
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- ТЕСТОВЫЕ ДАННЫЕ
-- =====================================================

INSERT INTO projects (code, name) VALUES ('ОБЪЕКТ-1', 'ЖК Горизонт (Корпус 2)');
