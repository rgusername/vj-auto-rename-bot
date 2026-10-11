import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pymongo.errors import DuplicateKeyError
from PIL import Image
from pyrogram.errors import BotResponseTimeout
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.commands import _normalize_thumbnail
from config import DB_NAME
from plugins.database import Database
from plugins.filename_utils import append_username, extract_original_filename
from plugins.rename_runtime import RenameRuntime, select_second_inline_button


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


class RuntimeSessionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_connected_session_for_requester_is_reused(self):
        runtime = RenameRuntime()
        runtime.user_client = SimpleNamespace(is_connected=True, get_me=AsyncMock(return_value=SimpleNamespace(id=123)))
        with patch("plugins.rename_runtime.db.get_session", new=AsyncMock()) as get_session:
            self.assertTrue(await runtime.ensure_user_session(123))
        get_session.assert_not_awaited()
        self.assertEqual(runtime.active_session_owner_id, 123)

    async def test_callback_timeout_waits_for_result_without_repeating_callback(self):
        runtime = RenameRuntime()
        runtime.rename_bot_id = 999
        stale_prompt = SimpleNamespace(
            id=12,
            chat=SimpleNamespace(id=999),
            text="Please enter new filename\nOld File Name :- another_episode.mp4",
            caption=None,
            reply_to_message_id=11,
            reply_markup=None,
            video=None,
            document=None,
        )
        prompt = SimpleNamespace(
            id=13,
            chat=SimpleNamespace(id=999),
            text="Please enter new filename\nOld File Name :- episode.mp4",
            caption=None,
            reply_to_message_id=11,
            reply_markup=None,
            video=None,
            document=None,
        )
        menu = SimpleNamespace(
            id=15,
            chat=SimpleNamespace(id=999),
            text="Select output type",
            caption=None,
            reply_to_message_id=14,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("document", callback_data="doc")],
                [InlineKeyboardButton("video", callback_data="video")],
            ]),
            video=None,
            document=None,
        )
        auto_delete_notice = SimpleNamespace(
            id=16,
            chat=SimpleNamespace(id=999),
            text="⏳ Auto-Delete Notice: this file will be deleted soon.",
            caption=None,
            reply_to_message_id=15,
            reply_markup=None,
            video=None,
            document=None,
        )
        final = SimpleNamespace(
            id=17,
            chat=SimpleNamespace(id=999),
            text=None,
            caption="S01E01",
            reply_to_message_id=16,
            reply_markup=None,
            video=SimpleNamespace(file_name="episode.mp4"),
            document=None,
        )
        sent_media = SimpleNamespace(id=11)

        async def copy_to_rename_bot(_chat_id):
            await runtime._rename_messages.put(stale_prompt)
            await runtime._rename_messages.put(prompt)
            return sent_media

        async def reply_filename(_chat_id, _filename, **_kwargs):
            await runtime._rename_messages.put(menu)
            return SimpleNamespace(id=14)

        async def callback_once(**_kwargs):
            await runtime._rename_messages.put(auto_delete_notice)
            await runtime._rename_messages.put(final)
            raise BotResponseTimeout("BOT_RESPONSE_TIMEOUT")

        source_message = SimpleNamespace(
            id=7,
            chat=SimpleNamespace(id=-100100),
            video=SimpleNamespace(file_name="episode.mp4"),
            document=None,
            copy=AsyncMock(side_effect=copy_to_rename_bot),
        )
        runtime.user_client = SimpleNamespace(
            send_photo=AsyncMock(return_value=SimpleNamespace(id=10)),
            send_message=AsyncMock(side_effect=reply_filename),
            request_callback_answer=AsyncMock(side_effect=callback_once),
        )
        current = {"stage": "preflight", "transitions": []}

        async def advance(_source, _message, expected, new_stage, **_fields):
            if current["stage"] != expected:
                return False
            current["transitions"].append((expected, new_stage))
            current["stage"] = new_stage
            return True

        with patch("plugins.rename_runtime.db.advance_job_stage", new=AsyncMock(side_effect=advance)), patch(
            "plugins.rename_runtime.db.get_username", new=AsyncMock(return_value="")
        ):
            returned, filename = await runtime._rename_conversation(source_message, {"thumbnail": b"thumb"})

        self.assertIs(returned, final)
        self.assertEqual(filename, "episode.mp4")
        runtime.user_client.request_callback_answer.assert_awaited_once()
        runtime.user_client.send_photo.assert_awaited_once()
        source_message.copy.assert_awaited_once()
        self.assertEqual(current["stage"], "final_received")
        self.assertIn(("callback_submitting", "callback_unknown"), current["transitions"])
        self.assertIn(("callback_unknown", "final_received"), current["transitions"])

    def test_only_preflight_failures_are_safe_to_retry(self):
        self.assertTrue(RenameRuntime._safe_to_retry("preflight", 1))
        for stage in ("thumbnail_sending", "media_sent", "filename_submitted", "callback_submitted", "publishing"):
            self.assertFalse(RenameRuntime._safe_to_retry(stage, 1), stage)

    async def test_timeout_after_callback_does_not_requeue_same_rename_workflow(self):
        runtime = RenameRuntime()
        source_message = SimpleNamespace(
            video=SimpleNamespace(file_name="episode.mp4"),
            document=None,
        )
        runtime.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=source_message))
        runtime._rename_conversation = AsyncMock(side_effect=asyncio.TimeoutError("final media timed out"))
        job = {"attempts": 1, "output_channel_id": -100202}
        current = {"attempts": 1, "stage": "callback_unknown"}

        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=[job, current])), patch(
            "plugins.rename_runtime.db.claim_job", new=AsyncMock(return_value=True)
        ), patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch("plugins.rename_runtime.db.update_job", new=AsyncMock()) as update_job:
            await runtime._process_job(-100101, 42)

        update_job.assert_awaited_once()
        fields = update_job.await_args.kwargs
        self.assertEqual(fields["status"], "failed")
        self.assertEqual(fields["stage"], "callback_unknown")

    async def test_requesters_saved_session_is_restored_without_relogin(self):
        runtime = RenameRuntime()
        with patch("plugins.rename_runtime.db.get_session", new=AsyncMock(return_value="saved-session")), patch.object(
            runtime, "start_user_session", new=AsyncMock(return_value=True)
        ) as start_session:
            self.assertTrue(await runtime.ensure_user_session(123))
        start_session.assert_awaited_once_with("saved-session", owner_id=123)

    async def test_pending_jobs_coroutine_is_awaited_before_cursor_iteration(self):
        class AsyncCursor:
            def __init__(self, items):
                self.items = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration

        runtime = RenameRuntime()
        runtime.user_client = object()
        cursor = AsyncCursor([
            {"source_channel_id": -100101, "source_message_id": 42},
            {"source_channel_id": -100101, "source_message_id": 43},
        ])
        with patch("plugins.rename_runtime.db.get_pending_jobs", new=AsyncMock(return_value=cursor)) as get_pending, patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch.object(runtime, "_launch_job") as launch_job:
            await runtime.resume_pending_jobs()

        get_pending.assert_awaited_once_with()
        self.assertEqual(launch_job.call_count, 2)
        launch_job.assert_any_call((-100101, 42), -100101, 42)
        launch_job.assert_any_call((-100101, 43), -100101, 43)


class VideoPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_is_uploaded_once_with_original_metadata_and_no_forward(self):
        runtime = RenameRuntime()
        payload = b"video bytes including audio and quality data"
        captured = {}

        async def download(file_name):
            with open(file_name, "wb") as handle:
                handle.write(payload)
            return file_name

        async def send_video(chat_id, video_path, **kwargs):
            with open(video_path, "rb") as handle:
                captured["bytes"] = handle.read()
            captured["chat_id"] = chat_id
            captured["kwargs"] = kwargs
            return SimpleNamespace(id=987)

        caption_entities = [SimpleNamespace(type="bold", offset=0, length=7)]
        final_message = SimpleNamespace(
            video=SimpleNamespace(
                file_name="original.mp4",
                duration=125,
                width=1920,
                height=1080,
                supports_streaming=True,
            ),
            document=None,
            caption="S01E02 | 1080p | AAC",
            caption_entities=caption_entities,
            has_media_spoiler=False,
            download=AsyncMock(side_effect=download),
            copy=AsyncMock(),
        )
        runtime.user_client = SimpleNamespace(
            send_video=AsyncMock(side_effect=send_video),
            send_document=AsyncMock(),
        )

        result = await runtime._publish_result(
            final_message,
            -100202,
            b"normalized-thumbnail",
            "renamed @tag.mp4",
        )

        self.assertEqual(result.id, 987)
        self.assertEqual(captured["bytes"], payload)
        self.assertEqual(captured["chat_id"], -100202)
        self.assertEqual(runtime.user_client.send_video.await_count, 1)
        runtime.user_client.send_document.assert_not_awaited()
        final_message.copy.assert_not_awaited()
        options = captured["kwargs"]
        self.assertEqual(options["file_name"], "renamed @tag.mp4")
        self.assertEqual(options["caption"], "S01E02 | 1080p | AAC")
        self.assertIs(options["caption_entities"], caption_entities)
        self.assertEqual((options["duration"], options["width"], options["height"]), (125, 1920, 1080))
        self.assertTrue(options["supports_streaming"])
        self.assertEqual(options["thumb"].getvalue(), b"normalized-thumbnail")

    async def test_video_upload_failure_does_not_forward_or_retry_as_copy(self):
        runtime = RenameRuntime()

        async def download(file_name):
            with open(file_name, "wb") as handle:
                handle.write(b"video bytes")
            return file_name

        final_message = SimpleNamespace(
            video=SimpleNamespace(file_name="original.mp4", duration=1, width=1, height=1),
            document=None,
            caption="",
            caption_entities=None,
            has_media_spoiler=False,
            download=AsyncMock(side_effect=download),
            copy=AsyncMock(),
        )
        runtime.user_client = SimpleNamespace(
            send_video=AsyncMock(side_effect=RuntimeError("upload failed")),
            send_document=AsyncMock(),
        )

        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            await runtime._publish_result(final_message, -100202, b"thumbnail", "renamed.mp4")

        runtime.user_client.send_video.assert_awaited_once()
        runtime.user_client.send_document.assert_not_awaited()
        final_message.copy.assert_not_awaited()


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
        database.jobs = SimpleNamespace(update_one=AsyncMock(side_effect=[
            SimpleNamespace(modified_count=1),
            SimpleNamespace(modified_count=0),
        ]))
        self.assertTrue(await database.claim_job(-100111, 42))
        self.assertFalse(await database.claim_job(-100111, 42))
        query = database.jobs.update_one.await_args.args[0]
        update = database.jobs.update_one.await_args.args[1]
        self.assertEqual(query["status"], "pending")
        self.assertEqual(query["stage"], {"$in": ["queued", "preflight"]})
        self.assertEqual(update["$inc"]["attempts"], 1)

    async def test_startup_installs_compound_unique_source_message_index(self):
        database = Database.__new__(Database)
        database._client = SimpleNamespace(admin=SimpleNamespace(command=AsyncMock()))
        database.configs = SimpleNamespace(create_index=AsyncMock())
        database.jobs = SimpleNamespace(create_index=AsyncMock())

        await database.init()

        database.jobs.create_index.assert_any_await(
            [("source_channel_id", 1), ("source_message_id", 1)],
            unique=True,
        )

    async def test_restart_recovery_retries_only_safe_preflight_work(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_many=AsyncMock())
        await database.recover_interrupted_jobs(max_attempts=3)
        self.assertEqual(database.jobs.update_many.await_count, 5)
        calls = database.jobs.update_many.await_args_list
        self.assertEqual(calls[0].args[0], {"status": "processing", "workflow_version": 2, "stage": "preflight"})
        self.assertEqual(calls[0].args[1]["$set"]["status"], "pending")
        self.assertEqual(calls[1].args[0]["stage"], {"$ne": "preflight"})
        self.assertEqual(calls[1].args[1]["$set"]["status"], "failed")
        self.assertEqual(calls[2].args[0], {"status": "publishing"})
        self.assertEqual(calls[2].args[1]["$set"]["status"], "failed")
        self.assertEqual(calls[3].args[0]["workflow_version"], {"$ne": 2})
        self.assertEqual(calls[3].args[0]["attempts"]["$gt"], 0)
        self.assertEqual(calls[4].args[0]["stage"], "preflight")
        self.assertEqual(calls[4].args[0]["attempts"]["$lt"], 3)

    async def test_job_stage_transition_is_atomic_and_requires_expected_state(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
        self.assertTrue(await database.advance_job_stage(-100111, 42, "preflight", "thumbnail_sending"))
        call = database.jobs.update_one.await_args
        self.assertEqual(call.args[0]["status"], "processing")
        self.assertEqual(call.args[0]["stage"], "preflight")
        self.assertEqual(call.args[1]["$set"]["stage"], "thumbnail_sending")


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
        self.assertEqual(payload["workflow_version"], 2)


if __name__ == "__main__":
    unittest.main()
