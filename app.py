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
    # yban / yeban / eban variants (Latin + Cyrillic + elongations)
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

VULGAR_REGEX = re.compile(
    r"(?i)\b(" + "|".join(VULGAR_PATTERNS) + r")\b",
    re.UNICODE
)

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

# Strict single-letter match
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

# --- Grade Progression Helper ---
def get_grade_info(q_num: int):
    """Calculates school grade level based on 1,000 questions per tier."""
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
    Sen Telegram viktorinasi uchun SAVOL TUZUVCHI pedagog va mutaxassissan.
    Hozirgi savol raqami: #{q_num} / 1 000 000.
    Unikal kalit: {cache_buster}.
    
    QAT'IY BOSQICH VA DARAJA TALABI:
    - Hozirgi daraja: **{grade_label}** darajasi ({grade_score}).
    - Savol murakkabligi, tili, variantlari va mantiqi aynan **{grade_label}** o'quvchisi qiynalmasdan tushunadigan soddalikda bo'lsin.
    - O'ta qiyin ilmiy terminlar, chalkash tafsilotlar yoki akademik ensiklopediya savollarini BERMA!
    - Agar mavzu maktab dasturida bo'lmasa ham (masalan, Kosmonavtika, Kriptografiya yoki Islom tarixi), uni aynan {grade_label} o'quvchisining dunyoqarashiga moslab, sodda va qiziqarli qilib tuz.

    MAVZU: {chosen_topic}
    USLUB: {chosen_angle}
    
    QO'SHIMCHA TAQIQLAR:
    1. LINGVISTIK VA SO'Z KELIB CHIQISHI TAQIQLANADI:
       - 'Qaysi tildan olingan?', 'So'zning ma'nosi nima?' kabi etimologik savollarni ASLO berma.
    2. SO'NGGI SAVOLLAR TAKRORLANMASIN:
    {history_block}
    3. Agar mavzu Islom dini yoki tarixiga oid bo'lsa, savol faqat ishonchli (sahih) manbalar asosida, hurmat bilan va nozik ixtilofli masalalarga kirmasdan tuzilsin.
    4. 4 ta variant (A, B, C, D) ber. Faqat 1 tasi to'g'ri bo'lsin.
    
    Faqat quyidagi JSON formatida javob ber (hech qanday markdown kod blokisiz):
    {{
        "subject": "{chosen_topic.split(':')[0].strip()}",
        "grade": "{grade_label}",
        "difficulty_score": "{grade_score}",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa va bolalar ham tushunadigan sodda tushuntirish (1-2 gap)"
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
        "temperature": 0.75
    }
    
    try:
        res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=15)
        data = res.json()
        if "error" in data:
            logger.error(f"Groq API Error: {data['error'].get('message')}")
            raise ValueError(data['error'].get('message'))
            
        raw_text = data['choices'][0]['message']['content'].strip()
        parsed = json.loads(raw_text)
        logger.info(f"Groq generated ({grade_label}): {parsed.get('question')[:40]}...")
        return parsed
    except Exception as e:
        logger.error(f"Groq request failed: {e}", exc_info=True)
        return {
            "subject": "Tarix",
            "grade": grade_label,
            "difficulty_score": grade_score,
            "question": "Amir Temur davlatining poytaxti qaysi shahar bo'lgan?",
            "options": {"A": "Samarqand", "B": "Buxoro", "C": "Toshkent", "D": "Xiva"},
            "correct": "A",
            "explanation": "Amir Temur Samarqand shahrini o'z saltanatining poytaxti etib belgilagan va uni obod qilgan."
        }

# --- 10-Second Intake Evaluator (Tiered Scoring) ---
async def evaluate_initial_window(app, target_q_num: int):
    """Waits 10 seconds, then awards points (10, 5, 3) to the fastest correct guesses."""
    await asyncio.sleep(10)
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
            winners_lines.append(f"{medals[idx]} {name} — +{pts} ball ({idx + 1}-o‘rin)")

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

        reply = (
            f"⏰ 10 SONIYA TUGADI!\n\n"
            f"🎯 TO‘G‘RI JAVOB: {correct_choice}) {ans_text}\n"
            f"💡 Izoh: {round_state['explanation']}\n\n"
            f"⚡ G‘OLIBLAR VA BALLAR:\n"
            f"{winners_block}{extra_note}\n\n"
            f"📊 TOP-10 REYTINQ:\n"
            f"{leaderboard_str}\n\n"
            f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
        )
        await app.bot.send_message(GROUP_CHAT_ID, reply)
    else:
        if answers:
            await app.bot.send_message(
                GROUP_CHAT_ID,
                "⏳ 10 soniya o‘tdi, ammo to‘g‘ri javob berilmadi!\n"
                "🔥 O‘yin davom etadi — endi kim birinchi to‘g‘ri topsa, to‘liq 10 ball oladi!"
            )

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
        f"⏳ Dastlabki 10 soniya ichida eng tez topgan 3 kishi ball oladi (🥇10, 🥈5, 🥉3)!\n"
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

    # Delete the user's answer immediately to eliminate chat spam & prevent copying
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

    # Live-edit the question message to show the updated list of respondents
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

    # PHASE 1: Collect guesses silently during the first 10 seconds
    if round_state["in_initial_window"]:
        round_state["first_10s_answers"].append({
            "user": user,
            "choice": choice,
            "timestamp": time.time()
        })
        return

    # PHASE 2: After the 10-second mark (Sudden-Death)
    try:
        if choice == round_state["correct"]:
            round_state["active"] = False
            winner_name = user_name
            
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
                f"⏳ Keyingi savol 5 daqiqadan so‘ng chiqadi."
            )
            await context.bot.send_message(GROUP_CHAT_ID, reply)
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
        f"⏳ Keyingi savol 5 daqiqadan so‘ng yoki /test orqali chiqadi."
    )
    await update.message.reply_text(reply)

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
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    app.run_polling()
