import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pymongo.errors import DuplicateKeyError
from PIL import Image
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.commands import _normalize_thumbnail
from config import DB_NAME
from plugins.database import Database
from plugins.filename_utils import append_username, extract_original_filename
from plugins.rename_runtime import select_second_inline_button


class FilenameTests(unittest.TestCase):
    def test_extracts_screenshot_filename_format(self):
        self.assertEqual(
            extract_original_filename("Please Enter New Filename...\nOld File Name :- Poulomi.mp4"),
            "Poulomi.mp4",
        )

    def test_username_before_extension_and_idempotent(self):
        result = append_username("Poulomi.mp4", "@Sitaratoons")
        self.assertEqual(result, "Poulomi @Sitaratoons.mp4")
        self.assertEqual(append_username(result, "@Sitaratoons"), result)

    def test_unicode_and_extensionless_names(self):
        self.assertEqual(append_username("日本語 🎬.mkv", "@例"), "日本語 🎬 @例.mkv")
        self.assertEqual(append_username("报告", "@tag"), "报告 @tag")

    def test_sanitizes_paths_and_invalid_characters(self):
        self.assertEqual(append_username("../bad:name.mp4", "@tag"), "badname @tag.mp4")

    def test_fallback_filename(self):
        self.assertEqual(extract_original_filename("Unexpected reply", "clip.mov"), "clip.mov")


class ThumbnailTests(unittest.TestCase):
    def test_thumbnail_is_small_jpeg_with_telegram_safe_dimensions(self):
        source = io.BytesIO()
        Image.new("RGB", (1200, 900), (130, 30, 210)).save(source, format="PNG")
        normalized = _normalize_thumbnail(source.getvalue())
        self.assertLessEqual(len(normalized), 190_000)
        with Image.open(io.BytesIO(normalized)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertLessEqual(max(result.size), 320)


class KeyboardTests(unittest.TestCase):
    def test_selects_second_button_by_actual_position_not_label(self):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("odd", callback_data="first")],
            [InlineKeyboardButton("DOCUMENT", callback_data="second")],
            [InlineKeyboardButton("ᴠɪᴅᴇᴏ", callback_data="third")],
        ])
        self.assertEqual(select_second_inline_button(markup).callback_data, "second")

    def test_rejects_missing_or_non_callback_second_button(self):
        with self.assertRaises(ValueError):
            select_second_inline_button(None)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("one", callback_data="1")],
            [InlineKeyboardButton("two", url="https://example.com")],
        ])
        with self.assertRaises(ValueError):
            select_second_inline_button(markup)


class SessionPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_session_record_is_read_from_users_collection(self):
        database = Database.__new__(Database)
        database.col = SimpleNamespace(find_one=AsyncMock(return_value={"session": "saved-session"}))
        self.assertEqual(await database.get_session(123), "saved-session")
        database.col.find_one.assert_awaited_once_with({"id": 123}, {"session": 1})

    async def test_missing_session_is_safe_and_legacy_database_name_is_preserved(self):
        database = Database.__new__(Database)
        database.col = SimpleNamespace(find_one=AsyncMock(return_value=None))
        self.assertIsNone(await database.get_session(123))
        self.assertEqual(DB_NAME, "vjjoinrequetbot")


class DatabaseMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_mapping_and_its_own_thumbnail_are_saved_by_source_id(self):
        database = Database.__new__(Database)
        database.configs = SimpleNamespace(update_one=AsyncMock())
        await database.set_channel_config(-100101, -100202, b"source-a-thumbnail")
        call = database.configs.update_one.await_args
        self.assertEqual(call.args[0], {"source_channel_id": -100101})
        self.assertEqual(call.kwargs["upsert"], True)
        self.assertEqual(call.args[1]["$set"]["output_channel_id"], -100202)
        self.assertEqual(call.args[1]["$set"]["thumbnail"], b"source-a-thumbnail")


class DatabaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_is_atomic_against_pending_status(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
        self.assertTrue(await database.claim_job(-100111, 42))
        query = database.jobs.update_one.await_args.args[0]
        update = database.jobs.update_one.await_args.args[1]
        self.assertEqual(query["status"], "pending")
        self.assertEqual(update["$inc"]["attempts"], 1)

    async def test_restart_recovery_retries_only_nonpublishing_failed_work(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_many=AsyncMock())
        await database.recover_interrupted_jobs(max_attempts=3)
        self.assertEqual(database.jobs.update_many.await_count, 3)
        calls = database.jobs.update_many.await_args_list
        self.assertEqual(calls[0].args[0], {"status": "processing"})
        self.assertEqual(calls[0].args[1]["$set"]["status"], "pending")
        self.assertEqual(calls[1].args[0], {"status": "publishing"})
        self.assertEqual(calls[1].args[1]["$set"]["status"], "failed")
        self.assertEqual(calls[2].args[0]["stage"]["$nin"], ["publishing", "cancelled"])
        self.assertEqual(calls[2].args[0]["attempts"]["$lt"], 3)


class DatabaseDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_source_message_is_not_reinserted(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(insert_one=AsyncMock(side_effect=DuplicateKeyError("duplicate")))
        self.assertFalse(await database.create_job(-100111, 42, -100222))
        database.jobs.insert_one.assert_awaited_once()

    async def test_first_source_message_is_inserted(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(insert_one=AsyncMock(return_value=None))
        self.assertTrue(await database.create_job(-100111, 42, -100222))
        payload = database.jobs.insert_one.await_args.args[0]
        self.assertEqual(payload["source_channel_id"], -100111)
        self.assertEqual(payload["source_message_id"], 42)
        self.assertEqual(payload["output_channel_id"], -100222)
        self.assertEqual(payload["status"], "pending")


if __name__ == "__main__":
    unittest.main()
