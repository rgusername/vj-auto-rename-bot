"""User-facing bot commands for VJ Auto Rename Bot."""

import asyncio
import io
import logging
import re

from PIL import Image, ImageOps
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import RPCError

from config import ADMINS, LOG_CHANNEL
from plugins.database import db
from plugins.rename_runtime import rename_runtime

logger = logging.getLogger(__name__)
_pending_config_users: set[int] = set()


def _normalize_thumbnail(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((320, 320), Image.Resampling.LANCZOS)
        for _ in range(10):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            result = output.getvalue()
            if len(result) <= 190_000:
                return result
            image = image.resize((max(1, int(image.width * 0.85)), max(1, int(image.height * 0.85))), Image.Resampling.LANCZOS)
        raise ValueError("Thumbnail could not be reduced below 190 KB.")


def _is_admin(message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS)


async def _require_admin(message) -> bool:
    if _is_admin(message):
        return True
    await message.reply_text("This command is restricted to configured administrators.")
    return False


@Client.on_message(filters.command("start") & filters.private)
async def start_message(client, message):
    if message.from_user:
        await db.add_user(message.from_user.id, message.from_user.first_name or "Telegram user")
        if LOG_CHANNEL:
            try:
                await client.send_message(LOG_CHANNEL, f"New user: <code>{message.from_user.id}</code>")
            except Exception:
                logger.debug("Could not send new-user log message.", exc_info=True)
    await message.reply_text(
        "<b>VJ Auto Rename Bot</b>\n\n"
        "Use /login to connect the Telegram user account that can read your source channels. "
        "Administrators can configure mappings with /set and a suffix with /setusername."
    )


@Client.on_message(filters.command("set") & filters.private)
async def set_channel(client, message):
    if not await _require_admin(message):
        return
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        return await message.reply_text("Usage: <code>/set SOURCE_CHANNEL_ID OUTPUT_CHANNEL_ID</code>")
    try:
        source_id, output_id = int(parts[1]), int(parts[2])
        if source_id >= 0 or output_id >= 0:
            raise ValueError
    except ValueError:
        return await message.reply_text("Both channel IDs must be valid negative Telegram chat IDs, usually beginning with <code>-100</code>.")

    if not rename_runtime.user_client:
        return await message.reply_text("Log in first with /login; the saved user account is needed to validate and access both channels.")
    try:
        source_chat = await rename_runtime.user_client.get_chat(source_id)
        output_chat = await rename_runtime.user_client.get_chat(output_id)
        if source_chat.type != ChatType.CHANNEL:
            raise ValueError("The source chat is not a channel.")
        if output_chat.type != ChatType.CHANNEL:
            raise ValueError("The output chat is not a channel.")
        account = await rename_runtime.user_client.get_me()
        source_member = await rename_runtime.user_client.get_chat_member(source_id, account.id)
        if source_member.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            raise ValueError("The logged-in account is not a member allowed to read the source channel.")
        output_member = await rename_runtime.user_client.get_chat_member(output_id, account.id)
        if output_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            raise ValueError("The logged-in account must be an administrator of the output channel.")
        if output_member.status == ChatMemberStatus.ADMINISTRATOR and not (
            output_member.privileges and output_member.privileges.can_post_messages
        ):
            raise ValueError("The logged-in account's output-channel administrator role cannot post messages.")
    except ValueError as exc:
        return await message.reply_text(f"Channel validation failed: {exc}")
    except RPCError as exc:
        return await message.reply_text(f"Channel validation failed. Ensure the logged-in account can access both chats. Details: <code>{str(exc)[:300]}</code>")

    try:
        await db.set_channel_config(source_id, output_id)
    except Exception:
        logger.exception("Could not persist source/output channel mapping.")
        return await message.reply_text("Database error: the channel mapping could not be saved.")
    _pending_config_users.add(message.from_user.id)
    listener_task = asyncio.create_task(client.listen(
        message.chat.id,
        filters=filters.photo | filters.command("cancel"),
        timeout=600,
    ))
    try:
        # Let the listener register before the prompt can trigger a fast reply.
        await asyncio.sleep(0)
        await message.reply_text(
            f"Mapping saved: <code>{source_id}</code> → <code>{output_id}</code>.\n"
            "Now send the thumbnail photo for this source channel. Send /cancel to stop."
        )
        photo_message = await listener_task
        if photo_message.text and photo_message.text.split()[0].split("@", 1)[0].lower() == "/cancel":
            await photo_message.reply_text("Thumbnail setup cancelled. The channel mapping is saved; its existing thumbnail was left unchanged.")
            return
        photo = photo_message.photo
        if not photo:
            return await photo_message.reply_text("Please send a photo, or use /cancel.")
        data = await photo_message.download(in_memory=True)
        if hasattr(data, "getvalue"):
            image_bytes = data.getvalue()
        else:
            image_bytes = bytes(data)
        if not image_bytes:
            return await photo_message.reply_text("The photo could not be downloaded; please run /set again.")
        image_bytes = _normalize_thumbnail(image_bytes)
        if not await db.set_channel_thumbnail(source_id, image_bytes):
            return await photo_message.reply_text("The mapping is no longer present. Please run /set again.")
        await rename_runtime.resume_pending_jobs()
        await photo_message.reply_text(f"Saved thumbnail for source channel <code>{source_id}</code>.")
    except asyncio.TimeoutError:
        await message.reply_text("Timed out waiting for the thumbnail. The mapping is saved; send /set again to attach a thumbnail.")
    except Exception:
        logger.exception("Could not save source-channel thumbnail.")
        await message.reply_text("Could not save the thumbnail. Please check database connectivity and run /set again.")
    finally:
        if not listener_task.done():
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
        _pending_config_users.discard(message.from_user.id)


@Client.on_message(filters.command("setusername") & filters.private)
async def set_username(client, message):
    if not await _require_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.reply_text("Usage: <code>/setusername @username</code>")
    username = parts[1].strip()
    if not re.fullmatch(r"@[\w]{1,64}", username, flags=re.UNICODE):
        return await message.reply_text("Provide a single @username suffix using letters, numbers, or underscores.")
    try:
        await db.set_username(username)
    except Exception:
        logger.exception("Could not persist filename username.")
        return await message.reply_text("Database error: the filename suffix could not be saved.")
    await message.reply_text(f"Filename suffix saved as <code>{username}</code>.")


@Client.on_message(filters.command("status") & filters.private)
async def status(client, message):
    if not await _require_admin(message):
        return
    try:
        mappings = await db.list_channel_configs()
        username = await db.get_username()
        counts = await db.job_counts()
        owner = rename_runtime.session_owner_id
        logged_in = bool(await db.get_session(owner)) and rename_runtime.logged_in if owner else False
    except Exception:
        logger.exception("Could not read bot status from MongoDB.")
        return await message.reply_text("Database error: status information is temporarily unavailable.")
    lines = [
        "<b>VJ Auto Rename Bot status</b>",
        f"User session: <b>{'connected' if logged_in else 'not connected'}</b>",
        f"Filename suffix: <code>{username or '(not set)'}</code>",
        "Channel mappings:",
    ]
    if mappings:
        for item in mappings:
            thumb = "thumbnail saved" if item.get("thumbnail") else "thumbnail missing"
            lines.append(f"<code>{item['source_channel_id']}</code> → <code>{item['output_channel_id']}</code> ({thumb})")
    else:
        lines.append("None configured")
    lines.append("Jobs: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    await message.reply_text("\n".join(lines))


@Client.on_message(filters.command("cancel") & filters.private)
async def cancel(client, message):
    if not await _require_admin(message):
        return
    if message.chat and client.has_pending_conversation(message.chat.id):
        # The in-flight /login or /set listener consumes /cancel and responds.
        return
    active = await rename_runtime.cancel_active()
    await message.reply_text("Cancelled active rename operation(s)." if active else "No active rename operation was running.")
