import os
import re
import json
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pytz
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# --- Enable Full Logging ---
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
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

ANSWER_PATTERN = re.compile(r"^\s*([A-Da-d])(\b|[.)\s]|$)")

round_state = {
    "active": False,
    "q_num": 1,
    "correct": None,
    "options": {},
    "explanation": "",
    "attempted_users": set()
}

# --- Web Server for Keep-Alive ---
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

# --- Question Generator ---
def fetch_question(q_num):
    prompt = f"""
    Telegram viktorinasi uchun savol tuz. Savol raqami: {q_num}/1000000.
    Fanlar: Tarix, Geografiya, Astronomiya, Fan-texnika, Biologiya, Mantiq.
    Faqat to'g'ridan-to'g'ri JSON qaytar:
    {{
        "subject": "Fan",
        "question": "Savol matni o'zbek tilida",
        "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
        "correct": "A",
        "explanation": "Qisqa tushuntirish"
    }}
    """
    try:
        res = requests.post(
            GEMINI_URL,
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"response_mime_type": "application/json"}},
            timeout=10
        )
        return json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
    except Exception as e:
        logger.warning(f"Gemini API fallback triggered: {e}")
        return {
            "subject": "Astronomiya",
            "question": "Quyoshga eng yaqin sayyora qaysi?",
            "options": {"A": "Venera", "B": "Merkuriy", "C": "Mars", "D": "Yer"},
            "correct": "B",
            "explanation": "Merkuriy Quyosh tizimidagi eng yaqin sayyoradir."
        }

# --- Pinned Leaderboard Handler ---
async def get_or_create_leaderboard(bot):
    chat = await bot.get_chat(GROUP_CHAT_ID)
    pinned = chat.pinned_message
    pinned_text = getattr(pinned, "text", "") or ""

    if pinned and "YETAKCHILAR JADVALI" in pinned_text:
        return pinned
    
    init_text = (
        "🏆 1 MILLION QUESTIONS · YETAKCHILAR JADVALI\n"
        "Joriy savol: #0000001\n\n"
        "— Hozircha ballar yo‘q —"
    )
    msg = await bot.send_message(GROUP_CHAT_ID, init_text)
    try:
        await bot.pin_chat_message(GROUP_CHAT_ID, msg.message_id)
    except Exception as e:
        logger.error(f"Could not pin leaderboard message: {e}")
    return msg

async def update_score_in_pin(bot, winner_name, points=10):
    try:
        pin = await get_or_create_leaderboard(bot)
        text = getattr(pin, "text", "") or ""
        
        q_match = re.search(r"Joriy savol: #(\d+)", text)
        current_q = int(q_match.group(1)) + 1 if q_match else 1
        
        scores = {}
        for line in text.split("\n"):
            m = re.match(r"^• (.+?): (\d+) ball", line.strip())
            if m:
                scores[m.group(1)] = int(m.group(2))
                
        scores[winner_name] = scores.get(winner_name, 0) + points
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        body = "\n".join([f"• {name}: {pts} ball" for name, pts in sorted_scores])
        
        new_text = (
            f"🏆 1 MILLION QUESTIONS · YETAKCHILAR JADVALI\n"
            f"Joriy savol: #{current_q:07d}\n\n"
            f"{body}"
        )
        await bot.edit_message_text(chat_id=GROUP_CHAT_ID, message_id=pin.message_id, text=new_text)
    except Exception as e:
        logger.error(f"Failed to update pinned message: {e}")

# --- Round Engine ---
async def post_hourly_question(app):
    global round_state
    
    pin = await get_or_create_leaderboard(app.bot)
    pin_text = getattr(pin, "text", "") or ""
    q_match = re.search(r"Joriy savol: #(\d+)", pin_text)
    q_num = int(q_match.group(1)) if q_match else 1
    
    data = fetch_question(q_num)
    
    round_state["active"] = True
    round_state["q_num"] = q_num
    round_state["correct"] = data["correct"].upper()
    round_state["options"] = data["options"]
    round_state["explanation"] = data["explanation"]
    round_state["attempted_users"].clear()
    
    msg_text = (
        f"🏆 1 MILLION QUESTIONS · #{q_num:07d}\n"
        f"📚 Fan: {data['subject']}\n\n"
        f"❓ {data['question']}\n\n"
        f"A) {data['options']['A']}\n"
        f"B) {data['options']['B']}\n"
        f"C) {data['options']['C']}\n"
        f"D) {data['options']['D']}\n\n"
        f"⚡ To‘g‘ri variant harfini yozing (A, B, C yoki D)! Bir kishiga 1 ta urinish."
    )
    await app.bot.send_message(GROUP_CHAT_ID, msg_text)
    logger.info(f"Question #{q_num} posted. Correct answer is {round_state['correct']}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global round_state
    
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    logger.info(f"Incoming group message: '{text}' from {update.message.from_user.first_name}")

    if not round_state["active"]:
        return

    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    match = ANSWER_PATTERN.match(text)
    if not match:
        return

    user = update.message.from_user
    if user.id in round_state["attempted_users"]:
        await update.message.reply_text(f"⚠️ {user.first_name}, siz allaqachon javob berdingiz!", quote=True)
        return

    round_state["attempted_users"].add(user.id)
    choice = match.group(1).upper()
    logger.info(f"User {user.first_name} guessed: {choice} (Correct: {round_state['correct']})")

    try:
        if choice == round_state["correct"]:
            round_state["active"] = False
            winner_name = user.first_name or "O‘yinchi"
            
            await update_score_in_pin(context.bot, winner_name, points=10)
            
            correct_letter = round_state["correct"]
            ans_text = round_state["options"].get(correct_letter, "")
            
            reply = (
                f"🎯 TO‘G‘RI JAVOB!\n\n"
                f"⚡ G‘olib: {winner_name} (+10 ball!)\n"
                f"✅ Javob: {correct_letter}) {ans_text}\n"
                f"💡 Izoh: {round_state['explanation']}\n\n"
                f"⏳ Keyingi savol keyingi soat boshida chiqadi."
            )
            await update.message.reply_text(reply, quote=True)
        else:
            await update.message.reply_text(f"❌ Noto‘g‘ri, {user.first_name}! Bu soat uchun o‘yindan chiqdingiz.", quote=True)
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
        logger.info(f"Next question scheduled in {seconds_to_wait} seconds.")
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
