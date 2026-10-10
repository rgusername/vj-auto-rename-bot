import asyncio
import importlib
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "placeholder_api_hash")
os.environ.setdefault("BOT_TOKEN", "123456:placeholder")
os.environ.setdefault("DB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("ADMINS", "123456789")
os.environ.setdefault("RENAME_BOT_USERNAME", "RenameBot")


class ImportSmokeTests(unittest.TestCase):
    def test_application_modules_import(self):
        for module_name in (
            "config",
            "plugins.database",
            "plugins.filename_utils",
            "plugins.rename_runtime",
            "plugins.commands",
            "plugins.generate",
            "plugins.broadcast",
            "bot",
        ):
            with self.subTest(module=module_name):
                importlib.import_module(module_name)

    def test_bot_has_private_conversation_methods(self):
        from bot import Bot
        self.assertTrue(callable(getattr(Bot, "ask", None)))
        self.assertTrue(callable(getattr(Bot, "listen", None)))

    def test_bot_constructor_registers_handlers_without_networking(self):
        import bot
        loop = asyncio.get_event_loop()
        with patch.object(bot, "API_ID", 12345), patch.object(bot, "API_HASH", "a" * 32), patch.object(bot, "BOT_TOKEN", "123456:placeholder"):
            client = bot.Bot()
        loop.run_until_complete(asyncio.sleep(0))
        self.assertIn(-100, client.dispatcher.groups)
        client.executor.shutdown(wait=False)

    def test_conversation_mixin_registers_private_message_handler(self):
        from plugins.conversation import ConversationMixin

        class FakeClient(ConversationMixin):
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append((handler, group))

        client = FakeClient()
        client.initialize_conversations()
        self.assertEqual(len(client.handlers), 1)
        self.assertEqual(client.handlers[0][1], -100)
        self.assertTrue(callable(client.handlers[0][0].callback))


if __name__ == "__main__":
    unittest.main()
