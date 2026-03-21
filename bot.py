import os
import asyncio
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "8710757819:AAFra83pBHkxPT9m6BYJRY9kEh8Akry39gI")
DB_NAME = os.getenv("DB_NAME", "bot.db")

MAX_REMINDERS = 10
MAX_TITLE_LEN = 64
MAX_TIMES = 10
MIN_INTERVAL = 1

WEEKDAYS = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Вс",
}


# =========================
# БАЗА
# =========================
def db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(cur, table_name: str, column_name: str, column_sql: str):
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in cur.fetchall()}
    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        country TEXT,
        timezone TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        target_date TEXT NOT NULL,
        exact_end_time TEXT,
        frequency_type TEXT NOT NULL,
        mode_type TEXT NOT NULL,
        times_text TEXT,
        weekdays_text TEXT,
        start_time TEXT,
        end_time TEXT,
        interval_minutes INTEGER,
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sent_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER NOT NULL,
        sent_key TEXT NOT NULL,
        UNIQUE(reminder_id, sent_key)
    )
    """)

    ensure_column(cur, "users", "notifications_hint_shown", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(cur, "users", "seen_intro", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(cur, "reminders", "reminder_kind", "TEXT NOT NULL DEFAULT 'regular'")
    ensure_column(cur, "reminders", "remind_date", "TEXT")

    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def set_user_country_timezone(user_id: int, country: str, timezone_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, country, timezone)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            country = excluded.country,
            timezone = excluded.timezone
    """, (user_id, country, timezone_name))
    conn.commit()
    conn.close()


def mark_notifications_hint_shown(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET notifications_hint_shown = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def mark_seen_intro(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET seen_intro = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_active_count(user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ? AND active = 1", (user_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def create_reminder_db(
    user_id: int,
    title: str,
    target_date: str,
    exact_end_time: str | None,
    frequency_type: str,
    mode_type: str,
    times_text: str | None,
    weekdays_text: str | None,
    start_time: str | None,
    end_time: str | None,
    interval_minutes: int | None,
    reminder_kind: str = "regular",
    remind_date: str | None = None,
):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reminders (
            user_id, title, target_date, exact_end_time, frequency_type, mode_type,
            times_text, weekdays_text, start_time, end_time, interval_minutes,
            active, reminder_kind, remind_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (
        user_id, title, target_date, exact_end_time, frequency_type, mode_type,
        times_text, weekdays_text, start_time, end_time, interval_minutes,
        reminder_kind, remind_date
    ))
    conn.commit()
    conn.close()


def update_reminder_db(reminder_id: int, user_id: int, data: dict):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE reminders
        SET title = ?,
            target_date = ?,
            exact_end_time = ?,
            frequency_type = ?,
            mode_type = ?,
            times_text = ?,
            weekdays_text = ?,
            start_time = ?,
            end_time = ?,
            interval_minutes = ?,
            reminder_kind = ?,
            remind_date = ?,
            active = 1
        WHERE id = ? AND user_id = ?
    """, (
        data["title"],
        data["target_date"],
        data.get("exact_end_time"),
        data["frequency_type"],
        data["mode_type"],
        data.get("times_text"),
        data.get("weekdays_text"),
        data.get("start_time"),
        data.get("end_time"),
        data.get("interval_minutes"),
        data.get("reminder_kind", "regular"),
        data.get("remind_date"),
        reminder_id,
        user_id,
    ))
    conn.commit()
    conn.close()


def get_user_reminders(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM reminders
        WHERE user_id = ? AND active = 1
    """, (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def delete_reminder_db(reminder_id: int, user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sent_log WHERE reminder_id = ?", (reminder_id,))
    cur.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
    conn.commit()
    conn.close()


def delete_all_reminders_db(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM sent_log
        WHERE reminder_id IN (SELECT id FROM reminders WHERE user_id = ?)
    """, (user_id,))
    cur.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def try_mark_sent(reminder_id: int, sent_key: str) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_log (reminder_id, sent_key) VALUES (?, ?)",
        (reminder_id, sent_key),
    )
    conn.commit()
    inserted = cur.rowcount > 0
    conn.close()
    return inserted


def deactivate_reminder(reminder_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE reminders SET active = 0 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


# =========================
# FSM
# =========================
class CountrySetup(StatesGroup):
    choose = State()


class CreateReminder(StatesGroup):
    title = State()
    target_date = State()
    ask_exact_end = State()
    exact_end_time = State()
    frequency = State()
    weekdays_pick = State()
    mode = State()
    fixed_times = State()
    periodic_range = State()
    periodic_interval = State()
    confirm = State()
    edit_field = State()


class OneTimeReminder(StatesGroup):
    title = State()
    target_date = State()
    remind_date = State()
    ask_exact_end = State()
    exact_end_time = State()
    mode = State()
    fixed_times = State()
    periodic_range = State()
    periodic_interval = State()
    confirm = State()
    edit_field = State()


class EditReminder(StatesGroup):
    pick = State()
    field = State()
    title = State()
    target_date = State()
    remind_date = State()
    ask_exact_end = State()
    exact_end_time = State()
    frequency = State()
    weekdays_pick = State()
    mode = State()
    fixed_times = State()
    periodic_range = State()
    periodic_interval = State()
    confirm = State()


class DeleteReminder(StatesGroup):
    pick = State()
    confirm = State()


# =========================
# УТИЛИТЫ
# =========================
def parse_date_ddmmyy(text: str):
    text = (text or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    try:
        return date(2000 + int(text[4:6]), int(text[2:4]), int(text[0:2]))
    except ValueError:
        return None


def parse_hhmm(text: str):
    text = (text or "").strip()
    if len(text) != 4 or not text.isdigit():
        return None
    hh = int(text[:2])
    mm = int(text[2:])
    if hh == 24 and mm == 0:
        return "24:00"
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None


def parse_times_space(text: str):
    parts = [p.strip() for p in (text or "").replace(",", " ").split() if p.strip()]
    if not parts:
        return None

    result = []
    seen = set()
    for part in parts:
        t = parse_hhmm(part)
        if not t or t == "24:00":
            return None
        if t not in seen:
            seen.add(t)
            result.append(t)

    result.sort()
    if len(result) > MAX_TIMES:
        return None
    return result


def parse_minutes_hhmm(value: str) -> int:
    if value == "24:00":
        return 1440
    hh, mm = map(int, value.split(":"))
    return hh * 60 + mm


def parse_range(text: str):
    parts = [p.strip() for p in (text or "").replace("-", " ").split() if p.strip()]
    if len(parts) != 2:
        return None

    start = parse_hhmm(parts[0])
    end = parse_hhmm(parts[1])
    if not start or not end:
        return None

    start_minutes = parse_minutes_hhmm(start)
    end_minutes = parse_minutes_hhmm(end)
    if end_minutes <= start_minutes:
        return None

    return start, end


def get_user_now(timezone_name: str) -> datetime:
    utc_now = datetime.now(ZoneInfo("UTC"))
    return utc_now.astimezone(ZoneInfo(timezone_name)).replace(second=0, microsecond=0)


def reminder_finish_dt(rem: dict, timezone_name: str) -> datetime:
    d = date.fromisoformat(rem["target_date"])
    tz = ZoneInfo(timezone_name)
    end_time_text = rem.get("exact_end_time")

    if end_time_text:
        if end_time_text == "24:00":
            return datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
        hh, mm = map(int, end_time_text.split(":"))
        return datetime.combine(d, time(hh, mm), tzinfo=tz)

    return datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)


def format_date_ru(iso_date: str):
    return date.fromisoformat(iso_date).strftime("%d.%m.%Y")


def format_dt_ru(dt: datetime):
    return dt.strftime("%d.%m.%Y в %H:%M")


def short_next_text(next_dt: datetime | None, timezone_name: str) -> str:
    if not next_dt:
        return "нет"

    now = get_user_now(timezone_name)
    next_date = next_dt.date()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    if next_date == today:
        return f"сегодня в {next_dt.strftime('%H:%M')}"
    if next_date == tomorrow:
        return f"завтра в {next_dt.strftime('%H:%M')}"

    diff_seconds = int((next_dt - now).total_seconds())
    if diff_seconds <= 0:
        return f"сегодня в {next_dt.strftime('%H:%M')}"

    days = diff_seconds // 86400
    diff_seconds %= 86400
    hours = diff_seconds // 3600
    diff_seconds %= 3600
    minutes = diff_seconds // 60

    if days > 0:
        return f"через {days} дн."
    if hours > 0 and minutes > 0:
        return f"через {hours} ч. {minutes} мин."
    if hours > 0:
        return f"через {hours} ч."
    return f"через {minutes} мин."


def reminder_text(rem: dict, timezone_name: str):
    user_now = get_user_now(timezone_name)
    finish_dt = reminder_finish_dt(rem, timezone_name)

    if rem.get("exact_end_time"):
        diff_seconds = int((finish_dt - user_now).total_seconds())
        if diff_seconds <= 0:
            return f'Сегодня {rem["title"]}'
        days = diff_seconds // 86400
        diff_seconds %= 86400
        hours = diff_seconds // 3600
        diff_seconds %= 3600
        minutes = diff_seconds // 60
        return f'До {rem["title"]} осталось {days} дн. {hours} ч. {minutes} мин.'

    diff_days = (finish_dt.date() - user_now.date()).days
    if diff_days <= 1:
        return f'Сегодня {rem["title"]}'
    return f'До {rem["title"]} осталось {diff_days - 1} дней'


def reminder_brief(rem: dict):
    if rem.get("reminder_kind") == "one_time":
        base = f'одноразовый · {format_date_ru(rem["remind_date"])}'
        if rem["mode_type"] == "fixed":
            return f'{base} · {rem["times_text"].replace(",", " ")}'
        return f'{base} · {rem["start_time"]}—{rem["end_time"]} · каждые {rem["interval_minutes"]} мин.'

    if rem["frequency_type"] == "daily":
        freq_text = "каждый день"
    else:
        days = [WEEKDAYS[int(x)] for x in rem["weekdays_text"].split(",") if x != ""]
        freq_text = ", ".join(days)

    if rem["mode_type"] == "fixed":
        return f'{freq_text} · {rem["times_text"].replace(",", " ")}'
    return f'{freq_text} · {rem["start_time"]}—{rem["end_time"]} · каждые {rem["interval_minutes"]} мин.'


def build_summary(data: dict):
    lines = ["Проверь заеб\n"]
    lines.append(f'{data["title"]}')
    lines.append(f'до {format_date_ru(data["target_date"])}')

    if data.get("reminder_kind") == "one_time":
        lines.append(f'день напоминания · {format_date_ru(data["remind_date"])}')

    if data.get("exact_end_time"):
        lines.append(f'точный конец · {data["exact_end_time"]}')

    if data["mode_type"] == "fixed":
        lines.append(data["times_text"].replace(",", " "))
    else:
        lines.append(f'{data["start_time"]}—{data["end_time"]} · каждые {data["interval_minutes"]} мин.')

    if data.get("reminder_kind") != "one_time":
        if data["frequency_type"] == "daily":
            lines.append("каждый день")
        else:
            days = [WEEKDAYS[int(x)] for x in data["weekdays_text"].split(",") if x != ""]
            lines.append(", ".join(days))

    lines.append("\nСохраняем?")
    return "\n".join(lines)


def reminder_matches_day(rem: dict, day: date) -> bool:
    if rem.get("reminder_kind") == "one_time":
        return rem.get("remind_date") == day.isoformat()

    if day > date.fromisoformat(rem["target_date"]):
        return False

    if rem["frequency_type"] == "daily":
        return True

    if rem["frequency_type"] == "weekdays":
        days = [int(x) for x in rem["weekdays_text"].split(",")] if rem.get("weekdays_text") else []
        return day.weekday() in days

    return False


def iter_day_candidates(rem: dict, day: date, timezone_name: str):
    tz = ZoneInfo(timezone_name)
    finish_dt = reminder_finish_dt(rem, timezone_name)

    if not reminder_matches_day(rem, day):
        return []

    candidates = []

    if rem["mode_type"] == "fixed":
        times = rem["times_text"].split(",") if rem.get("times_text") else []
        for t in times:
            hh, mm = map(int, t.split(":"))
            dt = datetime.combine(day, time(hh, mm), tzinfo=tz)
            if dt < finish_dt:
                candidates.append(dt)

    elif rem["mode_type"] == "periodic":
        if rem.get("start_time") and rem.get("end_time") and rem.get("interval_minutes"):
            start_minutes = parse_minutes_hhmm(rem["start_time"])
            end_minutes = parse_minutes_hhmm(rem["end_time"])
            interval = int(rem["interval_minutes"])
            minute = start_minutes
            while minute < end_minutes:
                hh = minute // 60
                mm = minute % 60
                dt = datetime.combine(day, time(hh, mm), tzinfo=tz)
                if dt < finish_dt:
                    candidates.append(dt)
                minute += interval

    return candidates


def next_notification_dt(rem: dict, timezone_name: str) -> datetime | None:
    now = get_user_now(timezone_name)
    finish_dt = reminder_finish_dt(rem, timezone_name)

    if now >= finish_dt:
        return None

    start_day = now.date()
    end_day = date.fromisoformat(rem["target_date"])

    if rem.get("reminder_kind") == "one_time":
        remind_day = date.fromisoformat(rem["remind_date"])
        if remind_day < start_day or remind_day > end_day:
            return None
        for dt in iter_day_candidates(rem, remind_day, timezone_name):
            if dt >= now and dt < finish_dt:
                return dt
        return None

    day = start_day
    while day <= end_day:
        for dt in iter_day_candidates(rem, day, timezone_name):
            if dt >= now and dt < finish_dt:
                return dt
        day += timedelta(days=1)

    return None


def list_numbered_reminders(reminders: list[dict], timezone_name: str):
    sorted_reminders = sorted(
        reminders,
        key=lambda rem: next_notification_dt(rem, timezone_name) or datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    )

    parts = []
    for i, rem in enumerate(sorted_reminders, start=1):
        next_dt = next_notification_dt(rem, timezone_name)
        parts.append(
            f'{i}. {rem["title"]}\n'
            f'до {format_date_ru(rem["target_date"])}\n'
            f'{reminder_brief(rem)}\n'
            f'следующее: {short_next_text(next_dt, timezone_name)}'
        )
    return "\n\n".join(parts)


# =========================
# АНТИЖМЯК
# =========================
async def reset_misclick(state: FSMContext):
    await state.update_data(_misclick_key=None, _misclick_count=0)


async def misclick_reply(message: Message, state: FSMContext, key: str, reply_markup=None):
    await state.update_data(_misclick_key=key)
    await message.answer(
        "Чел, используй кнопки пожалуйста, Денис очень старался делая их.",
        reply_markup=reply_markup,
    )


# =========================
# КНОПКИ
# =========================
def main_menu_inline():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Сделать новый заеб", callback_data="menu:create_regular"))
    builder.row(InlineKeyboardButton(text="⚡ Одноразовый заеб", callback_data="menu:create_one_time"))
    builder.row(InlineKeyboardButton(text="📋 Показать заебы", callback_data="menu:show"))
    builder.row(InlineKeyboardButton(text="🗑 Заебал", callback_data="menu:delete"))
    return builder.as_markup()


def country_inline():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Украина", callback_data="country:ukraine"))
    builder.row(InlineKeyboardButton(text="Чехия", callback_data="country:czechia"))
    return builder.as_markup()


def back_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True,
    )


def done_back_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Готово")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def yes_no_back_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да")],
            [KeyboardButton(text="Нет")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def save_edit_back_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сохранить")],
            [KeyboardButton(text="Изменить")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def freq_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Каждый день")],
            [KeyboardButton(text="В определенные дни недели")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def mode_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="В конкретное время дня")],
            [KeyboardButton(text="В промежутке")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def periodic_interval_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Каждую 1 минуту")],
            [KeyboardButton(text="Каждые 5 минут")],
            [KeyboardButton(text="Каждые 30 минут")],
            [KeyboardButton(text="Каждые 60 минут")],
            [KeyboardButton(text="Свой интервал")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def select_reminder_kb(reminders: list[dict], timezone_name: str, with_delete_all: bool = False):
    sorted_reminders = sorted(
        reminders,
        key=lambda rem: next_notification_dt(rem, timezone_name) or datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    )
    rows = []
    for i, rem in enumerate(sorted_reminders, start=1):
        rows.append([KeyboardButton(text=f'{i}. {rem["title"]}')])
    if with_delete_all:
        rows.append([KeyboardButton(text="Заебал, удалить всё")])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def edit_field_kb(reminder_kind: str, freq_type: str | None = None):
    if reminder_kind == "one_time":
        rows = [
            [KeyboardButton(text="Повод")],
            [KeyboardButton(text="Дату")],
            [KeyboardButton(text="День напоминания")],
            [KeyboardButton(text="Точное время конца")],
            [KeyboardButton(text="Время")],
            [KeyboardButton(text="Назад")],
        ]
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    rows = [
        [KeyboardButton(text="Повод")],
        [KeyboardButton(text="Дату")],
        [KeyboardButton(text="Точное время конца")],
        [KeyboardButton(text="Тип заеба")],
        [KeyboardButton(text="Время")],
    ]
    if freq_type == "weekdays":
        rows.insert(4, [KeyboardButton(text="Дни недели")])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def weekdays_inline(selected: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(7):
        title = WEEKDAYS[i]
        if i in selected:
            title = f"✅ {title}"
        builder.row(InlineKeyboardButton(text=title, callback_data=f"weekday_toggle:{i}"))
    return builder.as_markup()


def delete_confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, заебал")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


# =========================
# ИСТОРИЯ / РЕНДЕР
# =========================
async def push_history(state: FSMContext, current_state_name: str):
    data = await state.get_data()
    history = data.get("_history", [])
    history.append(current_state_name)
    await state.update_data(_history=history)


async def pop_history(state: FSMContext):
    data = await state.get_data()
    history = data.get("_history", [])
    if not history:
        return None
    prev = history.pop()
    await state.update_data(_history=history)
    return prev


async def clear_weekdays_picker(state: FSMContext, chat_id: int):
    data = await state.get_data()
    msg_id = data.get("_weekdays_msg_id")
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await state.update_data(_weekdays_msg_id=None)


async def send_menu_block(message: Message):
    await message.answer("ㅤ")
    await message.answer("—", reply_markup=main_menu_inline())


async def send_saved_card(message: Message, rem: dict, timezone_name: str):
    next_dt = next_notification_dt(rem, timezone_name)
    text = (
        "Сохранил.\n\n"
        f'{rem["title"]}\n'
        f'до {format_date_ru(rem["target_date"])}\n'
        f'{reminder_brief(rem)}\n'
        f'следующее: {short_next_text(next_dt, timezone_name)}'
    )
    await message.answer(text)
    await send_menu_block(message)


async def render_country_step(message: Message):
    await message.answer(
        "Где ты живешь.\n\n"
        "Это нужно для определения твоего часового пояса.",
        reply_markup=country_inline(),
    )


async def render_weekdays_step(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("weekdays_selected", [])
    selected_text = ", ".join(WEEKDAYS[x] for x in sorted(selected)) if selected else "пока ничего"

    text = (
        "Выбери дни, в которые тебя заебывать.\n\n"
        f"Выбрано: {selected_text}"
    )

    await clear_weekdays_picker(state, message.chat.id)
    sent = await message.answer(text, reply_markup=weekdays_inline(set(selected)))
    await state.update_data(_weekdays_msg_id=sent.message_id)
    await message.answer("Когда закончишь — жми кнопку снизу.", reply_markup=done_back_kb())


async def render_step(message: Message, state: FSMContext, state_name: str):
    data = await state.get_data()

    if state_name not in {
        CreateReminder.weekdays_pick.state,
        EditReminder.weekdays_pick.state,
    }:
        await clear_weekdays_picker(state, message.chat.id)

    if state_name == CreateReminder.title.state:
        await message.answer("Введи повод заеба.", reply_markup=back_kb())
        return

    if state_name == CreateReminder.target_date.state:
        await message.answer(
            "До какой даты тебя заебывать?\n\n"
            "Пиши в таком формате: 020726\n\n"
            "Это значит:\n"
            "02.07.2026",
            reply_markup=back_kb(),
        )
        return

    if state_name == CreateReminder.ask_exact_end.state:
        await message.answer(
            "Хочешь настроить точное время конца?",
            reply_markup=yes_no_back_kb(),
        )
        return

    if state_name == CreateReminder.exact_end_time.state:
        await message.answer(
            "Введи точное время конца.\n\n"
            "Пиши в таком формате: 1400\n\n"
            "Это значит:\n"
            "14:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == CreateReminder.frequency.state:
        await message.answer("Как часто заебывать?", reply_markup=freq_kb())
        return

    if state_name == CreateReminder.weekdays_pick.state:
        await render_weekdays_step(message, state)
        return

    if state_name == CreateReminder.mode.state:
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
        return

    if state_name == CreateReminder.fixed_times.state:
        await message.answer(
            "Введи время.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n"
            "14:00\n"
            "или 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    if state_name == CreateReminder.periodic_range.state:
        await message.answer(
            "Введи промежуток.\n\n"
            "Пиши в таком формате: 2300 2400\n\n"
            "Это значит:\n"
            "с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == CreateReminder.periodic_interval.state:
        await message.answer("Как часто заебывать в этом промежутке?", reply_markup=periodic_interval_kb())
        return

    if state_name == CreateReminder.confirm.state:
        await message.answer(build_summary(data), reply_markup=save_edit_back_kb())
        return

    if state_name == CreateReminder.edit_field.state:
        await message.answer(
            "Что будем менять?",
            reply_markup=edit_field_kb("regular", data.get("frequency_type")),
        )
        return

    if state_name == OneTimeReminder.title.state:
        await message.answer("Введи повод заеба.", reply_markup=back_kb())
        return

    if state_name == OneTimeReminder.target_date.state:
        await message.answer(
            "До какой даты тебя заебывать?\n\n"
            "Пиши в таком формате: 020726\n\n"
            "Это значит:\n"
            "02.07.2026",
            reply_markup=back_kb(),
        )
        return

    if state_name == OneTimeReminder.remind_date.state:
        await message.answer(
            "В какой день напомнить?\n\n"
            "Пиши в таком формате: 020726\n\n"
            "Это значит:\n"
            "02.07.2026",
            reply_markup=back_kb(),
        )
        return

    if state_name == OneTimeReminder.ask_exact_end.state:
        await message.answer(
            "Хочешь настроить точное время конца?",
            reply_markup=yes_no_back_kb(),
        )
        return

    if state_name == OneTimeReminder.exact_end_time.state:
        await message.answer(
            "Введи точное время конца.\n\n"
            "Пиши в таком формате: 1400\n\n"
            "Это значит:\n"
            "14:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == OneTimeReminder.mode.state:
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
        return

    if state_name == OneTimeReminder.fixed_times.state:
        await message.answer(
            "Введи время.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n"
            "14:00\n"
            "или 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    if state_name == OneTimeReminder.periodic_range.state:
        await message.answer(
            "Введи промежуток.\n\n"
            "Пиши в таком формате: 2300 2400\n\n"
            "Это значит:\n"
            "с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == OneTimeReminder.periodic_interval.state:
        await message.answer("Как часто заебывать в этом промежутке?", reply_markup=periodic_interval_kb())
        return

    if state_name == OneTimeReminder.confirm.state:
        await message.answer(build_summary(data), reply_markup=save_edit_back_kb())
        return

    if state_name == OneTimeReminder.edit_field.state:
        await message.answer(
            "Что будем менять?",
            reply_markup=edit_field_kb("one_time"),
        )
        return

    if state_name == EditReminder.pick.state:
        reminders = data.get("pick_list", [])
        timezone_name = data["user_timezone"]
        await message.answer("Выбери, какой заеб изменить.", reply_markup=select_reminder_kb(reminders, timezone_name))
        return

    if state_name == EditReminder.field.state:
        await message.answer(
            "Что будем менять?",
            reply_markup=edit_field_kb(data.get("reminder_kind", "regular"), data.get("frequency_type")),
        )
        return

    if state_name == EditReminder.title.state:
        await message.answer("Введи новый повод.", reply_markup=back_kb())
        return

    if state_name == EditReminder.target_date.state:
        await message.answer(
            "Введи новую дату.\n\n"
            "Пиши в таком формате: 020726\n\n"
            "Это значит:\n"
            "02.07.2026",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.remind_date.state:
        await message.answer(
            "Введи новый день напоминания.\n\n"
            "Пиши в таком формате: 020726\n\n"
            "Это значит:\n"
            "02.07.2026",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.ask_exact_end.state:
        await message.answer(
            "Хочешь настроить точное время конца?",
            reply_markup=yes_no_back_kb(),
        )
        return

    if state_name == EditReminder.exact_end_time.state:
        await message.answer(
            "Введи точное время конца.\n\n"
            "Пиши в таком формате: 1400\n\n"
            "Это значит:\n"
            "14:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.frequency.state:
        await message.answer("Как часто заебывать?", reply_markup=freq_kb())
        return

    if state_name == EditReminder.weekdays_pick.state:
        await render_weekdays_step(message, state)
        return

    if state_name == EditReminder.mode.state:
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
        return

    if state_name == EditReminder.fixed_times.state:
        await message.answer(
            "Введи время.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n"
            "14:00\n"
            "или 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.periodic_range.state:
        await message.answer(
            "Введи промежуток.\n\n"
            "Пиши в таком формате: 2300 2400\n\n"
            "Это значит:\n"
            "с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.periodic_interval.state:
        await message.answer("Как часто заебывать в этом промежутке?", reply_markup=periodic_interval_kb())
        return

    if state_name == EditReminder.confirm.state:
        await message.answer(build_summary(data), reply_markup=save_edit_back_kb())
        return

    if state_name == DeleteReminder.pick.state:
        reminders = data.get("pick_list", [])
        timezone_name = data["user_timezone"]
        await message.answer("Выбери, какой заеб удалить.", reply_markup=select_reminder_kb(reminders, timezone_name, with_delete_all=True))
        return

    if state_name == DeleteReminder.confirm.state:
        if data.get("delete_all"):
            await message.answer("Точно удалить вообще всё?", reply_markup=delete_confirm_kb())
            return

        rem = data["delete_reminder"]
        await message.answer(
            "Точно удалить этот заеб?\n\n"
            f'{rem["title"]}\n'
            f'до {format_date_ru(rem["target_date"])}\n'
            f'{reminder_brief(rem)}',
            reply_markup=delete_confirm_kb(),
        )
        return


# =========================
# БОТ
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =========================
# СТАРТ / СТРАНА / МЕНЮ
# =========================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
    user = get_user(message.from_user.id)

    if not user or not user.get("seen_intro"):
        await message.answer(
            "Привет.\n\n"
            "Обычный — напоминает до даты\n"
            "Одноразовый — в конкретный день"
        )
        mark_seen_intro(message.from_user.id)

    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    await send_menu_block(message)


@dp.callback_query(F.data == "country:ukraine")
async def country_ukraine(callback: CallbackQuery, state: FSMContext):
    ensure_user(callback.from_user.id)
    set_user_country_timezone(callback.from_user.id, "Украина", "Europe/Kyiv")
    await state.clear()
    await callback.answer()
    await callback.message.answer("Красавчик.")
    await send_menu_block(callback.message)


@dp.callback_query(F.data == "country:czechia")
async def country_czech(callback: CallbackQuery, state: FSMContext):
    ensure_user(callback.from_user.id)
    set_user_country_timezone(callback.from_user.id, "Чехия", "Europe/Prague")
    await state.clear()
    await callback.answer()
    await callback.message.answer("Красавчик.")
    await send_menu_block(callback.message)


@dp.callback_query(F.data == "menu:create_regular")
async def menu_create_regular(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = get_user(callback.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await callback.message.answer("Сначала выбери страну.", reply_markup=country_inline())
        return

    if get_active_count(callback.from_user.id) >= MAX_REMINDERS:
        await callback.message.answer(f"У тебя уже {MAX_REMINDERS} заебов.\nСначала удали один.")
        await send_menu_block(callback.message)
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(
        _history=[],
        _weekdays_msg_id=None,
        _create_edit_mode=False,
        _create_edit_target=None,
        reminder_kind="regular",
        remind_date=None,
        user_timezone=user["timezone"],
    )
    await state.set_state(CreateReminder.title)
    await render_step(callback.message, state, CreateReminder.title.state)


@dp.callback_query(F.data == "menu:create_one_time")
async def menu_create_one_time(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = get_user(callback.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await callback.message.answer("Сначала выбери страну.", reply_markup=country_inline())
        return

    if get_active_count(callback.from_user.id) >= MAX_REMINDERS:
        await callback.message.answer(f"У тебя уже {MAX_REMINDERS} заебов.\nСначала удали один.")
        await send_menu_block(callback.message)
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(
        _history=[],
        _weekdays_msg_id=None,
        _create_edit_mode=False,
        _create_edit_target=None,
        reminder_kind="one_time",
        frequency_type="one_time",
        weekdays_text=None,
        weekdays_selected=[],
        user_timezone=user["timezone"],
    )
    await state.set_state(OneTimeReminder.title)
    await render_step(callback.message, state, OneTimeReminder.title.state)


@dp.callback_query(F.data == "menu:show")
async def menu_show(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await reset_misclick(state)

    user = get_user(callback.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(callback.message)
        return

    reminders = get_user_reminders(callback.from_user.id)
    if not reminders:
        await callback.message.answer("Пока ни одного заеба нет.")
        await send_menu_block(callback.message)
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Изменить заеб")],
            [KeyboardButton(text="Меню")],
        ],
        resize_keyboard=True,
    )
    await callback.message.answer(list_numbered_reminders(reminders, user["timezone"]), reply_markup=kb)


@dp.callback_query(F.data == "menu:delete")
async def menu_delete(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reminders = get_user_reminders(callback.from_user.id)
    user = get_user(callback.from_user.id)

    if not reminders:
        await state.clear()
        await callback.message.answer("Пока ни одного заеба нет.")
        await send_menu_block(callback.message)
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(_history=[], pick_list=reminders, user_timezone=user["timezone"])
    await state.set_state(DeleteReminder.pick)
    await render_step(callback.message, state, DeleteReminder.pick.state)


# =========================
# ИНЛАЙН ДНИ
# =========================
@dp.callback_query(F.data.startswith("weekday_toggle:"))
async def weekday_toggle(callback: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current not in {CreateReminder.weekdays_pick.state, EditReminder.weekdays_pick.state}:
        await callback.answer()
        return

    day_num = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected = set(data.get("weekdays_selected", []))

    if day_num in selected:
        selected.remove(day_num)
    else:
        selected.add(day_num)

    await state.update_data(weekdays_selected=sorted(selected))

    text = "Выбери дни, в которые тебя заебывать.\n\n"
    text += "Выбрано: " + (", ".join(WEEKDAYS[x] for x in sorted(selected)) if selected else "пока ничего")

    await callback.message.edit_text(text, reply_markup=weekdays_inline(set(selected)))
    await callback.answer()


# =========================
# НАЗАД
# =========================
@dp.message(F.text == "Назад")
async def back_handler(message: Message, state: FSMContext):
    current = await state.get_state()
    if not current:
        await send_menu_block(message)
        return

    prev = await pop_history(state)
    if not prev:
        await clear_weekdays_picker(state, message.chat.id)
        await state.clear()
        await send_menu_block(message)
        return

    state_map = {
        CountrySetup.choose.state: CountrySetup.choose,

        CreateReminder.title.state: CreateReminder.title,
        CreateReminder.target_date.state: CreateReminder.target_date,
        CreateReminder.ask_exact_end.state: CreateReminder.ask_exact_end,
        CreateReminder.exact_end_time.state: CreateReminder.exact_end_time,
        CreateReminder.frequency.state: CreateReminder.frequency,
        CreateReminder.weekdays_pick.state: CreateReminder.weekdays_pick,
        CreateReminder.mode.state: CreateReminder.mode,
        CreateReminder.fixed_times.state: CreateReminder.fixed_times,
        CreateReminder.periodic_range.state: CreateReminder.periodic_range,
        CreateReminder.periodic_interval.state: CreateReminder.periodic_interval,
        CreateReminder.confirm.state: CreateReminder.confirm,
        CreateReminder.edit_field.state: CreateReminder.edit_field,

        OneTimeReminder.title.state: OneTimeReminder.title,
        OneTimeReminder.target_date.state: OneTimeReminder.target_date,
        OneTimeReminder.remind_date.state: OneTimeReminder.remind_date,
        OneTimeReminder.ask_exact_end.state: OneTimeReminder.ask_exact_end,
        OneTimeReminder.exact_end_time.state: OneTimeReminder.exact_end_time,
        OneTimeReminder.mode.state: OneTimeReminder.mode,
        OneTimeReminder.fixed_times.state: OneTimeReminder.fixed_times,
        OneTimeReminder.periodic_range.state: OneTimeReminder.periodic_range,
        OneTimeReminder.periodic_interval.state: OneTimeReminder.periodic_interval,
        OneTimeReminder.confirm.state: OneTimeReminder.confirm,
        OneTimeReminder.edit_field.state: OneTimeReminder.edit_field,

        EditReminder.pick.state: EditReminder.pick,
        EditReminder.field.state: EditReminder.field,
        EditReminder.title.state: EditReminder.title,
        EditReminder.target_date.state: EditReminder.target_date,
        EditReminder.remind_date.state: EditReminder.remind_date,
        EditReminder.ask_exact_end.state: EditReminder.ask_exact_end,
        EditReminder.exact_end_time.state: EditReminder.exact_end_time,
        EditReminder.frequency.state: EditReminder.frequency,
        EditReminder.weekdays_pick.state: EditReminder.weekdays_pick,
        EditReminder.mode.state: EditReminder.mode,
        EditReminder.fixed_times.state: EditReminder.fixed_times,
        EditReminder.periodic_range.state: EditReminder.periodic_range,
        EditReminder.periodic_interval.state: EditReminder.periodic_interval,
        EditReminder.confirm.state: EditReminder.confirm,

        DeleteReminder.pick.state: DeleteReminder.pick,
        DeleteReminder.confirm.state: DeleteReminder.confirm,
    }

    await state.set_state(state_map[prev])
    await render_step(message, state, prev)


# =========================
# СОЗДАНИЕ ОБЫЧНОГО ЗАЕБА
# =========================
@dp.message(CreateReminder.title)
async def create_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Хуйня. Введи повод нормально.", reply_markup=back_kb())
        return
    if len(title) > MAX_TITLE_LEN:
        await message.answer(f"Слишком длинно. Максимум {MAX_TITLE_LEN} символа.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(title=title)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "title":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.title.state)
        await state.set_state(CreateReminder.confirm)
        await render_step(message, state, CreateReminder.confirm.state)
        return

    await push_history(state, CreateReminder.title.state)
    await state.set_state(CreateReminder.target_date)
    await render_step(message, state, CreateReminder.target_date.state)


@dp.message(CreateReminder.target_date)
async def create_target_date(message: Message, state: FSMContext):
    data = await state.get_data()
    user_now = get_user_now(data["user_timezone"])

    d = parse_date_ddmmyy(message.text or "")
    if not d:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 020726\nЭто значит: 02.07.2026", reply_markup=back_kb())
        return
    if d < user_now.date():
        await message.answer("Дурак совсем? Это ведь прошлое.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(target_date=d.isoformat())

    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "target_date":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.target_date.state)
        await state.set_state(CreateReminder.confirm)
        await render_step(message, state, CreateReminder.confirm.state)
        return

    await push_history(state, CreateReminder.target_date.state)
    await state.set_state(CreateReminder.ask_exact_end)
    await render_step(message, state, CreateReminder.ask_exact_end.state)


@dp.message(CreateReminder.ask_exact_end, F.text == "Да")
async def create_exact_end_yes(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, CreateReminder.ask_exact_end.state)
    await state.set_state(CreateReminder.exact_end_time)
    await render_step(message, state, CreateReminder.exact_end_time.state)


@dp.message(CreateReminder.ask_exact_end, F.text == "Нет")
async def create_exact_end_no(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(exact_end_time=None)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "exact_end_time":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.ask_exact_end.state)
        await state.set_state(CreateReminder.confirm)
        await render_step(message, state, CreateReminder.confirm.state)
        return

    await push_history(state, CreateReminder.ask_exact_end.state)
    await state.set_state(CreateReminder.frequency)
    await render_step(message, state, CreateReminder.frequency.state)


@dp.message(CreateReminder.exact_end_time)
async def create_exact_end_time(message: Message, state: FSMContext):
    parsed = parse_hhmm(message.text or "")
    if not parsed:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 1400\nЭто значит: 14:00", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(exact_end_time=parsed)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "exact_end_time":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.exact_end_time.state)
        await state.set_state(CreateReminder.confirm)
        await render_step(message, state, CreateReminder.confirm.state)
        return

    await push_history(state, CreateReminder.exact_end_time.state)
    await state.set_state(CreateReminder.frequency)
    await render_step(message, state, CreateReminder.frequency.state)


@dp.message(CreateReminder.frequency, F.text == "Каждый день")
async def create_freq_daily(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(
        frequency_type="daily",
        weekdays_text=None,
        weekdays_selected=[],
    )

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") in {"frequency_type", "weekdays"}:
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.frequency.state)
        await state.set_state(CreateReminder.mode)
        await render_step(message, state, CreateReminder.mode.state)
        return

    await push_history(state, CreateReminder.frequency.state)
    await state.set_state(CreateReminder.mode)
    await render_step(message, state, CreateReminder.mode.state)


@dp.message(CreateReminder.frequency, F.text == "В определенные дни недели")
async def create_freq_weekdays(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(
        frequency_type="weekdays",
        weekdays_selected=[],
        weekdays_text=None,
    )
    await push_history(state, CreateReminder.frequency.state)
    await state.set_state(CreateReminder.weekdays_pick)
    await render_step(message, state, CreateReminder.weekdays_pick.state)


@dp.message(CreateReminder.weekdays_pick, F.text == "Готово")
async def create_weekdays_done(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = sorted(set(data.get("weekdays_selected", [])))
    if not selected:
        await message.answer("Сначала выбери хотя бы один день.", reply_markup=done_back_kb())
        return

    await reset_misclick(state)
    await state.update_data(weekdays_text=",".join(map(str, selected)))

    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "weekdays":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, CreateReminder.weekdays_pick.state)
        await state.set_state(CreateReminder.confirm)
        await render_step(message, state, CreateReminder.confirm.state)
        return

    await push_history(state, CreateReminder.weekdays_pick.state)
    await state.set_state(CreateReminder.mode)
    await render_step(message, state, CreateReminder.mode.state)


@dp.message(CreateReminder.weekdays_pick)
async def create_weekdays_buttons_only(message: Message, state: FSMContext):
    await misclick_reply(message, state, "create_weekdays", done_back_kb())


@dp.message(CreateReminder.mode, F.text == "В конкретное время дня")
async def create_mode_fixed(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="fixed")

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "time":
        await push_history(state, CreateReminder.mode.state)
        await state.set_state(CreateReminder.fixed_times)
        await render_step(message, state, CreateReminder.fixed_times.state)
        return

    await push_history(state, CreateReminder.mode.state)
    await state.set_state(CreateReminder.fixed_times)
    await render_step(message, state, CreateReminder.fixed_times.state)


@dp.message(CreateReminder.mode, F.text == "В промежутке")
async def create_mode_periodic(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="periodic")

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "time":
        await push_history(state, CreateReminder.mode.state)
        await state.set_state(CreateReminder.periodic_range)
        await render_step(message, state, CreateReminder.periodic_range.state)
        return

    await push_history(state, CreateReminder.mode.state)
    await state.set_state(CreateReminder.periodic_range)
    await render_step(message, state, CreateReminder.periodic_range.state)


@dp.message(CreateReminder.fixed_times)
async def create_fixed_times(message: Message, state: FSMContext):
    times = parse_times_space(message.text or "")
    if not times:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит: 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    await state.update_data(
        times_text=",".join(times),
        start_time=None,
        end_time=None,
        interval_minutes=None,
    )
    await push_history(state, CreateReminder.fixed_times.state)
    await state.set_state(CreateReminder.confirm)
    await render_step(message, state, CreateReminder.confirm.state)


@dp.message(CreateReminder.periodic_range)
async def create_periodic_range(message: Message, state: FSMContext):
    result = parse_range(message.text or "")
    if not result:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 2300 2400\n"
            "Это значит: с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    start_time, end_time = result
    await state.update_data(start_time=start_time, end_time=end_time, times_text=None)
    await push_history(state, CreateReminder.periodic_range.state)
    await state.set_state(CreateReminder.periodic_interval)
    await render_step(message, state, CreateReminder.periodic_interval.state)


@dp.message(CreateReminder.periodic_interval, F.text == "Каждую 1 минуту")
async def create_int_1(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_create_periodic(message, state, 1)


@dp.message(CreateReminder.periodic_interval, F.text == "Каждые 5 минут")
async def create_int_5(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_create_periodic(message, state, 5)


@dp.message(CreateReminder.periodic_interval, F.text == "Каждые 30 минут")
async def create_int_30(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_create_periodic(message, state, 30)


@dp.message(CreateReminder.periodic_interval, F.text == "Каждые 60 минут")
async def create_int_60(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_create_periodic(message, state, 60)


@dp.message(CreateReminder.periodic_interval, F.text == "Свой интервал")
async def create_custom_interval_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await message.answer("Введи интервал в минутах.\nМинимум 1.", reply_markup=back_kb())


@dp.message(CreateReminder.periodic_interval)
async def create_custom_interval(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Хуйня. Интервал должен быть числом.", reply_markup=back_kb())
        return

    n = int(text)
    if n < MIN_INTERVAL:
        await message.answer("Хуйня. Минимальный интервал — 1 минута.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await finish_create_periodic(message, state, n)


async def finish_create_periodic(message: Message, state: FSMContext, interval: int):
    await state.update_data(interval_minutes=interval)
    await push_history(state, CreateReminder.periodic_interval.state)
    await state.set_state(CreateReminder.confirm)
    await render_step(message, state, CreateReminder.confirm.state)


@dp.message(CreateReminder.confirm, F.text == "Сохранить")
async def create_save(message: Message, state: FSMContext):
    data = await state.get_data()
    create_reminder_db(
        user_id=message.from_user.id,
        title=data["title"],
        target_date=data["target_date"],
        exact_end_time=data.get("exact_end_time"),
        frequency_type=data["frequency_type"],
        mode_type=data["mode_type"],
        times_text=data.get("times_text"),
        weekdays_text=data.get("weekdays_text"),
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        interval_minutes=data.get("interval_minutes"),
        reminder_kind="regular",
        remind_date=None,
    )

    reminders = get_user_reminders(message.from_user.id)
    rem = reminders[-1]
    timezone_name = data["user_timezone"]

    await state.clear()
    await send_saved_card(message, rem, timezone_name)

    user = get_user(message.from_user.id)
    if user and not user.get("notifications_hint_shown"):
        mark_notifications_hint_shown(message.from_user.id)
        await message.answer("Не забудь включить уведомления,\nиначе я не смогу нормально заебывать.")


@dp.message(CreateReminder.confirm, F.text == "Изменить")
async def create_edit_from_confirm(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, CreateReminder.confirm.state)
    await state.set_state(CreateReminder.edit_field)
    await render_step(message, state, CreateReminder.edit_field.state)


@dp.message(CreateReminder.edit_field, F.text == "Повод")
async def create_edit_title(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="title")
    await push_history(state, CreateReminder.edit_field.state)
    await state.set_state(CreateReminder.title)
    await message.answer("Введи новый повод.", reply_markup=back_kb())


@dp.message(CreateReminder.edit_field, F.text == "Дату")
async def create_edit_date(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="target_date")
    await push_history(state, CreateReminder.edit_field.state)
    await state.set_state(CreateReminder.target_date)
    await render_step(message, state, CreateReminder.target_date.state)


@dp.message(CreateReminder.edit_field, F.text == "Точное время конца")
async def create_edit_exact_end(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="exact_end_time")
    await push_history(state, CreateReminder.edit_field.state)
    await state.set_state(CreateReminder.ask_exact_end)
    await render_step(message, state, CreateReminder.ask_exact_end.state)


@dp.message(CreateReminder.edit_field, F.text == "Тип заеба")
async def create_edit_type(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="frequency_type")
    await push_history(state, CreateReminder.edit_field.state)
    await state.set_state(CreateReminder.frequency)
    await render_step(message, state, CreateReminder.frequency.state)


@dp.message(CreateReminder.edit_field, F.text == "Дни недели")
async def create_edit_days(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("frequency_type") != "weekdays":
        await message.answer("У этого заеба нет дней недели.", reply_markup=edit_field_kb("regular", data["frequency_type"]))
        return

    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="weekdays")
    await push_history(state, CreateReminder.edit_field.state)
    await state.set_state(CreateReminder.weekdays_pick)
    await render_step(message, state, CreateReminder.weekdays_pick.state)


@dp.message(CreateReminder.edit_field, F.text == "Время")
async def create_edit_time(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="time")
    await push_history(state, CreateReminder.edit_field.state)

    data = await state.get_data()
    if data["mode_type"] == "fixed":
        await state.set_state(CreateReminder.fixed_times)
        await render_step(message, state, CreateReminder.fixed_times.state)
    else:
        await state.set_state(CreateReminder.periodic_range)
        await render_step(message, state, CreateReminder.periodic_range.state)


# =========================
# СОЗДАНИЕ ОДНОРАЗОВОГО ЗАЕБА
# =========================
@dp.message(OneTimeReminder.title)
async def one_time_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Хуйня. Введи повод нормально.", reply_markup=back_kb())
        return
    if len(title) > MAX_TITLE_LEN:
        await message.answer(f"Слишком длинно. Максимум {MAX_TITLE_LEN} символа.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(title=title)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "title":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, OneTimeReminder.title.state)
        await state.set_state(OneTimeReminder.confirm)
        await render_step(message, state, OneTimeReminder.confirm.state)
        return

    await push_history(state, OneTimeReminder.title.state)
    await state.set_state(OneTimeReminder.target_date)
    await render_step(message, state, OneTimeReminder.target_date.state)


@dp.message(OneTimeReminder.target_date)
async def one_time_target_date(message: Message, state: FSMContext):
    data = await state.get_data()
    user_now = get_user_now(data["user_timezone"])

    d = parse_date_ddmmyy(message.text or "")
    if not d:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 020726\nЭто значит: 02.07.2026", reply_markup=back_kb())
        return
    if d < user_now.date():
        await message.answer("Дурак совсем? Это ведь прошлое.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(target_date=d.isoformat())

    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "target_date":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, OneTimeReminder.target_date.state)
        await state.set_state(OneTimeReminder.confirm)
        await render_step(message, state, OneTimeReminder.confirm.state)
        return

    await push_history(state, OneTimeReminder.target_date.state)
    await state.set_state(OneTimeReminder.remind_date)
    await render_step(message, state, OneTimeReminder.remind_date.state)


@dp.message(OneTimeReminder.remind_date)
async def one_time_remind_date(message: Message, state: FSMContext):
    data = await state.get_data()
    user_now = get_user_now(data["user_timezone"])

    d = parse_date_ddmmyy(message.text or "")
    if not d:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 020726\nЭто значит: 02.07.2026", reply_markup=back_kb())
        return
    if d < user_now.date():
        await message.answer("Дурак совсем? Это ведь прошлое.", reply_markup=back_kb())
        return

    target_date = date.fromisoformat(data["target_date"])
    if d > target_date:
        await message.answer("Хуйня. День напоминания не может быть позже конечной даты.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(remind_date=d.isoformat())

    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "remind_date":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, OneTimeReminder.remind_date.state)
        await state.set_state(OneTimeReminder.confirm)
        await render_step(message, state, OneTimeReminder.confirm.state)
        return

    await push_history(state, OneTimeReminder.remind_date.state)
    await state.set_state(OneTimeReminder.ask_exact_end)
    await render_step(message, state, OneTimeReminder.ask_exact_end.state)


@dp.message(OneTimeReminder.ask_exact_end, F.text == "Да")
async def one_time_exact_end_yes(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, OneTimeReminder.ask_exact_end.state)
    await state.set_state(OneTimeReminder.exact_end_time)
    await render_step(message, state, OneTimeReminder.exact_end_time.state)


@dp.message(OneTimeReminder.ask_exact_end, F.text == "Нет")
async def one_time_exact_end_no(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(exact_end_time=None)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "exact_end_time":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, OneTimeReminder.ask_exact_end.state)
        await state.set_state(OneTimeReminder.confirm)
        await render_step(message, state, OneTimeReminder.confirm.state)
        return

    await push_history(state, OneTimeReminder.ask_exact_end.state)
    await state.set_state(OneTimeReminder.mode)
    await render_step(message, state, OneTimeReminder.mode.state)


@dp.message(OneTimeReminder.exact_end_time)
async def one_time_exact_end_time(message: Message, state: FSMContext):
    parsed = parse_hhmm(message.text or "")
    if not parsed:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 1400\nЭто значит: 14:00", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(exact_end_time=parsed)

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "exact_end_time":
        await state.update_data(_create_edit_mode=False, _create_edit_target=None)
        await push_history(state, OneTimeReminder.exact_end_time.state)
        await state.set_state(OneTimeReminder.confirm)
        await render_step(message, state, OneTimeReminder.confirm.state)
        return

    await push_history(state, OneTimeReminder.exact_end_time.state)
    await state.set_state(OneTimeReminder.mode)
    await render_step(message, state, OneTimeReminder.mode.state)


@dp.message(OneTimeReminder.mode, F.text == "В конкретное время дня")
async def one_time_mode_fixed(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="fixed")

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "time":
        await push_history(state, OneTimeReminder.mode.state)
        await state.set_state(OneTimeReminder.fixed_times)
        await render_step(message, state, OneTimeReminder.fixed_times.state)
        return

    await push_history(state, OneTimeReminder.mode.state)
    await state.set_state(OneTimeReminder.fixed_times)
    await render_step(message, state, OneTimeReminder.fixed_times.state)


@dp.message(OneTimeReminder.mode, F.text == "В промежутке")
async def one_time_mode_periodic(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="periodic")

    data = await state.get_data()
    if data.get("_create_edit_mode") and data.get("_create_edit_target") == "time":
        await push_history(state, OneTimeReminder.mode.state)
        await state.set_state(OneTimeReminder.periodic_range)
        await render_step(message, state, OneTimeReminder.periodic_range.state)
        return

    await push_history(state, OneTimeReminder.mode.state)
    await state.set_state(OneTimeReminder.periodic_range)
    await render_step(message, state, OneTimeReminder.periodic_range.state)


@dp.message(OneTimeReminder.fixed_times)
async def one_time_fixed_times(message: Message, state: FSMContext):
    times = parse_times_space(message.text or "")
    if not times:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит: 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    await state.update_data(
        times_text=",".join(times),
        start_time=None,
        end_time=None,
        interval_minutes=None,
    )
    await push_history(state, OneTimeReminder.fixed_times.state)
    await state.set_state(OneTimeReminder.confirm)
    await render_step(message, state, OneTimeReminder.confirm.state)


@dp.message(OneTimeReminder.periodic_range)
async def one_time_periodic_range(message: Message, state: FSMContext):
    result = parse_range(message.text or "")
    if not result:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 2300 2400\n"
            "Это значит: с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    start_time, end_time = result
    await state.update_data(start_time=start_time, end_time=end_time, times_text=None)
    await push_history(state, OneTimeReminder.periodic_range.state)
    await state.set_state(OneTimeReminder.periodic_interval)
    await render_step(message, state, OneTimeReminder.periodic_interval.state)


@dp.message(OneTimeReminder.periodic_interval, F.text == "Каждую 1 минуту")
async def one_time_int_1(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_one_time_periodic(message, state, 1)


@dp.message(OneTimeReminder.periodic_interval, F.text == "Каждые 5 минут")
async def one_time_int_5(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_one_time_periodic(message, state, 5)


@dp.message(OneTimeReminder.periodic_interval, F.text == "Каждые 30 минут")
async def one_time_int_30(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_one_time_periodic(message, state, 30)

@dp.message(OneTimeReminder.periodic_interval, F.text == "Каждые 60 минут")
async def one_time_int_60(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_one_time_periodic(message, state, 60)


@dp.message(OneTimeReminder.periodic_interval, F.text == "Свой интервал")
async def one_time_custom_interval_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await message.answer("Введи интервал в минутах.\nМинимум 1.", reply_markup=back_kb())


@dp.message(OneTimeReminder.periodic_interval)
async def one_time_custom_interval(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Хуйня. Интервал должен быть числом.", reply_markup=back_kb())
        return

    n = int(text)
    if n < MIN_INTERVAL:
        await message.answer("Хуйня. Минимальный интервал — 1 минута.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await finish_one_time_periodic(message, state, n)


async def finish_one_time_periodic(message: Message, state: FSMContext, interval: int):
    await state.update_data(interval_minutes=interval)
    await push_history(state, OneTimeReminder.periodic_interval.state)
    await state.set_state(OneTimeReminder.confirm)
    await render_step(message, state, OneTimeReminder.confirm.state)


@dp.message(OneTimeReminder.confirm, F.text == "Сохранить")
async def one_time_save(message: Message, state: FSMContext):
    data = await state.get_data()
    create_reminder_db(
        user_id=message.from_user.id,
        title=data["title"],
        target_date=data["target_date"],
        exact_end_time=data.get("exact_end_time"),
        frequency_type="one_time",
        mode_type=data["mode_type"],
        times_text=data.get("times_text"),
        weekdays_text=None,
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        interval_minutes=data.get("interval_minutes"),
        reminder_kind="one_time",
        remind_date=data["remind_date"],
    )

    reminders = get_user_reminders(message.from_user.id)
    rem = reminders[-1]
    timezone_name = data["user_timezone"]

    await state.clear()
    await send_saved_card(message, rem, timezone_name)

    user = get_user(message.from_user.id)
    if user and not user.get("notifications_hint_shown"):
        mark_notifications_hint_shown(message.from_user.id)
        await message.answer("Не забудь включить уведомления,\nиначе я не смогу нормально заебывать.")


@dp.message(OneTimeReminder.confirm, F.text == "Изменить")
async def one_time_edit_from_confirm(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, OneTimeReminder.confirm.state)
    await state.set_state(OneTimeReminder.edit_field)
    await render_step(message, state, OneTimeReminder.edit_field.state)


@dp.message(OneTimeReminder.edit_field, F.text == "Повод")
async def one_time_edit_title(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="title")
    await push_history(state, OneTimeReminder.edit_field.state)
    await state.set_state(OneTimeReminder.title)
    await message.answer("Введи новый повод.", reply_markup=back_kb())


@dp.message(OneTimeReminder.edit_field, F.text == "Дату")
async def one_time_edit_date(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="target_date")
    await push_history(state, OneTimeReminder.edit_field.state)
    await state.set_state(OneTimeReminder.target_date)
    await render_step(message, state, OneTimeReminder.target_date.state)


@dp.message(OneTimeReminder.edit_field, F.text == "День напоминания")
async def one_time_edit_remind_date(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="remind_date")
    await push_history(state, OneTimeReminder.edit_field.state)
    await state.set_state(OneTimeReminder.remind_date)
    await render_step(message, state, OneTimeReminder.remind_date.state)


@dp.message(OneTimeReminder.edit_field, F.text == "Точное время конца")
async def one_time_edit_exact_end(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="exact_end_time")
    await push_history(state, OneTimeReminder.edit_field.state)
    await state.set_state(OneTimeReminder.ask_exact_end)
    await render_step(message, state, OneTimeReminder.ask_exact_end.state)


@dp.message(OneTimeReminder.edit_field, F.text == "Время")
async def one_time_edit_time(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(_create_edit_mode=True, _create_edit_target="time")
    await push_history(state, OneTimeReminder.edit_field.state)

    data = await state.get_data()
    if data["mode_type"] == "fixed":
        await state.set_state(OneTimeReminder.fixed_times)
        await render_step(message, state, OneTimeReminder.fixed_times.state)
    else:
        await state.set_state(OneTimeReminder.periodic_range)
        await render_step(message, state, OneTimeReminder.periodic_range.state)


# =========================
# РЕДАКТИРОВАНИЕ
# =========================
@dp.message(F.text == "Изменить заеб")
async def edit_start(message: Message, state: FSMContext):
    reminders = get_user_reminders(message.from_user.id)
    user = get_user(message.from_user.id)

    if not reminders:
        await state.clear()
        await message.answer("Пока ни одного заеба нет.")
        await send_menu_block(message)
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(
        _history=[],
        pick_list=reminders,
        _weekdays_msg_id=None,
        user_timezone=user["timezone"],
    )
    await state.set_state(EditReminder.pick)
    await render_step(message, state, EditReminder.pick.state)


@dp.message(EditReminder.pick)
async def edit_pick(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    reminders = data.get("pick_list", [])
    timezone_name = data["user_timezone"]

    sorted_reminders = sorted(
        reminders,
        key=lambda rem: next_notification_dt(rem, timezone_name) or datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    )

    try:
        idx = int(text.split(".")[0]) - 1
        if idx < 0 or idx >= len(sorted_reminders):
            raise ValueError
    except Exception:
        await misclick_reply(message, state, "edit_pick", select_reminder_kb(reminders, timezone_name))
        return

    await reset_misclick(state)
    rem = sorted_reminders[idx]
    weekdays_selected = [int(x) for x in rem["weekdays_text"].split(",")] if rem.get("weekdays_text") else []

    await state.update_data(
        edit_id=rem["id"],
        title=rem["title"],
        target_date=rem["target_date"],
        remind_date=rem.get("remind_date"),
        exact_end_time=rem.get("exact_end_time"),
        frequency_type=rem["frequency_type"],
        reminder_kind=rem.get("reminder_kind", "regular"),
        mode_type=rem["mode_type"],
        times_text=rem.get("times_text"),
        weekdays_text=rem.get("weekdays_text"),
        start_time=rem.get("start_time"),
        end_time=rem.get("end_time"),
        interval_minutes=rem.get("interval_minutes"),
        weekdays_selected=weekdays_selected,
    )
    await push_history(state, EditReminder.pick.state)
    await state.set_state(EditReminder.field)
    await render_step(message, state, EditReminder.field.state)


@dp.message(EditReminder.field, F.text == "Повод")
async def edit_title_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.title)
    await render_step(message, state, EditReminder.title.state)


@dp.message(EditReminder.field, F.text == "Дату")
async def edit_date_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.target_date)
    await render_step(message, state, EditReminder.target_date.state)


@dp.message(EditReminder.field, F.text == "День напоминания")
async def edit_remind_date_ask(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("reminder_kind") != "one_time":
        await message.answer(
            "У этого заеба нет отдельного дня напоминания.",
            reply_markup=edit_field_kb(data.get("reminder_kind", "regular"), data.get("frequency_type")),
        )
        return

    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.remind_date)
    await render_step(message, state, EditReminder.remind_date.state)


@dp.message(EditReminder.field, F.text == "Точное время конца")
async def edit_exact_end_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.ask_exact_end)
    await render_step(message, state, EditReminder.ask_exact_end.state)


@dp.message(EditReminder.field, F.text == "Тип заеба")
async def edit_type_ask(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("reminder_kind") == "one_time":
        await message.answer(
            "У одноразового заеба тип менять не нужно.",
            reply_markup=edit_field_kb("one_time"),
        )
        return

    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.frequency)
    await render_step(message, state, EditReminder.frequency.state)


@dp.message(EditReminder.field, F.text == "Дни недели")
async def edit_days_ask(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("reminder_kind") == "one_time":
        await message.answer(
            "У одноразового заеба нет дней недели.",
            reply_markup=edit_field_kb("one_time"),
        )
        return

    if data["frequency_type"] != "weekdays":
        await message.answer(
            "У этого заеба нет дней недели.",
            reply_markup=edit_field_kb("regular", data["frequency_type"]),
        )
        return

    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)
    await state.set_state(EditReminder.weekdays_pick)
    await render_step(message, state, EditReminder.weekdays_pick.state)


@dp.message(EditReminder.field, F.text == "Время")
async def edit_time_ask(message: Message, state: FSMContext):
    data = await state.get_data()
    await reset_misclick(state)
    await push_history(state, EditReminder.field.state)

    if data["mode_type"] == "fixed":
        await state.set_state(EditReminder.fixed_times)
        await render_step(message, state, EditReminder.fixed_times.state)
    else:
        await state.set_state(EditReminder.periodic_range)
        await render_step(message, state, EditReminder.periodic_range.state)


@dp.message(EditReminder.title)
async def edit_title_set(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Хуйня. Введи повод нормально.", reply_markup=back_kb())
        return
    if len(title) > MAX_TITLE_LEN:
        await message.answer(f"Слишком длинно. Максимум {MAX_TITLE_LEN} символа.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(title=title)
    await push_history(state, EditReminder.title.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.target_date)
async def edit_date_set(message: Message, state: FSMContext):
    data = await state.get_data()
    user_now = get_user_now(data["user_timezone"])

    d = parse_date_ddmmyy(message.text or "")
    if not d:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 020726\nЭто значит: 02.07.2026", reply_markup=back_kb())
        return
    if d < user_now.date():
        await message.answer("Дурак совсем? Это ведь прошлое.", reply_markup=back_kb())
        return

    if data.get("reminder_kind") == "one_time" and data.get("remind_date"):
        remind_d = date.fromisoformat(data["remind_date"])
        if remind_d > d:
            await message.answer("Хуйня. Конечная дата не может быть раньше дня напоминания.", reply_markup=back_kb())
            return

    await reset_misclick(state)
    await state.update_data(target_date=d.isoformat())
    await push_history(state, EditReminder.target_date.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.remind_date)
async def edit_remind_date_set(message: Message, state: FSMContext):
    data = await state.get_data()
    user_now = get_user_now(data["user_timezone"])

    d = parse_date_ddmmyy(message.text or "")
    if not d:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 020726\nЭто значит: 02.07.2026", reply_markup=back_kb())
        return
    if d < user_now.date():
        await message.answer("Дурак совсем? Это ведь прошлое.", reply_markup=back_kb())
        return

    target_date = date.fromisoformat(data["target_date"])
    if d > target_date:
        await message.answer("Хуйня. День напоминания не может быть позже конечной даты.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(remind_date=d.isoformat())
    await push_history(state, EditReminder.remind_date.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.ask_exact_end, F.text == "Да")
async def edit_exact_end_yes(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, EditReminder.ask_exact_end.state)
    await state.set_state(EditReminder.exact_end_time)
    await render_step(message, state, EditReminder.exact_end_time.state)


@dp.message(EditReminder.ask_exact_end, F.text == "Нет")
async def edit_exact_end_no(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(exact_end_time=None)
    await push_history(state, EditReminder.ask_exact_end.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.exact_end_time)
async def edit_exact_end_time(message: Message, state: FSMContext):
    parsed = parse_hhmm(message.text or "")
    if not parsed:
        await message.answer("Хуйня. Напиши нормально.\n\nПиши в таком формате: 1400\nЭто значит: 14:00", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await state.update_data(exact_end_time=parsed)
    await push_history(state, EditReminder.exact_end_time.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.frequency, F.text == "Каждый день")
async def edit_freq_daily(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(
        frequency_type="daily",
        weekdays_text=None,
        weekdays_selected=[],
    )
    await push_history(state, EditReminder.frequency.state)
    await state.set_state(EditReminder.mode)
    await render_step(message, state, EditReminder.mode.state)


@dp.message(EditReminder.frequency, F.text == "В определенные дни недели")
async def edit_freq_weekdays(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(
        frequency_type="weekdays",
        weekdays_selected=[],
        weekdays_text=None,
    )
    await push_history(state, EditReminder.frequency.state)
    await state.set_state(EditReminder.weekdays_pick)
    await render_step(message, state, EditReminder.weekdays_pick.state)


@dp.message(EditReminder.weekdays_pick, F.text == "Готово")
async def edit_weekdays_done(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = sorted(set(data.get("weekdays_selected", [])))
    if not selected:
        await message.answer("Сначала выбери хотя бы один день.", reply_markup=done_back_kb())
        return

    await reset_misclick(state)
    await state.update_data(weekdays_text=",".join(map(str, selected)))
    await push_history(state, EditReminder.weekdays_pick.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.weekdays_pick)
async def edit_weekdays_buttons_only(message: Message, state: FSMContext):
    await misclick_reply(message, state, "edit_weekdays", done_back_kb())


@dp.message(EditReminder.mode, F.text == "В конкретное время дня")
async def edit_mode_fixed(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="fixed")
    await push_history(state, EditReminder.mode.state)
    await state.set_state(EditReminder.fixed_times)
    await render_step(message, state, EditReminder.fixed_times.state)


@dp.message(EditReminder.mode, F.text == "В промежутке")
async def edit_mode_periodic(message: Message, state: FSMContext):
    await reset_misclick(state)
    await state.update_data(mode_type="periodic")
    await push_history(state, EditReminder.mode.state)
    await state.set_state(EditReminder.periodic_range)
    await render_step(message, state, EditReminder.periodic_range.state)


@dp.message(EditReminder.fixed_times)
async def edit_fixed_times(message: Message, state: FSMContext):
    times = parse_times_space(message.text or "")
    if not times:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит: 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    await state.update_data(
        times_text=",".join(times),
        start_time=None,
        end_time=None,
        interval_minutes=None,
    )
    await push_history(state, EditReminder.fixed_times.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.periodic_range)
async def edit_periodic_range(message: Message, state: FSMContext):
    result = parse_range(message.text or "")
    if not result:
        await message.answer(
            "Хуйня. Напиши нормально.\n\n"
            "Пиши в таком формате: 2300 2400\n"
            "Это значит: с 23:00 до 24:00",
            reply_markup=back_kb(),
        )
        return

    await reset_misclick(state)
    start_time, end_time = result
    await state.update_data(start_time=start_time, end_time=end_time, times_text=None)
    await push_history(state, EditReminder.periodic_range.state)
    await state.set_state(EditReminder.periodic_interval)
    await render_step(message, state, EditReminder.periodic_interval.state)


@dp.message(EditReminder.periodic_interval, F.text == "Каждую 1 минуту")
async def edit_int_1(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_edit_periodic(message, state, 1)


@dp.message(EditReminder.periodic_interval, F.text == "Каждые 5 минут")
async def edit_int_5(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_edit_periodic(message, state, 5)


@dp.message(EditReminder.periodic_interval, F.text == "Каждые 30 минут")
async def edit_int_30(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_edit_periodic(message, state, 30)


@dp.message(EditReminder.periodic_interval, F.text == "Каждые 60 минут")
async def edit_int_60(message: Message, state: FSMContext):
    await reset_misclick(state)
    await finish_edit_periodic(message, state, 60)


@dp.message(EditReminder.periodic_interval, F.text == "Свой интервал")
async def edit_custom_interval_ask(message: Message, state: FSMContext):
    await reset_misclick(state)
    await message.answer("Введи интервал в минутах.\nМинимум 1.", reply_markup=back_kb())


@dp.message(EditReminder.periodic_interval)
async def edit_custom_interval(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Хуйня. Интервал должен быть числом.", reply_markup=back_kb())
        return

    n = int(text)
    if n < MIN_INTERVAL:
        await message.answer("Хуйня. Минимальный интервал — 1 минута.", reply_markup=back_kb())
        return

    await reset_misclick(state)
    await finish_edit_periodic(message, state, n)


async def finish_edit_periodic(message: Message, state: FSMContext, interval: int):
    await state.update_data(interval_minutes=interval)
    await push_history(state, EditReminder.periodic_interval.state)
    await state.set_state(EditReminder.confirm)
    await render_step(message, state, EditReminder.confirm.state)


@dp.message(EditReminder.confirm, F.text == "Сохранить")
async def edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    payload = {
        "title": data["title"],
        "target_date": data["target_date"],
        "exact_end_time": data.get("exact_end_time"),
        "frequency_type": data["frequency_type"],
        "mode_type": data["mode_type"],
        "times_text": data.get("times_text"),
        "weekdays_text": data.get("weekdays_text"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "interval_minutes": data.get("interval_minutes"),
        "reminder_kind": data.get("reminder_kind", "regular"),
        "remind_date": data.get("remind_date"),
    }
    update_reminder_db(data["edit_id"], message.from_user.id, payload)

    reminders = get_user_reminders(message.from_user.id)
    rem = next(r for r in reminders if r["id"] == data["edit_id"])
    timezone_name = data["user_timezone"]

    await state.clear()
    await send_saved_card(message, rem, timezone_name)


@dp.message(EditReminder.confirm, F.text == "Изменить")
async def edit_change_again(message: Message, state: FSMContext):
    await reset_misclick(state)
    await push_history(state, EditReminder.confirm.state)
    await state.set_state(EditReminder.field)
    await render_step(message, state, EditReminder.field.state)


# =========================
# УДАЛЕНИЕ
# =========================
@dp.message(F.text == "🗑 Заебал")
async def delete_start(message: Message, state: FSMContext):
    reminders = get_user_reminders(message.from_user.id)
    user = get_user(message.from_user.id)

    if not reminders:
        await state.clear()
        await message.answer("Пока ни одного заеба нет.")
        await send_menu_block(message)
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(_history=[], pick_list=reminders, user_timezone=user["timezone"])
    await state.set_state(DeleteReminder.pick)
    await render_step(message, state, DeleteReminder.pick.state)


@dp.message(DeleteReminder.pick)
async def delete_pick(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    reminders = data.get("pick_list", [])
    timezone_name = data["user_timezone"]

    if text == "Заебал, удалить всё":
        await reset_misclick(state)
        await state.update_data(delete_all=True)
        await push_history(state, DeleteReminder.pick.state)
        await state.set_state(DeleteReminder.confirm)
        await render_step(message, state, DeleteReminder.confirm.state)
        return

    sorted_reminders = sorted(
        reminders,
        key=lambda rem: next_notification_dt(rem, timezone_name) or datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    )

    try:
        idx = int(text.split(".")[0]) - 1
        if idx < 0 or idx >= len(sorted_reminders):
            raise ValueError
    except Exception:
        await misclick_reply(message, state, "delete_pick", select_reminder_kb(reminders, timezone_name, with_delete_all=True))
        return

    await reset_misclick(state)
    rem = sorted_reminders[idx]
    await state.update_data(delete_all=False, delete_reminder=rem)
    await push_history(state, DeleteReminder.pick.state)
    await state.set_state(DeleteReminder.confirm)
    await render_step(message, state, DeleteReminder.confirm.state)


@dp.message(DeleteReminder.confirm, F.text == "Да, заебал")
async def delete_yes(message: Message, state: FSMContext):
    data = await state.get_data()

    if data.get("delete_all"):
        delete_all_reminders_db(message.from_user.id)
        await state.clear()
        await message.answer("Удалил всё.")
        await send_menu_block(message)
        return

    rem = data["delete_reminder"]
    delete_reminder_db(rem["id"], message.from_user.id)
    await state.clear()
    await message.answer("Удалил.")
    await send_menu_block(message)


# =========================
# ПРОЧЕЕ
# =========================
@dp.message(F.text == "Меню")
async def menu_text(message: Message, state: FSMContext):
    await clear_weekdays_picker(state, message.chat.id)
    await state.clear()
    await send_menu_block(message)


@dp.message(F.text == "📋 Показать заебы")
async def show_reminders_by_text(message: Message, state: FSMContext):
    await state.clear()
    await reset_misclick(state)

    user = get_user(message.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("Пока ни одного заеба нет.")
        await send_menu_block(message)
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Изменить заеб")],
            [KeyboardButton(text="Меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer(list_numbered_reminders(reminders, user["timezone"]), reply_markup=kb)


@dp.message(CreateReminder.ask_exact_end)
@dp.message(CreateReminder.frequency)
@dp.message(CreateReminder.mode)
@dp.message(CreateReminder.periodic_interval)
@dp.message(CreateReminder.confirm)
@dp.message(CreateReminder.edit_field)
@dp.message(OneTimeReminder.ask_exact_end)
@dp.message(OneTimeReminder.mode)
@dp.message(OneTimeReminder.periodic_interval)
@dp.message(OneTimeReminder.confirm)
@dp.message(OneTimeReminder.edit_field)
@dp.message(EditReminder.field)
@dp.message(EditReminder.ask_exact_end)
@dp.message(EditReminder.frequency)
@dp.message(EditReminder.mode)
@dp.message(EditReminder.periodic_interval)
@dp.message(EditReminder.confirm)
@dp.message(DeleteReminder.confirm)
@dp.message(CountrySetup.choose)
async def buttons_only_states(message: Message, state: FSMContext):
    current = await state.get_state()
    key = current or "common"

    reply_markup = None
    if current == CountrySetup.choose.state:
        reply_markup = country_inline()

    await misclick_reply(message, state, key, reply_markup=reply_markup)


@dp.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await misclick_reply(message, state, "fallback")
    else:
        await message.answer(
            "Чел, используй кнопки пожалуйста, Денис очень старался делая их.",
            reply_markup=main_menu_inline(),
        )


# =========================
# ЦИКЛ НАПОМИНАНИЙ
# =========================
async def reminder_loop():
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                SELECT r.*, u.timezone
                FROM reminders r
                JOIN users u ON u.user_id = r.user_id
                WHERE r.active = 1 AND u.timezone IS NOT NULL
            """)
            reminders = [dict(x) for x in cur.fetchall()]
            conn.close()

            for rem in reminders:
                timezone_name = rem["timezone"]
                user_now = get_user_now(timezone_name)
                finish_dt = reminder_finish_dt(rem, timezone_name)

                if user_now >= finish_dt:
                    deactivate_reminder(rem["id"])
                    continue

                need_send = False
                current_hhmm = user_now.hour * 60 + user_now.minute
                current_text = f"{user_now.hour:02d}:{user_now.minute:02d}"
                weekday = user_now.weekday()

                if rem.get("reminder_kind") == "one_time":
                    remind_day = rem.get("remind_date")
                    if remind_day == user_now.date().isoformat():
                        if rem["mode_type"] == "fixed":
                            times = rem["times_text"].split(",") if rem.get("times_text") else []
                            if current_text in times:
                                need_send = True
                        elif rem["mode_type"] == "periodic":
                            if rem.get("start_time") and rem.get("end_time") and rem.get("interval_minutes"):
                                start_minutes = parse_minutes_hhmm(rem["start_time"])
                                end_minutes = parse_minutes_hhmm(rem["end_time"])
                                if start_minutes <= current_hhmm < end_minutes:
                                    if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                        need_send = True

                elif rem["frequency_type"] == "daily":
                    if rem["mode_type"] == "fixed":
                        times = rem["times_text"].split(",") if rem.get("times_text") else []
                        if current_text in times:
                            need_send = True
                    elif rem["mode_type"] == "periodic":
                        if rem.get("start_time") and rem.get("end_time") and rem.get("interval_minutes"):
                            start_minutes = parse_minutes_hhmm(rem["start_time"])
                            end_minutes = parse_minutes_hhmm(rem["end_time"])
                            if start_minutes <= current_hhmm < end_minutes:
                                if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                    need_send = True

                elif rem["frequency_type"] == "weekdays":
                    days = [int(x) for x in rem["weekdays_text"].split(",")] if rem.get("weekdays_text") else []
                    if weekday in days:
                        if rem["mode_type"] == "fixed":
                            times = rem["times_text"].split(",") if rem.get("times_text") else []
                            if current_text in times:
                                need_send = True
                        elif rem["mode_type"] == "periodic":
                            if rem.get("start_time") and rem.get("end_time") and rem.get("interval_minutes"):
                                start_minutes = parse_minutes_hhmm(rem["start_time"])
                                end_minutes = parse_minutes_hhmm(rem["end_time"])
                                if start_minutes <= current_hhmm < end_minutes:
                                    if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                        need_send = True

                if not need_send:
                    continue

                sent_key = f'{user_now.strftime("%Y-%m-%d %H:%M")}::{rem["id"]}'
                if not try_mark_sent(rem["id"], sent_key):
                    continue

                try:
                    await bot.send_message(
                        rem["user_id"],
                        reminder_text(rem, timezone_name),
                        reply_markup=main_menu_inline(),
                    )
                except Exception as send_error:
                    print("send reminder error:", send_error)

            await asyncio.sleep(20)
        except Exception as e:
            print("reminder_loop error:", e)
            await asyncio.sleep(10)


# =========================
# ЗАПУСК
# =========================
async def main():
    init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())