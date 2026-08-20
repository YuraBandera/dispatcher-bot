import os
import random
import asyncio
import logging
import re

import discord
from dotenv import load_dotenv
from google import genai
from google.genai import types
import edge_tts

# ── Конфігурація ───────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
CALLS_CHANNEL_ID   = int(os.getenv("CALLS_CHANNEL_ID", "0"))
FACTION_CHANNEL_ID = int(os.getenv("FACTION_CHANNEL_ID", "0"))
ROLE_ID_TO_PING    = int(os.getenv("ROLE_ID_TO_PING", "0"))
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Список ID голосових каналів через кому (наприклад: "111,222,333,444")
PATROL_VOICE_CHANNEL_IDS = [
    int(ch_id.strip())
    for ch_id in os.getenv("PATROL_VOICE_CHANNEL_IDS", "").split(",")
    if ch_id.strip().isdigit()
]

COMPLETION_TAG   = "[ВИКЛИК_ЗАВЕРШЕНО]"
INVALID_TAG      = "[ВИКЛИК_СКАСОВАНО]"
REACTION_TIMEOUT = 120  # 2 хвилини на очікування реакції

# База диспетчерів з відповідними голосами edge-tts
DISPATCHERS_MALE = [
    {"name": "Олексій Ткаченко", "callsign": "102-Альфа", "role_desc": "черговий диспетчер", "voice": "uk-UA-OstapNeural"},
    {"name": "Максим Бондаренко", "callsign": "102-Омега", "role_desc": "старший оператор лінії", "voice": "uk-UA-OstapNeural"},
    {"name": "Дмитро Козак", "callsign": "102-Браво", "role_desc": "черговий частини", "voice": "uk-UA-OstapNeural"},
    {"name": "Артем Шевчук", "callsign": "102-Дельта", "role_desc": "диспетчер зв'язку", "voice": "uk-UA-OstapNeural"}
]

DISPATCHERS_FEMALE = [
    {"name": "Світлана Мельник", "callsign": "102-Венера", "role_desc": "чергова диспетчерка", "voice": "uk-UA-PolinaNeural"},
    {"name": "Вікторія Коваль", "callsign": "102-Стріла", "role_desc": "старша операторка лінії", "voice": "uk-UA-PolinaNeural"},
    {"name": "Олена Мороз", "callsign": "102-Зоря", "role_desc": "чергова частини", "voice": "uk-UA-PolinaNeural"},
    {"name": "Катерина Бойко", "callsign": "102-Фенікс", "role_desc": "диспетчерка зв'язку", "voice": "uk-UA-PolinaNeural"}
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
intents.reactions = True
intents.voice_states = True
bot = discord.Client(intents=intents)

# thread_id -> {"chat": AsyncChat, "lock": asyncio.Lock(), "dispatcher": dict}
sessions: dict[int, dict] = {}


def generate_system_prompt(disp: dict) -> str:
    gender = disp["gender"]
    gender_rules = (
        "Ти ЧОЛОВІК. Використовуй виключно чоловічий рід у минулому часі (зрозумів, прийняв, передав, з'ясував, відправив)."
        if gender == "male"
        else "Ти ЖІНКА. Використовуй виключно жіночий рід у минулому часі (зрозуміла, прийняла, передала, з'ясувала, відправила)."
    )

    return f"""Ти — живий диспетчер екстреної служби 102/112 у текстовій рольовій грі (Roleplay).
Твоє ім'я: {disp['name']}.
Твій статус/посада: {disp['role_desc']} (Позивний: {disp['callsign']}).

ВАЖЛИВО ПРО СТАТЬ ТА МОВУ:
- {gender_rules}
- Спілкуйся виключно українською мовою.
- Не говори як сухий скрипт чи робот. Говори як живий, зосереджений диспетчер у навушниках за пультом зв'язку.

ПОВЕДІНКА ТА СТИЛЬ:
1. Завжди тримай контроль над розмовою. Якщо гравець панікує або говорить незв'язно — лаконічно заспокой і поверни до фактів.
2. Звертайся до заявника шанобливо, на «Ви», але строго та по справі.
3. У першому повідомленні обов'язково назви своє ім'я або посаду (наприклад: "Екстрена лінія 102, {disp['role_desc']} {disp['name']}. Що у Вас сталося?").

ТВОЄ ЗАВДАННЯ — ОПЕРАТИВНО ЗІБРАТИ 4 ПУНКТИ:
1. Що сталося (суть правопорушення або події).
2. Хто повідомляє (ПІБ або якщо каже "анонімно" — фіксуй як аноніма).
3. Точне місце події (ігрові будівлі, райони, траси, блокпости, відомі заклади: ГУНП, ДСНС, Лікарня, ВРУ, Банк, Баня, ТЦ тощо).
   - Якщо названо абстрактно ("в кущах", "десь біля дороги") — вимагай чіткий орієнтир.
4. Постраждалі (наявність, кількість, стан, чи потрібні медики).

ПРАВИЛА РОЗМОВИ:
- Задавай питання природно й по черзі, реагуючи на відповіді гравця. Не надсилай анкету з 4 питань одночасно.
- Якщо гравець відверто тролить, несе маячню або 2 рази підряд не дає конкретики — відхили діалог тегом {INVALID_TAG}.

ФІНАЛ ДІАЛОГУ:
Коли вся інформація є, твоє повідомлення має ПОЧИНАТИСЯ СТРОГО з: {COMPLETION_TAG}

Формат після тегу:
{COMPLETION_TAG}
• СИТУАЦІЯ: [Короткий опис]
• ЗАЯВНИК: [ПІБ або Анонім]
• ЛОКАЦІЯ: [Точна адреса чи орієнтир]
• ПОСТРАЖДАЛІ / ЗАГРОЗА: [Інформація про людей/зброю/травми]
• ДИСПЕТЧЕР: {disp['name']} ({disp['callsign']})"""


def new_chat_session():
    """Створює сесію з випадковим вибором імені та статі диспетчера."""
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
        temperature=0.6,
    )

    chat = genai_client.aio.chats.create(model=GEMINI_MODEL, config=config)
    return chat, disp_data


async def ask_gemini(chat, text: str) -> str:
    response = await chat.send_message(text)
    return (response.text or "").strip()


async def play_voice_alert(report_text: str, disp: dict):
    """Шукає всі активні голосові канали з людьми та послідовно озвучує рапорт."""
    if not PATROL_VOICE_CHANNEL_IDS:
        return

    # Відбираємо тільки ті канали, де є живі користувачі (не боти)
    active_channels: list[discord.VoiceChannel] = []
    for channel_id in PATROL_VOICE_CHANNEL_IDS:
        ch = bot.get_channel(channel_id)
        if isinstance(ch, discord.VoiceChannel):
            human_members = [m for m in ch.members if not m.bot]
            if human_members:
                active_channels.append(ch)

    if not active_channels:
        log.info("Усі 4 патрульні голосові канали порожні, голосове сповіщення пропущено.")
        return

    clean_text = re.sub(r"[•*]", "", report_text)
    speech_text = (
        f"Увага всім екіпажам! Говорить {disp['name']}. "
        f"Надійшов новий виклик: {clean_text}. Підтвердіть реагування в каналі зв'язку."
    )

    temp_audio = f"voice_alert_{random.randint(10000, 99999)}.mp3"
    try:
        communicate = edge_tts.Communicate(speech_text, disp["voice"])
        await communicate.save(temp_audio)

        for channel in active_channels:
            try:
                vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
                if vc is None:
                    vc = await channel.connect()
                elif vc.channel.id != channel.id:
                    await vc.move_to(channel)

                if vc.is_playing():
                    vc.stop()

                vc.play(discord.FFmpegPCMAudio(temp_audio))
                while vc.is_playing():
                    await asyncio.sleep(0.5)

            except Exception:
                log.exception("Помилка відтворення аудіо у войсі %s", channel.id)

        # Відключаємося після обходу всіх зайнятих каналів
        for vc in bot.voice_clients:
            if vc.is_connected():
                await vc.disconnect()

    except Exception:
        log.exception("Помилка під час генерації або відтворення TTS")
    finally:
        if os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except OSError:
                pass


async def handle_call_dispatch(thread: discord.Thread, report: str, disp: dict, mention: str | None):
    """Асинхронно відправляє рапорт, озвучує виклик та очікує реакцію галочки."""
    faction_channel = bot.get_channel(FACTION_CHANNEL_ID)
    if faction_channel is None:
        faction_channel = await bot.fetch_channel(FACTION_CHANNEL_ID)

    # 1. Запуск голосового сповіщення у фоні
    asyncio.create_task(play_voice_alert(report, disp))

    # 2. Публікація рапорту у текстовий канал фракції
    header = f"<@&{ROLE_ID_TO_PING}>\n🚨 **НОВИЙ ВИКЛИК 102 — ОПЕРАТИВНЕ РЕАГУВАННЯ**\n\n"
    report_message = await faction_channel.send(
        header + report + "\n\n*(Натисніть ✅, щоб прийняти виклик)*",
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True)
    )
    await report_message.add_reaction("✅")

    # 3. Очікування реакції
    def check_reaction(reaction: discord.Reaction, user: discord.User):
        return (
            reaction.message.id == report_message.id
            and str(reaction.emoji) == "✅"
            and not user.bot
        )

    try:
        reaction, user = await bot.wait_for("reaction_add", timeout=REACTION_TIMEOUT, check=check_reaction)

        # Сценарій 1: Патрульний прийняв виклик
        await report_message.edit(
            content=f"🟢 **ВИКЛИК ПРИЙНЯТО: {user.mention}**\n\n{report}"
        )
        closing_msg = (
            f"{mention or ''}\n🚔 **Екіпаж патрульної поліції ({user.display_name}) прийняв Ваш виклик і прямує за вказаною адресою.** "
            f"Залишайтеся в безпечному місці до прибуття служб."
        )
        await thread.send(closing_msg)
        log.info("Виклик у гілці %s прийняв співробітник %s.", thread.id, user.display_name)

    except asyncio.TimeoutError:
        # Сценарій 2: Таймаут 2 хвилини (немає реакції)
        await report_message.edit(
            content=f"🔴 **НЕМАЄ РЕАГУВАННЯ (ТАЙМАУТ)**\n\n{report}"
        )
        timeout_msg = (
            f"{mention or ''}\n❌ **На жаль, наразі всі вільні екіпажі зайняті або відсутні на лінії реагування.** "
            f"Заяву внесено до журналу обліку. За потреби зверніться до найближчого відділку поліції."
        )
        await thread.send(timeout_msg)
        log.info("Виклик у гілці %s скасовано через відсутність реакції.", thread.id)

    finally:
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            pass
        sessions.pop(thread.id, None)


async def process_turn(thread: discord.Thread, user_text: str, mention: str | None):
    """Обробка репліки в гілці виклику."""
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
            await thread.send("⚠️ Перешкоди на лінії зв'язку. Заявнику, повторіть останнє повідомлення.")
            return

        if not reply:
            await thread.send("Заявнику, Вас погано чути. Повторіть чіткіше.")
            return

        disp = session["dispatcher"]
        hung_up = "*Скинув слухавку.*" if disp["gender"] == "male" else "*Скинула слухавку.*"

        # Виклик успішно зібрано
        if COMPLETION_TAG in reply:
            report = reply.replace(COMPLETION_TAG, "").strip()
            await thread.send(f"Інформацію прийнято, формую картку виклику та передаю вільним екіпажам... {hung_up}")
            asyncio.create_task(handle_call_dispatch(thread, report, disp, mention))

        # Хибний виклик або відхилення
        elif INVALID_TAG in reply:
            msg = reply.replace(INVALID_TAG, "").strip()
            if msg:
                await thread.send(msg)
            await thread.send(f"Зв'язок примусово розірвано черговою частиною через некоректний виклик. {hung_up}")
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
            sessions.pop(thread.id, None)

        # Звичайна репліка в діалозі
        else:
            prefix = f"{mention}\n" if mention else ""
            await thread.send(prefix + reply)


@bot.event
async def on_ready():
    log.info("Головне Управління: диспетчерський центр запущено (%s | ID %s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 1. Повідомлення в каналі викликів -> створення нової гілки
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

    # 2. Повідомлення всередині активної гілки
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
