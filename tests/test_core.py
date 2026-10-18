import asyncio
import io
import unittest
from datetime import datetime, timedelta, timezone
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

    async def test_callback_timeout_job_does_not_return_before_late_output_delivery(self):
        runtime = RenameRuntime()
        source_message = SimpleNamespace(video=SimpleNamespace(file_name="episode.mp4"), document=None)
        runtime.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=source_message))
        job = {
            "source_channel_id": -100101,
            "source_message_id": 42,
            "attempts": 1,
            "output_channel_id": -100202,
            "expected_filename": "episode.mp4",
            "status": "pending",
            "stage": "queued",
        }
        current = {**job, "status": "submission_unknown", "stage": "callback_unknown"}
        completed = {**current, "status": "completed", "stage": "completed"}
        context = {"future": asyncio.get_running_loop().create_future()}
        delivery_started = asyncio.Event()
        release_delivery = asyncio.Event()

        async def deliver():
            delivery_started.set()
            await release_delivery.wait()

        delivery_task = asyncio.create_task(deliver())
        runtime._rename_conversation = AsyncMock(return_value={
            "context": context,
            "expected_filename": "episode.mp4",
            "submission_unknown": True,
        })
        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=[job, current, completed])), patch(
            "plugins.rename_runtime.db.claim_job", new=AsyncMock(return_value=True)
        ), patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch.object(runtime, "_ensure_output_task", Mock(return_value=delivery_task)) as ensure_output:
            processing = asyncio.create_task(runtime._process_job(-100101, 42))
            await asyncio.wait_for(delivery_started.wait(), timeout=1)
            self.assertFalse(processing.done())
            runtime._rename_conversation.assert_awaited_once()
            release_delivery.set()
            self.assertTrue(await asyncio.wait_for(processing, timeout=1))
        ensure_output.assert_called_once()

    async def test_job_processing_waits_until_output_is_delivered(self):
        runtime = RenameRuntime()
        source_message = SimpleNamespace(video=SimpleNamespace(file_name="episode.mp4"), document=None)
        job = {
            "source_channel_id": -100101,
            "source_message_id": 43,
            "attempts": 1,
            "output_channel_id": -100202,
            "expected_filename": "episode.mp4",
            "status": "pending",
            "stage": "queued",
        }
        output_context = {"future": asyncio.get_running_loop().create_future()}
        submission = {"context": output_context, "expected_filename": "episode.mp4", "submission_unknown": False}
        runtime.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=source_message))
        runtime._rename_conversation = AsyncMock(return_value=submission)
        submitted = {**job, "status": "submitted"}
        completed = {**job, "status": "completed"}
        delivery_started = asyncio.Event()
        release_delivery = asyncio.Event()

        async def deliver():
            delivery_started.set()
            await release_delivery.wait()

        delivery_task = asyncio.create_task(deliver())
        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=[job, submitted, completed])), patch(
            "plugins.rename_runtime.db.claim_job", new=AsyncMock(return_value=True)
        ), patch("plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})), patch.object(
            runtime, "_ensure_output_task"
        , Mock(return_value=delivery_task)) as ensure_output:
            processing = asyncio.create_task(runtime._process_job(-100101, 43))
            await asyncio.wait_for(delivery_started.wait(), timeout=1)
            self.assertFalse(processing.done())
            release_delivery.set()
            self.assertTrue(await asyncio.wait_for(processing, timeout=1))
        ensure_output.assert_called_once_with(job, {"thumbnail": b"thumb"}, output_context)

    async def test_requesters_saved_session_is_restored_without_relogin(self):
        runtime = RenameRuntime()
        with patch("plugins.rename_runtime.db.get_session", new=AsyncMock(return_value="saved-session")), patch.object(
            runtime, "start_user_session", new=AsyncMock(return_value=True)
        ) as start_session:
            self.assertTrue(await runtime.ensure_user_session(123))
        start_session.assert_awaited_once_with("saved-session", owner_id=123)

    async def test_persistent_global_fifo_cursor_is_awaited_and_restored_in_order(self):
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
        runtime.rename_bot_id = 999
        cursor = AsyncCursor([
            {"source_channel_id": -100101, "source_message_id": 42, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"source_channel_id": -100101, "source_message_id": 43, "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        ])
        with patch("plugins.rename_runtime.db.get_queue_jobs", new=AsyncMock(return_value=cursor)) as get_queue, patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch.object(runtime, "_ensure_submission_worker") as ensure_worker:
            await runtime.resume_pending_jobs()

        get_queue.assert_awaited_once_with()
        ensure_worker.assert_called()
        self.assertEqual([item["key"] for item in runtime._submission_items], [(-100101, 42), (-100101, 43)])

    async def test_resume_queues_submitted_output_before_later_jobs_without_detached_tracker(self):
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
        runtime.rename_bot_id = 999
        submitted = {
            "source_channel_id": 100,
            "source_message_id": 5,
            "output_channel_id": 900,
            "status": "submitted",
            "expected_filename": "restored.mp4",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        with patch("plugins.rename_runtime.db.get_queue_jobs", new=AsyncMock(return_value=AsyncCursor([submitted]))), patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ), patch.object(runtime, "_ensure_submission_worker") as ensure_worker, patch.object(
            runtime, "_ensure_output_task"
        ) as ensure_output:
            await runtime.resume_pending_jobs()

        self.assertEqual([item["key"] for item in runtime._submission_items], [(100, 5)])
        ensure_worker.assert_called()
        ensure_output.assert_not_called()

    async def test_restore_output_contexts_loads_owners_and_stale_anchors(self):
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
        old = {
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "superseded",
            "expected_filename": "old.mp4",
            "output_reply_ids": [10],
        }
        with patch("plugins.rename_runtime.db.get_matched_output_owners", new=AsyncMock(return_value=[{
                "source_channel_id": 100,
                "source_message_id": 6,
                "rename_bot_output_message_id": 66,
            }])
        ), patch(
            "plugins.rename_runtime.db.get_superseded_jobs", new=AsyncMock(return_value=AsyncCursor([old]))
        ):
            await runtime._restore_output_contexts()

        self.assertIn((100, 1), runtime._superseded_waiters)
        self.assertEqual(runtime._matched_output_owners[66], (100, 6))

    async def test_superseded_output_task_cannot_claim_destination_upload(self):
        runtime = RenameRuntime()
        job = {
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "output_channel_id": 900,
            "status": "submitted",
            "expected_filename": "old.mp4",
        }
        context = runtime._register_output_waiter(job)
        context["superseded"] = True
        context["future"].set_result(SimpleNamespace(id=50))
        with patch("plugins.rename_runtime.db.begin_output_publish", new=AsyncMock()) as begin, patch.object(
            runtime, "_publish_result", new=AsyncMock()
        ) as publish:
            await runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context)
        begin.assert_not_awaited()
        publish.assert_not_awaited()


class SharedQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_quality_variants_are_all_enqueued_and_duplicate_source_is_ignored(self):
        runtime = RenameRuntime()
        config = {"channel_number": 1, "output_channel_id": 900, "thumbnail": b"thumb"}
        with patch("plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value=config)), patch(
            "plugins.rename_runtime.db.get_username", new=AsyncMock(return_value="@tag")
        ), patch(
            "plugins.rename_runtime.db.create_job", new=AsyncMock(side_effect=[True, True, True, False])
        ) as create_job:
            messages = [
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=1, video=SimpleNamespace(file_name="Show.S01E04.480p.mkv"), document=None, caption=None),
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=2, video=SimpleNamespace(file_name="Show.S01E04.1080p.mkv"), document=None, caption=None),
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=3, video=SimpleNamespace(file_name="Show.S01E04.720p.mkv"), document=None, caption=None),
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=2, video=SimpleNamespace(file_name="Show.S01E04.1080p.mkv"), document=None, caption=None),
            ]
            for message in messages:
                await runtime._on_source_message(None, message)

        self.assertEqual(create_job.await_count, 4)
        self.assertEqual([item["key"] for item in runtime._submission_items], [(100, 1), (100, 2), (100, 3)])
        self.assertEqual([item["metadata"]["quality"] for item in runtime._submission_items], [480, 1080, 720])
        self.assertEqual(create_job.await_args_list[0].kwargs["media_metadata"]["quality"], 480)
        self.assertEqual(create_job.await_args_list[1].kwargs["media_metadata"]["quality"], 1080)
        self.assertEqual(create_job.await_args_list[2].kwargs["media_metadata"]["quality"], 720)
        self.assertEqual(create_job.await_args_list[0].kwargs["channel_number"], 1)
        self.assertEqual(create_job.await_args_list[0].kwargs["expected_filename"], "[@tag] S1 Ep4 480p.mkv")
        self.assertEqual(create_job.await_args_list[1].kwargs["expected_filename"], "[@tag] S1 Ep4 1080p.mkv")
        self.assertEqual(create_job.await_args_list[2].kwargs["expected_filename"], "[@tag] S1 Ep4 720p.mkv")

    async def test_strict_fifo_ignores_quality_and_preserves_arrival_order(self):
        runtime = RenameRuntime()
        runtime._enqueue_job(100, 1, extract_video_metadata("Show S01E01 1080p.mkv"))
        runtime._enqueue_job(100, 2, extract_video_metadata("Show S01E01 720p.mkv"))
        runtime._enqueue_job(100, 3, extract_video_metadata("Show S01E01 480p.mkv"))
        runtime._enqueue_job(200, 4, extract_video_metadata("Different.Movie.1080p.mp4"))
        self.assertEqual([item["key"] for item in runtime._submission_items], [
            (100, 1), (100, 2), (100, 3), (200, 4),
        ])

    async def test_queue_sort_normalizes_mixed_naive_and_aware_datetimes_to_utc(self):
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
        naive_utc = datetime(2026, 1, 1, 10, 0, 0)
        aware_same_instant = datetime(2026, 1, 1, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        earlier_aware = datetime(2026, 1, 1, 9, 59, tzinfo=timezone.utc)

        # Simulate an already-queued legacy item decoded by PyMongo as naive UTC.
        runtime._submission_items.append({
            "key": (100, 1),
            "source_id": 100,
            "message_id": 1,
            "metadata": {},
            "sequence": 1,
            "created_at": naive_utc,
        })
        runtime._queued_keys.add((100, 1))
        runtime._submission_sequence = 1

        runtime._enqueue_job(100, 2, created_at=aware_same_instant)
        runtime._enqueue_job(200, 3, created_at=earlier_aware)

        persisted = [
            {"source_channel_id": 300, "source_message_id": 4, "created_at": datetime(2026, 1, 1, 9, 58)},
            {"source_channel_id": 300, "source_message_id": 5, "created_at": datetime(2026, 1, 1, 10, 1, tzinfo=timezone.utc)},
        ]
        with patch("plugins.rename_runtime.db.get_queue_jobs", new=AsyncMock(return_value=AsyncCursor(persisted))), patch(
            "plugins.rename_runtime.db.get_channel_config", new=AsyncMock(return_value={"thumbnail": b"thumb"})
        ):
            await runtime._restore_queue_items()

        self.assertEqual([item["key"] for item in runtime._submission_items], [
            (300, 4), (200, 3), (100, 1), (100, 2), (300, 5),
        ])
        self.assertTrue(all(item["created_at"].tzinfo is timezone.utc for item in runtime._submission_items))
        self.assertEqual(runtime._submission_items[2]["created_at"], runtime._submission_items[3]["created_at"])

    async def test_global_worker_processes_two_channels_A_through_F_serially(self):
        class AsyncCursor:
            def __init__(self, items=()):
                self.items = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration

        runtime = RenameRuntime()
        jobs = {}
        first_batch = [(100, 1), (100, 2), (100, 3)]
        second_batch = [(200, 4), (200, 5), (200, 6)]
        release = {key: asyncio.Event() for key in first_batch + second_batch}
        started = []
        started_events = {key: asyncio.Event() for key in first_batch + second_batch}

        for index, key in enumerate(first_batch):
            jobs[key] = {"source_channel_id": key[0], "source_message_id": key[1], "status": "pending", "stage": "queued"}
            runtime._enqueue_job(*key, created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc))
        for key in second_batch:
            jobs[key] = {"source_channel_id": key[0], "source_message_id": key[1], "status": "pending", "stage": "queued"}

        runtime.user_client = object()
        runtime.rename_bot_id = 999

        async def fake_process(source_id, message_id):
            key = (source_id, message_id)
            started.append(key)
            started_events[key].set()
            await release[key].wait()  # delivery gate: next job cannot start until publish succeeds
            jobs[key]["status"] = "completed"
            return True

        async def get_job(source_id, message_id):
            return jobs[(source_id, message_id)]

        with patch("plugins.rename_runtime.db.acquire_queue_lease", new=AsyncMock(return_value=True)), patch(
            "plugins.rename_runtime.db.renew_queue_lease", new=AsyncMock(return_value=True)
        ), patch("plugins.rename_runtime.db.release_queue_lease", new=AsyncMock(return_value=True)), patch(
            "plugins.rename_runtime.db.recover_interrupted_jobs", new=AsyncMock()
        ), patch("plugins.rename_runtime.db.restore_superseded_output_jobs", new=AsyncMock(return_value=0)), patch(
            "plugins.rename_runtime.db.get_matched_output_owners", new=AsyncMock(return_value=[])
        ), patch("plugins.rename_runtime.db.get_superseded_jobs", new=AsyncMock(return_value=AsyncCursor())), patch(
            "plugins.rename_runtime.db.get_queue_jobs", new=AsyncMock(return_value=AsyncCursor())
        ), patch("plugins.rename_runtime.db.get_job", new=AsyncMock(side_effect=get_job)), patch.object(
            runtime, "_recover_recent_outputs", new=AsyncMock()
        ), patch.object(runtime, "_process_job", new=AsyncMock(side_effect=fake_process)):
            worker = asyncio.create_task(runtime._submission_worker())
            runtime._submission_worker_task = worker
            await asyncio.wait_for(started_events[first_batch[0]].wait(), timeout=1)
            # Channel 2 arrives while Channel 1's first file is still awaiting delivery.
            for index, key in enumerate(second_batch, start=3):
                runtime._enqueue_job(*key, created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc))
            self.assertEqual(started, [first_batch[0]])

            for key in first_batch + second_batch:
                if not started_events[key].is_set():
                    await asyncio.wait_for(started_events[key].wait(), timeout=1)
                self.assertEqual(started[-1], key)
                release[key].set()

            for _ in range(20):
                if not runtime._submission_items:
                    break
                await asyncio.sleep(0)
            self.assertFalse(runtime._submission_items)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(started, first_batch + second_batch)

    async def test_out_of_order_rename_outputs_match_their_own_waiters(self):
        runtime = RenameRuntime()
        first = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "status": "submitted",
            "expected_filename": "episode-a.mp4",
            "output_reply_ids": [10],
        })
        second = runtime._register_output_waiter({
            "source_channel_id": 200,
            "source_message_id": 2,
            "status": "submitted",
            "expected_filename": "episode-b.mp4",
            "output_reply_ids": [20],
        })
        media_b = SimpleNamespace(id=202, video=SimpleNamespace(file_name="episode-b.mp4"), document=None, reply_to_message_id=20)
        media_a = SimpleNamespace(id=101, video=SimpleNamespace(file_name="episode-a.mp4"), document=None, reply_to_message_id=10)

        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()):
            self.assertTrue(runtime._route_output_message(media_b))
            self.assertTrue(runtime._route_output_message(media_a))
            await asyncio.gather(*list(runtime._background_db_tasks))
        self.assertIs(second["future"].result(), media_b)
        self.assertIs(first["future"].result(), media_a)

    async def test_three_quality_variants_are_matched_and_published_once_out_of_order(self):
        runtime = RenameRuntime()
        qualities = [(1, 480), (2, 1080), (3, 720)]
        jobs = {}
        contexts = {}
        outputs = {}
        for message_id, quality in qualities:
            original = f"Show.S01E04.{quality}p.mkv"
            metadata = extract_video_metadata(original)
            expected = format_standardized_filename("@tag", metadata, original)
            job = {
                "source_channel_id": 100,
                "source_message_id": message_id,
                "job_id": f"100:{message_id}",
                "source_channel_number": 1,
                "output_channel_id": 900,
                "status": "submitted",
                "original_filename": original,
                "original_caption": f"Episode 4, {quality}p",
                "expected_filename": expected,
                "media_metadata": metadata,
                "output_reply_ids": [100 + message_id],
            }
            jobs[message_id] = job
            contexts[message_id] = runtime._register_output_waiter(job)
            outputs[message_id] = SimpleNamespace(
                id=200 + message_id,
                video=SimpleNamespace(file_name=expected, duration=120, width=1920, height=1080),
                document=None,
                caption=f"Renamed {quality}p",
                reply_to_message_id=100 + message_id,
            )

        upload_calls = []

        async def fake_publish(final_message, output_id, thumbnail, expected_filename, *, caption):
            upload_calls.append((final_message.id, output_id, expected_filename, caption))
            return SimpleNamespace(id=9000 + final_message.id)

        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()), patch(
            "plugins.rename_runtime.db.begin_output_publish", new=AsyncMock(return_value=True)
        ) as begin_publish, patch.object(runtime, "_publish_result", new=AsyncMock(side_effect=fake_publish)) as publish:
            for message_id in (3, 1, 2):
                self.assertTrue(runtime._route_output_message(outputs[message_id]))
            # Re-delivery of the same Telegram output message must not assign
            # that already-owned message to another quality variant.
            self.assertTrue(runtime._route_output_message(outputs[3]))
            await asyncio.gather(*list(runtime._background_db_tasks))
            await asyncio.gather(*(
                runtime._track_submitted_output(jobs[mid], {"thumbnail": b"thumb"}, contexts[mid])
                for mid, _quality in qualities
            ))

        self.assertEqual(begin_publish.await_count, 3)
        self.assertEqual(publish.await_count, 3)
        self.assertEqual(len(upload_calls), 3)
        self.assertEqual({call[0] for call in upload_calls}, {201, 202, 203})
        self.assertEqual({call[1] for call in upload_calls}, {900})
        self.assertEqual({call[2] for call in upload_calls}, {
            "[@tag] S1 Ep4 480p.mkv",
            "[@tag] S1 Ep4 1080p.mkv",
            "[@tag] S1 Ep4 720p.mkv",
        })
        self.assertEqual({call[3] for call in upload_calls}, {
            "Episode 4, 480p",
            "Episode 4, 1080p",
            "Episode 4, 720p",
        })

    async def test_metadata_matching_accepts_separator_and_username_suffix_variants(self):
        runtime = RenameRuntime()
        original = "Ultraviolet_Code_044_Episode_12_in_Hindi_dubbed_@SBANIME_480p @username.mp4"
        metadata = extract_video_metadata(original)
        context = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 3,
            "source_channel_number": 4,
            "output_channel_id": 900,
            "status": "submitted",
            "original_filename": original,
            "expected_filename": "[@Sitaratoons] Ep12 480p.mp4",
            "media_metadata": metadata,
            "output_reply_ids": [100],
        })
        returned = SimpleNamespace(
            id=101,
            video=SimpleNamespace(file_name="Ultraviolet.Code.044.Episode_12.in_Hindi_dubbed.@SBANIME.480p.mp4"),
            document=None,
            caption=None,
            reply_to_message_id=None,
        )
        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()):
            self.assertTrue(runtime._route_output_message(returned))
            await asyncio.gather(*list(runtime._background_db_tasks))
        self.assertIs(context["future"].result(), returned)

    async def test_episode_and_quality_alone_do_not_match_different_series(self):
        runtime = RenameRuntime()
        context = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 4,
            "status": "submitted",
            "expected_filename": "[@tag] Ep12 480p.mp4",
            "media_metadata": extract_video_metadata("Ultraviolet_Code_Episode_12_480p.mp4"),
            "output_reply_ids": [100],
        })
        unrelated = SimpleNamespace(
            id=101,
            video=SimpleNamespace(file_name="Another_Show_Episode_12_480p.mp4"),
            document=None,
            caption=None,
            reply_to_message_id=None,
        )
        self.assertFalse(runtime._route_output_message(unrelated))
        self.assertFalse(context["future"].done())

    async def test_unmatched_output_is_reconciled_when_matching_job_arrives(self):
        runtime = RenameRuntime()
        filename = "Ultraviolet.Code.044.Episode_12.in_Hindi_dubbed.@SBANIME.480p.mp4"
        unmatched = SimpleNamespace(
            id=202,
            video=SimpleNamespace(file_name=filename),
            document=None,
            caption=None,
            reply_to_message_id=None,
        )
        runtime._remember_unmatched_output(unmatched)
        metadata = extract_video_metadata("Ultraviolet_Code_044_Episode_12_in_Hindi_dubbed_@SBANIME_480p @username.mp4")
        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()):
            context = runtime._register_output_waiter({
                "source_channel_id": 100,
                "source_message_id": 5,
                "source_channel_number": 1,
                "output_channel_id": 900,
                "status": "submitted",
                "original_filename": "Ultraviolet_Code_044_Episode_12_in_Hindi_dubbed_@SBANIME_480p @username.mp4",
                "expected_filename": "[@tag] Ep12 480p.mp4",
                "media_metadata": metadata,
                "output_reply_ids": [200],
            })
            await asyncio.gather(*list(runtime._background_db_tasks))
        self.assertIs(context["future"].result(), unmatched)
        self.assertEqual(len(runtime._unmatched_outputs), 0)

    async def test_identical_unanchored_filenames_are_not_guessed(self):
        runtime = RenameRuntime()
        first = runtime._register_output_waiter({"source_channel_id": 100, "source_message_id": 1, "status": "submitted", "expected_filename": "same.mp4", "output_reply_ids": [10]})
        second = runtime._register_output_waiter({"source_channel_id": 200, "source_message_id": 2, "status": "submitted", "expected_filename": "same.mp4", "output_reply_ids": [20]})
        media = SimpleNamespace(id=30, video=SimpleNamespace(file_name="same.mp4"), document=None, reply_to_message_id=None)
        self.assertTrue(runtime._route_output_message(media))
        self.assertFalse(first["future"].done())
        self.assertFalse(second["future"].done())
        self.assertTrue(runtime._unmatched_outputs[0]["frozen"])
        first["future"].set_result(SimpleNamespace(id=31))
        runtime._reconcile_unmatched_outputs()
        self.assertFalse(second["future"].done())
        self.assertEqual(runtime._unmatched_outputs[0]["message"], media)

    async def test_delayed_superseded_output_is_recorded_but_not_matched_to_latest(self):
        runtime = RenameRuntime()
        old = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "superseded",
            "expected_filename": "old-episode.mp4",
            "output_reply_ids": [10],
        })
        runtime._output_waiters.pop((100, 1))
        runtime._superseded_waiters[(100, 1)] = old
        latest = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 6,
            "job_id": "100:6",
            "status": "submitted",
            "expected_filename": "latest-episode.mp4",
            "output_reply_ids": [20],
        })
        old_media = SimpleNamespace(
            id=30,
            video=SimpleNamespace(file_name="old-episode.mp4"),
            document=None,
            caption=None,
            reply_to_message_id=10,
        )
        with patch("plugins.rename_runtime.db.add_superseded_output", new=AsyncMock()) as add_old_output:
            self.assertTrue(runtime._route_output_message(old_media))
            await asyncio.gather(*list(runtime._background_db_tasks))
        add_old_output.assert_awaited_once_with(100, 1, 30)
        self.assertFalse(latest["future"].done())

    async def test_new_video_does_not_replace_prior_job_or_unmatched_output(self):
        runtime = RenameRuntime()
        old_context = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "submitted",
            "expected_filename": "old.mp4",
            "output_reply_ids": [10],
        })
        new_context = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 6,
            "job_id": "100:6",
            "status": "submitted",
            "expected_filename": "new.mp4",
            "output_reply_ids": [60],
        })
        old_output = SimpleNamespace(id=101, video=SimpleNamespace(file_name="old.mp4"), document=None, caption=None, reply_to_message_id=10)
        new_output = SimpleNamespace(id=102, video=SimpleNamespace(file_name="new.mp4"), document=None, caption=None, reply_to_message_id=60)
        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()):
            self.assertTrue(runtime._route_output_message(new_output))
            self.assertTrue(runtime._route_output_message(old_output))
            await asyncio.gather(*list(runtime._background_db_tasks))

        self.assertIs(old_context["future"].result(), old_output)
        self.assertIs(new_context["future"].result(), new_output)

    async def test_same_name_as_old_and_latest_request_is_held_without_anchor(self):
        runtime = RenameRuntime()
        old = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "superseded",
            "expected_filename": "same.mp4",
            "output_reply_ids": [10],
        })
        runtime._output_waiters.pop((100, 1))
        runtime._superseded_waiters[(100, 1)] = old
        latest = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 6,
            "job_id": "100:6",
            "status": "submitted",
            "expected_filename": "same.mp4",
            "output_reply_ids": [20],
        })
        media = SimpleNamespace(id=30, video=SimpleNamespace(file_name="same.mp4"), document=None, caption=None, reply_to_message_id=None)
        with patch("plugins.rename_runtime.db.add_superseded_output", new=AsyncMock()) as add_old_output:
            self.assertTrue(runtime._route_output_message(media))
            await asyncio.gather(*list(runtime._background_db_tasks))
        add_old_output.assert_not_awaited()
        self.assertFalse(latest["future"].done())
        self.assertEqual(runtime._unmatched_outputs[0]["message"], media)
        self.assertTrue(runtime._unmatched_outputs[0]["frozen"])

    async def test_latest_reply_anchor_routes_output_despite_stale_metadata_similarity(self):
        runtime = RenameRuntime()
        metadata = extract_video_metadata("Show.S01E04.1080p.mp4")
        old = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "job_id": "100:1",
            "status": "superseded",
            "expected_filename": "old-name.mp4",
            "media_metadata": metadata,
            "output_reply_ids": [10],
        })
        runtime._output_waiters.pop((100, 1))
        runtime._superseded_waiters[(100, 1)] = old
        latest = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 6,
            "job_id": "100:6",
            "status": "submitted",
            "expected_filename": "latest-name.mp4",
            "media_metadata": metadata,
            "output_reply_ids": [20],
        })
        media = SimpleNamespace(
            id=30,
            video=SimpleNamespace(file_name="latest-name.mp4"),
            document=None,
            caption=None,
            reply_to_message_id=20,
        )
        with patch("plugins.rename_runtime.db.update_job", new=AsyncMock()):
            self.assertTrue(runtime._route_output_message(media))
            await asyncio.gather(*list(runtime._background_db_tasks))
        self.assertIs(latest["future"].result(), media)

    async def test_unanchored_media_is_not_assigned_to_the_only_waiter(self):
        runtime = RenameRuntime()
        context = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 6,
            "status": "submitted",
            "expected_filename": "latest.mp4",
            "output_reply_ids": [20],
        })
        media = SimpleNamespace(id=30, video=SimpleNamespace(file_name=None), document=None, caption=None, reply_to_message_id=None)
        self.assertFalse(runtime._route_output_message(media))
        self.assertFalse(context["future"].done())


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

    async def test_non_video_document_uses_send_document(self):
        runtime = RenameRuntime()

        async def download(file_name):
            with open(file_name, "wb") as handle:
                handle.write(b"document bytes")
            return file_name

        final_message = SimpleNamespace(
            video=None,
            document=SimpleNamespace(file_name="result.pdf", mime_type="application/pdf"),
            caption="Episode notes",
            caption_entities=None,
            download=AsyncMock(side_effect=download),
            copy=AsyncMock(),
        )
        runtime.user_client = SimpleNamespace(send_video=AsyncMock(), send_document=AsyncMock(return_value=SimpleNamespace(id=321)))
        result = await runtime._publish_result(final_message, -100202, b"thumb", "result.pdf")
        self.assertEqual(result.id, 321)
        runtime.user_client.send_document.assert_awaited_once()
        runtime.user_client.send_video.assert_not_awaited()


class OutputTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_is_completed_only_after_output_upload_succeeds(self):
        runtime = RenameRuntime()
        job = {"source_channel_id": 100, "source_message_id": 7, "output_channel_id": 900, "status": "submitted", "expected_filename": "episode.mp4", "original_caption": "source caption"}
        context = runtime._register_output_waiter(job)
        final = SimpleNamespace(id=501)
        context["future"].set_result(final)
        events = []
        uploaded = SimpleNamespace(id=901)

        async def begin(source_id, message_id, output_message_id):
            events.append(("begin", source_id, message_id, output_message_id))
            return True

        async def publish(*_args, **_kwargs):
            events.append(("upload",))
            return uploaded

        async def update(source_id, message_id, **fields):
            events.append(("update", fields["status"]))

        with patch("plugins.rename_runtime.db.begin_output_publish", new=AsyncMock(side_effect=begin)), patch(
            "plugins.rename_runtime.db.update_job", new=AsyncMock(side_effect=update)
        ) as update_job, patch.object(runtime, "_publish_result", new=AsyncMock(side_effect=publish)) as publish_result:
            await runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context)

        self.assertEqual([event[0] for event in events], ["begin", "upload", "update"])
        self.assertEqual(events[-1], ("update", "completed"))
        self.assertEqual(update_job.await_args.kwargs["output_message_id"], 901)
        self.assertEqual(update_job.await_args.kwargs["destination_message_id"], 901)
        self.assertEqual(update_job.await_args.kwargs["rename_bot_output_message_id"], 501)
        self.assertEqual(update_job.await_args.kwargs["publication_status"], "published")
        publish_result.assert_awaited_once_with(final, 900, b"thumb", "episode.mp4", caption="source caption")

    async def test_upload_failure_is_logged_and_job_stays_uncompleted(self):
        runtime = RenameRuntime()
        job = {"source_channel_id": 100, "source_message_id": 8, "output_channel_id": 900, "status": "submitted", "expected_filename": "episode.mp4"}
        context = runtime._register_output_waiter(job)
        context["future"].set_result(SimpleNamespace(id=502))
        with patch("plugins.rename_runtime.db.begin_output_publish", new=AsyncMock(return_value=True)), patch(
            "plugins.rename_runtime.db.update_job", new=AsyncMock()
        ) as update_job, patch.object(runtime, "_publish_result", new=AsyncMock(side_effect=RuntimeError("output permission denied"))):
            await runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context)
        self.assertEqual(update_job.await_args.kwargs["status"], "failed")
        self.assertEqual(update_job.await_args.kwargs["publication_status"], "failed")
        self.assertEqual(update_job.await_args.kwargs["stage"], "publishing")
        self.assertIn("output permission denied", update_job.await_args.kwargs["error"])

    async def test_media_timeout_keeps_waiter_alive_for_late_output(self):
        runtime = RenameRuntime()
        job = {
            "source_channel_id": 100,
            "source_message_id": 9,
            "output_channel_id": 900,
            "status": "submitted",
            "expected_filename": "late.mp4",
            "output_reply_ids": [10],
        }
        context = runtime._register_output_waiter(job)
        media = SimpleNamespace(id=11, video=SimpleNamespace(file_name="late.mp4"), document=None, reply_to_message_id=10)
        with patch("plugins.rename_runtime.RENAME_RESPONSE_TIMEOUT", 0.01), patch(
            "plugins.rename_runtime.db.begin_output_publish", new=AsyncMock(return_value=True)
        ), patch("plugins.rename_runtime.db.update_job", new=AsyncMock()) as update_job, patch.object(
            runtime, "_publish_result", new=AsyncMock(return_value=SimpleNamespace(id=901))
        ):
            task = asyncio.create_task(runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context))
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            self.assertFalse(context["future"].done())
            self.assertTrue(runtime._route_output_message(media))
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(update_job.await_args.kwargs["status"], "completed")

    async def test_correlated_rename_bot_error_fails_output_job_without_upload(self):
        runtime = RenameRuntime()
        job = {
            "source_channel_id": 100,
            "source_message_id": 10,
            "output_channel_id": 900,
            "status": "submitted",
            "expected_filename": "bad.mp4",
            "output_reply_ids": [20],
        }
        context = runtime._register_output_waiter(job)
        error_message = SimpleNamespace(
            id=21,
            text="Rename failed: invalid filename",
            caption=None,
            video=None,
            document=None,
            reply_to_message_id=20,
            reply_markup=None,
        )
        with patch("plugins.rename_runtime.db.get_job", new=AsyncMock(return_value={"status": "submitted"})), patch(
            "plugins.rename_runtime.db.update_job", new=AsyncMock()
        ) as update_job, patch("plugins.rename_runtime.db.begin_output_publish", new=AsyncMock()) as begin, patch.object(
            runtime, "_publish_result", new=AsyncMock()
        ) as publish:
            task = asyncio.create_task(runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context))
            await runtime._on_rename_bot_message(None, error_message)
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(update_job.await_args.kwargs["status"], "failed")
        self.assertEqual(update_job.await_args.kwargs["stage"], "rename_bot_error")
        begin.assert_not_awaited()
        publish.assert_not_awaited()


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
        database.configs = SimpleNamespace(
            find_one=AsyncMock(side_effect=[None, {"channel_number": 7}]),
            update_one=AsyncMock(),
        )
        database.settings = SimpleNamespace(find_one_and_update=AsyncMock(return_value={"value": 7}))
        channel_number = await database.set_channel_config(-100101, -100202, b"source-a-thumbnail")
        call = database.configs.update_one.await_args
        self.assertEqual(channel_number, 7)
        self.assertEqual(call.args[0], {"source_channel_id": -100101})
        self.assertEqual(call.kwargs["upsert"], True)
        self.assertEqual(call.args[1]["$set"]["output_channel_id"], -100202)
        self.assertEqual(call.args[1]["$set"]["thumbnail"], b"source-a-thumbnail")
        self.assertEqual(call.args[1]["$setOnInsert"]["channel_number"], 7)

    async def test_reconfiguring_a_source_preserves_its_channel_number(self):
        database = Database.__new__(Database)
        database.configs = SimpleNamespace(
            find_one=AsyncMock(return_value={"channel_number": 7}),
            update_one=AsyncMock(),
        )
        database.settings = SimpleNamespace(find_one_and_update=AsyncMock())
        self.assertEqual(await database.set_channel_config(-100101, -100303), 7)
        database.settings.find_one_and_update.assert_not_awaited()

    async def test_legacy_channel_migration_allocates_stable_numbers(self):
        class Cursor:
            def __init__(self, items):
                self.items = list(items)

            def sort(self, *_args):
                return self

            def limit(self, *_args):
                return self

            async def to_list(self, length=None):
                return self.items[:length] if length else self.items

            def __aiter__(self):
                self.items = iter(self.items)
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration

        configs = SimpleNamespace(
            find=Mock(side_effect=[Cursor([]), Cursor([
                {"source_channel_id": -1001},
                {"source_channel_id": -1002},
            ])]),
            find_one=AsyncMock(side_effect=[{"source_channel_id": -1001}, {"source_channel_id": -1002}]),
            update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
        )
        database = Database.__new__(Database)
        database.configs = configs
        database.settings = SimpleNamespace(
            find_one_and_update=AsyncMock(side_effect=[{"value": 1}, {"value": 2}]),
            update_one=AsyncMock(),
        )

        await database._migrate_channel_numbers()

        self.assertEqual(configs.update_one.await_count, 2)
        self.assertEqual(configs.update_one.await_args_list[0].args[1]["$set"]["channel_number"], 1)
        self.assertEqual(configs.update_one.await_args_list[1].args[1]["$set"]["channel_number"], 2)


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
        self.assertEqual(update["$set"]["status"], "submitting")
        self.assertEqual(update["$set"]["workflow_version"], 3)
        self.assertEqual(update["$inc"]["attempts"], 1)

    async def test_startup_installs_compound_unique_source_message_index(self):
        database = Database.__new__(Database)
        database._client = SimpleNamespace(admin=SimpleNamespace(command=AsyncMock()))
        database.configs = SimpleNamespace(create_index=AsyncMock())
        database.jobs = SimpleNamespace(create_index=AsyncMock())
        database._migrate_channel_numbers = AsyncMock()

        await database.init()

        database.configs.create_index.assert_any_await("channel_number", unique=True)
        database.jobs.create_index.assert_any_await(
            [("source_channel_id", 1), ("source_message_id", 1)],
            unique=True,
        )
        database.jobs.create_index.assert_any_await(
            "rename_bot_output_message_id",
            unique=True,
            partialFilterExpression={"rename_bot_output_message_id": {"$type": "number"}},
        )

    async def test_restart_recovery_resumes_only_safe_preflight_and_output_waiters(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_many=AsyncMock())
        await database.recover_interrupted_jobs(max_attempts=3)
        self.assertEqual(database.jobs.update_many.await_count, 7)
        calls = database.jobs.update_many.await_args_list
        self.assertEqual(calls[0].args[0], {"status": "submitting", "workflow_version": 3, "stage": "preflight"})
        self.assertEqual(calls[0].args[1]["$set"]["status"], "pending")
        self.assertEqual(calls[1].args[0]["stage"], {"$in": ["callback_submitting", "callback_unknown"]})
        self.assertEqual(calls[1].args[1]["$set"]["status"], "submission_unknown")
        self.assertEqual(calls[2].args[0]["stage"]["$nin"], ["preflight", "callback_submitting", "callback_unknown"])
        self.assertEqual(calls[2].args[1]["$set"]["status"], "failed")
        self.assertEqual(calls[3].args[0], {"status": "publishing"})
        self.assertEqual(calls[4].args[0]["workflow_version"], {"$ne": 3})
        self.assertEqual(calls[4].args[0]["attempts"]["$gt"], 0)
        self.assertEqual(calls[5].args[0], {"status": "submitting", "workflow_version": {"$ne": 3}})
        self.assertEqual(calls[6].args[0]["stage"], "preflight")
        self.assertEqual(calls[6].args[0]["attempts"]["$lt"], 3)

    async def test_submitted_output_jobs_are_loaded_without_requeueing(self):
        database = Database.__new__(Database)
        cursor = SimpleNamespace(sort=Mock())
        database.jobs = SimpleNamespace(find=Mock(return_value=cursor))
        await database.get_output_jobs()
        database.jobs.find.assert_called_once_with({"status": {"$in": ["submitted", "submission_unknown"]}})
        cursor.sort.assert_called_once_with([("submitted_at", 1), ("created_at", 1), ("_id", 1)])

    async def test_global_queue_query_includes_pending_output_and_failed_barriers_in_fifo_order(self):
        database = Database.__new__(Database)
        cursor = SimpleNamespace(sort=Mock())
        cursor.sort.return_value = cursor
        database.jobs = SimpleNamespace(find=Mock(return_value=cursor))
        self.assertIs(await database.get_queue_jobs(), cursor)
        database.jobs.find.assert_called_once_with({
            "status": {"$in": ["pending", "submitted", "submission_unknown", "failed"]},
            "stage": {"$ne": "cancelled"},
        })
        cursor.sort.assert_called_once_with([("created_at", 1), ("_id", 1)])

    async def test_global_queue_lease_is_owned_and_renewed_atomically(self):
        database = Database.__new__(Database)
        database.settings = SimpleNamespace(
            find_one_and_update=AsyncMock(return_value={"owner": "worker-a"}),
            insert_one=AsyncMock(),
            update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
        )
        self.assertTrue(await database.acquire_queue_lease("worker-a", lease_seconds=60))
        query = database.settings.find_one_and_update.await_args.args[0]
        self.assertEqual(query["_id"], "global_rename_queue_lease")
        self.assertEqual(query["$or"][1], {"owner": "worker-a"})
        self.assertTrue(await database.renew_queue_lease("worker-a", lease_seconds=60))
        self.assertTrue(await database.release_queue_lease("worker-a"))
        self.assertEqual(database.settings.update_one.await_count, 2)

    async def test_global_queue_lease_contention_does_not_overwrite_another_worker(self):
        database = Database.__new__(Database)
        database.settings = SimpleNamespace(
            find_one_and_update=AsyncMock(return_value=None),
            insert_one=AsyncMock(side_effect=DuplicateKeyError("lease exists")),
        )
        self.assertFalse(await database.acquire_queue_lease("worker-b", lease_seconds=60))
        database.settings.insert_one.assert_awaited_once()

    async def test_matched_output_owner_lookup_restores_global_message_identity(self):
        owner = {
            "source_channel_id": 100,
            "source_message_id": 6,
            "rename_bot_output_message_id": 987,
        }
        cursor = SimpleNamespace(to_list=AsyncMock(return_value=[owner]))
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(find=Mock(return_value=cursor))
        self.assertEqual(await database.get_matched_output_owners(), [owner])
        database.jobs.find.assert_called_once_with(
            {"rename_bot_output_message_id": {"$type": "number"}},
            {"source_channel_id": 1, "source_message_id": 1, "rename_bot_output_message_id": 1},
        )
        cursor.to_list.assert_awaited_once_with(length=None)

    async def test_superseded_jobs_are_queryable_for_restart_correlation(self):
        database = Database.__new__(Database)
        cursor = SimpleNamespace(sort=Mock(return_value=None))
        # Motor cursors return themselves from sort; use a fluent test double.
        cursor.sort.return_value = cursor
        database.jobs = SimpleNamespace(find=Mock(return_value=cursor))
        result = await database.get_superseded_jobs()
        self.assertIs(result, cursor)
        database.jobs.find.assert_called_once_with({"status": "superseded"})
        cursor.sort.assert_called_once_with([("superseded_at", -1), ("created_at", -1), ("_id", -1)])

    async def test_restore_superseded_jobs_only_after_callback_attempt(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_many=AsyncMock(return_value=SimpleNamespace(modified_count=2)))
        self.assertEqual(await database.restore_superseded_output_jobs(), 2)
        query, update = database.jobs.update_many.await_args.args
        self.assertEqual(query, {"status": "superseded", "callback_attempted_at": {"$exists": True}})
        self.assertEqual(update["$set"]["status"], "submission_unknown")
        self.assertEqual(update["$set"]["submission_status"], "submission_unknown")
        self.assertEqual(update["$set"]["stage"], "callback_unknown")
        self.assertIn("rename actions were not repeated", update["$set"]["error"])

    async def test_output_publish_claim_is_atomic_for_submitted_states(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
        self.assertTrue(await database.begin_output_publish(100, 7, 999))
        query, update = database.jobs.update_one.await_args.args
        self.assertEqual(query["status"], {"$in": ["submitted", "submission_unknown"]})
        self.assertEqual(query["source_channel_id"], 100)
        self.assertEqual(query["source_message_id"], 7)
        self.assertEqual(update["$set"]["status"], "publishing")
        self.assertEqual(update["$set"]["submission_status"], "submitted")
        self.assertEqual(update["$set"]["publication_status"], "publishing")
        self.assertEqual(update["$set"]["output_candidate_message_id"], 999)

    async def test_output_message_id_already_claimed_by_another_job_cannot_publish_again(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(side_effect=DuplicateKeyError("duplicate output ID")))
        self.assertFalse(await database.begin_output_publish(100, 8, 999))

    async def test_job_stage_transition_is_atomic_and_requires_expected_state(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
        self.assertTrue(await database.advance_job_stage(-100111, 42, "preflight", "thumbnail_sending"))
        call = database.jobs.update_one.await_args
        self.assertEqual(call.args[0]["status"], {"$in": ["submitting", "processing"]})
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
        metadata = {"season": 1, "episode": 2, "quality": 480, "group_key": "series s01e02"}
        self.assertTrue(await database.create_job(
            -100111,
            42,
            -100222,
            media_metadata=metadata,
            channel_number=6,
            original_filename="Series_S01E02_480p.mkv",
            original_caption="Original caption",
            expected_filename="[@tag] S1 Ep2 480p.mkv",
        ))
        payload = database.jobs.insert_one.await_args.args[0]
        self.assertEqual(payload["job_id"], "-100111:42")
        self.assertEqual(payload["source_channel_number"], 6)
        self.assertEqual(payload["source_channel_id"], -100111)
        self.assertEqual(payload["source_message_id"], 42)
        self.assertEqual(payload["output_channel_id"], -100222)
        self.assertEqual(payload["destination_channel_id"], -100222)
        self.assertEqual(payload["original_filename"], "Series_S01E02_480p.mkv")
        self.assertEqual(payload["original_caption"], "Original caption")
        self.assertEqual(payload["expected_filename"], "[@tag] S1 Ep2 480p.mkv")
        self.assertEqual(payload["submission_status"], "queued")
        self.assertEqual(payload["publication_status"], "pending")
        self.assertEqual(payload["expected_output_message_ids"], [])
        self.assertIsNone(payload["destination_message_id"])
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["workflow_version"], 3)
        self.assertEqual(payload["media_metadata"], metadata)


if __name__ == "__main__":
    unittest.main()
