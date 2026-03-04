import logging
import os

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AGENT_ZERO_URL = os.environ.get("AGENT_ZERO_URL", "http://agent-zero:80")
AGENT_ZERO_API_KEY = os.environ["AGENT_ZERO_API_KEY"]
ALLOWED_USER_IDS: set[int] = set()

raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
if raw_ids.strip():
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # no allowlist configured = allow all
    return user_id in ALLOWED_USER_IDS


def context_id_for(chat_id: int) -> str:
    return f"telegram-{chat_id}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Hello! I'm a proxy to Agent Zero.\n\n"
        "Send me any message and I'll forward it to the agent.\n"
        "Use /reset to start a new conversation."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    ctx_id = context_id_for(update.effective_chat.id)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            await client.post(
                f"{AGENT_ZERO_URL}/api_reset_chat",
                json={"context_id": ctx_id},
                headers={"X-API-KEY": AGENT_ZERO_API_KEY},
            )
            await update.message.reply_text("Conversation has been reset. Send a new message to start fresh.")
        except Exception:
            logger.exception("Failed to reset chat")
            await update.message.reply_text("Failed to reset the conversation. Please try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    user_text = update.message.text
    ctx_id = context_id_for(update.effective_chat.id)

    # Send a "typing" indicator while waiting for Agent Zero
    await update.effective_chat.send_action("typing")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.post(
                f"{AGENT_ZERO_URL}/api_message",
                json={"message": user_text, "context_id": ctx_id},
                headers={"X-API-KEY": AGENT_ZERO_API_KEY},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            await update.message.reply_text("Agent Zero took too long to respond. Please try again.")
            return
        except httpx.ConnectError:
            await update.message.reply_text("Cannot reach Agent Zero. Is the service running?")
            return
        except Exception:
            logger.exception("Error calling Agent Zero API")
            await update.message.reply_text("Something went wrong while contacting Agent Zero.")
            return

    # Extract the agent's reply
    reply = data.get("response") or data.get("message") or str(data)

    if not reply or not reply.strip():
        reply = "(Agent Zero returned an empty response.)"

    # Telegram messages have a 4096-char limit; split if needed
    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i : i + 4096])


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram proxy bot starting (long-polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
