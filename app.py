import os
import re
import json
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pytz
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- Configuration ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROUP_CHAT_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

ANSWER_PATTERN = re.compile(r"^\s*([A-Da-d])(\b|[.)\s])")

round_state = {
    "active": False,
    "q_num": 1,
    "correct": None,
    "options": {},
    "explanation": "",
    "attempted_users": set()
}

# --- Web Server for Keep-Alive Pings ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Quiz bot is running 24/7!")

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
    except Exception:
        return {
            "subject": "Astronomiya",
            "question": "Quyoshga eng yaqin sayyora qaysi?",
            "options": {"A": "Venera", "B": "Merkuriy", "C": "Mars", "D": "Yer"},
            "correct": "B",
            "explanation": "Merkuriy Quyosh tizimidagi eng yaqin sayyoradir."
        }

# --- Persistent Telegram Pinned Leaderboard ---
async def get_or_create_leaderboard(bot):
    chat = await bot.get_chat(GROUP_CHAT_ID)
    if chat.pinned_message and "YETAKCHILAR JADVALI" in (chat.pinned_message.text or ""):
        return chat.pinned_message
    
    init_text = (
        "🏆 **1 MILLION QUESTIONS · YETAKCHILAR JADVALI**\n"
        "Joriy savol: #0000001\n\n"
        "— Hozircha ballar yo‘q —"
    )
    msg = await bot.send_message(GROUP_CHAT_ID, init_text, parse_mode="Markdown")
    await bot.pin_chat_message(GROUP_CHAT_ID, msg.message_id)
    return msg

async def update_score_in_pin(bot, winner_name, points=10):
    pin = await get_or_create_leaderboard(bot)
    text = pin.text
    
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
        f"🏆 **1 MILLION QUESTIONS · YETAKCHILAR JADVALI**\n"
        f"Joriy savol: #{current_q:07d}\n\n"
        f"{body}"
    )
    await bot.edit_message_text(chat_id=GROUP_CHAT_ID, message_id=pin.message_id, text=new_text, parse_mode="Markdown")

# --- Round Engine ---
async def post_hourly_question(app):
    global round_state
    
    pin = await get_or_create_leaderboard(app.bot)
    q_match = re.search(r"Joriy savol: #(\d+)", pin.text or "")
    q_num = int(q_match.group(1)) if q_match else 1
    
    data = fetch_question(q_num)
    
    round_state["active"] = True
    round_state["q_num"] = q_num
    round_state["correct"] = data["correct"]
    round_state["options"] = data["options"]
    round_state["explanation"] = data["explanation"]
    round_state["attempted_users"].clear()
    
    msg_text = (
        f"🏆 **1 MILLION QUESTIONS · #{q_num:07d}**\n"
        f"📚 Fan: **{data['subject']}**\n\n"
        f"❓ {data['question']}\n\n"
        f"**A)** {data['options']['A']}\n"
        f"**B)** {data['options']['B']}\n"
        f"**C)** {data['options']['C']}\n"
        f"**D)** {data['options']['D']}\n\n"
        f"⚡ _To‘g‘ri variant harfini yozing (A, B, C yoki D)! Bir kishiga 1 ta urinish._"
    )
    await app.bot.send_message(GROUP_CHAT_ID, msg_text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global round_state
    if not round_state["active"] or not update.message or not update.message.text:
        return
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    match = ANSWER_PATTERN.match(update.message.text.strip())
    if not match:
        return

    user = update.message.from_user
    if user.id in round_state["attempted_users"]:
        await update.message.reply_text(f"⚠️ {user.first_name}, siz allaqachon javob berdingiz!", quote=True)
        return

    round_state["attempted_users"].add(user.id)
    choice = match.group(1).upper()

    if choice == round_state["correct"]:
        round_state["active"] = False
        winner_name = user.first_name or "O‘yinchi"
        
        await update_score_in_pin(context.bot, winner_name, points=10)
        
        correct_letter = round_state["correct"]
        ans_text = round_state["options"].get(correct_letter, "")
        
        reply = (
            f"🎯 **TO‘G‘RI JAVOB!**\n\n"
            f"⚡ **G‘olib:** {user.mention_markdown()} (+10 ball!)\n"
            f"✅ **Javob:** {correct_letter}) {ans_text}\n"
            f"💡 **Izoh:** {round_state['explanation']}\n\n"
            f"⏳ Keyingi savol keyingi soat boshida chiqadi."
        )
        await update.message.reply_text(reply, parse_mode="Markdown", quote=True)
    else:
        await update.message.reply_text(f"❌ Noto‘g‘ri, {user.first_name}! Bu soat uchun o‘yindan chiqdingiz.", quote=True)

async def scheduler(app):
    while True:
        now = datetime.now(TASHKENT_TZ)
        seconds_to_wait = (60 - now.minute - 1) * 60 + (60 - now.second)
        await asyncio.sleep(seconds_to_wait)
        await post_hourly_question(app)
        await asyncio.sleep(5)

async def on_startup(app):
    asyncio.create_task(scheduler(app))

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
