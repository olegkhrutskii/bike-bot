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
SENT_TITLES_FILE = "sent_titles.json"  # {url: {"title": "...", "price": 123, "description": "..."}}

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
sent_details = load_json_data(SENT_TITLES_FILE, dict)  # {url: {"title": "...", "price": ..., "desc": "..."}}

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
        "🎯 <b>Возможности:</b>\n"
        "• Поиск всех байков до 9 млн VND\n"
        "• Компактный список проверенных вариантов при сканировании\n"
        "• Команда /reset для очистки истории памяти",
        parse_mode="HTML",
        reply_markup=get_keyboard(message.chat.id)
    )

@dp.message(F.text == "🔍 Проверить байки")
@dp.message(Command("check"))
async def manual_check_handler(message: types.Message):
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
        info = sent_details.get(url, {})
        title = info.get("title", "Байк из избранного")
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Показать детали", callback_data=f"show_{hash(url)}")],
                [InlineKeyboardButton(text="🗑 Удалить из избранного", callback_data=f"unfav_{hash(url)}")]
            ]
        )
        await message.answer(f"🛵 <b>{title}</b>", parse_mode="HTML", reply_markup=inline_kb)
        await asyncio.sleep(0.3)

@dp.message(Command("reset"))
async def reset_history_handler(message: types.Message):
    global sent_ads, sent_details
    sent_ads.clear()
    sent_details.clear()
    save_json_data(SENT_ADS_FILE, sent_ads)
    save_json_data(SENT_TITLES_FILE, sent_details)
    await message.answer("🔄 Память сброшена! При следующей проверке бот заново просканирует и пришлет все актуальные байки.", reply_markup=get_keyboard(message.chat.id))

@dp.message(F.text.in_(["🔕 Выключить уведомления", "🔔 Включить уведомления"]))
async def toggle_notifications_handler(message: types.Message):
    chat_id = message.chat.id
    current_state = user_notifications_active.get(chat_id, True)
    new_state = not current_state
    user_notifications_active[chat_id] = new_state
    
    if new_state:
        await message.answer("🔔 Уведомления включены!", reply_markup=get_keyboard(chat_id))
    else:
        await message.answer("🔕 Уведомления выключены (тихий режим).", reply_markup=get_keyboard(chat_id))

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
        price = target_info.get("price", 0)
        price_info = f"💰 <b>Цена:</b> {price:,} VND\n" if price > 0 else "💰 <b>Цена:</b> См. в описании ниже\n"
        ai_summary = target_info.get("summary", "Анализ недоступен.")
        
        text = (
            f"🔥 <b>Детали байка с Chợ Tốt:</b>\n\n"
            f"{price_info}"
            f"🤖 <b>Анализ Gemini:</b>\n{ai_summary}\n\n"
            f"🔗 {target_url}"
        )
        
        cb_data = f"fav_{hash(target_url)}"
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ В избранное", callback_data=cb_data)]
            ]
        )
        await callback.message.answer(text, parse_mode="HTML", reply_markup=inline_kb)
        await callback.answer("Готово!")
    else:
        await callback.answer("⚠️ Информация по этому байку не найдена.", show_alert=True)

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
            await callback.answer("⭐ Байк успешно добавлен в избранное!", show_alert=True)
        else:
            await callback.answer("⭐ Этот байк уже есть в избранном.", show_alert=True)
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
            model='gemini-3.5
