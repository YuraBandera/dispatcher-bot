"""
ШІ-Диспетчер екстрених служб — Discord-бот для текстового Roleplay-сервера.

Стек:
    • discord.py 2.x
    • google-genai — актуальний уніфікований SDK Gemini
      (стара бібліотека google-generativeai офіційно deprecated).

Логіка:
    1. Бот слухає канал викликів. Будь-яке повідомлення гравця → бот створює
       під ним гілку (Thread) і тегає гравця.
    2. Кожна гілка має власну сесію Gemini з окремою історією діалогу.
    3. Коли Gemini повертає відповідь із тегом [ВИКЛИК_ЗАВЕРШЕНО]:
         – рапорт (без тега) надсилається в канал фракції з пінгом ролі;
         – гравцю в гілці йде фінальне повідомлення;
         – гілка архівується та блокується.
"""

import os
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
CALLS_CHANNEL_ID   = int(os.getenv("CALLS_CHANNEL_ID", "0"))    # канал, де гравці пишуть виклики
FACTION_CHANNEL_ID = int(os.getenv("FACTION_CHANNEL_ID", "0"))  # канал фракції для рапортів
ROLE_ID_TO_PING    = int(os.getenv("ROLE_ID_TO_PING", "0"))     # роль фракції для пінгу
# За потреби постав новішу flash-модель, доступну на твоєму ключі (напр. gemini-3.7-flash).
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

COMPLETION_TAG = "[ВИКЛИК_ЗАВЕРШЕНО]"

CLOSING_MESSAGE = (
    "Ваш виклик зареєстровано. Оперативну групу поінформовано, екіпаж прямує "
    "за вказаною адресою. Залишайтеся в безпечному місці до прибуття служб. Гілку закрито."
)

SYSTEM_PROMPT = """Ти — штучний інтелект, автоматичний Головний Диспетчер Управління екстрених служб (@ДИСПЕТЧЕР-102) у текстовій рольовій грі (Roleplay) на базі цього Discord-сервера. Твоє завдання — оперативно, суворо офіційно та чітко зібрати інформацію про надзвичайну подію від заявника.

СТИЛЬ ТА ТОН СПІЛКУВАННЯ:
1. Спілкуйся виключно українською мовою.
2. Твій тон — максимально офіційний, дисциплінований та суворий. Ти представник закону та керівного органу.
3. Звертайся до гравця на "Ви" (з великої літери), використовуй офіційні терміни: "Громадянине", "Заявнику", "Оперативна інформація", "Чергова частина", "Екіпаж".
4. Пиши коротко, сухо, без зайвих емоцій, смайликів та "води".

РОЗУМІННЯ ЛОКАЦІЙ ТА ІГРОВОГО ВСЕСВІТУ:
- Гравці можуть називати специфічні ігрові локації вашого сервера (наприклад: назви вигаданих вулиць, блокпостів, районів, умовних баз фракцій або ТЦ). Приймай ці назви як реальні адреси.
- КРИТИЧНО: Якщо гравець називає локацію занадто розмито (наприклад: "я біля дерева", "десь у полі", "на дорозі", "в машині"), ти НЕ маєш цього приймати.
- У разі розмитої відповіді ти повинен офіційно наказати: "Заявнику, уточніть точну вулицю, номер сектора, траси або найближчий помітний орієнтир (державні будівлі, блокпости, дорожні знаки) для коректного спрямування оперативної групи".

ТВОЯ МЕТА — СУВОРО ЗА ПОРЯДКОМ ДІЗНАТИСЯ 3 РЕЧІ:
1. Суть правопорушення або надзвичайної ситуації (що конкретно сталося: ДТП, напад, порушення комендантської години, стрілянина, потрібна медична допомога).
2. ПІБ, якщо каже анонімно фіксуй анонімно! І передавай патрульним ПІБ також.
3. Точне місце події (конкретна ігрова адреса, сектор або чіткий орієнтир).
4. Наявність та кількість постраждалих, а також стан заявника.

ПРАВИЛА ДІАЛОГУ:
- Задавай питання строго по черзі. Не запитуй усе разом в одному повідомленні.
- На початку діалогу офіційно привітайся: "Чергова частина Головного Управління слухає. Що у Вас сталося?"
- Не припиняй опитування, поки не отримаєш чіткі дані по кожному з 4-х пунктів.

ФІНАЛ ДІАЛОГУ:
Як тільки всю інформацію зібрано, твоє фінальне повідомлення має СУВОРО починатися зі спеціального технічного тегу: [ВИКЛИК_ЗАВЕРШЕНО]
Після тегу сформуй офіційний рапорт для екіпажів.

Приклад фінального повідомлення:
[ВИКЛИК_ЗАВЕРШЕНО]
• СИТУАЦІЯ: Напад на патруль, ведеться вогонь із автоматичної зброї.
• ЛОКАЦІЯ: Перехрестя біля Блокпосту №3, північний напрямок.
• ПОСТРАЖДАЛІ: Один співробітник поранений.
Інформацію прийнято та передано найближчим екіпажам реагування. Залишайтеся в безпечному місці до прибуття сил підтримки."""

# Вимикаємо блокування контенту: RP-виклики містять згадки про зброю, поранення,
# напади тощо — зі стандартними фільтрами Gemini відхиляв би легітимні ігрові сценарії.
SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold="BLOCK_NONE")
    for category in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

GEN_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    safety_settings=SAFETY_SETTINGS,
    temperature=0.4,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dispatcher")

# ── Ініціалізація клієнтів ─────────────────────────────────────────────────
genai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True  # ПРИВІЛЕЙОВАНИЙ інтент — увімкни його в Developer Portal
bot = discord.Client(intents=intents)

# thread_id -> {"chat": AsyncChat, "lock": asyncio.Lock()}
sessions: dict[int, dict] = {}


def new_chat_session():
    """Створює нову асинхронну сесію Gemini з системним промтом та історією."""
    return genai_client.aio.chats.create(model=GEMINI_MODEL, config=GEN_CONFIG)


async def ask_gemini(chat, text: str) -> str:
    """Надсилає репліку в Gemini і повертає текст відповіді (історія ведеться автоматично)."""
    response = await chat.send_message(text)
    return (response.text or "").strip()


async def resolve_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


async def send_chunked(channel, content: str, **kwargs):
    """Розбиває довгий текст на частини до 2000 символів (ліміт Discord)."""
    for start in range(0, len(content), 2000):
        await channel.send(content[start:start + 2000], **kwargs)


async def forward_report(report: str):
    """Надсилає рапорт у канал фракції з пінгом ролі."""
    channel = await resolve_channel(FACTION_CHANNEL_ID)
    header = f"<@&{ROLE_ID_TO_PING}>\n**НОВИЙ ВИКЛИК — РАПОРТ ДИСПЕТЧЕРА**\n\n"
    await send_chunked(
        channel,
        header + report,
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )


async def process_turn(thread: discord.Thread, user_text: str, mention: str | None):
    """Один хід діалогу в межах гілки: запит до Gemini + реакція на відповідь."""
    session = sessions.get(thread.id)
    if session is None:
        return

    async with session["lock"]:
        if thread.id not in sessions:  # сесію могли закрити, поки чекали на лок
            return
        try:
            async with thread.typing():
                reply = await ask_gemini(session["chat"], user_text)
        except Exception:
            log.exception("Помилка звернення до Gemini")
            await thread.send("Технічний збій на лінії. Заявнику, повторіть останнє повідомлення.")
            return

        if not reply:
            await thread.send("Заявнику, Ваше повідомлення не розпізнано. Повторіть чіткіше.")
            return

        if COMPLETION_TAG in reply:
            report = reply.replace(COMPLETION_TAG, "").strip()
            await forward_report(report)
            closing = f"{mention} {CLOSING_MESSAGE}" if mention else CLOSING_MESSAGE
            await thread.send(closing)
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                log.warning("Не вдалося заархівувати гілку %s", thread.id)
            sessions.pop(thread.id, None)
            log.info("Виклик у гілці %s завершено та переданий фракції.", thread.id)
        else:
            prefix = f"{mention}\n" if mention else ""
            await send_chunked(thread, prefix + reply)


@bot.event
async def on_ready():
    log.info("Диспетчер онлайн: %s (ID %s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # A. Нове повідомлення в каналі викликів → створюємо гілку і стартуємо діалог
    if message.channel.id == CALLS_CHANNEL_ID:
        try:
            thread = await message.create_thread(
                name=f"Виклик · {message.author.display_name}"[:100],
                auto_archive_duration=60,
            )
        except discord.HTTPException:
            log.exception("Не вдалося створити гілку для повідомлення %s", message.id)
            return

        sessions[thread.id] = {"chat": new_chat_session(), "lock": asyncio.Lock()}
        # Перше повідомлення гравця одразу йде в Gemini як початок діалогу.
        await process_turn(thread, message.content or "(без тексту)", message.author.mention)
        return

    # B. Повідомлення всередині активної гілки виклику → передаємо в Gemini
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
