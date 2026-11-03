import os
import asyncio
import logging
import json
import re
import html
import hashlib
import time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from google import genai
from aiogram import Bot, Dispatcher, BaseMiddleware, types, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiohttp import web

# =====================================================================
# 1. КЛЮЧИ И НАСТРОЙКИ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Не найдены ключи TELEGRAM_TOKEN или GEMINI_API_KEY")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

# Если подключишь Render Disk, укажи DATA_DIR=/data (путь монтирования диска).
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

SENT_ADS_FILE = os.path.join(DATA_DIR, "sent_ads.json")
FAV_ADS_FILE = os.path.join(DATA_DIR, "favorites.json")
SENT_TITLES_FILE = os.path.join(DATA_DIR, "sent_titles.json")
CHAT_FILE = os.path.join(DATA_DIR, "chat.json")

CHOTOT_URL = "https://www.chotot.com/da-nang/mua-ban-xe-may?price=0-9000000"
MAX_PRICE_VND = 9_000_000
MAX_ADS_PER_SCAN = 20  # сколько НОВЫХ объявлений проверяем за один скан
MAX_SENT_ADS = 500
MAX_DETAILS = 500
STOP_WORDS = [
    "phụ tùng", "thế chấp", "cho thuê", "cần mua", "thu mua",
    "đồ chơi", "cho thue", "phu tung",
]


def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Не удалось прочитать {filepath}: {e}")
    return default


def save_json(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Ошибка сохранения файла {filepath}: {e}")


# sent_ads и favorite_ads: dict (url -> True), чтобы сохранялся порядок добавления.
sent_ads: dict = dict.fromkeys(load_json(SENT_ADS_FILE, []), True)
favorite_ads: dict = dict.fromkeys(load_json(FAV_ADS_FILE, []), True)
sent_details: dict = load_json(SENT_TITLES_FILE, {})

# Лучше всего задать OWNER_CHAT_ID в настройках Render (переживает любые перезапуски).
_owner_env = os.environ.get("OWNER_CHAT_ID", "").strip()
OWNER_FROM_ENV = _owner_env.lstrip("-").isdigit()
MY_CHAT_ID = int(_owner_env) if OWNER_FROM_ENV else load_json(CHAT_FILE, {}).get("chat_id")
AUTO_CHECK_SECONDS = int(os.environ.get("AUTO_CHECK_SECONDS", "600"))
user_notifications_active = {}
scan_lock = asyncio.Lock()
rejected_ads: dict = {}  # объявления, не прошедшие фильтр (хранятся только в памяти)
BOT_STARTED = time.time()
VN_TZ = timezone(timedelta(hours=7))  # время Дананга
last_check = {"time": None, "auto": False, "found": 0, "new": 0, "error": None}


class RememberChatMiddleware(BaseMiddleware):
    """Запоминает чат при любом сообщении или нажатии кнопки, чтобы автопроверка
    знала, куда слать объявления, даже если /start после перезапуска не нажимали.
    Если задан OWNER_CHAT_ID, чужие чаты ничего не меняют."""

    async def __call__(self, handler, event, data):
        global MY_CHAT_ID
        chat = None
        if isinstance(event, types.Message):
            chat = event.chat
        elif isinstance(event, types.CallbackQuery) and event.message:
            chat = event.message.chat
        if chat and chat.type == "private" and not OWNER_FROM_ENV and chat.id != MY_CHAT_ID:
            MY_CHAT_ID = chat.id
            save_json(CHAT_FILE, {"chat_id": MY_CHAT_ID})
            logging.info(f"Запомнил чат для автопроверки: {MY_CHAT_ID}")
        return await handler(event, data)


dp.message.outer_middleware(RememberChatMiddleware())
dp.callback_query.outer_middleware(RememberChatMiddleware())


def save_sent():
    save_json(SENT_ADS_FILE, list(sent_ads))


def save_favs():
    save_json(FAV_ADS_FILE, list(favorite_ads))


def save_details():
    save_json(SENT_TITLES_FILE, sent_details)


def trim_state():
    """Удаляет самые старые записи, избранное не трогает."""
    while len(sent_ads) > MAX_SENT_ADS:
        del sent_ads[next(iter(sent_ads))]
    if len(sent_details) > MAX_DETAILS:
        for url in list(sent_details):
            if len(sent_details) <= MAX_DETAILS:
                break
            if url not in favorite_ads:
                del sent_details[url]


def uid(url: str) -> str:
    """Стабильный идентификатор для callback_data (hash() меняется при каждом запуске)."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def find_url(h: str, *collections):
    for coll in collections:
        for url in coll:
            if uid(url) == h:
                return url
    return None


def fmt_price(price) -> str:
    return f"{price:,} VND" if price and price > 0 else "Цена в описании"


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def open_button(url: str) -> list:
    """Ссылка на объявление лежит в кнопке, а не в тексте, поэтому большого превью нет."""
    return [InlineKeyboardButton(text="🔗 Открыть объявление", url=url)]


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


# =====================================================================
# 2. МИНИ-ВЕБ-СЕРВЕР ДЛЯ RENDER
# =====================================================================
async def handle_ping(request):
    return web.Response(text="Bike Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")


# =====================================================================
# 3. ПАРСИНГ, ЦЕНА, GEMINI
# =====================================================================
def fetch_url(url: str, timeout: int = 8):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    return requests.get(url, headers=headers, timeout=timeout)


def extract_price_from_text(text: str) -> int:
    # «9tr5» -> 9 500 000
    m = re.search(r'(\d+)\s*tr\s*(\d+)', text, re.IGNORECASE)
    if m:
        try:
            return int(float(f"{m.group(1)}.{m.group(2)}") * 1_000_000)
        except ValueError:
            pass

    # «8tr», «8,5 triệu»; \b отсекает слова вроде «trên»
    m = re.search(r'(\d+[\.,]?\d*)\s*(?:triệu|trieu|tr)\b', text, re.IGNORECASE)
    if m:
        try:
            return int(float(m.group(1).replace(',', '.')) * 1_000_000)
        except ValueError:
            pass

    for num_str in re.findall(r'(?<![\d.,])\d[\d\.,]{6,}(?![\d])', text):
        clean = re.sub(r'[^\d]', '', num_str)
        if clean:
            val = int(clean)
            if 1_000_000 <= val <= 99_000_000:
                return val
    return 0


def _find_ad_dict(node, depth=0):
    """Ищет в __NEXT_DATA__ словарь объявления (есть subject и body/price)."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        if "subject" in node and ("body" in node or "price" in node):
            return node
        for v in node.values():
            found = _find_ad_dict(v, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node[:50]:
            found = _find_ad_dict(v, depth + 1)
            if found:
                return found
    return None


def parse_ad_page(html_text: str):
    """Достаёт название, цену и описание ТОЛЬКО самого объявления.
    Сначала пробует JSON из __NEXT_DATA__, потом h1 и блок описания."""
    soup = BeautifulSoup(html_text, 'html.parser')
    title, desc, price = None, "", 0

    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            ad = _find_ad_dict(json.loads(nd.string))
            if ad:
                title = ad.get("subject")
                desc = ad.get("body") or ""
                p = ad.get("price")
                if isinstance(p, (int, float)) and p > 0:
                    price = int(p)
        except Exception as e:
            logging.warning(f"Не удалось разобрать __NEXT_DATA__: {e}")

    if not title:
        h1 = soup.find('h1')
        title = h1.get_text().strip() if h1 else None

    if not desc:
        block = soup.find(
            'div',
            class_=lambda x: x and ('adBody' in x or 'description' in x.lower())
        )
        if block:
            desc = block.get_text(separator="\n").strip()

    if not title and not desc:
        return None

    title = (title or "Байк до 9 млн VND").strip()[:80]
    desc = desc.strip()
    if price == 0:
        price = extract_price_from_text(f"{title}\n{desc}")

    return {"title": title, "price": price, "description": desc[:3000]}


def markdown_to_html(text: str) -> str:
    text = html.escape(text, quote=False)  # сначала экранируем, потом размечаем
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'^\*\s+', '• ', text, flags=re.MULTILINE)
    return text


async def gemini_generate(contents: str) -> str:
    response = await asyncio.wait_for(
        asyncio.to_thread(
            lambda: client.models.generate_content(model=GEMINI_MODEL, contents=contents)
        ),
        timeout=20.0,
    )
    return response.text


async def analyze_ad_with_gemini(ad_text: str) -> str:
    prompt = (
        "Проанализируй текст объявления о продаже мопеда/скутера во Вьетнаме.\n"
        "Напиши краткую выжимку на русском языке:\n"
        "1. Точная модель, год и цена (в миллионах донг или VND).\n"
        "2. Состояние техники, мотора и документов.\n"
        "3. Вердикт: стоит ли ехать смотреть.\n\n"
        f"Текст объявления:\n{ad_text[:3000]}"
    )
    try:
        text = await gemini_generate(prompt)
        return markdown_to_html(text[:3000])
    except Exception as e:
        logging.error(f"Ошибка Gemini API: {e}")
        return "Не удалось проанализировать описание."


# =====================================================================
# 4. КНОПКИ И ОБРАБОТЧИКИ
# =====================================================================
async def set_main_menu(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="check", description="🔍 Проверить новые байки"),
        BotCommand(command="favorites", description="⭐ Избранные байки"),
        BotCommand(command="reset", description="🔄 Сбросить память"),
        BotCommand(command="status", description="ℹ️ Статус бота"),
        BotCommand(command="id", description="🆔 Мой chat id"),
    ])


def get_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    is_active = user_notifications_active.get(chat_id, True)
    toggle_text = "🔕 Выключить уведомления" if is_active else "🔔 Включить уведомления"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить байки"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text=toggle_text), KeyboardButton(text="ℹ️ Статус")],
        ],
        resize_keyboard=True,
    )


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_notifications_active[message.chat.id] = True

    await message.answer(
        "Привет! 🛵 Бот готов к работе.\n\n"
        "🎯 <b>Особенности:</b>\n"
        "• Быстрое сканирование без задержек\n"
        "• ИИ-анализ по клику на кнопку «🤖 Подробнее»\n"
        "• Удобное избранное",
        parse_mode="HTML",
        reply_markup=get_keyboard(message.chat.id),
    )


@dp.message(F.text == "ℹ️ Статус")
@dp.message(Command("status"))
async def status_handler(message: types.Message):
    chat_id = message.chat.id
    lines = [
        "ℹ️ <b>Статус бота</b>",
        f"🟢 Работает без перезапуска: {fmt_duration(time.time() - BOT_STARTED)}",
    ]

    if last_check["time"]:
        when = datetime.fromtimestamp(last_check["time"], VN_TZ).strftime("%d.%m %H:%M")
        kind = "авто" if last_check["auto"] else "вручную"
        if last_check["error"]:
            lines.append(f"⚠️ Последняя проверка ({when}, {kind}) закончилась ошибкой: {esc(last_check['error'])}")
        else:
            lines.append(
                f"🔄 Последняя проверка: {when} ({kind}), ссылок на странице "
                f"{last_check['found']}, новых {last_check['new']}"
            )
    else:
        lines.append("🔄 Проверок с момента запуска ещё не было")

    lines.append(f"⏱ Автопроверка каждые {AUTO_CHECK_SECONDS // 60} мин")

    if OWNER_FROM_ENV:
        lines.append("📬 Чат для уведомлений: закреплён (OWNER_CHAT_ID)")
    elif MY_CHAT_ID:
        lines.append("📬 Чат для уведомлений: запомнен, но может сброситься при перезапуске (лучше задать OWNER_CHAT_ID)")
    else:
        lines.append("📬 Чат для уведомлений: не задан, нажми /start")

    notif = "включены" if user_notifications_active.get(chat_id, True) else "выключены"
    lines.append(f"🔔 Уведомления: {notif}")
    lines.append(
        f"🧠 В памяти: отправлено {len(sent_ads)}, отклонено фильтром {len(rejected_ads)}, "
        f"в избранном {len(favorite_ads)}"
    )

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_keyboard(chat_id))


@dp.message(Command("id"))
async def id_handler(message: types.Message):
    await message.answer(
        f"Твой chat id: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@dp.message(F.text == "🔍 Проверить байки")
@dp.message(Command("check"))
async def manual_check_handler(message: types.Message):
    await message.answer("🔍 Быстро сканирую байки до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)


@dp.message(F.text == "⭐ Избранное")
@dp.message(Command("favorites"))
async def show_favorites_handler(message: types.Message):
    if not favorite_ads:
        await message.answer(
            "⭐ У тебя пока нет сохраненных байков в избранном.",
            reply_markup=get_keyboard(message.chat.id),
        )
        return

    await message.answer(
        "⭐ <b>Твои сохраненные байки:</b>",
        parse_mode="HTML",
        reply_markup=get_keyboard(message.chat.id),
    )

    for url in list(favorite_ads):
        info = sent_details.get(url, {})
        title = info.get("title", "Байк из избранного")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            open_button(url),
            [InlineKeyboardButton(text="🤖 Подробнее (ИИ-анализ)", callback_data=f"show_{uid(url)}")],
            [InlineKeyboardButton(text="🗑 Удалить из избранного", callback_data=f"unfav_{uid(url)}")],
        ])
        await message.answer(
            f"🛵 <b>{esc(title)}</b>\n💰 {fmt_price(info.get('price', 0))}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await asyncio.sleep(0.2)


@dp.message(Command("reset"))
async def reset_history_handler(message: types.Message):
    sent_ads.clear()
    # Детали избранного оставляем, иначе у него пропадут названия и кнопка «Подробнее».
    for url in list(sent_details):
        if url not in favorite_ads:
            del sent_details[url]
    save_sent()
    save_details()
    await message.answer(
        "🔄 Память сброшена! Избранное сохранено.",
        reply_markup=get_keyboard(message.chat.id),
    )


@dp.message(F.text.in_(["🔕 Выключить уведомления", "🔔 Включить уведомления"]))
async def toggle_notifications_handler(message: types.Message):
    chat_id = message.chat.id
    new_state = not user_notifications_active.get(chat_id, True)
    user_notifications_active[chat_id] = new_state
    text = "🔔 Уведомления включены!" if new_state else "🔕 Уведомления выключены."
    await message.answer(text, reply_markup=get_keyboard(chat_id))


@dp.callback_query(F.data.startswith("show_"))
async def callback_show_details_handler(callback: types.CallbackQuery):
    h = callback.data.replace("show_", "", 1)
    target_url = find_url(h, sent_details)
    if not target_url:
        await callback.answer("⚠️ Информация не найдена (возможно, сброшена память).", show_alert=True)
        return

    await callback.answer("🤖 Генерирую анализ Gemini...")
    info = sent_details[target_url]

    ai_summary = info.get("summary")
    if not ai_summary:
        desc = info.get("description", "")
        if not desc:
            try:
                resp = await asyncio.to_thread(fetch_url, target_url, 8)
                if resp.status_code == 200:
                    parsed = await asyncio.to_thread(parse_ad_page, resp.text)
                    if parsed:
                        desc = parsed["description"]
                        info["description"] = desc
            except Exception as e:
                logging.error(f"Не удалось догрузить описание {target_url}: {e}")

        ai_summary = await analyze_ad_with_gemini(desc or "Описание отсутствует")
        if "Не удалось проанализировать" not in ai_summary:
            info["summary"] = ai_summary  # неудачный результат не кэшируем
            save_details()

    text = (
        f"🔥 <b>Детали байка:</b>\n"
        f"🛵 {esc(info.get('title', ''))}\n\n"
        f"💰 <b>Цена:</b> {fmt_price(info.get('price', 0))}\n"
        f"🤖 <b>Анализ Gemini:</b>\n{ai_summary}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        open_button(target_url),
        [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_{uid(target_url)}")],
    ])
    await callback.message.answer(text[:4000], parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("fav_"))
async def callback_favorite_handler(callback: types.CallbackQuery):
    h = callback.data.replace("fav_", "", 1)
    target_url = find_url(h, sent_details, favorite_ads)
    if not target_url:
        await callback.answer("⚠️ Ссылка не найдена.", show_alert=True)
        return

    if target_url in favorite_ads:
        await callback.answer("⭐ Уже в избранном.", show_alert=True)
    else:
        favorite_ads[target_url] = True
        save_favs()
        await callback.answer("⭐ Добавлено в избранное!", show_alert=True)


@dp.callback_query(F.data.startswith("unfav_"))
async def callback_unfavorite_handler(callback: types.CallbackQuery):
    h = callback.data.replace("unfav_", "", 1)
    target_url = find_url(h, favorite_ads)
    if not target_url:
        await callback.answer("⚠️ Уже удалено.", show_alert=True)
        return

    del favorite_ads[target_url]
    save_favs()
    await callback.answer("🗑 Удалено из избранного.", show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass


# Обычный чат с Gemini. Регистрируется ПОСЛЕ всех обработчиков кнопок,
# поэтому служебные тексты сюда не доходят; команды отсекает фильтр.
@dp.message(F.text & ~F.text.startswith("/"))
async def chat_handler(message: types.Message):
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await gemini_generate(message.text)
        await message.answer(answer[:4000])
    except Exception as e:
        logging.error(f"Ошибка Gemini в чате: {e}")
        await message.answer("⚠️ Не удалось получить ответ от Gemini. Попробуй еще раз через пару секунд.")


# =====================================================================
# 5. СКАНИРОВАНИЕ И ФИЛЬТРАЦИЯ
# =====================================================================
def extract_ad_links(listing_html: str):
    soup = BeautifulSoup(listing_html, 'html.parser')
    urls = []
    for a in soup.find_all('a', href=True):
        href = a['href'].split('?')[0]
        # Ссылка на объявление выглядит так: /mua-ban-xe-may-<район>-da-nang/135599395.htm
        if re.search(r'/mua-ban-xe-may[^/]*/\d{6,}\.htm$', href):
            url = "https://www.chotot.com" + href if href.startswith("/") else href
            if url not in urls and url != CHOTOT_URL:
                urls.append(url)
    return urls


async def fetch_ad(url: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            resp = await asyncio.to_thread(fetch_url, url, 8)
            if resp.status_code != 200:
                return url, None
            return url, await asyncio.to_thread(parse_ad_page, resp.text)
        except Exception as e:
            logging.error(f"Ошибка при загрузке {url}: {e}")
            return url, None


# Приоритетные ликвидные модели: ключ = название в карточке, значение = шаблоны поиска.
# Хочешь добавить модель, допиши строку по такому же образцу.
PRIORITY_MODELS = {
    "Honda Air Blade": [r"air\s*-?\s*blade", r"\bab\s?(?:110|125|150|160)\b"],
    "Honda Vision": [r"\bvision\b"],
    "Honda Lead": [r"\blead\b"],
}
CC50_PATTERNS = [r"\b50\s*cc\b", r"\b50\s*phân\s*khối\b", r"\b50\s*phan\s*khoi\b"]


def classify_ad(ad: dict) -> list:
    """Возвращает метки: ликвидная модель и/или 50cc. Пустой список = обычное объявление."""
    title = ad["title"].lower()
    text = f"{ad['title']}\n{ad['description'][:300]}".lower()
    tags = []
    # Модель ищем только в названии: так меньше ложных срабатываний
    # (например, «меняю на Lead» в описании чужой модели).
    for name, patterns in PRIORITY_MODELS.items():
        if any(re.search(pt, title) for pt in patterns):
            tags.append(name)
    # 50cc ищем и в названии, и в начале описания.
    if any(re.search(pt, text) for pt in CC50_PATTERNS):
        tags.append("50cc")
    return tags


def is_suitable(ad: dict) -> bool:
    # Стоп-слова ищем только в названии и описании объявления, не по всей странице.
    haystack = f"{ad['title']}\n{ad['description']}".lower()
    if any(sw in haystack for sw in STOP_WORDS):
        return False
    if ad["price"] > MAX_PRICE_VND:
        return False
    return True


async def check_chotot_and_notify(chat_id: int, is_auto: bool = False):
    if is_auto and not user_notifications_active.get(chat_id, True):
        return

    if scan_lock.locked():
        if not is_auto:
            await bot.send_message(chat_id, "⏳ Сканирование уже идёт, подожди минуту.")
        return

    async with scan_lock:
        try:
            response = await asyncio.to_thread(fetch_url, CHOTOT_URL, 10)
            if response.status_code != 200:
                if not is_auto:
                    await bot.send_message(chat_id, f"⚠️ Сайт ответил с ошибкой: {response.status_code}")
                return

            found_urls = await asyncio.to_thread(extract_ad_links, response.text)
            total_found = len(found_urls)
            # Уже присланные и уже отклонённые фильтром повторно не скачиваем.
            fresh_urls = [u for u in found_urls if u not in sent_ads and u not in rejected_ads]

            sem = asyncio.Semaphore(3)
            results = await asyncio.gather(
                *(fetch_ad(u, sem) for u in fresh_urls[:MAX_ADS_PER_SCAN])
            )

            to_send = []

            for url, ad in results:
                if not ad:
                    continue  # не удалось загрузить, попробуем в следующий раз
                if not is_suitable(ad):
                    rejected_ads[url] = True
                    while len(rejected_ads) > 1000:
                        del rejected_ads[next(iter(rejected_ads))]
                    continue

                tags = classify_ad(ad)
                old = sent_details.get(url, {})
                sent_details[url] = {
                    "title": ad["title"],
                    "price": ad["price"],
                    "description": ad["description"],
                    "summary": old.get("summary"),
                    "tags": tags,
                }

                if url not in sent_ads:
                    sent_ads[url] = True
                    to_send.append((url, ad, tags))

            # Приоритетные байки идут первыми и приходят со звуком, остальные тихо.
            to_send.sort(key=lambda item: 0 if item[2] else 1)
            new_count = len(to_send)
            priority_count = sum(1 for _, _, t in to_send if t)

            for url, ad, tags in to_send:
                header = (
                    "🚨 <b>Приоритетный байк до 9 млн!</b>" if tags
                    else "🔥 <b>Новый байк до 9 млн!</b>"
                )
                tag_line = f"🏷 {esc(', '.join(tags))}\n" if tags else ""
                text = (
                    f"{header}\n"
                    f"{tag_line}"
                    f"🛵 {esc(ad['title'])}\n"
                    f"💰 {fmt_price(ad['price'])}"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    open_button(url),
                    [InlineKeyboardButton(text="🤖 Подробнее (ИИ-анализ)", callback_data=f"show_{uid(url)}")],
                    [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_{uid(url)}")],
                ])
                try:
                    await bot.send_message(
                        chat_id, text, parse_mode="HTML", reply_markup=kb,
                        disable_notification=not tags,
                    )
                except Exception as e:
                    logging.error(f"Не удалось отправить карточку {url}: {e}")
                await asyncio.sleep(0.5)

            trim_state()
            save_details()
            save_sent()

            last_check.update(
                time=time.time(), auto=is_auto, found=total_found, new=new_count, error=None
            )

            if not is_auto:
                if new_count > 0:
                    summary = (
                        f"✅ Готово! Новых байков: <b>{new_count}</b>"
                        + (f", из них приоритетных: <b>{priority_count}</b>" if priority_count else "")
                        + f".\nВсего ссылок на странице: {total_found}."
                    )
                else:
                    checked = len(fresh_urls[:MAX_ADS_PER_SCAN])
                    if checked == 0:
                        summary = f"😌 Новых объявлений нет: все {total_found} на странице уже известны."
                    else:
                        summary = (
                            f"😌 Новых подходящих байков нет. Проверено новых объявлений: {checked}, "
                            "но они не прошли фильтр (цена или стоп-слова)."
                        )
                await bot.send_message(
                    chat_id,
                    summary,
                    parse_mode="HTML",
                    reply_markup=get_keyboard(chat_id),
                )

        except Exception as e:
            logging.error(f"Ошибка при парсинг-цикле: {e}")
            last_check.update(time=time.time(), auto=is_auto, error=str(e)[:100])


# =====================================================================
# 6. ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# =====================================================================
async def auto_check_loop():
    while True:
        await asyncio.sleep(AUTO_CHECK_SECONDS)
        logging.info(f"Автопроверка: chat_id={MY_CHAT_ID}")
        if MY_CHAT_ID:
            try:
                await check_chotot_and_notify(MY_CHAT_ID, is_auto=True)
            except Exception as e:
                logging.error(f"Ошибка в автопроверке: {e}")


async def main():
    logging.info("Запуск веб-сервера для Render...")
    await start_web_server()

    logging.info("Инициализация бота...")
    await bot.delete_webhook(drop_pending_updates=True)
    await set_main_menu(bot)

    auto_task = asyncio.create_task(auto_check_loop())

    logging.info("Бот запущен и слушает Telegram...")
    try:
        await dp.start_polling(bot)
    finally:
        auto_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановлено.")
