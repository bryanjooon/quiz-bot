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
GROUP_CHAT_ID = int(os.getenv("TELEGRAM_GROUP_ID", "0"))
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'A',
    'В': 'B', 'в': 'B',
    'С': 'C', 'с': 'C',
    'D': 'D', 'd': 'D', 'Д': 'D', 'д': 'D'
}

INITIAL_WINDOW_SECONDS = 30

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
    r"nah\b",
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
    # yban / yeban / eban variants
    r"y+e*b+a+[nŋ]\w*",
    r"e+b+a+[nŋ]\w*",
    r"y+e*b+l+a+[nŋ]\w*",
    r"e+b+l+a+[nŋ]\w*",
    r"[её]+б+а+[нң]\w*",
    r"[её]+б+л+а+[нң]\w*",
    r"й+е+б+а+[нң]\w*",
    r"и+б+а+[нң]\w*",
    r"ы+б+а+[нң]\w*",
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

# --- Dynamic Custom Word Blacklist Storage ---
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
    """Checks if a message contains profanities, spaced bypasses, or leetspeak."""
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

# Strict single-letter answer match
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

round_state = {
    "active": False,
    "q_num": 1,
    "correct": None,
    "options": {},
    "explanation": "",
    "detailed_explanation": "",
    "question_text": "",
    "attempted_users": set(),
    "in_initial_window": False,
    "first_10s_answers": [],
    "message_id": None,
    "base_text": "",
    "answered_names": []
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
        await bot.edit_message_text(chat_id=GROUP_CHAT_ID, message_id=pin.message_id, text=new_text)
        return scores_body
    except Exception as e:
        logger.error(f"Failed to update pin: {e}")
        return ""

# --- HTML Safe Helper & Expandable Blockquote Formatter ---
def escape_tg_html(text: str) -> str:
    """Escapes only HTML reserved characters without altering apostrophes."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def format_izoh_block(explanation: str, detailed: str) -> str:
    """Renders short explanation followed by an expandable collapsible quote block."""
    clean_expl = escape_tg_html(explanation)
    clean_detail = escape_tg_html(detailed)
    if clean_detail and clean_detail.strip() != clean_expl.strip():
        return (
            f"💡 <b>Izoh:</b> {clean_expl}\n\n"
            f"<blockquote expandable>📖 <b>Batafsil ma'lumot (o'qish uchun bosing):</b>\n\n"
            f"{clean_detail}</blockquote>"
        )
    return f"💡 <b>Izoh:</b> {clean_expl}"

def format_leaderboard_block(leaderboard_str: str) -> str:
    """Wraps the leaderboard in an expandable collapsible quote block."""
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
        label = f"{grade_num}-sinf"
        score = f"{grade_num - 4}/10"
    elif grade_num <= 15:
        label = "Oliy ta'lim (Bakalavr)"
        score = "8/10"
    else:
        label = "Akademik / Ekspert"
        score = "10/10"
    return label, score

# --- Offline Fallback Questions ---
OFFLINE_BACKUP_QUESTIONS = [
    {
        "subject": "Astronomiya",
        "question": "Quyosh sistemasidagi eng katta sayyora qaysi?",
        "options": {"A": "Yupiter", "B": "Saturn", "C": "Mars", "D": "Venera"},
        "correct": "A",
        "explanation": "Yupiter Quyosh tizimidagi eng ulkan gigant gaz sayyorasidir.",
        "detailed_explanation": "Yupiter sayyorasi shu qadar ulkanki, uning ichiga Quyosh tizimidagi barcha boshqa sayyoralarni bemalol joylashtirish mumkin. U asosan vodorod va geliydan iborat bo'lib, o'zining kuchli magnit maydoniga ega. Mashhur 'Katta Qizil Dog'' esa unda kamida 300 yildan beri to'xtamayotgan ulkan bo'rondir."
    },
    {
        "subject": "Biologiya",
        "question": "O'simliklar quyosh nuri yordamida oziq modda ishlab chiqarish jarayoni nima deyiladi?",
        "options": {"A": "Fotosintez", "B": "Nafas olish", "C": "Transpiratsiya", "D": "Diffuziya"},
        "correct": "A",
        "explanation": "Fotosintez jarayonida xlorofill yorug'lik energiyasini kimyoviy energiyaga aylantiradi.",
        "detailed_explanation": "Fotosintez — yer yuzidagi barcha tiriklikning asosi. Yashil o'simliklar barglaridagi xloroplastlar yorug'lik, suv va karbonat angidriddan foydalanib oziq moddalar hamda biz nafas oladigan toza kislorodni ishlab chiqaradi."
    },
    {
        "subject": "Geografiya",
        "question": "Dunyodagi eng chuqur chuchuk suvli ko'l qaysi?",
        "options": {"A": "Baykal", "B": "Kaspiy", "C": "Viktoriya", "D": "Michigan"},
        "correct": "A",
        "explanation": "Baykal ko'li dunyodagi eng qadimgi va eng chuqur (1642 metr) chuchuk suv havzasidir.",
        "detailed_explanation": "Baykal ko'li Sibir janubida joylashgan bo'lib, uning yoshi 25 million yildan oshiq. U dunyodagi barcha erigan toza chuchuk suv zaxirasining salkam 20 foizini o'zida saqlaydi."
    },
    {
        "subject": "O'zbekiston tarixi",
        "question": "Buyuk ipak yo'lining markazida joylashgan qadimiy 'Afrosiyob' shaharchasi qaysi shahar hududida?",
        "options": {"A": "Samarqand", "B": "Buxoro", "C": "Xiva", "D": "Termiz"},
        "correct": "A",
        "explanation": "Afrosiyob qadimiy Samarqandning 13-asrgacha bo'lgan dastlabki o'rni hisoblanadi.",
        "detailed_explanation": "Afrosiyob tepaliklari qadimiy So'g'd poytaxti Samarqandning asl o'rnidir. 1220-yilda Chingizxon qo'shinlari vayron qilgunga qadar shahar aholisi shu yerda yashagan. U yerdan topilgan 'Elchilar zali' devoriy suratlari 7-asr madaniyatining noyob durdonasidir."
    },
    {
        "subject": "Fizika",
        "question": "Oddiy atmosfera bosimida toza suv necha daraja Selsiyda qaynaydi?",
        "options": {"A": "100°C", "B": "90°C", "C": "80°C", "D": "120°C"},
        "correct": "A",
        "explanation": "Dengiz sathidagi normal atmosfera bosimida toza suv 100°C da qaynaydi.",
        "detailed_explanation": "Qaynash nuqtasi atmosfera bosimiga bevosita bog'liq. Dengiz sathida 1 atmosfera bosimida suv 100°C da qaynasa, baland tog'larda (masalan, Everestda) past atmosfera bosimi tufayli suv 70°C dayoq qaynay boshlaydi."
    },
    {
        "subject": "Islom tarixi",
        "question": "Payg'ambarimiz Muhammad (s.a.v.) qaysi shaharda tavallud topganlar?",
        "options": {"A": "Makka", "B": "Madina", "C": "Toif", "D": "Quddus"},
        "correct": "A",
        "explanation": "Payg'ambarimiz (s.a.v.) milodiy 571-yilda Makka shahrida tavallud topganlar.",
        "detailed_explanation": "Payg'ambarimiz Muhammad (s.a.v.) milodiy 571-yil Makka shahrida dunyoga kelganlar. 40 yoshlarida Makka yaqinidagi Hiro g'orida ilk vahiy qabul qilib, Islom da'vatini aynan Makka zaminida boshlaganlar."
    }
]

# --- Question Generator ---
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
    Sen Telegram viktorinasi uchun ORIGINAL va TAKRORLANMAS savol tuzuvchisan.
    Hozirgi savol raqami: #{q_num} / 1 000 000.
    Unikal kalit: {cache_buster}.
    
    QAT'IY DARAJA TALABI:
    - Hozirgi daraja: **{grade_label}** darajasi ({grade_score}).
    - Savol murakkabligi va tili aynan **{grade_label}** o'quvchisi qiynalmasdan tushunadigan soddalikda bo'lsin.
    - O'ta qiyin akademik terminlar yoki chalkash formulalarni BERMA!
    - Agar mavzu maktab dasturida bo'lmasa ham, uni {grade_label} darajasiga moslab sodda va qiziqarli tuz.

    MAVZU: {chosen_topic}
    USLUB: {chosen_angle}
    
    QAT'IY TAQIQLAR:
    1. LINGVISTIK VA SO'Z KELIB CHIQISHI TAQIQLANADI ('qaysi tildan olingan', 'ma'nosi nima' kabi savollar bo'lmasin).
    2. SO'NGGI SAVOLLAR TAKRORLANMASIN:
    {history_block}
    3. Agar mavzu Islom dini yoki tarixiga oid bo'lsa, savol faqat sahih manbalar asosida, hurmat bilan tuzilsin.
    4. 4 ta variant (A, B, C, D) ber. Faqat 1 tasi to'g'ri bo'lsin.
    
    BATAFSIL IZOH TALABI:
    - 'detailed_explanation' maydonida mavzuning qiziqarli ilmiy/tarixiy sabab-oqibatlari, fonga oid tafsilotlari va qiziqarli faktlarini 2-3 xatboshida chiroyli tushuntirib ber. Bu matn Telegramda ochilib-yopiluvchi blokda ko'rinadi.
    
    Faqat toza JSON formatida javob ber:
    {{
        "subject": "{chosen_topic.split(':')[0].strip()}",
        "grade": "{grade_label}",
        "difficulty_score": "{grade_score}",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa tushuntirish (1 gap)",
        "detailed_explanation": "Kengaytirilgan tahlil, qiziqarli faktlar va sabab-oqibatlar (2-3 xatboshi)"
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
                {"role": "system", "content": "Sen faqat to'g'ri JSON qaytaruvchi yordamchisan."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.75
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

# --- Intake Window Evaluator ---
async def evaluate_initial_window(app, target_q_num: int):
    """Waits full calibrated duration, then awards points (10, 5, 3) to the fastest correct guesses."""
    await asyncio.sleep(INITIAL_WINDOW_SECONDS)
    global round_state

    if not round_state["active"] or round_state["q_num"] != target_q_num:
        return

    round_state["in_initial_window"] = False
    answers = round_state["first_10s_answers"]
    correct_choice = round_state["correct"]

    correct_answers = [ans for ans in answers if ans["choice"] == correct_choice]

    if correct_answers:
        round_state["active"] = False
        correct_answers.sort(key=lambda x: x["timestamp"])

        pin = await get_or_create_leaderboard(app.bot)
        q_num, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")

        points_map = [10, 5, 3]
        medals = ["🥇", "🥈", "🥉"]
        winners_lines = []

        unique_correct = []
        seen_ids = set()
        for ans in correct_answers:
            if ans["user"].id not in seen_ids:
                seen_ids.add(ans["user"].id)
                unique_correct.append(ans)

        for idx, ans in enumerate(unique_correct[:3]):
            user = ans["user"]
            name = user.first_name or "O‘yinchi"
            pts = points_map[idx]
            scores[name] = scores.get(name, 0) + pts
            winners_lines.append(f"{medals[idx]} {escape_tg_html(name)} — +{pts} ball ({idx + 1}-o‘rin)")

        leaderboard_str = await update_pin(
            app.bot,
            next_q_num=q_num + 1,
            scores_dict=scores,
            new_question_summary=round_state["question_text"]
        )

        ans_text = round_state["options"].get(correct_choice, "")
        winners_block = "\n".join(winners_lines)

        extra_correct = len(unique_correct) - 3
        extra_note = f"\n👏 Yana {extra_correct} kishi to‘g‘ri topdi, ammo shohsupaga ulgurmadi!" if extra_correct > 0 else ""

        izoh_formatted = format_izoh_block(round_state['explanation'], round_state['detailed_explanation'])
        rating_formatted = format_leaderboard_block(leaderboard_str)

        reply = (
            f"⏰ <b>30 SONIYA TUGADI!</b>\n\n"
            f"🎯 <b>TO‘G‘RI JAVOB:</b> {correct_choice}) {escape_tg_html(ans_text)}\n"
            f"{izoh_formatted}\n\n"
            f"⚡ <b>G‘OLIBLAR VA BALLAR:</b>\n"
            f"{winners_block}{extra_note}\n\n"
            f"{rating_formatted}\n\n"
            f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
        )
        await app.bot.send_message(
            GROUP_CHAT_ID, 
            reply, 
            parse_mode="HTML"
        )
    else:
        if answers:
            await app.bot.send_message(
                GROUP_CHAT_ID,
                "⏳ 30 soniya o‘tdi, ammo to‘g‘ri javob berilmadi!\n"
                "🔥 O‘yin davom etadi — endi kim birinchi to‘g‘ri topsa, to‘liq 10 ball oladi!"
            )

# --- Round Engine ---
async def post_hourly_question(app):
    global round_state

    # 1. AUTO-EXPIRE: If previous question was never answered, reveal it and advance q_num
    if round_state["active"]:
        round_state["active"] = False
        prev_q_num = round_state["q_num"]
        prev_correct = round_state["correct"]
        prev_ans = round_state["options"].get(prev_correct, "")
        prev_expl = round_state["explanation"]
        prev_detailed = round_state["detailed_explanation"]
        prev_q_text = round_state["question_text"]

        try:
            pin = await get_or_create_leaderboard(app.bot)
            curr_q, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")

            await update_pin(
                app.bot,
                next_q_num=curr_q + 1,
                scores_dict=scores,
                new_question_summary=prev_q_text
            )

            izoh_formatted = format_izoh_block(prev_expl, prev_detailed)
            timeout_msg = (
                f"⏰ <b>VAQT TUGADI!</b> (#{prev_q_num:07d})\n\n"
                f"Hech kim to‘g‘ri javob bera olmadi.\n"
                f"✅ <b>To‘g‘ri javob:</b> {prev_correct}) {escape_tg_html(prev_ans)}\n"
                f"{izoh_formatted}\n\n"
                f"⚡ Keyingi savol chiqmoqda..."
            )
            await app.bot.send_message(
                GROUP_CHAT_ID, 
                timeout_msg, 
                parse_mode="HTML"
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error expiring unanswered question: {e}", exc_info=True)
    
    # 2. Fetch data for the new round
    pin = await get_or_create_leaderboard(app.bot)
    q_num, _, history = parse_pin_data(getattr(pin, "text", "") or "")
    
    data = fetch_question(q_num, history)
    
    round_state["active"] = True
    round_state["q_num"] = q_num
    round_state["correct"] = data["correct"].upper()
    round_state["options"] = data["options"]
    round_state["explanation"] = data["explanation"]
    round_state["detailed_explanation"] = data.get("detailed_explanation", data["explanation"])
    round_state["question_text"] = data["question"]
    round_state["attempted_users"].clear()
    round_state["first_10s_answers"].clear()
    round_state["answered_names"].clear()
    round_state["in_initial_window"] = True
    
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
    round_state["base_text"] = msg_text
    sent_msg = await app.bot.send_message(GROUP_CHAT_ID, msg_text)
    round_state["message_id"] = sent_msg.message_id
    logger.info(f"Question #{q_num} ({grade_label}) posted. Correct: {round_state['correct']}")

    asyncio.create_task(evaluate_initial_window(app, q_num))

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global round_state
    
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # 1. SILENT PROFANITY CENSOR (Zero Tokens, Instant Deletion)
    if is_vulgar(text):
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete vulgar message: {e}")
        return

    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    if not round_state["active"]:
        return

    match = ANSWER_PATTERN.match(text)
    if not match:
        return

    user = update.message.from_user

    # Delete answer message immediately to prevent chat spam and copying
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete answer message: {e}")

    # Silently ignore repeat attempts
    if user.id in round_state["attempted_users"]:
        return

    # Register player's single attempt
    round_state["attempted_users"].add(user.id)
    user_name = user.first_name or "O‘yinchi"
    round_state["answered_names"].append(user_name)

    # Live-edit the question message to show the list of respondents
    if round_state["message_id"]:
        answered_lines = "\n".join([f"• {name}" for name in round_state["answered_names"]])
        updated_text = (
            f"{round_state['base_text']}\n\n"
            f"👥 Javob berganlar ({len(round_state['answered_names'])}):\n"
            f"{answered_lines}"
        )
        try:
            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=round_state["message_id"],
                text=updated_text
            )
        except Exception as e:
            logger.warning(f"Could not update question message: {e}")

    raw_choice = match.group(1)
    choice = CYRILLIC_TO_LATIN.get(raw_choice, raw_choice).upper()

    # PHASE 1: Collect guesses silently during initial intake window
    if round_state["in_initial_window"]:
        round_state["first_10s_answers"].append({
            "user": user,
            "choice": choice,
            "timestamp": time.time()
        })
        return

    # PHASE 2: After the intake window has closed (Sudden-Death)
    try:
        if choice == round_state["correct"]:
            round_state["active"] = False
            winner_name = user_name
            
            pin = await get_or_create_leaderboard(context.bot)
            curr_q, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
            scores[winner_name] = scores.get(winner_name, 0) + 10
            
            leaderboard_str = await update_pin(
                context.bot,
                next_q_num=curr_q + 1,
                scores_dict=scores,
                new_question_summary=round_state["question_text"]
            )
            
            correct_letter = round_state["correct"]
            ans_text = round_state["options"].get(correct_letter, "")
            
            izoh_formatted = format_izoh_block(round_state['explanation'], round_state['detailed_explanation'])
            rating_formatted = format_leaderboard_block(leaderboard_str)

            reply = (
                f"🎯 <b>TO‘G‘RI JAVOB!</b>\n\n"
                f"⚡ <b>G‘olib:</b> {escape_tg_html(winner_name)} (+10 ball!)\n"
                f"✅ <b>Javob:</b> {correct_letter}) {escape_tg_html(ans_text)}\n"
                f"{izoh_formatted}\n\n"
                f"{rating_formatted}\n\n"
                f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
            )
            await context.bot.send_message(
                GROUP_CHAT_ID, 
                reply, 
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error handling sudden death answer: {e}", exc_info=True)

# --- Commands ---
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
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
    detailed = round_state["detailed_explanation"]
    q_text = round_state["question_text"]
    
    pin = await get_or_create_leaderboard(context.bot)
    curr_q, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
    
    await update_pin(
        context.bot,
        next_q_num=curr_q + 1,
        scores_dict=scores,
        new_question_summary=q_text
    )
    
    izoh_formatted = format_izoh_block(explanation, detailed)
    reply = (
        f"⏭ <b>SAVOL O'TKAZIB YUBORILDI</b> (Admin tomonidan)\n\n"
        f"✅ <b>To‘g‘ri javob:</b> {correct_letter}) {escape_tg_html(ans_text)}\n"
        f"{izoh_formatted}\n\n"
        f"⏳ Keyingi savol 5 daqiqadan so‘ng yoki /test orqali chiqadi."
    )
    await update.message.reply_text(reply, parse_mode="HTML")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    
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
    minutes_past = now.minute % 5
    seconds_past = minutes_past * 60 + now.second
    seconds_to_wait = 300 - seconds_past
    if seconds_to_wait <= 0:
        seconds_to_wait = 300

    mins = seconds_to_wait // 60
    secs = seconds_to_wait % 60
    
    is_alive = scheduler_task is not None and not scheduler_task.done()
    task_status = "🟢 Ishlayapti (Faol)" if is_alive else "🔴 To'xtab qolgan (Xato)"

    msg = (
        f"🤖 BOT TIZIM HOLATI:\n\n"
        f"🕒 Toshkent vaqti: {now.strftime('%H:%M:%S')}\n"
        f"⚙️ Jadval vazifasi (Scheduler): {task_status}\n"
        f"⏳ Keyingi savolgacha: {mins} daqiqa {secs} soniya\n"
        f"🎯 Faol savol bormi: {'Ha' if round_state['active'] else 'Yo‘q'}\n"
    )
    await update.message.reply_text(msg)

async def cmd_cuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a custom word to the permanent ban list and deletes the trigger message."""
    if update.effective_chat.id != GROUP_CHAT_ID:
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
            GROUP_CHAT_ID,
            "⚠️ <b>Foydalanish:</b> <code>/cuss &lt;so'z&gt;</code>\nMasalan: <code>/cuss yiban</code>",
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
        GROUP_CHAT_ID,
        f"🚫 <b>'{escape_tg_html(new_word)}'</b> so'zi qora ro'yxatga qo'shildi va endi avtomatik o'chiriladi.",
        parse_mode="HTML"
    )
    await asyncio.sleep(6)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_uncuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a word from the custom ban list."""
    if update.effective_chat.id != GROUP_CHAT_ID:
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
            GROUP_CHAT_ID,
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
        text = f"ℹ️ <b>'{escape_tg_html(target_word)}'</b> maxsus taqiqlanganlar ro'yxatida topilmadi."

    msg = await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
    await asyncio.sleep(6)
    try:
        await msg.delete()
    except Exception:
        pass

async def cmd_cusslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all custom-added banned words."""
    if update.effective_chat.id != GROUP_CHAT_ID:
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
        msg_text = "📋 Hozircha qo'shimcha kiritilgan so'zlar yo'q (Faqat asosiy filtr faol)."
    else:
        words = ", ".join([f"<code>{escape_tg_html(w)}</code>" for w in sorted(CUSTOM_BANNED_WORDS)])
        msg_text = f"📋 <b>Qo'shilgan taqiqlangan so'zlar ({len(CUSTOM_BANNED_WORDS)} ta):</b>\n\n{words}"

    msg = await context.bot.send_message(GROUP_CHAT_ID, msg_text, parse_mode="HTML")
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

# --- Main Startup ---
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cuss", cmd_cuss))
    app.add_handler(CommandHandler("uncuss", cmd_uncuss))
    app.add_handler(CommandHandler("cusslist", cmd_cusslist))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    app.run_polling()
