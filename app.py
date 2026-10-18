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

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Configuration ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# Read multiple comma-separated keys
raw_keys = os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", ""))
API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
current_key_index = 0

CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'A',
    'В': 'B', 'в': 'B',
    'С': 'C', 'с': 'C',
    'D': 'D', 'd': 'D', 'Д': 'D', 'д': 'D'
}
# Strict single-letter match: rejects "a b c d", "abcd", etc.
ANSWER_PATTERN = re.compile(r"^\s*([A-Da-dА-Ва-вСсДд])[.)]?\s*$")

# Topic pools
TOPIC_POOLS = [
    "O'zbekiston tarixi: Qadimgi Baqtriya, So'g'diyona va Kushonlar davri",
    "O'zbekiston tarixi: Somoniylar, Qoraxoniylar va Xorazmshohlar davlati",
    "O'zbekiston tarixi: Amir Temur harbiy yurishlari va diplomatik aloqalari",
    "O'zbekiston tarixi: Turkiston jadidchilik harakati va 20-asr fojialari",
    "Jahon tarixi: Qadimgi Mesopotamiya, Bobil va Finikiya sivilizatsiyasi",
    "Jahon tarixi: Qadimgi Rim senati, imperatorlari va muhim janglari",
    "Jahon tarixi: O'rta asrlar Yevropasi, Salib yurishlari va feodalizm",
    "Astronomiya: Ekzosayyoralar, neytron yulduzlar va relyikt nurlanish",
    "Kosmonavtika: Mashhur kosmik zondlar (Voyager, Cassini, James Webb) missiyalari",
    "Fizika: Nisbiylik nazariyasi, optik illyuziyalar va kvant kashfiyotlari",
    "Kimyo: Davriy jadvaldagi radioaktiv elementlar va kutilmagan reaksiyalar",
    "Inson biologiyasi: Neyronlar, gormonlar, miya tuzilishi va qon aylanishi",
    "Zoologiya va Botanika: Dunyodagi eng zaharli va noyob tirik mavjudotlar",
    "Geografiya: Dengizga chiqish yo'li yo'q davlatlar, kutilmagan eksklavlar",
    "Texnologiya: Internet protokollari, kriptografiya va kompyuter tarixi",
    "Mantiq va Deduksiya: Detektiv uslubidagi faktlar va chalkash mantiqiy masalalar"
]

# In-memory question buffer (fills in batches of 8)
question_queue = []

round_state = {
    "active": False,
    "q_num": 1,
    "correct": None,
    "options": {},
    "explanation": "",
    "question_text": "",
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
        self.end_headers()

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# --- API Key Rotation ---
def get_active_gemini_url():
    global current_key_index
    if not API_KEYS:
        return ""
    key = API_KEYS[current_key_index % len(API_KEYS)]
    return f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={key}"

def rotate_key():
    global current_key_index
    if len(API_KEYS) > 1:
        current_key_index = (current_key_index + 1) % len(API_KEYS)
        logger.warning(f"Rotating to next API key index: {current_key_index + 1}/{len(API_KEYS)}")

# --- Telegram Storage ---
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

async def update_pin(bot, next_q_num, scores_dict, new_question_summary=None):
    try:
        pin = await get_or_create_leaderboard(bot)
        _, _, history = parse_pin_data(getattr(pin, "text", "") or "")
        
        if new_question_summary:
            clean_summary = new_question_summary.replace("\n", " ").strip()[:90]
            history.insert(0, clean_summary)
            history = history[:15]
            
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

# --- Batch Question Generator (8 Questions in 1 API Call) ---
def fetch_batch_questions(recent_history):
    history_block = "\n".join([f"- {item}" for item in recent_history]) if recent_history else "Yo'q"
    sampled_topics = random.sample(TOPIC_POOLS, 6)

    prompt = f"""
    Sen Telegram intellektual viktorinasi uchun 8 TA HAR XIL va UNIKAL savollar to'plamini tuzishing kerak.
    
    MAVZULAR: {', '.join(sampled_topics)}.
    
    QAT'IY QOIDALAR:
    1. Yaqinda berilgan savollarni takrorlama:
    {history_block}
    2. Eng sodda, qolipli savollarni (Merkuriy, Everest, Temur qachon tug'ilgan) aslo berma.
    3. Savollar o'rta va qiyin, faktik 100% to'g'ri bo'lsin.
    4. Har bir savolda 4 ta variant (A, B, C, D) bo'lsin.
    
    Faqat quyidagi JSON formatida 8 ta savoldan iborat LIST qaytar:
    [
        {{
            "subject": "Fan",
            "question": "Savol matni",
            "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
            "correct": "A",
            "explanation": "Qisqa tushuntirish"
        }},
        ...
    ]
    """

    attempts = len(API_KEYS) or 1
    for _ in range(attempts):
        url = get_active_gemini_url()
        try:
            res = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "response_mime_type": "application/json",
                        "temperature": 1.15
                    }
                },
                timeout=30
            )
            data = res.json()
            if "error" in data:
                logger.error(f"Gemini API Error: {data['error'].get('message')}")
                rotate_key()
                continue
                
            raw_text = data['candidates'][0]['content']['parts'][0]['text'].strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text)
                
            batch = json.loads(raw_text)
            if isinstance(batch, list) and len(batch) > 0:
                logger.info(f"Successfully generated batch of {len(batch)} questions!")
                return batch
        except Exception as e:
            logger.warning(f"Batch request failed on current key: {e}")
            rotate_key()

    return []

def get_next_question(recent_history):
    global question_queue
    if not question_queue:
        logger.info("Question queue empty. Requesting new batch from Gemini...")
        question_queue = fetch_batch_questions(recent_history)
        
    if question_queue:
        return question_queue.pop(0)
    
    # Emergency fallback if all keys are unavailable
    return {
        "subject": "Tarix",
        "question": "1402-yilgi Anqara jangida Amir Temur qo'shinida qaysi hayvonlardan qurol sifatida foydalanilgan?",
        "options": {"A": "Jangovar fillar", "B": "Otliq tuyalar", "C": "Burgutlar", "D": "Qoplonlar"},
        "correct": "A",
        "explanation": "Amir Temur Hindiston yurishida qo'lga kiritilgan jangovar fillardan foydalangan."
    }

# --- Round Engine ---
async def post_hourly_question(app):
    global round_state
    
    pin = await get_or_create_leaderboard(app.bot)
    q_num, _, history = parse_pin_data(getattr(pin, "text", "") or "")
    
    data = get_next_question(history)
    
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
        return  # Rejects "a b c d", "abcd", or conversational text

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
        logger.info(f"Next hourly question in {seconds_to_wait}s.")
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
