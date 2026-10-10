"""Environment-backed settings for VJ Auto Rename Bot."""

from os import environ

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> tuple[int, ...]:
    values = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                values.append(int(item))
            except ValueError as exc:
                raise ValueError("ADMINS must be a comma-separated list of Telegram numeric IDs") from exc
    return tuple(dict.fromkeys(values))


API_ID = int(environ.get("API_ID", "0") or "0")
API_HASH = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")
DB_URI = environ.get("DB_URI", "")
DB_NAME = environ.get("DB_NAME", "vjjoinrequetbot")

ADMINS = _parse_ids(environ.get("ADMINS", ""))
LOG_CHANNEL = int(environ.get("LOG_CHANNEL", "0") or "0")
SESSION_OWNER_ID = int(environ.get("SESSION_OWNER_ID", "0") or "0")

RENAME_BOT_USERNAME = environ.get("RENAME_BOT_USERNAME", "").strip().lstrip("@")
RENAME_RESPONSE_TIMEOUT = max(15, int(environ.get("RENAME_RESPONSE_TIMEOUT", "300")))
MAX_RENAME_ATTEMPTS = max(1, min(5, int(environ.get("MAX_RENAME_ATTEMPTS", "3"))))
