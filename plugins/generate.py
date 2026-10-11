"""Preserved interactive Telegram user-account login and logout commands."""

import logging

from pyrogram import Client, filters
from pyrogram.errors import (
    ApiIdInvalid,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)
from pyrogram.types import Message

from config import API_HASH, API_ID
from plugins.database import db
from plugins.rename_runtime import rename_runtime

logger = logging.getLogger(__name__)
SESSION_STRING_SIZE = 351


@Client.on_message(filters.private & ~filters.forwarded & filters.command(["logout"]))
async def logout(client, message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    user_data = await db.get_session(user_id)
    if user_data is None:
        return await message.reply_text("No saved Telegram user-account session was found.")
    if user_id == rename_runtime.session_owner_id:
        await rename_runtime.stop_user_session()
    await db.set_session(user_id, session=None)
    await message.reply_text("Logged out successfully.")


@Client.on_message(filters.private & ~filters.forwarded & filters.command(["login"]))
async def login_user(bot: Client, message: Message):
    if not message.from_user:
        return
    user_id = int(message.from_user.id)
    if await db.get_session(user_id):
        return await message.reply_text("You are already logged in. Use /logout before creating a new session.")

    phone_message = await bot.ask(
        chat_id=user_id,
        text="<b>Please send your phone number including the country code.</b>\nExample: <code>+13124562345</code>",
    )
    if not phone_message.text or phone_message.text.strip().lower() == "/cancel":
        return await phone_message.reply_text("Login cancelled.")
    phone_number = phone_message.text.strip()
    user_client = Client(":memory:", api_id=API_ID, api_hash=API_HASH)
    connected = False
    try:
        await user_client.connect()
        connected = True
        await phone_message.reply_text("Sending Telegram login code…")
        sent_code = await user_client.send_code(phone_number)
        code_message = await bot.ask(
            user_id,
            "Check Telegram for the login code and enter it with digits separated by spaces (for example, <code>1 2 3 4 5</code>). Send /cancel to stop.",
            filters=filters.text,
            timeout=600,
        )
        if not code_message.text or code_message.text.strip().lower() == "/cancel":
            return await code_message.reply_text("Login cancelled.")
        code = code_message.text.replace(" ", "").strip()
        try:
            await user_client.sign_in(phone_number, sent_code.phone_code_hash, code)
        except SessionPasswordNeeded:
            password_message = await bot.ask(
                user_id,
                "Two-step verification is enabled. Send the password, or /cancel to stop.",
                filters=filters.text,
                timeout=300,
            )
            if not password_message.text or password_message.text.strip().lower() == "/cancel":
                return await password_message.reply_text("Login cancelled.")
            await user_client.check_password(password=password_message.text)

        session_string = await user_client.export_session_string()
        if len(session_string) < SESSION_STRING_SIZE:
            return await message.reply_text("Telegram returned an invalid session string; no session was saved.")
        await db.add_user(user_id, message.from_user.first_name or "Telegram user")
        await db.set_session(user_id, session=session_string)
    except PhoneNumberInvalid:
        return await phone_message.reply_text("The phone number is invalid.")
    except PhoneCodeInvalid:
        return await message.reply_text("The login code is invalid.")
    except PhoneCodeExpired:
        return await message.reply_text("The login code has expired. Run /login again.")
    except PasswordHashInvalid:
        return await message.reply_text("The two-step verification password is incorrect.")
    except ApiIdInvalid:
        logger.exception("Telegram API credentials were rejected during user login.")
        return await message.reply_text("Telegram rejected API_ID/API_HASH. Check your environment configuration.")
    except Exception as exc:
        logger.exception("User-account login failed.")
        return await message.reply_text(f"Login failed: <code>{str(exc)[:300]}</code>")
    finally:
        if connected:
            try:
                await user_client.disconnect()
            except Exception:
                logger.debug("Temporary login client disconnect failed.", exc_info=True)

    await bot.send_message(user_id, "<b>Telegram account login succeeded. Your session has been saved and will be restored after restarts.</b>")
    if user_id == rename_runtime.session_owner_id:
        await rename_runtime.start_user_session(session_string, owner_id=user_id)
