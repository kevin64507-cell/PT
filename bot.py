"""TranslateBot - Telegram translation bot.
Single file, Python standard library only (no pip install needed).
Required environment variable: BOT_TOKEN
Optional: DB_PATH (where to save settings), MYMEMORY_EMAIL (raises the fallback daily limit)
"""
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s"
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "translatebot.db")
MYMEMORY_EMAIL = os.environ.get("MYMEMORY_EMAIL", "").strip()
USER_AGENT = "Mozilla/5.0 (compatible; TranslateBot/1.0)"
MAX_CHARS = 3000  # longest text translated in one go

# =====================================================================
#  LANGUAGES
# =====================================================================
LANGS = [
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
    ("pt", "Portuguese"), ("it", "Italian"), ("ar", "Arabic"),
    ("zh-CN", "Chinese (Simplified)"), ("zh-TW", "Chinese (Traditional)"),
    ("hi", "Hindi"), ("bn", "Bengali"), ("ur", "Urdu"), ("ru", "Russian"),
    ("ja", "Japanese"), ("ko", "Korean"), ("tr", "Turkish"), ("nl", "Dutch"),
    ("pl", "Polish"), ("uk", "Ukrainian"), ("sv", "Swedish"), ("el", "Greek"),
    ("he", "Hebrew"), ("fa", "Persian"), ("id", "Indonesian"), ("ms", "Malay"),
    ("th", "Thai"), ("vi", "Vietnamese"), ("tl", "Filipino"), ("sw", "Swahili"),
    ("ha", "Hausa"), ("yo", "Yoruba"), ("ig", "Igbo"), ("am", "Amharic"),
    ("zu", "Zulu"), ("af", "Afrikaans"), ("ro", "Romanian"), ("cs", "Czech"),
    ("hu", "Hungarian"), ("da", "Danish"), ("fi", "Finnish"), ("no", "Norwegian"),
]
LANG_NAME = dict(LANGS)
PAGE_SIZE = 15
QUICK = ["en", "fr", "es", "ar", "de", "pt", "zh-CN", "hi", "ru", "sw"]

LOOKUP = {}
for _code, _name in LANGS:
    LOOKUP[_code.lower()] = _code
    LOOKUP[_name.lower()] = _code
    LOOKUP[_name.lower().split(" (")[0]] = _code
LOOKUP.update({
    "chinese": "zh-CN", "mandarin": "zh-CN", "zh": "zh-CN", "cn": "zh-CN",
    "farsi": "fa", "tagalog": "tl", "iw": "he", "jp": "ja", "kr": "ko", "ua": "uk", "gr": "el",
})


def find_lang(word):
    return LOOKUP.get(word.strip().lower())


def norm_code(code):
    """Normalise a code returned by the translation service."""
    c = (code or "").lower()
    if c in LOOKUP:
        return LOOKUP[c]
    base = c.split("-")[0]
    return LOOKUP.get(base, code)


def label(code):
    return LANG_NAME.get(norm_code(code), code or "Auto")


# =====================================================================
#  TRANSLATION ENGINES
# =====================================================================
def http_get_json(url, params=None, timeout=15):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hard_cut(piece, limit):
    out = []
    while len(piece.encode("utf-8")) > limit:
        k = min(len(piece), limit)
        while len(piece[:k].encode("utf-8")) > limit:
            k -= 1
        space = piece.rfind(" ", 0, k)
        if space > k // 2:
            k = space + 1
        out.append(piece[:k])
        piece = piece[k:]
    if piece:
        out.append(piece)
    return out


def split_text(text, limit):
    """Split text into chunks of at most `limit` bytes, preferring sentence ends."""
    parts = re.findall(r".*?(?:[.!?。！？]+\s*|\n+|$)", text, flags=re.S)
    chunks = []
    cur = ""
    for part in parts:
        if not part:
            continue
        pieces = _hard_cut(part, limit) if len(part.encode("utf-8")) > limit else [part]
        for p in pieces:
            if cur and len((cur + p).encode("utf-8")) > limit:
                chunks.append(cur)
                cur = ""
            cur += p
    if cur:
        chunks.append(cur)
    return chunks


def google_translate(text, target):
    data = http_get_json(
        "https://translate.googleapis.com/translate_a/single",
        {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
    )
    out = "".join(seg[0] for seg in data[0] if seg and seg[0])
    detected = data[2] if len(data) > 2 and isinstance(data[2], str) else None
    return out, detected


def mymemory_translate(text, target, source="Autodetect"):
    params = {"q": text, "langpair": "%s|%s" % (source, target)}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    data = http_get_json("https://api.mymemory.translated.net/get", params)
    if str(data.get("responseStatus")) != "200":
        raise RuntimeError(str(data.get("responseDetails") or data.get("responseStatus")))
    return html.unescape(data["responseData"]["translatedText"])


def _run_chunks(text, limit, fn):
    out = []
    detected = None
    for chunk in split_text(text, limit):
        body = chunk.rstrip()
        if not body:
            out.append(chunk)
            continue
        trail = chunk[len(body):]
        result = fn(body)
        if isinstance(result, tuple):
            result, det = result
            detected = detected or det
        out.append(result + trail)
    return "".join(out), detected


def translate(text, target):
    """Return (translated_text, detected_language_code_or_None)."""
    try:
        return _run_chunks(text, 1500, lambda t: google_translate(t, target))
    except Exception as e:
        logging.warning("Google translate failed (%s), trying MyMemory", e)
    return _run_chunks(text, 450, lambda t: mymemory_translate(t, target))


# =====================================================================
#  TELEGRAM API
# =====================================================================
def api(method, **params):
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (TOKEN, method),
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except urllib.error.HTTPError as e:
        logging.error("Telegram error %s on %s: %s", e.code, method, e.read().decode("utf-8", "ignore"))
        return None


def btn(text, data):
    return {"text": text, "callback_data": data}


def markup(rows):
    return {"inline_keyboard": rows}


def send(chat_id, text, reply_markup=None, reply_to=None):
    params = {"chat_id": chat_id, "text": text[:4096]}
    if reply_markup:
        params["reply_markup"] = reply_markup
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return api("sendMessage", **params)


# =====================================================================
#  STORAGE (settings + history)
# =====================================================================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = db()
    try:
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS prefs (user_id INTEGER PRIMARY KEY, lang TEXT)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER, src TEXT, tgt TEXT, original TEXT, translated TEXT)"
            )
    finally:
        con.close()


def get_lang(uid):
    con = db()
    try:
        row = con.execute("SELECT lang FROM prefs WHERE user_id=?", (uid,)).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] in LANG_NAME else "en"


def set_lang(uid, code):
    con = db()
    try:
        with con:
            con.execute("INSERT OR REPLACE INTO prefs (user_id, lang) VALUES (?, ?)", (uid, code))
    finally:
        con.close()


def add_history(uid, src, tgt, original, translated):
    con = db()
    try:
        with con:
            con.execute(
                "INSERT INTO history (user_id, src, tgt, original, translated) VALUES (?, ?, ?, ?, ?)",
                (uid, src or "", tgt, original[:500], translated[:500]),
            )
            con.execute(
                "DELETE FROM history WHERE user_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10)",
                (uid, uid),
            )
    finally:
        con.close()


def get_history(uid, limit=5):
    con = db()
    try:
        return con.execute(
            "SELECT src, tgt, original, translated FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    finally:
        con.close()


# remember the original text behind each result message so buttons can re-translate it
TEXTS = OrderedDict()
TEXTS_LOCK = threading.Lock()


def remember(chat_id, message_id, text):
    with TEXTS_LOCK:
        TEXTS[(chat_id, message_id)] = text
        while len(TEXTS) > 1000:
            TEXTS.popitem(last=False)


def recall(chat_id, message_id):
    with TEXTS_LOCK:
        return TEXTS.get((chat_id, message_id))


# =====================================================================
#  TRANSLATING + KEYBOARDS
# =====================================================================
def result_markup(target, detected):
    skip = {target, norm_code(detected) if detected else None}
    codes = [c for c in QUICK if c not in skip][:6]
    rows = []
    for i in (0, 3):
        row = [btn(LANG_NAME[c].split(" (")[0], "rt:" + c) for c in codes[i:i + 3]]
        if row:
            rows.append(row)
    rows.append([btn("🌐 More languages", "mp")])
    return markup(rows)


def picker_markup(mode, page, current=None):
    pages = (len(LANGS) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    items = LANGS[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = []
    for i in range(0, len(items), 3):
        rows.append([
            btn(("✅ " if code == current else "") + name.split(" (")[0] + (" (TW)" if code == "zh-TW" else ""),
                "pk:%s:%s" % (mode, code))
            for code, name in items[i:i + 3]
        ])
    nav = []
    if page > 0:
        nav.append(btn("◀️ Prev", "pg:%s:%d" % (mode, page - 1)))
    nav.append(btn("%d/%d" % (page + 1, pages), "noop"))
    if page < pages - 1:
        nav.append(btn("Next ▶️", "pg:%s:%d" % (mode, page + 1)))
    rows.append(nav)
    return markup(rows)


def do_translate(chat_id, uid, text, target, reply_to=None):
    text = text.strip()
    if not text:
        return
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]
    api("sendChatAction", chat_id=chat_id, action="typing")
    try:
        out, detected = translate(text, target)
    except Exception as e:
        logging.error("Translation failed: %s", e)
        send(chat_id, "⚠️ The translation service is busy right now. Please try again in a moment.", reply_to=reply_to)
        return
    src_label = label(detected) if detected else "Auto-detected"
    body = "🌍 %s → %s\n\n%s" % (src_label, LANG_NAME[target], out)
    if detected and norm_code(detected) == target:
        body += "\n\nℹ️ This text already looks like %s. Use /lang to change your language." % LANG_NAME[target]
    if truncated:
        body += "\n\n✂️ Only the first %d characters were translated." % MAX_CHARS
    msg = send(chat_id, body, result_markup(target, detected), reply_to)
    if msg and "message_id" in msg:
        remember(chat_id, msg["message_id"], text)
    add_history(uid, norm_code(detected) if detected else "", target, text, out)


# =====================================================================
#  COMMANDS
# =====================================================================
HELP_TEXT = (
    "🌍 TranslateBot - instant translation\n\n"
    "Just send me any text and I'll detect the language and translate it into your language.\n\n"
    "Commands:\n"
    "/lang - choose your language (or /lang french)\n"
    "/tr <language> <text> - translate into a specific language, e.g. /tr es Good morning\n"
    "/detect <text> - find out what language a text is in\n"
    "/languages - list all supported languages\n"
    "/history - your recent translations\n"
    "/help - show this message\n\n"
    "Tip: reply to any message with /tr fr to translate it. In groups, use /tr."
)


def reply_text_of(msg):
    r = msg.get("reply_to_message") or {}
    return r.get("text") or r.get("caption") or ""


def cmd_start(msg, arg):
    uid = msg["from"]["id"]
    send(msg["chat"]["id"],
         "👋 Welcome!\n\n" + HELP_TEXT + "\n\nYour language is currently: %s" % LANG_NAME[get_lang(uid)])


def cmd_lang(msg, arg):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    if arg:
        code = find_lang(arg)
        if not code:
            send(chat_id, "I don't know the language '%s'. Send /languages to see the list." % arg[:30])
            return
        set_lang(uid, code)
        send(chat_id, "✅ Your language is now %s. Messages you send will be translated into it." % LANG_NAME[code])
        return
    current = get_lang(uid)
    send(chat_id, "🌐 Pick your language (now: %s):" % LANG_NAME[current], picker_markup("set", 0, current))


def cmd_tr(msg, arg):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    private = msg["chat"].get("type") == "private"
    reply_to = None if private else msg.get("message_id")
    tokens = arg.split(None, 1)
    replied = reply_text_of(msg)
    if not tokens and not replied:
        send(chat_id, "Usage: /tr <language> <text>\nExample: /tr fr Good morning\n\n"
                      "Or reply to a message with /tr fr", reply_to=reply_to)
        return
    target = find_lang(tokens[0]) if tokens else None
    if target:
        text = tokens[1] if len(tokens) > 1 else replied
    else:
        if len(tokens) > 1:
            send(chat_id, "I don't know the language '%s'. Send /languages to see the list." % tokens[0][:30],
                 reply_to=reply_to)
            return
        target = get_lang(uid)
        text = tokens[0] if tokens else replied
    if not text:
        send(chat_id, "Please give me some text to translate, or reply to a message.", reply_to=reply_to)
        return
    do_translate(chat_id, uid, text, target, reply_to)


def cmd_detect(msg, arg):
    chat_id = msg["chat"]["id"]
    text = arg or reply_text_of(msg)
    if not text:
        send(chat_id, "Usage: /detect <text>  (or reply to a message with /detect)")
        return
    try:
        _, detected = translate(text[:500], "en")
    except Exception:
        detected = None
    if detected:
        send(chat_id, "🔎 That looks like %s (%s)." % (label(detected), detected))
    else:
        send(chat_id, "I couldn't detect the language right now. Try again in a moment.")


def cmd_languages(msg, arg):
    lines = ["%s - %s" % (name, code) for code, name in LANGS]
    send(msg["chat"]["id"], "🌐 Supported languages (%d)\n\n%s\n\nUse a name or code, e.g. /tr yoruba Hello"
         % (len(LANGS), "\n".join(lines)))


def cmd_history(msg, arg):
    rows = get_history(msg["from"]["id"], 5)
    if not rows:
        send(msg["chat"]["id"], "No translations yet. Send me some text!")
        return
    blocks = []
    for src, tgt, original, translated in rows:
        blocks.append("%s → %s\n%s\n%s" % (
            label(src) if src else "Auto", LANG_NAME.get(tgt, tgt),
            original[:80] + ("…" if len(original) > 80 else ""),
            translated[:80] + ("…" if len(translated) > 80 else "")))
    send(msg["chat"]["id"], "🕘 Your recent translations\n\n" + "\n\n".join(blocks))


COMMANDS = {
    "start": cmd_start, "help": cmd_start, "lang": cmd_lang, "setlang": cmd_lang,
    "tr": cmd_tr, "translate": cmd_tr, "to": cmd_tr, "detect": cmd_detect,
    "languages": cmd_languages, "history": cmd_history,
}

BOT_COMMANDS = [
    ("tr", "Translate: /tr fr Hello"),
    ("lang", "Choose your language"),
    ("detect", "Detect a text's language"),
    ("languages", "List supported languages"),
    ("history", "Your recent translations"),
    ("help", "How to use the bot"),
]

DESCRIPTION = (
    "🌍 Translate text instantly between 40+ languages!\n\n"
    "Send any message and I'll detect the language and translate it. "
    "Use /tr fr Hello for quick translations, /lang to choose your language, "
    "or reply to any message with /tr. Fast, free and simple. ⚡️"
)
SHORT_DESCRIPTION = "Instant translation between 40+ languages. Just send a message! 🌍⚡️"


# =====================================================================
#  UPDATE HANDLERS
# =====================================================================
def on_message(msg):
    if "from" not in msg or msg["from"].get("is_bot"):
        return
    chat = msg["chat"]
    chat_id = chat["id"]
    uid = msg["from"]["id"]
    private = chat.get("type") == "private"
    text = (msg.get("text") or "").strip()

    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler = COMMANDS.get(cmd)
        if handler:
            handler(msg, arg)
        return
    if not private:
        return  # in groups only commands are handled
    if not text:
        send(chat_id, "I can translate text messages. Send me some text! (Photos and voice notes aren't supported.)")
        return
    do_translate(chat_id, uid, text, get_lang(uid))


def on_callback(cb):
    api("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    message = cb.get("message")
    if not message or data == "noop":
        return
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    uid = cb["from"]["id"]
    parts = data.split(":", 2)
    kind = parts[0]

    if kind == "rt" and len(parts) == 2 and parts[1] in LANG_NAME:
        text = recall(chat_id, message_id)
        if text is None:
            send(chat_id, "That text is no longer saved. Please send it again.")
        else:
            do_translate(chat_id, uid, text, parts[1])
    elif kind == "mp":
        text = recall(chat_id, message_id)
        if text is None:
            send(chat_id, "That text is no longer saved. Please send it again.")
            return
        m = send(chat_id, "🌐 Translate this text into:", picker_markup("re", 0))
        if m and "message_id" in m:
            remember(chat_id, m["message_id"], text)
    elif kind == "pg" and len(parts) == 3 and parts[1] in ("set", "re"):
        current = get_lang(uid) if parts[1] == "set" else None
        api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup=picker_markup(parts[1], int(parts[2]), current))
    elif kind == "pk" and len(parts) == 3 and parts[2] in LANG_NAME:
        code = parts[2]
        if parts[1] == "set":
            set_lang(uid, code)
            api("editMessageText", chat_id=chat_id, message_id=message_id,
                text="✅ Your language is now %s.\nSend me any text and I'll translate it for you." % LANG_NAME[code])
        elif parts[1] == "re":
            text = recall(chat_id, message_id)
            if text is None:
                send(chat_id, "That text is no longer saved. Please send it again.")
            else:
                do_translate(chat_id, uid, text, code)


USER_LOCKS = defaultdict(threading.Lock)


def handle_update(update):
    try:
        if "message" in update:
            uid = update["message"].get("from", {}).get("id", 0)
            with USER_LOCKS[uid]:
                on_message(update["message"])
        elif "callback_query" in update:
            uid = update["callback_query"]["from"]["id"]
            with USER_LOCKS[uid]:
                on_callback(update["callback_query"])
    except Exception:
        logging.exception("Error while handling update")


def main():
    logging.info("TranslateBot starting (standard library only, no pip needed)")
    if not TOKEN:
        sys.exit("Set the BOT_TOKEN environment variable first.")
    init_db()
    api("deleteWebhook")
    me = api("getMe")
    if me:
        logging.info("Logged in as @%s", me.get("username"))
    else:
        logging.error("Could not log in. Check that BOT_TOKEN is correct.")
    api("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
    api("setMyDescription", description=DESCRIPTION)
    api("setMyShortDescription", short_description=SHORT_DESCRIPTION)
    logging.info("TranslateBot is running...")

    pool = ThreadPoolExecutor(max_workers=8)
    offset = None
    while True:
        params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = api("getUpdates", **params)
        except Exception as e:
            logging.error("Network error: %s", e)
            time.sleep(5)
            continue
        if updates is None:
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            pool.submit(handle_update, update)


if __name__ == "__main__":
    main()
