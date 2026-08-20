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
import static_ffmpeg
static_ffmpeg.add_paths()

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


async def play_voice_alert(report_text: str, disp: dict):
    print(f"\n--- [VOICE START] Початок обробки войсу ---")
    if not PATROL_VOICE_CHANNEL_IDS:
        print("[VOICE ERROR] PATROL_VOICE_CHANNEL_IDS порожній!")
        return

    # 1. Пошук каналів з людьми
    target_channels = []
    for ch_id in PATROL_VOICE_CHANNEL_IDS:
        ch = bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await bot.fetch_channel(ch_id)
            except Exception:
                continue

        if isinstance(ch, discord.VoiceChannel):
            humans = [m for m in ch.members if not m.bot]
            if humans:
                target_channels.append(ch)

    if not target_channels:
        first_id = PATROL_VOICE_CHANNEL_IDS[0]
        ch = bot.get_channel(first_id)
        if ch and isinstance(ch, discord.VoiceChannel):
            target_channels.append(ch)
        else:
            print("[VOICE] Голосових каналів не знайдено.")
            return

    # 2. Формування тексту і генерація MP3
    clean_text = re.sub(r"[•*#_]", "", report_text)
    speech_text = (
        f"Увага всім патрулям! Говорить черговий диспетчер {disp['name']}. "
        f"Надійшов терміновий виклик: {clean_text}. Інформація в каналі зв'язку."
    )

    temp_audio = f"voice_{random.randint(10000, 99999)}.mp3"
    try:
        print(f"[VOICE] Генерація TTS ({disp['voice']})...")
        communicate = edge_tts.Communicate(speech_text, disp["voice"], rate="+5%")
        await communicate.save(temp_audio)
        print("[VOICE] Аудіофайл створено.")

        # Опції для стабільного програвання FFmpeg в Discord
        ffmpeg_options = {
            'options': '-vn -filter:a "volume=1.3"'
        }

        for channel in target_channels:
            print(f"[VOICE] Підключення до '{channel.name}'...")
            try:
                vc = None
                for client in bot.voice_clients:
                    if client.guild.id == channel.guild.id:
                        vc = client
                        break

                if vc is None or not vc.is_connected():
                    vc = await channel.connect(timeout=15.0, reconnect=True)
                elif vc.channel.id != channel.id:
                    await vc.move_to(channel)

                # Невелика пауза для синхронізації з Discord Voice Server
                await asyncio.sleep(1.0)

                if vc.is_playing():
                    vc.stop()

                # Подія завершення відтворення
                play_done = asyncio.Event()

                def after_playback(error):
                    if error:
                        print(f"[VOICE ERROR] Помилка відтворення: {error}")
                    bot.loop.call_soon_threadsafe(play_done.set)

                audio_source = discord.FFmpegPCMAudio(temp_audio, **ffmpeg_options)
                
                # Обгортаємо в Transformer для стабільності бітрейту
                audio_source = discord.PCMVolumeTransformer(audio_source, volume=1.0)

                print(f"[VOICE] Початок мовлення у '{channel.name}'...")
                vc.play(audio_source, after=after_playback)

                # Чекаємо поки звук реально закінчиться (або максимум 60 сек)
                try:
                    await asyncio.wait_for(play_done.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    print("[VOICE TIMEOUT] Таймаут програвання.")

                print(f"[VOICE] Мовлення у '{channel.name}' завершено.")
                await asyncio.sleep(1.0)

            except Exception as err:
                print(f"[VOICE CHANNEL ERROR] {channel.name}: {err}")
                log.exception("Voice Playback Exception")

        # Відключення
        for client in list(bot.voice_clients):
            try:
                await client.disconnect(force=True)
            except Exception:
                pass
        print("[VOICE] Бот відключився від голосових.\n--- [VOICE END] ---")

    except Exception as err:
        print(f"[VOICE CRITICAL ERROR]: {err}")
        log.exception("Voice Critical")
    finally:
        if os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except OSError:
                pass


async def handle_call_dispatch(thread: discord.Thread, report: str, disp: dict, mention: str | None):
    # 1. Відповідь громадянину в гілку і миттєве закриття лінії
    citizen_reply = (
        f"{mention or ''}\n🚔 **Інформацію прийнято та зареєстровано!** "
        f"Черговий екіпаж патрульної поліції вже направлено за вказаною адресою. "
        f"Залишайтеся в безпечному місці та очікуйте на прибуття співробітників."
    )
    await thread.send(citizen_reply)
    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass
    sessions.pop(thread.id, None)

    # 2. Запуск войсу у фоні
    asyncio.create_task(play_voice_alert(report, disp))

    # 3. Публікація картки виклику у фракційному каналі
    faction_channel = bot.get_channel(FACTION_CHANNEL_ID)
    if faction_channel is None:
        faction_channel = await bot.fetch_channel(FACTION_CHANNEL_ID)

    header = f"<@&{ROLE_ID_TO_PING}>\n🚨 **НОВИЙ ВИКЛИК 102 — ОПЕРАТИВНЕ РЕАГУВАННЯ**\n\n"
    report_message = await faction_channel.send(
        header + report + "\n\n*(Натисніть ✅, щоб закріпити виклик за собою)*",
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True)
    )
    await report_message.add_reaction("✅")

    # 4. Нескінченне очікування взяття виклику екіпажем
    def check_take(reaction: discord.Reaction, user: discord.User):
        return (
            reaction.message.id == report_message.id
            and str(reaction.emoji) == "✅"
            and not user.bot
        )

    reaction, officer = await bot.wait_for("reaction_add", check=check_take)

    # Оновлення картки: виклик в роботі
    await report_message.clear_reactions()
    await report_message.edit(
        content=(
            f"🟡 **ВИКЛИК В РОБОТІ: {officer.mention}**\n\n"
            f"{report}\n\n"
            f"*(Після завершення ситуації натисніть 🏁 для закриття картки)*"
        )
    )
    await report_message.add_reaction("🏁")

    # 5. Нескінченне очікування завершення виклику екіпажем
    def check_finish(reaction: discord.Reaction, user: discord.User):
        return (
            reaction.message.id == report_message.id
            and str(reaction.emoji) == "🏁"
            and not user.bot
        )

    finish_reaction, closing_officer = await bot.wait_for("reaction_add", check=check_finish)

    # Фінальне оновлення картки: виклик завершено
    await report_message.clear_reactions()
    await report_message.edit(
        content=(
            f"🟢 **ВИКЛИК УСПІШНО ЗАВЕРШЕНО: {closing_officer.mention}**\n\n"
            f"{report}\n\n"
            f"*(Картку переміщено в архів)*"
        )
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
