"""Single process entry point: command bot plus one restored user-account client."""

import asyncio
import logging

# Pyrofork binds Client instances to the current loop during construction.
# Python 3.12+ does not always create one for the main thread automatically.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client

from config import API_HASH, API_ID, BOT_TOKEN, DB_URI
from plugins.conversation import ConversationMixin
from plugins.database import db
from plugins.rename_runtime import rename_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


class Bot(ConversationMixin, Client):
    def __init__(self):
        super().__init__(
            "vj join request bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins={"root": "plugins"},
            workers=16,
            sleep_threshold=30,
        )
        self.initialize_conversations()

    async def start(self):
        missing = [
            name for name, value in (
                ("API_ID", API_ID),
                ("API_HASH", API_HASH),
                ("BOT_TOKEN", BOT_TOKEN),
                ("DB_URI", DB_URI),
            ) if not value
        ]
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        await db.init()
        await super().start()
        me = await self.get_me()
        self.username = "@" + (me.username or "")
        await rename_runtime.start(self)
        logging.getLogger(__name__).info("VJ Auto Rename Bot started as %s", self.username)

    async def stop(self, *args):
        await rename_runtime.stop_user_session()
        await super().stop()
        await db.close()
        logging.getLogger(__name__).info("VJ Auto Rename Bot stopped")


if __name__ == "__main__":
    Bot().run()
