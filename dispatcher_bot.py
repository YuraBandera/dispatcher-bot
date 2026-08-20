import os
import random
import asyncio
import logging

import discord
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── Конфігурація ───────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
CALLS_CHANNEL_ID   = int(os.getenv("CALLS_CHANNEL_ID", "0"))
FACTION_CHANNEL_ID = int(os.getenv("FACTION_CHANNEL_ID", "0"))
ROLE_ID_TO_PING    = int(os.getenv("ROLE_ID_TO_PING", "0"))
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

COMPLETION_TAG = "[ВИКЛИК_ЗАВЕРШЕНО]"
INVALID_TAG    = "[ВИКЛИК_СКАСОВАНО]"

# База імен диспетчерів
DISPATCHERS_MALE = [
    {"name": "Олексій Ткаченко", "callsign": "102-Альфа", "role_desc": "черговий диспетчер"},
    {"name": "Максим Бондаренко", "callsign": "102-Омега", "role_desc": "старший оператор лінії"},
    {"name": "Дмитро Козак", "callsign": "102-Браво", "role_desc": "черговий частини"},
    {"name": "Артем Шевчук", "callsign": "102-Дельта", "role_desc": "диспетчер зв'язку"}
]

DISPATCHERS_FEMALE = [
    {"name": "Світлана Мельник", "callsign": "102-Венера", "role_desc": "чергова диспетчерка"},
    {"name": "Вікторія Коваль", "callsign": "102-Стріла", "role_desc": "старша операторка лінії"},
    {"name": "Олена Мороз", "callsign": "102-Зоря", "role_desc": "чергова частини"},
    {"name": "Катерина Бойко", "callsign": "102-Фенікс", "role_desc": "диспетчерка зв'язку"}
]

SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold="BLOCK_NONE")
    for category in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dispatcher")

# ── Ініціалізація клієнтів ─────────────────────────────────────────────────
genai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# thread_id -> {"chat": AsyncChat, "lock": asyncio.Lock(), "dispatcher": dict}
sessions: dict[int, dict] = {}


def generate_system_prompt(disp: dict) -> str:
    gender = disp["gender"]
    gender_rules = (
        "Ти ЧОЛОВІК. Використовуй чоловічий рід (зрозумів, прийняв, передав, з'ясував, відправив)."
        if gender == "male"
        else "Ти ЖІНКА. Використовуй жіночий рід (зрозуміла, прийняла, передала, з'ясувала, відправила)."
    )

    return f"""Ти — живий диспетчер екстреної служби 102/112 у текстовій рольовій грі (Roleplay).
Твоє ім'я: {disp['name']}.
Твій статус/посада: {disp['role_desc']} (Позивний: {disp['callsign']}).

ВАЖЛИВО ПРО СТАТЬ ТА МОВУ:
- {gender_rules}
- Спілкуйся виключно українською мовою.
- Не говори як заготовлений скрипт чи робот. Говори як досвідчена, холоднокровна, зосереджена людина в навушниках за пультом зв'язку, яка щодня приймає критичні виклики.

ПОВЕДІНКА ТА СТИЛЬ:
1. Завжди тримай контроль над розмовою. Якщо гравець панікує або говорить незв'язно — лаконічно заспокой і поверни до фактів ("Заспокойтеся, допомога формується, скажіть чітко...").
2. Звертайся до заявника шанобливо, на «Ви», але строго та по справі.
3. У першому повідомленні обов'язково назви своє ім'я чи посаду (наприклад: "Екстрена лінія 102, {disp['role_desc']} {disp['name']}. Що у Вас трапилося?").

ТВОЄ ЗАВДАННЯ — ОПЕРАТИВНО ЗІБРАТИ 4 ПУНКТИ:
1. Що сталося (суть події, є загроза життю чи ні).
2. Хто повідомляє (ПІБ або якщо каже "анонімно" — фіксуй як аноніма).
3. Точне місце події (ігрові будівлі, райони, траси, блокпости, відомі заклади: ГУНП, ДСНС, Лікарня, ВРУ, Банк, Баня, ТЦ тощо).
   - Якщо названо абстрактно ("в кущах", "десь біля дороги") — вимагай чіткий орієнтир або перехрестя.
4. Постраждалі (наявність, кількість, стан, чи потрібні медики).

ПРАВИЛА РОЗМОВИ:
- Задавай питання природно й по черзі, реагуючи на сказане гравцем. Не надсилай суцільні анкети з 4 питань в одному повідомленні.
- Якщо гравець несе маячню, тролить або 2 рази підряд не може дати жодної конкретики — попередь про хибний виклик або відхили діалог, вивівши тег {INVALID_TAG}.

ФІНАЛ ДІАЛОГУ:
Коли вся інформація є, сформуй офіційний рапорт для патрульних.
Твоє останнє повідомлення має ПОЧИНАТИСЯ СТРОГО з: {COMPLETION_TAG}

Формат після тегу:
{COMPLETION_TAG}
• СИТУАЦІЯ: [Короткий опис]
• ЗАЯВНИК: [ПІБ або Анонім]
• ЛОКАЦІЯ: [Точна адреса чи орієнтир]
• ПОСТРАЖДАЛІ / ЗАГРОЗА: [Інформація про людей/зброю/травми]
• ДИСПЕТЧЕР: {disp['name']} ({disp['callsign']})"""


def new_chat_session():
    """Обирає стать, ім'я та генерує ізольовану сесію."""
    is_male = random.choice([True, False])
    if is_male:
        disp_data = random.choice(DISPATCHERS_MALE).copy()
        disp_data["gender"] = "male"
    else:
        disp_data = random.choice(DISPATCHERS_FEMALE).copy()
        disp_data["gender"] = "female"

    system_prompt = generate_system_prompt(disp_data)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        safety_settings=SAFETY_SETTINGS,
        temperature=0.6,  # Трохи вища температура для більш живої та природної мови
    )

    chat = genai_client.aio.chats.create(model=GEMINI_MODEL, config=config)
    return chat, disp_data


async def ask_gemini(chat, text: str) -> str:
    response = await chat.send_message(text)
    return (response.text or "").strip()


async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


async def send_chunked(channel, content: str, **kwargs):
    for start in range(0, len(content), 2000):
        await channel.send(content[start:start + 2000], **kwargs)


async def forward_report(report: str):
    channel = await resolve_channel(FACTION_CHANNEL_ID)
    header = f"<@&{ROLE_ID_TO_PING}>\n🚨 **НОВИЙ ВИКЛИК 102 — ОПЕРАТИВНЕ РЕАГУВАННЯ**\n\n"
    await send_chunked(
        channel,
        header + report,
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )


async def process_turn(thread: discord.Thread, user_text: str, mention: str | None):
    session = sessions.get(thread.id)
    if session is None:
        return

    async with session["lock"]:
        if thread.id not in sessions:
            return
        try:
            async with thread.typing():
                reply = await ask_gemini(session["chat"], user_text)
        except Exception:
            log.exception("Помилка звернення до Gemini")
            await thread.send("⚠️ Перешкоди на радіолінії. Заявнику, повторіть останнє повідомлення.")
            return

        if not reply:
            await thread.send("Заявнику, Вас погано чути. Повторіть.")
            return

        disp = session["dispatcher"]
        hung_up_text = "*Скинув слухавку.*" if disp["gender"] == "male" else "*Скинула слухавку.*"

        # Сценарій 1: Успішне завершення та формування рапорту
        if COMPLETION_TAG in reply:
            report = reply.replace(COMPLETION_TAG, "").strip()
            await forward_report(report)

            closing = (
                f"{mention or ''}\nВиклик прийнято в обробку. Екіпажі висуваються на місце події. "
                f"Залишайтеся в безпечному місці. {hung_up_text}"
            )
            await thread.send(closing)

            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                log.warning("Не вдалося закрити гілку %s", thread.id)

            sessions.pop(thread.id, None)
            log.info("Виклик у гілці %s оформлено диспетчером %s.", thread.id, disp["name"])

        # Сценарій 2: Анулювання / тролінг / відсутність інформації
        elif INVALID_TAG in reply:
            msg = reply.replace(INVALID_TAG, "").strip()
            if msg:
                await thread.send(msg)
            await thread.send(f"Зв'язок розірвано черговою частиною через некоректний виклик. {hung_up_text}")
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
            sessions.pop(thread.id, None)

        # Звичайний крок діалогу
        else:
            prefix = f"{mention}\n" if mention else ""
            await send_chunked(thread, prefix + reply)


@bot.event
async def on_ready():
    log.info("Диспетчерський центр запущено: %s (ID %s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Створення нового виклику
    if message.channel.id == CALLS_CHANNEL_ID:
        try:
            thread = await message.create_thread(
                name=f"102 · {message.author.display_name}"[:100],
                auto_archive_duration=60,
            )
        except discord.HTTPException:
            log.exception("Не вдалося створити гілку для повідомлення %s", message.id)
            return

        chat, disp_data = new_chat_session()
        sessions[thread.id] = {
            "chat": chat,
            "dispatcher": disp_data,
            "lock": asyncio.Lock(),
        }

        await process_turn(thread, message.content or "Алло, поліція?", message.author.mention)
        return

    # Продовження діалогу у гілці
    if isinstance(message.channel, discord.Thread) and message.channel.id in sessions:
        await process_turn(message.channel, message.content, None)


if __name__ == "__main__":
    missing = [
        name for name, value in {
            "DISCORD_TOKEN": DISCORD_TOKEN,
            "GEMINI_API_KEY": GEMINI_API_KEY,
            "CALLS_CHANNEL_ID": CALLS_CHANNEL_ID,
            "FACTION_CHANNEL_ID": FACTION_CHANNEL_ID,
            "ROLE_ID_TO_PING": ROLE_ID_TO_PING,
        }.items() if not value
    ]
    if missing:
        raise SystemExit(f"Не заповнені змінні оточення: {', '.join(missing)}")
    bot.run(DISCORD_TOKEN)
