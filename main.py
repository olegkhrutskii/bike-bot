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

# =====================================================================
# 1. КЛЮЧИ И НАСТРОЙКИ (БЕРЕМ ИЗ СЕКРЕТНЫХ ПЕРЕМЕННЫХ ОБЛАКА)
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Проверка, что ключи на месте
if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Ой! Не найдены ключи TELEGRAM_TOKEN или GEMINI_API_KEY в настройках окружения.")

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# (дальше идет весь твой остальной код без изменений: функции, парсинг, запуск...)

logging.basicConfig(level=logging.INFO)

sent_ads = set()
CHOTOT_URL = "https://www.chotot.com/da-nang/mua-ban-xe-may/50cc"
MY_CHAT_ID = None

# НАСТРОЙКИ ФИЛЬТРОВ
MAX_PRICE_VND = 9_000_000  # Строгий лимит: до 9 млн донгов
ALLOWED_KEYWORDS = ["50cc", "lead", "air blade", "airblade", "cub", "scoopy", "sym", "kymco", "today", "giorno"]
STOP_WORDS = ["phụ tùng", "thế chấp", "cho thuê", "cần mua", "thu mua", "đồ chơi"]


# =====================================================================
# 2. КНОПКА «МЕНЮ» У ПОЛЯ ВВОДА
# =====================================================================
async def set_main_menu(bot: Bot):
    main_commands = [
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="check", description="🔍 Проверить новые байки"),
    ]
    await bot.set_my_commands(main_commands)


# =====================================================================
# 3. ОБРАБОТЧИКИ КОМАНД И КНОПОК
# =====================================================================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    global MY_CHAT_ID
    MY_CHAT_ID = message.chat.id
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить байки")]
        ],
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


# =====================================================================
# 4. ОБРАБОТЧИК ИИ ДЛЯ ОБЫЧНЫХ СООБЩЕНИЙ
# =====================================================================
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
        await message.answer(f"Ошибочка вышла: {e}")


# =====================================================================
# 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ПАРСИНГ ЦЕНЫ И АНАЛИЗ)
# =====================================================================
def extract_price_from_text(text: str) -> int:
    """Пытается найти цену в тексте объявления (например, 8.5 triệu, 8,500,000 đ)"""
    # Ищем паттерны с миллионами (например, "8.5 tr", "9 triệu")
    match_tr = re.search(r'(\d+[\.,]?\d*)\s*(tr|triệu|trieu)', text, re.IGNORECASE)
    if match_tr:
        num_str = match_tr.group(1).replace(',', '.')
        try:
            return int(float(num_str) * 1_000_000)
        except ValueError:
            pass

    # Ищем полные суммы цифрами (например, 8500000 или 9.000.000)
    numbers = re.findall(r'\b\d[\d\.,]{5,}\b', text)
    for num_str in numbers:
        clean_num = re.sub(r'[^\d]', '', num_str)
        if clean_num:
            val = int(clean_num)
            # Если число похоже на цену байка (от 1 до 99 миллионов)
            if 1_000_000 <= val <= 99_000_000:
                return val
                
    return 0  # Если цену не нашли

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
# 6. ПАРСИНГ И СТРОГАЯ ФИЛЬТРАЦИЯ CHỢ TỐT
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
                        
                        # 1. Проверка на стоп-слова
                        if any(stop_word in full_page_text for stop_word in STOP_WORDS):
                            sent_ads.add(ad_url)
                            continue
                            
                        # 2. Проверка ключевых слов (модели)
                        if not any(keyword in full_page_text for keyword in ALLOWED_KEYWORDS):
                            sent_ads.add(ad_url)
                            continue

                        # 3. Извлечение и проверка цены
                        desc_block = sub_soup.find('div', class_=lambda x: x and 'adBody' in x)
                        ad_description = desc_block.get_text() if desc_block else "Описание отсутствует"
                        
                        # Собираем весь текст для поиска цены (включая заголовок и описание)
                        price_source_text = sub_soup.get_text()
                        detected_price = extract_price_from_text(price_source_text)

                        # Если цену нашли и она БОЛЬШЕ 9 млн — пропускаем объявление
                        if detected_price > 0 and detected_price > MAX_PRICE_VND:
                            sent_ads.add(ad_url) # Запоминаем, чтобы не проверять повторно
                            continue

                        # Помечаем как обработанное
                        sent_ads.add(ad_url)
                        if len(sent_ads) > 500:
                            sent_ads.pop()
                            
                        new_ads_count += 1

                        # Просим Gemini сделать выжимку
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
                        logging.error(f"Ошибка при обработке объявления {ad_url}: {sub_e}")
                        continue
                    
        if new_ads_count == 0 and not is_auto:
            await bot.send_message(chat_id, "📭 Новых вариантов строго до 9 млн VND с нужными моделями пока нет.")
            
    except Exception as e:
        logging.error(f"Ошибка при парсинге списка: {e}")


# =====================================================================
# 7. АВТОПРОВЕРКА КАЖДЫЕ 10 МИНУТ
# =====================================================================
async def auto_check_loop():
    while True:
        await asyncio.sleep(600)  # 10 минут
        if MY_CHAT_ID:
            await check_chotot_and_notify(MY_CHAT_ID, is_auto=True)


# =====================================================================
# 8. ГЛАВНЫЙ ЗАПУСК С ЗАЩИТОЙ ОТ ОБРЫВОВ СЕТИ
# =====================================================================
async def main():
    print("Бот запущен с фильтром по цене до 9 млн...")
    await set_main_menu(bot)
    asyncio.create_task(auto_check_loop())
    
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Связь с Telegram прервалась: {e}. Переподключение через 3 сек...")
            await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен вручную.")