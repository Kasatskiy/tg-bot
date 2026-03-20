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
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


BOT_TOKEN = "8710757819:AAFra83pBHkxPT9m6BYJRY9kEh8Akry39gI"
DB_NAME = "bot.db"

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
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        country TEXT,
        timezone TEXT,
        notification_hint_sent INTEGER NOT NULL DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        target_date TEXT NOT NULL,
        exact_end_time TEXT,
        frequency_type TEXT NOT NULL,   -- daily / weekdays / once
        mode_type TEXT NOT NULL,        -- fixed / periodic
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


def mark_notification_hint_sent(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET notification_hint_sent = 1 WHERE user_id = ?", (user_id,))
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
):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reminders (
            user_id, title, target_date, exact_end_time, frequency_type, mode_type,
            times_text, weekdays_text, start_time, end_time, interval_minutes, active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        user_id,
        title,
        target_date,
        exact_end_time,
        frequency_type,
        mode_type,
        times_text,
        weekdays_text,
        start_time,
        end_time,
        interval_minutes,
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
        ORDER BY target_date ASC, COALESCE(exact_end_time, '23:59') ASC, id ASC
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


def was_sent(reminder_id: int, sent_key: str) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_log WHERE reminder_id = ? AND sent_key = ?", (reminder_id, sent_key))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(reminder_id: int, sent_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_log (reminder_id, sent_key) VALUES (?, ?)",
        (reminder_id, sent_key),
    )
    conn.commit()
    conn.close()


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


class EditReminder(StatesGroup):
    pick = State()
    field = State()
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

    if rem["frequency_type"] == "once":
        if rem["mode_type"] == "fixed" and rem.get("times_text"):
            last_time = max([x for x in rem["times_text"].split(",") if x])
            hh, mm = map(int, last_time.split(":"))
            return datetime.combine(d, time(hh, mm), tzinfo=tz)

        if rem["mode_type"] == "periodic" and rem.get("end_time"):
            if rem["end_time"] == "24:00":
                return datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
            hh, mm = map(int, rem["end_time"].split(":"))
            return datetime.combine(d, time(hh, mm), tzinfo=tz)

        return datetime.combine(d, time(23, 59), tzinfo=tz)

    if rem.get("exact_end_time"):
        if rem["exact_end_time"] == "24:00":
            return datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
        hh, mm = map(int, rem["exact_end_time"].split(":"))
        return datetime.combine(d, time(hh, mm), tzinfo=tz)

    return datetime.combine(d, time(23, 59), tzinfo=tz)


def format_date_ru(iso_date: str):
    return date.fromisoformat(iso_date).strftime("%d.%m.%Y")


def format_duration_parts(total_seconds: int):
    if total_seconds <= 0:
        return 0, 0, 0
    days = total_seconds // 86400
    total_seconds %= 86400
    hours = total_seconds // 3600
    total_seconds %= 3600
    minutes = total_seconds // 60
    return int(days), int(hours), int(minutes)


def reminder_text(rem: dict, timezone_name: str):
    user_now = get_user_now(timezone_name)
    finish_dt = reminder_finish_dt(rem, timezone_name)

    if rem["frequency_type"] == "once":
        if rem["mode_type"] == "fixed":
            return f'Сегодня {rem["title"]}'
        diff_seconds = int((finish_dt - user_now).total_seconds())
        if diff_seconds <= 0:
            return f'Сегодня {rem["title"]}'
        days, hours, minutes = format_duration_parts(diff_seconds)
        return f'До {rem["title"]} осталось {days} дн. {hours} ч. {minutes} мин.'

    if rem.get("exact_end_time"):
        diff_seconds = int((finish_dt - user_now).total_seconds())
        if diff_seconds <= 0:
            return f'Сегодня {rem["title"]}'
        days, hours, minutes = format_duration_parts(diff_seconds)
        return f'До {rem["title"]} осталось {days} дн. {hours} ч. {minutes} мин.'

    diff_days = (finish_dt.date() - user_now.date()).days
    if diff_days <= 0:
        return f'Сегодня {rem["title"]}'
    return f'До {rem["title"]} осталось {diff_days} дней'


def reminder_brief(rem: dict):
    if rem["frequency_type"] == "daily":
        freq_text = "Каждый день"
    elif rem["frequency_type"] == "weekdays":
        days = [WEEKDAYS[int(x)] for x in rem["weekdays_text"].split(",") if x != ""]
        freq_text = ", ".join(days)
    else:
        freq_text = "Разово"

    if rem["mode_type"] == "fixed":
        return f'{freq_text} в {rem["times_text"].replace(",", " ")}'
    return f'{freq_text} с {rem["start_time"]} до {rem["end_time"]} каждые {rem["interval_minutes"]} мин.'


def build_summary(data: dict):
    lines = ["Проверь заеб\n"]
    lines.append(f'Повод: {data["title"]}')
    lines.append(f'До: {format_date_ru(data["target_date"])}')

    if data["frequency_type"] == "once":
        lines.append("Тип: одноразовый")
    elif data["frequency_type"] == "daily":
        lines.append("Частота: каждый день")
    else:
        days = [WEEKDAYS[int(x)] for x in data["weekdays_text"].split(",") if x != ""]
        lines.append(f'Дни: {", ".join(days)}')

    if data["frequency_type"] != "once" and data.get("exact_end_time"):
        lines.append(f'Точный конец: {data["exact_end_time"]}')

    if data["mode_type"] == "fixed":
        lines.append("Режим: в конкретное время дня")
        lines.append(f'Время: {data["times_text"].replace(",", " ")}')
    else:
        lines.append("Режим: в промежутке")
        lines.append(f'Промежуток: {data["start_time"]} — {data["end_time"]}')
        lines.append(f'Интервал: каждые {data["interval_minutes"]} минут')

    lines.append("\nСохраняем?")
    return "\n".join(lines)


def is_allowed_day(rem: dict, day: date) -> bool:
    target = date.fromisoformat(rem["target_date"])

    if rem["frequency_type"] == "daily":
        return day <= target

    if rem["frequency_type"] == "weekdays":
        if day > target:
            return False
        days = [int(x) for x in rem["weekdays_text"].split(",")] if rem.get("weekdays_text") else []
        return day.weekday() in days

    if rem["frequency_type"] == "once":
        return day == target

    return False


def get_day_candidates(rem: dict, day: date, timezone_name: str):
    tz = ZoneInfo(timezone_name)
    result = []

    if not is_allowed_day(rem, day):
        return result

    if rem["mode_type"] == "fixed":
        times = rem["times_text"].split(",") if rem.get("times_text") else []
        for t in times:
            hh, mm = map(int, t.split(":"))
            result.append(datetime.combine(day, time(hh, mm), tzinfo=tz))
        return result

    if rem["mode_type"] == "periodic" and rem.get("start_time") and rem.get("end_time") and rem.get("interval_minutes"):
        start_minutes = parse_minutes_hhmm(rem["start_time"])
        end_minutes = parse_minutes_hhmm(rem["end_time"])
        interval = int(rem["interval_minutes"])

        cur = start_minutes
        while cur < end_minutes:
            hh = cur // 60
            mm = cur % 60
            result.append(datetime.combine(day, time(hh, mm), tzinfo=tz))
            cur += interval

    return result


def get_next_notification_dt(rem: dict, timezone_name: str):
    user_now = get_user_now(timezone_name)
    finish_dt = reminder_finish_dt(rem, timezone_name)

    if user_now > finish_dt:
        return None

    end_day = date.fromisoformat(rem["target_date"])
    start_day = user_now.date() if rem["frequency_type"] != "once" else end_day

    day = start_day
    while day <= end_day:
        for candidate in get_day_candidates(rem, day, timezone_name):
            if candidate >= user_now and candidate <= finish_dt:
                return candidate
        day += timedelta(days=1)

    return None


def format_next_notification(next_dt: datetime | None, timezone_name: str):
    if not next_dt:
        return "завершен"

    now = get_user_now(timezone_name)
    if next_dt.date() == now.date():
        return f"сегодня в {next_dt.strftime('%H:%M')}"
    if next_dt.date() == now.date() + timedelta(days=1):
        return f"завтра в {next_dt.strftime('%H:%M')}"
    return f"{next_dt.strftime('%d.%m.%Y')} в {next_dt.strftime('%H:%M')}"


def list_numbered_reminders(reminders: list[dict], timezone_name: str):
    parts = []
    for i, rem in enumerate(reminders, start=1):
        next_dt = get_next_notification_dt(rem, timezone_name)
        extra = f'\nТочный конец: {rem["exact_end_time"]}' if rem.get("exact_end_time") and rem["frequency_type"] != "once" else ""
        parts.append(
            f'{i}. {rem["title"]}\n'
            f'До: {format_date_ru(rem["target_date"])}{extra}\n'
            f"{reminder_brief(rem)}\n"
            f"Следующее уведомление: {format_next_notification(next_dt, timezone_name)}"
        )
    return "\n\n".join(parts)


# =========================
# АНТИЖМЯК
# =========================
async def reset_misclick(state: FSMContext):
    await state.update_data(_misclick_key=None, _misclick_count=0)


async def misclick_reply(message: Message, state: FSMContext, key: str, reply_markup=None):
    data = await state.get_data()
    prev_key = data.get("_misclick_key")
    prev_count = data.get("_misclick_count", 0)
    count = prev_count + 1 if prev_key == key else 1
    await state.update_data(_misclick_key=key, _misclick_count=count)
    await message.answer("Чел, используй кнопки пожалуйста.", reply_markup=reply_markup)


# =========================
# КНОПКИ
# =========================
def main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Сделать новый заеб", callback_data="menu:create"))
    builder.row(InlineKeyboardButton(text="Одноразовый заеб", callback_data="menu:create_once"))
    builder.row(InlineKeyboardButton(text="Мои заебы", callback_data="menu:show"))
    builder.row(InlineKeyboardButton(text="Заебал", callback_data="menu:delete"))
    builder.row(InlineKeyboardButton(text="Изменить страну", callback_data="menu:country"))
    return builder.as_markup()


def show_list_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Изменить заеб", callback_data="menu:edit"))
    builder.row(InlineKeyboardButton(text="Меню", callback_data="menu:main"))
    return builder.as_markup()


def weekdays_inline(selected: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(7):
        title = WEEKDAYS[i]
        if i in selected:
            title = f"✅ {title}"
        builder.row(InlineKeyboardButton(text=title, callback_data=f"weekday_toggle:{i}"))
    return builder.as_markup()


def country_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Украина")],
            [KeyboardButton(text="Чехия")],
        ],
        resize_keyboard=True,
    )


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


def select_reminder_kb(reminders: list[dict], with_delete_all: bool = False):
    rows = []
    for i, rem in enumerate(reminders, start=1):
        rows.append([KeyboardButton(text=f'{i}. {rem["title"]}')])
    if with_delete_all:
        rows.append([KeyboardButton(text="Заебал, удалить всё")])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def edit_field_kb(freq_type: str):
    rows = [[KeyboardButton(text="Повод")], [KeyboardButton(text="Дату")]]

    if freq_type != "once":
        rows.append([KeyboardButton(text="Точное время конца")])
        rows.append([KeyboardButton(text="Тип заеба")])

    if freq_type == "weekdays":
        rows.append([KeyboardButton(text="Дни недели")])

    rows.append([KeyboardButton(text="Время")])
    rows.append([KeyboardButton(text="Назад")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def delete_confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, заебал")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


# =========================
# РЕНДЕР
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


async def open_main_menu_message(message: Message, text: str = "Привет.\nЯ буду заебывать тебя до нужной даты."):
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("Выбирай:", reply_markup=main_menu())


async def render_country_step(message: Message):
    await message.answer(
        "Где ты живешь.\n\n"
        "Это нужно для определения твоего часового пояса.",
        reply_markup=country_kb(),
    )


async def render_weekdays_step(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("weekdays_selected", [])
    selected_text = ", ".join(WEEKDAYS[x] for x in sorted(selected)) if selected else "пока ничего"

    await clear_weekdays_picker(state, message.chat.id)
    sent = await message.answer(
        "Выбери дни, в которые тебя заебывать.\n\n"
        f"Выбрано: {selected_text}",
        reply_markup=weekdays_inline(set(selected)),
    )
    await state.update_data(_weekdays_msg_id=sent.message_id)
    await message.answer("Когда закончишь — жми кнопку снизу.", reply_markup=done_back_kb())


async def render_step(message: Message, state: FSMContext, state_name: str):
    data = await state.get_data()

    if state_name not in {CreateReminder.weekdays_pick.state, EditReminder.weekdays_pick.state}:
        await clear_weekdays_picker(state, message.chat.id)

    is_one_time = data.get("_is_one_time", False)

    if state_name == CreateReminder.title.state:
        await message.answer("Напиши, из-за чего тебя заебывать.", reply_markup=back_kb())
        return

    if state_name == CreateReminder.target_date.state:
        if is_one_time:
            await message.answer(
                "На какую дату поставить заеб?\n\n"
                "Пиши в таком формате: 020726\n\n"
                "Это значит:\n"
                "02.07.2026",
                reply_markup=back_kb(),
            )
        else:
            await message.answer(
                "До какой даты заебывать?\n\n"
                "Пиши в таком формате: 020726\n\n"
                "Это значит:\n"
                "02.07.2026",
                reply_markup=back_kb(),
            )
        return

    if state_name == CreateReminder.ask_exact_end.state:
        await message.answer("Хочешь настроить точное время конца?", reply_markup=yes_no_back_kb())
        return

    if state_name == CreateReminder.exact_end_time.state:
        await message.answer(
            "Введи точное время конца.\n\n"
            "Пиши так: 1400\n\n"
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
            "Введи время, в которое тебя заёбывать.\n\n"
            "Пиши так: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n"
            "14:00\n"
            "или 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    if state_name == CreateReminder.periodic_range.state:
        await message.answer(
            "Введи промежуток времени, в который тебя заебывать.\n\n"
            "Пиши так: 2300 2400\n\n"
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
        await message.answer("Что будем менять?", reply_markup=edit_field_kb(data["frequency_type"]))
        return

    if state_name == EditReminder.pick.state:
        reminders = data.get("pick_list", [])
        await message.answer("Выбери, какой заеб изменить.", reply_markup=select_reminder_kb(reminders))
        return

    if state_name == EditReminder.field.state:
        await message.answer("Что будем менять?", reply_markup=edit_field_kb(data["frequency_type"]))
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

    if state_name == EditReminder.ask_exact_end.state:
        await message.answer("Хочешь настроить точное время конца?", reply_markup=yes_no_back_kb())
        return

    if state_name == EditReminder.exact_end_time.state:
        await message.answer(
            "Введи точное время конца.\n\n"
            "Пиши так: 1400\n\n"
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
            "Введи время, в которое тебя заёбывать.\n\n"
            "Пиши так: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n"
            "14:00\n"
            "или 14:00 16:00 21:30",
            reply_markup=back_kb(),
        )
        return

    if state_name == EditReminder.periodic_range.state:
        await message.answer(
            "Введи промежуток времени, в который тебя заебывать.\n\n"
            "Пиши так: 2300 2400\n\n"
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
        await message.answer("Выбери, какой заеб удалить.", reply_markup=select_reminder_kb(reminders, with_delete_all=True))
        return

    if state_name == DeleteReminder.confirm.state:
        if data.get("delete_all"):
            await message.answer("Точно удалить вообще всё?", reply_markup=delete_confirm_kb())
            return

        rem = data["delete_reminder"]
        await message.answer(
            "Точно удалить этот заеб?\n\n"
            f'Повод: {rem["title"]}\n'
            f'До: {format_date_ru(rem["target_date"])}\n'
            f"Расписание: {reminder_brief(rem)}",
            reply_markup=delete_confirm_kb(),
        )
        return


# =========================
# БОТ
# =========================
if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН":
    raise ValueError("Вставь BOT_TOKEN в код")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =========================
# ОБЩИЕ ХЕЛПЕРЫ
# =========================
async def start_create_common(message: Message, state: FSMContext, one_time: bool = False):
    user = get_user(message.from_user.id)

    if not user or not user.get("timezone"):
        await state.clear()
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    if get_active_count(message.from_user.id) >= MAX_REMINDERS:
        await open_main_menu_message(message, f"У тебя уже {MAX_REMINDERS} заебов.\nСначала удали один.")
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(
        _history=[],
        _weekdays_msg_id=None,
        _create_edit_mode=False,
        _create_edit_target=None,
        _is_one_time=one_time,
        frequency_type="once" if one_time else None,
        weekdays_text=None,
        weekdays_selected=[],
        exact_end_time=None,
    )
    await state.set_state(CreateReminder.title)
    await render_step(message, state, CreateReminder.title.state)


async def show_reminders_common(message: Message, state: FSMContext):
    await state.clear()
    await reset_misclick(state)

    user = get_user(message.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await open_main_menu_message(message, "Пока заебывать нечем.")
        return

    await message.answer(list_numbered_reminders(reminders, user["timezone"]), reply_markup=ReplyKeyboardRemove())
    await message.answer("Что дальше?", reply_markup=show_list_kb())


async def edit_start_common(message: Message, state: FSMContext):
    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await state.clear()
        await open_main_menu_message(message, "Пока заебывать нечем.")
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(_history=[], pick_list=reminders, _weekdays_msg_id=None)
    await state.set_state(EditReminder.pick)
    await render_step(message, state, EditReminder.pick.state)


async def delete_start_common(message: Message, state: FSMContext):
    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await state.clear()
        await open_main_menu_message(message, "Пока заебывать нечем.")
        return

    await state.clear()
    await reset_misclick(state)
    await state.update_data(_history=[], pick_list=reminders)
    await state.set_state(DeleteReminder.pick)
    await render_step(message, state, DeleteReminder.pick.state)


# =========================
# ГЛАВНОЕ МЕНЮ INLINE
# =========================
@dp.callback_query(F.data == "menu:main")
async def menu_main_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await clear_weekdays_picker(state, callback.message.chat.id)
    await open_main_menu_message(callback.message, "Главное меню.")


@dp.callback_query(F.data == "menu:create")
async def menu_create_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_create_common(callback.message, state, one_time=False)


@dp.callback_query(F.data == "menu:create_once")
async def menu_create_once_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_create_common(callback.message, state, one_time=True)


@dp.callback_query(F.data == "menu:show")
async def menu_show_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_reminders_common(callback.message, state)


@dp.callback_query(F.data == "menu:edit")
async def menu_edit_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await edit_start_common(callback.message, state)


@dp.callback_query(F.data == "menu:delete")
async def menu_delete_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await delete_start_common(callback.message, state)


@dp.callback_query(F.data == "menu:country")
async def menu_country_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await reset_misclick(state)
    await state.set_state(CountrySetup.choose)
    await render_country_step(callback.message)


# =========================
# СТРАНА
# =========================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
    user = get_user(message.from_user.id)

    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    await open_main_menu_message(message)


@dp.message(CountrySetup.choose, F.text == "Украина")
async def country_ua(message: Message, state: FSMContext):
    set_user_country_timezone(message.from_user.id, "Украина", "Europe/Kyiv")
    await state.clear()
    await open_main_menu_message(message, "Красава. Страну сохранил.")


@dp.message(CountrySetup.choose, F.text == "Чехия")
async def country_cz(message: Message, state: FSMContext):
    set_user_country_timezone(message.from_user.id, "Чехия", "Europe/Prague")
    await state.clear()
    await open_main_menu_message(message, "Красава. Страну сохранил.")


@dp.message(CountrySetup.choose)
async def country_choose_wrong(message: Message, state: FSMContext):
    await message.answer("Чел, используй кнопки пожалуйста.", reply_markup=country_kb())


# =========================
# ДНИ НЕДЕЛИ
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
        await open_main_menu_message(message, "Ты уже в меню.")
        return

    prev = await pop_history(state)
    if not prev:
        await state.clear()
        await clear_weekdays_picker(state, message.chat.id)
        user = get_user(message.from_user.id)
        if not user or not user.get("timezone"):
            await state.set_state(CountrySetup.choose)
            await render_country_step(message)
            return
        await open_main_menu_message(message, "Окей, возвращаю в меню.")
        return

    state_map = {
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
        EditReminder.pick.state: EditReminder.pick,
        EditReminder.field.state: EditReminder.field,
        EditReminder.title.state: EditReminder.title,
        EditReminder.target_date.state: EditReminder.target_date,
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
# ПОКАЗАТЬ
# =========================
@dp.message(F.text == "Мои заебы")
async def show_reminders(message: Message, state: FSMContext):
    await show_reminders_common(message, state)


# =========================
# КНОПОЧНЫЕ СОСТОЯНИЯ
# =========================
@dp.message(CreateReminder.ask_exact_end)
@dp.message(CreateReminder.frequency)
@dp.message(CreateReminder.mode)
@dp.message(CreateReminder.periodic_interval)
@dp.message(CreateReminder.confirm)
@dp.message(CreateReminder.edit_field)
@dp.message(EditReminder.field)
@dp.message(EditReminder.ask_exact_end)
@dp.message(EditReminder.frequency)
@dp.message(EditReminder.mode)
@dp.message(EditReminder.periodic_interval)
@dp.message(EditReminder.confirm)
@dp.message(DeleteReminder.confirm)
async def buttons_only_states(message: Message, state: FSMContext):
    await message.answer("Чел, используй кнопки пожалуйста.")


@dp.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    user = get_user(message.from_user.id)
    if not user or not user.get("timezone"):
        await state.set_state(CountrySetup.choose)
        await render_country_step(message)
        return

    await open_main_menu_message(message, "Главное меню.")


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

                if user_now > finish_dt:
                    deactivate_reminder(rem["id"])
                    continue

                need_send = False
                current_hhmm = user_now.hour * 60 + user_now.minute
                current_text = f"{user_now.hour:02d}:{user_now.minute:02d}"
                weekday = user_now.weekday()
                today = user_now.date()
                target_day = date.fromisoformat(rem["target_date"])

                if rem["frequency_type"] == "daily":
                    if rem["mode_type"] == "fixed":
                        times = rem["times_text"].split(",") if rem["times_text"] else []
                        if current_text in times:
                            need_send = True
                    elif rem["mode_type"] == "periodic" and rem["start_time"] and rem["end_time"] and rem["interval_minutes"]:
                        start_minutes = parse_minutes_hhmm(rem["start_time"])
                        end_minutes = parse_minutes_hhmm(rem["end_time"])
                        if start_minutes <= current_hhmm < end_minutes:
                            if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                need_send = True

                elif rem["frequency_type"] == "weekdays":
                    days = [int(x) for x in rem["weekdays_text"].split(",")] if rem["weekdays_text"] else []
                    if weekday in days:
                        if rem["mode_type"] == "fixed":
                            times = rem["times_text"].split(",") if rem["times_text"] else []
                            if current_text in times:
                                need_send = True
                        elif rem["mode_type"] == "periodic" and rem["start_time"] and rem["end_time"] and rem["interval_minutes"]:
                            start_minutes = parse_minutes_hhmm(rem["start_time"])
                            end_minutes = parse_minutes_hhmm(rem["end_time"])
                            if start_minutes <= current_hhmm < end_minutes:
                                if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                    need_send = True

                elif rem["frequency_type"] == "once":
                    if today == target_day:
                        if rem["mode_type"] == "fixed":
                            times = rem["times_text"].split(",") if rem["times_text"] else []
                            if current_text in times:
                                need_send = True
                        elif rem["mode_type"] == "periodic" and rem["start_time"] and rem["end_time"] and rem["interval_minutes"]:
                            start_minutes = parse_minutes_hhmm(rem["start_time"])
                            end_minutes = parse_minutes_hhmm(rem["end_time"])
                            if start_minutes <= current_hhmm < end_minutes:
                                if (current_hhmm - start_minutes) % int(rem["interval_minutes"]) == 0:
                                    need_send = True

                if not need_send:
                    continue

                sent_key = f'{user_now.strftime("%Y-%m-%d %H:%M")}::{rem["id"]}'
                if was_sent(rem["id"], sent_key):
                    continue

                try:
                    await bot.send_message(
                        rem["user_id"],
                        reminder_text(rem, timezone_name),
                        reply_markup=main_menu(),
                    )
                    mark_sent(rem["id"], sent_key)
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