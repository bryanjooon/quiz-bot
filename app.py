import os
import re
import json
import time
import copy
import html
import random
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta

import pytz
import requests
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

# =====================================================================
# Logging
# =====================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# =====================================================================
# Environment
# =====================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

raw_ids = os.getenv("TELEGRAM_GROUP_IDS", os.getenv("TELEGRAM_GROUP_ID", "0"))
GROUP_CHAT_IDS = [int(i.strip()) for i in raw_ids.split(",") if i.strip() and i.strip() != "0"]

# The "global player" whose answers count in every group.
# Set GLOBAL_USER_IDS to their numeric Telegram user ID (safe: nobody can fake it).
# If it's not set, the bot falls back to matching the first name (anyone can copy a name!).
GLOBAL_USER_IDS = {int(x) for x in os.getenv("GLOBAL_USER_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
GLOBAL_USER_NAME = os.getenv("GLOBAL_USER_NAME", "biloliddin")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Each model has its OWN free-tier budget (8K tokens/min, 200K tokens/day), so spreading
# work across three models triples what the bot can do per day.
GEN_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
CHECK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
ROAST_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

QUESTION_INTERVAL_MIN = 5          # a new question every 5 minutes
INITIAL_WINDOW_SECONDS = 30        # podium window (🥇10 🥈5 🥉3)
EXPIRE_CLEANUP_SECONDS = 60        # question + reveal deleted 1 minute after the reveal
HISTORY_KEEP = 400                 # remembered posted questions (≈ 1.5 days) — never repeated

SCORES_FILE = "scores.json"
STATE_FILE = "state.json"
CUSTOM_BANNED_FILE = "banned_words.json"


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {path}: {e}")
    return default


def _save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")


# =====================================================================
# Bot state (question counter, history, topic deck, pinned message ids)
# =====================================================================
STATE = _load_json(STATE_FILE, {})
STATE.setdefault("next_q", 1)
STATE.setdefault("history", [])
STATE.pop("deck", None)
STATE.setdefault("pins", {})


def save_state():
    _save_json(STATE_FILE, STATE)


# =====================================================================
# Scores — keyed by Telegram user ID (names can be copied, IDs can't)
#   ALL_SCORES[chat_id_str][key] = {"name": str, "score": int}
#   key = "<user_id>"  or  "legacy:<name>"  (old name-based scores)
# =====================================================================
ALL_SCORES = _load_json(SCORES_FILE, {})


def _migrate_scores():
    for cid, entries in ALL_SCORES.items():
        if not isinstance(entries, dict):
            ALL_SCORES[cid] = {}
            continue
        for key in list(entries.keys()):
            val = entries[key]
            if isinstance(val, (int, float)):
                entries.pop(key)
                legacy_key = f"legacy:{key}"
                old = entries.get(legacy_key, {"name": key, "score": 0})
                old["score"] = max(old["score"], int(val))  # old bug created "Name"/"name" twins with equal scores
                entries[legacy_key] = old


_migrate_scores()


def save_scores():
    _save_json(SCORES_FILE, ALL_SCORES)


def get_chat_scores(chat_id: int) -> dict:
    return ALL_SCORES.setdefault(str(chat_id), {})


def display_name(user) -> str:
    name = (getattr(user, "first_name", None) or "Player").strip()
    return clip(name.replace("—", "-"), 32) or "Player"


def is_global_user(user) -> bool:
    if not user:
        return False
    if GLOBAL_USER_IDS:
        return user.id in GLOBAL_USER_IDS
    return (user.first_name or "").strip().lower() == GLOBAL_USER_NAME.lower()


def _entry_for(chat_dict: dict, user) -> dict:
    """Returns the user's score entry, absorbing any old name-based score once."""
    uid = str(user.id)
    name = display_name(user)
    entry = chat_dict.get(uid)
    if entry is None:
        entry = {"name": name, "score": 0}
        chat_dict[uid] = entry
    legacy_keys = [
        k for k, v in chat_dict.items()
        if k.startswith("legacy:") and v.get("name", "").strip().lower() == name.lower()
    ]
    if legacy_keys:
        entry["score"] += max(chat_dict[k]["score"] for k in legacy_keys)
        for k in legacy_keys:
            chat_dict.pop(k, None)
    entry["name"] = name
    return entry


def add_points(chat_id: int, user, pts: int) -> int:
    entry = _entry_for(get_chat_scores(chat_id), user)
    entry["score"] += pts
    if is_global_user(user):
        entries = [_entry_for(get_chat_scores(c), user) for c in GROUP_CHAT_IDS]
        top = max(e["score"] for e in entries)
        for e in entries:
            e["score"] = top
    save_scores()
    return entry["score"]


def set_points(chat_id: int, user, value: int):
    entry = _entry_for(get_chat_scores(chat_id), user)
    entry["score"] = value
    save_scores()


def sorted_board(chat_id: int):
    entries = list(get_chat_scores(chat_id).items())
    entries.sort(key=lambda kv: kv[1].get("score", 0), reverse=True)
    return entries


def format_top10(chat_id: int) -> str:
    board = [e for e in sorted_board(chat_id) if e[1].get("score", 0) != 0][:10]
    if not board:
        return "— No points yet —"
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (_, e) in enumerate(board, start=1):
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}."
        lines.append(f"{prefix} {e['name']} — {e['score']} pts")
    return "\n".join(lines)


# =====================================================================
# Message cleanup sweeper
# =====================================================================
MESSAGES_TO_PURGE = []


def track_for_cleanup(chat_id: int, message_id, delay_seconds: int = EXPIRE_CLEANUP_SECONDS):
    if message_id:
        MESSAGES_TO_PURGE.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "delete_at": time.time() + delay_seconds,
        })


async def message_cleaner_task(bot):
    try:
        while True:
            try:
                now = time.time()
                due = [m for m in MESSAGES_TO_PURGE if now >= m["delete_at"]]
                MESSAGES_TO_PURGE[:] = [m for m in MESSAGES_TO_PURGE if now < m["delete_at"]]
                for item in due:
                    try:
                        await bot.delete_message(chat_id=item["chat_id"], message_id=item["message_id"])
                    except Exception as e:
                        logger.warning(f"Could not delete {item['message_id']} in {item['chat_id']}: {e}")
            except Exception as e:
                logger.error(f"Cleaner loop error: {e}")
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        logger.info("Message cleaner task cancelled.")


async def send_temp(bot, chat_id: int, text: str, seconds: int = 6, **kwargs):
    try:
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except Exception as e:
        logger.warning(f"send_temp failed in {chat_id}: {e}")
        return
    track_for_cleanup(chat_id, msg.message_id, delay_seconds=seconds)


# =====================================================================
# Profanity filter
# =====================================================================
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
    r"sik(?!ka|l)\w*", r"сики\w*", r"сик(?!ка|л)\w*", r"qo[t']oq\w*", r"қотоқ\w*", r"onangni\w*",
    r"otangni\w*", r"ona[nŋ]g\w*", r"itvachcha\w*", r"padariga\w*", r"iflos\w*",
    r"xezalak\w*", r"гей\w*", r"gey\w*",
]

# NOTE: no 'x'->'h' mapping any more — it broke normal Uzbek words (xabar, xop...).
LEET_MAP = str.maketrans({"@": "a", "4": "a", "0": "o", "1": "i", "!": "i", "$": "s", "3": "e"})
SPACED_LETTERS = re.compile(r"(?:\w[\W_]+){2,}\w")  # "f u c k", "s.u.k.a"

CUSTOM_BANNED_WORDS = set(_load_json(CUSTOM_BANNED_FILE, []))


def save_custom_banned():
    _save_json(CUSTOM_BANNED_FILE, sorted(CUSTOM_BANNED_WORDS))


def compile_vulgar_regex():
    patterns = list(VULGAR_PATTERNS)
    for word in CUSTOM_BANNED_WORDS:
        patterns.append(rf"{re.escape(word.strip().lower())}\w*")
    return re.compile(r"(?i)\b(" + "|".join(patterns) + r")\b", re.UNICODE)


VULGAR_REGEX = compile_vulgar_regex()


def is_vulgar(text: str) -> bool:
    if not text:
        return False
    if VULGAR_REGEX.search(text):
        return True
    normalized = text.translate(LEET_MAP)
    if VULGAR_REGEX.search(normalized):
        return True
    # Only glue together letters that were deliberately spaced out ("f u c k")
    joined = SPACED_LETTERS.sub(lambda m: re.sub(r"[\W_]", "", m.group(0)), normalized)
    return joined != normalized and bool(VULGAR_REGEX.search(joined))


ANSWER_PATTERN = re.compile(r"^\s*([A-Da-dАВСДавсд])[.)]?\s*$")
CYRILLIC_TO_LATIN = {"А": "A", "а": "A", "В": "B", "в": "B", "С": "C", "с": "C", "Д": "D", "д": "D"}

# =====================================================================
# Question categories
#   kind "logic" = must be solvable from the text alone (verified by a 2nd AI)
#   kind "fact"  = useful / surprising real-world knowledge
#   weight       = how often it appears in the rotation deck
# =====================================================================
CATEGORIES = [
    {
        "id": "detective", "label": "🕵️ Detective Case", "kind": "logic", "weight": 3,
        "angles": [
            "An alibi that is impossible because of a fact stated in the case (power cut, rain, a closed road, a dead phone battery, a clock that was stopped).",
            "Three suspects each make one statement; exactly one of them lies, and the liar is the thief. Only one assignment of liar is consistent.",
            "A timeline case: stated travel times and clock times show only one suspect could have been at the scene.",
            "A small physical clue stated in the story (a warm car engine, wet umbrella on a dry day, footprints going only one way, half-melted ice in a glass) exposes the culprit.",
            "A forged note or receipt with a giveaway detail stated in the case (a date like 31 April, a shop closed that day, a price in the wrong currency).",
            "Four housemates, one ate the last cake. 3-4 clear clues eliminate everyone except one person.",
            "A witness claims to have seen something that is impossible from where they said they were standing (all positions are given in the text).",
            "A 'locked room' style case with a simple, fair explanation where every needed fact is given in the story.",
        ],
    },
    {
        "id": "logic", "label": "🧩 Logic Puzzle", "kind": "logic", "weight": 2,
        "angles": [
            "Truth-tellers and liars: 2-3 people make statements; exactly one consistent solution.",
            "Ordering puzzle: 4-5 people in a queue or row with clues; ask who is in one specific position.",
            "A balance-scale or water-jug puzzle with a provable minimum number of steps.",
            "A number or letter sequence with ONE clear rule (no second plausible rule); ask the next item.",
            "An age or family-relations riddle with exactly one solution.",
            "Calendar or clock reasoning (day of the week after N days, angle between clock hands at a given time).",
            "Label-swap puzzle (boxes with all-wrong labels) where one action reveals everything.",
            "A 'who owns what' mini grid puzzle with 3 people and 3 items and 3 clues.",
        ],
    },
    {
        "id": "lateral", "label": "💡 Think Outside the Box", "kind": "logic", "weight": 1,
        "angles": [
            "A short strange situation with ONE logical everyday explanation. The key hint must be inside the text; only one option fits ALL stated facts.",
            "A 'how is this possible?' riddle that is fair (no word tricks), where three options each break one stated fact.",
        ],
    },
    {
        "id": "math", "label": "🎲 Probability & Number Traps", "kind": "logic", "weight": 1,
        "angles": [
            "Gambler's fallacy with coins or dice.",
            "Percent traps: price goes up 50% then down 50% — what's the final change?",
            "Average speed over a round trip at two different speeds.",
            "Expected value of a simple bet or game.",
            "Stacked discounts in a shop (e.g. 30% off then an extra 20% off) — the real total discount.",
            "A quick mental-math shortcut question with one exact answer.",
        ],
    },
    {
        "id": "money", "label": "💰 Money Smarts", "kind": "fact", "weight": 1,
        "angles": [
            "Compound interest and the Rule of 72.",
            "Inflation and what happens to savings kept in cash.",
            "How credit card / loan interest quietly grows debt.",
            "Sunk cost vs opportunity cost in an everyday decision.",
            "Why an emergency fund matters and common budgeting rules (like 50/30/20).",
            "How 'buy now, pay later' and subscriptions make people overspend.",
            "Diversification: why not to put all savings in one thing.",
        ],
    },
    {
        "id": "safety", "label": "🛡️ Scam & Online Safety", "kind": "fact", "weight": 1,
        "angles": [
            "Fake bank calls / SMS asking for a code — what to do.",
            "Signs of a phishing link or fake website.",
            "How two-factor authentication protects you and which type is safest.",
            "Public Wi-Fi risks and what is actually safe to do on it.",
            "Fake job offers or 'easy money' Telegram schemes and how they work.",
            "Voice-clone and deepfake calls pretending to be a relative.",
            "Strong passwords vs passphrases and password managers.",
        ],
    },
    {
        "id": "health", "label": "🩺 First Aid & Body Facts", "kind": "fact", "weight": 1,
        "angles": [
            "Correct first aid for a burn (current Red Cross / WHO guidance).",
            "What to do when someone is choking.",
            "Correct way to stop a nosebleed.",
            "A popular health myth that is false (e.g. cracking knuckles, swimming after eating, 'we use 10% of our brain' is banned — pick another).",
            "Sleep science: what really happens when you sleep too little.",
            "How the body handles extreme heat or cold.",
            "Caffeine, sugar and energy: what science actually says.",
        ],
    },
    {
        "id": "science", "label": "🧪 Everyday Science", "kind": "fact", "weight": 1,
        "angles": [
            "Everyday physics: why something common happens (ice floats, sky is blue, metal feels colder than wood).",
            "Kitchen chemistry (baking soda, why onions make you cry, why you never pour water on burning oil).",
            "Electricity at home and common electrical myths.",
            "Phone and battery myths (charging overnight, closing apps, cold weather).",
            "Weather phenomena explained (lightning, rainbows, why mountains are cold).",
        ],
    },
    {
        "id": "psych", "label": "🧠 Psychology & Mind Tricks", "kind": "fact", "weight": 1,
        "angles": [
            "A cognitive bias shown through a short real-life scenario (anchoring, confirmation bias, survivorship bias, bystander effect, Dunning-Kruger, sunk cost).",
            "Memory and learning science (spaced repetition, testing effect, why cramming fails).",
            "Persuasion tricks used by marketers and sellers (decoy pricing, scarcity, social proof).",
            "Famous psychology experiments and what they ACTUALLY found.",
            "Habits: how habits form and how to break them.",
        ],
    },
    {
        "id": "english", "label": "🗣️ English Power-Up", "kind": "fact", "weight": 1,
        "angles": [
            "Commonly confused words (affect/effect, borrow/lend, fewer/less, economic/economical) in a sentence.",
            "The real meaning of a common English idiom.",
            "The surprising origin of an everyday English word.",
            "A useful phrasal verb and its meaning in context.",
            "Which sentence is correct (ONE clear grammar rule, no style debate).",
            "False friends: English words that look like Russian/Uzbek words but mean something else.",
        ],
    },
    {
        "id": "tech", "label": "💻 Tech & AI", "kind": "fact", "weight": 1,
        "angles": [
            "How everyday tech works (GPS, Wi-Fi, QR codes, touchscreens, noise-cancelling headphones).",
            "Internet myths (incognito mode, more signal bars = faster internet).",
            "AI basics in plain words (what a language model actually does, what 'training' means).",
            "Surprising origins of famous tech companies or products.",
            "Computer history moments that sound fake but are true.",
        ],
    },
    {
        "id": "world", "label": "🌍 Surprising World", "kind": "fact", "weight": 1,
        "angles": [
            "Geography that surprises people (odd borders, enclaves, time zones, which country is bigger than it looks).",
            "Everyday customs that differ between countries.",
            "World records that sound fake but are true.",
            "Language facts (most spoken languages, alphabets, languages with no words for something).",
            "Famous landmarks and a surprising fact about how they were built.",
        ],
    },
    {
        "id": "history", "label": "📜 History's Weird Moments", "kind": "fact", "weight": 1,
        "angles": [
            "Inventions discovered by accident.",
            "Weird but true events from world history (not wars in Central Asia).",
            "How ordinary things were done before modern technology.",
            "Famous mistakes or misunderstandings that changed history.",
            "Origins of everyday things (forks, toothpaste, calendars, numbers).",
        ],
    },
    {
        "id": "space", "label": "🚀 Space & Physics", "kind": "fact", "weight": 1,
        "angles": [
            "Space facts that sound fake but are true.",
            "What happens to the human body in space.",
            "The Moon, tides and eclipses explained.",
            "How scientists know things about distant stars and planets.",
        ],
    },
    {
        "id": "nature", "label": "🐾 Clever Nature", "kind": "fact", "weight": 1,
        "angles": [
            "Animal intelligence on land or in the air (crows, parrots, elephants, bees) — NOT deep-sea creatures.",
            "Plants doing surprising things.",
            "Human body vs animal abilities.",
            "Animal myths people believe (bulls hate red, ostriches bury heads...).",
        ],
    },
    {
        "id": "business", "label": "📈 Business & Big Decisions", "kind": "fact", "weight": 1,
        "angles": [
            "Famous business failures and the decision that caused them (Kodak, Nokia, Blockbuster...).",
            "How 'free' apps and services actually make money.",
            "Store layout and pricing tricks.",
            "Negotiation basics that actually work.",
            "How a famous brand got its name.",
        ],
    },
    {
        "id": "food", "label": "🍳 Food Science", "kind": "fact", "weight": 1,
        "angles": [
            "Why food changes when cooked or stored (bread going stale, browning, salt in water).",
            "Popular food myths.",
            "Surprising origins of common foods and dishes.",
            "Food safety facts people get wrong (washing raw chicken, leaving rice out).",
        ],
    },
]

ISLAMIC_CATEGORY = {
    "id": "islamic", "label": "🕌 Islamic Knowledge", "kind": "fact", "weight": 0,
    "angles": [
        "A well-documented event from the Seerah (life of Prophet Muhammad ﷺ).",
        "Stories of the prophets as told in the Quran.",
        "Golden Age Muslim scholars and what they discovered (Al-Khwarizmi, Ibn al-Haytham, Ibn Sina, Al-Biruni, Al-Jazari).",
        "Great scholars from the land of today's Uzbekistan (Imam al-Bukhari, Imam at-Tirmidhi, Al-Maturidi, Az-Zamakhshari, Al-Khwarizmi).",
        "The Sahaba: wise decisions, character and well-known events.",
        "Quran facts (structure, surahs, revelation) that are agreed upon by all.",
        "Islamic manners and wisdom from well-known authentic hadith (Bukhari/Muslim).",
    ],
}

CAT_BY_ID = {c["id"]: c for c in CATEGORIES}
CAT_BY_ID["islamic"] = ISLAMIC_CATEGORY

FACT_FORMATS = [
    "Ask which ONE of four statements is TRUE (the other three are convincing myths).",
    "Ask which ONE of four statements is a MYTH (the other three are true).",
    "Describe a short real-life situation and ask what the smartest action is.",
    "Ask 'What happens if...?' about a surprising cause and effect.",
    "Ask a direct question whose real answer surprises most people.",
    "Ask a direct question whose real answer surprises most people.",
    "Ask for a number or estimate; options must be far apart so only one is right.",
]

BANNED_TOPICS = (
    "Amir Temur, Genghis Khan, deep-sea creatures, the Bermuda Triangle, 'which animal never drinks water', "
    "'how many months have 28 days', goldfish memory, 'we use 10% of our brain', the Great Wall seen from space, "
    "camels storing water in humps"
)


ISLAMIC_WEIGHT = 7  # ≈ every 4th question in the stock is Islamic
_DECK = []


def draw_category() -> dict:
    """Shuffled weighted deck: every category appears before any repeats."""
    if not _DECK:
        new_deck = [c["id"] for c in CATEGORIES for _ in range(c["weight"])] + ["islamic"] * ISLAMIC_WEIGHT
        for _ in range(50):
            random.shuffle(new_deck)
            if all(a != b for a, b in zip(new_deck, new_deck[1:])):
                break
        _DECK.extend(new_deck)
    return CAT_BY_ID[_DECK.pop(0)]


# =====================================================================
# Groq helpers — rate-limit aware (blocking: run via asyncio.to_thread)
#
# Free tier, per model: 8,000 tokens/minute and 200,000 tokens/day.
# Every call reserves (prompt + max_tokens) in a 60-second window so we
# never send a request Groq would refuse, and a 429 puts the model on
# cooldown for exactly as long as Groq says.
# =====================================================================
MODEL_TPM_BUDGET = 7600
RATE_LOCK = threading.Lock()
MODEL_WINDOW = {}     # model -> [[ts, tokens], ...] in the last 60 s
MODEL_COOLDOWN = {}   # model -> unix time when it may be used again
USAGE_TODAY = {"day": "", "tokens": {}, "calls": 0, "rate_limited": 0}
LAST_GROQ_ERROR = {"text": ""}


def _est_tokens(text: str) -> int:
    return len(text) // 3 + 60


def _try_reserve(model: str, needed: int):
    """Returns (entry, 0) if reserved, else (None, seconds_to_wait)."""
    with RATE_LOCK:
        now = time.time()
        until = MODEL_COOLDOWN.get(model, 0)
        if until > now:
            return None, until - now
        window = [e for e in MODEL_WINDOW.get(model, []) if now - e[0] < 61]
        MODEL_WINDOW[model] = window
        used = sum(e[1] for e in window)
        if used + needed > MODEL_TPM_BUDGET:
            oldest = min(e[0] for e in window) if window else now
            return None, max(1.0, 61 - (now - oldest))
        entry = [now, needed]
        window.append(entry)
        return entry, 0


def _parse_retry_seconds(res, err_text: str) -> float:
    try:
        ra = res.headers.get("retry-after")
        if ra:
            return float(ra)
    except Exception:
        pass
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", err_text)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + s
    return 60


def _roll_day():
    """Call with RATE_LOCK held."""
    day = datetime.now(pytz.utc).strftime("%Y-%m-%d")
    if USAGE_TODAY["day"] != day:
        USAGE_TODAY.update({"day": day, "tokens": {}, "calls": 0, "rate_limited": 0})


def _record_usage(model: str, tokens: int):
    with RATE_LOCK:
        _roll_day()
        USAGE_TODAY["tokens"][model] = USAGE_TODAY["tokens"].get(model, 0) + tokens
        USAGE_TODAY["calls"] += 1


def _call_model(model, system, user, json_mode, temperature, max_tokens, reasoning):
    """Returns (content | None, status) with status in ok / rate / error."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    for use_json in ([True, False] if json_mode else [False]):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        if reasoning and model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = reasoning
        try:
            res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
            data = res.json()
        except Exception as e:
            LAST_GROQ_ERROR["text"] = f"{model}: {e}"
            logger.warning(f"Groq {model} request failed: {e}")
            return None, "error"
        if res.status_code == 429:
            err = str(data.get("error", data))
            wait = _parse_retry_seconds(res, err)
            with RATE_LOCK:
                MODEL_COOLDOWN[model] = time.time() + max(wait, 10)
                _roll_day()
                USAGE_TODAY["rate_limited"] += 1
            LAST_GROQ_ERROR["text"] = f"{model}: rate limited for {int(wait)}s"
            logger.warning(f"Groq {model} rate-limited for {int(wait)}s: {err[:160]}")
            return None, "rate"
        if "error" in data:
            err = str(data["error"])
            LAST_GROQ_ERROR["text"] = f"{model}: {err[:150]}"
            logger.warning(f"Groq {model} error: {err[:200]}")
            if use_json and ("response_format" in err or "json" in err.lower()):
                continue  # retry this model without JSON mode
            if "decommissioned" in err or "does not exist" in err or "model_not_found" in err:
                with RATE_LOCK:
                    MODEL_COOLDOWN[model] = time.time() + 6 * 3600
            return None, "error"
        _record_usage(model, int((data.get("usage") or {}).get("total_tokens") or 0))
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except Exception:
            return None, "error"
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return (content, "ok") if content else (None, "error")
    return None, "error"


def groq_chat(system: str, user: str, *, models, json_mode=True, temperature=0.7,
              max_tokens=3000, reasoning="medium", max_wait=0):
    """Tries the models in order, skipping any that are out of budget.
    With max_wait > 0 it sleeps until a model frees up (used by the background worker)."""
    if not GROQ_API_KEY:
        return None
    needed = _est_tokens(system + user) + max_tokens
    deadline = time.time() + max_wait
    while True:
        shortest = None
        for model in models:
            entry, wait = _try_reserve(model, needed)
            if entry is None:
                shortest = wait if shortest is None else min(shortest, wait)
                continue
            content, status = _call_model(model, system, user, json_mode, temperature, max_tokens, reasoning)
            if content:
                return content
        if shortest is None or time.time() + shortest > deadline:
            return None
        time.sleep(min(shortest, 65) + 1)


def parse_json(raw):
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _clean_options(opts):
    if isinstance(opts, list) and len(opts) == 4:
        opts = dict(zip("ABCD", opts))
    if not isinstance(opts, dict):
        return None
    out = {}
    for letter in "ABCD":
        val = opts.get(letter) or opts.get(letter.lower())
        if not val:
            return None
        val = re.sub(r"^\s*[A-Da-d]\s*[\).:]\s*", "", str(val)).strip()
        if not val or len(val) > 140:
            return None
        out[letter] = val
    if len({v.lower() for v in out.values()}) < 4:
        return None
    return out


def _clean_uz(raw):
    if not isinstance(raw, dict):
        return None
    options = _clean_options(raw.get("options"))
    question = str(raw.get("question", "")).strip()
    if not options or not question:
        return None
    return {
        "question": clip(question, 800),
        "options": options,
        "explanation": clip(raw.get("explanation", ""), 350),
        "detailed": clip(raw.get("detailed", ""), 750),
    }


def _clean_question(raw):
    if not isinstance(raw, dict):
        return None
    question = str(raw.get("question", "")).strip()
    options = _clean_options(raw.get("options"))
    correct = str(raw.get("correct", "")).strip().upper()[:1]
    if not question or len(question) > 700 or not options or correct not in ("A", "B", "C", "D"):
        return None
    if any(re.search(r"\b(all|none|both) of the (above|options)\b", o, re.I) for o in options.values()):
        return None
    explanation = clip(raw.get("explanation", ""), 300)
    detailed = clip(raw.get("detailed", "") or raw.get("detailed_explanation", ""), 700)
    if not explanation:
        return None
    return {
        "question": question,
        "options": options,
        "correct": correct,
        "explanation": explanation,
        "detailed": detailed if detailed and detailed != explanation else "",
        "source": clip(raw.get("source", ""), 80),
        "uz": _clean_uz(raw.get("uz")),
    }


STOPWORDS = {"which", "what", "when", "where", "while", "with", "that", "this", "these", "those", "from",
             "have", "does", "your", "they", "their", "them", "there", "into", "about", "most", "more",
             "would", "could", "should", "will", "were", "been", "being", "only", "than", "then", "each",
             "people", "true", "following", "best", "after", "before", "other"}


def _words(text):
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in STOPWORDS}


def too_similar(question: str, recent: list) -> bool:
    w = _words(question)
    if not w:
        return False
    for old in recent:
        o = _words(old)
        if o and len(w & o) / len(w | o) > 0.45:
            return True
    return False


LOGIC_RULES = """LOGIC questions (the most important rules):
- 100% solvable using ONLY the information in the question. No outside knowledge, no word tricks, no hidden assumptions.
- State every clue that is needed. After applying the clues exactly ONE option survives; each wrong option breaks a specific clue.
- Concrete: names, times, places, objects. Short sentences. Up to 70 words.
- Solve your own puzzle before writing it and make sure the answer is unique.
- "detailed" walks through the deduction in 2-4 short steps: "1) ... 2) ... 3) ...".
- Options max 6 words."""

FACT_RULES = """FACT questions:
- Only well-established, checkable facts. No contested claims, no yearly-changing statistics, no country-specific laws.
- Exactly ONE correct option; the other three tempting but clearly wrong once you know the answer.
- Prefer things that are USEFUL in real life or make people say "wow, really?".
- "detailed" (2-3 sentences): why the right answer is right AND why the most tempting wrong option is wrong.
- Max 30 words. Options max 8 words (max 15 if they are full statements)."""

GEN_BATCH = 4


def make_slot():
    cat = draw_category()
    return {
        "cat": cat,
        "angle": random.choice(cat["angles"]),
        "fmt": random.choice(FACT_FORMATS) if cat["kind"] == "fact" else None,
        "difficulty": random.choices(["medium", "hard", "easy"], weights=[5, 3, 1])[0],
    }


def generate_batch(slots, recent):
    slot_lines = []
    for i, s in enumerate(slots, start=1):
        kind = "LOGIC" if s["cat"]["kind"] == "logic" else "FACT"
        line = f"{i}. [{kind}] {s['cat']['label']} — {s['angle']}"
        if s["fmt"]:
            line += f" FORMAT: {s['fmt']}"
        line += f" DIFFICULTY: {s['difficulty']}."
        if s["cat"]["id"] == "islamic":
            line += " Respectful; only facts all mainstream scholars agree on; no disputed fiqh; write ﷺ after the Prophet's name."
        slot_lines.append(line)
    recent_block = "\n".join(f"- {clip(r, 70)}" for r in recent[:30]) or "- (none)"
    prompt = f"""Write {len(slots)} multiple-choice questions for a fun Telegram quiz played by smart young adults in Uzbekistan who are practising English. One question per slot, in order:

{chr(10).join(slot_lines)}

GOAL: "aha, clever!" or "wow, useful, I didn't know that". Never boring textbook trivia. Every question must be about a DIFFERENT idea.
ENGLISH: clear, simple (B1-B2).

{LOGIC_RULES}

{FACT_RULES}

GENERAL:
- Never refer to options by letter in explanations (options get shuffled).
- No "All/None/Both of the above".
- "explanation": ONE punchy sentence. "source": short real reference type (e.g. "Red Cross first-aid guide", "Classic logic puzzle"); never invent a study.
- "uz": a natural, fluent Uzbek translation (Latin script, o‘ and g‘) of question, options, explanation and detailed. Not word-for-word. Same option letters. If the question is about an English word or grammar, keep the English in quotes and explain in Uzbek.
- Do NOT repeat or resemble these recent questions:
{recent_block}
- Avoid worn-out topics: {BANNED_TOPICS}. Also avoid: SMS/bank-code scams, the three mislabelled boxes, the origin of the word "algorithm", coin-flip streaks, the Rule of 72.

Return ONLY JSON:
{{"questions": [{{"slot": 1, "question": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}, "correct": "A", "explanation": "...", "detailed": "...", "source": "...", "uz": {{"question": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}, "explanation": "...", "detailed": "..."}}}}]}}"""
    raw = groq_chat(
        "You are a world-class quiz writer, puzzle designer and English-Uzbek translator. You return only valid JSON.",
        prompt, models=GEN_MODELS, temperature=0.9, max_tokens=4800, reasoning="low", max_wait=180,
    )
    data = parse_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return None
    out = []
    for idx, item in enumerate(data["questions"]):
        q = _clean_question(item)
        if not q:
            continue
        try:
            slot_no = int(item.get("slot", idx + 1)) - 1
        except (TypeError, ValueError):
            slot_no = idx
        slot = slots[slot_no] if 0 <= slot_no < len(slots) else slots[min(idx, len(slots) - 1)]
        q["cat"] = slot["cat"]["id"]
        q["kind"] = slot["cat"]["kind"]
        q["label"] = slot["cat"]["label"]
        out.append(q)
    return out


def verify_batch(qs):
    """A second AI solves every question blind. Returns list[(ok, reason)] or None if no budget."""
    blocks = []
    for i, q in enumerate(qs, start=1):
        kind = "LOGIC (must be solvable from the text alone)" if q["kind"] == "logic" else "FACT (facts must be accurate)"
        opts = "\n".join(f"{L}) {q['options'][L]}" for L in "ABCD")
        blocks.append(f"#{i} [{kind}]\n{q['question']}\n{opts}")
    prompt = f"""Solve each quiz question yourself. You are NOT told the intended answers.

{chr(10).join(blocks)}

For each: give your answer, and set ok=false if two options could be argued correct, if information is missing, if it relies on a wording trick, or if any fact is wrong. Be strict.

Return ONLY JSON: {{"results": [{{"n": 1, "answer": "A", "ok": true, "problem": ""}}]}}"""
    raw = groq_chat(
        "You are a strict quiz fact-checker and logic verifier. You return only valid JSON.",
        prompt, models=CHECK_MODELS, temperature=0.1, max_tokens=4000, reasoning="medium", max_wait=180,
    )
    data = parse_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    by_n = {}
    for r in data["results"]:
        try:
            by_n[int(r.get("n"))] = r
        except (TypeError, ValueError):
            continue
    out = []
    for i, q in enumerate(qs, start=1):
        r = by_n.get(i)
        if not r:
            out.append((False, "not checked"))
            continue
        ans = str(r.get("answer", "")).strip().upper()[:1]
        if ans != q["correct"]:
            out.append((False, f"checker said {ans}, writer said {q['correct']} ({r.get('problem', '')})"))
        elif r.get("ok") is False:
            out.append((False, f"flagged: {r.get('problem', '')}"))
        else:
            out.append((True, "ok"))
    return out


def _leading_number(text):
    m = re.match(r"^\s*[~≈$]?\s*(-?\d[\d,]*\.?\d*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def shuffle_options(q):
    """AIs love putting the answer in A — shuffle (or sort numeric options)."""
    letters = list("ABCD")
    nums = [_leading_number(q["options"][L]) for L in letters]
    order = letters[:]
    if all(n is not None for n in nums):
        order.sort(key=lambda L: nums[letters.index(L)])
    else:
        random.shuffle(order)
    q["options"] = {new: q["options"][old] for new, old in zip(letters, order)}
    if q.get("uz"):
        q["uz"]["options"] = {new: q["uz"]["options"][old] for new, old in zip(letters, order)}
    q["correct"] = letters[order.index(q["correct"])]
    return q


# =====================================================================
# Offline backup questions (used only if Groq is completely down)
# =====================================================================
OFFLINE_BACKUP_QUESTIONS = [
    {
        "label": "🕵️ Detective Case",
        "question": "A ring vanished at 9 PM. The power was off in the whole house from 8:45 to 9:15. Ann: \"I was asleep since 8.\" Ben: \"I was watching the 9 PM news on TV.\" Carl: \"I was reading on the balcony by moonlight.\" Who is lying?",
        "options": {"A": "Ann", "B": "Ben", "C": "Carl", "D": "Nobody is lying"},
        "correct": "B",
        "explanation": "No electricity means no TV — Ben's alibi is impossible.",
        "detailed": "1) The power was off from 8:45 to 9:15. 2) Sleeping and reading by moonlight need no electricity. 3) Watching TV at 9 PM does — so Ben's story cannot be true.",
        "source": "Classic detective logic",
        "uz": {
            "question": "Soat 21:00 da uzuk g‘oyib bo‘ldi. 20:45 dan 21:15 gacha butun uyda chiroq o‘chgan edi. Ann: \"Men soat 8 dan beri uxlayotgan edim.\" Ben: \"Men televizorda soat 9 dagi yangiliklarni ko‘rayotgan edim.\" Karl: \"Men balkonda oy yorug‘ida kitob o‘qiyotgan edim.\" Kim yolg‘on gapiryapti?",
            "options": {"A": "Ann", "B": "Ben", "C": "Karl", "D": "Hech kim"},
            "explanation": "Elektr yo‘q bo‘lsa, televizor ishlamaydi — Benning alibisi imkonsiz.",
            "detailed": "1) Chiroq 20:45 dan 21:15 gacha o‘chgan. 2) Uxlash va oy yorug‘ida o‘qish uchun elektr kerak emas. 3) Soat 9 da televizor ko‘rish uchun esa kerak — demak Ben yolg‘on gapiryapti.",
        },
    },
    {
        "label": "🧩 Logic Puzzle",
        "question": "Three boxes are labelled \"Apples\", \"Oranges\" and \"Mixed\". ALL three labels are wrong. You may take out one fruit from one box. Which box should you pick from to fix every label?",
        "options": {"A": "The \"Apples\" box", "B": "The \"Oranges\" box", "C": "The \"Mixed\" box", "D": "It's impossible with one fruit"},
        "correct": "C",
        "explanation": "The \"Mixed\" box can't be mixed, so one fruit tells you exactly what it holds.",
        "detailed": "1) The \"Mixed\" label is wrong, so that box has only apples or only oranges. 2) Say you pull an apple: it's the apple box. 3) The \"Oranges\" box can't be oranges and can't be apples now, so it's mixed. 4) The last box is oranges.",
        "source": "Classic logic puzzle",
        "uz": {
            "question": "Uchta qutida \"Olma\", \"Apelsin\" va \"Aralash\" degan yozuvlar bor. Uchala yozuv ham XATO. Faqat bitta qutidan bitta meva olishingiz mumkin. Hamma yozuvni to‘g‘irlash uchun qaysi qutidan olish kerak?",
            "options": {"A": "\"Olma\" qutisidan", "B": "\"Apelsin\" qutisidan", "C": "\"Aralash\" qutisidan", "D": "Bitta meva bilan imkonsiz"},
            "explanation": "\"Aralash\" qutisi aralash bo‘lishi mumkin emas, shuning uchun bitta meva uning ichidagini aniq ko‘rsatadi.",
            "detailed": "1) \"Aralash\" yozuvi xato, demak u qutida faqat olma yoki faqat apelsin bor. 2) Masalan, olma chiqdi — bu olma qutisi. 3) \"Apelsin\" qutisi apelsin ham, olma ham bo‘la olmaydi — demak aralash. 4) Oxirgisi — apelsin.",
        },
    },
    {
        "label": "💰 Money Smarts",
        "question": "Your savings grow 8% per year. Roughly how long until your money doubles?",
        "options": {"A": "About 4 years", "B": "About 9 years", "C": "About 15 years", "D": "About 25 years"},
        "correct": "B",
        "explanation": "Rule of 72: 72 ÷ 8 = 9 years.",
        "detailed": "The Rule of 72 is a quick way to estimate doubling time: divide 72 by the yearly percentage. Many people guess 12.5 years (100 ÷ 8), but compound growth means you also earn on your earlier earnings, so it's faster.",
        "source": "Rule of 72 (finance basics)",
        "uz": {
            "question": "Jamg‘armangiz yiliga 8% o‘sadi. Pulingiz taxminan necha yilda ikki baravar ko‘payadi?",
            "options": {"A": "Taxminan 4 yil", "B": "Taxminan 9 yil", "C": "Taxminan 15 yil", "D": "Taxminan 25 yil"},
            "explanation": "72 qoidasi: 72 ÷ 8 = 9 yil.",
            "detailed": "72 qoidasi — pul necha yilda ikki baravar bo‘lishini tez hisoblash usuli: 72 ni yillik foizga bo‘ling. Ko‘pchilik 12,5 yil (100 ÷ 8) deb o‘ylaydi, lekin murakkab foiz tufayli oldingi daromadingiz ham daromad keltiradi, shuning uchun tezroq bo‘ladi.",
        },
    },
    {
        "label": "🛡️ Scam & Online Safety",
        "question": "You get an SMS: \"Your bank card is blocked. Tell us the code we just sent you.\" What's the smartest move?",
        "options": {"A": "Send the code quickly", "B": "Reply and ask who they are", "C": "Call the bank's official number yourself", "D": "Send it but change PIN later"},
        "correct": "C",
        "explanation": "Real banks never ask for your SMS code — call the number on the back of your card.",
        "detailed": "The SMS code is the key that lets someone log in or confirm a payment as you. Scammers create panic so you act fast. Replying to them only confirms your number is active; always contact the bank through its official number or app.",
        "source": "Bank security guidelines",
        "uz": {
            "question": "Sizga SMS keldi: \"Kartangiz bloklandi. Hozir yuborgan kodimizni ayting.\" Eng to‘g‘ri yo‘l qaysi?",
            "options": {"A": "Kodni tezda yuborish", "B": "Javob yozib, kimligini so‘rash", "C": "Bankning rasmiy raqamiga o‘zingiz qo‘ng‘iroq qilish", "D": "Yuborib, keyin PIN-kodni almashtirish"},
            "explanation": "Haqiqiy bank hech qachon SMS kodingizni so‘ramaydi — karta orqasidagi raqamga qo‘ng‘iroq qiling.",
            "detailed": "SMS kod — sizning nomingizdan kirish yoki to‘lovni tasdiqlash kaliti. Firibgarlar shoshiltirish uchun vahima uyg‘otadi. Ularga javob yozish raqamingiz faol ekanini tasdiqlaydi xolos; bank bilan faqat rasmiy raqam yoki ilova orqali bog‘laning.",
        },
    },
    {
        "label": "🎲 Probability & Number Traps",
        "question": "A fair coin lands heads 5 times in a row. What's the chance the next flip is heads?",
        "options": {"A": "Less than 50%", "B": "Exactly 50%", "C": "More than 50%", "D": "1 in 64"},
        "correct": "B",
        "explanation": "A coin has no memory — every flip is still 50/50.",
        "detailed": "Believing a result is 'due' after a streak is called the gambler's fallacy. 1 in 64 is the chance of six heads in a row decided BEFORE any flipping — but the first five have already happened.",
        "source": "Probability basics (gambler's fallacy)",
        "uz": {
            "question": "Tanga ketma-ket 5 marta \"gerb\" tushdi. Keyingi tashlashda gerb tushish ehtimoli qancha?",
            "options": {"A": "50% dan kam", "B": "Aynan 50%", "C": "50% dan ko‘p", "D": "64 dan 1"},
            "explanation": "Tanganing xotirasi yo‘q — har bir tashlash baribir 50/50.",
            "detailed": "Ketma-ketlikdan keyin natija \"navbati keldi\" deb o‘ylash — \"o‘yinchi xatosi\" deyiladi. 64 dan 1 — bu hali hech narsa tashlanmasdan oldin 6 marta ketma-ket gerb tushish ehtimoli; lekin birinchi beshtasi allaqachon bo‘lib o‘tgan.",
        },
    },
    {
        "label": "🕌 Islamic Knowledge",
        "question": "The word \"algorithm\" comes from the name of which Muslim scholar from Khwarazm?",
        "options": {"A": "Ibn Sina", "B": "Al-Khwarizmi", "C": "Al-Biruni", "D": "Ibn Rushd"},
        "correct": "B",
        "explanation": "Latin translators wrote Al-Khwarizmi's name as \"Algoritmi\".",
        "detailed": "Muhammad ibn Musa al-Khwarizmi worked at the House of Wisdom in Baghdad in the 9th century. His book on calculation was translated into Latin as \"Algoritmi de numero Indorum\", and his book \"al-Jabr\" gave us the word \"algebra\".",
        "source": "History of mathematics",
        "uz": {
            "question": "\"Algoritm\" so‘zi Xorazmlik qaysi musulmon olimning ismidan kelib chiqqan?",
            "options": {"A": "Ibn Sino", "B": "Al-Xorazmiy", "C": "Al-Beruniy", "D": "Ibn Rushd"},
            "explanation": "Lotin tarjimonlari Al-Xorazmiyning ismini \"Algoritmi\" deb yozishgan.",
            "detailed": "Muhammad ibn Muso al-Xorazmiy IX asrda Bag‘doddagi Baytul-Hikmada ishlagan. Uning hisob haqidagi kitobi lotinchaga \"Algoritmi de numero Indorum\" nomi bilan tarjima qilingan, \"al-Jabr\" kitobi esa bizga \"algebra\" so‘zini bergan.",
        },
    },
]


def offline_question(recent):
    """Backup questions are used at most once per HISTORY_KEEP questions — never repeated."""
    pool = [q for q in OFFLINE_BACKUP_QUESTIONS if q["question"][:200] not in recent]
    if not pool:
        return None
    q = copy.deepcopy(random.choice(pool))
    q["cat"] = "offline"
    return shuffle_options(q)


# =====================================================================
# Question stock — a background worker keeps BANK_TARGET verified
# questions ready (saved to bank.json, survives restarts).
# =====================================================================
BANK_FILE = "bank.json"
BANK_TARGET = 40
BANK_LOCK = threading.Lock()
BANK = _load_json(BANK_FILE, [])
PENDING = []  # generated but not yet verified (verification ran out of budget)
BANK_STATS = {"added": 0, "rejected": 0}


def save_bank():
    with BANK_LOCK:
        snapshot = list(BANK)
    _save_json(BANK_FILE, snapshot)


def recent_questions():
    with BANK_LOCK:
        banked = [q["question"] for q in BANK]
    return list(STATE["history"]) + banked


def refill_once() -> int:
    """Blocking. Generates + verifies one batch. Returns how many questions were added."""
    if PENDING:
        cands = PENDING[:]
        PENDING.clear()
    else:
        recent = recent_questions()
        cands = generate_batch([make_slot() for _ in range(GEN_BATCH)], recent)
        if not cands:
            return 0
        kept = []
        for q in cands:
            if too_similar(q["question"], recent + [k["question"] for k in kept]):
                logger.info(f"Dropped (too similar): {q['question'][:70]}")
                continue
            kept.append(q)
        cands = kept
        if not cands:
            return 0
    results = verify_batch(cands)
    if results is None:
        PENDING.extend(cands)  # try verifying again later instead of wasting them
        return 0
    added = 0
    for q, (ok, why) in zip(cands, results):
        if not ok:
            BANK_STATS["rejected"] += 1
            logger.info(f"Rejected [{q['cat']}] {why} | {q['question'][:80]}")
            continue
        with BANK_LOCK:
            BANK.append(shuffle_options(q))
        BANK_STATS["added"] += 1
        added += 1
        logger.info(f"Banked [{q['cat']}] {q['question'][:80]}")
    if added:
        save_bank()
    return added


async def bank_worker():
    try:
        while True:
            try:
                if len(BANK) < BANK_TARGET:
                    added = await asyncio.to_thread(refill_once)
                    await asyncio.sleep(5 if added else 60)
                else:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Bank worker error: {e}", exc_info=True)
                await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Bank worker cancelled.")


def take_question(q_num: int):
    """Every 4th question is Islamic when the stock has one."""
    want_islamic = q_num % 4 == 0
    history = list(STATE["history"])
    with BANK_LOCK:
        # drop anything that became too similar to what was posted since it was banked
        BANK[:] = [q for q in BANK if not too_similar(q["question"], history)]
        if not BANK:
            return None
        idx = next((i for i, q in enumerate(BANK) if (q.get("cat") == "islamic") == want_islamic), 0)
        q = BANK.pop(idx)
    save_bank()
    return q


# =====================================================================
# Message building (HTML)
# =====================================================================
def fmt_options(options: dict) -> str:
    return "\n".join(f"<b>{L})</b> {esc(options[L])}" for L in "ABCD")


def build_question_text(q_num: int, q: dict) -> str:
    parts = [
        f"🏆 <b>1 MILLION QUESTIONS</b> · #{q_num:07d}",
        f"{esc(q.get('label', ''))}",
        "",
        f"❓ <b>{esc(q['question'])}</b>",
        "",
        fmt_options(q["options"]),
    ]
    uz = q.get("uz")
    if uz:
        parts += [
            "",
            f"<blockquote expandable>🇺🇿 <b>UZBEK VERSION ⬇️</b>\n\n"
            f"❓ {esc(uz['question'])}\n\n{fmt_options(uz['options'])}</blockquote>",
        ]
    parts += [
        "",
        "⚡ Reply with <b>A, B, C or D</b>",
        "⏳ First 30s: fastest 3 correct get 🥇10 · 🥈5 · 🥉3",
        "🤫 Your reply is hidden — you'll appear in the list below.",
    ]
    return "\n".join(parts)


def answer_block(q: dict) -> str:
    L = q["correct"]
    s = f"✅ <b>Answer:</b> {L}) {esc(q['options'][L])}\n💡 {esc(q['explanation'])}"
    if q.get("source"):
        s += f"\n📚 <i>{esc(q['source'])}</i>"
    if q.get("detailed"):
        s += f"\n\n<blockquote expandable>📖 <b>WHY? ⬇️</b>\n\n{esc(q['detailed'])}</blockquote>"
    uz = q.get("uz")
    if uz:
        uz_body = f"✅ <b>Javob:</b> {L}) {esc(uz['options'][L])}"
        if uz.get("explanation"):
            uz_body += f"\n💡 {esc(uz['explanation'])}"
        if uz.get("detailed"):
            uz_body += f"\n\n{esc(uz['detailed'])}"
        s += f"\n<blockquote expandable>🇺🇿 <b>UZBEK VERSION ⬇️</b>\n\n{uz_body}</blockquote>"
    return s


def leaderboard_block(chat_id: int) -> str:
    return f"<blockquote expandable>📊 <b>TOP-10 ⬇️</b>\n\n{esc(format_top10(chat_id))}</blockquote>"


def seconds_until_next_drop() -> int:
    now = datetime.now(TASHKENT_TZ)
    period = QUESTION_INTERVAL_MIN * 60
    past = (now.minute % QUESTION_INTERVAL_MIN) * 60 + now.second
    wait = period - past
    return wait if wait > 0 else period


def next_drop_label() -> str:
    t = datetime.now(TASHKENT_TZ) + timedelta(seconds=seconds_until_next_drop())
    return t.strftime("%H:%M")


# =====================================================================
# Roasts
# =====================================================================
ROAST_TEMPLATES = [
    "{names} went for {letter}) — bold move, wrong universe. 😅",
    "{names}: {letter}) was the trap, and you walked right in. 🪤",
    "{names} picked {letter}). Confidence: 100%. Accuracy: 0%. 😂",
    "{names} chose {letter}) — Sherlock is quietly shaking his head. 🕵️",
]


def generate_roast(q: dict, wrong_grouped: dict) -> str:
    """Blocking. Returns HTML-safe text (or "")."""
    if not wrong_grouped:
        return ""
    correct = q["correct"]
    wrong_lines = "\n".join(
        f'- Picked {L}) "{q["options"].get(L, "")}": {", ".join(names)}' for L, names in wrong_grouped.items()
    )
    prompt = f"""You are the witty host of a quiz game in a Telegram group of friends.

QUESTION: {q['question']}
CORRECT ANSWER: {correct}) {q['options'][correct]}
WHY: {q['explanation']}

WRONG GUESSES:
{wrong_lines}

Write ONE short funny line per wrong option. Mention the players by name and, in a clever way, show WHY their pick was wrong (use the real fact or the logic of the puzzle).
Playful teasing only: nothing about looks, family, religion or nationality, no swearing. Simple English. No heading or intro. Max 2 sentences per line."""
    raw = None
    if len(BANK) >= 10:  # questions come first — roasts only use spare budget
        raw = groq_chat(
            "You are a funny, clever quiz host. Reply with plain text only.",
            prompt, models=ROAST_MODELS, json_mode=False, temperature=0.9, max_tokens=700, reasoning="low",
        )
    if raw:
        body = esc(clip(raw, 900))
    else:
        body = "\n".join(
            random.choice(ROAST_TEMPLATES).format(names=esc(", ".join(names)), letter=L)
            for L, names in wrong_grouped.items()
        )
    return f"\n\n🔥 <b>WRONG GUESSES:</b>\n{body}"


def group_wrong(answers, correct):
    grouped = {}
    for a in answers:
        if a["choice"] != correct:
            grouped.setdefault(a["choice"], []).append(display_name(a["user"]))
    return grouped


# =====================================================================
# Round state
# =====================================================================
def create_empty_round():
    return {
        "active": False,
        "q_num": 0,
        "q": None,
        "attempted_users": set(),
        "in_initial_window": False,
        "answers": [],
        "message_id": None,
        "base_text": "",
        "answered_names": [],
        "edit_pending": False,
    }


ROUND_STATES = {}


def get_round_state(chat_id: int) -> dict:
    if chat_id not in ROUND_STATES:
        ROUND_STATES[chat_id] = create_empty_round()
    return ROUND_STATES[chat_id]


# =====================================================================
# Pinned leaderboard (display + backup if the server disk is wiped)
# =====================================================================
PIN_MARKERS = ("LEADERBOARD", "YETAKCHILAR JADVALI")


def render_pin(chat_id: int) -> str:
    last_q = max(STATE["next_q"] - 1, 0)
    hist = STATE["history"][:10]
    hist_lines = "\n".join(f"• {clip(h, 90)}" for h in hist) if hist else "• Starting soon..."
    return (
        f"🏆 1 MILLION QUESTIONS · LEADERBOARD\n"
        f"Last question: #{last_q:07d}\n\n"
        f"{format_top10(chat_id)}\n\n"
        f"────────────────────\n"
        f"📜 Recent questions:\n"
        f"{hist_lines}"
    )


def parse_pin_text(text: str):
    next_q = None
    m = re.search(r"Joriy savol: #(\d+)", text)
    if m:
        next_q = int(m.group(1))
    m = re.search(r"Last question: #(\d+)", text)
    if m:
        next_q = int(m.group(1)) + 1
    scores, history, in_hist = {}, [], False
    for line in text.split("\n"):
        line = line.strip()
        if "tarixi:" in line or "Recent questions:" in line:
            in_hist = True
            continue
        if not in_hist:
            m = re.match(r"^(?:🥇|🥈|🥉|\d+\.)\s*(.+?)\s*—\s*(-?\d+)\s*(?:ball|pts)\b", line)
            if m:
                scores[m.group(1).strip()] = int(m.group(2))
        elif line.startswith("•") and "boshlanmoqda" not in line and "Starting soon" not in line:
            history.append(line.lstrip("•").strip())
    return next_q, scores, history


async def update_pin(bot, chat_id: int):
    text = render_pin(chat_id)
    cid = str(chat_id)
    pin_id = STATE["pins"].get(cid)
    if pin_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=pin_id, text=text)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            logger.warning(f"Pinned leaderboard in {chat_id} not editable ({e}); creating a new one.")
        except Exception as e:
            logger.error(f"Failed to update pin in {chat_id}: {e}")
            return
    try:
        msg = await bot.send_message(chat_id, text)
        STATE["pins"][cid] = msg.message_id
        save_state()
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except Exception as e:
            logger.warning(f"Could not pin in {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Could not create leaderboard in {chat_id}: {e}")


async def recover_from_pins(bot):
    """Runs once at startup. Restores scores only if the local file lost them."""
    changed = False
    for chat_id in GROUP_CHAT_IDS:
        cid = str(chat_id)
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as e:
            logger.warning(f"get_chat failed for {chat_id}: {e}")
            continue
        pinned = chat.pinned_message
        text = (getattr(pinned, "text", "") or "") if pinned else ""
        if not pinned or not any(mark in text for mark in PIN_MARKERS):
            continue
        if cid not in STATE["pins"]:
            STATE["pins"][cid] = pinned.message_id
            changed = True
        next_q, scores, hist = parse_pin_text(text)
        if next_q and next_q > STATE["next_q"]:
            STATE["next_q"] = next_q
            changed = True
        if not STATE["history"] and hist:
            STATE["history"] = hist
            changed = True
        chat_scores = get_chat_scores(chat_id)
        if not chat_scores and scores:
            for name, pts in scores.items():
                chat_scores[f"legacy:{name}"] = {"name": name, "score": pts}
            logger.info(f"Recovered {len(scores)} scores from the pinned message in {chat_id}.")
            save_scores()
    if changed:
        save_state()


# =====================================================================
# Round engine
# =====================================================================
POST_LOCK = asyncio.Lock()


async def close_round(bot, chat_id: int, header: str):
    """Ends an active round that nobody won, reveals the answer."""
    rs = get_round_state(chat_id)
    if not rs["active"]:
        return
    rs["active"] = False
    q = rs["q"]
    await update_pin(bot, chat_id)
    roast = await asyncio.to_thread(generate_roast, q, group_wrong(rs["answers"], q["correct"]))
    text = f"{header}\n\n{answer_block(q)}{roast}"
    try:
        sent = await bot.send_message(chat_id, text, parse_mode="HTML")
        track_for_cleanup(chat_id, sent.message_id)
    except Exception as e:
        logger.error(f"Could not send reveal in {chat_id}: {e}")
    track_for_cleanup(chat_id, rs.get("message_id"))


async def evaluate_initial_window(bot, chat_id: int, q_num: int):
    await asyncio.sleep(INITIAL_WINDOW_SECONDS)
    rs = get_round_state(chat_id)
    if not rs["active"] or rs["q_num"] != q_num:
        return
    rs["in_initial_window"] = False
    q = rs["q"]
    correct = q["correct"]
    answers = list(rs["answers"])
    right = sorted([a for a in answers if a["choice"] == correct], key=lambda a: a["ts"])

    if right:
        rs["active"] = False
        points, medals, lines = [10, 5, 3], ["🥇", "🥈", "🥉"], []
        for idx, a in enumerate(right[:3]):
            add_points(chat_id, a["user"], points[idx])
            lines.append(f"{medals[idx]} {esc(display_name(a['user']))} — +{points[idx]} pts")
        extra = len(right) - 3
        if extra > 0:
            lines.append(f"👏 {extra} more got it right, but missed the podium!")
        await update_pin(bot, chat_id)
        roast = await asyncio.to_thread(generate_roast, q, group_wrong(answers, correct))
        reply = (
            f"⏰ <b>30 SECONDS ARE UP!</b>\n\n"
            f"{answer_block(q)}\n\n"
            f"⚡ <b>WINNERS:</b>\n" + "\n".join(lines) + f"{roast}\n\n"
            f"{leaderboard_block(chat_id)}\n\n"
            f"⏳ Next question at {next_drop_label()}"
        )
        try:
            sent = await bot.send_message(chat_id, reply, parse_mode="HTML")
            track_for_cleanup(chat_id, sent.message_id)
        except Exception as e:
            logger.error(f"Could not send results in {chat_id}: {e}")
        track_for_cleanup(chat_id, rs.get("message_id"))
    else:
        # Don't reveal which letters were wrong — that would give the answer away.
        wrong_count = len(answers)
        out_line = f"❌ {wrong_count} guess{'es' if wrong_count != 1 else ''} so far — all wrong, those players are out.\n" if wrong_count else ""
        text = (
            f"⏳ <b>30 seconds passed — nobody got it yet!</b>\n{out_line}\n"
            f"🔥 <b>SUDDEN DEATH:</b> the FIRST correct answer now takes all 10 pts!"
        )
        await send_temp(bot, chat_id, text, seconds=EXPIRE_CLEANUP_SECONDS)


async def post_question(app):
    async with POST_LOCK:
        bot = app.bot
        for chat_id in GROUP_CHAT_IDS:
            rs = get_round_state(chat_id)
            if rs["active"]:
                try:
                    await close_round(bot, chat_id, f"⏰ <b>TIME'S UP!</b> (#{rs['q_num']:07d})\nNobody found the answer.")
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error closing round in {chat_id}: {e}", exc_info=True)

        q_num = STATE["next_q"]
        q = take_question(q_num) or offline_question(STATE["history"])
        if not q:
            logger.warning("Question stock is empty and all backups were used recently — skipping this round.")
            return
        if q.get("cat") == "offline":
            logger.warning("Question stock empty — using a backup question.")
        STATE["next_q"] = q_num + 1
        STATE["history"].insert(0, q["question"][:200])
        del STATE["history"][HISTORY_KEEP:]
        save_state()

        text = build_question_text(q_num, q)
        for chat_id in GROUP_CHAT_IDS:
            try:
                rs = create_empty_round()
                rs.update({"active": True, "q_num": q_num, "q": q, "in_initial_window": True, "base_text": text})
                ROUND_STATES[chat_id] = rs
                sent = await bot.send_message(chat_id, text, parse_mode="HTML")
                rs["message_id"] = sent.message_id
                asyncio.create_task(evaluate_initial_window(bot, chat_id, q_num))
                logger.info(f"Question #{q_num} posted in {chat_id}.")
            except Exception as e:
                ROUND_STATES[chat_id] = create_empty_round()
                logger.error(f"Could not post question to {chat_id}: {e}")


# =====================================================================
# Answers
# =====================================================================
def schedule_list_edit(bot, chat_id: int):
    """Edits the 'who answered' list at most once every 1.5 s (avoids Telegram flood limits)."""
    rs = get_round_state(chat_id)
    if rs["edit_pending"]:
        return
    rs["edit_pending"] = True
    asyncio.create_task(_do_list_edit(bot, chat_id, rs["q_num"]))


async def _do_list_edit(bot, chat_id: int, q_num: int):
    await asyncio.sleep(1.5)
    rs = get_round_state(chat_id)
    rs["edit_pending"] = False
    if rs["q_num"] != q_num or not rs["message_id"]:
        return
    names = rs["answered_names"]
    shown = names[-30:]
    more = f"\n…and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
    text = (
        f"{rs['base_text']}\n\n👥 <b>Answered ({len(names)}):</b>\n"
        + "\n".join(f"• {esc(n)}" for n in shown) + more
    )
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=rs["message_id"], text=text, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Could not update answer list in {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"Could not update answer list in {chat_id}: {e}")


async def finish_sudden_death(bot, chat_id: int, rs: dict, user, thread_id=None):
    try:
        add_points(chat_id, user, 10)
        await update_pin(bot, chat_id)
        reply = (
            f"🎯 <b>CORRECT!</b>\n\n"
            f"⚡ <b>Winner:</b> {esc(display_name(user))} (+10 pts)\n\n"
            f"{answer_block(rs['q'])}\n\n"
            f"{leaderboard_block(chat_id)}\n\n"
            f"⏳ Next question at {next_drop_label()}"
        )
        kwargs = {"parse_mode": "HTML"}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        sent = await bot.send_message(chat_id, reply, **kwargs)
        track_for_cleanup(chat_id, sent.message_id)
        track_for_cleanup(chat_id, rs.get("message_id"))
    except Exception as e:
        logger.error(f"Error finishing sudden death in {chat_id}: {e}", exc_info=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return

    text = msg.text.strip()
    if is_vulgar(text):
        try:
            await msg.delete()
        except Exception as e:
            logger.warning(f"Could not delete vulgar message: {e}")
        return

    if not get_round_state(chat_id)["active"]:
        return
    match = ANSWER_PATTERN.match(text)
    if not match:
        return
    user = msg.from_user
    if not user:
        return

    try:
        await msg.delete()
    except Exception as e:
        logger.warning(f"Failed to delete answer message: {e}")

    raw = match.group(1)
    choice = CYRILLIC_TO_LATIN.get(raw, raw).upper()
    targets = GROUP_CHAT_IDS if is_global_user(user) else [chat_id]

    for cid in targets:
        rs = get_round_state(cid)
        if not rs["active"] or user.id in rs["attempted_users"]:
            continue
        rs["attempted_users"].add(user.id)
        rs["answered_names"].append(display_name(user))
        rs["answers"].append({"user": user, "choice": choice, "ts": time.time()})
        schedule_list_edit(context.bot, cid)

        # Sudden death: check + claim the win with no await in between (no double winners)
        if rs["active"] and not rs["in_initial_window"] and choice == rs["q"]["correct"]:
            rs["active"] = False
            thread_id = msg.message_thread_id if cid == chat_id else None
            await finish_sudden_death(context.bot, cid, rs, user, thread_id)


# =====================================================================
# Voice transcription
# =====================================================================
def _transcribe_blocking(file_bytes: bytes) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {
        "file": ("voice.ogg", file_bytes, "audio/ogg"),
        "model": (None, "whisper-large-v3"),
        "temperature": (None, "0.0"),
    }
    res = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, timeout=60)
    return (res.json().get("text") or "").strip()


async def cmd_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    reply_msg = update.message.reply_to_message
    if not reply_msg or (not reply_msg.voice and not reply_msg.audio):
        await send_temp(context.bot, chat_id, "⚠️ <b>Usage:</b> reply to a voice message with <code>/transcribe</code>", 6)
        return

    status_msg = await update.message.reply_text("🎧 <i>Listening and transcribing...</i>", parse_mode="HTML")
    try:
        audio_obj = reply_msg.voice or reply_msg.audio
        file_info = await context.bot.get_file(audio_obj.file_id)
        file_bytes = bytes(await file_info.download_as_bytearray())
        transcript = await asyncio.to_thread(_transcribe_blocking, file_bytes)
        if transcript:
            await status_msg.edit_text(f"🗣 <b>VOICE MESSAGE TEXT:</b>\n\n<i>\"{esc(clip(transcript, 3800))}\"</i>", parse_mode="HTML")
        else:
            await status_msg.edit_text("❌ Couldn't turn this voice message into text.")
    except Exception as e:
        logger.error(f"Error transcribing voice: {e}")
        await status_msg.edit_text("⚠️ Something went wrong while transcribing.")


# =====================================================================
# Commands
# =====================================================================
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.error(f"Error verifying admin status: {e}")
        return False


async def _admin_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the command may run. Deletes the command message from non-admins."""
    if update.effective_chat.id not in GROUP_CHAT_IDS:
        return False
    if await is_admin(update, context):
        return True
    try:
        await update.message.delete()
    except Exception:
        pass
    return False


async def _delete_cmd(update: Update):
    try:
        await update.message.delete()
    except Exception:
        pass


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    await send_temp(context.bot, update.effective_chat.id, "🧪 Posting a test question in all groups...", 10)
    await post_question(context.application)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_CHAT_IDS:
        return
    await update.message.reply_text(f"🏆 1 MILLION QUESTIONS · TOP-10\n\n{format_top10(chat_id)}")


async def cmd_myrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in GROUP_CHAT_IDS or not user:
        return
    await _delete_cmd(update)
    name = display_name(user)
    board = sorted_board(chat_id)
    rank, pts = None, 0
    for idx, (key, e) in enumerate(board, start=1):
        if key == str(user.id) or (key.startswith("legacy:") and e["name"].lower() == name.lower()):
            rank, pts = idx, e["score"]
            break
    if rank:
        text = f"👤 <b>{esc(name)}</b> in this group:\n🏆 Rank: #{rank} | ⭐️ {pts} pts"
    else:
        text = f"👤 <b>{esc(name)}</b>, you have no points in this group yet!"
    await send_temp(context.bot, chat_id, text, 10)


async def cmd_set_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    await _delete_cmd(update)
    reply = update.message.reply_to_message
    if not reply or not reply.from_user or not context.args:
        await send_temp(context.bot, chat_id, "⚠️ <b>Usage:</b> reply to a player's message with <code>/score 118</code>", 6)
        return
    try:
        new_score = int(context.args[0])
    except ValueError:
        await send_temp(context.bot, chat_id, "⚠️ The score must be a number!", 6)
        return
    target = reply.from_user
    set_points(chat_id, target, new_score)
    await update_pin(context.bot, chat_id)
    await send_temp(context.bot, chat_id, f"✅ <b>{esc(display_name(target))}</b> now has <b>{new_score}</b> pts in this group.", 6)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    if not get_round_state(chat_id)["active"]:
        await send_temp(context.bot, chat_id, "ℹ️ There's no active question right now.", 6)
        return
    await close_round(context.bot, chat_id, f"⏭ <b>QUESTION SKIPPED</b> (by admin)\n⏳ Next question at {next_drop_label()} or via /test")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    ALL_SCORES[str(chat_id)] = {}
    save_scores()
    await update_pin(context.bot, chat_id)
    await update.message.reply_text("🔄 All scores in this group have been reset to zero!")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    wait = seconds_until_next_drop()
    alive = scheduler_task is not None and not scheduler_task.done()
    now = time.time()
    with RATE_LOCK:
        usage = dict(USAGE_TODAY["tokens"])
        cooling = {m: int(t - now) for m, t in MODEL_COOLDOWN.items() if t > now}
    usage_lines = "\n".join(
        f"   • {m.split('/')[-1]}: {usage.get(m, 0):,} / 200,000"
        + (f" (paused {cooling[m] // 60}m)" if m in cooling else "")
        for m in dict.fromkeys(GEN_MODELS + CHECK_MODELS)
    )
    rs = get_round_state(update.effective_chat.id)
    msg = (
        f"🤖 BOT STATUS\n\n"
        f"👥 Active groups: {len(GROUP_CHAT_IDS)}\n"
        f"🕒 Tashkent time: {datetime.now(TASHKENT_TZ).strftime('%H:%M:%S')}\n"
        f"⚙️ Scheduler: {'🟢 running' if alive else '🔴 stopped'}\n"
        f"⏳ Next question in: {wait // 60} min {wait % 60} s (#{STATE['next_q']:07d})\n"
        f"📦 Questions in stock: {len(BANK)} / {BANK_TARGET} (waiting check: {len(PENDING)})\n"
        f"✅ Made / ❌ rejected since restart: {BANK_STATS['added']} / {BANK_STATS['rejected']}\n"
        f"🔋 Groq tokens today (UTC):\n{usage_lines}\n"
        f"⚠️ Last Groq problem: {LAST_GROQ_ERROR['text'] or 'none'}\n"
        f"🎯 Active question here: {'yes' if rs['active'] else 'no'}\n"
        f"🌐 Global player by ID: {'yes' if GLOBAL_USER_IDS else 'no (name fallback)'}"
    )
    await update.message.reply_text(msg)


async def cmd_cuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global VULGAR_REGEX
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    await _delete_cmd(update)
    if not context.args:
        await send_temp(context.bot, chat_id, "⚠️ <b>Usage:</b> <code>/cuss &lt;word&gt;</code>", 5)
        return
    word = context.args[0].strip().lower()
    CUSTOM_BANNED_WORDS.add(word)
    save_custom_banned()
    VULGAR_REGEX = compile_vulgar_regex()
    await send_temp(context.bot, chat_id, f"🚫 <b>'{esc(word)}'</b> added to the blacklist.", 6)


async def cmd_uncuss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global VULGAR_REGEX
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    await _delete_cmd(update)
    if not context.args:
        await send_temp(context.bot, chat_id, "⚠️ <b>Usage:</b> <code>/uncuss &lt;word&gt;</code>", 5)
        return
    word = context.args[0].strip().lower()
    if word in CUSTOM_BANNED_WORDS:
        CUSTOM_BANNED_WORDS.remove(word)
        save_custom_banned()
        VULGAR_REGEX = compile_vulgar_regex()
        text = f"✅ <b>'{esc(word)}'</b> removed from the blacklist."
    else:
        text = f"ℹ️ <b>'{esc(word)}'</b> is not in the list."
    await send_temp(context.bot, chat_id, text, 6)


async def cmd_cusslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    await _delete_cmd(update)
    if not CUSTOM_BANNED_WORDS:
        text = "📋 No custom banned words yet."
    else:
        words = ", ".join(f"<code>{esc(w)}</code>" for w in sorted(CUSTOM_BANNED_WORDS))
        text = f"📋 <b>Custom banned words ({len(CUSTOM_BANNED_WORDS)}):</b>\n\n{words}"
    await send_temp(context.bot, chat_id, text, 10)


# =====================================================================
# Scheduler & lifecycle
# =====================================================================
scheduler_task = None
cleaner_task = None
worker_task = None


async def scheduler(app):
    try:
        while True:
            try:
                wait = seconds_until_next_drop()
                logger.info(f"Next question in {wait}s.")
                await asyncio.sleep(wait)
                await post_question(app)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scheduler error (loop protected): {e}", exc_info=True)
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        logger.info("Scheduler task cancelled.")


async def on_startup(app):
    global scheduler_task, cleaner_task, worker_task
    if not GLOBAL_USER_IDS:
        logger.warning("GLOBAL_USER_IDS is not set — global player is matched by first name (unsafe).")
    await recover_from_pins(app.bot)
    worker_task = asyncio.create_task(bank_worker())
    scheduler_task = asyncio.create_task(scheduler(app))
    cleaner_task = asyncio.create_task(message_cleaner_task(app.bot))


async def on_shutdown(app):
    logger.info("Cancelling background tasks...")
    for task in (scheduler_task, cleaner_task, worker_task):
        if task and not task.done():
            task.cancel()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Quiz bot is running 24/7 on Groq!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
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
