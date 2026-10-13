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
from plugins.filename_utils import append_username, extract_original_filename, extract_video_metadata
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

    async def test_acknowledged_submission_does_not_wait_for_output_media(self):
        runtime = RenameRuntime()
        source_message = SimpleNamespace(video=SimpleNamespace(file_name="episode.mp4"), document=None)
        job = {"attempts": 1, "output_channel_id": -100202}
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
        with patch("plugins.rename_runtime.db.get_pending_jobs", new=AsyncMock(return_value=cursor)) as get_pending, patch(
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
        with patch("plugins.rename_runtime.db.get_pending_jobs", new=AsyncMock(return_value=AsyncCursor([]))), patch(
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


class SharedQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_messages_from_connected_channels_enqueue_once_by_compound_key(self):
        runtime = RenameRuntime()
        configs = {
            100: {"output_channel_id": 900, "thumbnail": b"thumb-a"},
            200: {"output_channel_id": 901, "thumbnail": b"thumb-b"},
        }
        with patch("plugins.rename_runtime.db.get_channel_config", new=AsyncMock(side_effect=configs.get)), patch(
            "plugins.rename_runtime.db.create_job", new=AsyncMock(side_effect=[True, True, False])
        ) as create_job, patch.object(runtime, "_enqueue_job") as enqueue:
            messages = [
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=1, video=SimpleNamespace(file_name="A S01E01 480p.mp4"), document=None),
                SimpleNamespace(chat=SimpleNamespace(id=200, type=enums.ChatType.CHANNEL), id=2, video=None, document=SimpleNamespace(file_name="B S01E02 1080p.mkv")),
                SimpleNamespace(chat=SimpleNamespace(id=100, type=enums.ChatType.CHANNEL), id=1, video=SimpleNamespace(file_name="A S01E01 480p.mp4"), document=None),
            ]
            await asyncio.gather(*(runtime._on_source_message(None, message) for message in messages))

        self.assertEqual(create_job.await_count, 3)
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(enqueue.call_args_list[0].args[:2], (100, 1))
        self.assertEqual(enqueue.call_args_list[1].args[:2], (200, 2))
        self.assertEqual(create_job.await_args_list[0].kwargs["media_metadata"]["quality"], 480)
        self.assertEqual(create_job.await_args_list[1].kwargs["media_metadata"]["quality"], 1080)

    async def test_next_channel_submits_before_previous_output_is_available(self):
        runtime = RenameRuntime()
        runtime.user_client = SimpleNamespace()
        runtime.rename_bot_id = 999
        first_started = asyncio.Event()
        release_first_submission = asyncio.Event()
        second_started = asyncio.Event()
        submissions = []
        slow_output_future = asyncio.get_running_loop().create_future()

        async def submit(source_id, message_id):
            submissions.append((source_id, message_id))
            if message_id == 1:
                first_started.set()
                await release_first_submission.wait()
                # Models A being accepted while its final file is still absent.
                self.assertFalse(slow_output_future.done())
            else:
                second_started.set()

        runtime._process_job = submit
        self.assertTrue(runtime._enqueue_job(100, 1))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        self.assertFalse(runtime._enqueue_job(100, 1))
        self.assertTrue(runtime._enqueue_job(200, 2))
        release_first_submission.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        self.assertEqual(submissions, [(100, 1), (200, 2)])
        self.assertFalse(slow_output_future.done())

        worker = runtime._submission_worker_task
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_quality_priority_reorders_only_related_queued_variants(self):
        runtime = RenameRuntime()
        with patch("plugins.rename_runtime.QUALITY_PRIORITY_ENABLED", True):
            runtime._enqueue_job(100, 1, extract_video_metadata("Show S01E01 1080p.mkv"))
            runtime._enqueue_job(100, 2, extract_video_metadata("Show S01E01 720p.mkv"))
            runtime._enqueue_job(100, 3, extract_video_metadata("Show S01E01 480p.mkv"))
            runtime._enqueue_job(200, 4, extract_video_metadata("Different.Movie.1080p.mp4"))
            ordered = [runtime._pop_next_submission()["message_id"] for _ in range(4)]
        self.assertEqual(ordered, [3, 2, 1, 4])

    async def test_out_of_order_rename_outputs_match_their_own_waiters(self):
        runtime = RenameRuntime()
        first = runtime._register_output_waiter({
            "source_channel_id": 100,
            "source_message_id": 1,
            "expected_filename": "episode-a.mp4",
            "output_reply_ids": [10],
        })
        second = runtime._register_output_waiter({
            "source_channel_id": 200,
            "source_message_id": 2,
            "expected_filename": "episode-b.mp4",
            "output_reply_ids": [20],
        })
        media_b = SimpleNamespace(id=202, video=SimpleNamespace(file_name="episode-b.mp4"), document=None, reply_to_message_id=20)
        media_a = SimpleNamespace(id=101, video=SimpleNamespace(file_name="episode-a.mp4"), document=None, reply_to_message_id=10)

        self.assertTrue(runtime._route_output_message(media_b))
        self.assertTrue(runtime._route_output_message(media_a))
        self.assertIs(second["future"].result(), media_b)
        self.assertIs(first["future"].result(), media_a)

    async def test_identical_unanchored_filenames_are_not_guessed(self):
        runtime = RenameRuntime()
        first = runtime._register_output_waiter({"source_channel_id": 100, "source_message_id": 1, "expected_filename": "same.mp4", "output_reply_ids": [10]})
        second = runtime._register_output_waiter({"source_channel_id": 200, "source_message_id": 2, "expected_filename": "same.mp4", "output_reply_ids": [20]})
        media = SimpleNamespace(id=30, video=SimpleNamespace(file_name="same.mp4"), document=None, reply_to_message_id=None)
        self.assertFalse(runtime._route_output_message(media))
        self.assertFalse(first["future"].done())
        self.assertFalse(second["future"].done())


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
        job = {"source_channel_id": 100, "source_message_id": 7, "output_channel_id": 900, "expected_filename": "episode.mp4"}
        context = runtime._register_output_waiter(job)
        final = SimpleNamespace(id=501)
        context["future"].set_result(final)
        events = []
        uploaded = SimpleNamespace(id=901)

        async def begin(source_id, message_id, output_message_id):
            events.append(("begin", source_id, message_id, output_message_id))
            return True

        async def publish(*_args):
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
        publish_result.assert_awaited_once_with(final, 900, b"thumb", "episode.mp4")

    async def test_upload_failure_is_logged_and_job_stays_uncompleted(self):
        runtime = RenameRuntime()
        job = {"source_channel_id": 100, "source_message_id": 8, "output_channel_id": 900, "expected_filename": "episode.mp4"}
        context = runtime._register_output_waiter(job)
        context["future"].set_result(SimpleNamespace(id=502))
        with patch("plugins.rename_runtime.db.begin_output_publish", new=AsyncMock(return_value=True)), patch(
            "plugins.rename_runtime.db.update_job", new=AsyncMock()
        ) as update_job, patch.object(runtime, "_publish_result", new=AsyncMock(side_effect=RuntimeError("output permission denied"))):
            await runtime._track_submitted_output(job, {"thumbnail": b"thumb"}, context)
        self.assertEqual(update_job.await_args.kwargs["status"], "failed")
        self.assertEqual(update_job.await_args.kwargs["stage"], "publishing")
        self.assertIn("output permission denied", update_job.await_args.kwargs["error"])

    async def test_media_timeout_keeps_waiter_alive_for_late_output(self):
        runtime = RenameRuntime()
        job = {
            "source_channel_id": 100,
            "source_message_id": 9,
            "output_channel_id": 900,
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
        self.assertEqual(update["$set"]["status"], "submitting")
        self.assertEqual(update["$set"]["workflow_version"], 3)
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

    async def test_output_publish_claim_is_atomic_for_submitted_states(self):
        database = Database.__new__(Database)
        database.jobs = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
        self.assertTrue(await database.begin_output_publish(100, 7, 999))
        query, update = database.jobs.update_one.await_args.args
        self.assertEqual(query["status"], {"$in": ["submitted", "submission_unknown"]})
        self.assertEqual(query["source_channel_id"], 100)
        self.assertEqual(query["source_message_id"], 7)
        self.assertEqual(update["$set"]["status"], "publishing")
        self.assertEqual(update["$set"]["output_candidate_message_id"], 999)

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
        self.assertTrue(await database.create_job(-100111, 42, -100222, media_metadata=metadata))
        payload = database.jobs.insert_one.await_args.args[0]
        self.assertEqual(payload["source_channel_id"], -100111)
        self.assertEqual(payload["source_message_id"], 42)
        self.assertEqual(payload["output_channel_id"], -100222)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["workflow_version"], 3)
        self.assertEqual(payload["media_metadata"], metadata)


if __name__ == "__main__":
    unittest.main()
