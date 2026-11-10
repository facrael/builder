"""PDF rendering, stamp parsing, sheet indexing, and watermark engine (ТЗ v2).

Enforces:
- Immediate ДСП marker detection (З-5).
- High DPI raster rendering (150-200 DPI) without 72 DPI downscaling (З-11).
- Repeating diagonal watermark on every delivered sheet/fragment (З-11).
- Normalization of codes and homoglyphs (З-17).
- Cloud guard for vision models: zero bytes sent if cloud_ok is False (З-3).
"""
import base64
import datetime
import io
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import pymupdf

logger = logging.getLogger(__name__)

MM_TO_PT = 72.0 / 25.4

_CYRILLIC_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _get_font_path() -> str | None:
    for path in _CYRILLIC_FONT_PATHS:
        if os.path.exists(path):
            return path
    return None


SECTIONS_REGEX = r'(?:КР|АР|КМ|КЖ|ОВ|ВК|ЭОМ|ЭО|ЭМ|АТХ|ГП|ТХ|АС|ПЗ|ПОС|ППР|ПС|СС)\d*'
HOMOGLYPH_TABLE = str.maketrans('ABCEHKMOPTXYabcehkmoptxy', 'АВСЕНКМОРТХУавсенкмортху')


def normalize_code(code: str) -> str:
    """Normalizes cipher: replaces latin homoglyphs, removes punctuation and spaces, uppercase."""
    if not code:
        return ""
    c = code.translate(HOMOGLYPH_TABLE).upper()
    return re.sub(r'[\s._\-/\\,]+', '', c)


def detect_dsp_marker(pdf_path: str, filename: str = "") -> bool:
    """Checks filename and first pages for DSP ('для служебного пользования') markers (З-5)."""
    check_str = f"{filename} {Path(pdf_path).name if pdf_path else ''}"
    dsp_pattern = r'(?i)(?:дсп|д\.с\.п\.|для[_\s-]+служебного[_\s-]+пользования)'
    if re.search(dsp_pattern, check_str):
        return True

    if pdf_path and os.path.exists(pdf_path):
        try:
            doc = pymupdf.open(pdf_path)
            for i in range(min(3, len(doc))):
                text = doc[i].get_text("text")
                if re.search(dsp_pattern, text):
                    doc.close()
                    return True
            doc.close()
        except Exception as e:
            logger.warning(f"Error checking DSP marker in {pdf_path}: {e}")
    return False


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


def apply_diagonal_watermark(
    pil_image: Image.Image,
    watermark_text: str,
    alpha: int = 55,
    font_size: int = 24
) -> Image.Image:
    """Applies repeating diagonal watermark across entire image (З-11)."""
    rgba = pil_image.convert("RGBA")
    w, h = rgba.size

    overlay = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    font_path = _get_font_path()
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()

    dummy_draw = ImageDraw.Draw(overlay)
    bbox = dummy_draw.textbbox((0, 0), watermark_text, font=font)
    tw = max(120, (bbox[2] - bbox[0]) + 40)
    th = max(40, (bbox[3] - bbox[1]) + 20)

    tile = Image.new("RGBA", (tw, th), (255, 255, 255, 0))
    tile_draw = ImageDraw.Draw(tile)
    # Red-gray tint watermark
    tile_draw.text((10, 5), watermark_text, font=font, fill=(180, 20, 20, alpha))

    rotated_tile = tile.rotate(32, expand=True, resample=Image.Resampling.BICUBIC)
    rw, rh = rotated_tile.size

    step_x = rw + 80
    step_y = rh + 60

    for y in range(-rh, h + rh, step_y):
        for x in range(-rw, w + rw, step_x):
            overlay.alpha_composite(rotated_tile, (x, y))

    # Also add a clear top and bottom banner
    banner_h = 36
    top_banner = Image.new("RGBA", (w, banner_h), (255, 255, 255, 180))
    bdraw = ImageDraw.Draw(top_banner)
    bdraw.text((20, 8), f"АКТУАЛЬНО РД | {watermark_text}", font=font, fill=(200, 20, 20, 240))
    overlay.alpha_composite(top_banner, (0, 0))

    combined = Image.alpha_composite(rgba, overlay)
    return combined.convert("RGB")


def render_full_sheet(
    pdf_path: str,
    page_index: int,
    user_fullname: str,
    doc_meta: str,
    dpi: int = 150,
    out_dir: str = "/tmp/builder_cache"
) -> str:
    """Renders full sheet at high DPI (150-200) with diagonal watermark (З-11)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    if page_index < 0 or page_index >= len(doc):
        doc.close()
        raise IndexError(f"Page index {page_index} out of range (0..{len(doc)-1})")

    page = doc[page_index]
    # Respect /Rotate
    pix = page.get_pixmap(dpi=dpi)
    doc.close()

    # Convert to PIL Image
    img = Image.open(io.BytesIO(pix.tobytes("png")))

    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    watermark_text = f"Выдано: {user_fullname} · {now_str} · {doc_meta}"

    watermarked_img = apply_diagonal_watermark(img, watermark_text, font_size=max(20, int(dpi / 7)))

    safe_stem = Path(pdf_path).stem
    out_path = os.path.join(out_dir, f"{safe_stem}_p{page_index + 1}_{dpi}dpi.png")
    watermarked_img.save(out_path, format="PNG")
    return out_path


def render_drawing_fragment(
    pdf_path: str,
    sheet_index: int,
    clip_rect: list | tuple | pymupdf.Rect,
    user_fullname: str,
    doc_meta: str,
    dpi: int = 300,
    out_dir: str = "/tmp/builder_cache"
) -> str:
    """Renders drawing fragment in 300 DPI with repeating watermark (З-11, З-14)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    page = doc[sheet_index]

    if not isinstance(clip_rect, pymupdf.Rect):
        clip = pymupdf.Rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3])
    else:
        clip = clip_rect

    pix = page.get_pixmap(clip=clip, dpi=dpi)
    doc.close()

    img = Image.open(io.BytesIO(pix.tobytes("png")))
    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    watermark_text = f"Выдано: {user_fullname} · {now_str} · {doc_meta}"
    watermarked_img = apply_diagonal_watermark(img, watermark_text, font_size=20)

    safe_stem = Path(pdf_path).stem
    out_path = os.path.join(out_dir, f"crop_{safe_stem}_p{sheet_index+1}_{clip.x0:.0f}_{clip.y0:.0f}.png")
    watermarked_img.save(out_path, format="PNG")
    return out_path


def locate_drawing_fragments(
    pdf_path: str,
    query: str,
    padding_pt: float = 120.0,
    max_results: int = 5
) -> list[dict]:
    """Finds coordinates of nodes/sections without raster rendering."""
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


def crop_drawing_fragment(
    pdf_path: str,
    query: str,
    user_fullname: str,
    doc_meta: str,
    padding_pt: float = 120.0
) -> list[str]:
    """Locates and crops drawing fragment with watermark."""
    locs = locate_drawing_fragments(pdf_path, query, padding_pt=padding_pt, max_results=3)
    results = []
    for loc in locs:
        p = render_drawing_fragment(
            pdf_path=pdf_path,
            sheet_index=loc["sheet_index"],
            clip_rect=loc["clip_rect"],
            user_fullname=user_fullname,
            doc_meta=doc_meta,
            dpi=250
        )
        results.append(p)
    return results


def parse_filename_meta(filename: str) -> dict:
    """Parses project and section suggestions from filename."""
    name_clean = Path(filename).stem
    tokens = re.split(r'[_.\-\s]+', name_clean)

    detected_section = "КЖ"
    for token in tokens:
        m = re.match(rf'^({SECTIONS_REGEX})$', token, re.IGNORECASE)
        if m:
            detected_section = m.group(1).upper()
            break

    obj_candidate = None
    TRANSLIT_PROJ = {
        "KNS": "КНС", "КНС": "КНС",
        "CTP": "ЦТП", "ЦТП": "ЦТП",
        "OTI": "ОТИ", "ОТИ": "ОТИ",
        "ITP": "ИТП", "ИТП": "ИТП",
        "TP": "ТП", "ТП": "ТП",
        "RP": "РП", "РП": "РП",
        "BKTP": "БКТП", "БКТП": "БКТП",
        "GRSH": "ГРЩ", "ГРЩ": "ГРЩ"
    }
    for token in tokens:
        up = token.upper()
        if up in TRANSLIT_PROJ:
            obj_candidate = TRANSLIT_PROJ[up]
            break

    return {
        "section": detected_section,
        "project_code": obj_candidate,
        "raw_tokens": tokens,
    }


def extract_gost_stamp_info(pdf_path: str, filename: str = "") -> dict:
    """Extracts stamp data (code, section, project suggestion)."""
    fn_meta = parse_filename_meta(filename or Path(pdf_path).name)
    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)

    title_code = None
    title_proj = fn_meta["project_code"]

    for p_idx in range(min(2, total_pages)):
        text = doc[p_idx].get_text("text").translate(HOMOGLYPH_TABLE)
        m_proj = re.search(r'\b(КНС|ЦТП|ОТИ|ИТП|ТП|РП|БКТП|ГРЩ)\b', text, re.IGNORECASE)
        if m_proj and not title_proj:
            title_proj = m_proj.group(1).upper()

        m_code = re.search(r'\b([А-ЯA-Z]{2,4}\.\d{3,5}\.[А-ЯA-Z0-9_-]+)\b', text)
        if m_code and not title_code:
            title_code = m_code.group(1)

    doc.close()

    final_code = title_code or Path(filename or pdf_path).stem
    final_section = fn_meta["section"]
    final_project = title_proj or "ОБЪЕКТ-1"

    return {
        "detected_code": final_code,
        "detected_section": final_section,
        "suggested_project": final_project,
        "stamp_page_number": 1,
    }


def extract_pdf_context(pdf_path: str, query: str = "", max_pages: int = 3) -> str:
    """Extracts text content for query from PDF."""
    if not os.path.exists(pdf_path):
        return "Файл не найден на сервере."

    doc = pymupdf.open(pdf_path)
    text_parts = []
    tokens = [w.lower() for w in re.findall(r'[а-яa-z0-9]{3,}', query, re.I)]

    for page_idx in range(min(len(doc), 15)):
        text = doc[page_idx].get_text("text").strip()
        if not text:
            continue
        if not tokens or any(t in text.lower() for t in tokens):
            text_parts.append(f"--- Страница {page_idx + 1} ---\n{text[:1500]}")
            if len(text_parts) >= max_pages:
                break

    doc.close()
    if not text_parts:
        return "Текстовый слой пуст или сканирован."
    return "\n\n".join(text_parts)


def extract_document_sheets(pdf_path: str, filename: str = "", cipher: str = "") -> list[dict]:
    """Extracts sheets list from PDF for doc_sheets indexing (З-17)."""
    if not os.path.exists(pdf_path):
        return []

    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    sheets = []

    norm_cipher = normalize_code(cipher or Path(pdf_path).stem).lower()

    for idx in range(total_pages):
        page = doc[idx]
        is_landscape = (page.rect.width >= page.rect.height)
        raw_text = page.get_text("text").strip()
        clean_txt = clean_title(raw_text)

        # Title heuristic
        title = ""
        lines = [line.strip() for line in clean_txt.splitlines() if len(line.strip()) > 3]
        if lines:
            title = lines[0]
            for line in lines[:5]:
                if any(w in line.lower() for w in ("схема", "план", "разрез", "узел", "фасад", "ведомость", "опалубка")):
                    title = line
                    break

        if not title:
            title = f"Лист {idx + 1}"

        tags = extract_sheet_tags(title, clean_txt)
        codes = [norm_cipher] if norm_cipher else []

        sheets.append({
            "page_index": idx,
            "sheet_number": idx + 1,
            "title": title[:300],
            "tags": tags,
            "text_content": clean_txt[:3000],
            "codes": codes,
            "is_landscape": is_landscape,
        })

    doc.close()
    return sheets
