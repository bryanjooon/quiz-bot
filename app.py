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
EXPIRE_CLEANUP_SECONDS = 600  # 10 minutes
GLOBAL_USER_NAME = "biloliddin"

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

def update_player_score(chat_id: int, player_name: str, pts_to_add: int) -> int:
    chat_dict = get_chat_scores(chat_id)
    new_score = chat_dict.get(player_name, 0) + pts_to_add
    chat_dict[player_name] = new_score

    if player_name.lower() == GLOBAL_USER_NAME.lower():
        max_global = new_score
        for c_key, c_scores in ALL_SCORES.items():
            for p_key, p_val in c_scores.items():
                if p_key.lower() == GLOBAL_USER_NAME.lower() and p_val > max_global:
                    max_global = p_val
        
        for c_key in ALL_SCORES.keys():
            for p_key in list(ALL_SCORES[c_key].keys()):
                if p_key.lower() == GLOBAL_USER_NAME.lower():
                    ALL_SCORES[c_key][p_key] = max_global
            if GLOBAL_USER_NAME not in ALL_SCORES[c_key]:
                ALL_SCORES[c_key][GLOBAL_USER_NAME] = max_global

    save_scores()
    return chat_dict[player_name]

# --- Message Cleanup Sweeper ---
MESSAGES_TO_PURGE = []

def track_for_cleanup(chat_id: int, message_id: int, delay_seconds: int = EXPIRE_CLEANUP_SECONDS):
    if message_id:
        MESSAGES_TO_PURGE.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "delete_at": time.time() + delay_seconds
        })

async def message_cleaner_task(bot):
    while True:
        try:
            now = time.time()
            remaining = []
            for item in MESSAGES_TO_PURGE:
                if now >= item["delete_at"]:
                    try:
                        await bot.delete_message(chat_id=item["chat_id"], message_id=item["message_id"])
                        logger.info(f"Cleaned up message {item['message_id']} in chat {item['chat_id']}")
                    except Exception as e:
                        logger.warning(f"Failed to delete message {item['message_id']} in {item['chat_id']}: {e}")
                else:
                    remaining.append(item)
            MESSAGES_TO_PURGE[:] = remaining
        except Exception as e:
            logger.error(f"Error in cleaner loop: {e}")
        await asyncio.sleep(15)

# --- Vulgarity & Profanity Filter ---
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
    "O'zbekiston tarixi: Qadimgi Baqtriya, So'g'diyona va Kushonlar davri strategik burilishlari",
    "O'zbekiston tarixi: Somoniylar, Qoraxoniylar va Xorazmshohlar davlati diplomatik munosabatlari",
    "O'zbekiston tarixi: Amir Temur harbiy strategiyalari va xalqaro savdo bitimlari",
    "O'zbekiston tarixi: Turkiston jadidchilik harakati va ma'rifatparvarlar ijtimoiy tahlili",
    "Jahon tarixi: Qadimgi Mesopotamiya, Bobil va Finikiya sivilizatsiyasi huquqiy va iqtisodiy tizimi",
    "Jahon tarixi: Qadimgi Rim senati, imperatorlari va harbiy-siyosiy burilish nuqtalari",
    "Jahon tarixi: O'rta asrlar Yevropasi feodalizmining sabab-oqibat mexanizmlari",
    "Astronomiya: Ekzosayyoralar, neytron yulduzlar va fizik ko'rsatkichlar",
    "Fizika: Kvant mexanikasi, termodinamika va nisbiylik nazariyasi konseptual mantiqlari",
    "Biologiya va Neyrobiologiya: Miya tuzilishi, neyronlar, evolyutsion adaptatsiyalar",
    "Geografiya va Geopolitika: Eksklavlar, tektonik hodisalar va strategik bo'g'ozlar",
    "Mantiq va Deduksiya: Detektiv uslubidagi mantiqiy xulosalar va paradokslar"
]

ISLAMIC_TOPIC_POOLS = [
    "Islom tarixi va Fiqh: Shariat manbalari va fiqhiy qoidalar mantiqiy asoslari",
    "Islom tarixi: Payg'ambarimiz Muhammad (s.a.v.) siyratlari — Makka va Madina davri strategik qarorlari",
    "Islom tarixi: Xulafoi Roshidin (Abu Bakr, Umar, Usmon, Ali r.a.) adolat va davlat boshqaruvi tizimi",
    "Islom tarixi: Movarounnahr buyuk muhaddislari (Imom Buxoriy, Imom Termiziy) ilmiy metodologiyasi",
    "Islom tarixi: Islom oltin davri, Bag'doddagi 'Baytul-Hikma' akademiyasi ilmiy kashfiyotlari"
]

QUESTION_ANGLES = [
    "Mantiqiy deduksiya va chuqur sabab-oqibat tahlili",
    "Tarixiy burilish yasagan keskin voqea yoki strategik qaror tahlili",
    "Tabiiy qonuniyat yoki hodisaning fizik/biologik mexanizmi",
    "Ilmiy gipotezani isbotlovchi yoki raddiyat beruvchi fakt"
]

def create_empty_round():
    return {
        "active": False,
        "q_num": 1,
        "correct": None,
        "options": {},
        "explanation": "",
        "detailed_explanation": "",
        "source": "",
        "question_text": "",
        "attempted_users": set(),
        "in_initial_window": False,
        "answers": [],
        "message_id": None,
        "base_text": "",
        "answered_names": []
    }

ROUND_STATES = {}

def get_round_state(chat_id: int) -> dict:
    if chat_id not in ROUND_STATES:
        ROUND_STATES[chat_id] = create_empty_round()
    return ROUND_STATES[chat_id]

# --- Keep-Alive Web Server ---
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
        logger.error(f"Error verifying admin status in {chat_id}: {e}")
        return False

# --- Pinned Storage Helpers with Auto-Recovery ---
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
    recovered_scores = {}
    
    for line in pin_text.split("\n"):
        line = line.strip()
        if "So‘nggi savollar tarixi:" in line:
            is_history_section = True
            continue
        
        if not is_history_section:
            m = re.match(r"^(?:[🥇🥈🥉]|\d+\.)\s*(.+?)\s*—\s*(\d+)\s*ball", line)
            if m:
                recovered_scores[m.group(1).strip()] = int(m.group(2))
        else:
            if line.startswith("•") and "boshlanmoqda" not in line:
                history.append(line.replace("•", "").strip())

    chat_scores = get_chat_scores(chat_id)
    for p_name, p_pts in recovered_scores.items():
        if p_name not in chat_scores or chat_scores[p_name] < p_pts:
            chat_scores[p_name] = p_pts
    save_scores()
                
    return current_q, chat_scores, history

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
        _, latest_scores, history = parse_pin_data(getattr(pin, "text", "") or "", chat_id)
        
        merged_scores = dict(latest_scores)
        for k, v in scores_dict.items():
            if k not in merged_scores or v > merged_scores[k]:
                merged_scores[k] = v

        if new_question_summary:
            clean_summary = new_question_summary.replace("\n", " ").strip()[:90]
            history.insert(0, clean_summary)
            history = history[:15]
            
        scores_body = format_top10_scores(merged_scores)
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

def format_izoh_block(explanation: str, detailed: str, source: str = "") -> str:
    clean_expl = escape_tg_html(explanation)
    clean_detail = escape_tg_html(detailed)
    clean_src = escape_tg_html(source)
    
    src_line = f"\n📚 <b>Manba:</b> {clean_src}" if clean_src else ""

    if clean_detail and clean_detail.strip() != clean_expl.strip():
        return (
            f"💡 <b>Izoh:</b> {clean_expl}{src_line}\n\n"
            f"<blockquote expandable>📖 <b>Batafsil ma'lumot (o'qish uchun bosing):</b>\n\n"
            f"{clean_detail}</blockquote>"
        )
    return f"💡 <b>Izoh:</b> {clean_expl}{src_line}"

def format_leaderboard_block(leaderboard_str: str) -> str:
    clean_lb = escape_tg_html(leaderboard_str)
    return (
        f"<blockquote expandable>📊 <b>TOP-10 REYTINQ (ko'rish uchun bosing):</b>\n\n"
        f"{clean_lb}</blockquote>"
    )

def clean_json_response(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)

def get_grade_info(q_num: int):
    grade_num = 5 + ((q_num - 1) // 1000)
    if grade_num <= 11:
        label = f"{grade_num}-sinf (BMBA / DTM)"
        score = f"{grade_num - 4}/10"
    elif grade_num <= 15:
        label = "Oliy ta'lim (Bakalavr)"
        score = "8/10"
    else:
        label = "Akademik / Ekspert"
        score = "10/10"
    return label, score

# --- Groq Dynamic Roast Generator ---
def generate_dynamic_roast(question_text, options, correct_letter, wrong_guesses_grouped):
    if not wrong_guesses_grouped:
        return ""

    correct_text = options.get(correct_letter, "")
    wrong_block = []
    for letter, names in wrong_guesses_grouped.items():
        opt_text = options.get(letter, "")
        wrong_block.append(f"- Variant {letter}) '{opt_text}': {', '.join(names)}")
    wrong_str = "\n".join(wrong_block)

    prompt = f"""
    Sen Telegram viktorinasi uchun ENG KULGILI, O'TKIR VA AQLLI "ROASTER"SAN.
    Ishtirokchilar noto'g'ri javob berishdi. Sening vazifang ularni SHAXSAN nomma-nom aytib, tanlagan varianti uchun samimiy va ma'lumot beruvchi tarzda kulgili tanqid qilish.

    SAVOL: {question_text}
    TO'G'RI JAVOB: {correct_letter}) {correct_text}

    NOTO'G'RI TAXMIN QILGANLAR VA ULARNING VARIANTLARI:
    {wrong_str}

    QAT'IY TALABLAR:
    1. Aholini SHAXSAN va VARIANTMA-VARIANT nomma-nom aytib roast qil!
    2. Har bir guruh/odam uchun ularning variantini MANTIQAN VA ILMIY/TARIXIY tomondan nima uchun xato ekanligini 1-2 gapda achchiq va kulgili qilib tushuntir (roast qilayotganda ularga bilim ham ber!).
    3. Aslo bir xil takroriy gaplar ('bu javobdan ancha yiroq edi' va h.k.) ISHLATMA! Har safar unikal va ijodiy yondash.
    4. Hech qanday sarlavha (masalan: "Roast:", "Losers:") YOZMA! To'g'ridan-to'g'ri birinchi odam/variantdan boshla.
    5. Har bir fikr to'liq va tugallangan nuqta bilan yakunlansin.
    """

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    models_to_try = [
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b"
    ]

    for model in models_to_try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Sen o'zbek tilida juda kulgili, o'tkir va aqlli viktorina botisan."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.85,
            "max_tokens": 700
        }
        try:
            res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=20)
            data = res.json()
            if "choices" in data and len(data["choices"]) > 0:
                roast_text = data["choices"][0]["message"]["content"].strip()
                if roast_text:
                    return f"\n\n🔥 <b>LOSERS:</b>\n{roast_text}"
        except Exception as e:
            logger.warning(f"Roast model {model} failed: {e}")

    ROAST_TEMPLATES = [
        "🔥 <b>{names}</b> — {letter}) variantini tanladingiz. Bu variant mantiqan to'g'ri javobga mutlaqo zid edi!",
        "🔥 <b>{names}</b> — sizlar {letter}) variantiga ishondingiz-a? Tarix va ilmfan bu tanlovingizdan biroz xafa bo'ldi! 😂",
        "🔥 <b>{names}</b> — {letter}) variantini qayerdan topdingiz? Bu variant mavzuga umuman aloqasiz edi!",
        "🔥 <b>{names}</b> — {letter}) variantini belgilab tavakkal qildingiz, lekin bu safar omadingiz umuman kelmadi!"
    ]

    fallback_lines = ["\n\n🔥 <b>LOSERS:</b>"]
    for letter, names in wrong_guesses_grouped.items():
        template = random.choice(ROAST_TEMPLATES)
        escaped_names = ", ".join([f"<b>{escape_tg_html(n)}</b>" for n in names])
        fallback_lines.append(template.format(names=escaped_names, letter=letter))
        
    return "\n".join(fallback_lines)

# --- Offline Fallback Questions ---
OFFLINE_BACKUP_QUESTIONS = [
    {
        "subject": "O'zbekiston Tarixi",
        "question": "Amir Temur Yevropa qirollari bilan diplomatik yozishmalarda asosan qanday iqtisodiy va strategik shartni ilgari surgan?",
        "options": {
            "A": "Usmoniylar imperiyasiga qarshi birgalikda dengiz floti ajratish",
            "B": "Ipak yo'li bo'ylab savdogarlarning erkin harakatlanishi va xavfsizlik kafolati",
            "C": "Yevropa konlaridan kumush va oltin xomashyosini import qilish",
            "D": "Ispaniya qirolligi bilan harbiy alyans tuzish"
        },
        "correct": "B",
        "explanation": "Amir Temur Sharl VI va Genrix IV ga yo'llagan maktublarida xalqaro savdo xavfsizligini ta'minlashni asosiy prioritet qilib qo'ygan.",
        "detailed_explanation": "Amir Temur va Yevropa hukmdorlari o'rtasidagi yozishmalar asosan Ipak yo'lining g'arbiy va sharqiy tarmoqlarida savdo xavfsizligini o'rnatishga qaratilgan edi. U savdogarlarga bojxona imtiyozlari berilishini ta'minlagan.",
        "source": "Fransiya Qirollik Arxivi (Archives Nationales) — Temur va Sharl VI maktublari"
    },
    {
        "subject": "Fizika va Astronomiya",
        "question": "Nima uchun neytron yulduzlarning aylanish tezligi uning radiusi qisqargan sari keskin ortib boradi?",
        "options": {
            "A": "Magnit maydoni kuchayishi natijasida",
            "B": "Impuls momentining saqlanish qonuniga ko'ra",
            "C": "Termoyadroviy reaksiyalar to'xtashi sababli",
            "D": "Gravitatsion to'lqinlar ajralishi oqibatida"
        },
        "correct": "B",
        "explanation": "Impuls momentining saqlanish qonuniga ko'ra, jism radiusi kichrayganda uning burchak tezligi avtomatik ravishda ortadi.",
        "detailed_explanation": "Aylanayotgan gigant yulduz o'z o'qi atrofida siqilib, neytron yulduziga aylanganda uning inersiya momenti kamayadi va burchak tezligi soniyasiga yuzlab marta aylanish darajasigacha ko'tariladi.",
        "source": "Astrophysics Journal — Neutron Star Dynamics"
    }
]

# --- Question Generator (Grounded & Non-Trivia System Prompt) ---
def fetch_question(q_num, recent_history):
    grade_label, grade_score = get_grade_info(q_num)

    if q_num % 4 == 0:
        chosen_topic = random.choice(ISLAMIC_TOPIC_POOLS)
    else:
        chosen_topic = random.choice(TOPIC_POOLS)

    chosen_angle = random.choice(QUESTION_ANGLES)
    cache_buster = f"{int(time.time())}_{random.randint(1000, 9999)}"
    history_block = "\n".join([f"- {item}" for item in recent_history]) if recent_history else "Hozircha yo'q"

    prompt = f"""
    Sen Telegram viktorinasi uchun AKADEMIK, TIZIMLI VA MA'NOLIY SAVOL TUZUVCHISAN.
    Hozirgi savol raqami: #{q_num} / 1 000 000.
    Unikal kalit: {cache_buster}.
    
    DARAJA TALABI:
    - Bosqich: **{grade_label}** ({grade_score}).
    
    MAVZU: {chosen_topic}
    USLUB: {chosen_angle}
    
    QAT'IY QOIDALAR (AI SLOP VA TRIVIANI YO'QOTISH):
    1. YODLASH SAVOLLARI TAQIQLANADI: Yillar (yilini topish), tildan olinganlik (ma'nosini topish), oddiy nomlarni yodlash savollari UMUMAN BO'LMASIN!
    2. CHUQUR SABAB-OQIBAT VA MANTIQ: Savol faqat "Nima uchun bu hodisa yuz bergan?", "Ushbu qarorning strategik oqibati nima bo'lgan?", "Qaysi fizik/biologik qonuniyat buni tushuntiradi?" uslubida bo'lsin.
    3. MANTIQIY VARIANTLAR: A, B, C, D variantlarining barchasi mantiqli va jiddiy ko'rinsin (osonlikcha chiqarib tashlanadigan kulgili variantlar berilmasin).
    4. SO'NGGI SAVOLLAR TAKRORLANMASIN:
    {history_block}
    5. ANIQ MANBA (SOURCE): 'source' maydonida ushbu fakt aynan qaysi maktab darsligi, ilmiy ensiklopediya, xalqaro arxiv yoki sahih manbadan olinganini ko'rsat (masalan: "O'zbekiston Tarixi 9-sinf darsligi", "Sahihul Buxoriy #128", "NASA Exoplanet Archive").

    Faqat toza JSON formatida javob ber:
    {{
        "subject": "{chosen_topic.split(':')[0].strip()}",
        "grade": "{grade_label}",
        "difficulty_score": "{grade_score}",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa tushuntirish (1 gap)",
        "detailed_explanation": "Kengaytirilgan tahlil va faktlar (2-3 xatboshi)",
        "source": "Aniq darslik, kitob yoki rasmiy ilmiy manba nomi"
    }}
    """

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    models_to_try = [
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b"
    ]

    for model in models_to_try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Sen faqat to'g'ri va akademik JSON qaytaruvchi yordamchisan."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7
        }
        try:
            res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
            data = res.json()
            if "error" in data:
                logger.warning(f"Groq {model} error: {data['error'].get('message')}. Retrying next model...")
                continue

            raw_text = data['choices'][0]['message']['content']
            parsed = clean_json_response(raw_text)
            
            if "question" in parsed and "options" in parsed and "correct" in parsed:
                parsed["grade"] = grade_label
                parsed["difficulty_score"] = grade_score
                if "detailed_explanation" not in parsed or not parsed["detailed_explanation"]:
                    parsed["detailed_explanation"] = parsed.get("explanation", "")
                if "source" not in parsed or not parsed["source"]:
                    parsed["source"] = "Rasmiy ta'lim va akademik manbalar"
                logger.info(f"Groq generated successfully using {model}: {parsed['question'][:40]}...")
                return parsed

        except Exception as e:
            logger.warning(f"Model {model} failed ({e}). Trying next model...")

    logger.error("All Groq models failed. Deploying diverse offline backup question.")
    available_backups = [
        q for q in OFFLINE_BACKUP_QUESTIONS 
        if not any(q["question"][:30] in h for h in recent_history)
    ]
    selected_backup = random.choice(available_backups if available_backups else OFFLINE_BACKUP_QUESTIONS).copy()
    selected_backup["grade"] = grade_label
    selected_backup["difficulty_score"] = grade_score
    return selected_backup

# --- Intake Window Evaluator per Group ---
async def evaluate_initial_window(app, chat_id: int, target_q_num: int):
    await asyncio.sleep(INITIAL_WINDOW_SECONDS)
    round_st = get_round_state(chat_id)

    if not round_st["active"] or round_st["q_num"] != target_q_num:
        return

    round_st["in_initial_window"] = False
    answers = round_st["answers"]
    correct_choice = round_st["correct"]

    unique_answers = []
    seen_ids = set()
    for ans in answers:
        if ans["user"].id not in seen_ids:
            seen_ids.add(ans["user"].id)
            unique_answers.append(ans)

    correct_answers = [ans for ans in unique_answers if ans["choice"] == correct_choice]
    wrong_answers = [ans for ans in unique_answers if ans["choice"] != correct_choice]

    wrong_grouped = {}
    for ans in wrong_answers:
        letter = ans["choice"]
        name = ans["user"].first_name or "O'yinchi"
        if letter not in wrong_grouped:
            wrong_grouped[letter] = []
        wrong_grouped[letter].append(name)

    if correct_answers:
        round_st["active"] = False
        correct_answers.sort(key=lambda x: x["timestamp"])

        points_map = [10, 5, 3]
        medals = ["🥇", "🥈", "🥉"]
        winners_lines = []

        for idx, ans in enumerate(correct_answers[:3]):
            user = ans["user"]
            name = user.first_name or "O‘yinchi"
            pts = points_map[idx]
            update_player_score(chat_id, name, pts)
            winners_lines.append(f"{medals[idx]} {escape_tg_html(name)} — +{pts} ball ({idx + 1}-o‘rin)")

        chat_scores = get_chat_scores(chat_id)
        leaderboard_str = await update_pin(
            app.bot,
            chat_id=chat_id,
            next_q_num=target_q_num + 1,
            scores_dict=chat_scores,
            new_question_summary=round_st["question_text"]
        )

        ans_text = round_st["options"].get(correct_choice, "")
        winners_block = "\n".join(winners_lines)

        extra_correct = len(correct_answers) - 3
        extra_note = f"\n👏 Yana {extra_correct} kishi to‘g‘ri topdi, ammo shohsupaga ulgurmadi!" if extra_correct > 0 else ""

        izoh_formatted = format_izoh_block(
            round_st['explanation'], 
            round_st['detailed_explanation'],
            round_st.get('source', '')
        )
        rating_formatted = format_leaderboard_block(leaderboard_str)
        
        roast_section = generate_dynamic_roast(
            round_st["question_text"],
            round_st["options"],
            correct_choice,
            wrong_grouped
        )

        reply = (
            f"⏰ <b>30 SONIYA TUGADI!</b>\n\n"
            f"🎯 <b>TO‘G‘RI JAVOB:</b> {correct_choice}) {escape_tg_html(ans_text)}\n"
            f"{izoh_formatted}\n\n"
            f"⚡ <b>G‘OLIBLAR VA BALLAR:</b>\n"
            f"{winners_block}{extra_note}"
            f"{roast_section}\n\n"
            f"{rating_formatted}\n\n"
            f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
        )
        sent_reveal = await app.bot.send_message(chat_id, reply, parse_mode="HTML")
        track_for_cleanup(chat_id, sent_reveal.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS)
        track_for_cleanup(chat_id, round_st.get("message_id"), delay_seconds=EXPIRE_CLEANUP_SECONDS)

    else:
        roast_section = generate_dynamic_roast(
            round_st["question_text"],
            round_st["options"],
            correct_choice,
            wrong_grouped
        )
        sent_reveal = await app.bot.send_message(
            chat_id,
            f"⏳ <b>30 soniya o‘tdi, ammo biror kishi ham to‘g‘ri topolmadi!</b>"
            f"{roast_section}\n\n"
            f"🔥 O‘yin davom etadi — endi kim birinchi to‘g‘ri topsa, to‘liq 10 ball oladi!",
            parse_mode="HTML"
        )
        track_for_cleanup(chat_id, sent_reveal.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS)

# --- Round Engine (Multi-Group Post) ---
async def post_hourly_question(app):
    for chat_id in GROUP_CHAT_IDS:
        try:
            round_st = get_round_state(chat_id)
            chat_scores = get_chat_scores(chat_id)

            if round_st["active"]:
                round_st["active"] = False
                prev_q_num = round_st["q_num"]
                prev_correct = round_st["correct"]
                prev_ans = round_st["options"].get(prev_correct, "")
                prev_expl = round_st["explanation"]
                prev_detailed = round_st["detailed_explanation"]
                prev_source = round_st.get("source", "")
                prev_q_text = round_st["question_text"]
                prev_q_mid = round_st["message_id"]

                answers = round_st["answers"]
                unique_answers = []
                seen_ids = set()
                for ans in answers:
                    if ans["user"].id not in seen_ids:
                        seen_ids.add(ans["user"].id)
                        unique_answers.append(ans)

                wrong_answers = [ans for ans in unique_answers if ans["choice"] != prev_correct]
                wrong_grouped = {}
                for ans in wrong_answers:
                    letter = ans["choice"]
                    name = ans["user"].first_name or "O'yinchi"
                    if letter not in wrong_grouped:
                        wrong_grouped[letter] = []
                    wrong_grouped[letter].append(name)

                leaderboard_str = await update_pin(
                    app.bot,
                    chat_id=chat_id,
                    next_q_num=prev_q_num + 1,
                    scores_dict=chat_scores,
                    new_question_summary=prev_q_text
                )

                izoh_formatted = format_izoh_block(prev_expl, prev_detailed, prev_source)
                roast_section = generate_dynamic_roast(
                    prev_q_text,
                    round_st["options"],
                    prev_correct,
                    wrong_grouped
                )

                timeout_msg = (
                    f"⏰ <b>VAQT TUGADI!</b> (#{prev_q_num:07d})\n\n"
                    f"Hech kim to‘g‘ri javob bera olmadi.\n"
                    f"✅ <b>To‘g‘ri javob:</b> {prev_correct}) {escape_tg_html(prev_ans)}\n"
                    f"{izoh_formatted}"
                    f"{roast_section}\n\n"
                    f"⚡ Keyingi savol chiqmoqda..."
                )
                sent_timeout = await app.bot.send_message(chat_id, timeout_msg, parse_mode="HTML")
                track_for_cleanup(chat_id, sent_timeout.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS)
                track_for_cleanup(chat_id, prev_q_mid, delay_seconds=EXPIRE_CLEANUP_SECONDS)
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error expiring unanswered question in {chat_id}: {e}", exc_info=True)

    sample_chat = GROUP_CHAT_IDS[0] if GROUP_CHAT_IDS else 0
    pin = await get_or_create_leaderboard(app.bot, sample_chat)
    q_num, _, history = parse_pin_data(getattr(pin, "text", "") or "", sample_chat)
    
    data = fetch_question(q_num, history)

    for chat_id in GROUP_CHAT_IDS:
        try:
            round_st = get_round_state(chat_id)
            
            round_st["active"] = True
            round_st["q_num"] = q_num
            round_st["correct"] = data["correct"].upper()
            round_st["options"] = data["options"]
            round_st["explanation"] = data["explanation"]
            round_st["detailed_explanation"] = data.get("detailed_explanation", data["explanation"])
            round_st["source"] = data.get("source", "Rasmiy ta'lim va akademik manbalar")
            round_st["question_text"] = data["question"]
            round_st["attempted_users"].clear()
            round_st["answers"].clear()
            round_st["answered_names"].clear()
            round_st["in_initial_window"] = True
            
            grade_label = data.get("grade", "5-sinf")
            diff_score = data.get("difficulty_score", "1/10")
            
            msg_text = (
                f"🏆 1 MILLION QUESTIONS · #{q_num:07d}\n"
                f"📚 Fan: {data['subject']} | 🎓 Bosqich: {grade_label} ({diff_score})\n\n"
                f"❓ {data['question']}\n\n"
                f"A) {data['options']['A']}\n"
                f"B) {data['options']['B']}\n"
                f"C) {data['options']['C']}\n"
                f"D) {data['options']['D']}\n\n"
                f"⚡ Variantni yozing (A, B, C yoki D)!\n"
                f"⏳ Dastlabki 30 soniya ichida eng tez topgan 3 kishi ball oladi (🥇10, 🥈5, 🥉3)!\n"
                f"🤫 Javobingiz bot tomonidan o‘chirilib, quyidagi ro‘yxatga kiritiladi."
            )
            round_st["base_text"] = msg_text
            sent_msg = await app.bot.send_message(chat_id, msg_text)
            round_st["message_id"] = sent_msg.message_id
            track_for_cleanup(chat_id, sent_msg.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS + 300)
            logger.info(f"Question #{q_num} posted in {chat_id}.")
            asyncio.create_task(evaluate_initial_window(app, chat_id, q_num))
        except Exception as e:
            logger.error(f"Could not post question to {chat_id}: {e}")

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return

    text = update.message.text.strip()

    if is_vulgar(text):
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete vulgar message: {e}")
        return

    round_st = get_round_state(chat_id)
    if not round_st["active"]:
        return

    match = ANSWER_PATTERN.match(text)
    if not match:
        return

    user = update.message.from_user

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except Exception as e:
        logger.error(f"Failed to delete user answer message: {e}")

    user_name = user.first_name or "O‘yinchi"
    raw_choice = match.group(1)
    choice = CYRILLIC_TO_LATIN.get(raw_choice, raw_choice).upper()

    target_chats = GROUP_CHAT_IDS if user_name.lower() == GLOBAL_USER_NAME.lower() else [chat_id]

    for target_cid in target_chats:
        target_round = get_round_state(target_cid)
        if not target_round["active"]:
            continue

        if user.id in target_round["attempted_users"]:
            continue

        target_round["attempted_users"].add(user.id)
        target_round["answered_names"].append(user_name)

        target_round["answers"].append({
            "user": user,
            "choice": choice,
            "timestamp": time.time()
        })

        if target_round["message_id"]:
            answered_lines = "\n".join([f"• {escape_tg_html(n)}" for n in target_round["answered_names"]])
            updated_text = (
                f"{target_round['base_text']}\n\n"
                f"👥 <b>Javob berganlar ({len(target_round['answered_names'])}):</b>\n"
                f"{answered_lines}"
            )
            try:
                await context.bot.edit_message_text(
                    chat_id=target_cid,
                    message_id=target_round["message_id"],
                    text=updated_text,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Could not update question message in {target_cid}: {e}")

        if not target_round["in_initial_window"]:
            try:
                if choice == target_round["correct"]:
                    target_round["active"] = False
                    winner_name = user_name
                    
                    update_player_score(target_cid, winner_name, 10)
                    chat_scores = get_chat_scores(target_cid)
                    
                    leaderboard_str = await update_pin(
                        context.bot,
                        chat_id=target_cid,
                        next_q_num=target_round["q_num"] + 1,
                        scores_dict=chat_scores,
                        new_question_summary=target_round["question_text"]
                    )
                    
                    correct_letter = target_round["correct"]
                    ans_text = target_round["options"].get(correct_letter, "")
                    
                    izoh_formatted = format_izoh_block(
                        target_round['explanation'], 
                        target_round['detailed_explanation'],
                        target_round.get('source', '')
                    )
                    rating_formatted = format_leaderboard_block(leaderboard_str)

                    reply = (
                        f"🎯 <b>TO‘G‘RI JAVOB!</b>\n\n"
                        f"⚡ <b>G‘olib:</b> {escape_tg_html(winner_name)} (+10 ball!)\n"
                        f"✅ <b>Javob:</b> {correct_letter}) {escape_tg_html(ans_text)}\n"
                        f"{izoh_formatted}\n\n"
                        f"{rating_formatted}\n\n"
                        f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
                    )
                    
                    kwargs = {"parse_mode": "HTML"}
                    if update.message.message_thread_id:
                        kwargs["message_thread_id"] = update.message.message_thread_id

                    sent_winner_msg = await context.bot.send_message(target_cid, reply, **kwargs)
                    track_for_cleanup(target_cid, sent_winner_msg.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS)
                    track_for_cleanup(target_cid, target_round.get("message_id"), delay_seconds=EXPIRE_CLEANUP_SECONDS)
            except Exception as e:
                logger.error(f"Error handling sudden death answer in {target_cid}: {e}", exc_info=True)

# --- Audio/Voice Message Transcriber ---
async def cmd_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return

    reply_msg = update.message.reply_to_message
    if not reply_msg or (not reply_msg.voice and not reply_msg.audio):
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Foydalanish:</b> Ovozli xabarga reply qilib <code>/transcribe</code> deb yozing.",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    status_msg = await update.message.reply_text("🎧 <i>Ovozli xabar eshitilmoqda va matnga o'girilmoqda...</i>", parse_mode="HTML")

    try:
        audio_obj = reply_msg.voice or reply_msg.audio
        file_info = await context.bot.get_file(audio_obj.file_id)
        file_bytes = await file_info.download_as_bytearray()

        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        files = {
            "file": ("voice.ogg", bytes(file_bytes), "audio/ogg"),
            "model": (None, "whisper-large-v3"),
            "temperature": (None, "0.0")
        }

        res = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, timeout=30)
        data = res.json()
        transcript = data.get("text", "").strip()

        if transcript:
            reply_text = f"🗣 <b>OVOZLI XABAR MATNI:</b>\n\n<i>\"{escape_tg_html(transcript)}\"</i>"
            await status_msg.edit_text(reply_text, parse_mode="HTML")
        else:
            await status_msg.edit_text("❌ Ovozli xabarni matnga o'girishning imkoni bo'lmadi.")

    except Exception as e:
        logger.error(f"Error transcribing voice: {e}")
        await status_msg.edit_text("⚠️ Ovozni tahlil qilishda xatolik yuz berdi.")

# --- Commands ---
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    await update.message.reply_text("🧪 Test savoli barcha guruhlarda ishga tushirilmoqda...")
    await post_hourly_question(context.application)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    scores = get_chat_scores(chat_id)
    body = format_top10_scores(scores)
    reply = (
        f"🏆 1 MILLION QUESTIONS · TOP-10 YETAKCHILAR\n\n"
        f"{body}"
    )
    await update.message.reply_text(reply)

async def cmd_myrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return

    user = update.effective_user
    if not user:
        return

    name = user.first_name or "O‘yinchi"
    scores = get_chat_scores(chat_id)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    player_rank = None
    player_pts = 0

    for idx, (p_name, pts) in enumerate(sorted_scores, start=1):
        if p_name.lower() == name.lower():
            player_rank = idx
            player_pts = pts
            break

    try:
        await update.message.delete()
    except Exception:
        pass

    if player_rank:
        text = f"👤 <b>{escape_tg_html(name)}</b>ning ushbu guruhdagi natijasi:\n🏆 O'rin: #{player_rank}-o'rin | ⭐️ Ball: {player_pts} ball"
    else:
        text = f"👤 <b>{escape_tg_html(name)}</b>, sizda ushbu guruhda hali ballar yo'q!"

    msg = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    await asyncio.sleep(8)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_set_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    if not update.message.reply_to_message:
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Foydalanish:</b> Ishtirokchi xabariga reply qilib: <code>/score 118</code>",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    if not context.args:
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Foydalanish:</b> <code>/score 118</code>",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    try:
        new_score = int(context.args[0])
    except ValueError:
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Xatolik:</b> Ball faqat raqamlarda bo'lishi kerak!",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    target_user = update.message.reply_to_message.from_user
    target_name = target_user.first_name or "O‘yinchi"

    chat_scores = get_chat_scores(chat_id)
    chat_scores[target_name] = new_score
    save_scores()

    pin = await get_or_create_leaderboard(context.bot, chat_id)
    q_num, _, _ = parse_pin_data(getattr(pin, "text", "") or "", chat_id)
    await update_pin(
        context.bot,
        chat_id=chat_id,
        next_q_num=q_num,
        scores_dict=chat_scores,
        new_question_summary=None
    )

    msg = await context.bot.send_message(
        chat_id,
        f"✅ <b>{escape_tg_html(target_name)}</b> ning ushbu guruhdagi bali <b>{new_score}</b> deb belgilandi!",
        parse_mode="HTML"
    )
    await asyncio.sleep(6)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    round_st = get_round_state(chat_id)
    if not round_st["active"]:
        await update.message.reply_text("ℹ️ Hozirda faol savol yo'q.")
        return

    round_st["active"] = False
    correct_letter = round_st["correct"]
    ans_text = round_st["options"].get(correct_letter, "")
    explanation = round_st["explanation"]
    detailed = round_st["detailed_explanation"]
    source = round_st.get("source", "")
    q_text = round_st.get("question_text", "")
    
    chat_scores = get_chat_scores(chat_id)
    await update_pin(
        context.bot,
        chat_id=chat_id,
        next_q_num=round_st["q_num"] + 1,
        scores_dict=chat_scores,
        new_question_summary=q_text
    )
    
    izoh_formatted = format_izoh_block(explanation, detailed, source)
    reply = (
        f"⏭ <b>SAVOL O'TKAZIB YUBORILDI</b> (Admin tomonidan)\n\n"
        f"✅ <b>To‘g‘ri javob:</b> {correct_letter}) {escape_tg_html(ans_text)}\n"
        f"{izoh_formatted}\n\n"
        f"⏳ Keyingi savol 5 daqiqadan so‘ng yoki /test orqali chiqadi."
    )
    sent_skip = await update.message.reply_text(reply, parse_mode="HTML")
    track_for_cleanup(chat_id, sent_skip.message_id, delay_seconds=EXPIRE_CLEANUP_SECONDS)
    track_for_cleanup(chat_id, round_st.get("message_id"), delay_seconds=EXPIRE_CLEANUP_SECONDS)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    cid = str(chat_id)
    ALL_SCORES[cid] = {}
    save_scores()
    
    pin = await get_or_create_leaderboard(context.bot, chat_id)
    q_num, _, _ = parse_pin_data(getattr(pin, "text", "") or "", chat_id)
    
    await update_pin(
        context.bot,
        chat_id=chat_id,
        next_q_num=q_num,
        scores_dict={},
        new_question_summary=None
    )
    
    await update.message.reply_text("🔄 Ushbu guruh ishtirokchilarining ballari muvaffaqiyatli nollashtirildi!")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    now = datetime.now(TASHKENT_TZ)
    minutes_past = now.minute % 5
    seconds_past = minutes_past * 60 + now.second
    seconds_to_wait = 300 - seconds_past
    if seconds_to_wait <= 0:
        seconds_to_wait = 300

    mins = seconds_to_wait // 60
    secs = seconds_to_wait % 60
    
    is_alive = scheduler_task is not None and not scheduler_task.done()
    task_status = "🟢 Ishlayapti (Faol)" if is_alive else "🔴 To'xtab qolgan (Xato)"

    round_st = get_round_state(chat_id)
    msg = (
        f"🤖 BOT TIZIM HOLATI:\n\n"
        f"👥 Faol guruhlar soni: {len(GROUP_CHAT_IDS)} ta\n"
        f"🕒 Toshkent vaqti: {now.strftime('%H:%M:%S')}\n"
        f"⚙️ Jadval vazifasi (Scheduler): {task_status}\n"
        f"⏳ Keyingi savolgacha: {mins} daqiqa {secs} soniya\n"
        f"🎯 Ushbu guruhda faol savol bormi: {'Ha' if round_st['active'] else 'Yo‘q'}\n"
    )
    await update.message.reply_text(msg)

async def cmd_cuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    if not context.args:
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Foydalanish:</b> <code>/cuss &lt;so'z&gt;</code>",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    new_word = context.args[0].strip().lower()
    CUSTOM_BANNED_WORDS.add(new_word)
    save_custom_banned()

    global VULGAR_REGEX
    VULGAR_REGEX = compile_vulgar_regex()

    msg = await context.bot.send_message(
        chat_id,
        f"🚫 <b>'{escape_tg_html(new_word)}'</b> so'zi qora ro'yxatga qo'shildi.",
        parse_mode="HTML"
    )
    await asyncio.sleep(6)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_uncuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    if not context.args:
        warn = await context.bot.send_message(
            chat_id,
            "⚠️ <b>Foydalanish:</b> <code>/uncuss &lt;so'z&gt;</code>",
            parse_mode="HTML"
        )
        await asyncio.sleep(5)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    target_word = context.args[0].strip().lower()
    if target_word in CUSTOM_BANNED_WORDS:
        CUSTOM_BANNED_WORDS.remove(target_word)
        save_custom_banned()
        global VULGAR_REGEX
        VULGAR_REGEX = compile_vulgar_regex()
        text = f"✅ <b>'{escape_tg_html(target_word)}'</b> taqiqdan olib tashlandi."
    else:
        text = f"ℹ️ <b>'{escape_tg_html(target_word)}'</b> ro'yxatda topilmadi."

    msg = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    await asyncio.sleep(6)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_cusslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    if not await is_admin(update, context):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    if not CUSTOM_BANNED_WORDS:
        msg_text = "📋 Hozircha qo'shimcha kiritilgan so'zlar yo'q."
    else:
        words = ", ".join([f"<code>{escape_tg_html(w)}</code>" for w in sorted(CUSTOM_BANNED_WORDS)])
        msg_text = f"📋 <b>Qo'shilgan taqiqlangan so'zlar ({len(CUSTOM_BANNED_WORDS)} ta):</b>\n\n{words}"

    msg = await context.bot.send_message(chat_id, text=msg_text, parse_mode="HTML")
    await asyncio.sleep(10)
    try:
        await msg.delete()
    except Exception:
        pass

# --- Scheduler Task (5-Minute Drops) ---
scheduler_task = None

async def scheduler(app):
    while True:
        try:
            now = datetime.now(TASHKENT_TZ)
            minutes_past = now.minute % 5
            seconds_past = minutes_past * 60 + now.second
            seconds_to_wait = 300 - seconds_past
            if seconds_to_wait <= 0:
                seconds_to_wait = 300
                
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
    asyncio.create_task(message_cleaner_task(app.bot))

# --- Main Startup ---
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("myrank", cmd_myrank))
    app.add_handler(CommandHandler("score", cmd_set_score))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cuss", cmd_cuss))
    app.add_handler(CommandHandler("uncuss", cmd_uncuss))
    app.add_handler(CommandHandler("cusslist", cmd_cusslist))
    app.add_handler(CommandHandler("transcribe", cmd_transcribe))
    app.add_handler(CommandHandler("stt", cmd_transcribe))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    app.run_polling()
