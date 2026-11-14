# broadcast_bot.py — Telegram-бот для рассылки по выбранным чатам
# Python 3.10+, aiogram 3.x
# Установка: pip install "aiogram>=3.4"
# Запуск:    python broadcast_bot.py
#
# Роли:
#   владелец (ADMIN_IDS)      — всё: рассылка, админы, ручная выдача подписок
#   администратор (в БД)      — рассылка + ручная выдача подписок (/grant)
#   подписчик                 — рассылка, пока активна подписка (Telegram Stars)

import asyncio
import functools
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# --- НАСТРОЙКИ ---
# Берутся из переменных окружения; можно задать и прямо в коде, заменив значения после запятой
BOT_TOKEN = os.getenv("BOT_TOKEN", "СЮДА_ТОКЕН_ОТ_BOTFATHER")
_ADMIN_RAW = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = {int(x) for x in _ADMIN_RAW.split(",") if x.strip().isdigit()}

SUB_PRICES = {1: 20, 7: 100, 30: 370}  # дней: цена в звёздах
PAUSE_BETWEEN_SENDS = 0.15  # сек между отправками (~6-7 сообщ/сек, безопасный темп)

DB_PATH = Path(os.getenv("DB_PATH", "broadcast.db"))
db = sqlite3.connect(DB_PATH)
db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '')")
db.execute("CREATE TABLE IF NOT EXISTS subs (user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
db.commit()

router = Router()
log = logging.getLogger("broadcast")


# --- Вспомогательное ---
def kv_get(key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def is_owner(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_staff(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    return db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None


def sub_expiry(user_id: int) -> int | None:
    """Timestamp окончания подписки или None, если подписки нет/истекла."""
    row = db.execute("SELECT expires_at FROM subs WHERE user_id = ?", (user_id,)).fetchone()
    if row and row[0] > time.time():
        return row[0]
    return None


def extend_sub(user_id: int, days: int) -> int:
    """Продлевает подписку поверх оставшейся. Возвращает новый timestamp."""
    now = time.time()
    current = db.execute("SELECT expires_at FROM subs WHERE user_id = ?", (user_id,)).fetchone()
    base = max(now, current[0]) if current else now
    expires = int(base + days * 86400)
    db.execute(
        "INSERT INTO subs (user_id, expires_at) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at",
        (user_id, expires),
    )
    db.commit()
    return expires


def drop_sub(user_id: int) -> bool:
    cur = db.execute("DELETE FROM subs WHERE user_id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def add_admin(user_id: int, name: str) -> bool:
    cur = db.execute("INSERT OR IGNORE INTO admins (user_id, name) VALUES (?, ?)", (user_id, name))
    db.commit()
    return cur.rowcount > 0


def remove_admin(user_id: int) -> bool:
    cur = db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def admin_only(handler):
    """Только владельцы (ADMIN_IDS)."""
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not message.from_user or not is_owner(message.from_user.id):
            return
        return await handler(message, *args, **kwargs)
    return wrapper


def staff_only(handler):
    """Владелец или назначенный администратор."""
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not message.from_user or not is_staff(message.from_user.id):
            return
        return await handler(message, *args, **kwargs)
    return wrapper


def access_only(handler):
    """Владелец, админ или активный подписчик."""
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not message.from_user:
            return
        uid = message.from_user.id
        if not is_staff(uid) and sub_expiry(uid) is None:
            await message.answer("Нет доступа к рассылке. Оплати подписку: /buy")
            return
        return await handler(message, *args, **kwargs)
    return wrapper


def all_chats() -> list[tuple[int, str]]:
    return db.execute("SELECT chat_id, title FROM chats ORDER BY chat_id").fetchall()


def add_chat(chat_id: int, title: str) -> bool:
    cur = db.execute("INSERT OR IGNORE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title))
    db.commit()
    return cur.rowcount > 0


def remove_chat(chat_id: int) -> bool:
    cur = db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    db.commit()
    return cur.rowcount > 0


def parse_target(raw: str) -> int | str | None:
    """'@username' -> str, '-100123...' -> int, иначе None."""
    raw = raw.strip()
    if raw.startswith("@") and len(raw) > 1:
        return raw
    digits = raw.lstrip("-")
    if digits.isdigit():
        return int(raw)
    return None


# --- Рассылка ---
async def broadcast(bot: Bot, text: str | None = None, source: Message | None = None) -> tuple[int, int, int]:
    """Отправка всем выбранным чатам. Возвращает (отправлено, убрано, ошибок)."""
    sent = skipped = errors = 0
    for chat_id, _title in all_chats():
        try:
            if source is not None:
                await source.copy_to(chat_id)
            else:
                await bot.send_message(chat_id, text or "")
            sent += 1
        except TelegramRetryAfter as e:
            log.warning("FloodWait %s сек, чат %s", e.retry_after, chat_id)
            await asyncio.sleep(e.retry_after)
            try:
                if source is not None:
                    await source.copy_to(chat_id)
                else:
                    await bot.send_message(chat_id, text or "")
                sent += 1
            except Exception:
                errors += 1
        except TelegramForbiddenError:
            remove_chat(chat_id)
            skipped += 1
        except Exception as e:
            errors += 1
            log.warning("Чат %s: %s: %s", chat_id, type(e).__name__, e)
        await asyncio.sleep(PAUSE_BETWEEN_SENDS)
    return sent, skipped, errors


# --- Хендлеры: общие ---
@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "Бот рассылки.\n\n"
        "Для подписчиков:\n"
        "/buy — купить подписку (Stars)\n"
        "/my — срок подписки\n"
        "/broadcast <текст> — разослать текст по всем чатам\n"
        "/settext <текст> — сохранить текст для рассылки\n\n"
        "Для администрации:\n"
        "/grant <id> <дней> — выдать подписку вручную\n"
        "/addchat, /delchat, /chats, /auto, /status — управление\n\n"
        "Для владельца:\n"
        "/addadmin <id> — назначить администратора\n"
        "/deladmin <id> — снять администратора\n"
        "/admins — список администраторов\n"
        "/revoke <id> — забрать подписку"
    )


@router.message(Command("my"))
async def cmd_my(msg: Message) -> None:
    if not msg.from_user:
        return
    uid = msg.from_user.id
    if is_owner(uid):
        role = "владелец"
    elif is_staff(uid):
        role = "администратор"
    else:
        role = "пользователь"
    exp = sub_expiry(uid)
    sub = f"подписка активна до {fmt_ts(exp)}" if exp else "подписки нет"
    await msg.answer(f"ID: <code>{uid}</code>\nРоль: {role}\n{sub}")


# --- Хендлеры: подписка и оплата (Stars) ---
@router.message(Command("buy"))
async def cmd_buy(msg: Message) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 день — {SUB_PRICES[1]}⭐", callback_data="buy_1")],
        [InlineKeyboardButton(text=f"7 дней — {SUB_PRICES[7]}⭐", callback_data="buy_7")],
        [InlineKeyboardButton(text=f"30 дней — {SUB_PRICES[30]}⭐", callback_data="buy_30")],
    ])
    exp = sub_expiry(msg.from_user.id) if msg.from_user else None
    extra = f"\nТвоя подписка активна до {fmt_ts(exp)} — новая продлит её сверху." if exp else ""
    await msg.answer("Выбери тариф (оплата Telegram Stars):", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def on_buy(cb: CallbackQuery) -> None:
    days = int(cb.data.split("_")[1])  # noqa: SLF001 — payload формируем сами
    price = SUB_PRICES[days]
    await cb.message.answer_invoice(
        title="Подписка на рассылку",
        description=f"Доступ к рассылке на {days} дн. (дн.: 1 — 20⭐, 7 — 100⭐, 30 — 370⭐)",
        payload=f"sub_{days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка {days} дн.", amount=price)],
    )
    await cb.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def on_paid(msg: Message) -> None:
    sp = msg.successful_payment
    payload = sp.invoice_payload
    if not payload.startswith("sub_"):
        log.warning("Неизвестный платежный payload: %s", payload)
        return
    days = int(payload.split("_")[1])
    expires = extend_sub(msg.from_user.id, days)
    log.info("Оплата: %s заплатил %s XTR за %s дн.", msg.from_user.id, sp.total_amount, days)
    await msg.answer(f"Оплата получена: {sp.total_amount}⭐\nПодписка активна до {fmt_ts(expires)}.")


# --- Хендлеры: управление подписками (владелец + админы) ---
@router.message(Command("grant"))
@staff_only
async def cmd_grant(msg: Message) -> None:
    parts = msg.text.split()
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Формат: /grant <user_id> <дней>  (пример: /grant 123456789 30)")
        return
    uid, days = int(parts[1]), int(parts[2])
    if days < 1:
        await msg.answer("Дней должно быть ≥ 1.")
        return
    expires = extend_sub(uid, days)
    await msg.answer(f"Подписка выдана: {uid} до {fmt_ts(expires)}.")
    try:
        await msg.bot.send_message(uid, f"Тебе выдана подписка на рассылку до {fmt_ts(expires)}.")
    except Exception:
        pass  # пользователь не писал боту — уведомить нельзя, подписка всё равно выдана


@router.message(Command("revoke"))
@admin_only
async def cmd_revoke(msg: Message) -> None:
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Формат: /revoke <user_id>")
        return
    await msg.answer("Подписка снята." if drop_sub(int(parts[1])) else "У этого пользователя нет подписки.")


# --- Хендлеры: администраторы (только владелец) ---
@router.message(Command("addadmin"))
@admin_only
async def cmd_addadmin(msg: Message) -> None:
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2:
        await msg.answer("Формат: /addadmin <user_id или @username>")
        return
    target = parse_target(parts[1])
    if target is None:
        await msg.answer("Нужен числовой ID или @username.")
        return
    name = parts[2] if len(parts) > 2 else ""
    if isinstance(target, str):
        try:
            chat = await msg.bot.get_chat(target)
            target, name = chat.id, name or (chat.full_name or target)
        except Exception:
            await msg.answer("Не удалось найти пользователя. Проще выдать числовой ID.")
            return
    if add_admin(target, name):
        await msg.answer(f"Назначен администратор: {target}")
        try:
            await msg.bot.send_message(target, "Тебе выданы права администратора бота рассылки.")
        except Exception:
            pass
    else:
        await msg.answer("Этот пользователь уже администратор.")


@router.message(Command("deladmin"))
@admin_only
async def cmd_deladmin(msg: Message) -> None:
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Формат: /deladmin <user_id>")
        return
    await msg.answer("Админ снят." if remove_admin(int(parts[1])) else "Этот пользователь не администратор.")


@router.message(Command("admins"))
@admin_only
async def cmd_admins(msg: Message) -> None:
    rows = db.execute("SELECT user_id, name FROM admins").fetchall()
    if not rows:
        await msg.answer("Администраторов нет. Назначить: /addadmin <id>")
        return
    await msg.answer("Администраторы:\n" + "\n".join(f"• {name or '—'} — <code>{uid}</code>" for uid, name in rows))


# --- Хендлеры: рассылка ---
@router.message(Command("addchat"))
@staff_only
async def cmd_addchat(msg: Message) -> None:
    arg = msg.text.split(maxsplit=1)[1] if len(msg.text.split(maxsplit=1)) > 1 else None
    if arg:
        target = parse_target(arg)
        if target is None:
            await msg.answer("Формат: /addchat -1001234567890 или /addchat @channelname")
            return
        try:
            chat = await msg.bot.get_chat(target)
        except Exception as e:
            await msg.answer(f"Не удалось найти чат: {e}\nБот должен состоять в этом чате.")
            return
        if add_chat(chat.id, chat.title or chat.username or str(chat.id)):
            await msg.answer(f"Добавлен: {chat.title or chat.username} ({chat.id})")
        else:
            await msg.answer("Этот чат уже в списке.")
        return

    if msg.chat.type == "private":
        await msg.answer("Эту команду нужно писать внутри чата, либо указать ID: /addchat <id|@канал>")
        return
    member = await msg.bot.get_chat_member(msg.chat.id, msg.from_user.id)
    if member.status not in ("administrator", "creator"):
        await msg.answer("Только админ этого чата может его добавить.")
        return
    if add_chat(msg.chat.id, msg.chat.title or msg.chat.username or str(msg.chat.id)):
        await msg.answer("Чат добавлен в рассылку.")
    else:
        await msg.answer("Этот чат уже в списке.")


@router.message(Command("delchat"))
@staff_only
async def cmd_delchat(msg: Message) -> None:
    arg = msg.text.split(maxsplit=1)[1].strip() if len(msg.text.split(maxsplit=1)) > 1 else None
    if arg:
        target = parse_target(arg)
        if target is None:
            await msg.answer("Формат: /delchat -1001234567890 или /delchat @channelname")
            return
        try:
            chat = await msg.bot.get_chat(target)
            removed = remove_chat(chat.id)
        except Exception:
            removed = remove_chat(int(target) if isinstance(target, int) else target)
        await msg.answer("Убран из рассылки." if removed else "Такого чата нет в списке.")
    elif msg.chat.type != "private":
        await msg.answer("Убран из рассылки." if remove_chat(msg.chat.id) else "Этого чата нет в списке.")
    else:
        await msg.answer("Пиши внутри чата или укажи ID: /delchat <id|@канал>")


@router.message(Command("chats"))
@staff_only
async def cmd_chats(msg: Message) -> None:
    chats = all_chats()
    if not chats:
        await msg.answer("Список пуст. Добавь чаты: /addchat")
        return
    lines = [f"• {title} — `{chat_id}`" for chat_id, title in chats]
    await msg.answer(f"Чатов в рассылке: {len(chats)}\n" + "\n".join(lines))


@router.message(Command("settext"))
@access_only
async def cmd_settext(msg: Message) -> None:
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Формат: /settext <текст рассылки>")
        return
    kv_set("text", parts[1])
    await msg.answer("Текст сохранён. Отправить сейчас: /broadcast")


@router.message(Command("broadcast"))
@access_only
async def cmd_broadcast(msg: Message) -> None:
    parts = msg.text.split(maxsplit=1)
    source = msg.reply_to_message
    text = parts[1] if len(parts) > 1 else None
    if source is None and text is None:
        stored = kv_get("text")
        if not stored:
            await msg.answer("Нет текста. Напиши /broadcast <текст> или ответь командой на сообщение.")
            return
        text = stored
    if not all_chats():
        await msg.answer("Список чатов пуст.")
        return
    notice = await msg.answer("Рассылка запущена…")
    sent, skipped, errors = await broadcast(msg.bot, text=text, source=source)
    await notice.edit_text(f"Готово. Отправлено: {sent}, убрано (бота выгнали): {skipped}, ошибок: {errors}")


@router.message(Command("auto"))
@staff_only
async def cmd_auto(msg: Message) -> None:
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Формат: /auto <минуты> или /auto off")
        return
    arg = parts[1].strip().lower()
    if arg in ("off", "выкл", "0"):
        kv_set("auto_minutes", "0")
        await msg.answer("Автопостинг выключен.")
        return
    if not arg.isdigit() or int(arg) < 1:
        await msg.answer("Нужны целые минуты, минимум 1. Пример: /auto 60")
        return
    kv_set("auto_minutes", arg)
    kv_set("last_auto_ts", str(time.time()))
    await msg.answer(f"Автопостинг включён: каждые {arg} мин уходит сохранённый текст (/settext).")


@router.message(Command("status"))
@staff_only
async def cmd_status(msg: Message) -> None:
    minutes = kv_get("auto_minutes", "0")
    text = kv_get("text") or "(не задан)"
    preview = text if len(text) <= 200 else text[:200] + "…"
    auto = f"каждые {minutes} мин" if minutes != "0" else "выключен"
    subs_active = db.execute("SELECT COUNT(*) FROM subs WHERE expires_at > ?", (time.time(),)).fetchone()[0]
    await msg.answer(
        f"Чатов: {len(all_chats())}\nАвтопостинг: {auto}\nАктивных подписок: {subs_active}\nСохранённый текст:\n{preview}"
    )


# --- Автопостинг ---
async def auto_loop(bot: Bot) -> None:
    while True:
        try:
            minutes = int(kv_get("auto_minutes", "0") or 0)
            if minutes > 0:
                last = float(kv_get("last_auto_ts", "0") or 0)
                if time.time() - last >= minutes * 60:
                    kv_set("last_auto_ts", str(time.time()))
                    text = kv_get("text")
                    if text:
                        sent, skipped, errors = await broadcast(bot, text=text)
                        log.info("Автопостинг: отправлено %s, убрано %s, ошибок %s", sent, skipped, errors)
        except Exception:
            log.exception("Ошибка в auto_loop")
        await asyncio.sleep(30)


# --- Запуск ---
async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)
    worker = asyncio.create_task(auto_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
