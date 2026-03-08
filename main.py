import os
import sqlite3
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from dotenv import load_dotenv
import logging
from pathlib import Path
import re
import calendar


# ===== Config =====

# load .env
load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = "https://api.gemini.com/v1/ai"
TIMEZONE = pytz.timezone("Asia/Singapore")  # Singapore time

# ===== Database =====
DB_PATH = "tasks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            description TEXT,
            details TEXT,
            deadline TEXT,
            priority INTEGER,
            status TEXT DEFAULT 'pending',
            recurrence TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

def add_task(user_id, description, details, deadline, priority, recurrence=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO tasks (user_id, description, details, deadline, priority, recurrence)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, description, details, deadline, priority, recurrence))
    conn.commit()
    conn.close()

def get_pending_tasks(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, description, details, deadline, priority, recurrence
        FROM tasks
        WHERE user_id=? AND status='pending'
    """, (user_id,))
    tasks = c.fetchall()
    conn.close()
    return tasks

def mark_done(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

# ===== Gemini Integration =====
def parse_task_with_gemini(text):
    """Parse task into structured info via Gemini. Fallbacks included."""
    prompt = f"""
    Extract task info from this text: "{text}"
    Output JSON with keys: task, details, deadline (YYYY-MM-DD), priority (1-5), recurrence (daily/weekly/monthly/null)
    """
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"} if GEMINI_API_KEY else {}
    try:
        response = requests.post(GEMINI_URL, json={"prompt": prompt}, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None

def summarize_task_with_gemini(task):
    """Return a concise summary string for digests and /list."""
    task_id, desc, details, deadline, priority, recurrence = task
    prompt = f"""
    Summarize this task into a single concise sentence including key details:
    Task: {desc}
    Details: {details or '-'}
    Deadline: {deadline}
    Priority: {priority}
    Recurrence: {recurrence or 'none'}
    """
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"} if GEMINI_API_KEY else {}
    try:
        response = requests.post(GEMINI_URL, json={"prompt": prompt}, headers=headers, timeout=10)
        response.raise_for_status()
        summary = response.json().get("summary")
        return summary or f"{desc} (Due: {deadline}, Priority: {priority}, Recurrence: {recurrence or 'none'})"
    except Exception:
        return f"{desc} (Due: {deadline}, Priority: {priority}, Recurrence: {recurrence or 'none'})"

def reorder_tasks_with_gemini(tasks):
    """Return tasks ordered by urgency/importance using Gemini. Fallback to deadline + priority."""
    task_text = "\n".join([f"- {t[0]}: {t[1]} (Due: {t[3]}, Priority: {t[4]})" for t in tasks])
    prompt = f"""
    Reorder the following tasks by urgency and importance. Output a JSON array of task ids in order:
    {task_text}
    """
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"} if GEMINI_API_KEY else {}
    try:
        response = requests.post(GEMINI_URL, json={"prompt": prompt}, headers=headers, timeout=10)
        response.raise_for_status()
        ordered_ids = response.json()
        id_map = {t[0]: t for t in tasks}
        return [id_map[i] for i in ordered_ids if i in id_map]
    except Exception:
        return sorted(tasks, key=lambda x: (x[3], -x[4]))

def detect_recurrence(text: str):
    text_lower = text.lower()

    # Daily patterns
    if any(word in text_lower for word in ["daily", "every day", "every night", "every morning", "every evening"]):
        return "daily"

    # Weekly patterns (only explicit repeating phrases)
    if any(phrase in text_lower for phrase in ["every monday","every tuesday","every wednesday","every thursday",
                                               "every friday","every saturday","every sunday","every week","weekly","every weekday"]):
        return "weekly"

    # Monthly patterns
    if "month" in text_lower or re.search(r"\b([1-9]|[12][0-9]|3[01])(?:st|nd|rd|th)?\b.*every month", text_lower):
        return "monthly"

    return None

def get_next_month_deadline(current_deadline: datetime):
    """Return safe next month deadline."""
    year = current_deadline.year
    month = current_deadline.month + 1
    if month > 12:
        month = 1
        year += 1
    day = min(current_deadline.day, calendar.monthrange(year, month)[1])
    return current_deadline.replace(year=year, month=month, day=day)

# ===== Telegram Bot Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Add tasks with /add followed by your description.\n"
        "Example: /add Revise ABC every night, high priority"
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Please provide a task description.")
        return

    # --- Parse task with Gemini ---
    parsed = parse_task_with_gemini(text) or {}
    description = parsed.get("task") or text
    details = parsed.get("details") or ""
    deadline = parsed.get("deadline") or (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
    priority = parsed.get("priority") or 3
    recurrence = parsed.get("recurrence")

    # --- Smart fallback recurrence detection ---
    if not recurrence:
        text_lower = text.lower()
        # Daily
        if any(word in text_lower for word in ["daily", "every day", "every night", "every morning", "every evening"]):
            recurrence = "daily"
        # Weekly — only explicit repeating phrases
        elif any(phrase in text_lower for phrase in ["every monday","every tuesday","every wednesday","every thursday",
                                                     "every friday","every saturday","every sunday","every week",
                                                     "weekly","every weekday"]):
            recurrence = "weekly"
        # Monthly — only explicit repeating patterns
        elif "month" in text_lower or re.search(r"\b([1-9]|[12][0-9]|3[01])(?:st|nd|rd|th)?\b.*every month", text_lower):
            recurrence = "monthly"
        else:
            recurrence = None  # one-time task

    # --- Add task to DB ---
    add_task(update.message.from_user.id, description, details, deadline, priority, recurrence)

    # --- Feedback to user ---
    await update.message.reply_text(
        f"Task '{description}' added!\nDeadline: {deadline}, "
        f"Priority: {priority}, Recurrence: {recurrence or 'none'}"
    )

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_pending_tasks(update.message.from_user.id)
    if not tasks:
        await update.message.reply_text("You have no pending tasks! 🎉")
        return

    tasks_ordered = reorder_tasks_with_gemini(tasks)

    msg = "📋 *Your Pending Tasks (Ordered by Urgency):*\n\n"
    keyboard = []

    for t in tasks_ordered:
        task_id = t[0]
        summary = summarize_task_with_gemini(t)
        msg += f"- {summary}\n\n"
        keyboard.append([InlineKeyboardButton(f"✅ Done", callback_data=f"done_{task_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("done_"):
        task_id = int(query.data.split("_")[1])
        mark_done(task_id)
        await query.edit_message_text("✅ Task marked done!")

# ===== Recurring Tasks =====
def create_recurring_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, user_id, description, details, deadline, priority, recurrence
        FROM tasks
        WHERE recurrence IS NOT NULL AND status='done'
    """)
    tasks = c.fetchall()
    for t in tasks:
        next_deadline = datetime.strptime(t[4], "%Y-%m-%d")
        if t[6] == "daily":
            next_deadline += timedelta(days=1)
        elif t[6] == "weekly":
            next_deadline += timedelta(weeks=1)
        elif t[6] == "monthly":
            next_deadline = get_next_month_deadline(next_deadline)
        else:
            continue
        add_task(t[1], t[2], t[3], next_deadline.strftime("%Y-%m-%d"), t[5], t[6])
    conn.close()

# ===== Daily Digest =====
async def daily_digest():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM tasks")
    users = c.fetchall()

    for (user_id,) in users:
        tasks = get_pending_tasks(user_id)
        if not tasks:
            continue

        tasks_ordered = reorder_tasks_with_gemini(tasks)

        msg = "📋 *Today's To-Do List:*\n\n"
        keyboard = []

        for t in tasks_ordered:
            task_id = t[0]
            summary = summarize_task_with_gemini(t)
            msg += f"- {summary}\n\n"
            keyboard.append([InlineKeyboardButton(f"✅ Done", callback_data=f"done_{task_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Error sending digest to {user_id}: {e}")

    conn.close()

# ===== Setup Bot =====
init_db()
application = ApplicationBuilder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("add", add))
application.add_handler(CommandHandler("list", list_tasks))
application.add_handler(CommandHandler("tasks", list_tasks))
application.add_handler(CallbackQueryHandler(button_handler))

scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(lambda: create_recurring_tasks(), 'cron', hour=0, minute=0)
scheduler.add_job(lambda: application.create_task(daily_digest()), 'cron', hour=9, minute=0)
scheduler.start()

print("Bot started...")
application.run_polling()