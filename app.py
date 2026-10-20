import os
import re
import json
import time
import random
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pytz
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

# --- Logging Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Configuration ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROUP_CHAT_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'A',
    'В': 'B', 'в': 'B',
    'С': 'C', 'с': 'C',
    'D': 'D', 'd': 'D', 'Д': 'D', 'д': 'D'
}

# --- Vulgarity, Harassment & Profanity Filter ---
VULGAR_PATTERNS = [
    # English vulgarities, harassment, and violence
    r"fuck\w*",
    r"shit\w*",
    r"bitch\w*",
    r"asshole\w*",
    r"bastard\w*",
    r"cunt\w*",
    r"dick\w*",
    r"pussy\w*",
    r"rape\w*",
    r"rapis\w*",
    r"slut\w*",
    r"whore\w*",
    # Russian / Slavic profanities and insults (Latin + Cyrillic)
    r"na[hx]u[iyj]\w*",
    r"nax\b",
    r"нах\b",
    r"наху[йяеё]\w*",
    r"поху[йяеё]\w*",
    r"ху[йяеёи]\w*",
    r"xuy\w*",
    r"пизд\w*",
    r"pizd\w*",
    r"бля\w*",
    r"blya\w*",
    r"еба\w*",
    r"ёб\w*",
    r"yob\w*",
    r"yeb\w*",
    r"сука\w*",
    r"сучк\w*",
    r"suka\w*",
    r"suchk\w*",
    r"мудак\w*",
    r"mudak\w*",
    r"гандон\w*",
    r"gandon\w*",
    r"презерватив\w*",
    r"лох\w*",
    r"lox\w*",
    r"чмо\w*",
    r"chmo\w*",
    r"дебил\w*",
    r"debil\w*",
    r"далба[её]б\w*",
    r"dalba[ye]ob\w*",
    # Uzbek profanities, slurs, and insults
    r"jalap\w*",
    r"jala\b",
    r"жалап\w*",
    r"жалаб\w*",
    r"qanjiq\w*",
    r"қанжиқ\w*",
    r"sik\w*",
    r"сики\w*",
    r"сик\w*",
    r"qo[t']oq\w*",
    r"қотоқ\w*",
    r"onangni\w*",
    r"otangni\w*",
    r"ona[nŋ]g\w*",
    r"itvachcha\w*",
    r"padariga\w*",
    r"iflos\w*",
    r"xezalak\w*",
    r"гей\w*",
    r"gey\w*",
]

VULGAR_REGEX = re.compile(
    r"(?i)\b(" + "|".join(VULGAR_PATTERNS) + r")\b",
    re.UNICODE
)

# Secondary character-substitution map to prevent common leetspeak bypasses
LEET_MAP = str.maketrans({
    '@': 'a',
    '4': 'a',
    '0': 'o',
    '1': 'i',
    '!': 'i',
    '$': 's',
    '3': 'e',
    'x': 'h',
})

def is_vulgar(text: str) -> bool:
    """Checks if a message contains blocked profanities, spaced bypasses, or leetspeak."""
    if not text:
        return False

    # 1. Standard word boundary check
    if VULGAR_REGEX.search(text):
        return True

    # 2. Check normalized leetspeak substitutions
    normalized_text = text.translate(LEET_MAP)
    if VULGAR_REGEX.search(normalized_text):
        return True

    # 3. Detect spaced-out evasions (e.g., 'n a x u y', 'l o x', 'r a p e')
    condensed = re.sub(r"[^\w]", "", normalized_text.lower())
    if VULGAR_REGEX.search(condensed):
        return True

    return False

# Strict single-letter match: rejects "a b c d", "abcd", or random chatter
ANSWER_PATTERN = re.compile(r"^\s*([A-Da-dА-Ва-вСсДд])[.)]?\s*$")

TOPIC_POOLS = [
    "O'zbekiston tarixi: Qadimgi Baqtriya, So'g'diyona va Kushonlar davri",
    "O'zbekiston tarixi: Somoniylar, Qoraxoniylar va Xorazmshohlar davlati",
    "O'zbekiston tarixi: Amir Temur harbiy yurishlari va diplomatik aloqalari",
    "O'zbekiston tarixi: Turkiston jadidchilik harakati va 20-asr fojialari",
    "Jahon tarixi: Qadimgi Mesopotamiya, Bobil va Finikiya sivilizatsiyasi",
    "Jahon tarixi: Qadimgi Rim senati, imperatorlari va mashhur janglari",
    "Jahon tarixi: O'rta asrlar Yevropasi, Salib yurishlari va feodalizm",
    "Jahon tarixi: Birinchi va Ikkinchi jahon urushidagi kam ma'lum burilish nuqtalari",
    "Astronomiya: Ekzosayyoralar, neytron yulduzlar va pulsar hodisalari",
    "Astronomiya: Galaktikalar to'qnashuvi, relyikt nurlanish va qora tuynuklar",
    "Kosmonavtika: Mashhur kosmik zondlar (Voyager, Cassini, James Webb) missiyalari",
    "Fizika: Nisbiylik nazariyasi, optik hodisalar va yorug'lik tabiati",
    "Fizika: Termodinamika qonunlari va zamonaviy kvant kashfiyotlari",
    "Kimyo: Davriy jadvaldagi radioaktiv elementlar va ularning ishlatilishi",
    "Kimyo: Kundalik hayotdagi g'aroyib kimyoviy reaksiyalar va qotishmalar",
    "Inson biologiyasi: Neyronlar, gormonlar, miya tuzilishi va qon aylanishi",
    "Biologiya va Zoologiya: Dengiz tubi (Abissal) mavjudotlari va ularning moslashuvi",
    "Biologiya va Botanika: Dunyodagi eng zaharli, kamyob va qadimgi o'simliklar",
    "Geografiya: Dengizga chiqish yo'li yo'q davlatlar, kutilmagan eksklavlar va chegaralar",
    "Geografiya: Dunyodagi eng xavfli vulqonlar, kanyonlar va tektonik yoriqlar",
    "Texnologiya: Internet protokollari, kriptografiya va kompyuter tarixi",
    "Adabiyot: Mumtoz adabiyot durdonalari va jahon falsafasi",
    "Mantiq va Deduksiya: Detektiv uslubidagi faktlar va chalkash mantiqiy masalalar"
]

ISLAMIC_TOPIC_POOLS = [
    "Islom dini va aqoid: Islom va iymon arkonlari, farz va vojib amallar, fiqh asoslari",
    "Islom tarixi: Payg'ambarimiz Muhammad (s.a.v.) siyratlari — Makka va Madina davri muhim voqealari",
    "Islom tarixi: Xulafoi Roshidin (Abu Bakr, Umar, Usmon, Ali r.a.) xalifaligi va adolat tizimi",
    "Islom tarixi: Movarounnahr buyuk muhaddislari (Imom Buxoriy, Imom Termiziy) va fiqh olimlari (Marg'inoniy)",
    "Islom tarixi: Islom oltin davri, Bag'doddagi 'Baytul-Hikma' akademiyasi va mutafakkirlar merosi",
    "Islomiy obidalar va geografiya: Ka'ba, Masjidul Aqso, Masjidun Nabaviy va qadimiy islom shaharlari",
    "Hadis ilmi va Qur'on tarixi: Mushafi Usmoniy jamlanishi va mashhur hadis to'plamlari"
]

QUESTION_ANGLES = [
    "Kutilmagan, ko'pchilik noto'g'ri taxmin qiladigan paradoksal fakt",
    "Aniq bir kashfiyot yoki tarixiy burilish ortida turgan shaxs",
    "Hodisaning sabab-oqibat mexanizmi",
    "Atamaning yoki nomning kelib chiqish tarixi (etimologiyasi)",
    "Noodatiy, kamchilik biladigan statistik yoki ilmiy qonuniyat"
]

round_state = {
    "active": False,
    "q_num": 1,
    "correct": None,
    "options": {},
    "explanation": "",
    "question_text": "",
    "attempted_users": set()
}

# --- Keep-Alive Web Server for Render ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Quiz bot is running 24/7 on Groq!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# --- Permission Helper ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user.id)
        return member.status in ["creator", "administrator"]
    except Exception as e:
        logger.error(f"Error verifying admin status: {e}")
        return False

# --- Pinned Storage Helpers ---
async def get_or_create_leaderboard(bot):
    chat = await bot.get_chat(GROUP_CHAT_ID)
    pinned = chat.pinned_message
    pinned_text = getattr(pinned, "text", "") or ""

    if pinned and "YETAKCHILAR JADVALI" in pinned_text:
        return pinned
    
    init_text = (
        "🏆 1 MILLION QUESTIONS · YETAKCHILAR JADVALI\n"
        "Joriy savol: #0000001\n\n"
        "— Hozircha ballar yo‘q —\n\n"
        "────────────────────\n"
        "📜 So‘nggi savollar tarixi:\n"
        "• Savollar boshlanmoqda..."
    )
    msg = await bot.send_message(GROUP_CHAT_ID, init_text)
    try:
        await bot.pin_chat_message(GROUP_CHAT_ID, msg.message_id)
    except Exception as e:
        logger.error(f"Could not pin message: {e}")
    return msg

def parse_pin_data(pin_text):
    q_match = re.search(r"Joriy savol: #(\d+)", pin_text)
    current_q = int(q_match.group(1)) if q_match else 1
    
    scores = {}
    history = []
    is_history_section = False
    
    for line in pin_text.split("\n"):
        line = line.strip()
        if "So‘nggi savollar tarixi:" in line:
            is_history_section = True
            continue
        
        if not is_history_section:
            m = re.match(r"^[🥇🥈🥉\d.]+\s*(.+?)\s*—\s*(\d+)\s*ball", line)
            if m:
                scores[m.group(1)] = int(m.group(2))
        else:
            if line.startswith("•") and "boshlanmoqda" not in line:
                history.append(line.replace("•", "").strip())
                
    return current_q, scores, history

def format_top10_scores(scores_dict):
    if not scores_dict:
        return "— Hozircha ballar yo‘q —"
    sorted_scores = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (name, pts) in enumerate(sorted_scores, start=1):
        prefix = medals[idx-1] if idx <= 3 else f"{idx}."
        lines.append(f"{prefix} {name} — {pts} ball")
    return "\n".join(lines)

async def update_pin(bot, next_q_num, scores_dict, new_question_summary=None):
    try:
        pin = await get_or_create_leaderboard(bot)
        _, _, history = parse_pin_data(getattr(pin, "text", "") or "")
        
        if new_question_summary:
            clean_summary = new_question_summary.replace("\n", " ").strip()[:90]
            history.insert(0, clean_summary)
            history = history[:15]  # Keep last 15 questions in memory
            
        scores_body = format_top10_scores(scores_dict)
        history_lines = [f"• {item}" for item in history] if history else ["• Savollar tarixi boshlanmoqda..."]
        history_body = "\n".join(history_lines)
        
        new_text = (
            f"🏆 1 MILLION QUESTIONS · YETAKCHILAR JADVALI\n"
            f"Joriy savol: #{next_q_num:07d}\n\n"
            f"{scores_body}\n\n"
            f"────────────────────\n"
            f"📜 So‘nggi savollar tarixi:\n"
            f"{history_body}"
        )
        await bot.edit_message_text(chat_id=GROUP_CHAT_ID, message_id=pin.message_id, text=new_text)
        return scores_body
    except Exception as e:
        logger.error(f"Failed to update pin: {e}")
        return ""

# --- Dynamic Groq Model Selector ---
PREFERRED_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b"
]

def get_working_groq_model():
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        res = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=8)
        data = res.json()
        if "data" in data:
            all_ids = [m["id"] for m in data["data"]]
            for model_id in PREFERRED_MODELS:
                if model_id in all_ids:
                    return model_id
    except Exception as e:
        logger.error(f"Failed to inspect Groq model list: {e}")
    return "openai/gpt-oss-20b"

def fetch_question(q_num, recent_history):
    # Cadence: 1 in every 4 questions is Islamic history / religion
    if q_num % 4 == 0:
        chosen_topic = random.choice(ISLAMIC_TOPIC_POOLS)
    else:
        chosen_topic = random.choice(TOPIC_POOLS)

    chosen_angle = random.choice(QUESTION_ANGLES)
    cache_buster = f"{int(time.time())}_{random.randint(1000, 9999)}"
    history_block = "\n".join([f"- {item}" for item in recent_history]) if recent_history else "Hozircha yo'q"

    prompt = f"""
    Sen Telegram viktorinasi uchun ORIGINAL va TAKRORLANMAS savol tuzuvchisan.
    Hozirgi savol raqami: #{q_num} / 1 000 000.
    Unikal kalit: {cache_buster}.
    
    MAVZU: {chosen_topic}
    USLUB: {chosen_angle}
    
    QAT'IY QOIDALAR:
    1. QUYIDAGI SO'NGGI SAVOLLARNI VA ULARNING JAVOBLARINI ASLO TAKRORLAMA:
    {history_block}
    
    2. Eng sodda, qolipli maktab savollarini (Merkuriy, Everest, Temur qachon tug'ilgan, Umarxon, Brom, Simob) aslo berma!
    3. Agar mavzu Islom dini yoki tarixiga oid bo'lsa, savol faqat ishonchli (sahih) manbalar asosida, hurmat bilan va nozik ixtilofli masalalarga kirmasdan tuzilsin.
    4. Savol faktik jihatdan 100% to'g'ri, chuqur, o'rta yoki qiyin darajada bo'lsin.
    5. 4 ta variant (A, B, C, D) ber. Faqat 1 tasi to'g'ri bo'lsin.
    
    Faqat quyidagi JSON formatida to'g'ridan-to'g'ri javob ber (hech qanday markdown kod blokisiz):
    {{
        "subject": "{chosen_topic.split(':')[0].strip()}",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa tushuntirish (1-2 gap)"
    }}
    """
    
    selected_model = get_working_groq_model()
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": selected_model,
        "messages": [
            {"role": "system", "content": "Faqat to'g'ridan-to'g'ri yaroqli JSON qaytar."},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.85
    }
    
    try:
        res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=15)
        data = res.json()
        if "error" in data:
            logger.error(f"Groq API Error: {data['error'].get('message')}")
            raise ValueError(data['error'].get('message'))
            
        raw_text = data['choices'][0]['message']['content'].strip()
        parsed = json.loads(raw_text)
        logger.info(f"Groq generated: {parsed.get('question')[:40]}...")
        return parsed
    except Exception as e:
        logger.error(f"Groq request failed: {e}", exc_info=True)
        return {
            "subject": "Islom tarixi",
            "question": "Payg'ambarimiz (s.a.v.) Madinaga hijrat qilganlarida ularning tuyalari dastlab kimning hovlisiga cho'kkan?",
            "options": {"A": "Abu Ayyub al-Ansoriy", "B": "Sa'd ibn Uboda", "C": "As'ad ibn Zurora", "D": "Usmon ibn Maz'un"},
            "correct": "A",
            "explanation": "Payg'ambarimiz (s.a.v.) Madinaga yetib kelganlarida Qasva nomli tuyalari Abu Ayyub al-Ansoriy (r.a.) uyining oldiga cho'kkan."
        }

# --- Round Engine ---
async def post_hourly_question(app):
    global round_state
    
    pin = await get_or_create_leaderboard(app.bot)
    q_num, _, history = parse_pin_data(getattr(pin, "text", "") or "")
    
    data = fetch_question(q_num, history)
    
    round_state["active"] = True
    round_state["q_num"] = q_num
    round_state["correct"] = data["correct"].upper()
    round_state["options"] = data["options"]
    round_state["explanation"] = data["explanation"]
    round_state["question_text"] = data["question"]
    round_state["attempted_users"].clear()
    
    msg_text = (
        f"🏆 1 MILLION QUESTIONS · #{q_num:07d}\n"
        f"📚 Fan: {data['subject']}\n\n"
        f"❓ {data['question']}\n\n"
        f"A) {data['options']['A']}\n"
        f"B) {data['options']['B']}\n"
        f"C) {data['options']['C']}\n"
        f"D) {data['options']['D']}\n\n"
        f"⚡ To‘g‘ri variant harfini yozing (A, B, C yoki D)! Har bir kishiga 1 ta urinish."
    )
    await app.bot.send_message(GROUP_CHAT_ID, msg_text)
    logger.info(f"Question #{q_num} posted. Correct: {round_state['correct']}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global round_state
    
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # 1. SILENT PROFANITY CENSOR (Deletes bad messages immediately with 0 tokens)
    if is_vulgar(text):
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete vulgar message: {e}")
        return

    # Don't process non-group messages
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    # 2. QUIZ LOGIC
    if not round_state["active"]:
        return

    match = ANSWER_PATTERN.match(text)
    if not match:
        return

    user = update.message.from_user

    # Repeat attempts: silently delete and say nothing
    if user.id in round_state["attempted_users"]:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete spam attempt: {e}")
        return

    # First attempt: register user attempt
    round_state["attempted_users"].add(user.id)
    raw_choice = match.group(1)
    choice = CYRILLIC_TO_LATIN.get(raw_choice, raw_choice).upper()

    try:
        if choice == round_state["correct"]:
            round_state["active"] = False
            winner_name = user.first_name or "O‘yinchi"
            
            pin = await get_or_create_leaderboard(context.bot)
            q_num, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
            scores[winner_name] = scores.get(winner_name, 0) + 10
            
            leaderboard_str = await update_pin(
                context.bot,
                next_q_num=q_num + 1,
                scores_dict=scores,
                new_question_summary=round_state["question_text"]
            )
            
            correct_letter = round_state["correct"]
            ans_text = round_state["options"].get(correct_letter, "")
            
            reply = (
                f"🎯 TO‘G‘RI JAVOB!\n\n"
                f"⚡ G‘olib: {winner_name} (+10 ball!)\n"
                f"✅ Javob: {correct_letter}) {ans_text}\n"
                f"💡 Izoh: {round_state['explanation']}\n\n"
                f"📊 TOP-10 REYTINQ:\n"
                f"{leaderboard_str}\n\n"
                f"⏳ Keyingi savol keyingi soat boshida chiqadi."
            )
            await update.message.reply_text(reply)
        else:
            # Announce first incorrect attempt so the player knows they are locked out
            await update.message.reply_text(f"❌ Noto‘g‘ri, {user.first_name}! Bu soat uchun o‘yindan chiqdingiz.")
    except Exception as e:
        logger.error(f"Error handling answer reply: {e}", exc_info=True)

# --- Commands ---
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    # Non-admin usage: silently delete command
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    await update.message.reply_text("🧪 Test savoli ishga tushirilmoqda...")
    await post_hourly_question(context.application)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    pin = await get_or_create_leaderboard(context.bot)
    q_num, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
    body = format_top10_scores(scores)
    
    reply = (
        f"🏆 1 MILLION QUESTIONS · TOP-10 YETAKCHILAR\n"
        f"Joriy savol: #{q_num:07d}\n\n"
        f"{body}"
    )
    await update.message.reply_text(reply)

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global round_state
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    
    # Non-admin usage: silently delete command
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    if not round_state["active"]:
        await update.message.reply_text("ℹ️ Hozirda o'tkazib yuborish uchun faol savol yo'q.")
        return

    round_state["active"] = False
    correct_letter = round_state["correct"]
    ans_text = round_state["options"].get(correct_letter, "")
    explanation = round_state["explanation"]
    q_text = round_state["question_text"]
    
    pin = await get_or_create_leaderboard(context.bot)
    q_num, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
    
    await update_pin(
        context.bot,
        next_q_num=q_num + 1,
        scores_dict=scores,
        new_question_summary=q_text
    )
    
    reply = (
        f"⏭ SAVOL O'TKAZIB YUBORILDI (Admin tomonidan)\n\n"
        f"✅ To‘g‘ri javob: {correct_letter}) {ans_text}\n"
        f"💡 Izoh: {explanation}\n\n"
        f"⏳ Keyingi savol yangi soat boshida yoki /test orqali chiqadi."
    )
    await update.message.reply_text(reply)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    
    # Non-admin usage: silently delete command
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    pin = await get_or_create_leaderboard(context.bot)
    q_num, _, _ = parse_pin_data(getattr(pin, "text", "") or "")
    
    await update_pin(
        context.bot,
        next_q_num=q_num,
        scores_dict={},
        new_question_summary=None
    )
    
    await update.message.reply_text("🔄 Barcha ishtirokchilarning ochkolari (ballari) muvaffaqiyatli nollashtirildi!")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    now = datetime.now(TASHKENT_TZ)
    seconds_to_wait = (60 - now.minute - 1) * 60 + (60 - now.second)
    mins = seconds_to_wait // 60
    secs = seconds_to_wait % 60
    
    is_alive = scheduler_task is not None and not scheduler_task.done()
    task_status = "🟢 Ishlayapti (Faol)" if is_alive else "🔴 To'xtab qolgan (Xato)"

    msg = (
        f"🤖 BOT TIZIM HOLATI:\n\n"
        f"🕒 Toshkent vaqti: {now.strftime('%H:%M:%S')}\n"
        f"⚙️ Jadval vazifasi (Scheduler): {task_status}\n"
        f"⏳ Keyingi soatlik savolgacha: {mins} daqiqa {secs} soniya\n"
        f"🎯 Faol savol bormi: {'Ha' if round_state['active'] else 'Yo‘q'}\n"
    )
    await update.message.reply_text(msg)

# Strong global reference so Python GC never kills the scheduler
scheduler_task = None

async def scheduler(app):
    while True:
        try:
            now = datetime.now(TASHKENT_TZ)
            seconds_to_wait = (60 - now.minute - 1) * 60 + (60 - now.second)
            logger.info(f"Tashkent time: {now.strftime('%H:%M:%S')}. Next drop in {seconds_to_wait}s.")
            
            await asyncio.sleep(seconds_to_wait)
            await post_hourly_question(app)
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Scheduler error caught (loop protected): {e}", exc_info=True)
            await asyncio.sleep(10)

async def on_startup(app):
    global scheduler_task
    scheduler_task = asyncio.create_task(scheduler(app))

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    app.run_polling()
