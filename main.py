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

MAX_PRICE_VND = 9_000_000
ALLOWED_KEYWORDS = ["50cc", "lead", "air blade", "airblade", "cub", "scoopy", "sym", "kymco", "today", "giorno"]
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
# 3. КНОПКА «МЕНЮ» И ОБРАБОТЧИКИ
# =====================================================================
async def set_main_menu(bot: Bot):
    main_commands = [
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="check", description="🔍 Проверить новые байки"),
    ]
    await bot.set_my_commands(main_commands)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    global MY_CHAT_ID
    MY_CHAT_ID = message.chat.id
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔍 Проверить байки")]],
        resize_keyboard=True
    )
    
    await message.answer(
        "Привет, Олег! 🛵 Жесткий фильтр по цене включен.\n\n"
        "🎯 **Критерии поиска**:\n"
        "• Цена: СТРОГО до 9 млн VND\n"
        "• Модели: 50cc, Honda Lead, Honda Air Blade (и др.)\n"
        "• ИИ-анализ каждого подходящего объявления активен!",
        reply_markup=keyboard
    )

@dp.message(F.text == "🔍 Проверить байки")
async def button_check_handler(message: types.Message):
    await message.answer("🔍 Ищу байки строго до 9 млн VND...")
    await check_chotot_and_notify(message.chat.id)

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
        return response.text
    except Exception:
        return "Не удалось проанализировать описание."


# =====================================================================
# 5. ПАРСИНГ И ФИЛЬТРАЦИЯ
# =====================================================================
async def check_chotot_and_notify(chat_id: int, is_auto: bool = False):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(CHOTOT_URL, headers=headers)
        if response.status_code != 200:
            if not is_auto:
                await bot.send_message(chat_id, f"⚠️ Сайт ответил с ошибкой: {response.status_code}")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        ads = soup.find_all('a', href=True)
        
        new_ads_count = 0
        for ad in ads:
            href = ad['href']
            if "/da-nang/mua-ban-xe-may/" in href and "-li" in href:
                ad_url = "https://www.chotot.com" + href if href.startswith("/") else href
                
                if ad_url not in sent_ads:
                    try:
                        sub_resp = requests.get(ad_url, headers=headers, timeout=5)
                        if sub_resp.status_code != 200:
                            continue
                        
                        sub_soup = BeautifulSoup(sub_resp.text, 'html.parser')
                        full_page_text = sub_soup.get_text().lower()
                        
                        if any(stop_word in full_page_text for stop_word in STOP_WORDS):
                            sent_ads.add(ad_url)
                            continue
                            
                        if not any(keyword in full_page_text for keyword in ALLOWED_KEYWORDS):
                            sent_ads.add(ad_url)
                            continue

                        desc_block = sub_soup.find('div', class_=lambda x: x and 'adBody' in x)
                        ad_description = desc_block.get_text() if desc_block else "Описание отсутствует"
                        
                        detected_price = extract_price_from_text(sub_soup.get_text())

                        if detected_price > 0 and detected_price > MAX_PRICE_VND:
                            sent_ads.add(ad_url)
                            continue

                        sent_ads.add(ad_url)
                        if len(sent_ads) > 500:
                            sent_ads.pop()
                            
                        new_ads_count += 1
                        ai_summary = await analyze_ad_with_gemini(ad_description)
                        price_info = f"💰 **Цена:** {detected_price:,} VND\n" if detected_price > 0 else "💰 **Цена:** Уточняйте в объявлении\n"

                        text = (
                            f"🔥 **Подходящий байк (до 9 млн) на Chợ Tốt!**\n\n"
                            f"{price_info}"
                            f"🤖 **Анализ Gemini:**\n{ai_summary}\n\n"
                            f"🔗 {ad_url}"
                        )
                        await bot.send_message(chat_id, text)
                        await asyncio.sleep(2)

                    except Exception as sub_e:
                        logging.error(f"Ошибка при обработке {ad_url}: {sub_e}")
                        continue
                    
        if new_ads_count == 0 and not is_auto:
            await bot.send_message(chat_id, "📭 Новых вариантов строго до 9 млн VND с нужными моделями пока нет.")
            
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
    await start_web_server()  # Запускаем мини веб-сервер для Render
    async def main():
    print("Бот и веб-сервер запускаются...")
    await set_main_menu(bot)
    
    # Запускаем веб-сервер параллельно в фоне, чтобы Render был доволен
    asyncio.create_task(start_web_server())
    
    # Запускаем автопроверку байков
    asyncio.create_task(auto_check_loop())
    
    # Запуск самого бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
