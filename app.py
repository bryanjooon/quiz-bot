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
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# Support comma-separated Group IDs or fallback single ID
raw_ids = os.getenv("TELEGRAM_GROUP_IDS", os.getenv("TELEGRAM_GROUP_ID", "0"))
GROUP_CHAT_IDS = [int(i.strip()) for i in raw_ids.split(",") if i.strip() and i.strip() != "0"]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'A',
    'В': 'B', 'в': 'B',
    'С': 'C', 'с': 'C',
    'D': 'D', 'd': 'D', 'Д': 'D', 'д': 'D'
}

INITIAL_WINDOW_SECONDS = 30

# --- Persistent Scores Storage per Chat ID ---
SCORES_FILE = "scores.json"
ALL_SCORES = {}  # { chat_id_str: { player_name: score } }

def load_scores():
    global ALL_SCORES
    if os.path.exists(SCORES_FILE):
        try:
            with open(SCORES_FILE, "r", encoding="utf-8") as f:
                ALL_SCORES = json.load(f)
        except Exception as e:
            logger.error(f"Error loading scores.json: {e}")

def save_scores():
    try:
        with open(SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(ALL_SCORES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving scores.json: {e}")

load_scores()

def get_chat_scores(chat_id: int) -> dict:
    cid = str(chat_id)
    if cid not in ALL_SCORES:
        ALL_SCORES[cid] = {}
    return ALL_SCORES[cid]

# --- Vulgarity, Harassment & Profanity Filter ---
VULGAR_PATTERNS = [
    r"fuck\w*", r"shit\w*", r"bitch\w*", r"asshole\w*", r"bastard\w*", r"cunt\w*",
    r"dick\w*", r"pussy\w*", r"rape\w*", r"rapis\w*", r"slut\w*", r"whore\w*",
    r"na[hx]u[iyj]\w*", r"nah\b", r"nax\b", r"нах\b", r"наху[йяеё]\w*", r"поху[йяеё]\w*",
    r"ху[йяеёи]\w*", r"xuy\w*", r"пизд\w*", r"pizd\w*", r"бля\w*", r"blya\w*",
    r"еба\w*", r"ёб\w*", r"yob\w*", r"yeb\w*", r"сука\w*", r"сучк\w*", r"suka\w*",
    r"suchk\w*", r"мудак\w*", r"mudak\w*", r"гандон\w*", r"gandon\w*", r"презерватив\w*",
    r"лох\w*", r"lox\w*", r"чмо\w*", r"chmo\w*", r"дебил\w*", r"debil\w*",
    r"далба[её]б\w*", r"dalba[ye]ob\w*",
    r"y+e*b+a+[nŋ]\w*", r"e+b+a+[nŋ]\w*", r"y+e*b+l+a+[nŋ]\w*", r"e+b+l+a+[nŋ]\w*",
    r"[её]+б+а+[нң]\w*", r"[её]+б+л+а+[нң]\w*", r"й+е+б+а+[нң]\w*", r"и+б+а+[нң]\w*", r"ы+б+а+[нң]\w*",
    r"jalap\w*", r"jala\b", r"жалап\w*", r"жалаб\w*", r"qanjiq\w*", r"қанжиқ\w*",
    r"sik\w*", r"сики\w*", r"сик\w*", r"qo[t']oq\w*", r"қотоқ\w*", r"onangni\w*",
    r"otangni\w*", r"ona[nŋ]g\w*", r"itvachcha\w*", r"padariga\w*", r"iflos\w*",
    r"xezalak\w*", r"гей\w*", r"gey\w*",
]

LEET_MAP = str.maketrans({
    '@': 'a', '4': 'a', '0': 'o', '1': 'i', '!': 'i', '$': 's', '3': 'e', 'x': 'h',
})

CUSTOM_BANNED_FILE = "banned_words.json"
CUSTOM_BANNED_WORDS = set()

def load_custom_banned():
    global CUSTOM_BANNED_WORDS
    if os.path.exists(CUSTOM_BANNED_FILE):
        try:
            with open(CUSTOM_BANNED_FILE, "r", encoding="utf-8") as f:
                CUSTOM_BANNED_WORDS = set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading custom banned words: {e}")

def save_custom_banned():
    try:
        with open(CUSTOM_BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(CUSTOM_BANNED_WORDS), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving custom banned words: {e}")

def compile_vulgar_regex():
    patterns = list(VULGAR_PATTERNS)
    for word in CUSTOM_BANNED_WORDS:
        escaped = re.escape(word.strip().lower())
        patterns.append(rf"{escaped}\w*")
    return re.compile(r"(?i)\b(" + "|".join(patterns) + r")\b", re.UNICODE)

load_custom_banned()
VULGAR_REGEX = compile_vulgar_regex()

def is_vulgar(text: str) -> bool:
    if not text:
        return False
    if VULGAR_REGEX.search(text):
        return True
    normalized_text = text.translate(LEET_MAP)
    if VULGAR_REGEX.search(normalized_text):
        return True
    condensed = re.sub(r"[\W_]", "", normalized_text.lower())
    if VULGAR_REGEX.search(condensed):
        return True
    return False

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
    "Tarixiy burilish yasagan keskin voqea yoki strategik qaror",
    "Hodisa yoki qonuniyatning sabab-oqibat mexanizmi",
    "Kamchilik biladigan, lekin muhim ahamiyatga ega ilmiy kashfiyot",
    "Mantiqiy deduksiya va chuqur tahlilni talab qiluvchi jumboq"
]

# Round State per Chat ID
def create_empty_round():
    return {
        "active": False,
        "q_num": 1,
        "correct": None,
        "options": {},
        "explanation": "",
        "detailed_explanation": "",
        "question_text": "",
        "attempted_users": set(),
        "in_initial_window": False,
        "answers": [],
        "message_id": None,
        "base_text": "",
        "answered_names": []
    }

ROUND_STATES = {}  # { chat_id: round_state_dict }

def get_round_state(chat_id: int) -> dict:
    if chat_id not in ROUND_STATES:
        ROUND_STATES[chat_id] = create_empty_round()
    return ROUND_STATES[chat_id]

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
    chat_id = update.effective_chat.id
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        return member.status in ["creator", "administrator"]
    except Exception as e:
        logger.error(f"Error verifying admin status: {e}")
        return False

# --- Pinned Storage Helpers per Group ---
async def get_or_create_leaderboard(bot, chat_id: int):
    chat = await bot.get_chat(chat_id)
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
    msg = await bot.send_message(chat_id, init_text)
    try:
        await bot.pin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        logger.error(f"Could not pin message in {chat_id}: {e}")
    return msg

def parse_pin_data(pin_text, chat_id: int):
    q_match = re.search(r"Joriy savol: #(\d+)", pin_text)
    current_q = int(q_match.group(1)) if q_match else 1
    
    history = []
    is_history_section = False
    
    for line in pin_text.split("\n"):
        line = line.strip()
        if "So‘nggi savollar tarixi:" in line:
            is_history_section = True
            continue
        
        if is_history_section:
            if line.startswith("•") and "boshlanmoqda" not in line:
                history.append(line.replace("•", "").strip())
                
    return current_q, get_chat_scores(chat_id), history

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

async def update_pin(bot, chat_id: int, next_q_num, scores_dict, new_question_summary=None):
    try:
        pin = await get_or_create_leaderboard(bot, chat_id)
        _, _, history = parse_pin_data(getattr(pin, "text", "") or "", chat_id)
        
        if new_question_summary:
            clean_summary = new_question_summary.replace("\n", " ").strip()[:90]
            history.insert(0, clean_summary)
            history = history[:15]
            
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
        await bot.edit_message_text(chat_id=chat_id, message_id=pin.message_id, text=new_text)
        return scores_body
    except Exception as e:
        logger.error(f"Failed to update pin in {chat_id}: {e}")
        return ""

def escape_tg_html(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def format_izoh_block(explanation: str, detailed: str) -> str:
    clean_expl = escape_tg_html(explanation)
    clean_detail = escape_tg_html(detailed)
    if clean_detail and clean_detail.strip() != clean_expl.strip():
        return (
            f"💡 <b>Izoh:</b> {clean_expl}\n\n"
            f"<blockquote expandable>📖 <b>Batafsil ma'lumot (o'qish uchun bosing):</b>\n\n"
            f"{clean_detail}

```python?code_reference&code_event_index=1
# Syntax check for multi-group app.py draft
test_multi = """
import os, re, json, time, random, asyncio, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pytz, requests

raw_ids = "-100123456789,-4821562106"
GROUP_CHAT_IDS = [int(i.strip()) for i in raw_ids.split(",") if i.strip() and i.strip() != "0"]
print("Parsed group IDs:", GROUP_CHAT_IDS)
"""
exec(test_multi)
