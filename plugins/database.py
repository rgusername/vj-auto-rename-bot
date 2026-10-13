"""Mongo persistence for user sessions and rename jobs."""

from datetime import datetime, timezone

import motor.motor_asyncio
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
        await self.jobs.create_index([("source_channel_id", 1), ("source_message_id", 1)], unique=True)
        await self.jobs.create_index("status")
        await self.jobs.create_index([("status", 1), ("created_at", 1)])

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

    async def set_channel_config(self, source_id: int, output_id: int, thumbnail: bytes | None = None):
        fields = {"output_channel_id": int(output_id), "updated_at": datetime.now(timezone.utc)}
        if thumbnail is not None:
            fields["thumbnail"] = thumbnail
        await self.configs.update_one(
            {"source_channel_id": int(source_id)},
            {"$set": fields, "$setOnInsert": {"source_channel_id": int(source_id), "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def set_channel_thumbnail(self, source_id: int, thumbnail: bytes):
        result = await self.configs.update_one(
            {"source_channel_id": int(source_id)},
            {"$set": {"thumbnail": thumbnail, "updated_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count > 0

    async def get_channel_config(self, source_id: int) -> dict | None:
        return await self.configs.find_one({"source_channel_id": int(source_id)})

    async def list_channel_configs(self) -> list[dict]:
        return await self.configs.find({}).sort("source_channel_id", 1).to_list(length=1000)

    async def set_username(self, username: str):
        await self.settings.update_one(
            {"_id": "filename_username"},
            {"$set": {"value": username, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_username(self) -> str:
        record = await self.settings.find_one({"_id": "filename_username"})
        return str(record.get("value", "")) if record else ""

    async def create_job(self, source_id: int, message_id: int, output_id: int, media_metadata: dict | None = None) -> bool:
        now = datetime.now(timezone.utc)
        record = {
            "source_channel_id": int(source_id),
            "source_message_id": int(message_id),
            "output_channel_id": int(output_id),
            "status": "pending",
            "stage": "queued",
            "workflow_version": 3,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        if media_metadata:
            record["media_metadata"] = dict(media_metadata)
        try:
            await self.jobs.insert_one(record)
            return True
        except DuplicateKeyError:
            return False

    async def get_job(self, source_id: int, message_id: int) -> dict | None:
        return await self.jobs.find_one({"source_channel_id": int(source_id), "source_message_id": int(message_id)})

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

    async def get_output_jobs(self):
        """Jobs that were submitted (or whose callback result was uncertain) but lack output."""
        return self.jobs.find({"status": {"$in": ["submitted", "submission_unknown"]}}).sort(
            [("submitted_at", 1), ("created_at", 1), ("_id", 1)]
        )

    async def begin_output_publish(self, source_id: int, message_id: int, output_message_id: int) -> bool:
        """Atomically fence the one output upload attempt for a submitted job."""
        result = await self.jobs.update_one(
            {
                "source_channel_id": int(source_id),
                "source_message_id": int(message_id),
                "status": {"$in": ["submitted", "submission_unknown"]},
            },
            {
                "$set": {
                    "status": "publishing",
                    "stage": "publishing",
                    "output_candidate_message_id": int(output_message_id),
                    "publishing_started_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count == 1

    async def add_output_reply_id(self, source_id: int, message_id: int, reply_id: int):
        await self.jobs.update_one(
            {"source_channel_id": int(source_id), "source_message_id": int(message_id)},
            {"$addToSet": {"output_reply_ids": int(reply_id)}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )

    async def job_counts(self) -> dict[str, int]:
        counts = {}
        for status in ("pending", "processing", "submitting", "submitted", "submission_unknown", "publishing", "completed", "failed"):
            counts[status] = await self.jobs.count_documents({"status": status})
        return counts

    async def recover_interrupted_jobs(self, max_attempts: int = 3):
        """Recover queued/submitted jobs without re-sending non-idempotent rename actions."""
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": "preflight"},
            {"$set": {"status": "pending", "error": "Recovered before rename-bot interaction; safe to retry.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": {"$in": ["callback_submitting", "callback_unknown"]}},
            {"$set": {"status": "submission_unknown", "stage": "callback_unknown", "error": "Restarted while callback acknowledgement was uncertain; output tracking restored without resubmission.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {"status": "submitting", "workflow_version": 3, "stage": {"$nin": ["preflight", "callback_submitting", "callback_unknown"]}},
            {"$set": {"status": "failed", "error": "Interrupted during submission; not restarted to prevent duplicate rename/callback actions.", "updated_at": datetime.now(timezone.utc)}},
        )
        await self.jobs.update_many(
            {
                "status": "publishing",
            },
            {"$set": {"status": "failed", "stage": "publishing", "error": "Interrupted during output publish; inspect output channel before manual retry.", "updated_at": datetime.now(timezone.utc)}},
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
            {"$set": {"status": "pending", "error": "Scheduled for bounded retry after restart.", "updated_at": datetime.now(timezone.utc)}},
        )

    async def close(self):
        self._client.close()


db = Database(DB_URI, DB_NAME)
