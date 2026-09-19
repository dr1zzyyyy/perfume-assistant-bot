import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder
import aiosqlite
import httpx
import re
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= КОНФИГ =================
BOT_TOKEN = "ваш_токен_telegram_бота"
GROQ_API_KEY = "ваш_токен_с_https://console.groq.com/home"
DB_NAME = "perfume_bot.db"

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Клиент Groq (OpenAI-совместимый)
ai_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
logging.basicConfig(level=logging.INFO)

# ================= FSM (СОСТОЯНИЯ) =================
class SetupState(StatesGroup):
    waiting_for_collection = State()
    waiting_for_city = State()
    waiting_for_single_perfume = State()

# ================= БАЗА ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                city TEXT,
                collection TEXT,
                auto_notify INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def save_user_data(user_id: int, city: str = None, collection: str = None, auto_notify: int = None):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT city, collection, auto_notify FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        
        if row:
            new_city = city if city is not None else row[0]
            new_coll = collection if collection is not None else row[1]
            new_notify = auto_notify if auto_notify is not None else row[2]
            await db.execute(
                "UPDATE users SET city = ?, collection = ?, auto_notify = ? WHERE user_id = ?",
                (new_city, new_coll, new_notify, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO users (user_id, city, collection, auto_notify) VALUES (?, ?, ?, ?)",
                (user_id, city, collection, auto_notify or 0)
            )
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT city, collection, auto_notify FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

# ================= КЛАВИАТУРЫ =================

def get_main_kb(auto_notify: int = 0):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🌤 Подобрать аромат на день")
    kb.button(text="🛍 Что докупить в коллекцию")
    kb.button(text="➕ Добавить аромат")
    kb.button(text="🧴 Моя полка")
    notify_text = "🔔 Автоматическая отправка рекомендаций: ВКЛ" if auto_notify else "🔕 Автоматическая отправка рекомендаций: ВЫКЛ"
    kb.button(text=notify_text)
    kb.button(text="⚙️ Сбросить профиль")
    kb.adjust(1, 1, 2, 2)
    return kb.as_markup(resize_keyboard=True)
# ================= ПОГОДА (Open-Meteo) =================
async def get_weather(city: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        geo_res = await client.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ru&format=json"
        )
        geo_data = geo_res.json()
        if not geo_data.get("results"):
            return "Не удалось определить погоду (проверь название города)."
        
        lat = geo_data["results"][0]["latitude"]
        lon = geo_data["results"][0]["longitude"]
        resolved_name = geo_data["results"][0]["name"]

        weather_res = await client.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto"
        )
        w = weather_res.json()
        
        current_temp = w["current"]["temperature_2m"]
        humidity = w["current"]["relative_humidity_2m"]
        min_temp = w["daily"]["temperature_2m_min"][0]
        max_temp = w["daily"]["temperature_2m_max"][0]
        rain_prob = w["daily"]["precipitation_probability_max"][0]

        return (
            f"Город: {resolved_name}\n"
            f"• Сейчас: {current_temp}°C (влажность {humidity}%)\n"
            f"• В течение дня: от {min_temp}°C до {max_temp}°C\n"
            f"• Вероятность дождя: {rain_prob}%"
        )

# ================= ФОРМАТИРОВАНИЕ HTML =================
def format_to_html(text: str) -> str:
    # Заменяем звездочки списков на аккуратные буллеты
    text = re.sub(r'(?m)^\s*[\*\-]\s+', '• ', text)
    # **жирный** -> <b>жирный</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # *курсив* -> <i>курсив</i>
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    # Убираем решетки markdown-заголовков
    text = re.sub(r'(?m)^#+\s*', '', text)
    return text

# ================= СОВЕТ ЧЕРЕЗ GROQ (QWEN) =================
async def generate_perfume_advice(collection: str, weather_report: str) -> str:
    system_prompt = (
        "Ты — топовый парфюмерный эксперт, стилист и близкий друг пользователя с безупречным носом и тонким вкусом.\n\n"
        "ТВОИ ЖЕЛЕЗНЫЕ ПРАВИЛА:\n"
        "1. ВЫБОР: Выбирай ароматы СТРОГО и ИСКЛЮЧИТЕЛЬНО из переданного списка пользователя. Никогда не предлагай ничего со стороны.\n"
        "2. РЕАЛИЗМ И ПИРАМИДЫ: Оценивай характер ароматов честно. Никаких розовых фантазий! Не называй дымную, кожаную или смолистую нишу (вроде Orto Parisi, Nasomatto, BeauFort) «уютным сладким свитерочком с ванилькой». Если аромат брутальный, горелый, анималистичный или ядовито-стойкий — предупреждай прямо.\n"
        "3. КАЛИБРОВКА ПО ПОГОДЕ:\n"
        "   - Выше +15...+17°C: Тяжелая амбра, дым, уд, гурманика и смолы быстро начинают душить. В такую погоду отдавай приоритет свежим, цитрусовым, минеральным, чайным или легким древесно-пряным композициям. Тяжеляки советуй только если на улице шторм/дождь или на поздний вечер на открытом воздухе.\n"
        "   - Ниже +10...+12°C: Идеальное время для плотных, согревающих, кожаных и смолистых флаконов.\n"
        "4. АДЕКВАТНАЯ ДОЗИРОВКА:\n"
        "   - Ядерная ниша / Экстракты (Orto Parisi, Megamare, Terroni, Black Afgano, Interlude и т.д.): 1-2-3 пшика (под одежду, на торс или затылок). Наносить такое за уши или поливаться 3 пшиками — ольфакторный терроризм.\n"
        "   - Плотная парфюмерная вода (EDP) / тяжелые арабы: 2, максимум 3 пшика.\n"
        "   - Свежаки, цитрусы, туалетки (EDT): 3–5 пшиков.\n"
        "5. ФОРМАТИРОВАНИЕ: Пиши живо, емко, с легкой иронией и эмодзи. Используй ТОЛЬКО HTML-теги (<b>жирный</b>, <i>курсив</i>). Никакого markdown (*, **, #)."
    )

    user_prompt = f"""Погода за окном:
{weather_report}

Мой гардероб ароматов (выбирай СТРОГО из них):
{collection}

Сделай экспертный, но краткий расклад на день:
1. <b>Фаворит на день</b>: точное название из списка и краткое, но экспертное объяснение, как именно пирамида раскроется при этой температуре и влажности.
2. <b>Альтернатива</b>: запасной вариант из списка (на вечер, спорт или смену настроения). Тоже кратко
3. <b>Дозировка и нанесение</b>: адекватное количество пшиков с учетом плотности парфюма и конкретные зоны нанесения, чтобы не задушить себя и людей в помещении.
"""

    try:
        response = await ai_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=1200
        )
        raw_text = response.choices[0].message.content
        return format_to_html(raw_text)
    except Exception as e:
        logging.error(f"Groq API error: {e}")
        return "Что-то нейросеть задумалась... Попробуй нажать кнопку еще разок!"

# ================= ПОДБОР НОВЫХ АРОМАТОВ К ПОКУПКЕ =================
async def generate_shopping_recommendations(collection: str, weather_report: str) -> str:
    system_prompt = (
        "Ты — топовый парфюмерный байер, консультант и стилист с глубоким знанием нот, пирамид и брендов.\n\n"
        "ТВОЯ ЗАДАЧА:\n"
        "1. Проанализировать текущую коллекцию пользователя: определить его вкусовые векторы (любимые ноты, направления, тяжесть звучания).\n"
        "2. Учесть текущую погоду и сезон в его городе.\n"
        "3. Предложить ровно 3–4 реальных флакона К ПОКУПКЕ, которых ЕЩЁ НЕТ в его коллекции.\n\n"
        "ПРАВИЛА ПОДБОРА:\n"
        "• СТРОГО запрещено предлагать ароматы, которые уже есть в списке пользователя.\n"
        "• Баланс подборки: 2 аромата в привычном для него любимом стиле под текущую погоду + 1–2 аромата, которые закроют пробел в гардеробе (например, если не хватает свежего шлейфа в офис или стойкой кожи на прохладный вечер).\n"
        "• Никаких абстрактных советов — только конкретные названия и реальные флаконы (ниша, хороший люкс или топ-арабы).\n"
        "• ФОРМАТИРОВАНИЕ: используй ТОЛЬКО HTML (<b>жирный</b>, <i>курсив</i>). Никаких markdown-звёздочек (** или *)."
    )

    user_prompt = f"""Погода и сезон сейчас:
{weather_report}

Моя текущая коллекция:
{collection}

Подбери 3–4 аромата, которые мне стоит купить или затестить. Кратко расскажи почему.
Для каждого аромата укажи:
• <b>Бренд и точное название</b>
• <b>Профиль и ключевые ноты</b>
• <b>Почему зайдет</b>: как он мэтчится с моими вкусами и как покажет себя в эту погоду. Кратко
"""

    try:
        response = await ai_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.5,
            max_tokens=900
        )
        raw_text = response.choices[0].message.content
        return format_to_html(raw_text)
    except Exception as e:
        logging.error(f"Groq shopping advice error: {e}")
        return "Не удалось составить подборку. Попробуй нажать кнопку еще разок!"

# ================= ХЭНДЛЕРЫ =================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user and user[0] and user[1]:
        await message.answer(
            f"Привет! Полка и город (<b>{user[0]}</b>) сохранены. Выбирай действие:",
            reply_markup=get_main_kb(user[2]),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "Салют! Давай настроим твоего персонального парфюм-гида 🧴✨\n\n"
            "Напиши список своих ароматов <b>текстом</b> через запятую или в столбик:\n"
            "<i>(например: Lattafa Khamrah, Korres Cashmere Kumquat, Rayhaan Terra)</i>",
            parse_mode="HTML"
        )
        await state.set_state(SetupState.waiting_for_collection)

@dp.message(SetupState.waiting_for_collection)
async def process_collection(message: types.Message, state: FSMContext):
    await state.update_data(collection=message.text.strip())
    await message.answer("Полку сохранил! Теперь напиши свой город (например: Москва):")
    await state.set_state(SetupState.waiting_for_city)

@dp.message(SetupState.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    data = await state.get_data()
    collection = data.get("collection")
    city = message.text.strip()
    
    await save_user_data(message.from_user.id, city=city, collection=collection)
    await state.clear()
    
    await message.answer(
        f"Город <b>{city}</b> успешно записан!\nТеперь запрашивай совет в любое время:",
        reply_markup=get_main_kb(auto_notify=0),
        parse_mode="HTML"
    )

# --- РЕКОМЕНДАЦИИ К ПОКУПКЕ ---
@dp.message(F.text == "🛍 Что докупить в коллекцию")
async def handle_shopping_advice(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user or not user[0] or not user[1]:
        await message.answer("Сначала настрой коллекцию и город через /start!")
        return

    city, collection, _ = user
    wait_msg = await message.answer("Сканирую пирамиды твоей полки и сопоставляю с погодой... Подбираю топовые варианты к покупке 🛍✨")
    
    weather_info = await get_weather(city)
    advice = await generate_shopping_recommendations(collection, weather_info)
    
    await wait_msg.delete()
    try:
        await message.answer(advice, parse_mode="HTML")
    except Exception:
        await message.answer(advice)

# --- ПРОСМОТР ПОЛКИ ---
@dp.message(F.text == "🧴 Моя полка")
@dp.message(Command("shelf"))
async def show_shelf(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user or not user[1]:
        await message.answer("Полка пока пустая. Заполни её через /start!")
        return
    await message.answer(
        f"🧴 <b>Твоя текущая коллекция ({user[0]}):</b>\n\n{user[1]}",
        parse_mode="HTML"
    )

# --- ДОБАВЛЕНИЕ НОВОГО ФЛАКОНА ЧЕРЕЗ КНОПКУ ---
@dp.message(F.text == "➕ Добавить аромат")
async def add_perfume_btn(message: types.Message, state: FSMContext):
    await message.answer("Напиши название аромата, который хочешь закинуть на полку:")
    await state.set_state(SetupState.waiting_for_single_perfume)

@dp.message(SetupState.waiting_for_single_perfume)
async def process_single_perfume(message: types.Message, state: FSMContext):
    new_perfume = message.text.strip()
    user = await get_user(message.from_user.id)
    
    if not user or not user[1]:
        await save_user_data(message.from_user.id, collection=new_perfume)
    else:
        updated_collection = f"{user[1]}, {new_perfume}"
        await save_user_data(message.from_user.id, collection=updated_collection)
    
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer(
        f"✅ Добавил на полку: <b>{new_perfume}</b>",
        reply_markup=get_main_kb(user[2] if user else 0),
        parse_mode="HTML"
    )

# --- ДОБАВЛЕНИЕ ЧЕРЕЗ БЫСТРУЮ КОМАНДУ /add ---
@dp.message(Command("add"))
async def cmd_add_perfume(message: types.Message):
    new_perfume = message.text.replace("/add", "").strip()
    if not new_perfume:
        await message.answer("Укажи название! Например: <code>/add Xerjoff Naxos</code>", parse_mode="HTML")
        return

    user = await get_user(message.from_user.id)
    if not user or not user[1]:
        await save_user_data(message.from_user.id, collection=new_perfume)
    else:
        updated_collection = f"{user[1]}, {new_perfume}"
        await save_user_data(message.from_user.id, collection=updated_collection)
    
    await message.answer(f"✅ Добавил в коллекцию: <b>{new_perfume}</b>", parse_mode="HTML")

# --- СБРОС И ПЕРЕНАСТРОЙКА ---
@dp.message(F.text == "⚙️ Сбросить профиль")
async def reset_settings(message: types.Message, state: FSMContext):
    await message.answer("Давай настроим всё заново. Присылай полный список духов текстом:")
    await state.set_state(SetupState.waiting_for_collection)

# --- ПОДБОР АРОМАТА ---
@dp.message(F.text == "🌤 Подобрать аромат на день")
async def handle_advice_request(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user or not user[0] or not user[1]:
        await message.answer("Сначала настрой коллекцию и город через /start!")
        return

    city, collection, _ = user
    wait_msg = await message.answer("Чекаю погоду за окном и подбираю парфюм... ⏳")
    
    weather_info = await get_weather(city)
    advice = await generate_perfume_advice(collection, weather_info)
    
    await wait_msg.delete()
    try:
        await message.answer(advice, parse_mode="HTML")
    except Exception:
        await message.answer(advice)

# --- ПЕРЕКЛЮЧЕНИЕ УВЕДОМЛЕНИЙ ---
@dp.message(F.text.startswith("🔔") | F.text.startswith("🔕"))
async def toggle_notifications(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user:
        return
    
    current_status = user[2]
    new_status = 0 if current_status == 1 else 1
    await save_user_data(message.from_user.id, auto_notify=new_status)
    
    status_msg = "включил! Каждое утро в 08:30 буду присылать расклад." if new_status else "выключил."
    await message.answer(
        f"Утренние уведомления {status_msg}",
        reply_markup=get_main_kb(new_status)
    )

# ================= ЕЖЕДНЕВНАЯ РАССЫЛКА =================
async def daily_weather_job():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, city, collection FROM users WHERE auto_notify = 1") as cursor:
            users = await cursor.fetchall()

    for user_id, city, collection in users:
        try:
            weather_info = await get_weather(city)
            advice = await generate_perfume_advice(collection, weather_info)
            header = "Доброе утро! ☀️ Твой ароматный план на сегодня:\n\n"
            try:
                await bot.send_message(user_id, f"{header}{advice}", parse_mode="HTML")
            except Exception:
                await bot.send_message(user_id, f"{header}{advice}")
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Failed to send alert to {user_id}: {e}")

# ================= ЗАПУСК =================
async def main():
    await init_db()
    
    scheduler.add_job(daily_weather_job, "cron", hour=4, minute=30)
    scheduler.start()
    
    print("Бот готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
