import os
import sqlite3
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from dotenv import load_dotenv
import logging
from pathlib import Path
import re
import calendar
import time
import threading
from flask import Flask

# ===== Config & Logging =====

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent"
TIMEZONE = pytz.timezone("Asia/Singapore")

# ===== Render Port Workaround =====
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "aidriel is online! 🚀"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port)

# ===== Database =====
DB_PATH = "tasks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, description TEXT, details TEXT, deadline TEXT, priority INTEGER, status TEXT DEFAULT 'pending', recurrence TEXT DEFAULT NULL, reminded INTEGER DEFAULT 0, tag TEXT DEFAULT 'General', postpone_count INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (user_id INTEGER PRIMARY KEY, reminder_offset INTEGER DEFAULT 30)""")
    conn.commit(); conn.close()

def get_user_offset(user_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT reminder_offset FROM settings WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return row[0] if row else 30

def set_user_offset(user_id, offset):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (user_id, reminder_offset) VALUES (?, ?)", (user_id, offset))
    conn.commit(); conn.close()

def get_task_by_id(db_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT description FROM tasks WHERE id=?", (db_id,))
    row = c.fetchone(); conn.close()
    return row[0] if row else "Unknown Task"

def add_task(user_id, description, details, deadline, priority, recurrence=None, tag="General"):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, description, details, deadline, priority, recurrence, reminded, tag, postpone_count) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0)", (user_id, description, details, deadline, priority, recurrence, tag))
    conn.commit(); conn.close()

def update_task(task_id, description, details, deadline, priority, recurrence, tag=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT deadline FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone(); old_deadline = row[0] if row else None
    postpone_inc = 1 if old_deadline and deadline > old_deadline else 0
    if tag: c.execute("UPDATE tasks SET description=?, details=?, deadline=?, priority=?, recurrence=?, reminded=0, tag=?, postpone_count=postpone_count+? WHERE id=?", (description, details, deadline, priority, recurrence, tag, postpone_inc, task_id))
    else: c.execute("UPDATE tasks SET description=?, details=?, deadline=?, priority=?, recurrence=?, reminded=0, postpone_count=postpone_count+? WHERE id=?", (description, details, deadline, priority, recurrence, postpone_inc, task_id))
    conn.commit(); conn.close()

def delete_task(task_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit(); conn.close()

def get_pending_tasks(user_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id, description, details, deadline, priority, recurrence, tag, postpone_count FROM tasks WHERE user_id=? AND status='pending'", (user_id,))
    tasks = c.fetchall(); conn.close()
    return tasks

def mark_done(task_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit(); conn.close()

# ===== AI Integration =====

def call_gemini(prompt, retries=3):
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    for attempt in range(retries):
        try:
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 429: time.sleep((attempt + 1) * 5); continue
            if not response.ok: raise Exception(f"API Error: {response.text}")
            result = response.json(); text_response = result['candidates'][0]['content']['parts'][0]['text']
            json_match = re.search(r'```json\s*(.*?)\s*```', text_response, re.DOTALL)
            if json_match: text_response = json_match.group(1)
            elif '```' in text_response: text_response = re.search(r'```\s*(.*?)\s*```', text_response, re.DOTALL).group(1)
            import json
            return json.loads(text_response.strip())
        except Exception as e:
            if attempt == retries - 1: raise e
            time.sleep(2)

def parse_task_with_gemini(text):
    try:
        now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M %A")
        prompt = f"""Today is {now}. Warm assistant aidriel. 
        Extract task info from: '{text}'. 
        CRITICAL: Do NOT repeat or summarize the task details in your 'friendly_confirm'. 
        Give a short, natural acknowledgement ONLY (e.g., "Alright!", "Got it!", "On it!").
        Output JSON: {{ 'task': '...', 'deadline': 'YYYY-MM-DD HH:MM', 'priority': 1-5, 'recurrence': '...', 'tag': 'Work|Personal|Errand|Home|General', 'friendly_confirm': 'short acknowledgement only' }}"""
        return call_gemini(prompt)
    except: return {"task": text, "deadline": (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d 09:00"), "priority": 3, "tag": "General", "friendly_confirm": "Got it!"}

def summarize_task_with_gemini(task):
    task_id, desc, details, deadline, priority, recurrence, tag, p_count = task
    p_emoji = ["", "🔴", "🟠", "🟡", "🟢", "⚪️"][priority] if 1 <= priority <= 5 else "🟡"
    shadow_msg = " 🫣" if p_count > 0 else ""
    return f"{p_emoji} `[{tag}]` *{desc}*{shadow_msg} \n   📅 `{deadline}`"

def reorder_tasks_with_gemini(tasks):
    base_sorted = sorted(tasks, key=lambda x: (x[4], x[3]))
    try:
        task_text = "\n".join([f"- {t[0]}: {t[1]} (Tag: {t[6]}, Due: {t[3]}, Priority: {t[4]}, Moved: {t[7]})" for t in tasks])
        prompt = f"Reorder these task IDs by Priority then Deadline. Output JSON array of IDs. \n{task_text}"
        ordered_ids = call_gemini(prompt)
        if isinstance(ordered_ids, dict): ordered_ids = next(iter(ordered_ids.values()))
        id_map = {t[0]: t for t in tasks}; result = [id_map[int(i)] for i in ordered_ids if int(i) in id_map]
        seen = set([t[0] for t in result])
        for t in base_sorted:
            if t[0] not in seen: result.append(t)
        return result
    except: return base_sorted

def handle_smart_input_with_gemini(user_text, tasks, user_offset):
    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M %A")
    task_context = "\n".join([f"ID {t[0]}: {t[1]} (Tag: {t[6]}, Due: {t[3]}, Moved: {t[7]})" for t in tasks])
    prompt = f"""Today is {now}. Tasks: {task_context}. Assistant aidriel. Warm vibe.
    User says: "{user_text}". 
    
    CRITICAL INSTRUCTIONS:
    1. If listing or referring to tasks, SUMMARIZE them into 5-7 words each. 
    2. NEVER copy-paste the user's original long task descriptions in your 'answer' field.
    3. If adding a new task, keep the 'answer' as a short, friendly acknowledgement only.
    
    Output JSON: {{ "type": "query|task|suggestion|delete|edit", "target_db_id": int, "answer": "your concise, summarized conversational response", "task_info": {{ ... }} }}"""
    return call_gemini(prompt)

# ===== UI Helpers =====
def format_priority(p): return ["", "High", "Medium", "Normal", "Low", "Very Low"][p] if 1 <= p <= 5 else "Normal"
def format_task_msg(description, deadline, priority, tag, custom_confirm=None):
    confirm = custom_confirm or "I've added that to your list!"
    tag_str = f"🏷 `{tag}` • " if tag and tag != "General" else ""
    return f"✨ {confirm}\n\n📝 *{description}*\n{tag_str}📅 `{deadline}`"

# ===== Background Jobs =====

async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM tasks"); users = c.fetchall()
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    for (user_id,) in users:
        tasks = get_pending_tasks(user_id)
        tasks_today = [t for t in tasks if t[3].startswith(today_str) or t[3] < today_str]
        if not tasks_today: continue
        task_text = "\n".join([f"- {t[1]} (Tag: {t[6]}, Due: {t[3]})" for t in tasks_today])
        prompt = f"Provide a warm energetic morning briefing for these tasks:\n{task_text}"
        try: res = call_gemini(prompt); briefing = res.get("answer") if isinstance(res, dict) else str(res)
        except: briefing = "Good morning! Let's tackle today together."
        msg = f"🌅 *Morning Briefing*\n\n{briefing}\n\n📋 *Your Day:*\n\n"
        keyboard = []
        for idx, t in enumerate(tasks_today, 1):
            msg += f"*{idx}.* {summarize_task_with_gemini(t)}\n"
            keyboard.append([InlineKeyboardButton(f"✅ Done #{idx}", callback_data=f"done_{t[0]}")])
        try: await context.bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass
    conn.close()

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id, user_id, description, deadline FROM tasks WHERE status='pending' AND reminded=0")
    all_pending = c.fetchall(); now = datetime.now(TIMEZONE)
    for tid, uid, desc, dl in all_pending:
        try:
            offset = get_user_offset(uid); deadline_dt = datetime.strptime(dl, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
            if now >= (deadline_dt - timedelta(minutes=offset)):
                await context.bot.send_message(uid, f"⏰ *Just a heads up!* \n'{desc}' is due soon.", parse_mode="Markdown")
                c.execute("UPDATE tasks SET reminded=1 WHERE id=?", (tid,))
        except: pass
    conn.commit(); conn.close()

def create_recurring_tasks():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT user_id, description, details, deadline, priority, recurrence, tag FROM tasks WHERE recurrence IS NOT NULL AND status='done'")
    for uid, desc, det, dl, pri, rec, tag in c.fetchall():
        try:
            nxt = datetime.strptime(dl, "%Y-%m-%d %H:%M")
            if rec == "daily": nxt += timedelta(days=1)
            elif rec == "weekly": nxt += timedelta(weeks=1)
            elif rec == "monthly":
                m = nxt.month + 1 if nxt.month < 12 else 1
                y = nxt.year + (1 if nxt.month == 12 else 0)
                nxt = nxt.replace(year=y, month=m)
            add_task(uid, desc, det, nxt.strftime("%Y-%m-%d %H:%M"), pri, rec, tag)
            c.execute("UPDATE tasks SET recurrence=NULL WHERE deadline=? AND description=? AND status='done'", (dl, desc))
        except: pass
    conn.commit(); conn.close()

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE); greeting = "Good morning" if 5 <= now.hour < 12 else "Good afternoon" if 12 <= now.hour < 18 else "Good evening"
    await update.message.reply_text(f"👋 *{greeting}! I'm aidriel.* Talk to me naturally or use /list!", parse_mode="Markdown")

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        offset = get_user_offset(update.message.from_user.id)
        return await update.message.reply_text(f"I'll remind you **{offset} minutes** early. Change it: `/settings [mins]`", parse_mode="Markdown")
    try:
        new_offset = int(context.args[0]); set_user_offset(update.message.from_user.id, new_offset)
        await update.message.reply_text(f"Got it! I'll now ping you **{new_offset} minutes** early.", parse_mode="Markdown")
    except: await update.message.reply_text("Usage: `/settings 60`")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text: return await update.message.reply_text("What should I add?")
    parsed = parse_task_with_gemini(text); add_task(update.message.from_user.id, parsed.get("task") or text, "", parsed.get("deadline"), parsed.get("priority") or 3, parsed.get("recurrence"), parsed.get("tag") or "General")
    await update.message.reply_text(format_task_msg(parsed.get("task") or text, parsed.get("deadline"), parsed.get("priority") or 3, parsed.get("tag") or "General", parsed.get("friendly_confirm")), parse_mode="Markdown")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id; tasks = get_pending_tasks(user_id)
    if not tasks: return await update.message.reply_text("🎉 *All caught up!*")
    tasks_ordered = reorder_tasks_with_gemini(tasks); msg = "📋 *Your Tasks:*\n\n"; keyboard = []; id_map = {}
    for idx, t in enumerate(tasks_ordered, 1):
        id_map[idx] = t[0]; msg += f"*{idx}.* {summarize_task_with_gemini(t)}\n\n"
        keyboard.append([InlineKeyboardButton("✅ Done", callback_data=f"done_{t[0]}"), InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{t[0]}"), InlineKeyboardButton("🗑 Del", callback_data=f"del_{t[0]}")])
    context.user_data['id_map'] = id_map; await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Which task? (e.g. /delete 1)")
    try:
        display_id = int(context.args[0]); id_map = context.user_data.get('id_map', {}); db_id = id_map.get(display_id, display_id)
        desc = get_task_by_id(db_id); delete_task(db_id)
        await update.message.reply_text(f"Done! I've removed '{desc}' from your list.")
    except: await update.message.reply_text("I couldn't find that task.")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return await update.message.reply_text("Usage: /edit 1 [new info]")
    try:
        display_id = int(context.args[0]); new_text = " ".join(context.args[1:]); id_map = context.user_data.get('id_map', {}); db_id = id_map.get(display_id, display_id)
        parsed = parse_task_with_gemini(new_text); update_task(db_id, parsed.get("task") or new_text, "", parsed.get("deadline"), parsed.get("priority"), parsed.get("recurrence"), parsed.get("tag"))
        await update.message.reply_text(f"Got it! I've updated '{parsed.get('task') or new_text}'.")
    except: await update.message.reply_text("I couldn't update that task.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text, user_id = update.message.text, update.message.from_user.id
    tasks = get_pending_tasks(user_id); offset = get_user_offset(user_id)
    try:
        res = handle_smart_input_with_gemini(user_text, tasks, offset)
        m_type = res.get("type")
        if m_type == "query": await update.message.reply_text(f"🤖 {res.get('answer')}", parse_mode="Markdown")
        elif m_type == "suggestion": await update.message.reply_text(f"💡 {res.get('answer')}", parse_mode="Markdown")
        elif m_type in ["delete", "edit"] and res.get("target_db_id"):
            tid = res["target_db_id"]
            if m_type == "delete": delete_task(tid)
            else: i = res.get("task_info", {}); update_task(tid, i.get("task"), "", i.get("deadline"), i.get("priority"), i.get("recurrence"), i.get("tag"))
            await update.message.reply_text(f"✨ {res.get('answer')}", parse_mode="Markdown")
        elif m_type == "task":
            i = res.get("task_info", {})
            add_task(user_id, i.get("task") or user_text, "", i.get("deadline"), i.get("priority") or 3, i.get("recurrence"), i.get("tag") or "General")
            tag_str = f"🏷 `{i.get('tag')}` • " if i.get('tag') and i.get('tag') != "General" else ""
            await update.message.reply_text(f"✨ {res.get('answer')}\n\n📝 *{i.get('task') or user_text}*\n{tag_str}📅 `{i.get('deadline')}`", parse_mode="Markdown")
    except: await update.message.reply_text("Added to your list!")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); db_id = int(query.data.split("_")[1])
    if query.data.startswith("done_"): mark_done(db_id); await query.edit_message_text("✅ Great job!")
    elif query.data.startswith("del_"): delete_task(db_id); await query.edit_message_text("🗑 Removed.")
    elif query.data.startswith("edit_"): await query.message.reply_text(f"Send me: `/edit ID [new info]`")

async def post_init(application):
    await application.bot.set_my_commands([BotCommand("start", "Help"), BotCommand("add", "Add task"), BotCommand("list", "Show tasks"), BotCommand("edit", "Edit task"), BotCommand("delete", "Delete task"), BotCommand("settings", "Timing")])

if __name__ == '__main__':
    time.sleep(10); threading.Thread(target=run_flask, daemon=True).start()
    init_db(); app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    app.job_queue.run_daily(daily_digest, time=datetime.strptime("09:00", "%H:%M").time())
    scheduler = BackgroundScheduler(timezone=TIMEZONE); scheduler.add_job(create_recurring_tasks, 'cron', hour=0, minute=0); scheduler.start()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("add", add)); app.add_handler(CommandHandler("list", list_tasks)); app.add_handler(CommandHandler("tasks", list_tasks)); app.add_handler(CommandHandler("edit", edit)); app.add_handler(CommandHandler("delete", delete)); app.add_handler(CommandHandler("settings", settings)); app.add_handler(CallbackQueryHandler(button_handler)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)); app.run_polling(drop_pending_updates=True)
