import asyncio
import os
import shutil
from pyrogram import Client, filters
from pyrogram.types import Message
import config
from renderer import run_render

app = Client(
    "render_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

render_lock = asyncio.Lock()

# In-memory storage for user states and registered UIDs
# Structure: {user_id: {"uid": str, "state": str, "pending_task": dict}}
user_sessions = {}


@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id, {})

    if not session.get("uid"):
        user_sessions[user_id] = {"state": "AWAITING_UID", "pending_task": None}
        return await message.reply_text(
            "👋 **Welcome!**\n\n"
            "Please send your **User ID (UID)** to verify your access and proceed."
        )

    await message.reply_text(
        f"✅ **Account Verified** (UID: `{session['uid']}`)\n\n"
        "Send `/render <LESSON_HASH> [SECONDS]` to generate your lecture video.\n\n"
        "**Example:**\n"
        "`/render 8NG670Y88C3YI1AB54NU 600`"
    )


@app.on_message(filters.command("setuid"))
async def set_uid_handler(client: Client, message: Message):
    args = message.command[1:]
    user_id = message.from_user.id

    if not args:
        user_sessions[user_id] = {"state": "AWAITING_UID", "pending_task": None}
        return await message.reply_text("Please enter your UID:")

    uid = args[0].strip()
    user_sessions[user_id] = {"uid": uid, "state": "IDLE", "pending_task": None}
    await message.reply_text(f"✅ UID updated to: `{uid}`. You can now use `/render`.")


@app.on_message(filters.command("render"))
async def render_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = user_sessions.setdefault(user_id, {"uid": None, "state": "IDLE", "pending_task": None})

    args = message.command[1:]
    if not args:
        return await message.reply_text("Usage: `/render <LESSON_HASH> [SECONDS]`")

    lesson_hash = args[0].strip()
    seconds = float(args[1]) if len(args) > 1 else 600.0

    # Gatekeep: Check if UID has been provided
    if not session.get("uid"):
        session["state"] = "AWAITING_UID"
        session["pending_task"] = {"lesson_hash": lesson_hash, "seconds": seconds}
        return await message.reply_text(
            "⚠️ **Verification Required**\n\n"
            "Please send your **User ID (UID)** first to begin processing this task."
        )

    await execute_render_job(client, message, session["uid"], lesson_hash, seconds)


@app.on_message(filters.text & ~filters.command(["start", "help", "render", "setuid"]))
async def text_input_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)

    if session and session.get("state") == "AWAITING_UID":
        uid = message.text.strip()
        session["uid"] = uid
        session["state"] = "IDLE"

        await message.reply_text(f"✅ UID registered successfully: `{uid}`")

        # Resume pending render job if user initiated /render beforehand
        pending = session.get("pending_task")
        if pending:
            session["pending_task"] = None
            await execute_render_job(
                client, message, uid, pending["lesson_hash"], pending["seconds"]
            )
        else:
            await message.reply_text(
                "You can now send `/render <LESSON_HASH> [SECONDS]` to start rendering."
            )


async def execute_render_job(client: Client, message: Message, uid: str, lesson_hash: str, seconds: float):
    if render_lock.locked():
        return await message.reply_text(
            "⏳ Another render job is currently processing. Your task has been queued..."
        )

    status_msg = await message.reply_text(
        f"🚀 **Task Initialized**\n"
        f"• **UID:** `{uid}`\n"
        f"• **Lesson:** `{lesson_hash}`\n"
        f"• **Duration:** `{seconds:.0f}s`\n\n"
        "Starting telemetry processing..."
    )

    async with render_lock:
        loop = asyncio.get_running_loop()

        def update_status(text: str):
            try:
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(
                        f"⚙️ **Status [UID: {uid}]:**\n`{text}`"
                    ),
                    loop,
                )
            except Exception:
                pass

        job_dir = os.path.join(config.WORK_DIR, lesson_hash)

        try:
            output_file = await asyncio.to_thread(
                run_render,
                lesson_hash=lesson_hash,
                window_seconds=seconds,
                work_dir=config.WORK_DIR,
                progress_cb=update_status,
            )

            await status_msg.edit_text("📤 Uploading completed video to Telegram...")
            await message.reply_video(
                video=output_file,
                caption=(
                    f"🎬 **Render Complete**\n"
                    f"• **UID:** `{uid}`\n"
                    f"• **Lesson:** `{lesson_hash}`\n"
                    f"• **Duration:** `{seconds:.0f}s`"
                ),
                supports_streaming=True,
            )
            await status_msg.delete()

        except Exception as e:
            await status_msg.edit_text(f"❌ **Error during render:**\n`{str(e)}`")
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    app.run()
  
