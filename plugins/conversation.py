"""Minimal Pyrogram-based private-chat ask/listen support for existing dialogs."""

import asyncio
from dataclasses import dataclass

from pyrogram import filters
from pyrogram.handlers import MessageHandler


@dataclass
class _Listener:
    future: asyncio.Future
    filters: object | None
    user_id: int | None


class ConversationMixin:
    """Add one-message private conversations to a regular Pyrogram Client."""

    def initialize_conversations(self):
        self._conversation_listeners = {}
        self.add_handler(MessageHandler(self._dispatch_conversation, filters.private), group=-100)

    async def _dispatch_conversation(self, _client, message):
        if not message.chat or not message.from_user:
            return
        listener = self._conversation_listeners.get(message.chat.id)
        if not listener or listener.future.done():
            return
        if listener.user_id is not None and listener.user_id != message.from_user.id:
            return
        if listener.filters is not None:
            try:
                matched = await listener.filters(self, message)
            except Exception:
                matched = False
            if not matched:
                return
        listener.future.set_result(message)

    async def _wait_for_message(self, chat_id, filters=None, timeout=600, user_id=None):
        chat_id = int(chat_id) if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit() else chat_id
        if chat_id in self._conversation_listeners:
            raise RuntimeError("A conversation is already waiting for a reply in this chat.")
        loop = asyncio.get_running_loop()
        listener = _Listener(loop.create_future(), filters, user_id)
        self._conversation_listeners[chat_id] = listener
        try:
            if timeout is None:
                return await listener.future
            return await asyncio.wait_for(listener.future, timeout=timeout)
        finally:
            if self._conversation_listeners.get(chat_id) is listener:
                self._conversation_listeners.pop(chat_id, None)

    def has_pending_conversation(self, chat_id) -> bool:
        chat_id = int(chat_id) if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit() else chat_id
        listener = self._conversation_listeners.get(chat_id)
        return bool(listener and not listener.future.done())

    async def listen(self, chat_id, filters=None, timeout=600, user_id=None):
        return await self._wait_for_message(chat_id, filters=filters, timeout=timeout, user_id=user_id)

    async def ask(self, chat_id, text, filters=None, timeout=600, user_id=None, **send_kwargs):
        chat_id_key = int(chat_id) if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit() else chat_id
        if chat_id_key in self._conversation_listeners:
            raise RuntimeError("A conversation is already waiting for a reply in this chat.")
        loop = asyncio.get_running_loop()
        listener = _Listener(loop.create_future(), filters, user_id)
        self._conversation_listeners[chat_id_key] = listener
        try:
            await self.send_message(chat_id, text, **send_kwargs)
            if timeout is None:
                return await listener.future
            return await asyncio.wait_for(listener.future, timeout=timeout)
        finally:
            if self._conversation_listeners.get(chat_id_key) is listener:
                self._conversation_listeners.pop(chat_id_key, None)
