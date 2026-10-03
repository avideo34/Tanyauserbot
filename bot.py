import os
import re
import ast
import sys
import json
import math
import time
import random
import asyncio
import logging
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events, errors
from telethon.tl.types import ChannelParticipantsAdmins

# ---------------------------------------------------------
# Configuration (Loaded from .env file)
# ---------------------------------------------------------
load_dotenv()  # Load variables from .env

try:
    API_ID = int(os.getenv("API_ID", 0))
except ValueError:
    API_ID = 0

API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Load Sudo Users
SUDO_USERS = []
for x in os.getenv("SUDO_USERS", "").split(","):
    x = x.strip()
    if x:
        try:
            SUDO_USERS.append(int(x))
        except ValueError:
            pass

# Safety check
if not API_ID or not API_HASH or not BOT_TOKEN:
    print("❌ ERROR: Missing credentials!")
    print("Please make sure you have created a '.env' file in this folder and added your API_ID, API_HASH, and BOT_TOKEN.")
    sys.exit(1)

if not SUDO_USERS:
    print("⚠️ WARNING: No SUDO_USERS defined in .env! Nobody will be able to use the bot commands.")

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("knightx_bot")

# Reduce noisy Telethon INFO logs
logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ===== TELETHON v1.30.0 CRASH FIX (date isoformat bug) =====
try:
    from telethon._updates import messagebox

    _orig_messagebox_trace = messagebox.MessageBox._trace

    def _safe_messagebox_trace(self, text, *args):
        try:
            if isinstance(getattr(self, "date", None), int):
                self.date = datetime.utcfromtimestamp(self.date)
        except Exception:
            pass
        try:
            return _orig_messagebox_trace(self, text, *args)
        except Exception:
            return

    messagebox.MessageBox._trace = _safe_messagebox_trace
    logger.debug("Applied Telethon messagebox._trace patch")
except Exception as e:
    logger.debug("Could not apply Telethon patch (may be unnecessary): %s", e)
# ===== END PATCH =====

# Client name: KnightX Bot
KnightX = TelegramClient("bot_session", API_ID, API_HASH)

# Constants
BATCH_SIZE = 100
SLEEP_BETWEEN_BATCHES = 0.4

# Task maps
active_tasks = {}   # chat_id -> asyncio.Task (spam one-shot)
raid_tasks = {}     # chat_id -> asyncio.Task (background raid)

def get_random_sentence():
    """
    Reads the sentences.json file dynamically.
    Uses absolute path so it never fails to find the file.
    """
    try:
        # Get the exact folder where this bot.py script is located
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, "sentences.json")
        
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                sentences = json.load(f)
                if isinstance(sentences, list) and len(sentences) > 0:
                    return random.choice(sentences)
    except Exception as e:
        logger.error("Error reading sentences.json: %s", e)
        
    # Fallback sentence if JSON is missing, empty, or broken
    return "Ping!"

# ---------------------------
# Safe math evaluator
# ---------------------------
_ALLOWED_NAMES = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
_ALLOWED_NAMES.update({"abs": abs, "round": round, "pow": pow, "min": min, "max": max})

class _MathEvalVisitor(ast.NodeVisitor):
    def generic_visit(self, node):
        allowed_nodes = (
            ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant, ast.Call,
            ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
            ast.Mod, ast.USub, ast.UAdd, ast.FloorDiv, ast.Tuple, ast.List
        )
        if not isinstance(node, allowed_nodes):
            raise ValueError(f"Disallowed expression: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function names allowed")
        if node.func.id not in _ALLOWED_NAMES:
            raise ValueError(f"Function '{node.func.id}' not allowed")
        for arg in node.args:
            self.visit(arg)

    def visit_Name(self, node):
        if node.id not in _ALLOWED_NAMES:
            raise ValueError(f"Name '{node.id}' is not allowed")

def safe_eval(expr: str):
    expr = expr.strip()
    parsed = ast.parse(expr, mode="eval")
    _MathEvalVisitor().visit(parsed)
    code = compile(parsed, "<math>", "eval")
    return eval(code, {"__builtins__": {}}, _ALLOWED_NAMES)

def extract_first_math_expression(text: str):
    if not text:
        return None
    expr = text.strip()
    if re.fullmatch(r"[\d\s\.\+\-\*\/\^\%\(\),eEpiPIrtginsoclqurta-]+", expr):
        expr = expr.replace("^", "**")
        return expr
    m = re.search(r"`([^`]+)`", text)
    if m:
        candidate = m.group(1).replace("^", "**")
        if re.search(r"[0-9]", candidate):
            return candidate
    m = re.search(r"([0-9\.\s\+\-\*\/\^\%\(\)eEpiPIrtginsoclqurta,-]{3,})", text)
    if m:
        candidate = m.group(1).strip().replace("^", "**")
        return candidate
    return None

# ---------------------------
# Purge functions
# ---------------------------
async def delete_messages_in_chat(dialog, bot_id, limit=None):
    deleted = 0
    try:
        async for msg in KnightX.iter_messages(dialog, from_user=bot_id):
            batch = [msg.id]
            async for m in KnightX.iter_messages(dialog, from_user=bot_id, offset_id=msg.id, limit=BATCH_SIZE - 1):
                batch.append(m.id)
                if len(batch) >= BATCH_SIZE:
                    break

            if not batch:
                continue

            try:
                await KnightX.delete_messages(dialog, batch)
                deleted += len(batch)
            except errors.FloodWaitError as e:
                wait = getattr(e, "seconds", 5)
                logger.warning("FloodWait while deleting: sleeping %s", wait)
                await asyncio.sleep(wait + 1)
                try:
                    await KnightX.delete_messages(dialog, batch)
                    deleted += len(batch)
                except Exception as ex:
                    logger.exception("Failed after FloodWait: %s", ex)
            except Exception as e:
                logger.exception("Failed to delete batch: %s", e)

            await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
            if limit and deleted >= limit:
                break
    except Exception as e:
        logger.exception("Error iterating messages in dialog: %s", e)
    return deleted

# ===== .PURGE =====
@KnightX.on(events.NewMessage(pattern=r"^\.purge(?:\s+(all))?$"))
async def cmd_purge(event):
    if event.sender_id not in SUDO_USERS:
        return
    me = await KnightX.get_me()
    is_all = bool(event.pattern_match.group(1))
    info_msg = await event.respond("🗡️ Preparing purge of bot messages...")
    try:
        if not is_all:
            chat = await event.get_input_chat()
            await info_msg.edit("🔎 Scanning this chat for bot messages...")
            deleted = await delete_messages_in_chat(chat, me.id)
            await info_msg.edit(f"✅ Deleted approximately {deleted} message(s) from this chat.")
        else:
            await info_msg.edit("⚠️ Purge all initiated. This may take a long time...")
            total = 0
            async for dialog in KnightX.iter_dialogs():
                try:
                    total += await delete_messages_in_chat(dialog.entity, me.id)
                    await asyncio.sleep(0.6)
                except errors.FloodWaitError as e:
                    wait = getattr(e, "seconds", 60)
                    logger.warning("FloodWait while processing dialogs: sleeping %s", wait)
                    await asyncio.sleep(wait + 1)
                except Exception as e:
                    logger.exception("Error processing dialog: %s", e)
            await info_msg.edit(f"✅ Purge complete. Deleted approximately {total} bot message(s) across all dialogs.")
    finally:
        try:
            await asyncio.sleep(2)
            await info_msg.delete()
            await event.delete()
        except Exception:
            pass

# ===== .spam (one-shot) =====
async def _spam_worker_once(chat_id, times, message_text, delay_between=0.8, reply_to=None):
    try:
        for _ in range(int(times)):
            await KnightX.send_message(chat_id, message_text, reply_to=reply_to)
            await asyncio.sleep(delay_between)
    except asyncio.CancelledError:
        return
    except errors.FloodWaitError as fw:
        await asyncio.sleep(getattr(fw, "seconds", 5) + 1)
    finally:
        active_tasks.pop(chat_id, None)

@KnightX.on(events.NewMessage(pattern=r"^\.spam\s+(\d+)\s+(.+)"))
async def cmd_spam(event):
    if event.sender_id not in SUDO_USERS:
        return
    times = int(event.pattern_match.group(1))
    body = event.pattern_match.group(2).strip()
    chat_id = event.chat_id
    
    # Check if command is a reply to target the specific message
    reply_msg = await event.get_reply_message()
    reply_to_id = reply_msg.id if reply_msg else None

    old = active_tasks.get(chat_id)
    if old and not old.done():
        old.cancel()
    try:
        await event.delete()
    except Exception:
        pass
        
    task = asyncio.create_task(_spam_worker_once(chat_id, times, body, reply_to=reply_to_id))
    active_tasks[chat_id] = task

# ===== .raid on/off =====
async def _background_raid(chat_id, min_delay=3.0, max_delay=7.0, reply_to=None, target_mention=None):
    try:
        while True:
            # Dynamically fetch sentence on every iteration from absolute path
            text = get_random_sentence()
            
            # Append targeted tag if provided
            if target_mention:
                text = f"{target_mention} {text}"
                
            await KnightX.send_message(chat_id, text, reply_to=reply_to)
            await asyncio.sleep(random.uniform(min_delay, max_delay))
    except asyncio.CancelledError:
        return
    except errors.FloodWaitError as fw:
        await asyncio.sleep(getattr(fw, "seconds", 5) + 1)
    finally:
        raid_tasks.pop(chat_id, None)

@KnightX.on(events.NewMessage(pattern=r"^\.raid\s+(on|off)(?:\s+(.+))?$"))
async def cmd_raid_onoff(event):
    if event.sender_id not in SUDO_USERS:
        return
    
    mode = event.pattern_match.group(1).lower()
    
    # Optional target handle e.g. `.raid on @username`
    target_mention = event.pattern_match.group(2)
    if target_mention:
        target_mention = target_mention.strip()
        
    chat_id = event.chat_id
    
    if mode == "on":
        # Check if replying to someone to automatically set them as the target
        reply_msg = await event.get_reply_message()
        reply_to_id = reply_msg.id if reply_msg else None

        # If no manual tag was given but a reply exists, generate the user mention dynamically
        if not target_mention and reply_msg:
            try:
                sender = await reply_msg.get_sender()
                if sender:
                    name = getattr(sender, "first_name", "User")
                    target_mention = f"[{name}](tg://user?id={sender.id})"
            except Exception:
                pass

        if chat_id in raid_tasks:
            notice = await event.respond("⚠ Raid already running in this chat.")
            await asyncio.sleep(2)
            try:
                await notice.delete()
                await event.delete()
            except Exception:
                pass
            return
            
        task = asyncio.create_task(_background_raid(chat_id, reply_to=reply_to_id, target_mention=target_mention))
        raid_tasks[chat_id] = task
        
        status = "🚀 Raid ON: "
        status += f"Targeting {target_mention}." if target_mention else "General spam."
        status_msg = await event.respond(status)
        
        # Vanish logs logic: Delete command and status msg after a brief delay
        await asyncio.sleep(2)
        try:
            await status_msg.delete()
            await event.delete()
        except Exception:
            pass

    else:
        t = raid_tasks.get(chat_id)
        if not t:
            notice = await event.respond("ℹ Raid is not running here.")
            await asyncio.sleep(2)
            try:
                await notice.delete()
                await event.delete()
            except Exception:
                pass
            return
        t.cancel()
        raid_tasks.pop(chat_id, None)
        
        done = await event.respond("🛑 Raid OFF.")
        
        # Vanish logs logic
        await asyncio.sleep(1.5)
        try:
            await done.delete()
            await event.delete()
        except Exception:
            pass

# ===== .stop =====
@KnightX.on(events.NewMessage(pattern=r"^\.stop$"))
async def cmd_stop(event):
    if event.sender_id not in SUDO_USERS:
        return
    chat_id = event.chat_id
    task = active_tasks.get(chat_id)
    if not task:
        m = await event.respond("ℹ️ Nothing running.")
        await asyncio.sleep(2)
        try:
            await m.delete()
            await event.delete()
        except Exception:
            pass
        return
    task.cancel()
    active_tasks.pop(chat_id, None)
    notice = await event.respond("🛑 Stopped.")
    await asyncio.sleep(1.5)
    try:
        await notice.delete()
        await event.delete()
    except Exception:
        pass

# ===== .tagadmins =====
@KnightX.on(events.NewMessage(pattern=r"^\.tagadmins$"))
async def cmd_tagadmins(event):
    if event.sender_id not in SUDO_USERS:
        return
    chat = await event.get_input_chat()
    checking = await event.respond("👀 Scanning for admins...")
    try:
        admins = []
        async for user in KnightX.iter_participants(chat, filter=ChannelParticipantsAdmins):
            if user.bot:
                continue
            username = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
            admins.append(username)
        if not admins:
            await checking.edit("⚠️ No admins found in this chat.")
            await asyncio.sleep(2)
            try:
                await checking.delete()
                await event.delete()
            except Exception:
                pass
            return
        message = " **Hello all**\n\n" + " ".join(admins)
        await checking.edit(message)
    except errors.ChatAdminRequiredError:
        await checking.edit("🚫 I need admin rights to see group admins.")
    except Exception as e:
        await checking.edit(f"❌ Error: {e}")
    finally:
        try:
            await asyncio.sleep(3)
            await checking.delete()
            await event.delete()
        except Exception:
            pass

# ===== .whois (profile photo + detailed info) =====
@KnightX.on(events.NewMessage(pattern=r"^\.whois(?:\s+(.+))?$"))
async def cmd_whois(event):
    if event.sender_id not in SUDO_USERS:
        return
    target = event.pattern_match.group(1)
    user = None
    try:
        if target:
            target = target.strip()
            try:
                user = await KnightX.get_entity(target)
            except Exception:
                try:
                    user = await KnightX.get_entity(int(target))
                except Exception:
                    await event.respond("❌ Invalid username/ID.")
                    return
        else:
            reply = await event.get_reply_message()
            if not reply:
                await event.respond("❌ Reply to someone or give @username or ID.")
                return
            user = await reply.get_sender()
    except Exception as e:
        await event.respond(f"❌ Error fetching user: {e}")
        return

    # Build comprehensive info (merges .info into .whois capabilities)
    is_bot = "Yes 🤖" if getattr(user, "bot", False) else "No 👤"
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    username = getattr(user, "username", None)
    phone = getattr(user, "phone", None)
    about = getattr(user, "about", None) if hasattr(user, "about") else None

    info_text = (
        f"🔎 **User Information**\n"
        f"—————————————\n"
        f"👤 **Name:** {name or 'Unknown'}\n"
        f"🆔 **User ID:** `{user.id}`\n"
        f"🏷 **Username:** @{username if username else 'None'}\n"
        f"📞 **Phone:** {phone if phone else 'Hidden'}\n"
        f"🤖 **Bot:** {is_bot}\n"
    )
    if about:
        info_text += f"💬 **Bio:** {about}\n"
    info_text += "—————————————"

    # Try to download profile photo (highest quality)
    photo_path = None
    try:
        photo_path = await KnightX.download_profile_photo(user, file=tempfile.gettempdir())
    except Exception:
        photo_path = None

    if photo_path:
        try:
            await KnightX.send_file(event.chat_id, photo_path, caption=info_text)
        except Exception as e:
            # fallback to text only if photo fails to upload
            await event.respond(info_text)
        finally:
            # cleanup downloaded photo
            try:
                if os.path.exists(photo_path):
                    os.remove(photo_path)
            except Exception:
                pass
    else:
        # User has no photo or could not be downloaded
        await event.respond(info_text)

    try:
        await asyncio.sleep(2)
        await event.delete()
    except Exception:
        pass

# ===== .ping =====
@KnightX.on(events.NewMessage(pattern=r"^\.ping$"))
async def cmd_ping(event):
    if event.sender_id not in SUDO_USERS:
        return
    t0 = time.perf_counter()
    msg = await event.respond("🏓 Pong...")
    t1 = time.perf_counter()
    latency = int((t1 - t0) * 1000)
    try:
        await msg.edit(f"🏓 Pong! {latency} ms")
    except Exception:
        pass
    await asyncio.sleep(2)
    try:
        await event.delete()
    except Exception:
        pass

# ===== .calculate =====
@KnightX.on(events.NewMessage(pattern=r"^\.calculate(?:\s+(.+))?$"))
async def cmd_calculate(event):
    if event.sender_id not in SUDO_USERS:
        return
    provided = event.pattern_match.group(1)
    expr = None
    if provided and provided.strip():
        expr = provided.strip()
    else:
        reply = await event.get_reply_message()
        if reply and getattr(reply, "text", None):
            expr = extract_first_math_expression(reply.text)
    if not expr:
        await event.respond("❌ Usage: `.calculate <expression>` or reply to a message.")
        return
    try:
        result = safe_eval(expr)
        await event.respond(f"🧮 Result:\n`{expr}`\n= `{result}`")
    except Exception as e:
        await event.respond(f"❌ Could not evaluate expression: {e}")
    finally:
        try:
            await asyncio.sleep(3)
            await event.delete()
        except Exception:
            pass

# ===== .help =====
@KnightX.on(events.NewMessage(pattern=r"^\.help$"))
async def cmd_help(event):
    if event.sender_id not in SUDO_USERS:
        return
    help_text = """
🛡 **Knight X Power — Bot Help (Sudo Only)** 🛡

🧹 **Purge / Delete**
• `.purge` — Delete bot messages in this chat
• `.purge all` — Delete bot messages in all chats

🎵 **Spam / Raid**
• `.spam <count> <text>` — Repeat text (reply to target a message)
• `.raid on` — Start background messages
• `.raid on @username` — Start raid & mention user automatically
• `.stop` — Stop running spam/raid (per chat)

👑 **Admin Tools**
• `.tagadmins` — Tag all admins in the group

🧍 **User Info**
• `.whois` — Send profile photo + detailed user info (reply/@/id)

🧮 **Utilities**
• `.calculate <expr>` — Evaluate math expressions safely (or reply)
• `.ping` — Ping (latency) check

⚡ *Knight X Power Bot is Online for Sudo Users*
"""
    msg = await event.respond(help_text)
    await asyncio.sleep(2)
    try:
        await event.delete()
    except Exception:
        pass

# ---------------------------
# Startup & graceful shutdown
# ---------------------------
async def main():
    await KnightX.start(bot_token=BOT_TOKEN)
    me = await KnightX.get_me()
    logger.info("Bot started as %s (%s)", me.username or me.first_name, me.id)
    print("🛡️ Bot Online as:", me.username or me.first_name)
    try:
        await KnightX.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info("Runner cancelled (shutting down).")
    except Exception as e:
        logger.exception("Error in run loop: %s", e)
    finally:
        for t in list(active_tasks.values()) + list(raid_tasks.values()):
            try:
                t.cancel()
            except Exception:
                pass
        await KnightX.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.")
