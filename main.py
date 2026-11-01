import os
import asyncio
import logging
import json
import re
import requests
from bs4 import BeautifulSoup
from google import genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# =====================================================================
# 1. КЛЮЧИ И НАСТРОЙКИ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Не найдены ключи TELEGRAM_TOKEN или GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

SENT_ADS_FILE = "sent_ads.json"
FAV_ADS_FILE = "favorites.json"

def load_json_set(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_json_set(filepath, data_set):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(list(data_set), f)
    except Exception as e:
        logging.error(f"Ошибка сохранения файла {filepath}: {e}")

sent_ads = load_json_set(SENT_ADS_FILE)
favorite_ads = load_json_set(FAV_ADS_FILE)

# Используем точную ссылку с системным фильтром цены до 9 млн VND в Дананге
CHOTOT_URL = "https://www.chotot.com/da-nang/mua-ban-xe-may?price=0-9000000"

MY_CHAT_ID = None
user_notifications_active = {}

MAX_PRICE_VND = 9_000_000

# Стоп-слова для отсечения запчастей, аренды и прочего мусора
STOP_WORDS = ["phụ tùng", "thế chấp", "cho thuê", "cần mua", "thu mua", "đồ chơi", "cho thue", "phu tung"]


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
# 3. КНОПКИ И ОБРАБОТЧИКИ
# =====================================================================
async def set_main_menu(bot: Bot):
    main_commands = [
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="check", description="🔍 Проверить новые байки"),
        BotCommand(command="favorites", description="⭐ Избранные байки"),
    ]
    await bot.set_my_commands(main_commands)

def get_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    is_active = user_notifications_active.get(chat_id, True)
    toggle_text = "🔕 Выключить уведомления" if is_active else "🔔 Включить уведомления"
    
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить байки"), KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text=toggle_text)]
        ],
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    global MY_CHAT_ID
    MY_CHAT_ID = message.chat.id
    user_notifications_active[message.chat.id] = True
    
    await message.answer(
        "Привет, Олег! 🛵 Бот настроен на поиск ВСЕХ байков до 9 млн VND по вашей ссылке.\n\n"
        "🎯 <b>Критерии поиска:</b>\n"
        "• Источник: Официальный фильтр Chợ Tốt (до 9 млн)\n"
        "• Модели: Любые байки (Lead, Air Blade, Cub и др.)\n"
        "• Избранное и ИИ-анализ активны!",
        parse_mode="HTML",
        reply_markup=get_keyboard(message.chat.id)
    )

@dp.message(F.text == "🔍 Проверить байки")
async def button_check_handler(message: types.Message):
    await message.answer("🔍 Ищу все байки до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)

@dp.message(F.text == "⭐ Избранное")
@dp.message(Command("favorites"))
async def show_favorites_handler(message: types.Message):
    if not favorite_ads:
        await message.answer("⭐ У тебя пока нет сохраненных байков в избранном.", reply_markup=get_keyboard(message.chat.id))
        return
    
    await message.answer("⭐ <b>Твои сохраненные байки:</b>", parse_mode="HTML", reply_markup=get_keyboard(message.chat.id))
    
    for url in list(favorite_ads):
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить из избранного", callback_data=f"unfav_{hash(url)}")]
            ]
        )
        await message.answer(f"🔗 {url}", reply_markup=inline_kb)
        await asyncio.sleep(0.3)

@dp.message(F.text.in_(["🔕 Выключить уведомления", "🔔 Включить уведомления"]))
async def toggle_notifications_handler(message: types.Message):
    chat_id = message.chat.id
    current_state = user_notifications_active.get(chat_id, True)
    new_state = not current_state
    user_notifications_active[chat_id] = new_state
    
    if new_state:
        await message.answer("🔔 Уведомления включены! Бот снова будет присылать найденные варианты.", reply_markup=get_keyboard(chat_id))
    else:
        await message.answer("🔕 Уведомления выключены. Бот работает в тихом режиме (автопроверка идет, но сообщения приходить не будут).", reply_markup=get_keyboard(chat_id))

@dp.message(Command("check"))
async def manual_check_handler(message: types.Message):
    await message.answer("🔍 Ищу все байки до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)

@dp.callback_query(F.data.startswith("fav_"))
async def callback_favorite_handler(callback: types.CallbackQuery):
    h_str = callback.data.replace("fav_", "", 1)
    target_url = None
    for url in sent_ads:
        if str(hash(url)) == h_str:
            target_url = url
            break
            
    if target_url:
        if target_url not in favorite_ads:
            favorite_ads.add(target_url)
            save_json_set(FAV_ADS_FILE, favorite_ads)
            await callback.answer("⭐ Байк успешно добавлен в избранное!", show_alert=True)
        else:
            await callback.answer("⭐ Этот байк уже есть в избранном.", show_alert=True)
    else:
        await callback.answer("⚠️ Не удалось найти ссылку для сохранения.", show_alert=True)

@dp.callback_query(F.data.startswith("unfav_"))
async def callback_unfavorite_handler(callback: types.CallbackQuery):
    h_str = callback.data.replace("unfav_", "", 1)
    target_url = None
    for url in favorite_ads:
        if str(hash(url)) == h_str:
            target_url = url
            break
            
    if target_url and target_url in favorite_ads:
        favorite_ads.remove(target_url)
        save_json_set(FAV_ADS_FILE, favorite_ads)
        await callback.answer("🗑 Байк удален из избранного.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
    else:
        await callback.answer("Байк уже удален.", show_alert=True)

@dp.message()
async def chat_handler(message: types.Message):
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=message.text,
        )
        await message.answer(response.text)
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# =====================================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================
def extract_price_from_text(text: str) -> int:
    match_tr = re.search(r'(\d+[\.,]?\d*)\s*(tr|triệu|trieu)', text, re.IGNORECASE)
    if match_tr:
        num_str = match_tr.group(1).replace(',', '.')
        try:
            return int(float(num_str) * 1_000_000)
        except ValueError:
            pass

    match_tr_split = re.search(r'(\d+)tr(\d+)', text, re.IGNORECASE)
    if match_tr_split:
        try:
            whole = match_tr_split.group(1)
            frac = match_tr_split.group(2)
            return int(float(f"{whole}.{frac}") * 1_000_000)
        except ValueError:
            pass

    numbers = re.findall(r'\b\d[\d\.,]{6,}\b', text)
    for num_str in numbers:
        clean_num = re.sub(r'[^\d]', '', num_str)
        if clean_num:
            val = int(clean_num)
            if 1_000_000 <= val <= 99_000_000:
                return val

    return 0

def markdown_to_html(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'^\*\s+', r'• ', text, flags=re.MULTILINE)
    return text

async def analyze_ad_with_gemini(ad_text: str) -> str:
    prompt = (
        "Проанализируй текст объявления о продаже мопеда/скутера во Вьетнаме. "
        "Сделай краткую выжимку на русском языке:\n"
        "1. Точная модель, год и цена (обязательно укажи цену в миллионах донг или VND).\n"
        "2. Состояние техники, мотора и документов.\n"
        "3. Вердикт: стоит ли ехать смотреть.\n\n"
        f"Текст объявления:\n{ad_text}"
    )
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
        )
        return markdown_to_html(response.text)
    except Exception:
        return "Не удалось проанализировать описание."


# =====================================================================
# 5. ПАРСИНГ И ФИЛЬТРАЦИЯ
# =====================================================================
async def check_chotot_and_notify(chat_id: int, is_auto: bool = False):
    if is_auto and not user_notifications_active.get(chat_id, True):
        return

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(CHOTOT_URL, headers=headers, timeout=10)
        
        if response.status_code != 200:
            if not is_auto:
                await bot.send_message(chat_id, f"⚠️ Сайт ответил с ошибкой: {response.status_code}")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        ads = soup.find_all('a', href=True)
        
        found_ad_urls = []
        for ad in ads:
            href = ad['href']
            # Ищем любые персональные объявления на Chợ Tốt
            if "-li" in href and re.search(r'\d+\.htm$', href):
                ad_url = "https://www.chotot.com" + href if href.startswith("/") else href
                if ad_url not in found_ad_urls and ad_url != CHOTOT_URL:
                    found_ad_urls.append(ad_url)
        
        total_found_on_page = len(found_ad_urls)
        new_ads_count = 0

        for ad_url in found_ad_urls:
            if ad_url not in sent_ads:
                try:
                    sub_resp = requests.get(ad_url, headers=headers, timeout=5)
                    if sub_resp.status_code != 200:
                        continue
                    
                    sub_soup = BeautifulSoup(sub_resp.text, 'html.parser')
                    full_page_text = sub_soup.get_text()
                    full_page_text_lower = full_page_text.lower()
                    
                    if "mua ban xe" in full_page_text_lower and len(full_page_text) < 500:
                        sent_ads.add(ad_url)
                        save_json_set(SENT_ADS_FILE, sent_ads)
                        continue

                    # Отсекаем по стоп-словам (запчасти, аренда и т.д.)
                    if any(stop_word in full_page_text_lower for stop_word in STOP_WORDS):
                        sent_ads.add(ad_url)
                        save_json_set(SENT_ADS_FILE, sent_ads)
                        continue

                    desc_block = sub_soup.find('div', class_=lambda x: x and ('adBody' in x or 'description' in x.lower()))
                    if desc_block:
                        ad_description = desc_block.get_text(separator="\n")
                    else:
                        ad_description = full_page_text[:2000]
                    
                    detected_price = extract_price_from_text(full_page_text)

                    # Дополнительная страховка по цене (если в тексте окажется больше 9 млн)
                    if detected_price > 0 and detected_price > MAX_PRICE_VND:
                        sent_ads.add(ad_url)
                        save_json_set(SENT_ADS_FILE, sent_ads)
                        continue

                    sent_ads.add(ad_url)
                    save_json_set(SENT_ADS_FILE, sent_ads)
                    if len(sent_ads) > 500:
                        sent_ads.pop()
                        
                    new_ads_count += 1
                    ai_summary = await analyze_ad_with_gemini(ad_description)
                    price_info = f"💰 <b>Цена:</b> {detected_price:,} VND\n" if detected_price > 0 else "💰 <b>Цена:</b> См. в описании ниже\n"

                    text = (
                        f"🔥 <b>Новый байк до 9 млн на Chợ Tốt!</b>\n\n"
                        f"{price_info}"
                        f"🤖 <b>Анализ Gemini:</b>\n{ai_summary}\n\n"
                        f"🔗 {ad_url}"
                    )
                    
                    cb_data = f"fav_{hash(ad_url)}"
                    inline_kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="⭐ В избранное", callback_data=cb_data)]
                        ]
                    )
                    
                    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=inline_kb)
                    await asyncio.sleep(2)

                except Exception as sub_e:
                    logging.error(f"Ошибка при обработке {ad_url}: {sub_e}")
                    continue
                
        if not is_auto:
            if new_ads_count == 0:
                await bot.send_message(
                    chat_id, 
                    f"📊 <b>Результат проверки</b>:\n"
                    f"• Просканировано объявлений по фильтру: <b>{total_found_on_page}</b>\n"
                    f"• Новых подходящих: <b>0</b>\n\n"
                    f"<i>Все текущие варианты уже проверялись.</i>",
                    parse_mode="HTML",
                    reply_markup=get_keyboard(chat_id)
                )
            
    except Exception as e:
        logging.error(f"Ошибка при парсинге: {e}")


# =====================================================================
# 6. АВТОПРОВЕРКА И ГЛАВНЫЙ ЗАПУСК
# =====================================================================
async def auto_check_loop():
    while True:
        await asyncio.sleep(600)
        if MY_CHAT_ID:
            await check_chotot_and_notify(MY_CHAT_ID, is_auto=True)

async def main():
    print("Бот и веб-сервер запускаются...")
    await set
