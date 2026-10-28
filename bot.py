"""
Telegram price calculator with per-user marketplace settings.
Each marketplace has its own USD/RUB exchange rate and commission.
Settings are stored in SQLite and survive restarts.
"""
import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import importlib.util
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# Install missing dependencies as a fallback. Hosting build steps should still
# install requirements.txt before launching the worker.
REQUIRED = {
    "aiogram": "aiogram>=3.20,<4.0",
    "dotenv": "python-dotenv>=1.0,<2.0",
}
missing = [package for module, package in REQUIRED.items()
           if importlib.util.find_spec(module) is None]
if missing:
    print("Installing missing dependencies: " + ", ".join(missing), flush=True)
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", *missing
    ])

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).with_name("bot_data.sqlite3")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
dp = Dispatcher()


class PriceForm(StatesGroup):
    platform = State()
    currency = State()
    cost = State()
    markup = State()


class SettingsForm(StatesGroup):
    action = State()
    new_name = State()
    edit_platform = State()
    edit_rate = State()
    edit_commission = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS platforms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                usd_rub_rate TEXT NOT NULL,
                commission TEXT NOT NULL,
                UNIQUE(user_id, name)
            )
        """)


def get_platforms(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM platforms WHERE user_id=? ORDER BY name COLLATE NOCASE",
            (user_id,)
        ).fetchall()


def get_platform(user_id: int, platform_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM platforms WHERE user_id=? AND id=?",
            (user_id, platform_id)
        ).fetchone()


def add_platform(user_id: int, name: str, rate: Decimal, commission: Decimal):
    with db() as conn:
        conn.execute(
            "INSERT INTO platforms(user_id,name,usd_rub_rate,commission) VALUES(?,?,?,?)",
            (user_id, name, str(rate), str(commission))
        )


def update_platform(user_id: int, platform_id: int, rate: Decimal, commission: Decimal):
    with db() as conn:
        conn.execute(
            "UPDATE platforms SET usd_rub_rate=?, commission=? WHERE user_id=? AND id=?",
            (str(rate), str(commission), user_id, platform_id)
        )


def delete_platform(user_id: int, platform_id: int):
    with db() as conn:
        conn.execute("DELETE FROM platforms WHERE user_id=? AND id=?", (user_id, platform_id))


def money(value: Decimal) -> str:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).__format__(",f").replace(",", " ").replace(".", ",")


def parse_decimal(text: str):
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Рассчитать цену")],
            [KeyboardButton(text="⚙️ Настройки площадок")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def platform_keyboard(platforms, include_back=True):
    rows = [[KeyboardButton(text=f"🏪 {p['name']}")] for p in platforms]
    if include_back:
        rows.append([KeyboardButton(text="↩️ В главное меню")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def settings_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить площадку")],
            [KeyboardButton(text="✏️ Изменить курс/комиссию")],
            [KeyboardButton(text="🗑 Удалить площадку")],
            [KeyboardButton(text="📋 Мои площадки")],
            [KeyboardButton(text="↩️ В главное меню")],
        ],
        resize_keyboard=True,
    )


def platform_id_from_text(text: str, platforms):
    name = text.removeprefix("🏪 ").strip()
    for p in platforms:
        if p["name"] == name:
            return p["id"]
    return None


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! 👋\n\n"
        "Рассчитываю цену продажи с учётом себестоимости, наценки и комиссии.\n"
        "Для каждой площадки можно отдельно задать курс USD/RUB и комиссию. "
        "В одном расчёте выбирается только одна площадка.\n\n"
        "Сначала добавьте площадку в «⚙️ Настройки площадок».",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("cancel"))
@dp.message(F.text == "↩️ В главное меню")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат в главное меню.", reply_markup=main_keyboard())


@dp.message(F.text == "⚙️ Настройки площадок")
async def settings(message: Message, state: FSMContext):
    await state.clear()
    platforms = get_platforms(message.from_user.id)
    if platforms:
        summary = "\n".join(
            f"• {p['name']}: курс 1 USD = {money(Decimal(p['usd_rub_rate']))} ₽; "
            f"комиссия {money(Decimal(p['commission']))}%"
            for p in platforms
        )
    else:
        summary = "Пока нет площадок. Добавьте первую."
    await message.answer("⚙️ Настройки площадок\n\n" + summary, reply_markup=settings_keyboard())


@dp.message(F.text == "📋 Мои площадки")
async def list_platforms(message: Message, state: FSMContext):
    platforms = get_platforms(message.from_user.id)
    if not platforms:
        await message.answer("Площадок пока нет. Нажмите «➕ Добавить площадку».", reply_markup=settings_keyboard())
        return
    lines = [
        f"🏪 <b>{p['name']}</b>\n   Курс: 1 USD = {money(Decimal(p['usd_rub_rate']))} ₽\n"
        f"   Комиссия: {money(Decimal(p['commission']))}%"
        for p in platforms
    ]
    await message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=settings_keyboard())


@dp.message(F.text == "➕ Добавить площадку")
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SettingsForm.new_name)
    await message.answer("Введите название площадки (например, Marketplace A). До 40 символов.")


@dp.message(SettingsForm.new_name)
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 40 or name.startswith("🏪"):
        await message.answer("Название должно содержать от 1 до 40 символов. Попробуйте ещё раз.")
        return
    if any(p["name"].casefold() == name.casefold() for p in get_platforms(message.from_user.id)):
        await message.answer("Такая площадка уже есть. Введите другое название.")
        return
    await state.update_data(new_name=name)
    await state.set_state(SettingsForm.edit_rate)
    await state.update_data(edit_context="add")
    await message.answer("Введите индивидуальный курс: сколько рублей за 1 USD? Например: 92.50")


async def prompt_edit_platform(message: Message, state: FSMContext, action: str):
    platforms = get_platforms(message.from_user.id)
    if not platforms:
        await state.clear()
        await message.answer("Сначала добавьте площадку.", reply_markup=settings_keyboard())
        return
    await state.update_data(edit_context=action)
    await state.set_state(SettingsForm.edit_platform)
    await message.answer("Выберите площадку:", reply_markup=platform_keyboard(platforms))


@dp.message(F.text == "✏️ Изменить курс/комиссию")
async def edit_start(message: Message, state: FSMContext):
    await state.clear()
    await prompt_edit_platform(message, state, "edit")


@dp.message(F.text == "🗑 Удалить площадку")
async def delete_start(message: Message, state: FSMContext):
    await state.clear()
    await prompt_edit_platform(message, state, "delete")


@dp.message(SettingsForm.edit_platform)
async def choose_edit_platform(message: Message, state: FSMContext):
    platforms = get_platforms(message.from_user.id)
    platform_id = platform_id_from_text(message.text or "", platforms)
    if platform_id is None:
        await message.answer("Выберите площадку кнопкой из списка.")
        return
    data = await state.get_data()
    if data.get("edit_context") == "delete":
        delete_platform(message.from_user.id, platform_id)
        await state.clear()
        await message.answer("Площадка удалена.", reply_markup=settings_keyboard())
        return
    await state.update_data(platform_id=platform_id)
    await state.set_state(SettingsForm.edit_rate)
    await message.answer("Введите новый курс: рублей за 1 USD. Например: 92.50")


@dp.message(SettingsForm.edit_rate)
async def enter_rate(message: Message, state: FSMContext):
    rate = parse_decimal(message.text or "")
    if rate is None or rate <= 0 or rate > 1000000:
        await message.answer("Курс должен быть положительным числом. Например: 92.50")
        return
    data = await state.get_data()
    await state.update_data(rate=str(rate))
    if data.get("edit_context") == "add":
        await state.set_state(SettingsForm.edit_commission)
        await message.answer("Введите комиссию этой площадки в процентах (от 0 до 99,99). Например: 15")
    else:
        await state.set_state(SettingsForm.edit_commission)
        await message.answer("Введите новую комиссию площадки в процентах (от 0 до 99,99). Например: 15")


@dp.message(SettingsForm.edit_commission)
async def enter_commission(message: Message, state: FSMContext):
    commission = parse_decimal(message.text or "")
    if commission is None or commission < 0 or commission >= 100:
        await message.answer("Комиссия должна быть от 0 до 99,99%. Например: 15")
        return
    data = await state.get_data()
    if data.get("edit_context") == "add":
        try:
            add_platform(message.from_user.id, data["new_name"], Decimal(data["rate"]), commission)
        except sqlite3.IntegrityError:
            await message.answer("Площадка с таким названием уже существует.")
            await state.clear()
            return
        note = f"Площадка «{data['new_name']}» добавлена."
    else:
        update_platform(message.from_user.id, data["platform_id"], Decimal(data["rate"]), commission)
        note = "Настройки площадки обновлены."
    await state.clear()
    await message.answer(note, reply_markup=settings_keyboard())


@dp.message(F.text == "💰 Рассчитать цену")
async def begin_calculation(message: Message, state: FSMContext):
    await state.clear()
    platforms = get_platforms(message.from_user.id)
    if not platforms:
        await message.answer("Сначала добавьте площадку в «⚙️ Настройки площадок».", reply_markup=settings_keyboard())
        return
    await state.set_state(PriceForm.platform)
    await message.answer(
        "Выберите одну площадку для этого расчёта:",
        reply_markup=platform_keyboard(platforms),
    )


@dp.message(PriceForm.platform)
async def choose_platform(message: Message, state: FSMContext):
    platforms = get_platforms(message.from_user.id)
    platform_id = platform_id_from_text(message.text or "", platforms)
    if platform_id is None:
        await message.answer("Выберите одну площадку кнопкой из списка.")
        return
    platform = get_platform(message.from_user.id, platform_id)
    await state.update_data(
        platform_id=platform_id,
        platform_name=platform["name"],
        rate=platform["usd_rub_rate"],
        commission=platform["commission"],
    )
    await state.set_state(PriceForm.currency)
    await message.answer(
        f"Площадка: {platform['name']}\n"
        f"Её курс: 1 USD = {money(Decimal(platform['usd_rub_rate']))} ₽\n"
        f"Её комиссия: {money(Decimal(platform['commission']))}%\n\n"
        "В какой валюте указать себестоимость?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="₽ Рубли"), KeyboardButton(text="$ Доллары")],
                [KeyboardButton(text="↩️ В главное меню")],
            ],
            resize_keyboard=True,
        ),
    )


@dp.message(PriceForm.currency)
async def choose_currency(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "₽ Рубли":
        currency = "RUB"
    elif text == "$ Доллары":
        currency = "USD"
    else:
        await message.answer("Выберите валюту кнопкой: ₽ Рубли или $ Доллары.")
        return
    await state.update_data(currency=currency)
    await state.set_state(PriceForm.cost)
    await message.answer(f"Введите себестоимость в {'рублях' if currency == 'RUB' else 'долларах'}. Например: 1250 или 19.99")


@dp.message(PriceForm.cost)
async def enter_cost(message: Message, state: FSMContext):
    cost = parse_decimal(message.text or "")
    if cost is None or cost <= 0:
        await message.answer("Введите положительное число, например 1250 или 19.99.")
        return
    await state.update_data(cost=str(cost))
    await state.set_state(PriceForm.markup)
    await message.answer("Введите наценку в процентах от себестоимости. Например: 30")


@dp.message(PriceForm.markup)
async def enter_markup(message: Message, state: FSMContext):
    markup = parse_decimal(message.text or "")
    if markup is None or markup < 0 or markup > 10000:
        await message.answer("Введите наценку от 0 до 10000%. Например: 30")
        return
    data = await state.get_data()
    cost = Decimal(data["cost"])
    commission = Decimal(data["commission"])
    rate = Decimal(data["rate"])
    currency = data["currency"]
    platform_name = data["platform_name"]

    desired_net = cost * (Decimal("1") + markup / Decimal("100"))
    sale_price = desired_net / (Decimal("1") - commission / Decimal("100"))

    if currency == "RUB":
        sale_rub = sale_price
        sale_usd = sale_rub / rate
        cost_label = f"{money(cost)} ₽"
    else:
        sale_usd = sale_price
        sale_rub = sale_usd * rate
        cost_label = f"${money(cost)}"

    await state.clear()
    await message.answer(
        "📊 <b>Результат расчёта</b>\n\n"
        f"Площадка: <b>{platform_name}</b>\n"
        f"Себестоимость: <b>{cost_label}</b>\n"
        f"Наценка: <b>{money(markup)}%</b>\n"
        f"Комиссия площадки: <b>{money(commission)}%</b>\n\n"
        f"💰 <b>Цена продажи:</b>\n"
        f"🇷🇺 <b>{money(sale_rub)} ₽</b>\n"
        f"🇺🇸 <b>${money(sale_usd)}</b>\n\n"
        f"Индивидуальный курс площадки: 1 USD = {money(rate)} RUB.\n"
        "Комиссия рассчитывается от цены продажи, наценка — от себестоимости.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message()
async def fallback(message: Message):
    await message.answer("Выберите действие в меню или отправьте /start.", reply_markup=main_keyboard())


async def main():
    init_db()
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавьте переменную окружения BOT_TOKEN в панели хостинга.")
    bot = Bot(token=TOKEN)
    log.info("Bot starting; SQLite database: %s", DB_PATH)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
