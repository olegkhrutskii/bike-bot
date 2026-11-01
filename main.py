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
SENT_TITLES_FILE = "sent_titles.json"

def load_json_data(filepath, default_type):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return default_type(data)
        except Exception:
            pass
    return default_type()

def save_json_data(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(list(data) if isinstance(data, set) else data, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Ошибка сохранения файла {filepath}: {e}")

sent_ads = load_json_data(SENT_ADS_FILE, set)
favorite_ads = load_json_data(FAV_ADS_FILE, set)
sent_details = load_json_data(SENT_TITLES_FILE, dict)

CHOTOT_URL = "https://www.chotot.com/da-nang/mua-ban-xe-may?price=0-9000000"

MY_CHAT_ID = None
user_notifications_active = {}
MAX_PRICE_VND = 9_000_000
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
        BotCommand(command="reset", description="🔄 Сбросить память"),
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
        "Привет, Олег! 🛵 Бот готов к работе.\n\n"
        "🎯 <b>Особенности:</b>\n"
        "• Быстрое сканирование без задержек\n"
        "• ИИ-анализ по клику на кнопку «🤖 Подробнее»\n"
        "• Удобное избранное",
        parse_mode="HTML",
        reply_markup=get_keyboard(message.chat.id)
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
        await message.answer("⭐ У тебя пока нет сохраненных байков в избранном.", reply_markup=get_keyboard(message.chat.id))
        return
    
    await message.answer("⭐ <b>Твои сохраненные байки:</b>", parse_mode="HTML", reply_markup=get_keyboard(message.chat.id))
    
    for url in list(favorite_ads):
        info = sent_details.get(url, {})
        title = info.get("title", "Байк из избранного")
        price = info.get("price", 0)
        price_str = f"{price:,} VND" if price > 0 else "Цена в описании"
        
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Подробнее (ИИ-анализ)", callback_data=f"show_{hash(url)}")],
                [InlineKeyboardButton(text="🗑 Удалить из избранного", callback_data=f"unfav_{hash(url)}")]
            ]
        )
        await message.answer(f"🛵 <b>{title}</b>\n💰 {price_str}", parse_mode="HTML", reply_markup=inline_kb)
        await asyncio.sleep(0.2)

@dp.message(Command("reset"))
async def reset_history_handler(message: types.Message):
    global sent_ads, sent_details
    sent_ads.clear()
    sent_details.clear()
    save_json_data(SENT_ADS_FILE, sent_ads)
    save_json_data(SENT_TITLES_FILE, sent_details)
    await message.answer("🔄 Память сброшена!", reply_markup=get_keyboard(message.chat.id))

@dp.message(F.text.in_(["🔕 Выключить уведомления", "🔔 Включить уведомления"]))
async def toggle_notifications_handler(message: types.Message):
    chat_id = message.chat.id
    current_state = user_notifications_active.get(chat_id, True)
    new_state = not current_state
    user_notifications_active[chat_id] = new_state
    
    if new_state:
        await message.answer("🔔 Уведомления включены!", reply_markup=get_keyboard(chat_id))
    else:
        await message.answer("🔕 Уведомления выключены.", reply_markup=get_keyboard(chat_id))

@dp.callback_query(F.data.startswith("show_"))
async def callback_show_details_handler(callback: types.CallbackQuery):
    h_str = callback.data.replace("show_", "", 1)
    target_url = None
    target_info = None
    for url, info in sent_details.items():
        if str(hash(url)) == h_str:
            target_url = url
            target_info = info
            break
            
    if target_url and target_info:
        await callback.answer("🤖 Генерирую анализ Gemini...")
        
        # Если анализ еще не делался, запрашиваем его у ИИ прямо сейчас по клику
        ai_summary = target_info.get("summary")
        if not ai_summary:
            ai_summary = await analyze_ad_with_gemini(target_info.get("description", ""))
            target_info["summary"] = ai_summary
            save_json_data(SENT_TITLES_FILE, sent_details)

        price = target_info.get("price", 0)
        price_info = f"💰 <b>Цена:</b> {price:,} VND\n" if price > 0 else "💰 <b>Цена:</b> См. в описании\n"
        
        text = (
            f"🔥 <b>Детали байка:</b>\n\n"
            f"{price_info}"
            f"🤖 <b>Анализ Gemini:</b>\n{ai_summary}\n\n"
            f"🔗 {target_url}"
        )
        
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_{hash(target_url)}")]
            ]
        )
        await callback.message.answer(text, parse_mode="HTML", reply_markup=inline_kb)
    else:
        await callback.answer("⚠️ Информация не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("fav_"))
async def callback_favorite_handler(callback: types.CallbackQuery):
    h_str = callback.data.replace("fav_", "", 1)
    target_url = None
    for url in sent_details:
        if str(hash(url)) == h_str:
            target_url = url
            break
            
    if target_url:
        if target_url not in favorite_ads:
            favorite_ads.add(target_url)
            save_json_data(FAV_ADS_FILE, favorite_ads)
            await callback.answer("⭐ Добавлено в избранное!", show_alert=True)
        else:
            await callback.answer("⭐ Уже в избранном.", show_alert=True)
    else:
        await callback.answer("⚠️ Ссылка не найдена.", show_alert=True)

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
        save_json_data(FAV_ADS_FILE, favorite_ads)
        await callback.answer("🗑 Удалено из избранного.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass

@dp.message()
async def chat_handler(message: types.Message):
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, 
            lambda: client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=message.text,
            )
        )
        await message.answer(response.text)
    except Exception as e:
        await message.answer(f"Ошибка ответа: {e}")


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

def fetch_url(url: str, timeout: int = 5):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    return requests.get(url, headers=headers, timeout=timeout)

async def analyze_ad_with_gemini(ad_text: str) -> str:
    prompt = (
        "Проанализируй текст объявления о продаже мопеда/скутера во Вьетнаме.\n"
        "Напиши краткую выжимку на русском языке:\n"
        "1. Точная модель, год и цена (в миллионах донг или VND).\n"
        "2. Состояние техники, мотора и документов.\n"
        "3. Вердикт: стоит ли ехать смотреть.\n\n"
        f"Текст объявления:\n{ad_text}"
    )
    try:
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None, 
                lambda: client.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=prompt,
                )
            ),
            timeout=15.0
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
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: fetch_url(CHOTOT_URL, 10))
        
        if response.status_code != 200:
            if not is_auto:
                await bot.send_message(chat_id, f"⚠️ Сайт ответил с ошибкой: {response.status_code}")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        ads = soup.find_all('a', href=True)
        
        found_ad_urls = []
        for ad in ads:
            href = ad['href']
            if "-li" in href and re.search(r'\d+\.htm$', href):
                ad_url = "https://www.chotot.com" + href if href.startswith("/") else href
                if ad_url not in found_ad_urls and ad_url != CHOTOT_URL:
                    found_ad_urls.append(ad_url)
        
        total_found_on_page = len(found_ad_urls)
        new_ads_count = 0
        current_batch_scanned = []

        for ad_url in found_ad_urls[:10]:
            try:
                sub_resp = await loop.run_in_executor(None, lambda: fetch_url(ad_url, 5))
                if sub_resp.status_code != 200:
                    continue
                
                sub_soup = BeautifulSoup(sub_resp.text, 'html.parser')
                full_page_text = sub_soup.get_text()
                full_page_text_lower = full_page_text.lower()
                
                if "mua ban xe" in full_page_text_lower and len(full_page_text) < 500:
                    continue

                if any(stop_word in full_page_text_lower for stop_word in STOP_WORDS):
                    continue

                desc_block = sub_soup.find('div', class_=lambda x: x and ('adBody' in x or 'description' in x.lower()))
                ad_description = desc_block.get_text(separator="\n") if desc_block else full_page_text[:2000]
                
                detected_price = extract_price_from_text(full_page_text)

                if detected_price > 0 and detected_price > MAX_PRICE_VND:
                    continue

                # Достаем заголовок прямо со страницы (без обращения к Gemini при сканировании)
                title_tag = sub_soup.find('h1')
                clean_title = title_tag.get_text().strip() if title_tag else "Байк до 9 млн VND"
                clean_title = clean_title[:60]
                
                current_batch_scanned.append((ad_url, clean_title, detected_price))

                # Сохраняем сырые данные для отложенного анализа по кнопке
                sent_details[ad_url] = {
                    "title": clean_title,
                    "price": detected_price,
                    "description": ad_description,
                    "summary": None # Сгенерируется по клику
                }

                if ad_url not in sent_ads:
                    sent_ads.add(ad_url)
                    new_ads_count += 1
                    
                    price_str = f"{detected_price:,} VND" if detected_price > 0 else "Цена в описании"
                    text = f"🔥 <b>Новый байк до 9 млн!</b>\n🛵 {clean_title}\n💰 {price_str}\n🔗 {ad_url}"
                    
                    inline_kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🤖 Подробнее (ИИ-анализ)", callback_data=f"show_{hash(ad_url)}")],
                            [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_{hash(ad_url)}")]
                        ]
                    )
                    
                    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=inline_kb)
                    await asyncio.sleep(0.5)

                save_json_data(SENT_TITLES_FILE, sent_details)
                save_json_data(SENT_ADS_FILE, sent_ads)

            except Exception as sub_e:
                logging.error(f"Ошибка при обработке {ad_url}: {sub_e}")
                continue
                
        if len(sent_ads) > 500:
            sent_ads.pop()

        if not is_auto:
            report_text = (
                f"📊 <b>Результат проверки:</b>\n"
                f"• Просканировано объявлений по фильтру: <b>{total_found_on_page}</b>\n"
                f"• Новых подходящих отправлено: <b>{new_ads_count}</b>\n\n"
                f"📜 <b>Все текущие варианты на странице:</b>"
            )
            
            await bot.send_message(chat_id, report_text, parse_mode="HTML", reply_markup=get_keyboard(chat_id))

            for url, title, price in current_batch_scanned:
                price_str = f"{price:,} VND" if price > 0 else "Цена в описании"
                inline_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🤖 Подробнее", callback_data=f"show_{hash(url)}")]
                    ]
                )
                await bot.send_message(chat_id, f"🛵 <b>{title}</b>\n💰 {price_str}", parse_mode="HTML", reply_markup=inline_kb)
                await asyncio.sleep(0.2)
            
    except Exception as e:
        logging.error(f"Ошибка при парсинг-цикле: {e}")


# =====================================================================
# 6. ФОНОВЫЕ ЗАДАЧИ И ГЛАВНЫЙ ЗАПУСК
# =====================================================================
async def auto_check_loop():
    while True:
        await asyncio.sleep(600)
        if MY_CHAT_ID:
            try:
                await check_chotot_and_notify(MY_CHAT_ID, is_auto=True)
            except Exception as e:
                logging.error(f"Ошибка в автопроверке: {e}")

async def main():
    print("Запуск веб-сервера для Render...")
    asyncio.create_task(start_web_server())
    
    print("Инициализация бота...")
    await set_main_menu(bot)
    
    asyncio.create_task(auto_check_loop())
    
    print("Бот запущен и слушает Telegram...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановлено.")
