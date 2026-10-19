"""Event-driven rename orchestration using the preserved logged-in user session."""

import asyncio
import io
import logging
import os
import re
import tempfile
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

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
from plugins.filename_utils import (
    extract_original_filename,
    extract_video_metadata,
    format_standardized_filename,
    normalize_filename_key,
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
_OUTPUT_RECOVERY_POLL_SECONDS = 15
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
        self._queue_lease_owner = uuid.uuid4().hex
        self._queue_lease_heartbeat_task: asyncio.Task | None = None
        self._queue_lease_lost = asyncio.Event()
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
            # The rename bot may return media without a usable filename/caption,
            # or the live handler may have missed it. Under the global FIFO,
            # exactly one job can be awaiting output; only use that fact when
            # the media is newer than the request anchors and its Telegram date
            # is at/after that submission. Ambiguous/stale outputs stay pending.
            reply_to = self._reply_to_message_id(message)
            message_date = getattr(message, "date", None)
            if isinstance(message_date, datetime):
                message_date = self._normalize_queue_datetime(message_date)
                active = []
                for key, context in self._output_waiters.items():
                    if context["future"].done() or not context.get("accepting_output"):
                        continue
                    if message_id <= max(context.get("reply_ids", set()), default=0):
                        continue
                    if reply_to is not None and reply_to not in context.get("reply_ids", set()):
                        continue
                    submitted_order = self._normalize_queue_datetime(context.get("submitted_order"))
                    # Telegram message dates have one-second granularity.
                    if message_date + timedelta(seconds=2) < submitted_order:
                        continue
                    active.append((70, submitted_order, key, context))
                if len(active) == 1:
                    candidates = active
                    logger.warning(
                        "Correlating rename-bot media message=%s to the only active FIFO job=%s using post-submission timing; filename metadata was unavailable.",
                        message_id,
                        active[0][3].get("job_id"),
                    )
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
                        "destination": item[3].get("output_channel_id"),
                    }
                    for item in candidates if item[0] == candidates[0][0]
                ],
            )
            return True
        context = candidates[0][3]
        key = candidates[0][2]
        self._matched_output_owners[message_id] = key
        context["future"].set_result(message)
        context["output_message_id"] = int(message.id)
        context["output_metadata"] = output_metadata
        self._schedule_db_task(
            self._persist_matched_output(context, message, output_metadata),
            f"match:{context['source_id']}:{context['message_id']}:{message.id}",
        )
        logger.info(
            "Matched rename-bot output message=%s to Channel %s source=%s message=%s filename=%s metadata=%s.",
            message.id,
            context.get("source_channel_number", "?"),
            context["source_id"],
            context["message_id"],
            filename or context.get("expected_filename"),
            output_metadata,
        )
        return True

    def _output_match_strength(self, context: dict, message, filename: str | None, output_metadata: dict) -> int | None:
        reply_to = self._reply_to_message_id(message)
        anchor_floor = max(context.get("reply_ids", set()), default=0)
        if int(getattr(message, "id", 0)) <= anchor_floor:
            return None
        if reply_to is not None and reply_to in context.get("reply_ids", set()):
            return 100
        expected = context.get("expected_filename")
        if filename and expected and normalize_filename_key(filename) == normalize_filename_key(expected):
            return 90
        original = context.get("original_filename")
        if filename and original and normalize_filename_key(filename) == normalize_filename_key(original):
            return 85
        return self._metadata_match_score(context.get("media_metadata"), output_metadata)

    def _hold_superseded_output(self, message, stale_matches):
        message_id = int(getattr(message, "id", 0))
        strongest = max(item[0] for item in stale_matches)
        matches = [item for item in stale_matches if item[0] == strongest]
        if len(matches) != 1:
            self._remember_unmatched_output(message, frozen=True)
            logger.warning(
                "Leaving rename-bot output message=%s pending: it matches multiple superseded jobs and cannot be safely attributed.",
                message_id,
            )
            return
        _, key, context = matches[0]
        if message_id not in context["superseded_output_message_ids"]:
            context["superseded_output_message_ids"].add(message_id)
            self._schedule_db_task(
                db.add_superseded_output(key[0], key[1], message_id),
                f"superseded-output:{key[0]}:{key[1]}:{message_id}",
            )
        logger.info(
            "Leaving late output message=%s pending for superseded job=%s; it will not be published to the latest request.",
            message_id,
            context.get("job_id"),
        )

    async def _persist_matched_output(self, context: dict, message, metadata: dict):
        try:
            await db.update_job(
                context["source_id"],
                context["message_id"],
                rename_bot_output_message_id=int(message.id),
                actual_output_filename=self._media_filename(message),
                actual_output_caption=getattr(message, "caption", None),
                output_metadata=metadata,
            )
        except Exception:
            logger.exception(
                "Could not persist matched rename-bot output for job=%s message=%s.",
                context["job_id"],
                getattr(message, "id", None),
            )

    def _schedule_db_task(self, coroutine, name: str):
        task = asyncio.create_task(coroutine, name=name)
        self._background_db_tasks.add(task)
        task.add_done_callback(self._background_db_tasks.discard)
        return task

    def _prune_unmatched_outputs(self):
        now = asyncio.get_running_loop().time()
        kept = [
            item for item in self._unmatched_outputs
            if now - item["received_at"] <= _UNMATCHED_OUTPUT_TTL_SECONDS
        ]
        expired = len(self._unmatched_outputs) - len(kept)
        self._unmatched_outputs = deque(kept)
        if expired:
            logger.info("Expired %s unmatched rename-bot output item(s) beyond the retention window.", expired)

    def _remember_unmatched_output(self, message, *, frozen: bool = False):
        loop = asyncio.get_running_loop()
        now = loop.time()
        message_date = getattr(message, "date", None)
        received_at = now
        if isinstance(message_date, datetime):
            if message_date.tzinfo is None:
                message_date = message_date.replace(tzinfo=timezone.utc)
            age = max((datetime.now(timezone.utc) - message_date.astimezone(timezone.utc)).total_seconds(), 0)
            if age > _UNMATCHED_OUTPUT_TTL_SECONDS:
                logger.debug("Skipping stale historical rename output message_id=%s age=%ss.", getattr(message, "id", None), round(age))
                return
            received_at = now - age
        self._prune_unmatched_outputs()
        message_id = int(getattr(message, "id", 0))
        if any(int(getattr(item["message"], "id", 0)) == message_id for item in self._unmatched_outputs):
            return
        if len(self._unmatched_outputs) >= _UNMATCHED_OUTPUT_MAX:
            oldest = self._unmatched_outputs.popleft()
            logger.warning("Dropping unmatched output message_id=%s due to bounded queue capacity.", getattr(oldest["message"], "id", None))
        filename = self._media_filename(message)
        metadata = extract_video_metadata(filename, getattr(message, "caption", None))
        possible = [
            {
                "channel": context.get("source_channel_number"),
                "source": context["source_id"],
                "message": context["message_id"],
                "episode": (context.get("media_metadata") or {}).get("episode"),
                "quality": (context.get("media_metadata") or {}).get("quality"),
            }
            for context in self._output_waiters.values()
            if context.get("accepting_output") and not context["future"].done()
        ]
        self._unmatched_outputs.append({
            "message": message,
            "metadata": metadata,
            "received_at": received_at,
            "frozen": bool(frozen),
        })
        logger.warning(
            "Stored unmatched rename-bot media message=%s filename=%s metadata=%s possible_jobs=%s; retained for reconciliation.",
            message_id,
            filename,
            metadata,
            possible,
        )

    def _reconcile_unmatched_outputs(self):
        if not self._unmatched_outputs:
            return
        self._prune_unmatched_outputs()
        pending = list(self._unmatched_outputs)
        self._unmatched_outputs.clear()
        for item in pending:
            if item.get("frozen"):
                self._unmatched_outputs.append(item)
            elif not self._route_output_message(item["message"]):
                self._unmatched_outputs.append(item)

    def _ensure_output_task(self, job: dict, config: dict, context: dict):
        key = (int(job["source_channel_id"]), int(job["source_message_id"]))
        current = self._output_tasks.get(key)
        if current and not current.done():
            return current
        task = asyncio.create_task(self._track_submitted_output(job, config, context), name=f"rename-output:{key[0]}:{key[1]}")
        self._output_tasks[key] = task
        task.add_done_callback(lambda completed, job_key=key: self._output_tasks.pop(job_key, None) if self._output_tasks.get(job_key) is completed else None)
        return task

    async def _wait_for_output_message(self, context: dict, source_id: int, message_id: int):
        output_future = context["future"]
        if output_future.done():
            return output_future.result()
        error_task = asyncio.create_task(context["error_event"].wait())
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        timeout_logged = False
        try:
            while True:
                if output_future.done():
                    return output_future.result()
                if error_task.done():
                    raise RuntimeError(f"Rename bot reported an error for source={source_id} message={message_id}: {context.get('bot_error', 'unknown rename-bot failure')}")

                # Events are the fast path. History polling recovers media if
                # Pyrogram missed an update or the result arrived during a
                # transient disconnect. This never submits/clicks again.
                await self._recover_recent_outputs()
                if output_future.done():
                    return output_future.result()
                if error_task.done():
                    raise RuntimeError(f"Rename bot reported an error for source={source_id} message={message_id}: {context.get('bot_error', 'unknown rename-bot failure')}")

                if not timeout_logged and loop.time() - started_at >= RENAME_RESPONSE_TIMEOUT:
                    logger.error(
                        "No renamed media found from the rename bot after %s seconds for source=%s message=%s (expected=%s); continuing history recovery without resubmitting.",
                        RENAME_RESPONSE_TIMEOUT,
                        source_id,
                        message_id,
                        context.get("expected_filename") or "unknown filename",
                    )
                    timeout_logged = True
                poll_timeout = min(float(_OUTPUT_RECOVERY_POLL_SECONDS), max(float(RENAME_RESPONSE_TIMEOUT), 0.01))
                done, _ = await asyncio.wait(
                    {output_future, error_task},
                    timeout=poll_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if output_future in done:
                    return output_future.result()
                if error_task in done:
                    raise RuntimeError(f"Rename bot reported an error for source={source_id} message={message_id}: {context.get('bot_error', 'unknown rename-bot failure')}")
        finally:
            if not error_task.done():
                error_task.cancel()
            await asyncio.gather(error_task, return_exceptions=True)

    async def _track_submitted_output(self, job: dict, config: dict, context: dict):
        source_id, message_id = int(job["source_channel_id"]), int(job["source_message_id"])
        key = (source_id, message_id)
        try:
            job_id = job.get("job_id") or f"{source_id}:{message_id}"
            if context.get("superseded"):
                logger.info("Not publishing output for superseded job=%s", job_id)
                return False
            final_message = await self._wait_for_output_message(context, source_id, message_id)
            if context.get("superseded"):
                logger.info("Keeping late output pending for superseded job=%s", job_id)
                return False
            if not await db.begin_output_publish(source_id, message_id, int(final_message.id)):
                logger.info("Output publication already claimed or job no longer eligible source=%s message=%s", source_id, message_id)
                current = await db.get_job(source_id, message_id)
                return bool(current and current.get("status") == "completed")
            context["publishing"] = True
            output_id = int(job["output_channel_id"])
            expected_filename = context.get("expected_filename") or job.get("expected_filename") or job.get("final_filename") or self._media_filename(final_message)
            try:
                logger.info(
                    "Publishing matched rename-bot media job=%s source=%s/%s rename_bot_output=%s destination=%s filename=%s",
                    job_id,
                    source_id,
                    message_id,
                    getattr(final_message, "id", None),
                    output_id,
                    expected_filename,
                )
                uploaded = await self._publish_result(
                    final_message,
                    output_id,
                    config.get("thumbnail"),
                    expected_filename,
                    caption=job.get("original_caption") if job.get("original_caption") is not None else _CAPTION_UNSET,
                )
            except Exception as exc:
                logger.exception(
                    "Output upload failed job=%s Channel %s source=%s message=%s destination=%s",
                    job.get("job_id") or f"{source_id}:{message_id}",
                    job.get("source_channel_number", "?"),
                    source_id,
                    message_id,
                    output_id,
                )
                await db.update_job(
                    source_id,
                    message_id,
                    status="failed",
                    publication_status="failed",
                    stage="publishing",
                    error=str(exc)[:1000],
                )
                return False
            await db.update_job(
                source_id,
                message_id,
                status="completed",
                publication_status="published",
                stage="completed",
                output_message_id=uploaded.id,
                destination_message_id=uploaded.id,
                rename_bot_output_message_id=int(final_message.id),
                error=None,
                completed_at=datetime.now(timezone.utc),
            )
            logger.info(
                "Completed rename job=%s Channel %s source=%s message=%s output_channel=%s destination_message=%s filename=%s",
                job.get("job_id") or f"{source_id}:{message_id}",
                job.get("source_channel_number", "?"),
                source_id,
                message_id,
                output_id,
                uploaded.id,
                expected_filename,
            )
            if self.bot_client and ADMINS:
                try:
                    await self.bot_client.send_message(ADMINS[0], f"Rename complete: <code>{expected_filename}</code>\nSource: <code>{source_id}/{message_id}</code>")
                except Exception:
                    logger.debug("Could not send job-completion notification to the primary administrator.", exc_info=True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Output tracking failed source=%s message=%s", source_id, message_id)
            current = await db.get_job(source_id, message_id)
            if current and current.get("status") in {"submitted", "submission_unknown"}:
                await db.update_job(source_id, message_id, status="failed", submission_status="failed", stage="rename_bot_error", error=str(exc)[:1000])
            return False
        finally:
            if self._output_waiters.get(key) is context:
                self._output_waiters.pop(key, None)
            self._reconcile_unmatched_outputs()

    async def _recover_recent_outputs(self):
        if not self._output_waiters or not self.user_client or not self.rename_bot_id:
            return
        history = getattr(self.user_client, "get_chat_history", None)
        if not history:
            return
        try:
            async for message in history(self.rename_bot_id, limit=1000):
                if message.video or message.document:
                    matched = self._route_output_message(message)
                    if not matched:
                        self._remember_unmatched_output(message)
                elif self._is_auto_delete_notice(message):
                    await self._on_rename_bot_message(self.user_client, message)
                if not any(not context["future"].done() for context in self._output_waiters.values()):
                    break
        except Exception:
            logger.exception("Could not scan recent rename-bot history to recover submitted outputs.")

    async def _on_source_message(self, _client, message):
        if not message.chat or message.chat.type != enums.ChatType.CHANNEL:
            return
        try:
            source_id, message_id = int(message.chat.id), int(message.id)
            media = message.video or message.document
            original_filename = getattr(media, "file_name", None)
            original_caption = getattr(message, "caption", None)
            media_metadata = extract_video_metadata(original_filename, original_caption)
            async with self._accept_lock:
                config = await db.get_channel_config(source_id)
                if not config:
                    return
                username = await db.get_username()
                expected_filename = format_standardized_filename(username, media_metadata, original_filename)
                created = await db.create_job(
                    source_id,
                    message_id,
                    config["output_channel_id"],
                    media_metadata=media_metadata,
                    channel_number=config.get("channel_number"),
                    original_filename=original_filename,
                    original_caption=original_caption,
                    expected_filename=expected_filename,
                )
                if created:
                    self._enqueue_job(source_id, message_id, media_metadata)
        except Exception:
            logger.exception("Could not load source configuration or record source=%s message=%s", message.chat.id, message.id)
            return
        if not created:
            logger.info("Ignoring duplicate source job source=%s message=%s", source_id, message_id)
            return
        logger.info(
            "Accepted rename job=%s Channel %s source=%s message=%s destination=%s status=queued metadata=%s expected_filename=%s",
            f"{source_id}:{message_id}",
            config.get("channel_number", "?"),
            source_id,
            message_id,
            config["output_channel_id"],
            media_metadata,
            expected_filename,
        )

    def _ensure_submission_worker(self):
        if not self.user_client or not self.rename_bot_id:
            logger.debug(
                "Global FIFO worker not started yet (user_session=%s rename_bot=%s); queued jobs remain durable and will resume when ready.",
                bool(self.user_client),
                bool(self.rename_bot_id),
            )
            return
        if self._submission_worker_task and not self._submission_worker_task.done():
            return
        self._submission_worker_task = asyncio.create_task(self._submission_worker(), name="rename-submission-queue")
        logger.info("Started global FIFO submission worker; queued_jobs=%s", len(self._submission_items))

    @staticmethod
    def _normalize_queue_datetime(value, *, fallback: datetime | None = None) -> datetime:
        """Return a comparable UTC timestamp; Mongo's legacy naive dates mean UTC."""
        if not isinstance(value, datetime):
            value = fallback or datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _enqueue_job(
        self,
        source_id: int,
        message_id: int,
        metadata: dict | None = None,
        *,
        created_at: datetime | None = None,
    ) -> bool:
        key = (int(source_id), int(message_id))
        current = self._jobs.get(key)
        if key in self._queued_keys or (current and not current.done()):
            return False
        self._submission_sequence += 1
        created_at = self._normalize_queue_datetime(created_at)
        self._submission_items.append({
            "key": key,
            "source_id": key[0],
            "message_id": key[1],
            "metadata": dict(metadata or {}),
            "sequence": self._submission_sequence,
            "created_at": created_at,
        })
        # Normalize legacy/in-memory entries too, before Python compares sort keys.
        for item in self._submission_items:
            item["created_at"] = self._normalize_queue_datetime(
                item.get("created_at"),
                fallback=datetime.now(timezone.utc),
            )
        self._submission_items.sort(key=lambda item: (item["created_at"], item["sequence"]))
        self._queued_keys.add(key)
        self._submission_event.set()
        self._ensure_submission_worker()
        position = next((index + 1 for index, item in enumerate(self._submission_items) if item["key"] == key), "unknown")
        logger.info(
            "Enqueued global FIFO job source=%s message=%s position=%s worker_running=%s",
            key[0],
            key[1],
            position,
            bool(self._submission_worker_task and not self._submission_worker_task.done()),
        )
        return True

    def _pop_next_submission(self) -> dict:
        if not self._submission_items:
            raise IndexError("The shared submission queue is empty.")
        return self._submission_items[0]

    async def _queue_lease_heartbeat(self):
        try:
            while self.user_client and self.rename_bot_id:
                await asyncio.sleep(15)
                if not await db.renew_queue_lease(self._queue_lease_owner, lease_seconds=60):
                    logger.error("Lost the global MongoDB queue lease; stopping to prevent concurrent rename submissions.")
                    self._queue_lease_lost.set()
                    worker = self._submission_worker_task
                    if worker and worker is not asyncio.current_task() and not worker.done():
                        worker.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not renew the global queue lease; stopping this worker for safety.")
            self._queue_lease_lost.set()
            worker = self._submission_worker_task
            if worker and worker is not asyncio.current_task() and not worker.done():
                worker.cancel()

    async def _restore_queue_items(self):
        cursor = await db.get_queue_jobs()
        async for job in cursor:
            source_id = int(job["source_channel_id"])
            config = await db.get_channel_config(source_id)
            if config:
                self._enqueue_job(
                    source_id,
                    int(job["source_message_id"]),
                    job.get("media_metadata"),
                    created_at=self._normalize_queue_datetime(job.get("created_at")),
                )
            else:
                logger.error(
                    "Persisted FIFO job source=%s message=%s has no channel configuration; leaving it durable but not runnable.",
                    source_id,
                    job.get("source_message_id"),
                )

    async def _restore_output_contexts(self):
        for record in await db.get_matched_output_owners():
            output_id = record.get("rename_bot_output_message_id")
            if output_id is not None:
                key = (int(record["source_channel_id"]), int(record["source_message_id"]))
                self._matched_output_owners[int(output_id)] = key
        superseded_jobs = await db.get_superseded_jobs()
        async for job in superseded_jobs:
            key = (int(job["source_channel_id"]), int(job["source_message_id"]))
            context = self._register_output_waiter(job, accepting_output=False)
            self._output_waiters.pop(key, None)
            context["accepting_output"] = False
            context["superseded"] = True
            self._superseded_waiters[key] = context

    async def _submission_worker(self):
        heartbeat = None
        lease_acquired = False
        try:
            while self.user_client and self.rename_bot_id:
                if not await db.acquire_queue_lease(self._queue_lease_owner, lease_seconds=60):
                    await asyncio.sleep(5)
                    continue
                lease_acquired = True
                logger.info("Acquired global rename queue lease owner=%s", self._queue_lease_owner)
                self._queue_lease_lost.clear()
                heartbeat = asyncio.create_task(self._queue_lease_heartbeat(), name="rename-queue-lease-heartbeat")
                async with self._accept_lock:
                    await db.recover_interrupted_jobs(MAX_RENAME_ATTEMPTS)
                    restored = await db.restore_superseded_output_jobs()
                    if restored:
                        logger.warning("Restored %s callback-uncertain job(s) as output-only without resubmission.", restored)
                    await self._restore_output_contexts()
                    await self._restore_queue_items()
                await self._recover_recent_outputs()

                while self.user_client and self.rename_bot_id and not self._queue_lease_lost.is_set():
                    if not self._submission_items:
                        self._submission_event.clear()
                        if self._submission_items:
                            continue
                        await self._submission_event.wait()
                        continue
                    item = self._pop_next_submission()
                    key = item["key"]
                    logger.info(
                        "Selected next global FIFO job source=%s message=%s remaining=%s created_at=%s",
                        key[0],
                        key[1],
                        max(len(self._submission_items) - 1, 0),
                        item.get("created_at"),
                    )
                    current = await db.get_job(*key)
                    if current is None:
                        logger.error("Dropping stale in-memory FIFO entry without a persisted job source=%s message=%s", *key)
                        if self._submission_items and self._submission_items[0] is item:
                            self._submission_items.pop(0)
                        self._queued_keys.discard(key)
                        continue
                    if current and current.get("status") == "completed":
                        if self._submission_items and self._submission_items[0] is item:
                            self._submission_items.pop(0)
                        self._queued_keys.discard(key)
                        continue
                    if current and current.get("stage") == "cancelled":
                        if self._submission_items and self._submission_items[0] is item:
                            self._submission_items.pop(0)
                        self._queued_keys.discard(key)
                        continue
                    if current and current.get("status") == "failed":
                        # Strict FIFO: an uncertain or failed file blocks all later files.
                        await asyncio.sleep(5)
                        continue
                    task = asyncio.create_task(
                        self._process_job(item["source_id"], item["message_id"]),
                        name=f"rename-global-fifo:{key[0]}:{key[1]}",
                    )
                    self._jobs[key] = task
                    try:
                        logger.info("Beginning rename-bot submission for first eligible FIFO job source=%s message=%s", *key)
                        delivered = await task
                    except asyncio.CancelledError:
                        if asyncio.current_task() and asyncio.current_task().cancelling():
                            raise
                        logger.warning("Rename job cancelled; retaining its FIFO position source=%s message=%s", *key)
                        delivered = False
                    except Exception:
                        logger.exception("Unexpected FIFO job error source=%s message=%s; later jobs remain held.", *key)
                        delivered = False
                    finally:
                        if self._jobs.get(key) is task:
                            self._jobs.pop(key, None)
                    current = await db.get_job(*key)
                    if delivered and current and current.get("status") == "completed":
                        if self._submission_items and self._submission_items[0] is item:
                            self._submission_items.pop(0)
                        self._queued_keys.discard(key)
                        continue
                    if current and current.get("status") == "pending" and current.get("stage") == "preflight":
                        attempts = int(current.get("attempts", 1))
                        if attempts < MAX_RENAME_ATTEMPTS:
                            await asyncio.sleep(min(2 ** max(attempts, 1), 30))
                            continue
                    logger.error(
                        "Global FIFO is held at source=%s message=%s status=%s stage=%s; later files will not start until this job is safely completed or cancelled.",
                        *key,
                        current.get("status") if current else "missing",
                        current.get("stage") if current else "missing",
                    )
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Global FIFO worker stopped unexpectedly.")
        finally:
            if heartbeat and not heartbeat.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if lease_acquired:
                try:
                    await db.release_queue_lease(self._queue_lease_owner)
                except Exception:
                    logger.exception("Could not release the global queue lease cleanly.")
            if self._submission_worker_task is asyncio.current_task():
                self._submission_worker_task = None
                if self.user_client and self.rename_bot_id and self._submission_items:
                    self._ensure_submission_worker()

    async def _retry_after_delay(self, source_id: int, message_id: int, delay: float):
        try:
            await asyncio.sleep(delay)
            job = await db.get_job(source_id, message_id)
            if job and job.get("status") == "pending":
                config = await db.get_channel_config(source_id)
                if config and config.get("thumbnail"):
                    self._enqueue_job(source_id, message_id, job.get("media_metadata"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not requeue safe preflight retry source=%s message=%s", source_id, message_id)

    async def _persist_output_reply_id(self, source_id: int, message_id: int, reply_id: int):
        try:
            await db.add_output_reply_id(source_id, message_id, reply_id)
        except Exception:
            logger.exception("Could not persist rename-bot output reply anchor source=%s message=%s reply=%s", source_id, message_id, reply_id)

    def _launch_job(self, key: tuple[int, int], source_id: int, message_id: int):
        self._enqueue_job(source_id, message_id)

    async def resume_pending_jobs(self):
        if self.user_client and self.rename_bot_id:
            async with self._accept_lock:
                await self._restore_queue_items()
            self._ensure_submission_worker()

    async def cancel_active(self) -> bool:
        active = [task for task in self._jobs.values() if not task.done()]
        output = [(key, task) for key, task in self._output_tasks.items() if not task.done()]
        queued = list(self._submission_items)
        retry_tasks = [task for task in self._retry_tasks if not task.done()]
        if not active and not output and not queued and not retry_tasks:
            return False
        for task in active:
            task.cancel()
        for key, task in output:
            await db.update_job(key[0], key[1], status="failed", publication_status="failed", stage="cancelled", error="Output tracking cancelled by administrator.")
            task.cancel()
        for item in queued:
            await db.update_job(item["source_id"], item["message_id"], status="failed", submission_status="failed", stage="cancelled", error="Queued submission cancelled by administrator.")
        self._submission_items.clear()
        self._queued_keys.clear()
        for task in retry_tasks:
            retry_key = self._retry_job_keys.get(task)
            if retry_key:
                await db.update_job(retry_key[0], retry_key[1], status="failed", submission_status="failed", stage="cancelled", error="Scheduled retry cancelled by administrator.")
            task.cancel()
        tasks = active + [task for _, task in output] + retry_tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
            if self._is_auto_delete_notice(message):
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

    async def _rename_conversation(self, source_message, config: dict, job: dict | None = None, output_context: dict | None = None) -> dict:
        if not self.user_client or not self.rename_bot_id:
            raise RuntimeError("The logged-in account or rename bot is unavailable.")
        source_id = int(source_message.chat.id)
        source_message_id = int(source_message.id)
        job = job or {
            "source_channel_id": source_id,
            "source_message_id": source_message_id,
            "output_channel_id": config.get("output_channel_id"),
            "original_filename": self._source_filename(source_message),
            "original_caption": getattr(source_message, "caption", None),
            "media_metadata": extract_video_metadata(self._source_filename(source_message), getattr(source_message, "caption", None)),
        }
        output_context = output_context or self._register_output_waiter(job, accepting_output=False)
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
            logger.info("Sending configured thumbnail to rename bot for source=%s message=%s", source_id, source_message_id)
            sent_photo = await self._bounded_flood_retry(self.user_client.send_photo, self.rename_bot_id, photo)
            logger.info(
                "Thumbnail send succeeded for source=%s message=%s rename_bot_message=%s",
                source_id,
                source_message_id,
                getattr(sent_photo, "id", None),
            )
            await self._advance_job_stage(source_id, source_message_id, "thumbnail_sending", "thumbnail_sent")
            await self._advance_job_stage(source_id, source_message_id, "thumbnail_sent", "media_sending")
            logger.info("Submitting source media to rename bot for source=%s message=%s", source_id, source_message_id)
            sent_media = await self._bounded_flood_retry(source_message.copy, self.rename_bot_id)
            logger.info(
                "Source media submission succeeded for source=%s message=%s rename_bot_message=%s",
                source_id,
                source_message_id,
                getattr(sent_media, "id", None),
            )
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
            original = extract_original_filename(text, job.get("original_filename") or self._source_filename(source_message))
            if not original:
                raise RuntimeError("The rename bot did not provide a usable original filename.")
            username = await db.get_username()
            metadata = job.get("media_metadata") or extract_video_metadata(original, getattr(source_message, "caption", None))
            new_filename = job.get("expected_filename") or format_standardized_filename(username, metadata, original)
            if not new_filename:
                raise RuntimeError("The source filename is unavailable; refusing to invent a rename target.")
            output_context["expected_filename"] = new_filename
            output_context["original_filename"] = job.get("original_filename") or original
            output_context["original_caption"] = job.get("original_caption", getattr(source_message, "caption", None))
            output_context["media_metadata"] = dict(metadata)
            await self._advance_job_stage(
                source_id,
                source_message_id,
                "filename_prompt_received",
                "filename_sending",
                expected_filename=new_filename,
            )
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
                submission_status="submitting",
                callback_attempted_at=datetime.now(timezone.utc),
                callback_attempt_count=1,
                expected_filename=new_filename,
                output_reply_ids=[int(value) for value in (getattr(sent_media, "id", None), getattr(filename_reply, "id", None), getattr(menu, "id", None)) if value is not None],
                expected_output_message_ids=[int(value) for value in (getattr(sent_media, "id", None), getattr(filename_reply, "id", None), getattr(menu, "id", None)) if value is not None],
            )
            job.update({
                "expected_filename": new_filename,
                "rename_bot_media_message_id": getattr(sent_media, "id", None),
                "filename_message_id": getattr(filename_reply, "id", None),
                "rename_bot_menu_message_id": getattr(menu, "id", None),
            })
            output_context = self._register_output_waiter(
                job,
                expected_filename=new_filename,
                reply_ids=[getattr(sent_media, "id", None), getattr(filename_reply, "id", None), getattr(menu, "id", None)],
                accepting_output=True,
            )
            output_context["submitted_order"] = datetime.now(timezone.utc)
            self._reconcile_unmatched_outputs()
            callback_unknown = False
            try:
                await self.user_client.request_callback_answer(
                    chat_id=self.rename_bot_id,
                    message_id=menu.id,
                    callback_data=second_button.callback_data,
                    timeout=RENAME_RESPONSE_TIMEOUT,
                )
            except BotResponseTimeout:
                # The click may have been accepted despite the missing callback answer.
                # Fence it as uncertain, keep tracking output, and never click again.
                logger.warning("Rename-bot callback acknowledgement timed out for source=%s/%s; continuing queue without retrying.", source_id, source_message_id)
                await self._advance_job_stage(
                    source_id,
                    source_message_id,
                    "callback_submitting",
                    "callback_unknown",
                    status="submission_unknown",
                    submission_status="submission_unknown",
                    callback_outcome="unknown_after_timeout",
                    submitted_at=datetime.now(timezone.utc),
                )
                callback_unknown = True
            except Exception as exc:
                # Network/RPC failure after issuing a non-idempotent callback is
                # ambiguous too; do not repeat it or block later queued submissions.
                logger.exception("Rename-bot callback result is uncertain for source=%s/%s.", source_id, source_message_id)
                await self._advance_job_stage(
                    source_id,
                    source_message_id,
                    "callback_submitting",
                    "callback_unknown",
                    status="submission_unknown",
                    submission_status="submission_unknown",
                    callback_outcome=f"unknown_after_error: {str(exc)[:300]}",
                    submitted_at=datetime.now(timezone.utc),
                )
                callback_unknown = True
            else:
                await self._advance_job_stage(
                    source_id,
                    source_message_id,
                    "callback_submitting",
                    "submitted",
                    status="submitted",
                    submission_status="submitted",
                    callback_outcome="acknowledged",
                    callback_completed_at=datetime.now(timezone.utc),
                    submitted_at=datetime.now(timezone.utc),
                )
            return {
                "source_id": source_id,
                "message_id": source_message_id,
                "expected_filename": new_filename,
                "context": output_context,
                "submission_unknown": callback_unknown,
            }

    async def _publish_result(
        self,
        final_message,
        output_id: int,
        thumbnail: bytes | None,
        expected_filename: str,
        *,
        caption=_CAPTION_UNSET,
    ):
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
            preserve_output_caption = caption is _CAPTION_UNSET
            send_options = {
                "caption": (final_message.caption or "") if preserve_output_caption else (caption or ""),
                "thumb": thumb,
                "file_name": filename,
            }
            caption_entities = getattr(final_message, "caption_entities", None) if preserve_output_caption else None
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
        key = (int(source_id), int(message_id))
        try:
            job = await db.get_job(source_id, message_id)
            if not job:
                return False
            if job.get("status") == "completed":
                return True
            if job.get("status") == "superseded":
                return False
            if job.get("status") == "failed":
                return False
        except Exception:
            logger.exception("Could not load or claim persisted rename job source=%s message=%s", source_id, message_id)
            return False

        try:
            config = await db.get_channel_config(source_id)
            if not config:
                raise RuntimeError("Source-channel configuration was removed.")
            status = job.get("status")
            if status == "pending":
                if int(job.get("attempts", 0)) >= MAX_RENAME_ATTEMPTS:
                    await db.update_job(source_id, message_id, status="failed", submission_status="failed", error="Maximum processing attempts reached.")
                    return False
                if not await db.claim_job(source_id, message_id):
                    return False
                output_context = self._register_output_waiter(job, accepting_output=False)
                logger.info(
                    "Claimed global FIFO job=%s Channel %s source=%s message=%s destination=%s; only this job may interact with the rename bot until published.",
                    job.get("job_id") or f"{source_id}:{message_id}",
                    job.get("source_channel_number", "?"),
                    source_id,
                    message_id,
                    job.get("output_channel_id"),
                )
                source_message = await self.user_client.get_messages(source_id, message_id)
                if not source_message or not (source_message.video or source_message.document):
                    raise RuntimeError("Source message is no longer available or is not a video/document.")
                submission = await self._rename_conversation(source_message, config, job, output_context)
                current = await db.get_job(source_id, message_id)
                if not current or current.get("status") == "superseded":
                    logger.error("Job changed to a non-processable state during rename: %s", job.get("job_id"))
                    return False
                job["expected_filename"] = submission["expected_filename"]
                output_context = submission["context"]
            elif status in {"submitted", "submission_unknown"}:
                output_context = self._register_output_waiter(job, accepting_output=True)
                await self._recover_recent_outputs()
            else:
                logger.error("FIFO job has unexpected persistent state job=%s status=%s", job.get("job_id"), status)
                return False

            output_task = self._ensure_output_task(job, config, output_context)
            await output_task
            current = await db.get_job(source_id, message_id)
            return bool(current and current.get("status") == "completed")
        except asyncio.CancelledError:
            current = await db.get_job(source_id, message_id)
            if current and current.get("status") == "superseded":
                logger.info("Stopped superseded rename workflow job=%s without changing its persistent state.", current.get("job_id"))
                raise
            stage = current.get("stage") if current else None
            if stage == "preflight" and self._stopping_session:
                await db.update_job(source_id, message_id, status="pending", submission_status="queued", stage="preflight", error="Session stopped before any rename-bot action; safe to resume.")
            elif stage in {"callback_submitting", "callback_unknown"} and key in self._output_waiters:
                if self._stopping_session:
                    await db.update_job(source_id, message_id, status="submission_unknown", submission_status="submission_unknown", stage="callback_unknown", error="Session stopped during callback; output tracking will be restored without resubmission.")
                else:
                    await db.update_job(source_id, message_id, status="failed", submission_status="failed", stage="cancelled", error="Cancelled by administrator during callback.")
                    context = self._output_waiters.pop(key)
                    if not context["future"].done():
                        context["future"].cancel()
            elif self._stopping_session and stage in {"submitted", "submission_unknown"}:
                await db.update_job(
                    source_id,
                    message_id,
                    status="submission_unknown" if stage == "submission_unknown" else "submitted",
                    submission_status="submission_unknown" if stage == "submission_unknown" else "submitted",
                    publication_status="pending",
                    error="Session stopped while waiting for the renamed output; restored without resubmitting.",
                )
            else:
                await db.update_job(source_id, message_id, status="failed", submission_status="failed", stage="cancelled", error="Cancelled by administrator.")
            if key in self._output_waiters and stage not in {"callback_submitting", "callback_unknown", "submitted", "submission_unknown"}:
                context = self._output_waiters.pop(key)
                if not context["future"].done():
                    context["future"].cancel()
            raise
        except Exception as exc:
            logger.exception("Rename job failed for source=%s message=%s", source_id, message_id)
            error = str(exc)[:1000]
            current = await db.get_job(source_id, message_id)
            if current and current.get("status") == "superseded":
                logger.info("Ignored error from superseded rename workflow job=%s: %s", current.get("job_id"), error)
                return
            attempt_count = int(current.get("attempts", 0)) if current else MAX_RENAME_ATTEMPTS
            stage = current.get("stage") if current else "publishing"
            if stage in {"callback_submitting", "callback_unknown"} and key in self._output_waiters:
                await db.update_job(
                    source_id,
                    message_id,
                    status="submission_unknown",
                    submission_status="submission_unknown",
                    stage="callback_unknown",
                    error=error,
                    submitted_at=datetime.now(timezone.utc),
                )
                config = await db.get_channel_config(source_id)
                if current and config:
                    output_task = self._ensure_output_task(current, config, self._output_waiters[key])
                    await output_task
                    final = await db.get_job(source_id, message_id)
                    return bool(final and final.get("status") == "completed")
                return False
            # Once the rename bot has seen any side effect, restarting the full
            # conversation can rename the same media again or repeat its callback.
            can_retry = self._safe_to_retry(stage, attempt_count)
            await db.update_job(
                source_id,
                message_id,
                status="pending" if can_retry else "failed",
                stage="preflight" if can_retry else stage,
                submission_status="queued" if can_retry else "failed",
                error=error,
            )
            context = self._output_waiters.pop(key, None)
            if context and not context["future"].done():
                context["future"].cancel()
            return False


rename_runtime = RenameRuntime()
