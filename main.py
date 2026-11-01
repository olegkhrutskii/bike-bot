import os
import asyncio
import logging
import re
import requests
from bs4 import BeautifulSoup
from google import genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, ReplyKeyboardMarkup, KeyboardButton
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

sent_ads = set()
CHOTOT_URL = "https://www.chotot.com/da-nang/mua-ban-xe-may/50cc"
MY_CHAT_ID = None

user_notifications_active = {}

MAX_PRICE_VND = 9_000_000
# Удалили общий "sym", чтобы не пропускать 125-кубовые Attila и т.д.
ALLOWED_KEYWORDS = ["50cc", "lead", "air blade", "airblade", "cub", "scoopy", "kymco", "today", "giorno", "sym 50"]
STOP_WORDS = ["phụ tùng", "thế chấp", "cho thuê", "cần mua", "thu mua", "đồ chơi"]


# =====================================================================
# 2. МИНИ-ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО ТАРИФА RENDER
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
    ]
    await bot.set_my_commands(main_commands)

def get_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    is_active = user_notifications_active.get(chat_id, True)
    toggle_text = "🔕 Выключить уведомления" if is_active else "🔔 Включить уведомления"
    
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить байки")],
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
        "Привет, Олег! 🛵 Жесткий фильтр по цене включен.\n\n"
        "🎯 **Критерии поиска**:\n"
        "• Цена: СТРОГО до 9 млн VND\n"
        "• Модели: 50cc, Honda Lead, Honda Air Blade (и др.)\n"
        "• ИИ-анализ каждого подходящего объявления активен!",
        reply_markup=get_keyboard(message.chat.id)
    )

@dp.message(F.text == "🔍 Проверить байки")
async def button_check_handler(message: types.Message):
    await message.answer("🔍 Ищу байки строго до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)

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
    await message.answer("🔍 Ищу байки строго до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)

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

    numbers = re.findall(r'\b\d[\d\.,]{5,}\b', text)
    for num_str in numbers:
        clean_num = re.sub(r'[^\d]', '', num_str)
        if clean_num:
            val = int(clean_num)
            if 1_000_000 <= val <= 99_000_000:
                return val
    return 0

def markdown_to_html(text: str) -> str:
    # Конвертируем маркдаун звездочки Gemini в HTML-теги для красивого жирного текста в Телеграме
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    return text

async def analyze_ad_with_gemini(ad_text: str) -> str:
    prompt = (
        "Проанализируй текст объявления о продаже мопеда/скутера во Вьетнаме. "
        "Сделай краткую выжимку на русском языке:\n"
        "1. Точная модель, год и цена.\n"
        "2. Состояние техники, мотора и документов.\n"
        "3. Стоит ли ехать смотреть.\n\n"
        f"Текст объявления:\n{ad_text}"
    )
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
        )
        # Очищаем от звездочек маркдауна перед выдачей
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
            if "-li" in href or re.search(r'\d+\.htm$', href):
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
                        continue

                    if any(stop_word in full_page_text_lower for stop_word in STOP_WORDS):
                        sent_ads.add(ad_url)
                        continue
                        
                    if not any(keyword in full_page_text_lower for keyword in ALLOWED_KEYWORDS):
                        sent_ads.add(ad_url)
                        continue

                    desc_block = sub_soup.find('div', class_=lambda x: x and ('adBody' in x or 'description' in x.lower()))
                    if desc_block:
                        ad_description = desc_block.get_text(separator="\n")
                    else:
                        ad_description = full_page_text[:2000]
                    
                    detected_price = extract_price_from_text(full_page_text)

                    if detected_price > 0 and detected_price > MAX_PRICE_VND:
                        sent_ads.add(ad_url)
                        continue

                    sent_ads.add(ad_url)
                    if len(sent_ads) > 500:
                        sent_ads.pop()
                        
                    new_ads_count += 1
                    ai_summary = await analyze_ad_with_gemini(ad_description)
                    price_info = f"💰 <b>Цена:</b> {detected_price:,} VND\n" if detected_price > 0 else "💰 <b>Цена:</b> Уточняйте в объявлении\n"

                    text = (
                        f"🔥 <b>Подходящий байк (до 9 млн) на Chợ Tốt!</b>\n\n"
                        f"{price_info}"
                        f"🤖 <b>Анализ Gemini:</b>\n{ai_summary}\n\n"
                        f"🔗 {ad_url}"
                    )
                    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=get_keyboard(chat_id))
                    await asyncio.sleep(2)

                except Exception as sub_e:
                    logging.error(f"Ошибка при обработке {ad_url}: {sub_e}")
                    continue
                
        if not is_auto:
            if new_ads_count == 0:
                await bot.send_message(
                    chat_id, 
                    f"📊 <b>Результат проверки</b>:\n"
                    f"• Просканировано объявлений на странице: <b>{total_found_on_page}</b>\n"
                    f"• Новых подходящих (до 9 млн VND и нужных моделей): <b>0</b>\n\n"
                    f"<i>Все текущие варианты либо уже проверялись, либо не прошли фильтр цены/модели.</i>",
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
    await set_main_menu(bot)
    
    asyncio.create_task(start_web_server())
    asyncio.create_task(auto_check_loop())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
