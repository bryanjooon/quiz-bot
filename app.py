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
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROUP_CHAT_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"

CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'A',
    'В': 'B', 'в': 'B',
    'С': 'C', 'с': 'C',
    'D': 'D', 'd': 'D', 'Д': 'D', 'д': 'D'
}
ANSWER_PATTERN = re.compile(r"^\s*([A-Da-dА-Ва-вСсДд])(\b|[.)\s]|$)")

# Deep pools of specialized topics
TOPIC_POOLS = [
    "O'zbekiston tarixi: Qadimgi Baqtriya, So'g'diyona va Kushon imperiyasi",
    "O'zbekiston tarixi: Somoniylar, Qoraxoniylar va Xorazmshohlar davlati",
    "O'zbekiston tarixi: Amir Temur harbiy yurishlari va diplomatik aloqalari",
    "O'zbekiston tarixi: Turkiston jadidchilik harakati va 20-asr fojialari",
    "Jahon tarixi: Qadimgi Mesopotamiya, Bobil va Finikiya sivilizatsiyasi",
    "Jahon tarixi: Qadimgi Rim senati, imperatorlari va muhim janglari",
    "Jahon tarixi: O'rta asrlar Yevropasi, Salib yurishlari va feodalizm",
    "Jahon tarixi: Birinchi va Ikkinchi jahon urushidagi kam ma'lum burilish nuqtalari",
    "Astronomiya: Ekzosayyoralar, neytron yulduzlar va pulsar hodisalari",
    "Astronomiya: Galaktikalar to'qnashuvi, relyikt nurlanish va qora tuynuklar",
    "Kosmonavtika: Mashhur kosmik zondlar (Voyager, Cassini, New Horizons) missiyalari",
    "Fizika: Nisbiylik nazariyasi, optik illyuziyalar va yorug'lik tabiati",
    "Fizika: Termodinamika qonunlari va zamonaviy kvant kashfiyotlari",
    "Kimyo: Davriy jadvaldagi radioaktiv elementlar va ularning ishlatilishi",
    "Kimyo: Kundalik hayotdagi g'aroyib kimyoviy reaksiyalar va qotishmalar",
    "Inson biologiyasi: Neyronlar, gormonlar, miya tuzilishi va qon aylanishi",
    "Biologiya va Zoologiya: Dengiz tubi (Abissal) mavjudotlari va ularning moslashuvi",
    "Biologiya va Botanika: Dunyodagi eng zaharli, kamyob va qadimgi o'simliklar",
    "Geografiya: Dengizga chiqish yo'li yo'q davlatlar, kutilmagan eksklavlar va chegaralar",
    "Geografiya: Dunyodagi eng xavfli vulqonlar, chuqur botqoqliklar va tektonik yoriqlar",
    "Texnologiya va Kiberxavfsizlik: Internet protokollari, kriptografiya va kompyuter tarixi",
    "Adabiyot va Falsafa: Antik davr faylasuflari (Suqrot, Aflotun) va Sharq donishmandlari",
    "Mantiq va Deduksiya: Detektiv uslubidagi faktlar va chalkash mantiqiy masalalar"
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
    "attempted_users": set()
}

# --- Keep-Alive Web Server ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Quiz bot is running 24/7!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# --- Pinned Message Parsing (Scores + Question History) ---
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
        logger.error(f"Could not pin leaderboard: {e}")
    return msg

def parse_pin_data(pin_text):
    """Extracts scores, current question count, and recent questions history."""
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
            if line.startswith("•") and not "boshlanmoqda" in line:
                history.append(line.replace("•", "").strip())
                
    return current_q, scores, history

async def update_pin(bot, next_q_num, scores_dict, new_question_summary=None):
    try:
        pin = await get_or_create_leaderboard(bot)
        _, _, history = parse_pin_data(getattr(pin, "text", "") or "")
        
        if new_question_summary:
            clean_summary = new_question_summary.replace("\n", " ").strip()[:90]
            history.insert(0, clean_summary)
            history = history[:15]  # Keep last 15 questions in memory
            
        # Format scores
        if scores_dict:
            sorted_scores = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
            medals = ["🥇", "🥈", "🥉"]
            score_lines = []
            for idx, (name, pts) in enumerate(sorted_scores, start=1):
                prefix = medals[idx-1] if idx <= 3 else f"{idx}."
                score_lines.append(f"{prefix} {name} — {pts} ball")
            scores_body = "\n".join(score_lines)
        else:
            scores_body = "— Hozircha ballar yo‘q —"
            
        # Format history
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

def fetch_question(q_num, recent_history):
    chosen_topic = random.choice(TOPIC_POOLS)
    chosen_angle = random.choice(QUESTION_ANGLES)
    cache_buster = f"{int(time.time())}_{random.randint(1000, 9999)}"
    
    history_block = "\n".join([f"- {item}" for item in recent_history]) if recent_history else "Hozircha yo'q"

    prompt = f"""
    Sen Telegram viktorinasi uchun ORIGINAL va TAKRORLANMAS savol tuzuvchisan.
    Hozirgi savol raqami: #{q_num} / 1 000 000.
    Tasodifiy kalit: {cache_buster}.
    
    MAVZU: {chosen_topic}
    USLUB: {chosen_angle}
    
    QAT'IY QOIDALAR:
    1. QUYIDAGI YAQUNDA BERILGAN SAVOLLARNI VA ULARNING MAVZUSINI TAKRORLAMA:
    {history_block}
    
    2. Eng sodda, qolipli savollarni aslo berma (Merkuriy, Everest, Baykal, Temur qachon tug'ilgan kabilar taqiqlanadi).
    3. Savol aqlli, qiziqarli, o'rta yoki qiyin darajada bo'lsin.
    4. 4 ta variant (A, B, C, D) ber. Faqat 1 tasi to'g'ri bo'lsin.
    
    Faqat quyidagi JSON formatida to'g'ridan-to'g'ri javob ber:
    {{
        "subject": "{chosen_topic.split(':')[0].strip()}",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa tushuntirish (1-2 gap)"
    }}
    """
    
    try:
        res = requests.post(
            GEMINI_URL,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "temperature": 1.15,
                    "top_p": 0.95
                }
            },
            timeout=30  # Expanded from 12s to prevent premature timeouts
        )
        data = res.json()
        if "error" in data:
            logger.error(f"Google API Error: {data['error'].get('message')}")
            raise ValueError(data['error'].get('message'))
            
        raw_text = data['candidates'][0]['content']['parts'][0]['text'].strip()
        
        # Clean any accidental markdown wrap
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text)
            
        return json.loads(raw_text)
        
    except Exception as e:
        logger.error(f"Gemini API request failed completely: {e}", exc_info=True)
        
        # Multi-question emergency pool so it never repeats the same fallback
        emergency_pool = [
            {
                "subject": "Tarix",
                "question": "Qaysi mashhur xonlik davrida 'Qo'qon adabiy muhiti' o'zining eng yuqori cho'qqisiga erishgan?",
                "options": {"A": "Umarxon", "B": "Madalixon", "C": "Olimxon", "D": "Xudoyorxon"},
                "correct": "A",
                "explanation": "Amir Umarxon (taxallusi Amiriy) davrida Qo'qonda Fazliy, Nodira kabi 70 dan ortiq shoirlar ijod qilgan."
            },
            {
                "subject": "Astronomiya",
                "question": "Quyosh tizimida o'z o'qi atrofida soat millari yo'nalishida (teskari) aylanuvchi sayyora qaysi?",
                "options": {"A": "Venera", "B": "Neptun", "C": "Yupiter", "D": "Mars"},
                "correct": "A",
                "explanation": "Venera boshqa barcha sayyoralardan farqli ravishda sharqdan g'arbga qarab teskari aylanadi."
            },
            {
                "subject": "Fizika",
                "question": "Absolyut nol harorat Selsiy shkalasi bo'yicha necha darajaga teng?",
                "options": {"A": "-273.15°C", "B": "-100°C", "C": "-300°C", "D": "-212°C"},
                "correct": "A",
                "explanation": "0 Kelvin absolyut nol hisoblanadi va u -273.15°C ga to'g'ri keladi."
            }
        ]
        return random.choice(emergency_pool)
# --- Round Engine ---
async def post_hourly_question(app):
    global round_state
    
    pin = await get_or_create_leaderboard(app.bot)
    q_num, scores, history = parse_pin_data(getattr(pin, "text", "") or "")
    
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
    if not round_state["active"]:
        return

    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    match = ANSWER_PATTERN.match(text)
    if not match:
        return

    user = update.message.from_user
    if user.id in round_state["attempted_users"]:
        await update.message.reply_text(f"⚠️ {user.first_name}, siz bu savolga javob berib bo‘ldingiz!")
        return

    round_state["attempted_users"].add(user.id)
    raw_choice = match.group(1)
    choice = CYRILLIC_TO_LATIN.get(raw_choice, raw_choice).upper()

    try:
        if choice == round_state["correct"]:
            round_state["active"] = False
            winner_name = user.first_name or "O‘yinchi"
            
            # Read current pin data, calculate next question number & new score
            pin = await get_or_create_leaderboard(context.bot)
            q_num, scores, _ = parse_pin_data(getattr(pin, "text", "") or "")
            scores[winner_name] = scores.get(winner_name, 0) + 10
            
            # Save question into the persistent history inside the pinned message
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
                f"📊 JORIY REYTINQ:\n"
                f"{leaderboard_str}\n\n"
                f"⏳ Keyingi savol keyingi soat boshida chiqadi."
            )
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(f"❌ Noto‘g‘ri, {user.first_name}! Bu soat uchun o‘yindan chiqdingiz.")
    except Exception as e:
        logger.error(f"Error handling answer reply: {e}", exc_info=True)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return
    await update.message.reply_text("🧪 Test savoli ishga tushirilmoqda...")
    await post_hourly_question(context.application)

async def scheduler(app):
    while True:
        now = datetime.now(TASHKENT_TZ)
        seconds_to_wait = (60 - now.minute - 1) * 60 + (60 - now.second)
        logger.info(f"Next question in {seconds_to_wait}s.")
        await asyncio.sleep(seconds_to_wait)
        await post_hourly_question(app)
        await asyncio.sleep(5)

async def on_startup(app):
    asyncio.create_task(scheduler(app))

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    app.run_polling()
