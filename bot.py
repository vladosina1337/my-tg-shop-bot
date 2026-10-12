"""
Telegram-бот + HTTP API для приёма заказов из Telegram Mini App.
Библиотеки: aiogram 3.x (вместе с ним ставится aiohttp)

Заказ приходит двумя путями:
  1) POST /api/order  — основной (fetch из Mini App, работает и через Menu Button)
  2) web_app_data     — запасной (sendData, работает только с кнопкой-клавиатурой из /start)

Запуск:
    pip install -U aiogram
    python bot.py
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from urllib.parse import parse_qsl, urlparse

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
PORT = int(os.getenv("PORT", "8080"))                            # порт веб-сервера (Render задаёт PORT сам)

# Откуда разрешены запросы к API (CORS). По умолчанию — домен вашего Mini App.
# Если домен на Vercel другой, задайте переменную окружения ALLOWED_ORIGIN.
_parsed = urlparse(WEBAPP_URL)
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", f"{_parsed.scheme}://{_parsed.netloc}")

INIT_DATA_MAX_AGE = 24 * 60 * 60  # initData старше суток считаем недействительным
MAX_ITEMS = 100

THANKS_TEXT = "Спасибо за заказ! Администратор уже принял его в работу и свяжется с вами."
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


def validate_init_data(init_data, bot_token: str, max_age: int = INIT_DATA_MAX_AGE):
    """
    Проверяет подпись Telegram.WebApp.initData (HMAC-SHA256) по алгоритму из документации Telegram.
    Возвращает словарь пользователя (id, first_name, username...) или None, если данные поддельные.
    """
    if not isinstance(init_data, str) or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > max_age:
        return None

    try:
        user = json.loads(pairs.get("user", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


def build_order_report(data: dict, user: dict) -> str:
    """
    Формирует текст заказа для администратора.

    data — JSON заказа:
    {
      "items": [ { "name": "Карбоновая ткань 3K", "quantity": 2, "price": 1500 } ],
      "total": 3000,
      "client": { "phone": "...", "city": "...", "address": "...", "comment": "..." }  // необязательно
    }
    user — данные Telegram-пользователя (id, first_name, last_name, username).
    ID и username берутся из проверенных данных Telegram, а не из JSON заказа.
    """
    client = data.get("client")
    if not isinstance(client, dict):
        client = {}
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]

    full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    username = f"@{user['username']}" if user.get("username") else "нет username"

    # --- Данные клиента ---
    lines = [
        "🛒 <b>НОВЫЙ ЗАКАЗ</b>",
        "",
        "👤 <b>Клиент</b>",
        f"Имя: {esc(client.get('name') or full_name)}",
        f"Telegram: {esc(username)}",
        f"ID: <code>{user['id']}</code>",
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
        lines.append(
            f"{i}. {esc(name)}\n"
            f"    {qty:g} × {money(price)} = <b>{money(subtotal)}</b>"
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

    return "\n".join(lines)[:4000]  # лимит Telegram — 4096 символов


# ---------- Хендлеры бота ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
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
    """Запасной путь: заказ через sendData (только для кнопки-клавиатуры)."""
    raw = message.web_app_data.data
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON должен быть объектом")
    except (json.JSONDecodeError, ValueError):
        logging.exception("Не удалось разобрать данные из Mini App")
        await message.answer("Не удалось обработать заказ. Попробуйте оформить его ещё раз.")
        return

    u = message.from_user
    user = {
        "id": u.id,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "username": u.username,
    }
    try:
        await bot.send_message(ADMIN_ID, build_order_report(data, user))
    except Exception:
        logging.exception("Не удалось отправить заказ админу")
        await message.answer("Не удалось передать заказ администратору. Попробуйте позже.")
        return
    await message.answer(THANKS_TEXT)


# ---------- HTTP API (aiohttp) ----------

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Разрешает запросы из Mini App (другой домен) и отвечает на preflight-запрос OPTIONS."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc

    origin = request.headers.get("Origin")
    if origin and origin == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"
    return response


def error(message: str, status: int) -> web.Response:
    return web.json_response({"status": "error", "message": message}, status=status)


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def handle_order(request: web.Request) -> web.Response:
    """POST /api/order — принимает заказ из Mini App."""
    bot: Bot = request.app["bot"]

    # 1. Читаем JSON
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return error("Некорректный JSON", 400)

    # 2. Проверяем подпись initData — так мы знаем, что запрос действительно из Telegram
    user = validate_init_data(data.get("initData"), BOT_TOKEN)
    if user is None:
        return error("Не удалось подтвердить пользователя Telegram. Откройте магазин из бота.", 401)

    # 3. Проверяем корзину
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return error("Корзина пуста", 400)
    if len(items) > MAX_ITEMS:
        return error("Слишком много позиций в заказе", 400)

    # 4. Отправляем отчёт администратору
    try:
        await bot.send_message(ADMIN_ID, build_order_report(data, user))
    except Exception:
        logging.exception("Не удалось отправить заказ админу")
        return error("Не удалось передать заказ администратору", 500)

    # 5. Подтверждение покупателю (если не получилось — заказ всё равно принят)
    try:
        await bot.send_message(user["id"], THANKS_TEXT)
    except Exception:
        logging.warning("Не удалось отправить подтверждение покупателю %s", user["id"])

    return web.json_response({"status": "ok"})


async def run_web_server(bot: Bot):
    """Слушает PORT: GET / -> 200 OK (для Render), POST /api/order -> приём заказов."""
    app = web.Application(middlewares=[cors_middleware])
    app["bot"] = bot
    app.router.add_get("/", handle_root)
    app.router.add_post("/api/order", handle_order)

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

async def run_bot(bot: Bot):
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Бот (polling) и веб-сервер работают одновременно
    await asyncio.gather(run_bot(bot), run_web_server(bot))


if __name__ == "__main__":
    asyncio.run(main())
