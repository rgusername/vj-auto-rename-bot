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


API_ID = int(environ.get("API_ID", "24916173") or "0")
API_HASH = environ.get("API_HASH", "5c82a6ae14e291efc20dee7757c9feee")
BOT_TOKEN = environ.get("BOT_TOKEN", "8765889989:AAHy6H9mmC-9ZnAYVuKKSeiiYs4aMLhizpQ")
DB_URI = environ.get("DB_URI", "mongodb+srv://rj5706603:O95nvJYxapyDHfkw@cluster0.fzmckei.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
DB_NAME = environ.get("DB_NAME", "vjjoinrequetbot")

ADMINS = _parse_ids(environ.get("ADMINS", "7622078787"))
LOG_CHANNEL = int(environ.get("LOG_CHANNEL", "-1004450037911") or "0")
SESSION_OWNER_ID = int(environ.get("SESSION_OWNER_ID", "7622078787") or "0")

RENAME_BOT_USERNAME = environ.get("RENAME_BOT_USERNAME", "Sitaratoons_Rename_bot").strip().lstrip("@")
RENAME_RESPONSE_TIMEOUT = max(15, int(environ.get("RENAME_RESPONSE_TIMEOUT", "300")))
MAX_RENAME_ATTEMPTS = max(1, min(5, int(environ.get("MAX_RENAME_ATTEMPTS", "3"))))
