"""Polls, reactions and stickers: what the agent sends with them, and what comes back (docs/design.md, Tools,
Events; slice 4).

- A poll the agent sends is non-anonymous (sendpoll.md, ``is_anonymous``), so Telegram reports each answer
  with its user (pollanswer.md). An answer comes back as ``[poll] "<question>": "<option>"[, ...]``.
- A reaction to one of the bot's own messages comes back as ``[reaction] <emoji> on message <id>``; the
  bot's messages are remembered as they are sent. Telegram documents ``message_reaction`` for chats where
  the bot is an administrator; delivery in a private chat is not verified (live acceptance).
- The sticker catalog: the stickers users sent (Hermes describes each one once, ``gateway/sticker_cache.py``;
  this plugin adds its ``file_id``), and the sticker sets the profile names (``extra.sticker_sets``), read
  with getStickerSet. ``find`` searches emoji, descriptions and set names; ``send`` sends one of them.

Each comes back as an ordinary inbound message of the same chat from the user who acted, through
``handle_message`` (authorized and routed as a text from that user), with an id of its own.
"""

import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_POLL_MEMORY = 500
_STICKER_MEMORY = 500
_BOT_MESSAGE_MEMORY = 2000


def poll_event_text(question: str, options: list[str]) -> str:
    return f'[poll] "{question}": ' + ", ".join(f'"{o}"' for o in options)


def reaction_event_text(emoji: str, message_id: Any) -> str:
    return f"[reaction] {emoji} on message {message_id}"


class _JsonStore:
    """A bounded ``OrderedDict`` persisted as one JSON file under the profile's Hermes home."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path, self.limit = path, limit
        self._data: Optional[OrderedDict[str, Any]] = None

    @property
    def data(self) -> "OrderedDict[str, Any]":
        if self._data is None:
            try:
                self._data = OrderedDict(json.loads(self.path.read_text()))
            except FileNotFoundError:
                self._data = OrderedDict()
            except Exception:
                logger.warning("[Telegram] Ignoring an unreadable store at %s", self.path, exc_info=True)
                self._data = OrderedDict()
        return self._data

    def put(self, key: str, value: Any) -> None:
        from utils import atomic_json_write

        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.limit:
            self.data.popitem(last=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(self.path, dict(self.data))
        except Exception:
            logger.warning("[Telegram] Could not save the store at %s", self.path, exc_info=True)


def sticker_matches(catalog: list[dict[str, Any]], query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Stickers of ``catalog`` best matching ``query``: an emoji in it, then words of the description and
    the set's name and title."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
    scored = []
    for sticker in catalog:
        emoji = sticker.get("emoji") or ""
        text = " ".join(str(sticker.get(k) or "") for k in ("description", "set", "set_title")).lower()
        score = (3 if emoji and emoji in query else 0) + sum(1 for w in words if w in text)
        if score:
            scored.append((score, sticker))
    scored.sort(key=lambda item: -item[0])
    return [s for _, s in scored[:limit]]


class ActionsMixin:
    """Polls, reactions and stickers; listed before the harvested mixins."""

    def _store(self, name: str, limit: int) -> _JsonStore:
        stores = self.__dict__.setdefault("_hermes_telegram_stores", {})
        if name not in stores:
            stores[name] = _JsonStore(Path(self._update_receipt_dir) / f"telegram_{name}.json", limit)
        return stores[name]

    # --- the bot's own messages, for reactions ----------------------------------------------------------

    def _remember_bot_messages(self, chat_id: Any, result: Any) -> None:
        ids: list[Any] = []
        for item in result if isinstance(result, (list, tuple)) else [result]:
            if hasattr(item, "success"):  # a SendResult: its first id, and every part's for a long answer
                if item.success:
                    raw = item.raw_response if isinstance(item.raw_response, dict) else {}
                    ids.extend([item.message_id, *(raw.get("message_ids") or ())])
            else:  # a Telegram Message
                ids.append(getattr(item, "message_id", None))
        mine = self.__dict__.setdefault("_hermes_telegram_bot_messages", OrderedDict())
        for mid in ids:
            if mid is not None:
                mine[(str(chat_id), str(mid))] = None
        while len(mine) > _BOT_MESSAGE_MEMORY:
            mine.popitem(last=False)

    def _is_bot_message(self, chat_id: Any, message_id: Any) -> bool:
        return (str(chat_id), str(message_id)) in self.__dict__.get("_hermes_telegram_bot_messages", {})

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> Any:
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        self._remember_bot_messages(chat_id, result)
        return result

    async def _send_with_dm_topic_reply_anchor_retry(self, send_fn: Any, kwargs: dict[str, Any], *args: Any,
                                                     **more: Any) -> Any:
        sent = await super()._send_with_dm_topic_reply_anchor_retry(send_fn, kwargs, *args, **more)
        if kwargs.get("chat_id") is not None:
            self._remember_bot_messages(kwargs["chat_id"], sent)
        return sent

    # --- polls --------------------------------------------------------------------------------------------

    async def send_poll(self, chat_id: str, question: str, options: list[str], metadata: Optional[dict[str, Any]],
                        *, quiz_answer: Optional[int] = None, multiple: bool = False) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text

        kwargs: dict[str, Any] = {"question": question, "options": options, "is_anonymous": False}
        if quiz_answer is not None:
            # Bot API 9.x replaced correct_option_id with correct_option_ids; PTB 22.8 knows the old name only.
            kwargs.update(type="quiz", api_kwargs={"correct_option_ids": [quiz_answer]})
        elif multiple:
            kwargs["allows_multiple_answers"] = True
        try:
            msg = await self._send_media(self._bot.send_poll, chat_id, None, metadata, "poll", **kwargs)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        poll = getattr(msg, "poll", None)
        if poll is not None:
            self._store("polls", _POLL_MEMORY).put(str(poll.id), {
                "chat": str(chat_id), "thread": self._metadata_thread_id(metadata), "message_id": str(msg.message_id),
                "question": question, "options": options})
        return SendResult(success=True, message_id=str(msg.message_id))

    async def _on_poll_answer(self, update: Any, context: Any) -> None:
        answer = getattr(update, "poll_answer", None)
        if answer is None or answer.user is None:
            return
        self._accept_update()
        poll = self._store("polls", _POLL_MEMORY).data.get(str(answer.poll_id))
        if poll is None or not answer.option_ids:  # not this bot's poll, or a vote retracted
            return
        chosen = [poll["options"][i] for i in answer.option_ids if 0 <= i < len(poll["options"])]
        await self.handle_message(self._action_event(
            poll["chat"], poll.get("thread"), answer.user, poll_event_text(poll["question"], chosen),
            f"pa{update.update_id}", poll["message_id"]))

    # --- reactions -----------------------------------------------------------------------------------------

    async def react(self, chat_id: str, message_id: str, emoji: str) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.set_message_reaction(chat_id=normalize_telegram_chat_id(chat_id),
                                                 message_id=int(message_id), reaction=emoji)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(message_id))

    async def _on_message_reaction(self, update: Any, context: Any) -> None:
        changed = getattr(update, "message_reaction", None)
        if changed is None or changed.user is None:
            return
        chat_id, message_id = str(changed.chat.id), str(changed.message_id)
        if not self._is_bot_message(chat_id, message_id):
            return  # the agent hears of reactions to its own messages only
        old = {getattr(r, "emoji", None) for r in changed.old_reaction or ()}
        added = [r.emoji for r in changed.new_reaction or () if getattr(r, "emoji", None) and r.emoji not in old]
        if not added:
            return
        self._accept_update()
        await self.handle_message(self._action_event(
            chat_id, None, changed.user, reaction_event_text(" ".join(added), message_id), f"re{update.update_id}",
            message_id))

    # --- stickers ------------------------------------------------------------------------------------------

    async def _handle_sticker(self, msg: Any, event: Any) -> None:
        await super()._handle_sticker(msg, event)
        sticker = msg.sticker
        description = ""
        try:
            from gateway.sticker_cache import get_cached_description
            description = (get_cached_description(sticker.file_unique_id) or {}).get("description", "")
        except Exception:
            pass
        self._store("stickers", _STICKER_MEMORY).put(sticker.file_unique_id, {
            "sticker_id": sticker.file_id, "emoji": sticker.emoji or "", "description": description,
            "set": sticker.set_name or ""})

    async def sticker_catalog(self) -> list[dict[str, Any]]:
        """The stickers users sent, then those of the profile's sticker sets (read once per process)."""
        catalog = list(self._store("stickers", _STICKER_MEMORY).data.values())
        sets = self.__dict__.get("_hermes_telegram_sticker_sets")
        if sets is None:
            sets = []
            names = (self.config.extra or {}).get("sticker_sets") or []
            for name in names if isinstance(names, list) else []:
                try:
                    found = await self._bot.get_sticker_set(name=str(name))
                except Exception:
                    logger.warning("[%s] sticker set %s could not be read", self.name, name, exc_info=True)
                    continue
                sets.extend({"sticker_id": s.file_id, "emoji": s.emoji or "", "description": "", "set": found.name,
                             "set_title": found.title} for s in found.stickers)
            self.__dict__["_hermes_telegram_sticker_sets"] = sets
        return catalog + sets

    async def send_sticker(self, chat_id: str, sticker_id: str, metadata: Optional[dict[str, Any]]) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text

        if sticker_id not in {s["sticker_id"] for s in await self.sticker_catalog()}:
            return SendResult(success=False, error="not a sticker of the catalog: find one first")
        try:
            msg = await self._send_media(self._bot.send_sticker, chat_id, None, metadata, "sticker", sticker=sticker_id)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(msg.message_id))

    # --- handlers and events -------------------------------------------------------------------------------

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram.ext import MessageReactionHandler, PollAnswerHandler

        app.add_handler(PollAnswerHandler(self._on_poll_answer), group=-9)
        app.add_handler(MessageReactionHandler(self._on_message_reaction), group=-8)

    def _action_event(self, chat_id: str, thread_id: Optional[str], user: Any, text: str, event_id: str,
                      anchor: str) -> Any:
        """An action as an inbound text from ``user`` in the chat it happened in; ``event_id`` is its own,
        and the answer replies to the message acted on."""
        from gateway.platforms.base import MessageEvent, MessageType

        source = self.build_source(chat_id=str(chat_id), chat_type="dm" if int(chat_id) > 0 else "group",
                                   user_id=str(user.id), user_name=user.full_name, thread_id=thread_id,
                                   message_id=event_id, is_bot=bool(getattr(user, "is_bot", False)))
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id=event_id,
                            reply_anchor_override=str(anchor), user_id=str(user.id), user_name=user.full_name)
