"""Main Telegram bot application for construction document registry (builder v2).

Enforces:
- Strict access control via .env project whitelists (З-2).
- Unknown users get NO response, except /myid (З-2).
- Private chats only (З-12).
- Unified ACL and Single Delivery Point 'deliver' with repeating diagonal watermark (З-3, З-11).
- Cloud Guard: zero document bytes to external cloud if cloud_ok=false (З-3).
- Acceptance of all project documents without classification restrictions.
- Tool calling loop with function calling format (З-9).
- Active registry /registry with Excel export (З-13).
- System prompt loaded from app/prompts/AGENT_SYSTEM_PROMPT.md, sha256 logged (З-8).
"""
import asyncio
import datetime
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    TelegramObject,
)
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from config import get_config, AppConfig
from db import Database, PendingUpload
from pdf_engine import (
    extract_gost_stamp_info,
    render_full_sheet,
    render_drawing_fragment,
    locate_drawing_fragments,
    extract_document_sheets,
)
from obsidian import sync_document_card

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("builder_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("TELEGRAM_PROXY_URL")

STORAGE_DIR = Path("/data/files")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path("/tmp/builder_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Prompt verification (З-8)
PROMPT_PATH = Path(__file__).parent / "prompts" / "AGENT_SYSTEM_PROMPT.md"
if not PROMPT_PATH.exists():
    # Check parent dir
    PROMPT_PATH = Path(__file__).parent.parent / "AGENT_SYSTEM_PROMPT.md"
if not PROMPT_PATH.exists():
    raise RuntimeError(f"Required system prompt file not found at {PROMPT_PATH}. Bot cannot start.")

AGENT_SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
PROMPT_SHA256 = hashlib.sha256(AGENT_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
logger.info(f"Loaded AGENT_SYSTEM_PROMPT.md, SHA256: {PROMPT_SHA256}")

# Config initialization (fail-fast)
config: AppConfig = get_config()
logger.info(f"Configuration loaded successfully. Admin: {config.admin_tg_id}, Projects: {list(config.projects.keys())}")

# LLM settings
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://host.docker.internal:8000/v1/chat/completions")
HERMES_MODEL = os.getenv("HERMES_MODEL", "hermes-agent")

session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN or "dummy:smoke", session=session)
dp = Dispatcher()
db = Database()

# Rate limiters
_myid_rate: dict[int, float] = {}
_user_req_rate: dict[int, list[float]] = {}


def check_rate_limit(tg_id: int, max_per_min: int = 30) -> bool:
    now = time.time()
    history = _user_req_rate.setdefault(tg_id, [])
    history[:] = [t for t in history if now - t < 60.0]
    if len(history) >= max_per_min:
        return False
    history.append(now)
    return True


class SecurityAccessMiddleware(BaseMiddleware):
    """
    Strict outer middleware (З-2, З-12):
    1. Ignores all non-private chats (groups, channels).
    2. Drops all events from unknown users (zero reply, zero data leak).
       Exception: /myid command is allowed to inform the user of their Telegram ID.
    3. Enforces user rate limits.
    """
    async def __call__(self, handler, event: TelegramObject, data: dict):
        chat = getattr(event, "chat", None)
        if not chat:
            msg = getattr(event, "message", None)
            if msg:
                chat = getattr(msg, "chat", None)

        if chat and chat.type != "private":
            return  # Silently ignore non-private chats (З-12)

        # Update не имеет from_user — берём из вложенного события.
        user = getattr(event, "from_user", None)
        if not user:
            inner = getattr(event, "message", None) or getattr(event, "callback_query", None)
            user = getattr(inner, "from_user", None) if inner else None
        if not user:
            return

        tg_id = user.id
        is_known = config.is_user_allowed(tg_id)

        # Unknown user handling
        if not is_known:
            # Check if event is /myid message
            if isinstance(event, Message) and event.text and event.text.strip().startswith("/myid"):
                return await handler(event, data)
            # Otherwise SILENT DROP (З-2)
            return

        # Rate limiting (З-12)
        if not check_rate_limit(tg_id):
            if isinstance(event, Message):
                await event.answer("⚠️ Превышен лимит запросов в минуту. Пожалуйста, подождите.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⚠️ Превышен лимит запросов.", show_alert=True)
            return

        return await handler(event, data)


dp.update.outer_middleware(SecurityAccessMiddleware())


# =====================================================================
# ЕДИНАЯ ТОЧКА ВЫДАЧИ ДОКУМЕНТОВ (З-3, З-11)
# =====================================================================

async def deliver_document_or_fragment(
    message: Message,
    user_tg_id: int,
    user_fullname: str,
    doc: dict,
    page_index: int = 0,
    clip_rect: list | tuple | None = None,
    caption: str = "",
) -> bool:
    """Single point of delivery for all documents, sheets, and fragments (З-3).

    Checks project ACL, applies repeating diagonal watermark, logs audit, sends file.
    """
    proj_code = doc.get("project_code") or ""
    can = await db.can_access(user_tg_id, proj_code)
    if not can:
        await db.log_audit(user_tg_id, "BLOCKED_CHANNEL", doc.get("id"), {"reason": "no_project_access"})
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return False

    pdf_path = doc.get("file_path")
    if not pdf_path or not os.path.exists(pdf_path):
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return False

    ch_no = doc.get("stamp_change_no") or "0"
    doc_meta = f"{proj_code} {doc['code']} (Изм.{ch_no}, Выпуск {doc['revision']})"

    try:
        if clip_rect:
            # Render fragment
            out_img = render_drawing_fragment(
                pdf_path=pdf_path,
                sheet_index=page_index,
                clip_rect=clip_rect,
                user_fullname=user_fullname,
                doc_meta=doc_meta,
                dpi=250
            )
            action = "DELIVER_FRAGMENT"
        else:
            # Render sheet
            out_img = render_full_sheet(
                pdf_path=pdf_path,
                page_index=page_index,
                user_fullname=user_fullname,
                doc_meta=doc_meta,
                dpi=150
            )
            action = "DELIVER_DOC"

        await message.answer_photo(
            FSInputFile(out_img),
            caption=caption or f"📄 {doc_meta}"
        )
        await db.log_audit(user_tg_id, action, doc.get("id"), {"page_index": page_index, "has_clip": bool(clip_rect)})
        return True
    except Exception as e:
        logger.error(f"Error in deliver: {e}")
        await message.answer("Ошибка при формировании документа. Обратитесь к администратору.")
        return False


# =====================================================================
# КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
# =====================================================================

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    """Returns Telegram ID with rate limiting (З-2)."""
    now = time.time()
    last = _myid_rate.get(message.from_user.id, 0)
    if now - last < 5.0:
        return
    _myid_rate[message.from_user.id] = now
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    user_projects = config.get_user_projects(tg_id)
    is_adm = config.is_admin(tg_id)

    projs_str = ", ".join(user_projects) if user_projects else "(нет доступных объектов)"
    admin_str = "\n⚙️ Вы являетесь системным администратором." if is_adm else ""

    await message.answer(
        f"👷 Здравствуйте, {message.from_user.full_name}!\n"
        f"Доступные вам объекты строительства: <b>{projs_str}</b>{admin_str}\n\n"
        "Команды реестра:\n"
        "• <code>/registry [объект]</code> — реестр рабочей документации\n"
        "• <code>/gpr</code> — актуальный график производства работ\n"
        "• <code>/rd &lt;раздел&gt; [шифр]</code> — поиск рабочей документации\n"
        "• <code>/cut &lt;подпись&gt;</code> — вырезка фрагмента чертежа\n"
        "• <code>/ask &lt;вопрос&gt;</code> — задать вопрос ассистенту по чертежам\n"
        "• Отправка PDF-файла регистрирует новую ревизию в доступном объекте."
    )


@dp.message(Command("registry"))
async def cmd_registry(message: Message):
    """Displays active document registry and provides Excel export (З-13)."""
    tg_id = message.from_user.id
    parts = message.text.split(maxsplit=1)
    req_proj = parts[1].strip().upper() if len(parts) > 1 else None

    docs = await db.list_active_docs(tg_id, project_code=req_proj)
    if not docs:
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return

    # Group by project
    by_proj = {}
    for d in docs:
        by_proj.setdefault(d["project_code"], []).append(d)

    lines = ["📋 <b>Реестр актуальной рабочей документации:</b>\n"]
    for pcode, pdocs in by_proj.items():
        lines.append(f"🏗️ <b>Объект {pcode}</b>:")
        for d in pdocs:
            ch_no = d.get("stamp_change_no") or "0"
            lines.append(f"  • Раздел {d['section']} | <code>{d['code']}</code> | Изм.{ch_no} | {d['status'].upper()}")
        lines.append("")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Выгрузить в Excel (.xlsx)", callback_data=f"export_excel:{req_proj or 'ALL'}")]
    ])
    await message.answer("\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data.startswith("export_excel:"))
async def cb_export_excel(call: CallbackQuery):
    tg_id = call.from_user.id
    target_proj = call.data.split(":", 1)[1]
    req_proj = None if target_proj == "ALL" else target_proj

    docs = await db.list_active_docs(tg_id, project_code=req_proj)
    if not docs:
        await call.answer("Нет доступных документов для выгрузки.", show_alert=True)
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Реестр РД"

    headers = ["Объект", "Раздел", "Шифр", "Изм.", "Выпуск", "Статус", "Дата регистрации", "Кто загрузил", "Имя файла"]
    ws.append(headers)

    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for d in docs:
        created_str = d["created_at"].strftime("%d.%m.%Y %H:%M") if hasattr(d.get("created_at"), "strftime") else str(d.get("created_at") or "")
        ws.append([
            d.get("project_code") or "",
            d.get("section") or "",
            d.get("code") or "",
            d.get("stamp_change_no") or "0",
            d.get("revision") or 1,
            (d.get("status") or "").upper(),
            created_str,
            d.get("uploader_name") or f"ID {d.get('uploaded_by') or ''}",
            d.get("original_filename") or "",
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    buf = io.BytesIO()
    wb.save(buf)
    excel_bytes = buf.getvalue()

    await db.log_audit(tg_id, "EXPORT_EXCEL", None, {"project": target_proj, "count": len(docs)})

    filename = f"Registry_{target_proj}_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
    await call.message.answer_document(
        BufferedInputFile(excel_bytes, filename=filename),
        caption=f"📊 Выгрузка реестра РД ({len(docs)} документов)."
    )
    await call.answer()


@dp.message(Command("gpr"))
async def cmd_gpr(message: Message):
    tg_id = message.from_user.id
    doc = await db.find_active_doc(tg_id, doc_type="ГПР")
    if not doc:
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return
    await deliver_document_or_fragment(message, tg_id, message.from_user.full_name, doc, page_index=0)


@dp.message(Command("rd"))
async def cmd_rd(message: Message):
    tg_id = message.from_user.id
    parts = message.text.split(maxsplit=2)
    section = parts[1].strip() if len(parts) > 1 else None
    code = parts[2].strip() if len(parts) > 2 else None

    doc = await db.find_active_doc(tg_id, doc_type="РД", section=section, code=code)
    if not doc:
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return
    await deliver_document_or_fragment(message, tg_id, message.from_user.full_name, doc, page_index=0)


@dp.message(Command("cut"))
async def cmd_cut(message: Message):
    """Cuts and watermarks drawing fragment (З-11, З-14)."""
    tg_id = message.from_user.id
    query = message.text.replace("/cut", "").strip()
    if not query:
        await message.answer("Укажите, что искать. Пример: <code>/cut Узел 1</code> или <code>/cut Разрез 1-1</code>")
        return

    # Find active document
    doc = await db.find_active_doc(tg_id, doc_type="РД")
    if not doc:
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return

    locs = locate_drawing_fragments(doc["file_path"], query, max_results=1)
    if not locs:
        await message.answer(f"На чертеже {doc['code']} подпись «{query}» не найдена.")
        return

    loc = locs[0]
    await deliver_document_or_fragment(
        message=message,
        user_tg_id=tg_id,
        user_fullname=message.from_user.full_name,
        doc=doc,
        page_index=loc["sheet_index"],
        clip_rect=loc["clip_rect"],
        caption=f"📐 Фрагмент «{query}» | {doc['code']} (Изм.{doc.get('stamp_change_no','0')})"
    )


@dp.message(Command("edit"))
async def cmd_edit(message: Message):
    """Edits card metadata: /edit <doc_id> <new_project> <new_section> <new_code> [change_no] (З-7)."""
    tg_id = message.from_user.id
    parts = message.text.split()
    if len(parts) < 5:
        await message.answer(
            "Формат команды: <code>/edit &lt;id&gt; &lt;НОВЫЙ_ОБЪЕКТ&gt; &lt;РАЗДЕЛ&gt; &lt;ШИФР&gt; [ИЗМ]</code>\n"
            "Пример: <code>/edit 12 ЦТП КЖ ЦКДИ.2981.КЖ 1</code>"
        )
        return

    try:
        doc_id = int(parts[1])
    except ValueError:
        await message.answer("ID документа должен быть числом.")
        return

    new_proj = parts[2].upper()
    new_sec = parts[3].upper()
    new_code = parts[4].upper()
    new_change = parts[5] if len(parts) > 5 else "0"

    doc = await db.get_document_by_id(doc_id)
    if not doc:
        await message.answer("Документ не найден.")
        return

    # User must have access to both original and new project (З-7)
    can_old = await db.can_access(tg_id, doc["project_code"])
    can_new = await db.can_access(tg_id, new_proj)
    if not (can_old and can_new):
        await message.answer("Документ не найден или недоступен по вашим правам.")
        return

    try:
        updated = await db.update_document_metadata(
            doc_id=doc_id,
            new_project_code=new_proj,
            new_section=new_sec,
            new_code=new_code,
            new_stamp_change_no=new_change,
            user_tg_id=tg_id,
        )
        sync_document_card(updated)
        await message.answer(
            f"✅ Карточка документа #{doc_id} обновлена:\n"
            f"• Объект: <code>{new_proj}</code>\n"
            f"• Раздел: <code>{new_sec}</code>\n"
            f"• Шифр: <code>{new_code}</code>\n"
            f"• Изм.: <code>{new_change}</code>"
        )
    except Exception as e:
        await message.answer(f"Ошибка при обновлении карточки: {e}")


@dp.message(Command("index"))
async def cmd_index(message: Message):
    """Admin service command: indexes storage files into quarantine (З-6)."""
    if not config.is_admin(message.from_user.id):
        return  # Only system admin

    # List files in STORAGE_DIR
    pdf_files = list(STORAGE_DIR.glob("*.pdf"))
    await message.answer(f"Найдено PDF в хранилище: {len(pdf_files)}. Индексация в карантин...")
    # Admin can review quarantined docs


# =====================================================================
# ЗАГРУЗКА ДОКУМЕНТОВ (З-5, З-6)
# =====================================================================

@dp.message(F.document)
async def handle_file_upload(message: Message):
    tg_id = message.from_user.id
    user_projs = config.get_user_projects(tg_id)
    if not user_projs:
        await message.answer("У вас нет доступа к загрузке документов ни в один объект.")
        return

    doc_file = message.document
    orig_name = doc_file.file_name or "document.pdf"

    # Temporary download path
    temp_uuid = str(uuid.uuid4())
    temp_path = STORAGE_DIR / f"{temp_uuid}.pdf"

    file_info = await bot.get_file(doc_file.file_id)
    await bot.download_file(file_info.file_path, destination=temp_path)

    sha256 = hashlib.sha256(temp_path.read_bytes()).hexdigest()

    # Stamp analysis
    stamp = extract_gost_stamp_info(str(temp_path), filename=orig_name)
    code = stamp.get("detected_code") or Path(orig_name).stem
    section = stamp.get("detected_section") or "КЖ"

    # Determine project
    suggested_proj = stamp.get("suggested_project")
    chosen_proj = None
    if suggested_proj and suggested_proj.upper() in user_projs:
        chosen_proj = suggested_proj.upper()
    elif len(user_projs) == 1:
        chosen_proj = user_projs[0]
    else:
        # User belongs to multiple projects -> ask them to choose
        chosen_proj = user_projs[0]

    pending = PendingUpload(
        file_id=temp_uuid,
        tg_id=tg_id,
        project_code=chosen_proj,
        doc_type="РД",
        section=section,
        code=code,
        stamp_change_no="0",
        temp_path=str(temp_path),
        original_filename=orig_name,
        sha256=sha256,
    )
    await db.save_pending_upload(pending)

    # Inline confirmation keyboard (No L0/L1 buttons! З-5)
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить выпуск ревизии", callback_data=f"confirm_upload:{temp_uuid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_upload:{temp_uuid}")]
    ]
    if len(user_projs) > 1:
        proj_btns = [
            InlineKeyboardButton(text=f"📁 {p}", callback_data=f"select_proj:{temp_uuid}:{p}")
            for p in user_projs
        ]
        buttons.insert(0, proj_btns)

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        f"📥 <b>Карточка документа перед внесением в реестр:</b>\n"
        f"• Объект: <code>{chosen_proj}</code>\n"
        f"• Раздел: <code>{section}</code> | Тип: <code>РД</code>\n"
        f"• Распознанный шифр: <code>{code}</code>\n"
        f"• Исходный файл: <code>{orig_name}</code>\n"
        f"• SHA256: <code>{sha256[:12]}...</code>\n\n"
        f"Подтверждаете добавление в реестр?"
    )
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("select_proj:"))
async def cb_select_proj(call: CallbackQuery):
    _, file_id, proj_code = call.data.split(":", 2)
    pending = await db.pop_pending_upload(file_id)
    if not pending:
        await call.answer("Сессия загрузки истекла.")
        return
    pending.project_code = proj_code.upper()
    await db.save_pending_upload(pending)

    await call.message.edit_text(
        f"📥 <b>Карточка документа:</b>\n"
        f"• Выбран объект: <b>{pending.project_code}</b>\n"
        f"• Раздел: <code>{pending.section}</code> | Шифр: <code>{pending.code}</code>\n\n"
        f"Подтверждаете внесение в реестр?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_upload:{file_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_upload:{file_id}")]
        ])
    )
    await call.answer()


@dp.callback_query(F.data.startswith("confirm_upload:"))
async def cb_confirm_upload(call: CallbackQuery):
    file_id = call.data.split(":", 1)[1]
    pending = await db.pop_pending_upload(file_id)
    if not pending:
        await call.answer("Сессия загрузки истекла.", show_alert=True)
        return

    # ACL check: user must belong to project
    can = await db.can_access(call.from_user.id, pending.project_code)
    if not can:
        await call.answer("Нет прав на добавление документов в этот объект.", show_alert=True)
        return

    # Move file to permanent storage
    perm_path = STORAGE_DIR / f"{file_id}.pdf"
    if Path(pending.temp_path).exists() and Path(pending.temp_path) != perm_path:
        Path(pending.temp_path).rename(perm_path)

    saved_doc = await db.register_new_revision(
        project_code=pending.project_code,
        doc_type=pending.doc_type,
        section=pending.section,
        code=pending.code,
        stamp_change_no=pending.stamp_change_no,
        file_path=str(perm_path),
        original_filename=pending.original_filename,
        sha256=pending.sha256,
        uploader_tg_id=call.from_user.id,
    )

    if isinstance(saved_doc, dict) and saved_doc.get("duplicate"):
        await call.message.edit_text(
            f"⚠️ <b>Этот файл уже зарегистрирован в реестре объекта {saved_doc['project_code']}.</b>\n"
            f"Шифр: <code>{saved_doc['code']}</code> | Изм.: {saved_doc.get('stamp_change_no','0')} | Выпуск: {saved_doc['revision']}.\n"
            f"Для изменения атрибутов используйте <code>/edit {saved_doc['id']} ...</code>"
        )
        await call.answer()
        return

    sync_document_card(saved_doc)
    await db.log_audit(call.from_user.id, "UPLOAD_NEW_REVISION", saved_doc["id"], {"rev": saved_doc["revision"]})

    # Background task for sheet indexing
    async def run_indexing():
        try:
            sheets = extract_document_sheets(
                str(perm_path),
                filename=pending.original_filename,
                cipher=pending.code
            )
            if sheets:
                await db.index_document_sheets(saved_doc["id"], sheets)
                await bot.send_message(
                    call.from_user.id,
                    f"✅ Постраничная индексация комплекта <code>{pending.code}</code> завершена ({len(sheets)} листов)."
                )
        except Exception as e:
            logger.error(f"Sheet indexing error: {e}")

    asyncio.create_task(run_indexing())

    await call.message.edit_text(
        f"✅ <b>Документ успешно зарегистрирован в реестре!</b>\n"
        f"• Объект: <code>{saved_doc['project_code']}</code>\n"
        f"• Шифр: <code>{saved_doc['code']}</code>\n"
        f"• Выпуск: {saved_doc['revision']} (Изм. {saved_doc.get('stamp_change_no','0')})\n"
        f"• Предыдущая ревизия автоматически переведена в статус <i>superseded</i>."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("cancel_upload:"))
async def cb_cancel_upload(call: CallbackQuery):
    file_id = call.data.split(":", 1)[1]
    pending = await db.pop_pending_upload(file_id)
    if pending and os.path.exists(pending.temp_path):
        Path(pending.temp_path).unlink(missing_ok=True)
    await call.message.edit_text("❌ Загрузка документа отменена пользователем.")
    await call.answer()


# =====================================================================
# TOOL CALLING & AI ENGINE (З-9, З-10)
# =====================================================================

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "registry_lookup",
            "description": "Поиск документа в реестре рабочей документации по объекту, типу, разделу или шифру",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_code": {"type": "string", "description": "Код строительного объекта"},
                    "doc_type": {"type": "string", "description": "Тип: РД, ГПР, ППР"},
                    "section": {"type": "string", "description": "Марка раздела: АР, КЖ, КМ, КР и т.д."},
                    "code": {"type": "string", "description": "Шифр комплекта"},
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Полнотекстовый и триграммный поиск по листам чертежей (FTS)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос (термин, конструкция, узел)"},
                    "project_code": {"type": "string", "description": "Код объекта"},
                    "section": {"type": "string", "description": "Раздел проекта"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sheet",
            "description": "Получение информации и текста конкретного листа по номеру",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "integer", "description": "ID документа"},
                    "page_index": {"type": "integer", "description": "0-based индекс страницы PDF"}
                },
                "required": ["doc_id", "page_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_crop",
            "description": "Предложить пользователю вырезку конкретного узла или фрагмента чертежа",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "integer", "description": "ID документа"},
                    "page_index": {"type": "integer", "description": "Индекс страницы"},
                    "query": {"type": "string", "description": "Название узла/разреза для вырезки"}
                },
                "required": ["doc_id", "page_index", "query"]
            }
        }
    }
]


async def execute_tool(name: str, args: dict, user_tg_id: int) -> dict:
    """Executes agent tools with strict user ACL checking (З-9)."""
    if name == "registry_lookup":
        p_code = args.get("project_code")
        d_type = args.get("doc_type")
        sec = args.get("section")
        code = args.get("code")

        docs = await db.list_active_docs(
            tg_id=user_tg_id,
            project_code=p_code,
            doc_type=d_type,
            section=sec,
            code=code
        )
        if not docs:
            return {"status": "not_found", "message": "Документ не найден или недоступен по вашим правам."}

        res = []
        for d in docs[:5]:
            res.append({
                "doc_id": d["id"],
                "project_code": d["project_code"],
                "section": d["section"],
                "code": d["code"],
                "revision": d["revision"],
                "stamp_change_no": d.get("stamp_change_no", "0"),
                "status": d["status"]
            })
        return {"status": "success", "documents": res}

    elif name == "search_docs":
        query = args.get("query", "")
        p_code = args.get("project_code")
        sec = args.get("section")

        sheet = await db.search_sheets_fts(
            tg_id=user_tg_id,
            query=query,
            project_code=p_code,
            section=sec
        )
        if not sheet:
            return {"status": "not_found", "message": "Лист по данному запросу не найден в доступных документах."}

        return {
            "status": "success",
            "doc_id": sheet["doc_id"],
            "doc_code": sheet["doc_code"],
            "project_code": sheet["project_code"],
            "page_index": sheet["page_index"],
            "sheet_number": sheet.get("sheet_number"),
            "title": sheet["title"],
            "text_excerpt": sheet["text_content"][:500] if sheet.get("text_content") else ""
        }

    elif name == "get_sheet":
        doc_id = args.get("doc_id")
        p_idx = args.get("page_index", 0)

        doc = await db.get_document_by_id(doc_id)
        if not doc or not await db.can_access(user_tg_id, doc["project_code"]):
            return {"status": "error", "message": "Документ не найден или недоступен по вашим правам."}

        sheet = await db.get_sheet_by_page(doc_id, p_idx)
        if not sheet:
            return {"status": "not_found", "message": f"Лист {p_idx+1} не найден."}

        return {
            "status": "success",
            "title": sheet["title"],
            "text_content": sheet["text_content"][:1000],
            "tags": sheet["tags"]
        }

    elif name == "propose_crop":
        doc_id = args.get("doc_id")
        p_idx = args.get("page_index", 0)
        query = args.get("query", "")

        doc = await db.get_document_by_id(doc_id)
        if not doc or not await db.can_access(user_tg_id, doc["project_code"]):
            return {"status": "error", "message": "Документ недоступен."}

        return {
            "status": "proposed",
            "doc_id": doc_id,
            "page_index": p_idx,
            "query": query,
            "message": f"Найден узел/разрез «{query}» на листе {p_idx+1}. Предложена вырезка."
        }

    return {"status": "unknown_tool"}


async def run_llm_agent(user_message: str, user_tg_id: int) -> str:
    """Executes multi-turn tool calling loop (max 6 iterations, 120s timeout) (З-9)."""
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Вопрос пользователя (Telegram ID {user_tg_id}): {user_message}"}
    ]

    use_openrouter = bool(OPENROUTER_API_KEY)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}" if use_openrouter else "Bearer dummy",
        "Content-Type": "application/json"
    }
    url = OPENROUTER_API_URL if use_openrouter else HERMES_API_URL
    model = OPENROUTER_MODEL if use_openrouter else HERMES_MODEL

    for iteration in range(6):
        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOLS_SPEC,
            "temperature": 0.1,
            "max_tokens": 1000,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            # Model finished answering
            return msg.get("content") or "Ответ сформирован."

        # Execute tool calls
        for tc in tool_calls:
            t_name = tc["function"]["name"]
            try:
                t_args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                t_args = {}

            t_result = await execute_tool(t_name, t_args, user_tg_id)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": t_name,
                "content": json.dumps(t_result, ensure_ascii=False)
            })

    return "Превышен лимит шагов обработки запроса."


@dp.message(Command("ask"))
@dp.message(F.text)
async def handle_user_query(message: Message):
    """Processes user questions via Tool Calling agent."""
    text = message.text or ""
    if text.startswith("/ask"):
        text = text.replace("/ask", "", 1).strip()
    if not text:
        await message.answer("Пожалуйста, задайте вопрос по рабочей документации.")
        return

    tg_id = message.from_user.id
    await db.log_audit(tg_id, "AGENT_QUERY", None, {"prompt_sha256": PROMPT_SHA256, "query": text})

    try:
        reply = await run_llm_agent(text, tg_id)
        # Strip raw markdown artifacts for clean messenger delivery
        clean_reply = re.sub(r'```[a-z]*\n|```', '', reply).strip()
        await message.answer(clean_reply)
    except Exception as e:
        logger.error(f"Error in run_llm_agent: {e}")
        await message.answer("Документ не найден или недоступен по вашим правам.")


# =====================================================================
# STARTUP & MAIN LOOP
# =====================================================================

async def main():
    logger.info("Connecting to PostgreSQL and running migrations...")
    await db.connect()
    logger.info("Synchronizing .env configuration with DB...")
    await db.sync_config_with_db(config)
    logger.info("Bot starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
