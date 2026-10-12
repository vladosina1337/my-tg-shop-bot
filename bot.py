"""
Telegram-бот для приёма заказов из Telegram Mini App.
Библиотека: aiogram 3.x

Запуск:
    pip install -U aiogram
    python bot.py
"""

import asyncio
import html
import json
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# ============================================================
#                       НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА")   # токен от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))               # ваш Telegram ID (число), узнать у @userinfobot
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-app.vercel.app")  # ссылка на Mini App (только https)
PORT = int(os.getenv("PORT", "8080"))                            # порт для веб-сервера (Render задаёт PORT сам)
# ============================================================

router = Router()


# ---------- Вспомогательные функции ----------

def esc(value) -> str:
    """Экранирует пользовательский текст для HTML-разметки Telegram."""
    return html.escape(str(value)) if value not in (None, "") else "—"


def to_number(value, default=0.0) -> float:
    """Безопасно превращает значение в число."""
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def money(value) -> str:
    """12345.5 -> '12 346 ₽'"""
    return f"{to_number(value):,.0f}".replace(",", " ") + " ₽"


def build_order_report(data: dict, message: Message) -> str:
    """
    Ожидаемый формат JSON из Mini App (лишние поля игнорируются):

    {
      "client": { "id": 123456789, "name": "Влад Иванов", "username": "@vlad" },
      "items": [ { "name": "Карбоновая ткань 3K", "quantity": 2, "price": 1500 } ],
      "total": 3000
    }

    Необязательно можно добавить в "client" поля phone, city, address, comment —
    если они есть, бот покажет их в отчёте.
    """
    client = data.get("client") or {}
    items = data.get("items") or []
    user = message.from_user

    # Telegram ID берём из самого сообщения (его нельзя подделать),
    # а не из JSON, который приходит от клиентской части приложения.
    username = client.get("username") or (f"@{user.username}" if user.username else "")
    if username and not username.startswith("@"):
        username = "@" + username

    # --- Данные клиента ---
    lines = [
        "🛒 <b>НОВЫЙ ЗАКАЗ</b>",
        "",
        "👤 <b>Клиент</b>",
        f"Имя: {esc(client.get('name') or user.full_name)}",
        f"Telegram: {esc(username or 'нет username')}",
        f"ID: <code>{user.id}</code>",
    ]
    for key, label in (("phone", "Телефон"), ("city", "Город"), ("address", "Адрес / ТК")):
        if client.get(key):
            lines.append(f"{label}: {esc(client[key])}")
    if client.get("comment"):
        lines.append(f"💬 Комментарий: {esc(client['comment'])}")

    # --- Список товаров ---
    lines += ["", "📦 <b>Состав заказа</b>"]
    calculated_total = 0.0
    for i, item in enumerate(items, start=1):
        name = item.get("name") or item.get("title") or "Товар"
        qty = to_number(item.get("quantity", item.get("qty", 1)), 1)
        price = to_number(item.get("price"))
        subtotal = qty * price
        calculated_total += subtotal
        qty_text = f"{qty:g}"
        lines.append(
            f"{i}. {esc(name)}\n"
            f"    {qty_text} × {money(price)} = <b>{money(subtotal)}</b>"
        )
    if not items:
        lines.append("— список товаров пуст —")

    # --- Итого ---
    total = data.get("total")
    total_value = to_number(total) if total is not None else calculated_total
    lines += ["", f"💰 <b>Итого: {money(total_value)}</b>"]

    # Предупреждение, если сумма из приложения не сходится с суммой по позициям
    if total is not None and items and abs(total_value - calculated_total) > 1:
        lines.append(
            f"⚠️ Сумма по позициям: {money(calculated_total)} — не совпадает с итогом из приложения."
        )

    report = "\n".join(lines)
    return report[:4000]  # лимит Telegram — 4096 символов


# ---------- Хендлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    # ВАЖНО: sendData работает только если Mini App открыт через
    # обычную кнопку-клавиатуру (KeyboardButton), а не через inline-кнопку.
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]
        ],
        resize_keyboard=True,
    )
    await message.answer(
        f"Привет, {html.escape(message.from_user.first_name or 'друг')}! 👋\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог и оформить заказ.",
        reply_markup=keyboard,
    )


@router.message(F.web_app_data)
async def handle_web_app_order(message: Message, bot: Bot):
    raw = message.web_app_data.data

    # 1. Разбираем JSON
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON должен быть объектом")
    except (json.JSONDecodeError, ValueError) as e:
        logging.exception("Не удалось разобрать данные из Mini App")
        await message.answer(
            "Не удалось обработать заказ. Попробуйте оформить его ещё раз."
        )
        # Сообщаем админу, чтобы заказ не потерялся
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Ошибка разбора заказа от ID {message.from_user.id}: {html.escape(str(e))}\n\n"
                f"Сырые данные:\n<code>{html.escape(raw[:3000])}</code>",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа об ошибке")
        return

    # 2. Формируем отчёт и отправляем админу
    report = build_order_report(data, message)
    try:
        await bot.send_message(ADMIN_ID, report)
    except Exception:
        logging.exception("Не удалось отправить заказ админу")
        await message.answer(
            "Не удалось передать заказ администратору. Пожалуйста, попробуйте позже."
        )
        return

    # 3. Подтверждение покупателю
    await message.answer(
        "Спасибо за заказ! Администратор уже принял его в работу и свяжется с вами."
    )


# ---------- Фиктивный веб-сервер (для Render) ----------

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def run_web_server():
    """Слушает PORT и отвечает 200 OK на '/', чтобы Render видел открытый порт."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info("Веб-сервер запущен на порту %s", PORT)
    try:
        await asyncio.Event().wait()  # работаем бесконечно
    finally:
        await runner.cleanup()


# ---------- Запуск ----------

async def run_bot():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    logging.basicConfig(level=logging.INFO)
    # Бот (polling) и веб-сервер работают одновременно
    await asyncio.gather(run_bot(), run_web_server())


if __name__ == "__main__":
    asyncio.run(main())
