import os
import sqlite3
import requests
from datetime import datetime, timedelta, time as dt_time
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
    # Force Priority 1 for life-critical keywords (REGEX matching)
    critical_pattern = r"\b(surgery|hospital|doctor|er|urgent|critical|final deadline|deadline)\b"
    if re.search(critical_pattern, description, re.IGNORECASE):
        priority = 1
        
    # Auto-silence reminders for historical tasks
    reminded = 0
    if deadline:
        try:
            deadline_dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
            # If the task is already past, mark as reminded immediately
            if datetime.now(TIMEZONE) >= deadline_dt: reminded = 1
        except: pass

    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, description, details, deadline, priority, recurrence, reminded, tag, postpone_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)", (user_id, description, details, deadline, priority, recurrence, reminded, tag))
    conn.commit(); conn.close()

def update_task(task_id, description, details, deadline, priority, recurrence, tag=None):
    # Force Priority 1 for life-critical keywords (REGEX matching)
    critical_pattern = r"\b(surgery|hospital|doctor|er|urgent|critical|final deadline|deadline)\b"
    if re.search(critical_pattern, description, re.IGNORECASE):
        priority = 1

    # Auto-silence reminders for historical tasks
    reminded = 0
    if deadline:
        try:
            deadline_dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
            if datetime.now(TIMEZONE) >= deadline_dt: reminded = 1
        except: pass

    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT deadline, postpone_count FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()
    old_deadline = row[0] if row else None
    old_postpone_count = row[1] if row else 0
    
    # Calculate postpone increment: only if new deadline is strictly later than old deadline
    postpone_inc = 1 if old_deadline and deadline and str(deadline) > str(old_deadline) else 0
    new_postpone_count = old_postpone_count + postpone_inc
    
    if tag: c.execute("UPDATE tasks SET description=?, details=?, deadline=?, priority=?, recurrence=?, reminded=?, tag=?, postpone_count=? WHERE id=?", (description, details, deadline, priority, recurrence, reminded, tag, new_postpone_count, task_id))
    else: c.execute("UPDATE tasks SET description=?, details=?, deadline=?, priority=?, recurrence=?, reminded=?, postpone_count=? WHERE id=?", (description, details, deadline, priority, recurrence, reminded, new_postpone_count, task_id))
    conn.commit(); conn.close()
    logger.info(f"Updated task {task_id}: {description} (P:{priority}, D:{deadline}, R:{reminded}, Postponed:{new_postpone_count})")

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
        prompt = f"""Today is {now}. You are aidriel, a warm and supportive personal assistant.
        Extract task info from: '{text}'. 
        
        CRITICAL TASK RULES:
        - If the user lists multiple items (e.g. "milk, eggs, and bread"), YOU MUST include ALL of them in the 'task' field. Do not truncate.
        
        CRITICAL DEADLINE RULES:
        - If the user specifies a time or relative date (e.g., "1am tomorrow", "9pm tonight", "yesterday", "last Monday"), you MUST calculate the exact 'YYYY-MM-DD HH:MM' and put it in the 'deadline' field.
        - If NO time is specified or implied, you MUST leave 'deadline' as null. Do NOT default to the current time.
        - "Tonight" means today's date. "Tomorrow" means today's date + 1 day. "Yesterday" means today's date - 1 day.
        
        STRICT PRIORITY RULES:
        - YOU MUST use Priority 1 (Red) for any life-critical, medical, or high-stakes items.
        - For example: surgery, hospital visits, critical deadlines, or ER trips MUST be Priority 1. 
        - If a task sounds like it has serious consequences if missed, set Priority to 1.
        - Use Priority 3 (Normal) as the default.
        
        CRITICAL CONFIRMATION RULES:
        - Do NOT repeat or summarize the task details in your 'friendly_confirm'. 
        - Give a short, conversational acknowledgement as if you're speaking to a friend (e.g., "I'll take care of that!", "Added to your list.", "Got you covered!").
        - Avoid being robotic.
        
        Output JSON: {{ 'task': '...', 'deadline': 'YYYY-MM-DD HH:MM', 'priority': 1-5, 'recurrence': 'daily'|'weekly'|'monthly', 'tag': 'Work|Personal|Errand|Home|Shopping|Health|Finance|Social|General', 'friendly_confirm': 'short natural response' }}"""
        return call_gemini(prompt)
    except: return {"task": text, "deadline": (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d 09:00"), "priority": 3, "tag": "General", "friendly_confirm": "I've added that for you."}

def summarize_task_with_gemini(task):
    task_id, desc, details, deadline, priority, recurrence, tag, p_count = task
    # Handle priority as None
    safe_priority = priority if priority is not None else 3
    
    # Human-like priority correction (Using whole-word regex to avoid "grocery" matching "er")
    critical_pattern = r"\b(surgery|hospital|doctor|er|urgent|critical|final deadline|deadline)\b"
    is_critical = bool(re.search(critical_pattern, desc, re.IGNORECASE))
    
    if is_critical:
        safe_priority = 1
        
    # Shadow emoji logic
    shadow_msg = ""
    if p_count is not None and p_count > 0:
        shadow_msg = " 🫣"

    p_emoji = ["", "🔴", "🟠", "🟡", "🟢", "⚪️"][safe_priority] if 1 <= safe_priority <= 5 else "🟡"
    return f"{p_emoji} `[{tag}]` *{desc}*{shadow_msg} \n   📅 `{deadline}`"

def reorder_tasks_with_gemini(tasks):
    # Handle None values in priority (index 4) or deadline (index 3) to prevent TypeError
    base_sorted = sorted(tasks, key=lambda x: (x[4] if x[4] is not None else 3, x[3] if x[3] is not None else "9999-12-31 23:59"))
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
    prompt = f"""Today is {now}. Tasks: {task_context}. You are aidriel, a warm and empathetic assistant.
    User says: "{user_text}". 
    
    INTENT DETECTION RULES (STRICT):
    1. LISTING: If the user wants to see their pending tasks (e.g., "what's on my list?", "show my tasks"), return {{"type": "query", "answer": "list_tasks"}}.
    2. TASK DETECTION (PRIORITY): If the input describes a new future action, appointment, or deadline (e.g., "surgery tomorrow at 8am", "call the bank"), return {{"type": "task", ...}}. THIS TAKES PRECEDENCE OVER EMOTION.
    3. EMOTION/SUPPORT: If the user expresses purely feelings (e.g., "I'm overwhelmed", "I'm tired") WITHOUT any mention of a task or action, return {{"type": "query", "answer": "a warm, empathetic supportive response under 15 words"}}.
    4. DELETING/EDITING: If target name/ID matches context AND the user clearly wants to remove or change that specific existing task, return {{"type": "delete"|"edit", "target_db_id": ...}}. 
    5. DISCARDING INPUT: If the user says "discard" or "cancel" without specifying an existing task from the list, they likely mean to cancel the action they are currently in (like historical confirmation). Return {{"type": "query", "answer": "Action cancelled."}}.
    6. QUERY: General bot questions or small talk.
    
    CRITICAL CONSTRAINTS:
    - If user adds/edits a task with a time (e.g. "1am tomorrow"), you MUST calculate the exact 'YYYY-MM-DD HH:MM' for 'deadline' in 'task_info'.
    - If NO time is specified for a new task, 'deadline' MUST be null.
    - NEVER repeat the user's full input in the 'answer'.
    - Keep 'answer' conversational and human-like, not like a machine.
    - Keep 'answer' under 15 words.
    
        Output JSON: {{ "type": "query|task|suggestion|delete|edit", "target_db_id": int, "answer": "list_tasks or a friendly, natural response", "task_info": {{ "task": "...", "deadline": "YYYY-MM-DD HH:MM", "recurrence": "daily|weekly|monthly", "tag": "Work|Personal|Errand|Home|Shopping|Health|Finance|Social|General", ... }} }}"""
    return call_gemini(prompt)

# ===== UI Helpers =====
def format_priority(p): return ["", "High", "Medium", "Normal", "Low", "Very Low"][p] if 1 <= p <= 5 else "Normal"
def format_task_msg(description, deadline, priority, tag, custom_confirm=None):
    confirm = custom_confirm or "I've added that for you."
    tag_str = f"🏷 `{tag}` • " if tag and tag != "General" else ""
    deadline_str = f"📅 `{deadline}`" if deadline else "📅 `No deadline set`"
    return f"{confirm}\n\n📝 *{description}*\n{tag_str}{deadline_str}"

# ===== Background Jobs =====

async def daily_digest(context: ContextTypes.DEFAULT_TYPE, target_user_id=None):
    logger.info(f"Triggering Daily Digest (Target: {target_user_id})...")
    try:
        if target_user_id:
            users = [(target_user_id,)]
        else:
            conn = sqlite3.connect(DB_PATH); c = conn.cursor()
            c.execute("SELECT DISTINCT user_id FROM tasks"); users = c.fetchall()
            conn.close()
    except Exception as e:
        logger.error(f"Error fetching users for digest: {e}")
        return

    now_dt = datetime.now(TIMEZONE)
    today_str = now_dt.strftime("%Y-%m-%d")
    tomorrow_str = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    
    for (user_id,) in users:
        try:
            tasks = get_pending_tasks(user_id)
            # Filter for tasks due today (startswith today_str) or overdue (strictly less than today_str AND not today)
            # Use 'sorted' consistency: priority then deadline
            tasks_today = [t for t in tasks if t[3] and (t[3][:10] <= today_str)]
            # Sort them so they show up correctly in the digest
            tasks_today = sorted(tasks_today, key=lambda x: (x[4] if x[4] is not None else 3, x[3]))

            # Also fetch high priority tasks coming up tomorrow
            tasks_tomorrow_high = [t for t in tasks if t[3] and t[3].startswith(tomorrow_str) and t[4] is not None and t[4] <= 2]

            # In TEST mode, if we have no tasks today but have some tomorrow, let's show them in the digest
            # This makes testing easier when you add something for "tomorrow" and immediately run /test_digest
            tasks_to_show = tasks_today + (tasks_tomorrow_high if context.user_data and context.user_data.get('is_test') else [])

            if not tasks_to_show:
                logger.info(f"No tasks for user {user_id} today.")
                # Only send the "no tasks" message if specifically requested via test command
                if context.user_data and context.user_data.get('is_test'):
                    await context.bot.send_message(user_id, "🌅 *Morning Briefing*\n\nYou have no pending tasks for today! Enjoy your day. ✨", parse_mode="Markdown")
                continue

            task_list_text = [f"- {t[1]} (Tag: {t[6]}, Due: {t[3]})" for t in tasks_today]
            if tasks_tomorrow_high:
                task_list_text.append("\nUpcoming tomorrow (High Priority):")
                task_list_text.extend([f"- {t[1]} (Tag: {t[6]}, Due: {t[3]})" for t in tasks_tomorrow_high])
            
            task_text = "\n".join(task_list_text)
            prompt = f"You are aidriel, a warm and energetic personal assistant. Provide a short, enthusiastic morning briefing for these tasks. Be empathetic and encouraging. If there are high priority items tomorrow, give a brief heads up. Keep it under 3 sentences:\n{task_text}"
            briefing = "Good morning! I've looked at your schedule, and it looks like we have a productive day ahead. Let's tackle it together!"
            try:
                res = call_gemini(prompt)
                if isinstance(res, dict) and res.get("answer"): briefing = res.get("answer")
                elif isinstance(res, str): briefing = res
            except Exception as e:
                logger.warning(f"Gemini briefing failed for {user_id}: {e}")

            msg = f"🌅 *Morning Briefing*\n\n{briefing}\n\n📋 *Your Day:*\n\n"
            keyboard = []
            for idx, t in enumerate(tasks_to_show, 1):
                msg += f"*{idx}.* {summarize_task_with_gemini(t)}\n"
                keyboard.append([InlineKeyboardButton(f"✅ Done #{idx}", callback_data=f"done_{t[0]}")])

            await context.bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            logger.info(f"Sent digest to {user_id}")
        except Exception as e:
            logger.error(f"Failed to send digest to {user_id}: {e}")

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    # Fetch tasks that haven't been reminded yet
    c.execute("SELECT id, user_id, description, deadline FROM tasks WHERE status='pending' AND reminded=0")
    all_pending = c.fetchall(); now = datetime.now(TIMEZONE)
    
    if all_pending:
        logger.info(f"REMINDER LOOP: Checking {len(all_pending)} tasks at {now.strftime('%H:%M:%S')}")

    for tid, uid, desc, dl in all_pending:
        try:
            if not dl: continue
            # Ensure proper parsing of the deadline string
            try:
                deadline_dt = datetime.strptime(dl, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
            except ValueError:
                # Handle cases like "2026-03-16" without time
                deadline_dt = datetime.strptime(dl, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
                
            offset = get_user_offset(uid)
            
            # 1. Silently mark as reminded if it's already past the deadline.
            if now >= deadline_dt:
                c.execute("UPDATE tasks SET reminded=1 WHERE id=?", (tid,))
                continue
                
            # 2. Only send if we are within the notification window AND before the deadline.
            if now >= (deadline_dt - timedelta(minutes=offset)) and now < deadline_dt:
                import random
                intro = random.choice(["Just a quick reminder that", "Hey, just letting you know that", "Thought I'd remind you that", "Friendly heads up:"])
                await context.bot.send_message(uid, f"⏰ {intro} '{desc}' is due soon.", parse_mode="Markdown")
                c.execute("UPDATE tasks SET reminded=1 WHERE id=?", (tid,))
        except Exception as e:
            logger.error(f"Error in reminder loop for ID {tid}: {e}")
    conn.commit(); conn.close()

def create_recurring_tasks():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id, user_id, description, details, deadline, priority, recurrence, tag FROM tasks WHERE recurrence IS NOT NULL AND status='done'")
    rows = c.fetchall()
    for tid, uid, desc, det, dl, pri, rec, tag in rows:
        try:
            # If no deadline, default to today 9am to calculate next
            if not dl: dl = datetime.now(TIMEZONE).strftime("%Y-%m-%d 09:00")
            
            # Handle cases where deadline might be just date
            if len(dl) == 10: dl += " 09:00"
            nxt = datetime.strptime(dl, "%Y-%m-%d %H:%M")
            if rec == "daily": nxt += timedelta(days=1)
            elif rec == "weekly": nxt += timedelta(weeks=1)
            elif rec == "monthly":
                m = nxt.month + 1 if nxt.month < 12 else 1
                y = nxt.year + (1 if nxt.month == 12 else 0)
                nxt = nxt.replace(year=y, month=m)
            add_task(uid, desc, det, nxt.strftime("%Y-%m-%d %H:%M"), pri, rec, tag)
            c.execute("UPDATE tasks SET recurrence=NULL WHERE id=?", (tid,))
        except Exception as e:
            logger.error(f"Error creating recurring task for ID {tid}: {e}")
    conn.commit(); conn.close()

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE); greeting = "Good morning" if 5 <= now.hour < 12 else "Good afternoon" if 12 <= now.hour < 18 else "Good evening"
    await update.message.reply_text(f"👋 *{greeting}! I'm aidriel.* I'm here to help you stay organized. Feel free to talk to me naturally, or use /list to see what's on your plate!", parse_mode="Markdown")

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    await update.message.reply_text(f"🕒 My current time is `{now.strftime('%Y-%m-%d %H:%M:%S %Z')}`", parse_mode="Markdown")

async def test_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("Triggering test digest for you...")
    
    # Mock context to run digest
    class MockContext:
        def __init__(self, bot, user_data):
            self.bot = bot
            self.user_data = user_data

    # Temporarily mark as test to get feedback even if 0 tasks
    context.user_data['is_test'] = True
    # Pass user_id to isolate the digest
    await daily_digest(MockContext(context.bot, context.user_data), target_user_id=user_id)
    context.user_data['is_test'] = False

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        offset = get_user_offset(update.message.from_user.id)
        return await update.message.reply_text(f"I'll remind you **{offset} minutes** early. Change it: `/settings [mins]`", parse_mode="Markdown")
    try:
        new_offset = int(context.args[0])
        if new_offset < 0:
            return await update.message.reply_text("The reminder offset cannot be negative. Try `/settings 0` for reminders at the exact time.")
        set_user_offset(update.message.from_user.id, new_offset)
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
    if not context.args: return await update.message.reply_text("Which task would you like me to remove? (e.g. /delete 1)")
    try:
        display_id = int(context.args[0]); id_map = context.user_data.get('id_map', {}); db_id = id_map.get(display_id, display_id)
        desc = get_task_by_id(db_id); delete_task(db_id)
        await update.message.reply_text(f"Got it. I've removed '{desc}' from your list.")
    except: await update.message.reply_text("I'm sorry, I couldn't find that task. Could you double-check the ID?")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return await update.message.reply_text("How would you like to update it? (e.g., /edit 1 buy groceries tomorrow)")
    try:
        display_id = int(context.args[0]); new_text = " ".join(context.args[1:]); id_map = context.user_data.get('id_map', {}); db_id = id_map.get(display_id, display_id)
        parsed = parse_task_with_gemini(new_text); update_task(db_id, parsed.get("task") or new_text, "", parsed.get("deadline"), parsed.get("priority"), parsed.get("recurrence"), parsed.get("tag"))
        await update.message.reply_text(f"All set! I've updated '{parsed.get('task') or new_text}' for you.")
    except: await update.message.reply_text("I had a bit of trouble updating that task. Could you try again?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text, user_id = update.message.text, update.message.from_user.id
    tasks = get_pending_tasks(user_id); offset = get_user_offset(user_id)

    # Handle confirmation for historical tasks
    if context.user_data.get('awaiting_historical_confirm'):
        pending = context.user_data['awaiting_historical_confirm']
        user_text_low = user_text.lower()
        if "yes" in user_text_low or "add" in user_text_low or "ok" in user_text_low:
            add_task(user_id, pending['task'], "", pending['deadline'], pending['priority'], pending['recurrence'], pending['tag'])
            context.user_data.pop('awaiting_historical_confirm')
            return await update.message.reply_text("Done! I've added it to your list anyway.")
        elif "no" in user_text_low or "cancel" in user_text_low or "discard" in user_text_low:
            context.user_data.pop('awaiting_historical_confirm')
            return await update.message.reply_text("No problem, I've discarded that. What else can I help with?")
        
        # If they are correcting the time (e.g., "i mean tomorrow"), we merge the context
        context.user_data.pop('awaiting_historical_confirm')
        user_text = f"Add task: {pending['task']} {user_text}"
        logger.info(f"Historical Correction: Merged text to '{user_text}'")

    try:
        res = handle_smart_input_with_gemini(user_text, tasks, offset)
        if not res: raise Exception("Empty AI response")
        m_type = res.get("type")

        # Intent: List Tasks (AI-Driven + Catch-all safety)
        ai_answer = str(res.get("answer", "")).strip().lower()
        is_list_intent = "list" in ai_answer or "pending" in ai_answer or "show" in ai_answer or "tasks" in ai_answer or "items" in ai_answer
        if m_type == "query" and is_list_intent:
            return await list_tasks(update, context)

        if m_type == "query": await update.message.reply_text(res.get('answer'), parse_mode="Markdown")
        elif m_type == "suggestion": await update.message.reply_text(res.get('answer'), parse_mode="Markdown")
        elif m_type in ["delete", "edit"] and res.get("target_db_id"):
            tid = res["target_db_id"]
            if m_type == "delete": delete_task(tid)
            else:
                # To prevent data loss during AI updates, fetch current task data first
                conn = sqlite3.connect(DB_PATH); c = conn.cursor()
                c.execute("SELECT description, details, deadline, priority, recurrence, tag FROM tasks WHERE id=?", (tid,))
                current = c.fetchone(); conn.close()
                if not current: raise Exception(f"Task {tid} not found")
                
                i = res.get("task_info", {})
                # Use updated value if provided by AI, otherwise keep current value
                new_desc = i.get("task") or current[0]
                new_details = i.get("details") or current[1]
                new_deadline = i.get("deadline") or current[2]
                new_priority = i.get("priority") if i.get("priority") is not None else current[3]
                new_recurrence = i.get("recurrence") or current[4]
                new_tag = i.get("tag") or current[5]
                
                update_task(tid, new_desc, new_details, new_deadline, new_priority, new_recurrence, new_tag)
            await update.message.reply_text(res.get('answer'), parse_mode="Markdown")
        elif m_type == "task":
            i = res.get("task_info", {})
            deadline = i.get("deadline")
            desc = i.get("task") or user_text
            tag = i.get("tag") or "General"
            priority = i.get("priority") or 3
            recurrence = i.get("recurrence")
            
            # Historical Warning & Confirmation Flow
            is_past = False
            if deadline:
                try:
                    now = datetime.now(TIMEZONE)
                    dl_clean = str(deadline).strip()
                    if len(dl_clean) == 10:
                        deadline_dt = datetime.strptime(dl_clean, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
                        if now.date() > deadline_dt.date(): is_past = True
                    else:
                        deadline_dt = datetime.strptime(dl_clean, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
                        if now >= deadline_dt: is_past = True
                except: pass

            if is_past:
                context.user_data['awaiting_historical_confirm'] = {'task': desc, 'deadline': deadline, 'priority': priority, 'recurrence': recurrence, 'tag': tag}
                return await update.message.reply_text(f"Hold on, I noticed `{deadline}` has already passed. Did you still want me to add this task, or did you mean a different time?", parse_mode="Markdown")

            add_task(user_id, desc, "", deadline, priority, recurrence, tag)
            tag_str = f"🏷 `{tag}` • " if tag and tag != "General" else ""
            deadline_str = f"📅 `{deadline}`" if deadline else "📅 `No deadline set`"
            await update.message.reply_text(f"{res.get('answer')}\n\n📝 *{desc}*\n{tag_str}{deadline_str}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Handle message failed: {e}")
        # Default fallback: add as task if it doesn't look like a question
        if "?" not in user_text:
            parsed = parse_task_with_gemini(user_text)
            add_task(user_id, parsed.get("task") or user_text, "", parsed.get("deadline"), parsed.get("priority") or 3, parsed.get("recurrence"), parsed.get("tag") or "General")
            
            # Check for recurring tasks immediately if the fallback add_task has a recurrence
            if parsed.get("recurrence"):
                create_recurring_tasks()

            await update.message.reply_text(format_task_msg(parsed.get("task") or user_text, parsed.get("deadline"), parsed.get("priority") or 3, parsed.get("tag") or "General", parsed.get("friendly_confirm")), parse_mode="Markdown")
        else:
            await update.message.reply_text("I'm not sure how to help with that. Try /list or ask me something else!")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); db_id = int(query.data.split("_")[1])
    if query.data.startswith("done_"): 
        mark_done(db_id)
        # Check for recurring tasks immediately
        create_recurring_tasks()
        import random
        feedback = random.choice(["Great job getting that done!", "Nice work!", "One less thing to worry about!", "All set!", "Checked that off for you!"])
        await query.edit_message_text(f"✅ {feedback}")
    elif query.data.startswith("del_"): 
        delete_task(db_id)
        await query.edit_message_text("🗑 Removed that from your list.")
    elif query.data.startswith("edit_"): 
        await query.message.reply_text("Just let me know what needs to change: `/edit ID [new info]`")

async def post_init(application):
    logger.info("Initializing Bot Commands...")
    await application.bot.set_my_commands([BotCommand("start", "Help"), BotCommand("add", "Add task"), BotCommand("list", "Show tasks"), BotCommand("edit", "Edit task"), BotCommand("delete", "Delete task"), BotCommand("settings", "Timing")])

if __name__ == '__main__':
    # Fix Conflict: Wait 10 seconds to allow old instance to shutdown on Render
    time.sleep(10)
    
    # Start Flask in a background thread for Render port health check
    threading.Thread(target=run_flask, daemon=True).start()
    
    init_db()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    # Schedule Jobs
    job_queue = app.job_queue
    job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    # Explicit 9:00 AM Singapore Time
    digest_time = dt_time(hour=9, minute=0, tzinfo=TIMEZONE)
    job_queue.run_daily(daily_digest, time=digest_time)
    logger.info(f"Daily digest scheduled for {digest_time}")
    
    scheduler = BackgroundScheduler(timezone=TIMEZONE); scheduler.add_job(create_recurring_tasks, 'cron', hour=0, minute=0); scheduler.start()
    
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("time", get_time)); app.add_handler(CommandHandler("add", add)); app.add_handler(CommandHandler("list", list_tasks)); app.add_handler(CommandHandler("tasks", list_tasks)); app.add_handler(CommandHandler("edit", edit)); app.add_handler(CommandHandler("delete", delete)); app.add_handler(CommandHandler("settings", settings)); app.add_handler(CommandHandler("test_digest", test_digest)); app.add_handler(CallbackQueryHandler(button_handler)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling(drop_pending_updates=True)
