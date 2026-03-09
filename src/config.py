import logging
import os

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AGENT_ZERO_URL = os.environ.get("AGENT_ZERO_URL", "http://agent-zero:80")
AGENT_ZERO_API_KEY = os.environ["AGENT_ZERO_API_KEY"]
AGENT_ZERO_LOGIN = os.environ.get("AGENT_ZERO_LOGIN", "admin")
AGENT_ZERO_PASSWORD = os.environ.get("AGENT_ZERO_PASSWORD", "")
ALLOWED_USER_IDS: set[int] = set()

raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
if raw_ids.strip():
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip()}

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))
DRAFT_THROTTLE_MS = int(os.environ.get("DRAFT_THROTTLE_MS", "200"))


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def context_id_for(chat_id: int, thread_id: int | None = None) -> str:
    if thread_id is not None:
        return f"telegram-{chat_id}-topic-{thread_id}"
    return f"telegram-{chat_id}"
