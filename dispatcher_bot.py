import os
import io
import random
import asyncio
import logging
import re
import shutil
import ctypes.util

import discord
from dotenv import load_dotenv
from google import genai
from google.genai import types
import edge_tts
import static_ffmpeg
static_ffmpeg.add_paths()

# ── Завантаження Opus для голосових функцій ─────────────────────────────────
if not discord.opus.is_loaded():
    _opus_candidates = ['libopus.so.0', 'libopus.so', 'libopus', 'opus']
    _found = ctypes.util.find_library('opus')
    if _found:
        _opus_candidates.insert(0, _found)
    for opus_lib in _opus_candidates:
        try:
            discord.opus.load_opus(opus_lib)
            break
        except Exception:
            continue

# ── Конфігурація ───────────────────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
CALLS_CHANNEL_ID   = int(os.getenv("CALLS_CHANNEL_ID", "0"))
FACTION_CHANNEL_ID = int(os.getenv("FACTION_CHANNEL_ID", "0"))
ROLE_ID_TO_PING    = int(os.getenv("ROLE_ID_TO_PING", "0"))
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_voice_raw = [
    os.getenv("PATROL_VOICE_CHANNEL_1", ""),
    os.getenv("PATROL_VOICE_CHANNEL_2", ""),
    os.getenv("PATROL_VOICE_CHANNEL_3", ""),
    os.getenv("PATROL_VOICE_CHANNEL_4", ""),
]
PATROL_VOICE_CHANNEL_IDS = [
    int(ch_id.strip()) for ch_id in _voice_raw if ch_id.strip().isdigit()
]

COMPLETION_TAG = "[ВИКЛИК_ЗАВЕРШЕНО]"
INVALID_TAG    = "[ВИКЛИК_СКАСОВАНО]"

# Сигнали реагування (реакції під карткою у фракційному каналі)
EMOJI_ACCEPT = "✅"   # екіпаж прийняв виклик
EMOJI_BUSY   = "⛔"   # екіпаж зайнятий / недоступний
EMOJI_FINISH = "🏁"   # виклик завершено

ACCEPT_TIMEOUT = 180    # скільки чекати ✅/⛔ від екіпажу, сек
FINISH_TIMEOUT = 1800   # скільки чекати 🏁 після прийняття, сек (авто-відбій)

# Бот один — не може бути у двох войсах одночасно. Голосові виклики опрацьовуємо по черзі.
VOICE_LOCK = asyncio.Lock()

DISPATCHERS_MALE = [
    {"name": "Олексій Ткаченко", "callsign": "102-Альфа", "role_desc": "черговий диспетчер", "voice": "uk-UA-OstapNeural", "gender": "male"},
    {"name": "Максим Бондаренко", "callsign": "102-Омега", "role_desc": "старший оператор лінії", "voice": "uk-UA-OstapNeural", "gender": "male"},
    {"name": "Дмитро Козак", "callsign": "102-Браво", "role_desc": "черговий частини", "voice": "uk-UA-OstapNeural", "gender": "male"},
    {"name": "Артем Шевчук", "callsign": "102-Дельта", "role_desc": "диспетчер зв'язку", "voice": "uk-UA-OstapNeural", "gender": "male"}
]

DISPATCHERS_FEMALE = [
    {"name": "Світлана Мельник", "callsign": "102-Венера", "role_desc": "чергова диспетчерка", "voice": "uk-UA-PolinaNeural", "gender": "female"},
    {"name": "Вікторія Коваль", "callsign": "102-Стріла", "role_desc": "старша операторка лінії", "voice": "uk-UA-PolinaNeural", "gender": "female"},
    {"name": "Олена Мороз", "callsign": "102-Зоря", "role_desc": "чергова частини", "voice": "uk-UA-PolinaNeural", "gender": "female"},
    {"name": "Катерина Бойко", "callsign": "102-Фенікс", "role_desc": "диспетчерка зв'язку", "voice": "uk-UA-PolinaNeural", "gender": "female"}
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

if discord.opus.is_loaded():
    log.info("[VOICE] Opus завантажено.")
else:
    log.warning("[VOICE] Opus НЕ завантажено (для FFmpegOpusAudio це не критично).")


def resolve_ffmpeg() -> str:
    """Знаходимо реальний шлях до ffmpeg: static_ffmpeg → системний PATH → 'ffmpeg'."""
    try:
        from static_ffmpeg import run as _sf_run
        ffmpeg_path, _ = _sf_run.get_or_fetch_platform_executables_else_raise()
        if ffmpeg_path and os.path.exists(ffmpeg_path):
            return ffmpeg_path
    except Exception:
        log.warning("[VOICE] static_ffmpeg не дав шлях, пробую системний ffmpeg.")
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG_PATH = resolve_ffmpeg()
log.info("[VOICE] FFmpeg executable: %s", FFMPEG_PATH)

# ── Ініціалізація клієнтів ─────────────────────────────────────────────────
genai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.all()
bot = discord.Client(intents=intents)

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

ВАЖЛИВО:
- {gender_rules}
- Спілкуйся виключно українською мовою.
- Не говори як сухий скрипт чи робот. Говори чітко, швидко й зібрано.

ТВОЄ ЗАВДАННЯ — ОПЕРАТИВНО ЗІБРАТИ 4 ПУНКТИ:
1. Що сталося (суть правопорушення).
2. Хто повідомляє (ПІБ або якщо анонімно).
3. Точне місце події (конкретний орієнтир, будівля, вулиця).
4. Постраждалі (стан, кількість, чи є зброя/загроза).

ПРАВИЛА РОЗМОВИ:
- Задавай питання природно й по черзі, реагуючи на відповіді заявника.
- Якщо гравець несе маячню або спамить — відхили діалог тегом {INVALID_TAG}.

ФІНАЛ ДІАЛОГУ:
Коли вся інформація зібрана, твоя відповідь ПОВИННА ПОЧИНАТИСЯ СТРОГО З ТЕГУ {COMPLETION_TAG}

Формат картки після тегу:
{COMPLETION_TAG}
• СИТУАЦІЯ: [Короткий опис]
• ЗАЯВНИК: [ПІБ або Анонім]
• ЛОКАЦІЯ: [Адреса чи орієнтир]
• ПОСТРАЖДАЛІ / ЗАГРОЗА: [Інформація про людей/зброю]
• ДИСПЕТЧЕР: {disp['name']} ({disp['callsign']})"""


def new_chat_session():
    is_male = random.choice([True, False])
    disp_data = (random.choice(DISPATCHERS_MALE) if is_male else random.choice(DISPATCHERS_FEMALE)).copy()
    disp_data["gender"] = "male" if is_male else "female"

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


# ── Синтез мовлення (edge-tts прямо в пам'ять, без mp3-файлів) ─────────────

async def synthesize_speech_bytes(text: str, voice: str) -> bytes:
    try:
        buffer = bytearray()
        communicate = edge_tts.Communicate(text, voice)  # без rate — природніший темп
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        return bytes(buffer)
    except Exception:
        log.exception("[VOICE] Помилка синтезу мовлення (edge-tts).")
        return b""


async def compose_announcement_audio(report: str, disp: dict) -> bytes:
    """Диспетчер сам формулює живе радіозвернення, одразу синтезуємо його в аудіо."""
    gender_word = "чоловік" if disp["gender"] == "male" else "жінка"
    instruction = (
        f"Ти — диспетчер {disp['name']}, позивний {disp['callsign']}, стать: {gender_word}. "
        "Сформулюй КОРОТКЕ (2–3 речення) усне радіозвернення до патрульних екіпажів по гучному зв'язку "
        "за наведеною карткою виклику. Українською, від першої особи, у правильному роді, живим діловим тоном. "
        "Поверни ЛИШЕ текст звернення — без тегів, розмітки, зірочок, маркерів та емодзі.\n\n"
        f"Картка виклику:\n{report}"
    )
    speech_text = None
    try:
        response = await genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=instruction,
            config=types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS, temperature=0.7),
        )
        speech_text = re.sub(r"[•*#_`]", "", (response.text or "")).strip()
    except Exception:
        log.exception("[VOICE] Не вдалося згенерувати текст звернення — використовую шаблон.")

    if not speech_text:
        clean = re.sub(r"[•*#_`]", "", report).strip()
        speech_text = (
            f"Увага всім патрулям! Говорить черговий диспетчер {disp['name']}. "
            f"Надійшов терміновий виклик. {clean}. Інформація в каналі зв'язку."
        )
    return await synthesize_speech_bytes(speech_text, disp["voice"])


# ── Робота з голосом ───────────────────────────────────────────────────────

def find_crew_channel() -> discord.VoiceChannel | None:
    """Перший патрульний канал, де є хоч одна жива людина."""
    for ch_id in PATROL_VOICE_CHANNEL_IDS:
        ch = bot.get_channel(ch_id)
        if isinstance(ch, discord.VoiceChannel) and any(not m.bot for m in ch.members):
            return ch
    return None


async def connect_to(channel: discord.VoiceChannel) -> discord.VoiceClient:
    vc = next((c for c in bot.voice_clients if c.guild.id == channel.guild.id), None)
    if vc is None or not vc.is_connected():
        vc = await channel.connect(timeout=20.0, reconnect=True)
    elif vc.channel.id != channel.id:
        await vc.move_to(channel)
    return vc


async def play_audio_bytes(vc: discord.VoiceClient, audio_bytes: bytes):
    if not vc or not audio_bytes:
        return
    play_done = asyncio.Event()

    def after_playback(error):
        if error:
            log.error("[VOICE] Помилка відтворення: %s", error)
        bot.loop.call_soon_threadsafe(play_done.set)

    if vc.is_playing():
        vc.stop()
    # FFmpeg кодує в Opus (libopus для кодування не потрібен); loudnorm = чистий, рівний звук.
    source = discord.FFmpegOpusAudio(
        io.BytesIO(audio_bytes),
        pipe=True,
        executable=FFMPEG_PATH,
        bitrate=128,
        options='-af loudnorm=I=-16:TP=-1.5:LRA=11',
    )
    vc.play(source, after=after_playback)
    try:
        await asyncio.wait_for(play_done.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        log.warning("[VOICE] Таймаут відтворення.")


async def speak(vc: discord.VoiceClient, text: str, voice: str):
    """Синтез + програвання короткої службової фрази."""
    await play_audio_bytes(vc, await synthesize_speech_bytes(text, voice))


async def disconnect_all():
    for client in list(bot.voice_clients):
        try:
            await client.disconnect(force=True)
        except Exception:
            pass


async def wait_for_reaction(message: discord.Message, emojis: set[str], timeout: float | None):
    """Чекаємо будь-яку з реакцій emojis на message. Повертає (емодзі, користувач) або (None, None)."""
    def check(reaction: discord.Reaction, user: discord.User):
        return (
            reaction.message.id == message.id
            and str(reaction.emoji) in emojis
            and not user.bot
        )
    try:
        reaction, user = await bot.wait_for("reaction_add", check=check, timeout=timeout)
        return str(reaction.emoji), user
    except asyncio.TimeoutError:
        return None, None


async def close_thread_with(thread: discord.Thread, text: str):
    try:
        await thread.send(text)
    except discord.HTTPException:
        pass
    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass


# ── Головний цикл виклику: голос + очікування реагування ───────────────────

async def run_callout(faction_message: discord.Message, report: str, disp: dict, thread: discord.Thread, mention: str | None):
    voice = disp["voice"]
    # Бот один — тримаємо один голосовий цикл за раз, щоб виклики не билися за войс.
    async with VOICE_LOCK:
        vc = None
        try:
            crew = find_crew_channel()

            if crew:
                # Пришвидшення: паралельно підключаємось і готуємо аудіо звернення.
                log.info("[VOICE] Екіпаж у '%s' — підключаюсь і готую звернення...", crew.name)
                connect_task = asyncio.create_task(connect_to(crew))
                audio_task = asyncio.create_task(compose_announcement_audio(report, disp))
                try:
                    vc, announce_audio = await asyncio.gather(connect_task, audio_task)
                except Exception:
                    log.exception("[VOICE] Помилка підключення/підготовки аудіо.")
                    vc, announce_audio = None, b""
                if vc:
                    await asyncio.sleep(0.4)  # даємо голосовому каналу «прогрітись»
                    await play_audio_bytes(vc, announce_audio)
            else:
                log.info("[VOICE] У войсах нікого — працюємо лише текстом.")

            # Чекаємо сигнал від екіпажу: ✅ прийнято або ⛔ зайняті/недоступні.
            emoji, officer = await wait_for_reaction(
                faction_message, {EMOJI_ACCEPT, EMOJI_BUSY}, ACCEPT_TIMEOUT
            )

            # ── Екіпаж зайнятий / недоступний ──────────────────────────────
            if emoji == EMOJI_BUSY:
                await faction_message.clear_reactions()
                await faction_message.edit(content=f"🔴 **ЕКІПАЖ ЗАЙНЯТИЙ / НЕДОСТУПНИЙ**\n\n{report}")
                if vc:
                    await speak(vc, "Зрозуміло, екіпаж зайнятий. Передаю виклик далі. Відбій.", voice)
                await close_thread_with(
                    thread,
                    f"{mention or ''}\n❌ **Наразі всі вільні екіпажі зайняті або відсутні на лінії.** "
                    f"Заяву внесено до журналу обліку. За потреби зверніться до найближчого відділку поліції."
                )
                return

            # ── Ніхто не відреагував за ACCEPT_TIMEOUT ─────────────────────
            if emoji is None:
                await faction_message.clear_reactions()
                await faction_message.edit(content=f"🔴 **НЕМАЄ РЕАГУВАННЯ — ТАЙМАУТ**\n\n{report}")
                if vc:
                    await speak(vc, "Відповіді від екіпажу немає. Виклик лишається без реагування.", voice)
                await close_thread_with(
                    thread,
                    f"{mention or ''}\n❌ **На жаль, наразі екіпажі недоступні на лінії реагування.** "
                    f"Заяву внесено до журналу обліку. За потреби зверніться до найближчого відділку поліції."
                )
                return

            # ── Виклик ПРИЙНЯТО (✅) ───────────────────────────────────────
            await faction_message.clear_reactions()
            await faction_message.edit(
                content=(
                    f"🟡 **ВИКЛИК В РОБОТІ: {officer.mention}**\n\n{report}\n\n"
                    f"*(Після завершення ситуації натисніть 🏁 для закриття картки)*"
                )
            )
            await faction_message.add_reaction(EMOJI_FINISH)
            if vc:
                await speak(vc, f"Виклик прийнято, дякую. Екіпаж на завданні. Чекаю доповідь про завершення.", voice)

            # Повідомляємо заявника й закриваємо гілку — реагування підтверджено.
            await close_thread_with(
                thread,
                f"{mention or ''}\n🚔 **Екіпаж патрульної поліції прийняв Ваш виклик** і прямує за вказаною адресою. "
                f"Залишайтеся в безпечному місці та очікуйте на прибуття співробітників."
            )

            # Лишаємось на лінії й чекаємо 🏁 (з запобіжним авто-таймаутом).
            finish_emoji, closing_officer = await wait_for_reaction(
                faction_message, {EMOJI_FINISH}, FINISH_TIMEOUT
            )
            await faction_message.clear_reactions()
            who = closing_officer.mention if closing_officer else "авто-відбій"
            await faction_message.edit(content=f"🟢 **ВИКЛИК ЗАВЕРШЕНО: {who}**\n\n{report}")
            if vc:
                await speak(vc, "Виклик завершено. Дякую за роботу. Відбій.", voice)

        except Exception:
            log.exception("[VOICE] Помилка у циклі виклику.")
        finally:
            await disconnect_all()
            log.info("[VOICE] Цикл виклику завершено, бот вийшов з голосового каналу.")


async def handle_call_dispatch(thread: discord.Thread, report: str, disp: dict, mention: str | None):
    # Опитування завершене — Gemini більше не веде цю гілку.
    sessions.pop(thread.id, None)

    # Проміжне повідомлення заявнику (гілку поки НЕ закриваємо — чекаємо реагування).
    await thread.send(
        f"{mention or ''}\n🚔 **Виклик прийнято до опрацювання** та передано патрульним екіпажам. "
        f"Очікуйте підтвердження реагування."
    )

    # Картка у фракційний канал із сигналами реагування.
    faction_channel = bot.get_channel(FACTION_CHANNEL_ID)
    if faction_channel is None:
        faction_channel = await bot.fetch_channel(FACTION_CHANNEL_ID)

    header = f"<@&{ROLE_ID_TO_PING}>\n🚨 **НОВИЙ ВИКЛИК 102 — ОПЕРАТИВНЕ РЕАГУВАННЯ**\n\n"
    footer = "\n\n*(✅ — прийняти виклик · ⛔ — екіпаж зайнятий/недоступний)*"
    faction_message = await faction_channel.send(
        header + report + footer,
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )
    await faction_message.add_reaction(EMOJI_ACCEPT)
    await faction_message.add_reaction(EMOJI_BUSY)

    # Окремою задачею: голос + очікування реагування (не блокує нові виклики).
    asyncio.create_task(run_callout(faction_message, report, disp, thread, mention))


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
            await thread.send("⚠️ Перешкоди на лінії зв'язку. Заявнику, повторіть останнє повідомлення.")
            return

        if not reply:
            await thread.send("Заявнику, Вас погано чути. Повторіть чіткіше.")
            return

        disp = session["dispatcher"]

        if COMPLETION_TAG in reply:
            report = reply.replace(COMPLETION_TAG, "").strip()
            await handle_call_dispatch(thread, report, disp, mention)

        elif INVALID_TAG in reply:
            msg = reply.replace(INVALID_TAG, "").strip()
            if msg:
                await thread.send(msg)
            hung_up = "*Скинув слухавку.*" if disp["gender"] == "male" else "*Скинула слухавку.*"
            await thread.send(f"Зв'язок розірвано. {hung_up}")
            try:
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
            sessions.pop(thread.id, None)

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

