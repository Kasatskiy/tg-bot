import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any, Tuple

import aiosqlite
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties


# =========================
# CONFIG
# =========================

TOKEN = "8710757819:AAFra83pBHkxPT9m6BYJRY9kEh8Akry39gI"
DB_PATH = "bot.db"

MAX_REMINDERS = 10
MAX_TITLE_LEN = 64
MAX_TIMES = 10

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


# =========================
# DB
# =========================

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    country TEXT,
    timezone TEXT,
    notification_hint_sent INTEGER DEFAULT 0
)
"""

CREATE_REMINDERS = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    target_date TEXT NOT NULL,          -- YYYY-MM-DD
    exact_end_time TEXT,                -- HH:MM or NULL
    frequency_type TEXT NOT NULL,       -- daily / weekdays / once
    mode_type TEXT NOT NULL,            -- fixed / periodic
    times_text TEXT,                    -- HH:MM HH:MM ...
    weekdays_text TEXT,                 -- 0 1 2 ...
    start_time TEXT,                    -- HH:MM
    end_time TEXT,                      -- HH:MM
    interval_minutes INTEGER,
    active INTEGER DEFAULT 1
)
"""

CREATE_SENT_LOG = """
CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    sent_key TEXT NOT NULL,
    UNIQUE(reminder_id, sent_key)
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_REMINDERS)
        await db.execute(CREATE_SENT_LOG)
        await db.commit()


async def db_fetchone(query: str, params: tuple = ()) -> Optional[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        await cur.close()
        return row


async def db_fetchall(query: str, params: tuple = ()) -> List[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows


async def db_execute(query: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


# =========================
# MODELS / UTILS
# =========================

WEEKDAY_NAMES = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Вс",
}

COUNTRIES = {
    "Украина": "Europe/Kyiv",
    "Чехия": "Europe/Prague",
}

BUTTON_ONLY_STATES = {
    "Flow.choose_country",
    "Flow.ask_exact_end_yes_no",
    "Flow.ask_frequency",
    "Flow.ask_mode",
    "Flow.ask_interval_preset",
    "Flow.confirm_save",
    "Flow.edit_choose_field",
    "Flow.edit_select_reminder",
    "Flow.delete_select_reminder",
    "Flow.delete_confirm_one",
    "Flow.delete_confirm_all",
}

WEEKDAY_INLINE_STATE = "Flow.ask_weekdays"
READY_BACK_STATE = "Flow.ask_weekdays_ready"

YES_NO_KB = ["Да", "Нет", "Назад"]


@dataclass
class Reminder:
    id: Optional[int]
    user_id: int
    title: str
    target_date: str
    exact_end_time: Optional[str]
    frequency_type: str
    mode_type: str
    times_text: Optional[str]
    weekdays_text: Optional[str]
    start_time: Optional[str]
    end_time: Optional[str]
    interval_minutes: Optional[int]
    active: int = 1

    @classmethod
    def from_row(cls, row: tuple) -> "Reminder":
        return cls(
            id=row[0],
            user_id=row[1],
            title=row[2],
            target_date=row[3],
            exact_end_time=row[4],
            frequency_type=row[5],
            mode_type=row[6],
            times_text=row[7],
            weekdays_text=row[8],
            start_time=row[9],
            end_time=row[10],
            interval_minutes=row[11],
            active=row[12],
        )


def today_in_tz(tz_name: str) -> date:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).date()


def now_in_tz(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz)


def parse_ddmmyy(text: str) -> Optional[date]:
    if not re.fullmatch(r"\d{6}", text or ""):
        return None
    try:
        return datetime.strptime(text, "%d%m%y").date()
    except ValueError:
        return None


def parse_hhmm(text: str, allow_2400: bool = False) -> Optional[str]:
    if not re.fullmatch(r"\d{4}", text or ""):
        return None
    hh = int(text[:2])
    mm = int(text[2:])
    if allow_2400 and hh == 24 and mm == 0:
        return "24:00"
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None


def hhmm_to_minutes(hhmm: str) -> int:
    if hhmm == "24:00":
        return 24 * 60
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def minutes_to_hhmm(value: int) -> str:
    hh = value // 60
    mm = value % 60
    return f"{hh:02d}:{mm:02d}"


def parse_multiple_times(text: str) -> Optional[List[str]]:
    parts = (text or "").strip().split()
    if not parts or len(parts) > MAX_TIMES:
        return None

    times = []
    for part in parts:
        t = parse_hhmm(part, allow_2400=False)
        if not t:
            return None
        times.append(t)

    if len(set(times)) != len(times):
        return None

    return sorted(times, key=hhmm_to_minutes)


def parse_range(text: str) -> Optional[Tuple[str, str]]:
    parts = (text or "").strip().split()
    if len(parts) != 2:
        return None
    start = parse_hhmm(parts[0], allow_2400=False)
    end = parse_hhmm(parts[1], allow_2400=True)
    if not start or not end:
        return None
    if hhmm_to_minutes(end) <= hhmm_to_minutes(start):
        return None
    return start, end


def format_date_ru(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def format_target_date(iso_date: str) -> str:
    y, m, d = map(int, iso_date.split("-"))
    return f"{d:02d}.{m:02d}.{y}"


def weekdays_text_to_list(text: Optional[str]) -> List[int]:
    if not text:
        return []
    return sorted(int(x) for x in text.split())


def weekdays_list_to_text(days: List[int]) -> str:
    return " ".join(str(x) for x in sorted(days))


def format_weekdays(days: List[int]) -> str:
    if not days:
        return "—"
    return ", ".join(WEEKDAY_NAMES[d] for d in sorted(days))


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сделать новый заеб", callback_data="menu:new")],
        [InlineKeyboardButton(text="Одноразовый заеб", callback_data="menu:once")],
        [InlineKeyboardButton(text="Мои заебы", callback_data="menu:list")],
        [InlineKeyboardButton(text="Заебал", callback_data="menu:delete")],
        [InlineKeyboardButton(text="Изменить страну", callback_data="menu:country")],
    ])


def country_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Украина")],
            [KeyboardButton(text="Чехия")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def yes_no_back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да"), KeyboardButton(text="Нет")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def frequency_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Каждый день")],
            [KeyboardButton(text="В определенные дни недели")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def weekdays_ready_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Готово")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def mode_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="В конкретное время дня")],
            [KeyboardButton(text="В промежутке")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def interval_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Каждую 1 минуту"), KeyboardButton(text="Каждые 5 минут")],
            [KeyboardButton(text="Каждые 30 минут"), KeyboardButton(text="Каждые 60 минут")],
            [KeyboardButton(text="Свой интервал")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сохранить"), KeyboardButton(text="Изменить")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def edit_fields_kb(rem: Dict[str, Any]) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Повод")],
        [KeyboardButton(text="Дату")],
    ]
    if rem["frequency_type"] != "once":
        rows.append([KeyboardButton(text="Точное время конца")])
        rows.append([KeyboardButton(text="Тип заеба")])
        if rem["frequency_type"] == "weekdays":
            rows.append([KeyboardButton(text="Дни недели")])
    rows.append([KeyboardButton(text="Время")])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def single_back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True,
    )


def delete_confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, заебал")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def reminders_select_kb(rows: List[tuple], include_delete_all: bool = False) -> ReplyKeyboardMarkup:
    keyboard = []
    for idx, row in enumerate(rows, start=1):
        title = row[2]
        short_title = title if len(title) <= 32 else title[:29] + "..."
        keyboard.append([KeyboardButton(text=f"{idx}. {short_title}")])
    if include_delete_all:
        keyboard.append([KeyboardButton(text="Заебал, удалить всё")])
    keyboard.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def my_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить заеб", callback_data="list:edit")],
        [InlineKeyboardButton(text="Меню", callback_data="list:menu")],
    ])


def weekdays_inline_kb(selected: List[int]) -> InlineKeyboardMarkup:
    row1 = []
    row2 = []
    for d in range(7):
        title = WEEKDAY_NAMES[d]
        if d in selected:
            title = f"✅ {title}"
        btn = InlineKeyboardButton(text=title, callback_data=f"weekday:{d}")
        if d < 4:
            row1.append(btn)
        else:
            row2.append(btn)
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


async def get_user(user_id: int) -> Optional[tuple]:
    return await db_fetchone("SELECT user_id, country, timezone, notification_hint_sent FROM users WHERE user_id=?", (user_id,))


async def upsert_user(user_id: int, country: str, timezone: str) -> None:
    old = await get_user(user_id)
    hint = old[3] if old else 0
    await db_execute("""
        INSERT INTO users (user_id, country, timezone, notification_hint_sent)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            country=excluded.country,
            timezone=excluded.timezone
    """, (user_id, country, timezone, hint))


async def count_active_reminders(user_id: int) -> int:
    row = await db_fetchone("SELECT COUNT(*) FROM reminders WHERE user_id=? AND active=1", (user_id,))
    return row[0] if row else 0


async def get_active_reminders(user_id: int) -> List[Reminder]:
    rows = await db_fetchall("""
        SELECT id, user_id, title, target_date, exact_end_time, frequency_type, mode_type,
               times_text, weekdays_text, start_time, end_time, interval_minutes, active
        FROM reminders
        WHERE user_id=? AND active=1
        ORDER BY id
    """, (user_id,))
    return [Reminder.from_row(r) for r in rows]


async def get_reminder(reminder_id: int) -> Optional[Reminder]:
    row = await db_fetchone("""
        SELECT id, user_id, title, target_date, exact_end_time, frequency_type, mode_type,
               times_text, weekdays_text, start_time, end_time, interval_minutes, active
        FROM reminders WHERE id=?
    """, (reminder_id,))
    return Reminder.from_row(row) if row else None


async def insert_reminder(data: Dict[str, Any]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO reminders (
                user_id, title, target_date, exact_end_time, frequency_type, mode_type,
                times_text, weekdays_text, start_time, end_time, interval_minutes, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            data["user_id"],
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
        ))
        await db.commit()
        return cur.lastrowid


async def update_reminder(reminder_id: int, data: Dict[str, Any]) -> None:
    await db_execute("""
        UPDATE reminders
        SET title=?, target_date=?, exact_end_time=?, frequency_type=?, mode_type=?,
            times_text=?, weekdays_text=?, start_time=?, end_time=?, interval_minutes=?, active=1
        WHERE id=?
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
    ))


async def deactivate_reminder(reminder_id: int) -> None:
    await db_execute("UPDATE reminders SET active=0 WHERE id=?", (reminder_id,))


async def deactivate_all_user_reminders(user_id: int) -> None:
    await db_execute("UPDATE reminders SET active=0 WHERE user_id=? AND active=1", (user_id,))


async def has_sent(reminder_id: int, sent_key: str) -> bool:
    row = await db_fetchone("SELECT 1 FROM sent_log WHERE reminder_id=? AND sent_key=?", (reminder_id, sent_key))
    return row is not None


async def mark_sent(reminder_id: int, sent_key: str) -> None:
    try:
        await db_execute("INSERT INTO sent_log(reminder_id, sent_key) VALUES(?, ?)", (reminder_id, sent_key))
    except Exception:
        pass


# =========================
# FSM
# =========================

class Flow(StatesGroup):
    choose_country = State()

    ask_title = State()
    ask_date = State()
    ask_exact_end_yes_no = State()
    ask_exact_end_time = State()
    ask_frequency = State()
    ask_weekdays = State()
    ask_mode = State()
    ask_fixed_times = State()
    ask_periodic_range = State()
    ask_interval_preset = State()
    ask_custom_interval = State()
    confirm_save = State()

    edit_select_reminder = State()
    edit_choose_field = State()

    delete_select_reminder = State()
    delete_confirm_one = State()
    delete_confirm_all = State()


# =========================
# FSM HELPERS
# =========================

async def push_history(state: FSMContext, current_state: str) -> None:
    data = await state.get_data()
    history = data.get("history", [])
    history.append(current_state)
    await state.update_data(history=history)


async def back_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    history = data.get("history", [])
    user = await get_user(message.from_user.id)

    if not history:
        await state.clear()
        if not user or not user[2]:
            await ask_country(message, state)
        else:
            await send_main_menu(message)
        return

    prev = history.pop()
    await state.update_data(history=history)
    await state.set_state(prev)

    if prev == Flow.ask_title.state:
        await message.answer("Напиши, из-за чего тебя заебывать.", reply_markup=single_back_kb())
    elif prev == Flow.ask_date.state:
        draft = (await state.get_data()).get("draft", {})
        if draft.get("frequency_type") == "once":
            await message.answer(
                "В какую дату заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
                reply_markup=single_back_kb()
            )
        else:
            await message.answer(
                "До какой даты заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
                reply_markup=single_back_kb()
            )
    elif prev == Flow.ask_exact_end_yes_no.state:
        await message.answer("Хочешь настроить точное время конца?", reply_markup=yes_no_back_kb())
    elif prev == Flow.ask_exact_end_time.state:
        await message.answer("Введи точное время конца.\n\nПиши так: 1400\n\nЭто значит:\n14:00", reply_markup=single_back_kb())
    elif prev == Flow.ask_frequency.state:
        await message.answer("Как часто заебывать?", reply_markup=frequency_kb())
    elif prev == Flow.ask_weekdays.state:
        st = await state.get_data()
        draft = st.get("draft", {})
        selected = draft.get("weekdays", [])
        await message.answer(
            f"Выбери дни, в которые тебя заебывать.\nВыбрано: {format_weekdays(selected)}",
            reply_markup=weekdays_ready_kb()
        )
        await message.answer("Дни недели:", reply_markup=weekdays_inline_kb(selected))
    elif prev == Flow.ask_mode.state:
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
    elif prev == Flow.ask_fixed_times.state:
        await message.answer(
            "Введи время, в которое тебя заёбывать.\n\n"
            "Пиши так: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n14:00\nили 14:00 16:00 21:30",
            reply_markup=single_back_kb()
        )
    elif prev == Flow.ask_periodic_range.state:
        await message.answer(
            "Введи промежуток времени, в который тебя заебывать.\n\n"
            "Пиши так: 2300 2400\n\n"
            "Это значит:\nс 23:00 до 24:00",
            reply_markup=single_back_kb()
        )
    elif prev == Flow.ask_interval_preset.state:
        await message.answer("Как часто заебывать в этом промежутке?", reply_markup=interval_kb())
    elif prev == Flow.ask_custom_interval.state:
        await message.answer("Введи интервал в минутах.\nМинимум 1.", reply_markup=single_back_kb())
    elif prev == Flow.confirm_save.state:
        draft = (await state.get_data()).get("draft", {})
        await message.answer(build_summary_text(draft) + "\n\nСохраняем?", reply_markup=confirm_kb())
    elif prev == Flow.edit_choose_field.state:
        st = await state.get_data()
        await message.answer("Что будем менять?", reply_markup=edit_fields_kb(st["draft"]))
    elif prev == Flow.edit_select_reminder.state:
        reminders = await get_active_reminders(message.from_user.id)
        if not reminders:
            await state.clear()
            await message.answer("Пока заебывать нечем.", reply_markup=ReplyKeyboardRemove())
            await send_main_menu(message)
            return
        rows = [(r.id, r.user_id, r.title) for r in reminders]
        await message.answer("Выбери заеб для редактирования.", reply_markup=reminders_select_kb(rows))
    elif prev == Flow.delete_select_reminder.state:
        reminders = await get_active_reminders(message.from_user.id)
        if not reminders:
            await state.clear()
            await message.answer("Пока заебывать нечем.", reply_markup=ReplyKeyboardRemove())
            await send_main_menu(message)
            return
        rows = [(r.id, r.user_id, r.title) for r in reminders]
        await message.answer("Что удалить?", reply_markup=reminders_select_kb(rows, include_delete_all=True))
    elif prev == Flow.delete_confirm_one.state:
        st = await state.get_data()
        rem = st["draft"]
        txt = (
            f"Повод: {rem['title']}\n"
            f"Дата: {format_target_date(rem['target_date'])}\n"
            f"Расписание: {short_schedule(rem)}"
        )
        await message.answer(txt, reply_markup=delete_confirm_kb())
    elif prev == Flow.delete_confirm_all.state:
        await message.answer("Точно удалить вообще всё?", reply_markup=delete_confirm_kb())


async def ask_country(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Flow.choose_country)
    await message.answer(
        "Где ты живешь.\n\nЭто нужно для определения твоего часового пояса.",
        reply_markup=country_kb()
    )


async def send_main_menu(message: Message) -> None:
    await message.answer(
        "Привет.\nЯ буду заебывать тебя до нужной даты.\n\nВыбирай:",
        reply_markup=main_menu_kb()
    )


def build_summary_text(rem: Dict[str, Any]) -> str:
    lines = []
    reminder_type = "одноразовый" if rem["frequency_type"] == "once" else "обычный"

    lines.append(f"Тип: {reminder_type}")
    lines.append(f"Повод: {rem['title']}")
    lines.append(f"Дата: {format_target_date(rem['target_date'])}")

    if rem["frequency_type"] != "once":
        if rem.get("exact_end_time"):
            lines.append(f"Точный конец: {rem['exact_end_time']}")
        else:
            lines.append("Точный конец: нет")

    if rem["frequency_type"] == "daily":
        lines.append("Частота: каждый день")
    elif rem["frequency_type"] == "weekdays":
        lines.append("Частота: в определенные дни недели")
        lines.append(f"Дни недели: {format_weekdays(rem.get('weekdays', []))}")

    if rem["mode_type"] == "fixed":
        lines.append("Режим: в конкретное время дня")
        lines.append(f"Время: {' '.join(rem.get('times', []))}")
    else:
        lines.append("Режим: в промежутке")
        lines.append(f"Промежуток: {rem['start_time']} — {rem['end_time']}")
        lines.append(f"Интервал: {rem['interval_minutes']} мин.")

    return "\n".join(lines)


def short_schedule(rem: Dict[str, Any]) -> str:
    if rem["mode_type"] == "fixed":
        base = ", ".join(rem.get("times", rem.get("times_text", "").split()))
    else:
        base = f"{rem['start_time']}-{rem['end_time']} / {rem['interval_minutes']} мин"

    if rem["frequency_type"] == "daily":
        return f"каждый день / {base}"
    if rem["frequency_type"] == "weekdays":
        days = rem.get("weekdays", weekdays_text_to_list(rem.get("weekdays_text")))
        return f"{format_weekdays(days)} / {base}"
    return f"один день / {base}"


# =========================
# REMINDER TIME LOGIC
# =========================

def reminder_finish_dt(rem: Reminder, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    y, m, d = map(int, rem.target_date.split("-"))

    if rem.frequency_type == "once":
        if rem.mode_type == "fixed":
            times = rem.times_text.split()
            end_t = max(times, key=hhmm_to_minutes)
        else:
            end_t = rem.end_time
    else:
        end_t = rem.exact_end_time if rem.exact_end_time else "23:59"

    if end_t == "24:00":
        naive = datetime(y, m, d, 0, 0) + timedelta(days=1)
    else:
        hh, mm = map(int, end_t.split(":"))
        naive = datetime(y, m, d, hh, mm)

    return tz.localize(naive)


def reminder_is_finished(rem: Reminder, tz_name: str, dt_local: Optional[datetime] = None) -> bool:
    dt_local = dt_local or now_in_tz(tz_name)
    return dt_local > reminder_finish_dt(rem, tz_name)


def reminder_is_active_on_date(rem: Reminder, local_date: date) -> bool:
    target = datetime.strptime(rem.target_date, "%Y-%m-%d").date()

    if rem.frequency_type == "once":
        return local_date == target

    if local_date > target:
        return False

    if rem.frequency_type == "daily":
        return True

    if rem.frequency_type == "weekdays":
        days = weekdays_text_to_list(rem.weekdays_text)
        return local_date.weekday() in days

    return False


def reminder_times_for_date(rem: Reminder, local_date: date, tz_name: str) -> List[datetime]:
    tz = pytz.timezone(tz_name)
    if not reminder_is_active_on_date(rem, local_date):
        return []

    y, m, d = local_date.year, local_date.month, local_date.day
    result = []

    if rem.mode_type == "fixed":
        for t in rem.times_text.split():
            hh, mm = map(int, t.split(":"))
            result.append(tz.localize(datetime(y, m, d, hh, mm)))
    else:
        start_min = hhmm_to_minutes(rem.start_time)
        end_min = hhmm_to_minutes(rem.end_time)
        step = rem.interval_minutes
        cur = start_min
        while cur < end_min:
            hh = cur // 60
            mm = cur % 60
            result.append(tz.localize(datetime(y, m, d, hh, mm)))
            cur += step

    finish = reminder_finish_dt(rem, tz_name)
    return [x for x in result if x <= finish]


def next_notification_dt(rem: Reminder, tz_name: str, from_dt: Optional[datetime] = None) -> Optional[datetime]:
    from_dt = from_dt or now_in_tz(tz_name)
    target = datetime.strptime(rem.target_date, "%Y-%m-%d").date()

    for i in range(0, 400):
        day = from_dt.date() + timedelta(days=i)
        if rem.frequency_type != "once" and day > target:
            return None
        if rem.frequency_type == "once" and day != target and day > target:
            return None

        times = reminder_times_for_date(rem, day, tz_name)
        for dt_ in times:
            if dt_ >= from_dt:
                return dt_

    return None


def human_next_notification(rem: Reminder, tz_name: str) -> str:
    now_local = now_in_tz(tz_name)
    if reminder_is_finished(rem, tz_name, now_local):
        return "завершен"

    nxt = next_notification_dt(rem, tz_name, now_local.replace(second=0, microsecond=0))
    if not nxt:
        return "завершен"

    if nxt.date() == now_local.date():
        return f"сегодня в {nxt.strftime('%H:%M')}"
    if nxt.date() == now_local.date() + timedelta(days=1):
        return f"завтра в {nxt.strftime('%H:%M')}"
    return nxt.strftime("%d.%m.%Y в %H:%M")


def reminder_left_text(rem: Reminder, tz_name: str, dt_local: datetime) -> str:
    finish_dt = reminder_finish_dt(rem, tz_name)
    delta = finish_dt - dt_local
    if delta.total_seconds() < 0:
        delta = timedelta(0)

    total_minutes = int(delta.total_seconds() // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60

    if rem.frequency_type == "once" or rem.exact_end_time:
        if dt_local.date() == finish_dt.date():
            return f"Сегодня {rem.title}"
        return f"До {rem.title} осталось {days} дн. {hours} ч. {minutes} мин."
    else:
        target = datetime.strptime(rem.target_date, "%Y-%m-%d").date()
        days_left = (target - dt_local.date()).days
        if days_left <= 0:
            return f"Сегодня {rem.title}"
        return f"До {rem.title} осталось {days_left} дней"


def should_send_now(rem: Reminder, tz_name: str, dt_local: datetime) -> bool:
    if reminder_is_finished(rem, tz_name, dt_local):
        return False
    if not reminder_is_active_on_date(rem, dt_local.date()):
        return False

    minute_key = dt_local.strftime("%Y-%m-%d %H:%M")
    times = reminder_times_for_date(rem, dt_local.date(), tz_name)
    return any(t.strftime("%Y-%m-%d %H:%M") == minute_key for t in times)


# =========================
# PARSE / DRAFT HELPERS
# =========================

def reminder_to_draft(rem: Reminder) -> Dict[str, Any]:
    return {
        "title": rem.title,
        "target_date": rem.target_date,
        "exact_end_time": rem.exact_end_time,
        "frequency_type": rem.frequency_type,
        "mode_type": rem.mode_type,
        "times": rem.times_text.split() if rem.times_text else [],
        "weekdays": weekdays_text_to_list(rem.weekdays_text),
        "start_time": rem.start_time,
        "end_time": rem.end_time,
        "interval_minutes": rem.interval_minutes,
    }


def draft_to_db(user_id: int, draft: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "title": draft["title"],
        "target_date": draft["target_date"],
        "exact_end_time": draft.get("exact_end_time"),
        "frequency_type": draft["frequency_type"],
        "mode_type": draft["mode_type"],
        "times_text": " ".join(draft["times"]) if draft.get("times") else None,
        "weekdays_text": weekdays_list_to_text(draft.get("weekdays", [])) if draft.get("weekdays") else None,
        "start_time": draft.get("start_time"),
        "end_time": draft.get("end_time"),
        "interval_minutes": draft.get("interval_minutes"),
    }


async def show_summary(message: Message, state: FSMContext) -> None:
    st = await state.get_data()
    draft = st["draft"]
    await message.answer(build_summary_text(draft) + "\n\nСохраняем?", reply_markup=confirm_kb())
    await state.set_state(Flow.confirm_save)


async def begin_create(message: Message, state: FSMContext, frequency_type: str) -> None:
    if await count_active_reminders(message.from_user.id) >= MAX_REMINDERS:
        await message.answer(
            "У тебя уже 10 заебов.\nСначала удали один.",
            reply_markup=ReplyKeyboardRemove()
        )
        await send_main_menu(message)
        return

    await state.clear()
    await state.set_state(Flow.ask_title)
    await state.update_data(
        history=[],
        editing=False,
        editing_reminder_id=None,
        draft={
            "frequency_type": frequency_type,  # for once already known
        }
    )
    await message.answer("Напиши, из-за чего тебя заебывать.", reply_markup=single_back_kb())


# =========================
# MENU / START
# =========================

@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    await state.clear()

    if not user or not user[2]:
        await ask_country(message, state)
        return

    await message.answer("Привет.\nЯ буду заебывать тебя до нужной даты.\n\nВыбирай:", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "menu:new")
async def menu_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not user[2]:
        await ask_country(callback.message, state)
        return
    await begin_create(callback.message, state, frequency_type="normal")


@dp.callback_query(F.data == "menu:once")
async def menu_once(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not user[2]:
        await ask_country(callback.message, state)
        return
    await begin_create(callback.message, state, frequency_type="once")


@dp.callback_query(F.data == "menu:country")
async def menu_country(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await ask_country(callback.message, state)


@dp.callback_query(F.data == "menu:list")
async def menu_list(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_my_reminders(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "menu:delete")
async def menu_delete(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reminders = await get_active_reminders(callback.from_user.id)
    if not reminders:
        await callback.message.answer("Пока заебывать нечем.")
        await send_main_menu(callback.message)
        return

    await state.clear()
    await state.set_state(Flow.delete_select_reminder)
    await state.update_data(history=[])
    rows = [(r.id, r.user_id, r.title) for r in reminders]
    await callback.message.answer("Что удалить?", reply_markup=reminders_select_kb(rows, include_delete_all=True))


@dp.callback_query(F.data == "list:menu")
async def list_back_menu(callback: CallbackQuery):
    await callback.answer()
    await send_main_menu(callback.message)


@dp.callback_query(F.data == "list:edit")
async def list_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    reminders = await get_active_reminders(callback.from_user.id)
    if not reminders:
        await callback.message.answer("Пока заебывать нечем.")
        await send_main_menu(callback.message)
        return

    await state.clear()
    await state.set_state(Flow.edit_select_reminder)
    await state.update_data(history=[])
    rows = [(r.id, r.user_id, r.title) for r in reminders]
    await callback.message.answer("Выбери заеб для редактирования.", reply_markup=reminders_select_kb(rows))


# =========================
# COUNTRY
# =========================

@dp.message(Flow.choose_country)
async def choose_country_handler(message: Message, state: FSMContext):
    if message.text not in COUNTRIES:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    country = message.text
    tz_name = COUNTRIES[country]
    await upsert_user(message.from_user.id, country, tz_name)
    await state.clear()

    await message.answer("Ок.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message)


# =========================
# CREATE / EDIT FLOW
# =========================

@dp.message(Flow.ask_title)
async def ask_title_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Хуйня. Введи повод нормально.")
        return
    if len(text) > MAX_TITLE_LEN:
        await message.answer(f"Слишком длинно. Максимум {MAX_TITLE_LEN} символа.")
        return

    st = await state.get_data()
    draft = st.get("draft", {})
    draft["title"] = text
    await state.update_data(draft=draft)

    await push_history(state, Flow.ask_title.state)
    await state.set_state(Flow.ask_date)

    if draft["frequency_type"] == "once":
        await message.answer(
            "В какую дату заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
            reply_markup=single_back_kb()
        )
    else:
        await message.answer(
            "До какой даты заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
            reply_markup=single_back_kb()
        )


@dp.message(Flow.ask_date)
async def ask_date_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    d = parse_ddmmyy((message.text or "").strip())
    if not d:
        await message.answer("Хуйня. Напиши нормально.")
        return

    user = await get_user(message.from_user.id)
    tz_name = user[2]
    if d < today_in_tz(tz_name):
        await message.answer("Дурак совсем? Это ведь прошлое.")
        return

    st = await state.get_data()
    draft = st["draft"]
    draft["target_date"] = d.isoformat()
    await state.update_data(draft=draft)

    if draft["frequency_type"] == "once":
        await push_history(state, Flow.ask_date.state)
        await state.set_state(Flow.ask_mode)
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
        return

    await push_history(state, Flow.ask_date.state)
    await state.set_state(Flow.ask_exact_end_yes_no)
    await message.answer("Хочешь настроить точное время конца?", reply_markup=yes_no_back_kb())


@dp.message(Flow.ask_exact_end_yes_no)
async def ask_exact_end_yes_no_handler(message: Message, state: FSMContext):
    if message.text not in {"Да", "Нет", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    draft = st["draft"]

    if message.text == "Да":
        await push_history(state, Flow.ask_exact_end_yes_no.state)
        await state.set_state(Flow.ask_exact_end_time)
        await message.answer(
            "Введи точное время конца.\n\nПиши так: 1400\n\nЭто значит:\n14:00",
            reply_markup=single_back_kb()
        )
        return

    draft["exact_end_time"] = None
    await state.update_data(draft=draft)
    await push_history(state, Flow.ask_exact_end_yes_no.state)
    await state.set_state(Flow.ask_frequency)
    await message.answer("Как часто заебывать?", reply_markup=frequency_kb())


@dp.message(Flow.ask_exact_end_time)
async def ask_exact_end_time_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    t = parse_hhmm((message.text or "").strip(), allow_2400=True)
    if not t:
        await message.answer("Хуйня. Напиши нормально.")
        return

    st = await state.get_data()
    draft = st["draft"]
    draft["exact_end_time"] = t
    await state.update_data(draft=draft)

    await push_history(state, Flow.ask_exact_end_time.state)
    await state.set_state(Flow.ask_frequency)
    await message.answer("Как часто заебывать?", reply_markup=frequency_kb())


@dp.message(Flow.ask_frequency)
async def ask_frequency_handler(message: Message, state: FSMContext):
    if message.text not in {"Каждый день", "В определенные дни недели", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    draft = st["draft"]

    if message.text == "Каждый день":
        draft["frequency_type"] = "daily"
        draft["weekdays"] = []
        await state.update_data(draft=draft)
        await push_history(state, Flow.ask_frequency.state)
        await state.set_state(Flow.ask_mode)
        await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())
        return

    draft["frequency_type"] = "weekdays"
    draft["weekdays"] = []
    await state.update_data(draft=draft)
    await push_history(state, Flow.ask_frequency.state)
    await state.set_state(Flow.ask_weekdays)
    await message.answer(
        "Выбери дни, в которые тебя заебывать.\nВыбрано: —",
        reply_markup=weekdays_ready_kb()
    )
    await message.answer("Дни недели:", reply_markup=weekdays_inline_kb([]))


@dp.callback_query(F.data.startswith("weekday:"))
async def weekday_toggle_handler(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != Flow.ask_weekdays.state:
        await callback.answer()
        return

    day = int(callback.data.split(":")[1])
    st = await state.get_data()
    draft = st["draft"]
    selected = set(draft.get("weekdays", []))

    if day in selected:
        selected.remove(day)
    else:
        selected.add(day)

    draft["weekdays"] = sorted(selected)
    await state.update_data(draft=draft)

    await callback.message.edit_reply_markup(reply_markup=weekdays_inline_kb(draft["weekdays"]))
    await callback.answer()


@dp.message(Flow.ask_weekdays)
async def ask_weekdays_handler(message: Message, state: FSMContext):
    if message.text not in {"Готово", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    draft = st["draft"]
    if not draft.get("weekdays"):
        await message.answer("Сначала выбери хотя бы один день.")
        return

    await push_history(state, Flow.ask_weekdays.state)
    await state.set_state(Flow.ask_mode)
    await message.answer("Как именно тебя заебывать?", reply_markup=mode_kb())


@dp.message(Flow.ask_mode)
async def ask_mode_handler(message: Message, state: FSMContext):
    if message.text not in {"В конкретное время дня", "В промежутке", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    draft = st["draft"]

    if message.text == "В конкретное время дня":
        draft["mode_type"] = "fixed"
        draft["start_time"] = None
        draft["end_time"] = None
        draft["interval_minutes"] = None
        await state.update_data(draft=draft)
        await push_history(state, Flow.ask_mode.state)
        await state.set_state(Flow.ask_fixed_times)
        await message.answer(
            "Введи время, в которое тебя заёбывать.\n\n"
            "Пиши так: 1400\n"
            "Если нужно несколько — так: 1400 1600 2130\n\n"
            "Это значит:\n14:00\nили 14:00 16:00 21:30",
            reply_markup=single_back_kb()
        )
    else:
        draft["mode_type"] = "periodic"
        draft["times"] = []
        await state.update_data(draft=draft)
        await push_history(state, Flow.ask_mode.state)
        await state.set_state(Flow.ask_periodic_range)
        await message.answer(
            "Введи промежуток времени, в который тебя заебывать.\n\n"
            "Пиши так: 2300 2400\n\n"
            "Это значит:\nс 23:00 до 24:00",
            reply_markup=single_back_kb()
        )


@dp.message(Flow.ask_fixed_times)
async def ask_fixed_times_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    times = parse_multiple_times((message.text or "").strip())
    if not times:
        await message.answer("Хуйня. Напиши нормально.")
        return

    st = await state.get_data()
    draft = st["draft"]
    draft["times"] = times
    await state.update_data(draft=draft)
    await show_summary(message, state)


@dp.message(Flow.ask_periodic_range)
async def ask_periodic_range_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    parsed = parse_range((message.text or "").strip())
    if not parsed:
        await message.answer("Хуйня. Напиши нормально.")
        return

    start_time, end_time = parsed
    st = await state.get_data()
    draft = st["draft"]
    draft["start_time"] = start_time
    draft["end_time"] = end_time
    await state.update_data(draft=draft)

    await push_history(state, Flow.ask_periodic_range.state)
    await state.set_state(Flow.ask_interval_preset)
    await message.answer("Как часто заебывать в этом промежутке?", reply_markup=interval_kb())


@dp.message(Flow.ask_interval_preset)
async def ask_interval_preset_handler(message: Message, state: FSMContext):
    allowed = {
        "Каждую 1 минуту": 1,
        "Каждые 5 минут": 5,
        "Каждые 30 минут": 30,
        "Каждые 60 минут": 60,
        "Свой интервал": None,
        "Назад": None,
    }
    if message.text not in allowed:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    if message.text == "Свой интервал":
        await push_history(state, Flow.ask_interval_preset.state)
        await state.set_state(Flow.ask_custom_interval)
        await message.answer("Введи интервал в минутах.\nМинимум 1.", reply_markup=single_back_kb())
        return

    st = await state.get_data()
    draft = st["draft"]
    draft["interval_minutes"] = allowed[message.text]
    await state.update_data(draft=draft)
    await show_summary(message, state)


@dp.message(Flow.ask_custom_interval)
async def ask_custom_interval_handler(message: Message, state: FSMContext):
    if message.text == "Назад":
        await back_step(message, state)
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Хуйня. Интервал должен быть числом.")
        return

    value = int(text)
    if value < 1:
        await message.answer("Хуйня. Минимальный интервал — 1 минута.")
        return

    st = await state.get_data()
    draft = st["draft"]
    draft["interval_minutes"] = value
    await state.update_data(draft=draft)
    await show_summary(message, state)


@dp.message(Flow.confirm_save)
async def confirm_save_handler(message: Message, state: FSMContext):
    if message.text not in {"Сохранить", "Изменить", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    draft = st["draft"]

    if message.text == "Изменить":
        await push_history(state, Flow.confirm_save.state)
        await state.set_state(Flow.edit_choose_field)
        await message.answer("Что будем менять?", reply_markup=edit_fields_kb(draft))
        return

    db_data = draft_to_db(message.from_user.id, draft)

    editing = st.get("editing", False)
    reminder_id = st.get("editing_reminder_id")

    if editing and reminder_id:
        await update_reminder(reminder_id, db_data)
    else:
        await insert_reminder(db_data)

    user = await get_user(message.from_user.id)
    first_hint = (user[3] == 0)

    await state.clear()
    await message.answer("Готово. Начинаю заебывать.", reply_markup=ReplyKeyboardRemove())

    if first_hint:
        await message.answer(
            "И еще: включи уведомления у бота.\nИначе я не смогу нормально тебя заебывать."
        )
        await db_execute("UPDATE users SET notification_hint_sent=1 WHERE user_id=?", (message.from_user.id,))

    await send_main_menu(message)


@dp.message(Flow.edit_choose_field)
async def edit_choose_field_handler(message: Message, state: FSMContext):
    st = await state.get_data()
    draft = st["draft"]

    allowed = {"Повод", "Дату", "Время", "Назад"}
    if draft["frequency_type"] != "once":
        allowed.update({"Точное время конца", "Тип заеба"})
        if draft["frequency_type"] == "weekdays":
            allowed.add("Дни недели")

    if message.text not in allowed:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    await push_history(state, Flow.edit_choose_field.state)

    if message.text == "Повод":
        await state.set_state(Flow.ask_title)
        await message.answer("Напиши, из-за чего тебя заебывать.", reply_markup=single_back_kb())
        return

    if message.text == "Дату":
        await state.set_state(Flow.ask_date)
        if draft["frequency_type"] == "once":
            await message.answer(
                "В какую дату заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
                reply_markup=single_back_kb()
            )
        else:
            await message.answer(
                "До какой даты заебывать?\n\nПиши в таком формате: 020726\n\nЭто значит:\n02.07.2026",
                reply_markup=single_back_kb()
            )
        return

    if message.text == "Точное время конца":
        await state.set_state(Flow.ask_exact_end_yes_no)
        await message.answer("Хочешь настроить точное время конца?", reply_markup=yes_no_back_kb())
        return

    if message.text == "Тип заеба":
        await state.set_state(Flow.ask_frequency)
        await message.answer("Как часто заебывать?", reply_markup=frequency_kb())
        return

    if message.text == "Дни недели":
        await state.set_state(Flow.ask_weekdays)
        selected = draft.get("weekdays", [])
        await message.answer(
            f"Выбери дни, в которые тебя заебывать.\nВыбрано: {format_weekdays(selected)}",
            reply_markup=weekdays_ready_kb()
        )
        await message.answer("Дни недели:", reply_markup=weekdays_inline_kb(selected))
        return

    if message.text == "Время":
        if draft["mode_type"] == "fixed":
            await state.set_state(Flow.ask_fixed_times)
            await message.answer(
                "Введи время, в которое тебя заёбывать.\n\n"
                "Пиши так: 1400\n"
                "Если нужно несколько — так: 1400 1600 2130\n\n"
                "Это значит:\n14:00\nили 14:00 16:00 21:30",
                reply_markup=single_back_kb()
            )
        else:
            await state.set_state(Flow.ask_periodic_range)
            await message.answer(
                "Введи промежуток времени, в который тебя заебывать.\n\n"
                "Пиши так: 2300 2400\n\n"
                "Это значит:\nс 23:00 до 24:00",
                reply_markup=single_back_kb()
            )
        return


# =========================
# MY REMINDERS
# =========================

async def show_my_reminders(message: Message, user_id: int) -> None:
    user = await get_user(user_id)
    reminders = await get_active_reminders(user_id)

    if not reminders:
        await message.answer("Пока заебывать нечем.")
        await send_main_menu(message)
        return

    tz_name = user[2]
    parts = []
    for idx, rem in enumerate(reminders, start=1):
        draft = reminder_to_draft(rem)
        lines = [
            f"<b>{idx}. {rem.title}</b>",
            f"Дата: {format_target_date(rem.target_date)}",
        ]
        if rem.exact_end_time and rem.frequency_type != "once":
            lines.append(f"Точный конец: {rem.exact_end_time}")
        lines.append(f"Расписание: {short_schedule(draft)}")
        lines.append(f"Следующее уведомление: {human_next_notification(rem, tz_name)}")
        parts.append("\n".join(lines))

    await message.answer("\n\n".join(parts), reply_markup=my_list_kb())


# =========================
# EDIT SELECT
# =========================

@dp.message(Flow.edit_select_reminder)
async def edit_select_reminder_handler(message: Message, state: FSMContext):
    reminders = await get_active_reminders(message.from_user.id)
    if message.text == "Назад":
        await state.clear()
        await message.answer("Ок.", reply_markup=ReplyKeyboardRemove())
        await send_main_menu(message)
        return

    matched = re.fullmatch(r"(\d+)\.\s.+", message.text or "")
    if not matched:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    idx = int(matched.group(1))
    if idx < 1 or idx > len(reminders):
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    rem = reminders[idx - 1]
    draft = reminder_to_draft(rem)

    await state.update_data(
        editing=True,
        editing_reminder_id=rem.id,
        draft=draft,
    )
    await push_history(state, Flow.edit_select_reminder.state)
    await state.set_state(Flow.edit_choose_field)
    await message.answer("Что будем менять?", reply_markup=edit_fields_kb(draft))


# =========================
# DELETE FLOW
# =========================

@dp.message(Flow.delete_select_reminder)
async def delete_select_reminder_handler(message: Message, state: FSMContext):
    reminders = await get_active_reminders(message.from_user.id)

    if message.text == "Назад":
        await state.clear()
        await message.answer("Ок.", reply_markup=ReplyKeyboardRemove())
        await send_main_menu(message)
        return

    if message.text == "Заебал, удалить всё":
        await push_history(state, Flow.delete_select_reminder.state)
        await state.set_state(Flow.delete_confirm_all)
        await message.answer("Точно удалить вообще всё?", reply_markup=delete_confirm_kb())
        return

    matched = re.fullmatch(r"(\d+)\.\s.+", message.text or "")
    if not matched:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    idx = int(matched.group(1))
    if idx < 1 or idx > len(reminders):
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    rem = reminders[idx - 1]
    draft = reminder_to_draft(rem)

    await state.update_data(delete_reminder_id=rem.id, draft=draft)
    await push_history(state, Flow.delete_select_reminder.state)
    await state.set_state(Flow.delete_confirm_one)

    txt = (
        f"Повод: {rem.title}\n"
        f"Дата: {format_target_date(rem.target_date)}\n"
        f"Расписание: {short_schedule(draft)}"
    )
    await message.answer(txt, reply_markup=delete_confirm_kb())


@dp.message(Flow.delete_confirm_one)
async def delete_confirm_one_handler(message: Message, state: FSMContext):
    if message.text not in {"Да, заебал", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    st = await state.get_data()
    reminder_id = st["delete_reminder_id"]
    await deactivate_reminder(reminder_id)

    await state.clear()
    await message.answer("Удалил. Больше не буду заебывать.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message)


@dp.message(Flow.delete_confirm_all)
async def delete_confirm_all_handler(message: Message, state: FSMContext):
    if message.text not in {"Да, заебал", "Назад"}:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if message.text == "Назад":
        await back_step(message, state)
        return

    await deactivate_all_user_reminders(message.from_user.id)
    await state.clear()
    await message.answer("Удалил всё. Больше не буду заебывать.", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(message)


# =========================
# GENERIC BACK / BUTTON GUARD
# =========================

@dp.message(F.text == "Назад")
async def global_back(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur:
        await back_step(message, state)


@dp.message()
async def fallback_router(message: Message, state: FSMContext):
    cur = await state.get_state()

    if cur == Flow.choose_country.state:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    if cur in BUTTON_ONLY_STATES or cur == Flow.ask_weekdays.state:
        await message.answer("Чел, используй кнопки пожалуйста.")
        return

    user = await get_user(message.from_user.id)
    if not user or not user[2]:
        await ask_country(message, state)
        return

    await send_main_menu(message)


# =========================
# SCHEDULER
# =========================

async def finish_expired_reminders() -> None:
    rows = await db_fetchall("""
        SELECT id, user_id, title, target_date, exact_end_time, frequency_type, mode_type,
               times_text, weekdays_text, start_time, end_time, interval_minutes, active
        FROM reminders
        WHERE active=1
    """)
    for row in rows:
        rem = Reminder.from_row(row)
        user = await get_user(rem.user_id)
        if not user or not user[2]:
            continue
        tz_name = user[2]
        if reminder_is_finished(rem, tz_name):
            await deactivate_reminder(rem.id)


async def scheduler_loop() -> None:
    while True:
        try:
            await finish_expired_reminders()

            rows = await db_fetchall("""
                SELECT id, user_id, title, target_date, exact_end_time, frequency_type, mode_type,
                       times_text, weekdays_text, start_time, end_time, interval_minutes, active
                FROM reminders
                WHERE active=1
            """)

            for row in rows:
                rem = Reminder.from_row(row)
                user = await get_user(rem.user_id)
                if not user or not user[2]:
                    continue

                tz_name = user[2]
                dt_local = now_in_tz(tz_name).replace(second=0, microsecond=0)

                if not should_send_now(rem, tz_name, dt_local):
                    continue

                sent_key = dt_local.strftime("%Y-%m-%d %H:%M")
                if await has_sent(rem.id, sent_key):
                    continue

                text = reminder_left_text(rem, tz_name, dt_local)
                try:
                    await bot.send_message(rem.user_id, text, reply_markup=main_menu_kb())
                    await mark_sent(rem.id, sent_key)
                except Exception as e:
                    logging.exception("Send error: %s", e)

            await asyncio.sleep(20)
        except Exception as e:
            logging.exception("Scheduler error: %s", e)
            await asyncio.sleep(5)


# =========================
# MAIN
# =========================

async def main():
    await init_db()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())