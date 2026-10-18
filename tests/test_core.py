import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pymongo.errors import DuplicateKeyError
from PIL import Image
from pyrogram import enums
from pyrogram.errors import BotResponseTimeout
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.commands import _normalize_thumbnail
from config import DB_NAME
from plugins.database import Database
from plugins.filename_utils import (
    append_username,
    extract_original_filename,
    extract_video_metadata,
    format_standardized_filename,
    normalize_filename_key,
)
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

    def test_extracts_requested_episode_and_quality_filename_formats(self):
        cases = {
            "Show.S01E01.480p.mkv": (1, 1, 480),
            "Show S1_EP_2 1080p.mp4": (1, 2, 1080),
            "Show Season 1 Episode 3 1440p.mkv": (1, 3, 1440),
            "Show Ep_1_1080p.mp4": (None, 1, 1080),
            "Show Ep1_1080p.mp4": (None, 1, 1080),
            "Show_Episode_12_in_Hindi_480p.mp4": (None, 12, 480),
            "Show_S1_Episode_12_1080p.mkv": (1, 12, 1080),
            "Show E1-720.mkv": (None, 1, 720),
            "Show S01E04 4K.mkv": (1, 4, 2160),
            "Show S02E01 2160p.mkv": (2, 1, 2160),
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                metadata = extract_video_metadata(filename)
                self.assertEqual((metadata["season"], metadata["episode"], metadata["quality"]), expected)
        self.assertEqual(
            extract_video_metadata("Show.S01E01.480p.mkv")["group_key"],
            extract_video_metadata("Show S1_EP_1 1080p.mp4")["group_key"],
        )
        for label, expected in (("480", 480), ("480p", 480), ("720", 720), ("720p", 720), ("1080", 1080), ("1080p", 1080), ("1440p", 1440), ("2160p", 2160), ("4K", 2160)):
            with self.subTest(quality=label):
                self.assertEqual(extract_video_metadata(f"Movie.{label}.mkv")["quality"], expected)
        self.assertIsNone(extract_video_metadata("Show.2024.mkv")["quality"])
        self.assertEqual(extract_video_metadata("Show.E480.1080p.mkv")["quality"], 1080)
        self.assertNotEqual(
            extract_video_metadata("Show.E480.1080p.mkv")["group_key"],
            extract_video_metadata("Show.E481.720p.mkv")["group_key"],
        )

    def test_standard_filename_uses_only_detected_metadata_and_original_extension(self):
        original = "Ultraviolet_Code_044_Episode_12_in_Hindi_dubbed_@SBANIME_480p @username.mp4"
        metadata = extract_video_metadata(original)
        self.assertEqual((metadata["episode"], metadata["quality"]), (12, 480))
        self.assertEqual(format_standardized_filename("@Sitaratoons", metadata, original), "[@Sitaratoons] Ep12 480p.mp4")
        no_metadata = extract_video_metadata("Movie.2024.mkv")
        self.assertEqual(format_standardized_filename("@Sitaratoons", no_metadata, "Movie.2024.mkv"), "Movie.2024 @Sitaratoons.mkv")

    def test_separator_variants_have_equal_normalized_filename_keys(self):
        self.assertEqual(
            normalize_filename_key("Ultraviolet_Code_044_Episode_12_480p.mp4"),
            normalize_filename_key("Ultraviolet.Code.044.Ep12.480p.MP4"),
        )


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
            await runtime._on_rename_bot_message(runtime.user_client, auto_delete_notice)
            await runtime._on_rename_bot_message(runtime.user_client, final)
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
        ), patch("plugins.rename_runtime.db.add_output_reply_id", new=AsyncMock()):
            submission = await runtime._rename_conversation(source_message, {"thumbnail": b"thumb"})
            if runtime._retry_tasks:
                await asyncio.gather(*list(runtime._retry_tasks))

        self.assertEqual(submission["expected_filename"], "episode.mp4")
        self.assertTrue(submission["submission_unknown"])
        self.assertTrue(submission["context"]["future"].done())
        self.assertIs(submission["context"]["future"].result(), final)
        runtime.user_client.request_callback_answer.assert_awaited_once()
        self.assertEqual(runtime.user_client.request_callback_answer.await_args.kwargs["timeout"], 300)
        runtime.user_client.send_photo.assert_awaited_once()
        source_message.copy.assert_awaited_once()
        self.assertEqual(current["stage"], "callback_unknown")
        self.assertIn(("callback_submitting", "callback_unknown"), current["transitions"])

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
        job = {
            "source_channel_id": -100101,
            "source_message_id": 42,
            "attempts": 1,
            "output_channel_id": -100202,
            "expected_filename": "episode.mp4",
        }
        current = {
            "source_channel_id": -100101,
            "source_message_id": 42,
            "attempts": 1,
            "stage": "callback_unknown",
            "status": "submission_unknown",
        }

        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=[job, current])), patch(
            "plugins.rename_runtime.db.claim_job", new=AsyncMock(return_value=True)
        ), patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch("plugins.rename_runtime.db.update_job", new=AsyncMock()) as update_job:
            await runtime._process_job(-100101, 42)

        update_job.assert_awaited_once()
        fields = update_job.await_args.kwargs
        self.assertEqual(fields["status"], "submission_unknown")
        self.assertEqual(fields["submission_status"], "submission_unknown")
        self.assertEqual(fields["stage"], "callback_unknown")

    async def test_acknowledged_submission_does_not_wait_for_output_media(self):
        runtime = RenameRuntime()
        source_message = SimpleNamespace(video=SimpleNamespace(file_name="episode.mp4"), document=None)
        job = {
            "source_channel_id": -100101,
            "source_message_id": 43,
            "attempts": 1,
            "output_channel_id": -100202,
            "expected_filename": "episode.mp4",
        }
        output_context = {"future": asyncio.get_running_loop().create_future()}
        submission = {"context": output_context, "expected_filename": "episode.mp4", "submission_unknown": False}
        runtime.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=source_message))
        runtime._rename_conversation = AsyncMock(return_value=submission)
        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=[job, job])), patch(
            "plugins.rename_runtime.db.claim_job", new=AsyncMock(return_value=True)
        ), patch("plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})), patch.object(
            runtime, "_ensure_output_task"
        ) as ensure_output:
            await asyncio.wait_for(runtime._process_job(-100101, 43), timeout=0.5)
        self.assertFalse(output_context["future"].done())
        ensure_output.assert_called_once_with(job, {"thumbnail": b"thumb"}, output_context)

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
        with patch("plugins.rename_runtime.db.restore_superseded_output_jobs", new=AsyncMock(return_value=0)), patch(
            "plugins.rename_runtime.db.get_matched_output_owners", new=AsyncMock(return_value=[])
        ), patch(
            "plugins.rename_runtime.db.get_superseded_jobs", new=AsyncMock(return_value=AsyncCursor([]))
        ), patch("plugins.rename_runtime.db.get_pending_jobs", new=AsyncMock(return_value=cursor)) as get_pending, patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch("plugins.rename_runtime.db.get_output_jobs", new=AsyncMock(return_value=AsyncCursor([]))), patch.object(
            runtime, "_enqueue_job"
        ) as enqueue_job:
            await runtime.resume_pending_jobs()

        get_pending.assert_awaited_once_with()
        self.assertEqual(enqueue_job.call_count, 2)
        enqueue_job.assert_any_call(-100101, 42, None)
        enqueue_job.assert_any_call(-100101, 43, None)

    async def test_resume_restores_submitted_output_watchers_not_submissions(self):
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
        submitted = {
            "source_channel_id": 100,
            "source_message_id": 5,
            "output_channel_id": 900,
            "status": "submitted",
            "expected_filename": "restored.mp4",
        }
        context = {"future": asyncio.get_running_loop().create_future()}
        with patch("plugins.rename_runtime.db.restore_superseded_output_jobs", new=AsyncMock(return_value=0)), patch(
            "plugins.rename_runtime.db.get_matched_output_owners", new=AsyncMock(return_value=[])
        ), patch(
            "plugins.rename_runtime.db.get_superseded_jobs", new=AsyncMock(return_value=AsyncCursor([]))
        ), patch("plugins.rename_runtime.db.get_pending_jobs", new=AsyncMock(return_value=AsyncCursor([]))), patch(
            "plugins.rename_runtime.db.get_output_jobs", new=AsyncMock(return_value=AsyncCursor([submitted]))
        ), patch("plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})), patch.object(
            runtime, "_enqueue_job"
        ) as enqueue, patch.object(runtime, "_register_output_waiter", return_value=context) as register, patch.object(
            runtime, "_ensure_output_task"
        ) as ensure_output, patch.object(runtime, "_recover_recent_outputs", new=AsyncMock()):
            await runtime.resume_pending_jobs()

        enqueue.assert_not_called()
        register.assert_called_once_with(submitted)
        ensure_output.assert_called_once_with(submitted, {"thumbnail": b"thumb"}, context)

    async def test_resume_restores_superseded_jobs_only_as_stale_output_anchors(self):
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
        latest = {
            "source_channel_id": 100,
            "source_message_id": 6,
            "job_id": "100:6",
            "status": "submitted",
            "expected_filename": "latest.mp4",
            "output_reply_ids": [60],
            "rename_bot_output_message_id": 66,
        }
        old = {
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "superseded",
            "expected_filename": "old.mp4",
            "output_reply_ids": [10],
        }
        with patch("plugins.rename_runtime.db.restore_superseded_output_jobs", new=AsyncMock(return_value=0)), patch(
            "plugins.rename_runtime.db.get_matched_output_owners", new=AsyncMock(return_value=[{
                "source_channel_id": 100,
            
