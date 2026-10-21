"""Mongo persistence for user sessions and rename jobs."""

from datetime import datetime, timedelta, timezone

import motor.motor_asyncio
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config import DB_NAME, DB_URI

class Database:
    def __init__(self, uri: str, database_name: str):
        # A harmless local default keeps module imports/test discovery credential-free;
        # bot.py validates DB_URI before attempting an actual database connection.
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri or "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=10000)
        self.db = self._client[database_name]
        # Keep the original collection and session fields for /login and /logout.
        self.col = self.db.users
        self.configs = self.db.rename_channel_configs
        self.jobs = self.db.rename_jobs
        self.settings = self.db.rename_settings

    async def init(self):
        """Check connectivity and install uniqueness constraints once at startup."""
        await self._client.admin.command("ping")
        await self.configs.create_index("source_channel_id", unique=True)
        await self._migrate_channel_numbers()
        await self.configs.create_index("channel_number", unique=True)
        await self.jobs.create_index([("source_channel_id", 1), ("source_message_id", 1)], unique=True)
        await self.jobs.create_index("status")
        await self.jobs.create_index([("status", 1), ("created_at", 1)])
        await self.jobs.create_index(
            "rename_bot_output_message_id",
            unique=True,
            partialFilterExpression={"rename_bot_output_message_id": {"$type": "number"}},
        )

    def new_user(self, user_id: int, name: str) -> dict:
        return {"id": int(user_id), "name": name, "session": None}

    async def add_user(self, user_id: int, name: str):
        await self.col.update_one(
            {"id": int(user_id)},
            {"$setOnInsert": self.new_user(user_id, name)},
            upsert=True,
        )

    async def is_user_exist(self, user_id: int) -> bool:
        return await self.col.find_one({"id": int(user_id)}, {"_id": 1}) is not None

    async def total_users_count(self) -> int:
        return await self.col.count_documents({})

    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id: int):
        await self.col.delete_many({"id": int(user_id)})

    async def set_session(self, user_id: int, session: str | None):
        await self.col.update_one({"id": int(user_id)}, {"$set": {"session": session}}, upsert=True)

    async def get_session(self, user_id: int) -> str | None:
        user = await self.col.find_one({"id": int(user_id)}, {"session": 1})
        return user.get("session") if user else None

    async def _allocate_channel_number(self) -> int:
        query = {"_id": "channel_identity_counter"}
        update = {"$inc": {"value": 1}}
        try:
            record = await self.settings.find_one_and_update(
                query, update, upsert=True, return_document=ReturnDocument.AFTER
            )
        except DuplicateKeyError:
            # Another simultaneous /set created the counter first; increment it.
            record = await self.settings.find_one_and_update(
                query, update, upsert=False, return_document=ReturnDocument.AFTER
            )
        return int(record["value"])

    async def _ensure_channel_number(self, source_id: int) -> int | None:
        source_id = int(source_id)
        config = await self.configs.find_one({"source_channel_id": source_id}, {"channel_number": 1})
        if config is None:
            return None
        if config.get("channel_number") is not None:
            return int(config["channel_number"])
        number = await self._allocate_channel_number()
        result = await self.configs.update_one(
            {
                "source_channel_id": source_id,
                "$or": [
                    {"channel_number": {"$exists": False}},
                    {"channel_number": None},
                ],
            },
            {"$set": {"channel_number": number}},
        )
        if result.modified_count:
            return number
        config = await self.configs.find_one({"source_channel_id": source_id}, {"channel_number": 1})
        return int(config["channel_number"]) if config and config.get("channel_number") is not None else None

    async def _migrate_channel_numbers(self):
        numbered = await self.configs.find({"channel_number": {"$exists": True}}).sort("channel_number", -1).limit(1).to_list(length=1)
        if numbered:
            await self.settings.update_one(
                {"_id": "channel_identity_counter"},
                {"$max": {"value": int(numbered[0]["channel_number"])}},
                upsert=True,
            )
        legacy = self.configs.find({
            "$or": [
                {"channel_number": {"$exists": False}},
                {"channel_number": None},
            ]
        }).sort(
            [("created_at", 1), ("source_channel_id", 1)]
        )
        async for config in legacy:
            await self._ensure_channel_number(int(config["source_channel_id"]))

    async def set_channel_config(self, source_id: int, output_id: int, thumbnail: bytes | None = None) -> int:
        source_id = int(source_id)
        existing = await self.configs.find_one({"source_channel_id": source_id}, {"channel_number": 1})
        if existing and existing.get("channel_number") is not None:
            channel_number = int(existing["channel_number"])
        elif existing:
            channel_number = await self._ensure_channel_number(source_id)
        else:
            channel_number = await self._allocate_channel_number()
        fields = {"output_channel_id": int(output_id), "updated_at": datetime.now(timezone.utc)}
        if thumbnail is not None:
            fields["thumbnail"] = thumbnail
        await self.configs.update_one(
            {"source_channel_id": source_id},
            {"$set": fields, "$setOnInsert": {
                "source_channel_id": source_id,
                "channel_number": channel_number,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        saved = await self.configs.find_one({"source_channel_id": source_id}, {"channel_number": 1})
        if saved and saved.get("channel_number") is not None:
            return int(saved["channel_number"])
        number = await self._ensure_channel_number(source_id)
        if number is None:
            raise RuntimeError(f"Could not persist stable channel identity for source {source_id}.")
        return number

    async def set_channel_thumbnail(self, source_id: int, thumbnail: bytes):
        result = await self.configs.update_one(
            {"source_channel_id": int(source_id)},
            {"$set": {"thumbnail": thumbnail, "updated_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count > 0

    async def get_channel_config(self, source_id: int) -> dict | None:
        return await self.configs.find_one({"source_channel_id": int(source_id)})

    async def list_channel_configs(self) -> list[dict]:
        return await self.configs.find({}).sort([("channel_number", 1), ("source_channel_id", 1)]).to_list(length=1000)

    async def set_username(self, username: str):
        await self.settings.update_one(
            {"_id": "filename_username"},
            {"$set": {"value": username, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_username(self) -> str:
        record = await self.settings.find_one({"_id": "filename_username"})
        return str(record.get("value", "")) if record else ""

    async def set_output_caption(self, output_channel_id: int, template: str):
        """Persist an output caption template independently for each destination channel."""
        await self.settings.update_one(
            {"_id": f"output_caption:{int(output_channel_id)}"},
            {
                "$set": {
                    "output_channel_id": int(output_channel_id),
                    "value": str(template),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def get_output_caption(self, output_channel_id: int) -> str | None:
        record = await self.settings.find_one({"_id": f"output_caption:{int(output_channel_id)}"})
        return str(record["value"]) if record and "value" in record else None

    async def create_job(
        self,
        source_id: int,
        message_id: int,
        output_id: int,
        media_metadata: dict | None = None,
        *,
        channel_number: int | None = None,
        original_filename: str | None = None,
        original_caption: str | None = None,
        expected_filename: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        record = {
            "job_id": f"{int(source_id)}:{int(message_id)}",
            "source_channel_id": int(source_id),
            "source_message_id": int(message_id),
            "output_channel_id": int(output_id),
            "destination_channel_id": int(output_id),
            "original_filename": original_filename,
            "original_caption": original_caption,
            "expected_filename": expected_filename,
            "expected_output_message_id": None,
            "expected_output_message_ids": [],
            "rename_bot_output_message_id": None,
            "destination_message_id": None,
            "status": "pending",
            "submission_status": "queued",
            "publication_status": "pending",
            "stage": "queued",
            "workflow_version": 3,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        if channel_number is not None:
            record["source_channel_number"] = int(channel_number)
        if media_metadata:
            record["media_metadata"] = dict(media_metadata)
        try:
            await self.jobs.insert_one(record)
            return True
        except DuplicateKeyError:
            return False

    async def get_job(self, source_id: int, message_id: int) -> dict | None:
        return await self.jobs.find_one({"source_channel_id": int(source_id), "source_message_id": int(message_id)})

    async def get_superseded_jobs(self):
        return self.jobs.find({"status": "superseded"}).sort(
            [("superseded_at", -1), ("created_at", -1), ("_id", -1)]
        )

    async def restore_superseded_output_jobs(self) -> int:
        """Restore only superseded jobs known to have issued the callback; never replay rename actions."""
        result = await self.jobs.update_many(
            {"status": "superseded", "callback_attempted_at": {"$exists": True}},
            {
                "$set": {
                    "status": "submission_unknown",
                    "submission_status": "submission_unknown",
                    "publication_status": "pending",
                    "stage": "callback_unknown",
                    "error": "Restored output tracking for a previously superseded callback; rename actions were not repeated.",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return int(result.modified_count)

    async def get_matched_output_owners(self) -> list[dict]:
        cursor = self.jobs.find(
            {"rename_bot_output_message_id": {"$type": "number"}},
            {"source_channel_id": 1, "source_message_id": 1, "rename_bot_output_message_id": 1},
        )
        return await cursor.to_list(length=None)

    async def add_superseded_output(self, source_id: int, message_id: int, output_message_id: int):
        await self.jobs.update_one(
            {"source_channel_id": int(source_id), "source_message_id": int(message_id), "status": "superseded"},
            {
                "$addToSet": {"superseded_output_message_ids": int(output_message_id)},
                "$set": {"last_superseded_output_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
            },
        )

    async def update_job(self, source_id: int, message_id: int, **fields):
        fields["updated_at"] = datetime.now(timezone.utc)
        await self.jobs.update_one(
            {"source_channel_id": int(source_id), "source_message_id": int(message_id)},
            {"$set": fields},
        )

    async def advance_job_stage(self, source_id: int, message_id: int, expected_stage: str, stage: str, **fields) -> bool:
        """Atomically checkpoint a workflow step before or after an external side effect."""
        fields.update({"stage": stage, "updated_at": datetime.now(timezone.utc)})
        result = await self.jobs.update_one(
            {
                "source_channel_id": int(source_id),
                "source_message_id": int(message_id),
                "status": {"$in": ["submitting", "processing"]},
                "stage": expected_stage,
            },
            {"$set": fields},
        )
        return result.modified_count == 1

    async def claim_job(self, source_id: int, message_id: int) -> bool:
        result = await self.jobs.update_one(
            {
                "source_channel_id": int(source_id),
                "source_message_id": int(message_id),
                "status": "pending",
                "stage": {"$in": ["queued", "preflight"]},
            },
            {
                "$set": {
                    "status": "submitting",
                    "submission_status": "submitting",
                    "stage": "preflight",
                    "workflow_version": 3,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$inc": {"attempts": 1},
            },
        )
        return result.modified_count == 1

    async def get_pending_jobs(self):
        return self.jobs.find({"status": "pending"}).sort([("created_at", 1), ("_id", 1)])

    async def get_queue_jobs(self):
        """Return nonterminal jobs in durable FIFO order for the single global worker."""
        return self.jobs.find(
            {
                "status": {"$in": ["pending", "submitted", "submission_unknown", "failed"]},
                "stage": {"$ne": "cancelled"},
            }
        ).sort([("created_at", 1), ("_id", 1)])

    async def clear_queue_jobs(self) -> int:
        """Delete queued/in-flight job records while preserving completed history.

        Submission checkpoints and output anchors are stored on the job document,
        so deleting non-completed records also removes their stale durable queue
        state. Channel mappings, sessions, settings, and completed jobs live in
        other documents and are not touched.
        """
        result = await self.jobs.delete_many({"status": {"$ne": "completed"}})
        return int(result.deleted_count)

    async def acquire_queue_lease(self, owner: str, lease_seconds: int = 60) -> bool:
        """Atomically acquire or refresh the singleton cross-instance queue lease."""
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=int(lease_seconds))
        query = {
            "_id": "global_rename_queue_lease",
            "clearing": {"$ne": True},
            "$or": [{"lease_until": {"$lte": now}}, {"owner": str(owner)}],
        }
        record = await self.settings.find_one_and_update(
            query,
            {"$set": {"owner": str(owner), "lease_until": lease_until, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if record:
            return True
        try:
            await self.settings.insert_one({
                "_id": "global_rename_queue_lease",
                "owner": str(owner),
                "lease_until": lease_until,
                "updated_at": now,
            })
            return True
        except DuplicateKeyError:
            return False

    async def renew_queue_lease(self, owner: str, lease_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        result = await self.settings.update_one(
            {
                "_id": "global_rename_queue_lease",
                "owner": str(owner),
                "clearing": {"$ne": True},
                "lease_until": {"$gt": now},
            },
            {"$set": {"lease_until": now + timedelta(seconds=int(lease_seconds)), "updated_at": now}},
        )
        return result.matched_count == 1

    async def begin_queue_clear(self, owner: str, lease_seconds: int = 120) -> dict | None:
        """Fence queue workers and return the lease record that was displaced."""
        now = datetime.now(timezone.utc)
        query = {
            "_id": "global_rename_queue_lease",
            "$or": [
                {"clearing": {"$ne": True}},
                {"clear_lease_until": {"$lte": now}},
                {"clear_owner": str(owner)},
            ],
        }
        previous = await self.settings.find_one_and_update(
            query,
            {
                "$set": {
                    "owner": str(owner),
                    "lease_until": now + timedelta(seconds=int(lease_seconds)),
                    "clearing": True,
                    "clear_owner": str(owner),
                    "clear_lease_until": now + timedelta(seconds=int(lease_seconds)),
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.BEFORE,
        )
        if previous:
            return previous
        try:
            await self.settings.insert_one({
                "_id": "global_rename_queue_lease",
                "owner": str(owner),
                "lease_until": now + timedelta(seconds=int(lease_seconds)),
                "clearing": True,
                "clear_owner": str(owner),
                "clear_lease_until": now + timedelta(seconds=int(lease_seconds)),
                "updated_at": now,
            })
            return {}
        except DuplicateKeyError:
            return None

    async def end_queue_clear(self, owner: str) -> bool:
        now = datetime.now(timezone.utc)
        result = await self.settings.update_one(
            {
                "_id": "global_rename_queue_lease",
                "clear_owner": str(owner),
                "clearing": True,
            },
            {
                "$set": {
                    "clearing": False,
                    "lease_until": now,
                    "clear_lease_until": now,
                    "updated_at": now,
                },
                "$unset": {"clear_owner": ""},
            },
        )
        return result.modified_count == 1

    async def release_queue_lease(self, owner: str) -> bool:
        now = datetime.now(timezone.utc)
        result = await self.settings.update_one(
            {"_id": "global_rename_queue_lease", "owner": str(owner)},
            {"$set": {"lease_until": now, "updated_at": now}},
        )
        return result.matched_count == 1

    async def get_output_jobs(self):
        """Jobs that were submitted (or whose callback result was uncertain) but lack output."""
        return self.jobs.find({"status": {"$in": ["submitted", "submission_unknown"]}}).sort(
            [("submitted_at", 1), ("created_at", 1), ("_id", 1)]
        )

    async def begin_output_publish(self, source_id: int, message_id: int, output_message_id: int) -> bool:
        """Atomically claim copying one matched rename-bot message to its destination."""
        try:
            result = await self.jobs.update_one(
                {
                    "source_channel_id": int(source_id),
                    "source_message_id": int(message_id),
                    "status": {"$in": ["submitted", "submission_unknown"]},
                },
                {
                    "$set": {
                        "status": "publishing",
                        "submission_status": "submitted",
                        "publication_status": "publishing",
                        "stage": "publishing",
                        "output_candidate_message_id": int(output_message_id),
                        "rename_bot_output_message_id": int(output_message_id),
                        "publishing_started_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            return result.modified_count == 1
        except DuplicateKeyError:
            # The same rename-bot message has already been assigned to another
            # source job; never publish it twice or to a second destination.
            return False

    async def add_output_reply_id(self, source_id: int, message_id: int, reply_id: int):
        await self.jobs.update_one(
            {"source_channel_id": int(source_id), "source_message_id": int(message_id)},
            {"$addToSet": {"output_reply_ids": int(reply_id)}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )

    async def job_counts(self) -> dict[str, int]:
        counts = {}
        for status in ("pending", "processing", "submitting", "submitted", "submission_unknown", "publishing", "completed", "failed", "superseded"):
            counts[status] = await self.jobs.count_documents({"status": status})
        return counts

    async def recover_interrupted_jobs(self, max_attempts: int = 3):
        """Recover queued/submitted jobs without re-sending non-idempotent rename actions."""
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": "preflight"},
            {"$set": {"status": "pending", "submission_status": "queued", "error": "Recovered before rename-bot interaction; safe to retry.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": {"$in": ["callback_submitting", "callback_unknown"]}},
            {"$set": {"status": "submission_unknown", "submission_status": "submission_unknown", "stage": "callback_unknown", "error": "Restarted while callback acknowledgement was uncertain; output tracking restored without resubmission.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": {"$nin": ["preflight", "callback_submitting", "callback_unknown"]}},
            {"$set": {"status": "failed", "submission_status": "failed", "error": "Interrupted during submission; not restarted to prevent duplicate rename/callback actions.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "publishing", "destination_message_id": {"$type": "number"}},
            {"$set": {"status": "submitted", "submission_status": "submitted", "publication_status": "caption_edit_retry", "stage": "caption_edit_retry", "error": "Restarted after Telegram copy succeeded; resume caption editing on the stored destination message without copying again.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "publishing"},
            {"$set": {"status": "failed", "publication_status": "copy_uncertain", "stage": "copy_uncertain", "error": "Restarted during Telegram message copy without a persisted destination message ID; not copied again to avoid duplicate delivery.", "updated_at": datetime.now(timezone.utc)}},
        )
        # Older records have no separate submission/output checkpoints. An attempted
        # legacy workflow is fenced instead of blindly restarting its callback.
        await self.jobs.update_many(
            {
                "workflow_version": {"$ne": 3},
                "status": {"$in": ["pending", "processing", "submitting", "failed"]},
                "attempts": {"$gt": 0},
                "stage": {"$nin": ["publishing", "cancelled"]},
            },
            {"$set": {"status": "failed", "stage": "legacy_ambiguous", "error": "Legacy job state is ambiguous; not retried to avoid repeating a rename-bot workflow.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": {"$ne": 3}},
            {"$set": {"status": "failed", "error": "Legacy submission interrupted; not restarted to prevent duplicates.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "failed", "workflow_version": 3, "stage": "preflight", "attempts": {"$lt": int(max_attempts)}},
            {"$set": {"status": "pending", "submission_status": "queued", "error": "Scheduled for bounded retry after restart.", "updated_at": datetime.now(timezone.utc)}},
        )

    async def close(self):
        self._client.close()


db = Database(DB_URI, DB_NAME)
