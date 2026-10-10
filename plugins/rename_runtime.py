"""Event-driven rename orchestration using the preserved logged-in user session."""

import asyncio
import io
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, RPCError
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


def select_second_inline_button(markup):
    """Return the second actual button in row-major keyboard order."""
    keyboard = getattr(markup, "inline_keyboard", None)
    if not keyboard:
        raise ValueError("Rename bot returned no inline keyboard.")
    buttons = [button for row in keyboard for button in row]
    if len(buttons) < 2:
        raise ValueError("Rename bot returned fewer than two inline buttons.")
    button = buttons[1]
    if not getattr(button, "callback_data", None):
        raise ValueError("The second inline button has no callback data; it cannot be clicked safely.")
    return button


class RenameRuntime:
    def __init__(self):
        self.bot_client = None
        self.user_client = None
        self.rename_bot_id = None
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

    async def start_user_session(self, session: str | None = None):
        if self.user_client and getattr(self.user_client, "is_connected", False):
            return True
        session = session or await db.get_session(self.session_owner_id)
        if not session:
            return False
        if not RENAME_BOT_USERNAME:
            logger.warning("Cannot start user monitor: RENAME_BOT_USERNAME is not configured.")
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
            rename_peer = await client.get_users(RENAME_BOT_USERNAME)
            self.rename_bot_id = rename_peer.id
            bot_filter = filters.private & filters.incoming & filters.user(self.rename_bot_id)
            client.add_handler(MessageHandler(self._on_rename_bot_message, bot_filter), group=self._handler_group)
            client.add_handler(EditedMessageHandler(self._on_rename_bot_message, bot_filter), group=self._handler_group)
            client.add_handler(
                MessageHandler(self._on_source_message, filters.channel & (filters.video | filters.document)),
                group=self._handler_group + 1,
            )
            self.user_client = client
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
            return False

    async def stop_user_session(self):
        client, self.user_client = self.user_client, None
        self.rename_bot_id = None
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
        task.add_done_callback(lambda _task, job_key=key: self._jobs.pop(job_key, None))

    async def resume_pending_jobs(self):
        if not self.user_client:
            return
        async for job in db.get_pending_jobs():
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

    async def _next_rename_message(self, deadline: float):
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Timed out waiting for the rename bot.")
            message = await asyncio.wait_for(self._rename_messages.get(), timeout=remaining)
            if message.chat and message.chat.id == self.rename_bot_id:
                return message

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
            photo = io.BytesIO(bytes(thumbnail))
            photo.name = "source-thumbnail.jpg"
            await self._bounded_flood_retry(self.user_client.send_photo, self.rename_bot_id, photo)
            await self._bounded_flood_retry(source_message.copy, self.rename_bot_id)

            prompt = None
            while prompt is None:
                candidate = await self._next_rename_message(deadline)
                text = candidate.text or candidate.caption or ""
                if re.search(r"please\s+enter\s+new\s+filename|old\s+file\s+name", text, re.IGNORECASE):
                    prompt = candidate
                elif re.search(r"error|failed|invalid", text, re.IGNORECASE):
                    raise RuntimeError(f"Rename bot reported an error: {text[:500]}")

            text = prompt.text or prompt.caption or ""
            original = extract_original_filename(text, self._source_filename(source_message))
            if not original:
                raise RuntimeError("The rename bot did not provide a usable original filename.")
            username = await db.get_username()
            new_filename = append_username(original, username)
            await self._bounded_flood_retry(
                self.user_client.send_message,
                self.rename_bot_id,
                new_filename,
                reply_to_message_id=prompt.id,
            )

            menu = None
            while menu is None:
                candidate = await self._next_rename_message(deadline)
                if candidate.reply_markup and getattr(candidate.reply_markup, "inline_keyboard", None):
                    menu = candidate
                else:
                    body = candidate.text or candidate.caption or ""
                    if re.search(r"error|failed|invalid", body, re.IGNORECASE):
                        raise RuntimeError(f"Rename bot reported an error: {body[:500]}")
            second_button = select_second_inline_button(menu.reply_markup)
            await self._bounded_flood_retry(
                self.user_client.request_callback_answer,
                chat_id=self.rename_bot_id,
                message_id=menu.id,
                callback_data=second_button.callback_data,
                timeout=30,
            )

            while True:
                candidate = await self._next_rename_message(deadline)
                if candidate.video or candidate.document:
                    return candidate, new_filename
                body = candidate.text or candidate.caption or ""
                if re.search(r"error|failed|invalid", body, re.IGNORECASE):
                    raise RuntimeError(f"Rename bot reported an error: {body[:500]}")

    async def _publish_result(self, final_message, output_id: int, thumbnail: bytes | None, expected_filename: str):
        if not thumbnail:
            return await self._bounded_flood_retry(final_message.copy, output_id)
        media = final_message.video or final_message.document
        filename = sanitize_filename(expected_filename or (media.file_name if media else None) or "renamed_file")
        with tempfile.TemporaryDirectory(prefix="vjrename-") as tmpdir:
            source_path = os.path.join(tmpdir, filename)
            try:
                downloaded = await final_message.download(file_name=source_path)
            except Exception:
                logger.exception("Could not download final rename-bot media for thumbnail application; copying the returned file instead.")
                return await self._bounded_flood_retry(final_message.copy, output_id)
            if not downloaded:
                return await self._bounded_flood_retry(final_message.copy, output_id)
            thumb = io.BytesIO(bytes(thumbnail))
            thumb.name = "thumbnail.jpg"
            caption = final_message.caption
            try:
                if final_message.video:
                    return await self._bounded_flood_retry(
                        self.user_client.send_video,
                        output_id,
                        downloaded,
                        caption=caption,
                        thumb=thumb,
                        file_name=filename,
                        duration=final_message.video.duration,
                        width=final_message.video.width,
                        height=final_message.video.height,
                    )
                return await self._bounded_flood_retry(
                    self.user_client.send_document,
                    output_id,
                    downloaded,
                    caption=caption,
                    thumb=thumb,
                    file_name=filename,
                )
            except RPCError:
                logger.warning("Telegram rejected the custom thumbnail; forwarding returned file without applying that thumbnail.", exc_info=True)
                return await self._bounded_flood_retry(final_message.copy, output_id)

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
            await db.update_job(
                source_id,
                message_id,
                status="publishing",
                stage="publishing",
                final_filename=new_filename,
                rename_bot_message_id=final_message.id,
            )
            # Once publishing begins, interrupted jobs are fenced as failed on restart to avoid blind duplicate sends.
            sent = await self._publish_result(final_message, int(job["output_channel_id"]), config.get("thumbnail"), new_filename)
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
            can_retry = stage != "publishing" and attempt_count < MAX_RENAME_ATTEMPTS
            await db.update_job(
                source_id,
                message_id,
                status="pending" if can_retry else "failed",
                error=error,
            )
            if can_retry:
                delay = min(2 ** max(attempt_count, 1), 30)
                key = (source_id, message_id)
                asyncio.get_running_loop().call_later(delay, self._launch_job, key, source_id, message_id)


rename_runtime = RenameRuntime()
