"""Event-driven rename orchestration using the preserved logged-in user session."""

import asyncio
import io
import logging
import os
import re
import tempfile
from collections import deque
from datetime import datetime, timezone

from pyrogram import Client, filters, enums
from pyrogram.errors import BotResponseTimeout, FloodWait
from pyrogram.handlers import EditedMessageHandler, MessageHandler

from config import (
    API_HASH,
    API_ID,
    ADMINS,
    MAX_RENAME_ATTEMPTS,
    QUALITY_PRIORITY_ENABLED,
    RENAME_BOT_USERNAME,
    RENAME_RESPONSE_TIMEOUT,
    SESSION_OWNER_ID,
)
from plugins.database import db
from plugins.filename_utils import (
    extract_original_filename,
    extract_video_metadata,
    format_standardized_filename,
    normalize_filename_key,
    quality_priority,
    sanitize_filename,
    title_similarity,
)

logger = logging.getLogger(__name__)
_VIDEO_EXTENSIONS = frozenset({
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".ogv", ".ts", ".webm", ".wmv",
})
_UNMATCHED_OUTPUT_MAX = 100
_UNMATCHED_OUTPUT_TTL_SECONDS = 3600
_CAPTION_UNSET = object()


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
        self._accept_lock = asyncio.Lock()
        self._submission_items: list[dict] = []
        self._submission_sequence = 0
        self._submission_event = asyncio.Event()
        self._submission_worker_task: asyncio.Task | None = None
        self._queued_keys: set[tuple[int, int]] = set()
        self._jobs: dict[tuple[int, int], asyncio.Task] = {}
        self._retry_tasks: set[asyncio.Task] = set()
        self._retry_job_keys: dict[asyncio.Task, tuple[int, int]] = {}
        self._background_db_tasks: set[asyncio.Task] = set()
        self._output_waiters: dict[tuple[int, int], dict] = {}
        self._superseded_waiters: dict[tuple[int, int], dict] = {}
        self._output_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._matched_output_owners: dict[int, tuple[int, int]] = {}
        self._unmatched_outputs: deque[dict] = deque()
        self._stopping_session = False
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
        self._stopping_session = True
        client, self.user_client = self.user_client, None
        self.rename_bot_id = None
        self.active_session_owner_id = 0
        tasks = list(self._jobs.values()) + list(self._output_tasks.values()) + list(self._retry_tasks) + list(self._background_db_tasks)
        worker, self._submission_worker_task = self._submission_worker_task, None
        if worker:
            tasks.append(worker)
        tasks = list({task for task in tasks if task and not task.done()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client and getattr(client, "is_connected", False):
            try:
                await client.stop()
            except Exception:
                logger.exception("Error while stopping the logged-in user session.")
        self._jobs.clear()
        self._output_tasks.clear()
        self._retry_tasks.clear()
        self._retry_job_keys.clear()
        self._background_db_tasks.clear()
        self._queued_keys.clear()
        self._submission_items.clear()
        self._submission_event.clear()
        self._unmatched_outputs.clear()
        for context in self._output_waiters.values():
            future = context.get("future")
            if future and not future.done():
                future.cancel()
        self._output_waiters.clear()
        self._superseded_waiters.clear()
        self._matched_output_owners.clear()
        self._stopping_session = False

    async def _on_rename_bot_message(self, _client, message):
        body = message.text or message.caption or ""
        if not (message.video or message.document) and self._is_auto_delete_notice(message):
            reply_to = self._reply_to_message_id(message)
            all_contexts = list(self._output_waiters.values()) + list(self._superseded_waiters.values())
            candidates = [
                context for context in all_contexts
                if reply_to is not None and reply_to in context["reply_ids"]
            ]
            if not candidates:
                text_matches = [
                    context for context in all_contexts
                    if context.get("expected_filename") and context["expected_filename"].casefold() in body.casefold()
                ]
                if len(text_matches) == 1:
                    candidates = text_matches
            for context in candidates:
                context["reply_ids"].add(int(message.id))
                task = asyncio.create_task(
                    self._persist_output_reply_id(context["source_id"], context["message_id"], int(message.id))
                )
                self._background_db_tasks.add(task)
                task.add_done_callback(self._background_db_tasks.discard)
            logger.debug("Ignoring rename-bot auto-delete notice message_id=%s", getattr(message, "id", None))
            return
        if message.video or message.document:
            if self._route_output_message(message):
                self._reconcile_unmatched_outputs()
            else:
                self._remember_unmatched_output(message)
            return
        reply_to = self._reply_to_message_id(message)
        if body and re.search(r"error|failed|invalid", body, re.IGNORECASE) and reply_to is not None:
            matches = [
                context for context in self._output_waiters.values()
                if reply_to in context["reply_ids"] and not context["future"].done()
            ]
            if matches:
                for context in matches:
                    context["bot_error"] = body[:500]
                    context["error_event"].set()
                    logger.error(
                        "Rename bot reported an error for source=%s message=%s: %s",
                        context["source_id"],
                        context["message_id"],
                        body[:500],
                    )
                return
        if body or message.reply_markup:
            self._rename_messages.put_nowait(message)

    @staticmethod
    def _is_auto_delete_notice(message) -> bool:
        body = message.text or message.caption or ""
        return bool(re.search(r"auto[\s-]*delete\s+notice", body, re.IGNORECASE))

    @staticmethod
    def _media_filename(message) -> str | None:
        media = message.video or message.document
        name = getattr(media, "file_name", None) if media else None
        return os.path.basename(name.replace("\\", "/")) if isinstance(name, str) and name else None

    def _register_output_waiter(
        self,
        job: dict,
        expected_filename: str | None = None,
        reply_ids=None,
        *,
        accepting_output: bool | None = None,
    ) -> dict:
        source_id, message_id = int(job["source_channel_id"]), int(job["source_message_id"])
        key = (source_id, message_id)
        context = self._output_waiters.get(key)
        if context:
            if expected_filename:
                context["expected_filename"] = expected_filename
            context["reply_ids"].update(int(value) for value in (reply_ids or []) if value is not None)
            context["media_metadata"] = dict(job.get("media_metadata") or context.get("media_metadata") or {})
            if accepting_output is not None:
                context["accepting_output"] = accepting_output
            if context.get("accepting_output"):
                self._reconcile_unmatched_outputs()
            return context
        anchors = list(job.get("output_reply_ids", []))
        anchors.extend([
            job.get("rename_bot_media_message_id"),
            job.get("filename_message_id"),
            job.get("rename_bot_menu_message_id"),
        ])
        anchors.extend(reply_ids or [])
        context = {
            "source_id": source_id,
            "message_id": message_id,
            "expected_filename": expected_filename or job.get("expected_filename") or job.get("final_filename"),
            "reply_ids": {int(value) for value in anchors if value is not None},
            "future": asyncio.get_running_loop().create_future(),
            "error_event": asyncio.Event(),
            "submitted_order": job.get("submitted_at") or job.get("created_at") or datetime.now(timezone.utc),
            "job_id": job.get("job_id") or f"{source_id}:{message_id}",
            "created_at": job.get("created_at"),
            "source_channel_number": job.get("source_channel_number"),
            "output_channel_id": int(job["output_channel_id"]) if job.get("output_channel_id") is not None else None,
            "original_filename": job.get("original_filename"),
            "original_caption": job.get("original_caption"),
            "media_metadata": dict(job.get("media_metadata") or {}),
            "superseded_output_message_ids": set(job.get("superseded_output_message_ids") or []),
            "accepting_output": accepting_output if accepting_output is not None else job.get("status") in {"submitted", "submission_unknown"},
        }
        self._output_waiters[key] = context
        if context["accepting_output"]:
            self._reconcile_unmatched_outputs()
        return context

    @staticmethod
    def _metadata_match_score(source_metadata: dict | None, output_metadata: dict | None) -> int | None:
        source_metadata = source_metadata or {}
        output_metadata = output_metadata or {}
        source_episode, output_episode = source_metadata.get("episode"), output_metadata.get("episode")
        if source_episode is None or output_episode is None or int(source_episode) != int(output_episode):
            return None
        source_season, output_season = source_metadata.get("season"), output_metadata.get("season")
        if source_season != output_season:
            return None
        source_quality, output_quality = source_metadata.get("quality"), output_metadata.get("quality")
        if source_quality is not None and source_quality != output_quality:
            return None
        similarity, overlap = title_similarity(source_metadata.get("title_key"), output_metadata.get("title_key"))
        if not overlap or similarity < 0.3:
            return None
        return 50 + round(similarity * 10) + (5 if source_quality is not None and source_quality == output_quality else 0)

    def _route_output_message(self, message, *, allow_inactive: bool = False) -> bool:
        if not (message.video or message.document):
            return False
        message_id = int(getattr(message, "id", 0))
        filename = self._media_filename(message)
        output_metadata = extract_video_metadata(filename, getattr(message, "caption", None))
        known_owner = self._matched_output_owners.get(message_id)
        if known_owner is not None:
            context = self._output_waiters.get(known_owner)
            if context and not context["future"].done():
                if not allow_inactive and not context.get("accepting_output"):
                    return False
                context["future"].set_result(message)
                context["output_message_id"] = message_id
                context["output_metadata"] = output_metadata
                self._schedule_db_task(
                    self._persist_matched_output(context, message, output_metadata),
                    f"match:{known_owner[0]}:{known_owner[1]}:{message_id}",
                )
                return True
            logger.debug("Ignoring already-associated rename-bot output message=%s owner=%s", message_id, known_owner)
            return True

        stale_matches = []
        for key, context in self._superseded_waiters.items():
            score = self._output_match_strength(context, message, filename, output_metadata)
            if score is not None:
                stale_matches.append((score, key, context))

        candidates = []
        for key, context in self._output_waiters.items():
            if context["future"].done() or (not allow_inactive and not context.get("accepting_output")):
                continue
            score = self._output_match_strength(context, message, filename, output_metadata)
            if score is not None:
                candidates.append((score, context["submitted_order"], key, context))

        # Keep legacy superseded output separate, but compare it against every
        # current candidate rather than letting any weak stale match hide a
        # stronger match for a different quality/job.
        best_stale = max((item[0] for item in stale_matches), default=None)
        best_current = max((item[0] for item in candidates), default=None)
        if best_stale is not None and (best_current is None or best_stale >= best_current):
            if best_current == best_stale:
                self._remember_unmatched_output(message, frozen=True)
                logger.warning(
                    "Rename output message=%s is equally compatible with an old superseded job and a current job; leaving it pending.",
                    message_id,
                )
            else:
                self._hold_superseded_output(message, stale_matches)
            return True
        if not candidates:
            return False
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            self._remember_unmatched_output(message, frozen=True)
            logger.error(
                "Rename-bot output cannot be safely associated for filename=%s (ambiguous jobs=%s); leaving it unassigned rather than publishing to the wrong destination.",
                filename or "<no filename>",
                [
                    {
                        "job_id": item[3].get("job_id"),
                        "channel": item[3].get("source_channel_number"),
                        "source": item[2][0],
                        "message": item[2][1],
         
