"""
Telegram price calculator bot.
Dependencies are installed automatically at startup if missing.
For production hosting, using requirements.txt during deployment is still recommended.
"""
import importlib.util
import subprocess
import sys
import os

REQUIRED = {
    "aiogram": "aiogram>=3.20,<4.0",
    "aiohttp": "aiohttp>=3.9,<4.0",
    "dotenv": "python-dotenv>=1.0,<2.0",
}

def ensure_dependencies():
    missing = [package for module, package in REQUIRED.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        print("Installing missing Python dependencies:", ", ".join(missing), flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing])

ensure_dependencies()

import asyncio
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
FALLBACK_USD_RUB = Decimal(os.getenv("FALLBACK_USD_RUB", "90"))
RATE_URL = "https://open.er-api.com/v6/latest/USD"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
dp = Dispatcher()


class PriceForm(StatesGroup):
    currency = State()
    cost = State()
    markup = State()
    commission = State()


main_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="💰 Оценить товар")]],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)

currency_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="₽ Рубли"), KeyboardButton(text="$ Доллары")],
        [KeyboardButton(text="↩️ В главное меню")],
    ],
    resize_keyboard=True,
)


def money(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}".replace(",", " ").replace(".", ",")


def parse_decimal(text: str) -> Decimal | None:
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


async def get_usd_rub_rate() -> tuple[Decimal, str]:
    timeout = aiohttp.ClientTimeout(total=7)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(RATE_URL) as response:
                response.raise_for_status()
                data = await response.json()
        rate = Decimal(str(data["rates"]["RUB"]))
        if rate > 0:
            return rate, "онлайн-источник"
    except Exception as exc:
        logging.warning("Could not retrieve USD/RUB rate: %s", exc)
    return FALLBACK_USD_RUB, "резервный курс из настроек"


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! 👋\n\n"
        "Я рассчитаю цену продажи с учётом себестоимости, наценки и комиссии площадки.\n"
        "Результат покажу в рублях и долларах.\n\n"
        "Нажмите «💰 Оценить товар», чтобы начать.",
        reply_markup=main_keyboard,
    )


@dp.message(Command("cancel"))
@dp.message(F.text == "↩️ В главное меню")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Расчёт отменён. Выберите действие в меню.", reply_markup=main_keyboard)


@dp.message(F.text == "💰 Оценить товар")
async def begin(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PriceForm.currency)
    await message.answer("В какой валюте указать себестоимость?", reply_markup=currency_keyboard)


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
    await message.answer(
        f"Введите себестоимость в {'рублях' if currency == 'RUB' else 'долларах'}.\n"
        "Например: 1250 или 19.99"
    )


@dp.message(PriceForm.cost)
async def enter_cost(message: Message, state: FSMContext):
    cost = parse_decimal(message.text or "")
    if cost is None or cost <= 0:
        await message.answer("Введите положительное число, например 1250 или 19.99.")
        return
    await state.update_data(cost=str(cost))
    await state.set_state(PriceForm.markup)
    await message.answer("Введите желаемую наценку в процентах от себестоимости. Например: 30")


@dp.message(PriceForm.markup)
async def enter_markup(message: Message, state: FSMContext):
    markup = parse_decimal(message.text or "")
    if markup is None or markup < 0 or markup > 10000:
        await message.answer("Введите наценку числом от 0 до 10000%. Например: 30")
        return
    await state.update_data(markup=str(markup))
    await state.set_state(PriceForm.commission)
    await message.answer(
        "Введите комиссию площадки в процентах от цены продажи. Например: 15.\n"
        "Если комиссии нет, введите 0."
    )


@dp.message(PriceForm.commission)
async def enter_commission(message: Message, state: FSMContext):
    commission = parse_decimal(message.text or "")
    if commission is None or commission < 0 or commission >= 100:
        await message.answer("Введите комиссию от 0 до 99,99%. Например: 15")
        return

    data = await state.get_data()
    cost = Decimal(data["cost"])
    markup = Decimal(data["markup"])
    currency = data["currency"]

    desired_net = cost * (Decimal("1") + markup / Decimal("100"))
    sale_price = desired_net / (Decimal("1") - commission / Decimal("100"))

    rate, rate_source = await get_usd_rub_rate()
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
        f"Себестоимость: <b>{cost_label}</b>\n"
        f"Наценка: <b>{money(markup)}%</b>\n"
        f"Комиссия площадки: <b>{money(commission)}%</b>\n\n"
        f"💰 <b>Цена продажи:</b>\n"
        f"🇷🇺 <b>{money(sale_rub)} ₽</b>\n"
        f"🇺🇸 <b>${money(sale_usd)}</b>\n\n"
        f"Курс: 1 USD = {money(rate)} RUB ({rate_source}).\n\n"
        "Формула учитывает комиссию от цены продажи и наценку от себестоимости.",
        reply_markup=main_keyboard,
        parse_mode="HTML",
    )


@dp.message()
async def fallback(message: Message):
    await message.answer("Чтобы начать расчёт, нажмите «💰 Оценить товар».", reply_markup=main_keyboard)


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Укажите переменную окружения BOT_TOKEN в панели хостинга."
        )
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
