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
import aiohttp
import static_ffmpeg
static_ffmpeg.add_paths()

# Приймання голосу (детекція «хто говорить») — окреме розширення discord.py.
try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except Exception:
    voice_recv = None
    VOICE_RECV_AVAILABLE = False

# ── Завантаження Opus ──────────────────────────────────────────────────────
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

# ElevenLabs — живий голос. Без ключа бот падає на безкоштовний edge-tts.
ELEVEN_API_KEY     = os.getenv("ELEVEN_API_KEY", "").strip()
ELEVEN_MODEL       = os.getenv("ELEVEN_MODEL", "eleven_multilingual_v2")
# voice_id бери в ElevenLabs → Voice Library / My Voices (три крапки → Copy Voice ID).
# Дефолти — стабільні premade-голоси; заміни на ті, що краще звучать українською.
ELEVEN_VOICE_MALE   = os.getenv("ELEVEN_VOICE_MALE", "pNInz6obpgDQGcFmaJgB")    # Adam
ELEVEN_VOICE_FEMALE = os.getenv("ELEVEN_VOICE_FEMALE", "21m00Tcm4TlvDq8ikWAM")  # Rachel

# Як екіпаж підтверджує прийняття у войсі: "speech" — голосом (треба voice-recv),
# "reaction" — реакцією ✅ під карткою (надійніше, якщо приймання голосу капризує).
VOICE_ACK_MODE = os.getenv("VOICE_ACK_MODE", "speech").strip().lower()

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

EMOJI_ACCEPT = "✅"   # прийняти виклик
EMOJI_BUSY   = "⛔"   # зайнятий / недоступний

VOICE_ACK_TIMEOUT   = 40    # скільки чекати відповіді екіпажу у войсі, сек
TEXT_ACCEPT_TIMEOUT = 180   # скільки чекати реакції, коли у войсі нікого немає, сек

# Бот один — не може бути у двох войсах одночасно. Голосові виклики — по черзі.
VOICE_LOCK = asyncio.Lock()

DISPATCHERS_MALE = [
    {"name": "Олексій Ткаченко", "callsign": "102-Альфа", "role_desc": "черговий диспетчер", "gender": "male"},
    {"name": "Максим Бондаренко", "callsign": "102-Омега", "role_desc": "старший оператор лінії", "gender": "male"},
    {"name": "Дмитро Козак", "callsign": "102-Браво", "role_desc": "черговий частини", "gender": "male"},
    {"name": "Артем Шевчук", "callsign": "102-Дельта", "role_desc": "диспетчер зв'язку", "gender": "male"}
]

DISPATCHERS_FEMALE = [
    {"name": "Світлана Мельник", "callsign": "102-Венера", "role_desc": "чергова диспетчерка", "gender": "female"},
    {"name": "Вікторія Коваль", "callsign": "102-Стріла", "role_desc": "старша операторка лінії", "gender": "female"},
    {"name": "Олена Мороз", "callsign": "102-Зоря", "role_desc": "чергова частини", "gender": "female"},
    {"name": "Катерина Бойко", "callsign": "102-Фенікс", "role_desc": "диспетчерка зв'язку", "gender": "female"}
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

log.info("[VOICE] Приймання голосу (voice-recv): %s", "доступне" if VOICE_RECV_AVAILABLE else "НЕ встановлено")
log.info("[VOICE] Рушій озвучення: %s", "ElevenLabs" if ELEVEN_API_KEY else "edge-tts (fallback)")


def resolve_ffmpeg() -> str:
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

# ── Клієнти ────────────────────────────────────────────────────────────────
genai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.all()
bot = discord.Client(intents=intents)

sessions: dict[int, dict] = {}


# Сінк, що фіксує факт мовлення живого учасника (без декодування — легкий).
if VOICE_RECV_AVAILABLE:
    class SpeakingSink(voice_recv.AudioSink):
        def __init__(self, on_speak):
            super().__init__()
            self._on_speak = on_speak

        def wants_opus(self) -> bool:
            return True  # не декодуємо PCM — нам потрібен лише факт голосу

        def write(self, user, data):
            if user is not None and not getattr(user, "bot", False):
                self._on_speak()

        def cleanup(self):
            pass
else:
    SpeakingSink = None


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

    config = types.GenerateContentConfig(
        system_instruction=generate_system_prompt(disp_data),
        safety_settings=SAFETY_SETTINGS,
        temperature=0.6,
    )
    chat = genai_client.aio.chats.create(model=GEMINI_MODEL, config=config)
    return chat, disp_data


async def ask_gemini(chat, text: str) -> str:
    response = await chat.send_message(text)
    return (response.text or "").strip()


# ── Синтез мовлення: ElevenLabs (живий) з фолбеком на edge-tts ─────────────

async def _eleven_tts(text: str, voice_id: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
            "style": 0.35,
            "use_speaker_boost": True,
        },
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("[VOICE] ElevenLabs HTTP %s: %s", resp.status, body[:200])
                return b""
            return await resp.read()


async def _edge_tts(text: str, gender: str) -> bytes:
    voice = "uk-UA-OstapNeural" if gender == "male" else "uk-UA-PolinaNeural"
    try:
        buffer = bytearray()
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        return bytes(buffer)
    except Exception:
        log.exception("[VOICE] Помилка edge-tts.")
        return b""


async def synthesize_speech_bytes(text: str, disp: dict) -> bytes:
    gender = disp["gender"]
    if ELEVEN_API_KEY:
        voice_id = ELEVEN_VOICE_MALE if gender == "male" else ELEVEN_VOICE_FEMALE
        try:
            data = await _eleven_tts(text, voice_id)
            if data:
                return data
        except Exception:
            log.exception("[VOICE] ElevenLabs недоступний — фолбек на edge-tts.")
    return await _edge_tts(text, gender)


async def compose_announcement_audio(report: str, disp: dict) -> bytes:
    """Диспетчер сам формулює живе радіозвернення й одразу озвучує його."""
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
        log.exception("[VOICE] Не вдалося згенерувати текст звернення — шаблон.")

    if not speech_text:
        clean = re.sub(r"[•*#_`]", "", report).strip()
        speech_text = (
            f"Увага всім патрулям! Говорить черговий диспетчер {disp['name']}. "
            f"Надійшов терміновий виклик. {clean}. Інформація в каналі зв'язку."
        )
    return await synthesize_speech_bytes(speech_text, disp)


# ── Робота з голосом ───────────────────────────────────────────────────────

def find_crew_channel() -> discord.VoiceChannel | None:
    for ch_id in PATROL_VOICE_CHANNEL_IDS:
        ch = bot.get_channel(ch_id)
        if isinstance(ch, discord.VoiceChannel) and any(not m.bot for m in ch.members):
            return ch
    return None


async def _clean_existing(guild_id: int):
    existing = next((c for c in bot.voice_clients if c.guild.id == guild_id), None)
    if existing:
        try:
            await existing.disconnect(force=True)
        except Exception:
            pass


async def connect_to(channel: discord.VoiceChannel, use_recv: bool) -> discord.VoiceClient:
    await _clean_existing(channel.guild.id)
    if use_recv:
        return await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=20.0, reconnect=True)
    return await channel.connect(timeout=20.0, reconnect=True)


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
    source = discord.FFmpegOpusAudio(
        io.BytesIO(audio_bytes),
        pipe=True,
        executable=FFMPEG_PATH,
        bitrate=128,
        options='-af loudnorm=I=-16:TP=-1.5:LRA=11',
    )
    vc.play(source, after=after_playback)
    try:
        await asyncio.wait_for(play_done.wait(), timeout=90.0)
    except asyncio.TimeoutError:
        log.warning("[VOICE] Таймаут відтворення.")


async def speak(vc: discord.VoiceClient, text: str, disp: dict):
    await play_audio_bytes(vc, await synthesize_speech_bytes(text, disp))


async def disconnect_all():
    for client in list(bot.voice_clients):
        try:
            await client.disconnect(force=True)
        except Exception:
            pass


async def listen_for_voice_ack(vc, timeout: float) -> bool:
    """Чекаємо, поки хтось із живих учасників заговорить. True — відповіли."""
    if not (VOICE_RECV_AVAILABLE and SpeakingSink):
        return False
    spoke = asyncio.Event()
    try:
        vc.listen(SpeakingSink(lambda: bot.loop.call_soon_threadsafe(spoke.set)))
    except Exception:
        log.exception("[VOICE] Не вдалося почати приймання голосу.")
        return False
    try:
        await asyncio.wait_for(spoke.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        try:
            vc.stop_listening()
        except Exception:
            pass


async def wait_for_reaction(message: discord.Message, emojis: set[str], timeout: float):
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


# ── Сценарії реагування ────────────────────────────────────────────────────

CITIZEN_ACCEPTED = ("🚔 **Екіпаж патрульної поліції прийняв Ваш виклик** і прямує за вказаною адресою. "
                    "Залишайтеся в безпечному місці до прибуття служб.")
CITIZEN_UNAVAILABLE = ("❌ **Наразі всі вільні екіпажі зайняті або відсутні на лінії реагування.** "
                       "Заяву внесено до журналу обліку. За потреби зверніться до найближчого відділку поліції.")


async def voice_flow(crew, faction_message, report, disp, thread, mention) -> bool:
    """Екіпаж є у войсі: заходимо, оголошуємо голосом, чекаємо відповідь. True — оброблено."""
    use_speech = VOICE_ACK_MODE == "speech" and VOICE_RECV_AVAILABLE
    try:
        vc, announce_audio = await asyncio.gather(
            connect_to(crew, use_recv=use_speech),
            compose_announcement_audio(report, disp),
        )
    except Exception:
        log.exception("[VOICE] Захід у войс не вдався — перехід на текст.")
        return False

    await asyncio.sleep(0.4)
    await play_audio_bytes(vc, announce_audio)

    if use_speech:
        log.info("[VOICE] Чекаю голосову відповідь екіпажу до %ss...", VOICE_ACK_TIMEOUT)
        accepted = await listen_for_voice_ack(vc, VOICE_ACK_TIMEOUT)
        taken_by = crew.name
    else:
        await faction_message.add_reaction(EMOJI_ACCEPT)
        await faction_message.add_reaction(EMOJI_BUSY)
        emoji, officer = await wait_for_reaction(faction_message, {EMOJI_ACCEPT, EMOJI_BUSY}, VOICE_ACK_TIMEOUT)
        await faction_message.clear_reactions()
        accepted = emoji == EMOJI_ACCEPT
        taken_by = officer.mention if officer else crew.name

    if accepted:
        await speak(vc, "Виклик прийнято, закріплюю за вашим екіпажем. Дякую, відбій.", disp)
        await faction_message.edit(content=f"🟢 **ВИКЛИК ПРИЙНЯТО ЕКІПАЖЕМ · {taken_by}**\n\n{report}")
        await close_thread_with(thread, f"{mention or ''}\n{CITIZEN_ACCEPTED}")
    else:
        await speak(vc, "Відповіді від екіпажу немає. Екіпаж вважається недоступним. Відбій.", disp)
        await faction_message.edit(content=f"🔴 **ЕКІПАЖ НЕ ВІДПОВІВ — НЕДОСТУПНІ**\n\n{report}")
        await close_thread_with(thread, f"{mention or ''}\n{CITIZEN_UNAVAILABLE}")
    return True


async def text_flow(faction_message, report, disp, thread, mention):
    """У войсі нікого: підтвердження реакціями під карткою."""
    await faction_message.edit(content=faction_message.content + "\n\n*(✅ — прийняти · ⛔ — зайнятий/недоступний)*")
    await faction_message.add_reaction(EMOJI_ACCEPT)
    await faction_message.add_reaction(EMOJI_BUSY)
    emoji, officer = await wait_for_reaction(faction_message, {EMOJI_ACCEPT, EMOJI_BUSY}, TEXT_ACCEPT_TIMEOUT)
    await faction_message.clear_reactions()

    if emoji == EMOJI_ACCEPT:
        await faction_message.edit(content=f"🟢 **ВИКЛИК ПРИЙНЯТО: {officer.mention}**\n\n{report}")
        await close_thread_with(thread, f"{mention or ''}\n{CITIZEN_ACCEPTED}")
    else:
        await faction_message.edit(content=f"🔴 **ЕКІПАЖ НЕДОСТУПНИЙ / ТАЙМАУТ**\n\n{report}")
        await close_thread_with(thread, f"{mention or ''}\n{CITIZEN_UNAVAILABLE}")


async def run_callout(faction_message: discord.Message, report: str, disp: dict, thread: discord.Thread, mention: str | None):
    async with VOICE_LOCK:
        try:
            crew = find_crew_channel()
            handled = False
            if crew:
                handled = await voice_flow(crew, faction_message, report, disp, thread, mention)
            if not handled:
                await text_flow(faction_message, report, disp, thread, mention)
        except Exception:
            log.exception("[VOICE] Помилка у циклі виклику.")
        finally:
            await disconnect_all()
            log.info("[VOICE] Цикл виклику завершено.")


async def handle_call_dispatch(thread: discord.Thread, report: str, disp: dict, mention: str | None):
    sessions.pop(thread.id, None)

    await thread.send(
        f"{mention or ''}\n🚔 **Виклик прийнято до опрацювання** та передано патрульним екіпажам. "
        f"Очікуйте підтвердження реагування."
    )

    faction_channel = bot.get_channel(FACTION_CHANNEL_ID)
    if faction_channel is None:
        faction_channel = await bot.fetch_channel(FACTION_CHANNEL_ID)

    header = f"<@&{ROLE_ID_TO_PING}>\n🚨 **НОВИЙ ВИКЛИК 102 — ОПЕРАТИВНЕ РЕАГУВАННЯ**\n\n"
    faction_message = await faction_channel.send(
        header + report,
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )
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

