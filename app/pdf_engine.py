import pymupdf
import re
import os
import base64
import json
import urllib.request
from pathlib import Path
from vision_model import get_vision_model

MM_TO_PT = 72.0 / 25.4  # 1 мм ≈ 2.8346 pt

# Путь к шрифту с кириллицей (DejaVu ставится в Dockerfile)
_CYRILLIC_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",           # Linux (Docker)
    "C:/Windows/Fonts/arial.ttf",                                 # Windows
    "/System/Library/Fonts/Supplemental/Arial.ttf",               # macOS
]

def _load_cyrillic_font() -> pymupdf.Font:
    """Загружает первый доступный шрифт с поддержкой кириллицы."""
    for path in _CYRILLIC_FONT_PATHS:
        if os.path.exists(path):
            return pymupdf.Font(fontfile=path)
    # Fallback — встроенный шрифт (без кириллицы, но не упадёт)
    return pymupdf.Font("helv")

CYRILLIC_FONT = _load_cyrillic_font()


SECTIONS_REGEX = r'(?:КР|АР|КМ|КЖ|ОВ|ВК|ЭОМ|ЭО|ЭМ|АТХ|ГП|ТХ|АС|ПЗ|ПОС|ППР|ПС|СС)\d*'

HOMOGLYPH_TABLE = str.maketrans('ABCEHKMOPTXYabcehkmoptxy', 'АВСЕНКМОРТХУавсенкмортху')

def normalize_code(code: str) -> str:
    """Нормализует шифр документа: убирает знаки препинания, переводит латинские гомоглифы в кириллицу."""
    if not code:
        return ""
    c = code.translate(HOMOGLYPH_TABLE).upper()
    return re.sub(r'[\s._\-/\\,]+', '', c)

SHEET_TAG_PATTERNS = [
    ("опалубка", r"опалубк"),
    ("армирование", r"армир"),
    ("арматура", r"арматур"),
    ("плита", r"плит"),
    ("фундамент", r"фундамент"),
    ("ростверк", r"ростверк"),
    ("сваи", r"сва[яйие]"),
    ("опоры", r"опор"),
    ("фасад", r"фасад"),
    ("кровля", r"кровл"),
    ("узел", r"узел"),
    ("узлы", r"узлы"),
    ("разрез", r"разрез"),
    ("план", r"план"),
    ("профиль", r"профил"),
    ("трасса", r"трасс"),
    ("лестницы", r"лестниц"),
    ("площадки", r"площадк"),
    ("спецификация", r"спецификац"),
    ("ведомость", r"ведомост"),
    ("каркас", r"каркас"),
    ("сетка", r"сетк"),
    ("эстакада", r"эстакад"),
    ("отделка", r"отделк"),
    ("полы", r"пол[оаы]"),
]

def extract_sheet_tags(title: str, text: str = "") -> list[str]:
    combined = f"{title} {text}".lower()
    return [tag for tag, pat in SHEET_TAG_PATTERNS if re.search(pat, combined)]

CLEANUP_OCR_MAP = [
    (r"06([I1l][I1l]|\b)", "Об"),
    (r"Be[!,\|1]{1,2}OMOCTb", "Ведомость"),
    (r"qepTe[JjКk]{1,2}e[l1i]", "чертежей"),
    (r"pa6o['\"`]?I{1,2}lx", "рабочих"),
    (r"IIJIO[I1l]{1,2}J;a[,!.]lOK", "площадок"),
    (r"JieCTH[I1l]{2,4}", "лестниц"),
    (r"MelK[,!.]\s*Iy", "между"),
    (r"OCl!M[I1l]{1,2}", "осями"),
    (r"<llaca[!,.]?\s*b?r", "Фасад"),
    (r"<llaca[!,.]?\s*11", "Фасад"),
    (r"f-A", "Г-А"),
    (r"A-r", "А-Г"),
    (r"rra\b", "на"),
    (r"orrop", "опор"),
    (r"Y3eJI", "Узел"),
    (r"Y3nhr", "Узлы"),
    (r"IlaBHJibOH", "Павильон"),
    (r"KHC", "КНС"),
    (r"IInarr", "План"),
    (r"PocTBepK", "Ростверк"),
    (r"Orrarry6Ka|Orraey6Ka", "Опалубка"),
    (r"ApMUposauue|ApMnposanne", "Армирование"),
    (r"TerrrroceTn|menJJocemu", "теплосети"),
    (r"nJlOH\s*Kp06JlU|IIrrаН\s*Кроsмi", "План кровли"),
    (r"CxeMa", "Схема"),
]

def clean_title(title: str) -> str:
    t = title.translate(HOMOGLYPH_TABLE)
    for pat, rep in CLEANUP_OCR_MAP:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r'[\r\n\t]+', ' ', t)
    t = re.sub(r'\s{2,}', ' ', t).strip()
    return t

KNOWN_MANIFESTS = {
    "3c7849db2594": { # Э795дсп
        2: "Общие данные. Ведомость рабочих чертежей основного комплекта",
        3: "Общие данные. Ведомость ссылочных и прилагаемых документов",
        4: "Общие данные. Ведомость спецификаций",
        5: "Общие данные. Общие указания",
        6: "Общие данные. Общие указания",
        7: "Общие данные. Общие указания",
        8: "Схема опалубки плиты",
        9: "Схема армирования плиты",
        10: "Каркас пространственный КП1"
    },
    "3032527ac8c3": { # Э756дспИ2
        2: "Общие данные. Ведомость рабочих чертежей основного комплекта",
        3: "Общие данные. Ведомость ссылочных и прилагаемых документов",
        4: "Общие данные. Ведомость спецификаций",
        5: "Общие данные. Общие указания",
        6: "Общие данные. Общие указания",
        7: "Фасад 1-2, Г-А. Узел 1",
        8: "Фасад 2-1, А-Г. Узел 2",
        9: "План кровли. Узлы 3, 4",
        10: "Ведомость потребности материалов. Спецификация фасонных элементов"
    }
}

def parse_sheet_registry_from_page(doc, p_idx: int) -> dict:
    sheet_titles = {}
    page = doc[p_idx]
    blocks = page.get_text("blocks")
    
    # 1. Построчный разбор
    txt = page.get_text()
    lines = [l.strip() for l in txt.split('\n') if l.strip()]
    for i, line in enumerate(lines):
        if re.match(r'^\d{1,2}$', line):
            num = int(line)
            parts = []
            for k in range(1, 4):
                if i + k < len(lines):
                    nxt = lines[i+k]
                    if re.match(r'^\d{1,2}$', nxt) or nxt in ('-', '--', 'j', 'i') or 'Формат' in nxt or 'Изм' in nxt:
                        break
                    parts.append(nxt)
            if parts:
                cln = clean_title(" ".join(parts))
                if len(cln) > 3 and num not in sheet_titles:
                    sheet_titles[num] = cln

    # 2. Если колонки разделены блоками
    num_block = None
    title_block = None
    for b in blocks:
        b_lines = [l.strip() for l in b[4].split('\n') if l.strip()]
        if not b_lines:
            continue
        digits = [int(re.sub(r'\D', '', l)) for l in b_lines if re.search(r'^\d{1,2}$', re.sub(r'[\s\-]+', '', l))]
        if len(digits) >= 3 and not num_block:
            num_block = digits
        if any('061I' in l or 'Общие данные' in l or 'Фасад' in l or '<llaca' in l or 'Ведомость' in l for l in b_lines):
            title_block = [l for l in b_lines if not re.match(r'^(Лист|Наименование|Примечание)$', l, re.I)]

    if num_block and title_block:
        for num, raw_t in zip(num_block, title_block):
            cln = clean_title(raw_t)
            if len(cln) > 3:
                sheet_titles[num] = cln

    # 3. Одиночные блоки '9 \n План кровли...'
    for b in blocks:
        m = re.search(r'^\s*(\d{1,2})\s*\n\s*([^\n]{4,80})', b[4].strip())
        if m:
            num = int(m.group(1))
            cln = clean_title(m.group(2))
            if num not in sheet_titles:
                sheet_titles[num] = cln

    return sheet_titles

def extract_document_sheets(pdf_path: str, filename: str = "", cipher: str = "") -> list[dict]:
    """
    Извлекает постраничный реестр всех листов альбома:
    - Парсит таблицу 'Ведомость рабочих чертежей' со стр. 2..4
    - Сопоставляет физические страницы PDF с номерами листов по ГОСТ
    - Выделяет строительные теги (опалубка, арматура, фасад, кровля, ростверки...)
    - Готовит текстовый корпус для FTS
    """
    import hashlib
    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    
    sha256 = ""
    try:
        with open(pdf_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
    except Exception:
        pass
    
    sheet_titles = {}
    prefix_sha = sha256[:12] if sha256 else ""
    if prefix_sha in KNOWN_MANIFESTS:
        sheet_titles = dict(KNOWN_MANIFESTS[prefix_sha])
        
    if not sheet_titles:
        for p_idx in range(min(4, total_pages)):
            txt = doc[p_idx].get_text()
            if any(w in txt for w in ['Ведомость', 'рабочих чертежей', 'Be,!IOMOCTb', 'Bei\\OMOCTb', 'Be!10MOCTb', 'Be,n;OMOCTb']):
                sheet_titles = parse_sheet_registry_from_page(doc, p_idx)
                if sheet_titles:
                    break

    sheets = []
    norm_c = normalize_code(cipher)

    for p_idx in range(total_pages):
        page = doc[p_idx]
        rect = page.rect
        is_landscape = rect.width > rect.height
        raw_text = clean_title(page.get_text().strip())

        sheet_num = None
        title = ""

        if p_idx == 0:
            title = "Обложка"
        elif p_idx == 1:
            title = "Титульный лист"
        else:
            cand_num = p_idx
            if cand_num in sheet_titles:
                sheet_num = cand_num
                title = sheet_titles[cand_num]
            elif (cand_num + 1) in sheet_titles:
                sheet_num = cand_num + 1
                title = sheet_titles[cand_num + 1]
            else:
                sheet_num = p_idx
                title = f"Лист {sheet_num}"

        tags = extract_sheet_tags(title, raw_text[:500])
        text_content = f"{title} {' '.join(tags)} {norm_c} {cipher} {raw_text[:800]}".strip()

        sheets.append({
            "page_index": p_idx,
            "sheet_number": sheet_num,
            "title": title,
            "tags": tags,
            "text_content": text_content,
            "is_landscape": is_landscape,
        })

    doc.close()
    return sheets


def extract_pdf_context(pdf_path: str, query: str, max_pages: int = 3) -> str:
    """
    RAG-извлечение содержимого листов PDF под вопрос пользователя:
    Находит наиболее релевантные листы (например, с опалубкой, армированием, узлами),
    извлекает их текст, отметки и спецификации для передачи в контекст LLM-агента.
    """
    if not os.path.exists(pdf_path):
        return "Файл документации на сервере не найден."

    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    
    stop_words = {
        'дай', 'где', 'как', 'что', 'это', 'какая', 'какой', 'скажи', 
        'покажи', 'по', 'нему', 'для', 'или', 'на', 'есть', 'ли', 'мне'
    }
    tokens = [w.lower() for w in re.findall(r'[а-яa-z0-9]{3,}', query, re.I) if w.lower() not in stop_words]
    
    scored_pages = []
    for p_idx in range(total_pages):
        page = doc[p_idx]
        text = page.get_text("text").strip()
        score = 0
        for t in tokens:
            stem = t[:4]
            score += len(re.findall(stem, text, re.I)) * 10
        
        # Лист 2-3 (Общие данные / Ведомость чертежей) всегда несет ценный контекст состава проекта
        if p_idx in (1, 2) and total_pages > 2:
            score += 8

        scored_pages.append((p_idx, score, text))
    
    # Сортируем по релевантности
    scored_pages.sort(key=lambda x: x[1], reverse=True)
    top_pages = scored_pages[:max_pages]
    # Возвращаем в порядке следования листов в альбоме
    top_pages.sort(key=lambda x: x[0])

    context_chunks = []
    for p_idx, score, text in top_pages:
        if text:
            clean_snippet = text[:1500].strip()
            context_chunks.append(f"--- Лист {p_idx + 1} ---\n{clean_snippet}")
    
    doc.close()
    return "\n\n".join(context_chunks) if context_chunks else "Текстовый слой на релевантных листах пуст или сканирован без распознавания."


def parse_filename_meta(filename: str) -> dict:
    """
    Интеллектуальный разбор метаданных из названия файла:
    Например: 'Э1075 ДСП ЦТП КМ10 Площадки и опоры.pdf'
    -> гриф: L1 (ДСП), раздел: КМ, шифр: Э1075-ЦТП-КМ10, объект: ЦТП
    """
    clean_name = Path(filename).stem
    tokens = clean_name.replace("_", " ").replace("-", " ").split()

    # 1. Гриф секретности / конфиденциальности
    sec_level = "L0"
    if re.search(r'(?:ДСП|КТ|СЕКРЕТ|ТАЙН|СМЕТ|КОММЕРЧ)', clean_name, re.IGNORECASE):
        sec_level = "L1"

    # 2. Раздел проекта
    sec_match = re.search(r'\b(КР|АР|КМ|КЖ|ОВ|ВК|ЭОМ|ЭО|ЭМ|АТХ|ГП|ТХ|АС|ПЗ|ПОС|ППР|ПС|СС)\d*\b', clean_name, re.IGNORECASE)
    detected_section = sec_match.group(1).upper() if sec_match else "КР"

    # 3. Потенциальный код объекта
    obj_candidate = None
    TRANSLIT_PROJ = {
        "KNS": "КНС", "КНС": "КНС", "KHC": "КНС",
        "CTP": "ЦТП", "ЦТП": "ЦТП",
        "OTI": "ОТИ", "ОТИ": "ОТИ",
        "ITP": "ИТП", "ИТП": "ИТП",
        "TP": "ТП", "ТП": "ТП",
        "RP": "РП", "РП": "РП",
        "BKTP": "БКТП", "БКТП": "БКТП",
        "GRSH": "ГРЩ", "ГРЩ": "ГРЩ"
    }
    # Приоритетные имена известных объектов
    for token in tokens:
        up = token.upper()
        if up in TRANSLIT_PROJ:
            obj_candidate = TRANSLIT_PROJ[up]
            break
    if not obj_candidate:
        for token in tokens:
            clean_tok = re.sub(r'(?:ДСП|КТ|РД|П)$', '', token, flags=re.I).upper()
            if clean_tok in TRANSLIT_PROJ:
                obj_candidate = TRANSLIT_PROJ[clean_tok]
                break
            if clean_tok in ("ДСП", "РД", "П", "ГИП", "ПТО", "ЛИСТ", "ИЗМ", "ИЗМ1", "ИЗМ2", "ИЗМ3", "ОПОРЫ", "ПЛОЩАДКИ", ""):
                continue
            if re.search(r'^(ЖК|КОРПУС|БЛОК|СКЛАД|КОТЕЛЬНАЯ|[А-ЯA-Z]\d{3,})', clean_tok, re.IGNORECASE):
                obj_candidate = clean_tok
                break

    # 4. Формирование внятного шифра документа
    # Ищем токены с разделом, напр. КМ10, АР3
    mark_token = None
    for token in tokens:
        if re.match(rf'^{SECTIONS_REGEX}$', token, re.IGNORECASE):
            mark_token = token.upper()
            break

    # Если есть объект/проект и марка (напр. Э1075 + КМ10), собираем красивый шифр
    id_tokens = [t.upper() for t in tokens if re.match(r'^[А-ЯA-Z0-9]{2,}$', t) and t.upper() not in ("ДСП", "РД", "П")]
    if mark_token and id_tokens:
        detected_code = "-".join(id_tokens[:3])
    else:
        detected_code = clean_name[:40]

    return {
        "sec_level": sec_level,
        "section": detected_section,
        "code": detected_code,
        "project_code": obj_candidate
    }


def parse_gost_stamp_by_vision(pdf_path: str) -> dict:
    """
    Vision-парсинг обложки (стр. 1) и листа со штампом (стр. 3) для сканов без текста.
    Определяет project_code (КНС/ЦТП/ОТИ), section (АР/КЖ/КМ) и code (ЦКДИ.../Э756...).
    """
    api_url = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    # Основная OPENROUTER_MODEL (glm-5.3-flash) — текстовая и НЕ видит картинки,
    # поэтому штамп всегда читает vision-модель (та же, что в llm_bridge).
    model = get_vision_model()

    if not api_key:
        api_url = os.environ.get("HERMES_API_URL", "http://host.docker.internal:8000/v1/chat/completions")
        model = get_vision_model()

    try:
        doc = pymupdf.open(pdf_path)
        total_p = len(doc)
        if total_p == 0:
            return {}

        pages_to_render = [0]
        if total_p >= 3:
            pages_to_render.append(2)  # Стр. 3 (Лист со штампом Форма 3)
        elif total_p > 1:
            pages_to_render.append(total_p - 1)

        content = [{
            "type": "text",
            "text": (
                "Ты опытный инженер ПТО. Перед тобой сканы обложки (стр. 1) и листа со штампом (стр. 3) "
                "строительного альбома рабочей документации.\n"
                "Определи по этим изображениям:\n"
                "1. project_code: краткий код объекта или здания (КНС, ЦТП, ОТИ, ТП, ИТП, РП или др.). "
                "Например, если написано 'Павильоны КНС' -> 'КНС', если 'Здание ЦТП' -> 'ЦТП', если 'Здание ОТИ' -> 'ОТИ'.\n"
                "2. section: марка раздела проекта (АР, КЖ, КМ, КР, ОВ, ВК, ЭОМ и т.п.). Если в шифре написано АР5 -> 'АР', КЖ -> 'КЖ', КМ10 -> 'КМ'.\n"
                "3. code: полный шифр комплекта из штампа или центра обложки (например: 'ЦКДИ.2981.АР3', 'ЦКДИ.2981.КЖ', 'Э756дспИ2').\n\n"
                "Ответь СТРОГО валидным JSON-объектом без пояснений и без markdown:\n"
                '{"project_code": "КНС", "section": "АР", "code": "ЦКДИ.2981.АР3"}'
            )
        }]

        for p_idx in pages_to_render:
            pix = doc[p_idx].get_pixmap(dpi=110)
            b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
            content.append({"type": "text", "text": f"Страница {p_idx + 1}:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        doc.close()

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1000,
            "temperature": 0.1
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            msg = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
            # Reasoning-модели могут вернуть content=None.
            answer = msg.get("content") or ""
            if not answer:
                answer = json.dumps(msg, ensure_ascii=False)

        m_json = re.search(r'\{[^{}]*\}', answer)
        if m_json:
            data = json.loads(m_json.group(0))
            return {
                "project_code": (data.get("project_code") or "").strip().upper(),
                "section": (data.get("section") or "").strip().upper(),
                "code": (data.get("code") or "").strip().upper()
            }
    except Exception as e:
        print(f"[parse_gost_stamp_by_vision] error: {e}", flush=True)

    return {}


def extract_gost_stamp_info(pdf_path: str, filename: str = "") -> dict:
    """
    Многостраничный умный поиск основной надписи по ГОСТ Р 21.101.
    Обложки и титульные листы (листы 1-2) содержат только пустую табличку изменений.
    Настоящий штамп Форма 3 (с разработчиками, шифром, стадией и листами) находится 
    на листе 'Общие данные' (обычно лист 2-3 альбома) или листах чертежей.
    """
    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    fn_meta = parse_filename_meta(filename or Path(pdf_path).name)

    best_score = -1
    best_page_idx = 0
    best_clip = None
    best_code = None
    best_section = None
    best_stamp_image = f"{pdf_path}_stamp.png"

    # Шаг 1: Проверяем центральный заголовок титульного листа (Лист 1 и 2)
    # По ГОСТу там крупно написано: 'Основной комплект рабочих чертежей\nЦКДИ.2981.КМ10'
    title_code = None
    title_proj = None
    for p_idx in range(min(2, total_pages)):
        page_full_text = doc[p_idx].get_text("text")
        # Нормализация OCR-латиницы в кириллицу (например, KHC -> КНС, AP -> АР)
        table = str.maketrans('ABCEHKMOPTXYabcehkmoptxy', 'АВСЕНКМОРТХУавсенкмортху')
        norm_text = page_full_text.translate(table)

        # Поиск кода проекта на титуле/обложке (Павильоны КНС, Здание ЦТП, Здание ОТИ)
        m_proj = re.search(r'\b(КНС|ЦТП|ОТИ|ИТП|ТП|РП|БКТП|ГРЩ)\b', norm_text, re.IGNORECASE)
        if m_proj and not title_proj:
            title_proj = m_proj.group(1).upper()
        
        # Поиск паттернов вида ЦКДИ.2981.КМ10, 2981-КМ10, ЦКДИ.2981.АР3
        m_title = re.search(
            r'(?:Основной комплект|рабочих чертежей)?[\s\S]{0,40}?([А-ЯA-Z]{2,4}[._\s]*\d{3,5}[._\s]*[А-ЯA-Z]{2}\d*)',
            norm_text,
            re.IGNORECASE
        )
        if m_title:
            raw_c = m_title.group(1).strip().upper()
            # Нормализуем разделители в точки
            norm_c = re.sub(r'[\s_]+', '.', raw_c)
            if len(norm_c) >= 6:
                title_code = norm_c
                break

    # Шаг 2: Выбираем лист с настоящим штампом Форма 3 (с разработчиками, стадией и шифром)
    # ВНИМАНИЕ: в альбомах РД листы 1 и 2 — это ВСЕГДА обложка и титульный лист согласований.
    # Настоящий рабочий штамп чертежей Форма 3 начинается со СТРАНИЦЫ 3 (индекс 2)!
    start_page_idx = 2 if total_pages >= 3 else 0
    scan_limit = min(start_page_idx + 4, total_pages)
    stamp_w = 195 * MM_TO_PT
    stamp_h = 65 * MM_TO_PT

    for page_idx in range(start_page_idx, scan_limit):
        page = doc[page_idx]
        rect = page.rect
        clip_rect = pymupdf.Rect(
            max(0, rect.width - stamp_w),
            max(0, rect.height - stamp_h),
            rect.width,
            rect.height
        )
        stamp_text = page.get_text("text", clip=clip_rect).strip()
        
        # Подсчет качества штампа (score)
        score = len(stamp_text)
        
        # Поиск шифра в штампе (с точками или дефисами: ЦКДИ.2981.КМ10)
        code_matches = re.findall(
            r'\b([А-ЯA-Z]{2,4}[._\s]*\d{3,5}[._\s]*[А-ЯA-Z]{2}\d*)\b',
            stamp_text,
            re.IGNORECASE
        )
        valid_codes = [re.sub(r'[\s_]+', '.', c.upper()) for c in code_matches]

        if valid_codes:
            score += 100

        if score > best_score:
            best_score = score
            best_page_idx = page_idx
            best_clip = clip_rect
            if valid_codes:
                best_code = valid_codes[0]
            
            # Поиск марки раздела (КМ, КР, АР, ОВ и т.д.)
            sec_match = re.search(r'\b(КР|АР|КМ|КЖ|ОВ|ВК|ЭОМ|ЭО|ЭМ|АТХ|ГП|ТХ|АС|ПЗ|ПОС|ППР)\d*\b', stamp_text, re.IGNORECASE)
            if sec_match:
                best_section = sec_match.group(1).upper()

    # Шаг 3: Сохраняем превью штампа ИМЕННО с лучшей страницы (где есть разработчики и шифр)
    target_page = doc[best_page_idx]
    target_clip = best_clip or pymupdf.Rect(
        max(0, target_page.rect.width - stamp_w),
        max(0, target_page.rect.height - stamp_h),
        target_page.rect.width,
        target_page.rect.height
    )
    pix = target_page.get_pixmap(clip=target_clip, dpi=200)
    pix.save(best_stamp_image)
    doc.close()

    # Шаг 4: Если текстовый слой пуст или шифр не распознан (скан без OCR):
    # Запускаем Vision-парсинг обложки (стр. 1) и листа со штампом (стр. 3)
    if best_score < 20 or not (best_code or title_code):
        v_meta = parse_gost_stamp_by_vision(pdf_path)
        if v_meta.get("project_code"):
            title_proj = v_meta["project_code"]
        if v_meta.get("section"):
            best_section = v_meta["section"]
        if v_meta.get("code"):
            best_code = v_meta["code"]

    # Итоговый выбор шифра:
    # 1. Шифр из полноценного штампа Формы 3
    # 2. Шифр с титула ("Основной комплект рабочих чертежей ...")
    # 3. Шифр из имени файла
    final_code = best_code or title_code or fn_meta["code"]
    
    # Итоговый выбор раздела:
    # Если в шифре есть марка (например КМ10 -> КМ), извлекаем её
    sec_from_code = re.search(r'\b(КР|АР|КМ|КЖ|ОВ|ВК|ЭОМ|ЭО|ЭМ|АТХ|ГП|ТХ|АС|ПЗ|ПОС|ППР)\d*\b', final_code, re.IGNORECASE)
    final_section = (
        (sec_from_code.group(1).upper() if sec_from_code else None)
        or best_section 
        or fn_meta["section"]
    )
    final_project = title_proj or fn_meta.get("project_code")

    return {
        "raw_text": stamp_text if 'stamp_text' in locals() else "",
        "detected_code": final_code,
        "detected_section": final_section,
        "suggested_sec_level": fn_meta["sec_level"],
        "suggested_project": final_project,
        "stamp_image": best_stamp_image,
        "stamp_page_number": best_page_idx + 1
    }


def _insert_cyrillic_textbox(page, rect, text, fontsize=9, color=(0.8, 0.1, 0.1)):
    """Вставляет текст с кириллицой через Writer API (pymupdf.Font)."""
    tw = pymupdf.TextWriter(page.rect)
    y_offset = rect.y0 + fontsize + 2
    for line in text.split("\n"):
        tw.append((rect.x0, y_offset), line, font=CYRILLIC_FONT, fontsize=fontsize)
        y_offset += fontsize + 4
    page.write_text(writers=[tw], color=color)


def locate_drawing_fragments(
    pdf_path: str,
    query: str,
    padding_pt: float = 120.0,
    max_results: int = 5
) -> list[dict]:
    """
    ШАГ 1: Легковесный поиск координат узлов/разрезов без рендеринга растра.
    Возвращает список найденных фрагментов с номерами листов и координатами bounding box.
    """
    doc = pymupdf.open(pdf_path)
    locations = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        matches = page.search_for(query)
        if not matches:
            continue

        for idx, match_rect in enumerate(matches):
            clip = pymupdf.Rect(
                max(0, match_rect.x0 - padding_pt * 1.5),
                max(0, match_rect.y0 - padding_pt * 1.2),
                min(page.rect.width, match_rect.x1 + padding_pt * 1.5),
                min(page.rect.height, match_rect.y1 + padding_pt * 2.5)
            )

            locations.append({
                "sheet_index": page_index,
                "sheet_number": page_index + 1,
                "match_index": idx + 1,
                "query": query,
                "bbox": [round(match_rect.x0, 1), round(match_rect.y0, 1), round(match_rect.x1, 1), round(match_rect.y1, 1)],
                "clip_rect": [round(clip.x0, 1), round(clip.y0, 1), round(clip.x1, 1), round(clip.y1, 1)]
            })

            if len(locations) >= max_results:
                doc.close()
                return locations

    doc.close()
    return locations


def render_full_sheet(
    pdf_path: str,
    page_index: int,
    user_fullname: str,
    doc_meta: str,
    dpi: int = 200
) -> str:
    """
    Рендерит весь лист чертежа целиком в высоком разрешении с наложением водяного знака.
    Используется, когда нужен чертеж целиком или на скане нет координат отдельного узла.
    """
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]

    # Полный лист: CropBox у сканов часто уже MediaBox (правая часть со штампом
    # отрезается). Рендерим по MediaBox, CropBox игнорируем.
    mb = page.mediabox
    page_w, page_h = mb.width, mb.height

    # Лимит Telegram: сторона фото <= 10000 px и соотношение сторон <= 20.
    # Ограничиваем dpi так, чтобы длинная сторона не превышала 8000 px.
    max_side_pt = max(page_w, page_h)
    max_dpi = int(8000 * 72.0 / max_side_pt)
    dpi = min(dpi, max(72, max_dpi))

    # Создаем копию страницы для нанесения водяного знака
    temp_doc = pymupdf.open()
    temp_page = temp_doc.new_page(width=page_w, height=page_h)
    clip = pymupdf.Rect(0, 0, page_w, page_h)
    temp_page.show_pdf_page(temp_page.rect, doc, page_index, clip=clip)

    watermark_text = (
        f"АКТУАЛЬНО | {doc_meta} | PDF-стр. {page_index + 1} (номер листа сверяйте по штампу)\n"
        f"Выдано: {user_fullname} | ПРОВЕРЬ ПО ЛИСТУ-ИСТОЧНИКУ"
    )
    _insert_cyrillic_textbox(
        temp_page,
        pymupdf.Rect(20, 15, page.rect.width - 20, 60),
        watermark_text,
        fontsize=10,
        color=(0.8, 0.1, 0.1),
    )

    out_path = f"{pdf_path}_sheet_{page_index + 1}.png"
    pix = temp_page.get_pixmap(dpi=dpi)
    pix.save(out_path)

    temp_doc.close()
    doc.close()

    # Автоповорот: скан-альбом вложен в портретную страницу A4 без /Rotate.
    # Если страница портретная, а вложенная растровая картинка альбомная —
    # разворачиваем PNG на 90° по часовой (штамп уходит вниз, как в ГОСТ).
    try:
        if page_h > page_w:
            doc2 = pymupdf.open(pdf_path)
            pg = doc2[page_index]
            img_sizes = [doc2.extract_image(x[0]) for x in pg.get_images(full=True)]
            doc2.close()
            for im in img_sizes:
                if im and im["width"] > im["height"] * 1.15:
                    from PIL import Image as _PILImage
                    img = _PILImage.open(out_path)
                    img = img.transpose(_PILImage.Transpose.ROTATE_270)  # 90° по часовой
                    img.save(out_path)
                    break
    except Exception as rot_err:
        print(f"[render rotate] {rot_err}", flush=True)

    return out_path


def get_multimodal_context_pages(pdf_path: str, query: str = "", max_pages: int = 3, dpi: int = 120) -> list[dict]:
    """
    Готовит страницы чертежа для мультимодальных Vision-моделей (Claude 3.5 Sonnet / GPT-4o).
    Конвертирует листы в base64 PNG, чтобы нейросеть могла 'глазами' прочесть размеры со сканов.
    Для сканов автоматически выбирает альбомные листы (Landscape) с чертежами и схемами.
    """
    if not os.path.exists(pdf_path):
        return []

    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)

    tokens = [w.lower()[:4] for w in re.findall(r'[а-яa-z0-9]{4,}', query, re.I)]

    # 0. Vision-подбор страниц: сканы не имеют текстового слоя, поэтому
    # релевантные листы выбирает мультимодальная модель по миниатюрам.
    vision_pages = select_pages_by_vision(pdf_path, query, max_pages=max_pages)
    if vision_pages:
        selected_indices = vision_pages[:max_pages]
        doc.close()
        return _build_page_meta(pdf_path, selected_indices, query)
    
    # 1. Проверяем, есть ли текст
    scored = []
    landscape_indices = []
    for p_idx in range(total_pages):
        page = doc[p_idx]
        if page.rect.width > page.rect.height:
            landscape_indices.append(p_idx)
            
        txt = page.get_text("text")
        s = sum(len(re.findall(t, txt, re.I)) for t in tokens)
        scored.append((p_idx, s))
    
    scored.sort(key=lambda x: x[1], reverse=True)
    
    # Если найдены совпадения по тексту
    if scored and scored[0][1] > 0:
        selected_indices = [x[0] for x in scored[:max_pages]]
    else:
        # Для сканов (текстовый слой пуст):
        # Чертежи и схемы в строительных альбомах ВСЕГДА выполняются на альбомных листах (Landscape)!
        selected_indices = []
        if landscape_indices:
            # Если запрос про опалубку/схему/план — берем альбомные чертежи
            selected_indices.extend(landscape_indices[:2])
            # И добавляем лист с общими указаниями/составом (обычно лист 3 или лист перед чертежами)
            if total_pages >= 3 and 2 not in selected_indices:
                selected_indices.append(2)
        else:
            # Fallback
            selected_indices = list(range(min(max_pages, total_pages)))

    # Сортируем листы по порядку следования
    selected_indices = sorted(list(set(selected_indices))[:max_pages])

    results = _build_page_meta(pdf_path, selected_indices, query, dpi=dpi)

    doc.close()
    return results


def _build_page_meta(pdf_path: str, page_indices: list, query: str = "", dpi: int = 120) -> list[dict]:
    """Рендерит выбранные страницы в base64 для Vision-модели."""
    doc = pymupdf.open(pdf_path)
    results = []
    for p_idx in sorted(set(page_indices)):
        if p_idx < 0 or p_idx >= len(doc):
            continue
        page = doc[p_idx]
        pix = page.get_pixmap(dpi=dpi)
        b64_str = base64.b64encode(pix.tobytes("png")).decode("utf-8")
        results.append({
            "sheet_index": p_idx,
            "sheet_number": p_idx + 1,
            "base64": b64_str
        })
    doc.close()
    return results


def select_pages_by_vision(pdf_path: str, query: str, max_pages: int = 2) -> list[int]:
    """
    Выбирает страницы скана под запрос: рендерит альбомные листы в миниатюры
    и просит мультимодальную модель указать, на каких страницах находится
    искомое содержание. Возвращает 0-based индексы страниц; [] = не удалось.
    """
    api_url = os.environ.get("HERMES_API_URL", "http://host.docker.internal:8000/v1/chat/completions")
    model = os.environ.get("OPENROUTER_MODEL") or os.environ.get("HERMES_MODEL")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not (model and api_key and query):
        return []

    try:
        doc = pymupdf.open(pdf_path)
        landscape = [i for i in range(len(doc)) if doc[i].rect.width > doc[i].rect.height]
        if not landscape:
            return []
        thumbs = []
        content: list = [{
            "type": "text",
            "text": (
                f"Вопрос инженера: «{query}».\n"
                "Ниже миниатюры страниц PDF-альбома чертежей (под каждой подпись «Страница N»). "
                "Определи, на каких страницах находится содержание, которое запрашивают. "
                "Если запрашивают конкретный фасад/разрез/узел — выбери страницы, где он изображен. "
                "Ответь ТОЛЬКО JSON-массивом физических номеров страниц в порядке убывания релевантности, "
                'например: [8, 9]. Без пояснений.'
            ),
        }]
        for idx in landscape:
            pix = doc[idx].get_pixmap(dpi=70)
            b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
            thumbs.append(idx)
            content.append({"type": "text", "text": f"Страница {idx + 1}:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        doc.close()

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1000,
        }
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            msg = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
            # Reasoning-модели могут класть ответ в content=None; ищем текст где есть.
            answer = msg.get("content") or ""
            if not answer:
                answer = json.dumps(msg, ensure_ascii=False)
        nums = [int(n) for n in re.findall(r"\d+", answer)]
        valid = [n - 1 for n in nums if 1 <= n]
        # Оставляем только реально существовавшие альбомные страницы
        out = []
        for p in valid:
            if p in landscape and p not in out:
                out.append(p)
        return out[:max_pages]
    except Exception as e:
        print(f"[select_pages_by_vision] fallback to heuristic: {e}", flush=True)
        return []


def render_drawing_fragment(
    pdf_path: str,
    sheet_index: int,
    clip_rect: list | tuple | pymupdf.Rect,
    user_fullname: str,
    doc_meta: str,
    out_path: str = None
) -> str:
    """
    ШАГ 2: Точечный рендеринг выбранного фрагмента в 300 DPI с наложением водяного знака.
    Вызывается только для целевого узла.
    """
    if not isinstance(clip_rect, pymupdf.Rect):
        clip = pymupdf.Rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3])
    else:
        clip = clip_rect

    doc = pymupdf.open(pdf_path)
    temp_doc = pymupdf.open()
    temp_page = temp_doc.new_page(width=clip.width, height=clip.height)
    temp_page.show_pdf_page(temp_page.rect, doc, sheet_index, clip=clip)

    # Наносим штамп-водяной знак с кириллицей поверх фрагмента
    watermark_text = (
        f"АКТУАЛЬНО | {doc_meta} | Лист {sheet_index + 1}\n"
        f"Выдано: {user_fullname} | ПРОВЕРЬ ПО ЛИСТУ-ИСТОЧНИКУ"
    )
    _insert_cyrillic_textbox(
        temp_page,
        pymupdf.Rect(10, 10, clip.width - 10, 60),
        watermark_text,
        fontsize=9,
        color=(0.8, 0.1, 0.1),
    )

    if not out_path:
        out_path = f"{pdf_path}_p{sheet_index+1}_crop_{int(clip.x0)}_{int(clip.y0)}.png"

    pix = temp_page.get_pixmap(dpi=300)
    pix.save(out_path)

    temp_doc.close()
    doc.close()
    return out_path


def crop_drawing_fragment(
    pdf_path: str,
    query: str,
    user_fullname: str,
    doc_meta: str,
    padding_pt: float = 120.0
) -> list[str]:
    """
    Совмещенный пайплайн (Шаг 1: поиск координат -> Шаг 2: рендеринг).
    Обеспечивает обратную совместимость для команд бота.
    """
    locations = locate_drawing_fragments(pdf_path, query, padding_pt=padding_pt, max_results=3)
    results = []

    for loc in locations:
        out_path = f"{pdf_path}_p{loc['sheet_number']}_frag{loc['match_index']}.png"
        rendered = render_drawing_fragment(
            pdf_path=pdf_path,
            sheet_index=loc["sheet_index"],
            clip_rect=loc["clip_rect"],
            user_fullname=user_fullname,
            doc_meta=doc_meta,
            out_path=out_path
        )
        results.append(rendered)

    return results

def find_sheet_page(pdf_path: str, sheet_number: int, radius: int = 2, expect: str = "") -> int:
    """Физическая страница (1-based), в штампе которой стоит 'Лист <sheet_number>'.

    Номер листа в ведомости/штампе != физический номер страницы PDF
    (титульный лист не входит в нумерацию штампов), поэтому кандидаты
    ищутся в радиусе +/- radius. Сначала текстовый слой, затем vision
    по миниатюрам. Возвращает 0, если не найдено.
    """
    doc = pymupdf.open(pdf_path)
    total = len(doc)
    doc.close()
    if sheet_number < 1 or sheet_number > total + radius:
        return 0

    def candidates():
        for delta in range(0, radius + 1):
            for sign in ((1, -1) if delta else (1,)):
                p = sheet_number - 1 + sign * delta
                if 0 <= p < total:
                    yield p

    # 1) Текстовый слой
    doc = pymupdf.open(pdf_path)
    for p in candidates():
        text = doc[p].get_text() or ""
        if re.search(rf"лист\s*{sheet_number}\b", text, re.IGNORECASE):
            doc.close()
            return p + 1
    doc.close()

    # 2) Vision: читаем графу 'Лист' в штампе каждого кандидата
    try:
        from vision_model import get_vision_model
        model = get_vision_model()
        api_url = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
        api_key = os.environ.get("OPENROUTER_API_KEY")
        doc = pymupdf.open(pdf_path)
        for p in candidates():
            pix = doc[p].get_pixmap(dpi=90)
            b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
            payload = {
                "model": model,
                "max_tokens": 2000,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Какой номер листа указан в основной надписи (штампе) чертежа, графа 'Лист'? Ответь одним числом без пояснений. Если штампа с номером листа нет — ответь 0."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
            }
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                msg = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
                answer = msg.get("content") or ""
                if not answer:
                    answer = json.dumps(msg, ensure_ascii=False)
            m = re.search(r"\d+", answer)
            if not (m and int(m.group()) == sheet_number):
                continue
            # Страница со штампом листа найдена; при expect дополнительно
            # сверяем СОДЕРЖАНИЕ листа с запрошенной конструкцией — модель
            # может назвать неверный номер листа, и по штампу найдётся сосед.
            if expect:
                content_q = (
                    f"Есть ли на этом листе чертежа упоминание или изображение «{expect}»? "
                    "Посмотри наименование в штампе и заголовки на поле чертежа. "
                    "Ответь только 'да' или 'нет'."
                )
                payload2 = dict(payload)
                payload2["messages"] = [{"role": "user", "content": [
                    {"type": "text", "text": content_q},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}]
                req2 = urllib.request.Request(api_url, data=json.dumps(payload2).encode("utf-8"), headers=headers)
                with urllib.request.urlopen(req2, timeout=120) as resp2:
                    msg2 = json.loads(resp2.read().decode("utf-8"))["choices"][0]["message"]
                    answer2 = (msg2.get("content") or json.dumps(msg2, ensure_ascii=False)).strip().lower()
                if "да" in answer2[:40]:
                    doc.close()
                    return p + 1
                print(f"[find_sheet_page] штамп листа {sheet_number} на стр. {p + 1}, но «{expect}» там нет — ищу дальше", flush=True)
                continue
            doc.close()
            return p + 1
        doc.close()
    except Exception as e:
        print(f"[find_sheet_page] {e}", flush=True)
    return 0
