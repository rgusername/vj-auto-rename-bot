"""Event-driven rename orchestration using the preserved logged-in user session."""

import asyncio
import io
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from pyrogram import Client, filters, enums
from pyrogram.errors import BotResponseTimeout, FloodWait
from pyrogram.handlers import EditedMessageHandler, MessageHandler

from config import (
    API_HASH,
    API_ID,
    ADMINS,
    MAX_RENAME_ATTEMPTS,
    RENAME_BOT_USERNAME,
    RENAME_RESPONSE_TIMEOUT,
    SESSION_OWNER_ID,
)
from plugins.database import db
from plugins.filename_utils import append_username, extract_original_filename, sanitize_filename

logger = logging.getLogger(__name__)
_VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".ogv", ".ts", ".webm", ".wmv",
})


def select_second_inline_button(markup):
    """Return the second actual button in row-major keyboard order."""
    keyboard = getattr(markup, "inline_keyboard", None)
    if not isinstance(keyboard, (list, tuple)) or not keyboard:
        raise ValueError("Rename bot returned no inline keyboard.")
    buttons = [
        button
        for row in keyboard
        if isinstance(row, (list, tuple))
        for button in row
        if button is not None
    ]
    if len(buttons) < 2:
        raise ValueError("Rename bot returned fewer than two inline buttons.")
    button = buttons[1]
    callback_data = getattr(button, "callback_data", None)
    if not isinstance(callback_data, str) or not callback_data:
        raise ValueError("The second inline button has no callback data; it cannot be clicked safely.")
    return button


class RenameRuntime:
    def __init__(self):
        self.bot_client = None
        self.user_client = None
        self.rename_bot_id = None
        self.active_session_owner_id = 0
        self._rename_messages = asyncio.Queue()
        self._conversation_lock = asyncio.Lock()
        self._jobs: dict[tuple[int, int], asyncio.Task] = {}
        self._handler_group = 70

    @property
    def session_owner_id(self) -> int:
        return SESSION_OWNER_ID or (ADMINS[0] if ADMINS else 0)

    @property
    def logged_in(self) -> bool:
        return self.user_client is not None and bool(getattr(self.user_client, "is_connected", False))

    async def start(self, bot_client: Client):
        self.bot_client = bot_client
        if not RENAME_BOT_USERNAME:
            logger.warning("RENAME_BOT_USERNAME is unset; source monitoring is disabled until configured.")
        if not self.session_owner_id:
            logger.warning("SESSION_OWNER_ID is unset and ADMINS is empty; no saved user session can be restored.")
            return False
        session = await db.get_session(self.session_owner_id)
        if not session:
            logger.info("No saved user-account session for SESSION_OWNER_ID=%s; /login remains available.", self.session_owner_id)
            return False
        return await self.start_user_session(session)

    async def ensure_user_session(self, user_id: int) -> bool:
        """Reuse a live account or restore this administrator's saved session on demand."""
        user_id = int(user_id)
        if self.user_client and getattr(self.user_client, "is_connected", False):
            try:
                account = await self.user_client.get_me()
                if account and int(account.id) == user_id:
                    self.active_session_owner_id = user_id
                    return True
            except Exception:
                logger.warning("The current Telegram user session is no longer valid; attempting saved-session restore.", exc_info=True)

        session = await db.get_session(user_id)
        if not session:
            return False
        if self.user_client:
            await self.stop_user_session()
        return await self.start_user_session(session, owner_id=user_id)

    async def start_user_session(self, session: str | None = None, owner_id: int | None = None):
        owner_id = int(owner_id or self.session_owner_id)
        if self.user_client and getattr(self.user_client, "is_connected", False):
            try:
                account = await self.user_client.get_me()
                if account and int(account.id) == owner_id:
                    self.active_session_owner_id = owner_id
                    return True
            except Exception:
                logger.warning("Existing Telegram user session is not healthy; replacing it.", exc_info=True)
            await self.stop_user_session()
        session = session or await db.get_session(owner_id)
        if not session:
            return False
        client = Client(
            ":memory:",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session,
            workers=8,
            sleep_threshold=30,
        )
        try:
            await client.start()
            self.rename_bot_id = None
            if RENAME_BOT_USERNAME:
                try:
                    rename_peer = await client.get_users(RENAME_BOT_USERNAME)
                    self.rename_bot_id = rename_peer.id
                    bot_filter = filters.private & filters.incoming & filters.user(self.rename_bot_id)
                    client.add_handler(MessageHandler(self._on_rename_bot_message, bot_filter), group=self._handler_group)
                    client.add_handler(EditedMessageHandler(self._on_rename_bot_message, bot_filter), group=self._handler_group)
                    client.add_handler(
                        MessageHandler(self._on_source_message, filters.channel & (filters.video | filters.document)),
                        group=self._handler_group + 1,
                    )
                except Exception:
                    self.rename_bot_id = None
                    logger.warning("Could not resolve RENAME_BOT_USERNAME; saved session remains available for /set channel validation.", exc_info=True)
            else:
                logger.info("RENAME_BOT_USERNAME is unset; saved session will be available for /set, but media monitoring is disabled.")
            self.user_client = client
            self.active_session_owner_id = owner_id
            await db.recover_interrupted_jobs(MAX_RENAME_ATTEMPTS)
            logger.info("Logged-in user session started; monitoring configured source channels.")
            await self.resume_pending_jobs()
            return True
        except Exception:
            logger.exception("Could not restore/start the logged-in user session or resolve the rename bot.")
            try:
                await client.stop()
            except Exception:
                pass
            self.user_client = None
            self.active_session_owner_id = 0
            return False

    async def stop_user_session(self):
        client, self.user_client = self.user_client, None
        self.rename_bot_id = None
        self.active_session_owner_id = 0
        if client and getattr(client, "is_connected", False):
            try:
                await client.stop()
            except Exception:
                logger.exception("Error while stopping the logged-in user session.")
        tasks = list(self._jobs.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()

    async def _on_rename_bot_message(self, _client, message):
        await self._rename_messages.put(message)

    async def _on_source_message(self, _client, message):
        if not message.chat or message.chat.type != enums.ChatType.CHANNEL:
            return
        try:
            config = await db.get_channel_config(message.chat.id)
            if not config:
                return
            created = await db.create_job(message.chat.id, message.id, config["output_channel_id"])
        except Exception:
            logger.exception("Could not load source configuration or record source=%s message=%s", message.chat.id, message.id)
            return
        if not created or not config.get("thumbnail"):
            # /set persists the mapping before the admin sends the image; retain any
            # source messages as pending until the per-channel thumbnail is available.
            return
        key = (message.chat.id, message.id)
        self._launch_job(key, message.chat.id, message.id)

    def _launch_job(self, key: tuple[int, int], source_id: int, message_id: int):
        current = self._jobs.get(key)
        if current and not current.done():
            return
        task = asyncio.create_task(self._process_job(source_id, message_id), name=f"rename:{source_id}:{message_id}")
        self._jobs[key] = task
        task.add_done_callback(
            lambda completed, job_key=key: self._jobs.pop(job_key, None)
            if self._jobs.get(job_key) is completed else None
        )

    async def resume_pending_jobs(self):
        if not self.user_client:
            return
        pending_jobs = await db.get_pending_jobs()
        async for job in pending_jobs:
            source_id = int(job["source_channel_id"])
            config = await db.get_channel_config(source_id)
            if not config or not config.get("thumbnail"):
                continue
            key = (source_id, int(job["source_message_id"]))
            self._launch_job(key, *key)

    async def cancel_active(self) -> bool:
        active = [task for task in self._jobs.values() if not task.done()]
        if not active:
            return False
        for task in active:
            task.cancel()
        return True

    async def _next_rename_message(
        self,
        deadline: float,
        *,
        after_message_id: int | None = None,
        reply_to_ids: set[int] | None = None,
    ):
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Timed out waiting for the rename bot.")
            message = await asyncio.wait_for(self._rename_messages.get(), timeout=remaining)
            if not message.chat or message.chat.id != self.rename_bot_id:
                continue
            if after_message_id is not None and int(getattr(message, "id", 0)) <= int(after_message_id):
                continue
            body = message.text or message.caption or ""
            if re.search(r"auto[\s-]*delete\s+notice", body, re.IGNORECASE):
                logger.debug("Ignoring rename-bot auto-delete notice message_id=%s", getattr(message, "id", None))
                continue
            reply_to_id = self._reply_to_message_id(message)
            if reply_to_id is not None and reply_to_ids is not None and int(reply_to_id) not in reply_to_ids:
                continue
            return message

    @staticmethod
    def _reply_to_message_id(message) -> int | None:
        reply_to_id = getattr(message, "reply_to_message_id", None)
        if reply_to_id is None:
            reply = getattr(message, "reply_to_message", None)
            reply_to_id = getattr(reply, "id", None) if reply else None
        return int(reply_to_id) if reply_to_id is not None else None

    async def _advance_job_stage(self, source_id: int, message_id: int, expected: str, stage: str, **fields):
        if not await db.advance_job_stage(source_id, message_id, expected, stage, **fields):
            raise RuntimeError(f"Rename job state changed unexpectedly ({expected} → {stage}); stopping to avoid duplicate actions.")

    @staticmethod
    def _safe_to_retry(stage: str | None, attempts: int) -> bool:
        """Only retry preflight failures, before a rename-bot action can occur."""
        return stage == "preflight" and int(attempts) < MAX_RENAME_ATTEMPTS

    @staticmethod
    async def _bounded_flood_retry(operation, *args, **kwargs):
        for attempt in range(2):
            try:
                return await operation(*args, **kwargs)
            except FloodWait as exc:
                if attempt:
                    raise
                await asyncio.sleep(max(int(exc.value), 1))
        raise RuntimeError("Unreachable flood-wait retry state")

    @staticmethod
    def _source_filename(message) -> str | None:
        if message.video:
            return message.video.file_name
        if message.document:
            return message.document.file_name
        return None

    async def _rename_conversation(self, source_message, config: dict) -> tuple[object, str]:
        if not self.user_client or not self.rename_bot_id:
            raise RuntimeError("The logged-in account or rename bot is unavailable.")
        source_id = int(source_message.chat.id)
        source_message_id = int(source_message.id)
        async with self._conversation_lock:
            while not self._rename_messages.empty():
                try:
                    self._rename_messages.get_nowait()
                except asyncio.QueueEmpty:
                    break
            loop = asyncio.get_running_loop()
            deadline = loop.time() + RENAME_RESPONSE_TIMEOUT
            thumbnail = config.get("thumbnail")
            if not thumbnail:
                raise RuntimeError("No thumbnail is configured for this source channel.")
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "preflight",
                "thumbnail_sending",
                conversation_started_at=datetime.now(timezone.utc),
            )
            photo = io.BytesIO(bytes(thumbnail))
            photo.name = "source-thumbnail.jpg"
            await self._bounded_flood_retry(self.user_client.send_photo, self.rename_bot_id, photo)
            await self._advance_job_stage(source_id, source_message_id, "thumbnail_sending", "thumbnail_sent")
            await self._advance_job_stage(source_id, source_message_id, "thumbnail_sent", "media_sending")
            sent_media = await self._bounded_flood_retry(source_message.copy, self.rename_bot_id)
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "media_sending",
                "media_sent",
                rename_bot_media_message_id=getattr(sent_media, "id", None),
            )

            prompt = None
            while prompt is None:
                candidate = await self._next_rename_message(
                    deadline,
                    after_message_id=getattr(sent_media, "id", None),
                    reply_to_ids={int(sent_media.id)} if getattr(sent_media, "id", None) is not None else None,
                )
                text = candidate.text or candidate.caption or ""
                if re.search(r"please\s+enter\s+new\s+filename|old\s+file\s+name", text, re.IGNORECASE):
                    prompt_filename = extract_original_filename(text)
                    source_filename = self._source_filename(source_message)
                    if prompt_filename and source_filename:
                        expected_name = os.path.basename(source_filename.replace("\\", "/")).casefold()
                        observed_name = os.path.basename(prompt_filename.replace("\\", "/")).casefold()
                        if observed_name != expected_name:
                            logger.warning(
                                "Ignoring stale rename-bot prompt for %s while processing source=%s/%s.",
                                prompt_filename,
                                source_id,
                                source_message_id,
                            )
                            continue
                    prompt = candidate
                elif re.search(r"error|failed|invalid", text, re.IGNORECASE):
                    raise RuntimeError(f"Rename bot reported an error: {text[:500]}")
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "media_sent",
                "filename_prompt_received",
                filename_prompt_message_id=prompt.id,
            )

            text = prompt.text or prompt.caption or ""
            original = extract_original_filename(text, self._source_filename(source_message))
            if not original:
                raise RuntimeError("The rename bot did not provide a usable original filename.")
            username = await db.get_username()
            new_filename = append_username(original, username)
            await self._advance_job_stage(source_id, source_message_id, "filename_prompt_received", "filename_sending")
            filename_reply = await self._bounded_flood_retry(
                self.user_client.send_message,
                self.rename_bot_id,
                new_filename,
                reply_to_message_id=prompt.id,
            )
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "filename_sending",
                "filename_submitted",
                filename_message_id=getattr(filename_reply, "id", None),
                requested_filename=new_filename,
            )

            menu = None
            second_button = None
            while menu is None:
                candidate = await self._next_rename_message(
                    deadline,
                    after_message_id=getattr(filename_reply, "id", None),
                    reply_to_ids={
                        int(value)
                        for value in (getattr(filename_reply, "id", None), getattr(prompt, "id", None), getattr(sent_media, "id", None))
                        if value is not None
                    },
                )
                if candidate.reply_markup and getattr(candidate.reply_markup, "inline_keyboard", None):
                    try:
                        second_button = select_second_inline_button(candidate.reply_markup)
                    except ValueError as exc:
                        logger.warning("Ignoring an unusable rename-bot keyboard for source=%s/%s: %s", source_id, source_message_id, exc)
                        continue
                    menu = candidate
                else:
                    body = candidate.text or candidate.caption or ""
                    if re.search(r"error|failed|invalid", body, re.IGNORECASE):
                        raise RuntimeError(f"Rename bot reported an error: {body[:500]}")
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "filename_submitted",
                "menu_received",
                rename_bot_menu_message_id=menu.id,
                callback_button_position=2,
                callback_data=second_button.callback_data,
            )
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "menu_received",
                "callback_submitting",
                callback_attempted_at=datetime.now(timezone.utc),
                callback_attempt_count=1,
            )
            try:
                await self.user_client.request_callback_answer(
                    chat_id=self.rename_bot_id,
                    message_id=menu.id,
                    callback_data=second_button.callback_data,
                    timeout=30,
                )
            except BotResponseTimeout:
                # The action may have succeeded even though the callback answer timed
                # out. Never submit that callback again; wait for the resulting media.
                logger.warning("Rename-bot callback answer timed out for source=%s/%s; waiting without retrying the callback.", source_id, source_message_id)
                await self._advance_job_stage(
                    source_id,
                    source_message_id,
                    "callback_submitting",
                    "callback_unknown",
                    callback_outcome="unknown_after_timeout",
                )
                callback_stage = "callback_unknown"
            else:
                await self._advance_job_stage(
                    source_id,
                    source_message_id,
                    "callback_submitting",
                    "callback_submitted",
                    callback_outcome="acknowledged",
                    callback_completed_at=datetime.now(timezone.utc),
                )
                callback_stage = "callback_submitted"

            while True:
                try:
                    candidate = await self._next_rename_message(deadline, after_message_id=int(menu.id))
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        "Timed out waiting for the renamed video/document from the rename bot; auto-delete notices were ignored."
                    ) from exc
                if candidate.video or candidate.document:
                    media = candidate.video or candidate.document
                    output_filename = getattr(media, "file_name", None)
                    reply_to_id = self._reply_to_message_id(candidate)
                    allowed_reply_ids = {
                        int(value)
                        for value in (getattr(menu, "id", None), getattr(filename_reply, "id", None), getattr(sent_media, "id", None))
                        if value is not None
                    }
                    filename_matches = bool(
                        output_filename
                        and os.path.basename(output_filename.replace("\\", "/")).casefold()
                        == os.path.basename(new_filename.replace("\\", "/")).casefold()
                    )
                    if reply_to_id is not None and reply_to_id not in allowed_reply_ids and not filename_matches:
                        logger.warning(
                            "Ignoring unrelated rename-bot media %s (reply_to=%s) while waiting for %s for source=%s/%s.",
                            output_filename or "<no filename>",
                            reply_to_id,
                            new_filename,
                            source_id,
                            source_message_id,
                        )
                        continue
                    if output_filename and not filename_matches and reply_to_id not in allowed_reply_ids:
                        logger.warning(
                            "Ignoring rename-bot media with unexpected filename %s while waiting for %s for source=%s/%s.",
                            output_filename,
                            new_filename,
                            source_id,
                            source_message_id,
                        )
                        continue
                    await self._advance_job_stage(
                        source_id,
                        source_message_id,
                        callback_stage,
                        "final_received",
                        rename_bot_message_id=candidate.id,
                    )
                    return candidate, new_filename
                body = candidate.text or candidate.caption or ""
                if re.search(r"error|failed|invalid", body, re.IGNORECASE):
                    raise RuntimeError(f"Rename bot reported an error: {body[:500]}")

    async def _publish_result(self, final_message, output_id: int, thumbnail: bytes | None, expected_filename: str):
        video_media = final_message.video
        document_media = final_message.document
        media = video_media or document_media
        if media is None:
            raise RuntimeError("The rename bot did not return a video or document to publish.")
        if not thumbnail:
            raise RuntimeError("No thumbnail is configured; refusing to publish without the configured thumbnail.")

        filename = sanitize_filename(expected_filename or getattr(media, "file_name", None) or "renamed_file")
        mime_type = (getattr(media, "mime_type", None) or "").lower()
        extension = os.path.splitext(filename)[1].lower()
        is_video = bool(video_media) or bool(
            document_media and (mime_type.startswith("video/") or extension in _VIDEO_EXTENSIONS)
        )
        with tempfile.TemporaryDirectory(prefix="vjrename-") as tmpdir:
            source_path = os.path.join(tmpdir, filename)
            downloaded = await final_message.download(file_name=source_path)
            if not downloaded:
                raise RuntimeError("Could not download the rename bot's final media for upload.")
            thumb = io.BytesIO(bytes(thumbnail))
            thumb.name = "thumbnail.jpg"
            send_options = {
                "caption": final_message.caption or "",
                "thumb": thumb,
                "file_name": filename,
            }
            caption_entities = getattr(final_message, "caption_entities", None)
            if caption_entities:
                send_options["caption_entities"] = caption_entities

            if is_video:
                send_video = video_media or document_media
                for field in ("duration", "width", "height"):
                    value = getattr(send_video, field, None)
                    if value is not None:
                        send_options[field] = value
                send_options["supports_streaming"] = bool(getattr(send_video, "supports_streaming", True))
                has_spoiler = getattr(final_message, "has_media_spoiler", None)
                if has_spoiler is not None:
                    send_options["has_spoiler"] = bool(has_spoiler)
                uploaded = await self._bounded_flood_retry(
                    self.user_client.send_video,
                    output_id,
                    downloaded,
                    **send_options,
                )
            else:
                uploaded = await self._bounded_flood_retry(
                    self.user_client.send_document,
                    output_id,
                    downloaded,
                    **send_options,
                )

            if uploaded is None or not getattr(uploaded, "id", None):
                raise RuntimeError("Telegram did not confirm successful delivery of the renamed media.")
            return uploaded

    async def _process_job(self, source_id: int, message_id: int):
        try:
            job = await db.get_job(source_id, message_id)
            if not job:
                return
            if int(job.get("attempts", 0)) >= MAX_RENAME_ATTEMPTS:
                await db.update_job(source_id, message_id, status="failed", error="Maximum processing attempts reached.")
                return
            if not await db.claim_job(source_id, message_id):
                return
        except Exception:
            logger.exception("Could not load or claim persisted rename job source=%s message=%s", source_id, message_id)
            return
        try:
            config = await db.get_channel_config(source_id)
            if not config:
                raise RuntimeError("Source-channel configuration was removed.")
            source_message = await self.user_client.get_messages(source_id, message_id)
            if not source_message or not (source_message.video or source_message.document):
                raise RuntimeError("Source message is no longer available or is not a video/document.")
            final_message, new_filename = await self._rename_conversation(source_message, config)
            await self._advance_job_stage(
                source_id,
                message_id,
                "final_received",
                "publishing",
                status="publishing",
                final_filename=new_filename,
                rename_bot_message_id=final_message.id,
            )
            # Once publishing begins, interrupted jobs are fenced as failed on restart to avoid blind duplicate sends.
            output_id = int(job["output_channel_id"])
            try:
                sent = await self._publish_result(final_message, output_id, config.get("thumbnail"), new_filename)
            except Exception:
                logger.exception(
                    "Failed to upload renamed media to output channel=%s for source=%s/%s.",
                    output_id,
                    source_id,
                    message_id,
                )
                raise
            await db.update_job(
                source_id,
                message_id,
                status="completed",
                output_message_id=sent.id,
                error=None,
                completed_at=datetime.now(timezone.utc),
            )
            logger.info("Completed rename job for source=%s message=%s as %s", source_id, message_id, new_filename)
            if self.bot_client and ADMINS:
                try:
                    await self.bot_client.send_message(ADMINS[0], f"Rename complete: <code>{new_filename}</code>\nSource: <code>{source_id}/{message_id}</code>")
                except Exception:
                    logger.debug("Could not send job-completion notification to the primary administrator.", exc_info=True)
        except asyncio.CancelledError:
            current = await db.get_job(source_id, message_id)
            stage = current.get("stage") if current else None
            fields = {"status": "failed", "error": "Cancelled by administrator."}
            if stage != "publishing":
                fields["stage"] = "cancelled"
            await db.update_job(source_id, message_id, **fields)
            raise
        except Exception as exc:
            logger.exception("Rename job failed for source=%s message=%s", source_id, message_id)
            error = str(exc)[:1000]
            current = await db.get_job(source_id, message_id)
            attempt_count = int(current.get("attempts", 0)) if current else MAX_RENAME_ATTEMPTS
            stage = current.get("stage") if current else "publishing"
            # Once the rename bot has seen any side effect, restarting the full
            # conversation can rename the same media again or repeat its callback.
            can_retry = self._safe_to_retry(stage, attempt_count)
            await db.update_job(
                source_id,
                message_id,
                status="pending" if can_retry else "failed",
                stage="preflight" if can_retry else stage,
                error=error,
            )
            if can_retry:
                delay = min(2 ** max(attempt_count, 1), 30)
                key = (source_id, message_id)
                asyncio.get_running_loop().call_later(delay, self._launch_job, key, source_id, message_id)


rename_runtime = RenameRuntime()
