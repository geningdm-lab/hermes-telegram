"""Polls, reactions and stickers: what the agent sends with them, and what comes back (docs/design.md, Tools,
Events; slice 4).

- A poll the agent sends is non-anonymous (sendpoll.md, ``is_anonymous``), so Telegram reports each answer
  with its user (pollanswer.md). An answer comes back as ``[poll] "<question>": "<option>"[, ...]``. A vote
  that arrives before ``sendPoll`` has returned waits for it.
- A reaction to one of the bot's own messages comes back as ``[reaction] <emoji> on message <id>``. The bot's
  messages are remembered with their topic as they are sent, every part of a long answer and every control
  prompt, in a store that outlives a restart. Telegram documents ``message_reaction`` for chats where the
  bot is an administrator; delivery in a private chat is not verified (live acceptance). A reaction the agent
  chose stays: Hermes' own processing reactions do not replace it.
- The sticker catalog: the stickers users sent (Hermes describes each one once, ``gateway/sticker_cache.py``;
  this plugin adds its ``file_id``), kept per routed profile, and the sticker sets the profile names
  (``extra.sticker_sets``), read with getStickerSet and read again later when Telegram failed.

Actions are accepted in private chats only (groups are out of scope), from a user the adapter admits as it
would admit their text. Each comes back as an ordinary inbound message of its chat and topic, with the
topic's skill and prompt, from the user who acted, through ``handle_message``, with an id of its own. Quoted
fields are JSON-escaped, so a quote in an option cannot read as two options.
"""

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .buttons import StoreError, quoted
from .delivery import DELIVERED_IDS

logger = logging.getLogger(__name__)

_POLL_MEMORY = 500
_STICKER_MEMORY = 500
_BOT_MESSAGE_MEMORY = 4000
_AGENT_REACTION_MEMORY = 500
_SET_RETRY_SECS = 60.0
_POLL_WAIT_SECS = 5.0


def poll_event_text(question: str, options: list[str]) -> str:
    return f"[poll] {quoted(question)}: " + ", ".join(quoted(o) for o in options)


def reaction_event_text(emoji: str, message_id: Any) -> str:
    return f"[reaction] {emoji} on message {message_id}"


class _JsonStore:
    """A bounded mapping persisted as one JSON file under the profile's Hermes home."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path, self.limit = path, limit
        self._data: Optional[dict[str, Any]] = None

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = dict(json.loads(self.path.read_text()))
            except FileNotFoundError:
                self._data = {}
            except Exception:
                logger.warning("[Telegram] Ignoring an unreadable store at %s", self.path, exc_info=True)
                self._data = {}
        return self._data

    def put(self, key: str, value: Any) -> None:
        """Keep ``value``; ``StoreError`` when it could not be saved."""
        from utils import atomic_json_write

        data = self.data
        data.pop(key, None)
        data[key] = value
        while len(data) > self.limit:
            data.pop(next(iter(data)))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(self.path, data)
        except Exception as exc:
            raise StoreError(f"the store at {self.path} could not be saved: {exc}") from exc

    def drop(self, key: str) -> None:
        """Forget ``key``; ``StoreError`` when that could not be saved."""
        from utils import atomic_json_write

        if self.data.pop(key, None) is None:
            return
        try:
            atomic_json_write(self.path, self.data)
        except Exception as exc:
            raise StoreError(f"the store at {self.path} could not be saved: {exc}") from exc


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


def _sent_ids(result: Any) -> list[Any]:
    """Every message id a send reports as delivered, partly delivered sends included."""
    if isinstance(result, (str, int)):  # a message id
        return [str(result)]
    if not hasattr(result, "success"):  # a Telegram Message
        return [getattr(result, "message_id", None)]
    raw = result.raw_response if isinstance(result.raw_response, dict) else {}
    ids = [result.message_id] if result.success else []
    for key in ("message_ids", DELIVERED_IDS, "continuation_message_ids"):
        ids.extend(raw.get(key) or ())
    ids.extend(getattr(result, "continuation_message_ids", None) or ())
    if raw.get("partial_overflow"):
        ids.append(raw.get("last_message_id"))
    return list(dict.fromkeys(ids))


class ActionsMixin:
    """Polls, reactions and stickers; listed before the harvested mixins."""

    def _store(self, name: str, limit: int) -> _JsonStore:
        stores = self.__dict__.setdefault("_hermes_telegram_stores", {})
        if name not in stores:
            stores[name] = _JsonStore(Path(self._update_receipt_dir) / f"telegram_{name}.json", limit)
        return stores[name]

    # --- who may act, and as what ------------------------------------------------------------------------

    def _actor_admitted(self, user: Any, chat_id: str, thread_id: Any) -> bool:
        """The adapter's admission of a text from ``user`` in this private chat (``_is_user_authorized_from_
        message``): the adapter's allowlist when set, else the gateway's full chain, failing closed."""
        from gateway.authz_mixin import _coerce_allow_set

        user_id = str(getattr(user, "id", "") or "")
        if not user_id or getattr(user, "is_bot", False):
            return False
        allow_from = self.config.extra.get("allow_from")
        if allow_from is not None:
            allowed = _coerce_allow_set(allow_from)
            return user_id in allowed or "*" in allowed
        return self._is_callback_user_authorized(user_id, chat_id=chat_id, chat_type="private",
                                                 thread_id=str(thread_id) if thread_id else None,
                                                 user_name=getattr(user, "full_name", None))

    def _action_event(self, chat_id: str, thread_id: Optional[str], user: Any, text: str, event_id: str,
                      anchor: str) -> Any:
        """An action as an inbound text from ``user`` in its private chat and topic, with the topic's skill and
        prompt as a text there gets them; ``event_id`` is its own, and the answer replies to the message acted
        on."""
        from gateway.platforms.base import MessageEvent, MessageType, resolve_channel_prompt

        thread = str(thread_id) if thread_id else None
        chat_topic, topic_skill = self._resolve_topic_binding(SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
                                                              "dm", thread)
        source = self.build_source(chat_id=str(chat_id), chat_type="dm", user_id=str(user.id),
                                   user_name=user.full_name, thread_id=thread, chat_topic=chat_topic,
                                   message_id=event_id, is_bot=bool(getattr(user, "is_bot", False)))
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id=event_id,
                            reply_anchor_override=str(anchor), user_id=str(user.id), user_name=user.full_name,
                            auto_skill=topic_skill,
                            channel_prompt=resolve_channel_prompt(self.config.extra, thread or str(chat_id),
                                                                  str(chat_id) if thread else None))

    # --- the bot's own messages, for reactions ----------------------------------------------------------

    def _bot_identity(self) -> str:
        """This bot's id, the token's prefix: records of another bot in the same chat are not this one's."""
        return str(getattr(self.config, "token", "") or "").split(":", 1)[0]

    def _route_profile(self, chat_id: Any, thread_id: Any) -> str:
        """The profile a private chat's text is routed to (``build_source``: in a private chat the user is the
        chat)."""
        try:
            source = self.build_source(chat_id=str(chat_id), chat_type="dm", user_id=str(chat_id),
                                       thread_id=str(thread_id) if thread_id else None)
        except Exception:
            return ""
        return getattr(source, "profile", None) or ""

    @staticmethod
    def _delivered_thread(requested: Any, sent: Any) -> Optional[str]:
        """The topic a send landed in: the one asked for, unless Hermes' delivery fell back to the root chat
        (a SendResult's ``thread_fallback``, or a returned message without a topic)."""
        if not requested:
            return None
        if hasattr(sent, "success"):
            raw = sent.raw_response if isinstance(sent.raw_response, dict) else {}
            return None if raw.get("thread_fallback") else str(requested)
        messages = sent if isinstance(sent, (list, tuple)) else [sent]
        if any(hasattr(m, "message_thread_id") and m.message_thread_id is None for m in messages):
            return None
        return str(requested)

    def _remember_bot_messages(self, chat_id: Any, results: Any, thread_id: Any = None, *, kind: str = "text",
                               markup: Optional[dict[str, Any]] = None, rich: Optional[bool] = None) -> None:
        """Keep the bot's sent messages with the bot, the routed profile and the topic they landed in, their
        kind (``text``, ``media``, ``prompt``, ``widget``), whether a text went rich, and a tool message's
        keyboard, which an edit keeps (slice 6)."""
        store = self._store("bot_messages", _BOT_MESSAGE_MEMORY)
        profile = self._route_profile(chat_id, thread_id)
        for result in results if isinstance(results, (list, tuple)) else [results]:
            for mid in _sent_ids(result):
                if mid is None:
                    continue
                record: dict[str, Any] = {"thread": str(thread_id) if thread_id else None, "kind": kind,
                                          "bot": self._bot_identity(), "profile": profile}
                if markup:
                    record["markup"] = markup
                if rich is not None:
                    record["rich"] = rich
                try:
                    store.put(f"{chat_id}:{mid}", record)
                except StoreError:
                    logger.warning("[%s] a sent message could not be remembered for reactions", self.name,
                                   exc_info=True)

    def _bot_message(self, chat_id: Any, message_id: Any) -> Optional[dict[str, Any]]:
        """``{"thread": ..., "kind": ...}`` when ``message_id`` is one of the bot's messages in ``chat_id``.
        Records kept before slice 6 have no kind."""
        return self._store("bot_messages", _BOT_MESSAGE_MEMORY).data.get(f"{chat_id}:{message_id}")

    def _forget_bot_message(self, chat_id: Any, message_id: Any) -> None:
        try:
            self._store("bot_messages", _BOT_MESSAGE_MEMORY).drop(f"{chat_id}:{message_id}")
        except StoreError:
            logger.warning("[%s] a deleted message could not be forgotten", self.name, exc_info=True)

    def _is_bot_message(self, chat_id: Any, message_id: Any) -> bool:
        return self._bot_message(chat_id, message_id) is not None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> Any:
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        self._remember_bot_messages(chat_id, result, self._delivered_thread(self._metadata_thread_id(metadata), result))
        return result

    async def _send_with_dm_topic_reply_anchor_retry(self, send_fn: Any, send_kwargs: dict[str, Any],
                                                     metadata: Optional[dict[str, Any]],
                                                     reply_to_message_id: Optional[int], media_label: str,
                                                     reset_media: Optional[Any] = None) -> Any:
        sent = await super()._send_with_dm_topic_reply_anchor_retry(send_fn, send_kwargs, metadata,
                                                                    reply_to_message_id, media_label, reset_media)
        self._remember_bot_messages(send_kwargs.get("chat_id"), sent,
                                    self._delivered_thread(self._metadata_thread_id(metadata), sent), kind="media")
        return sent

    @staticmethod
    def _with_partial_send(result: Any, undelivered: list[str], delivered: list[str], *,
                           tail_certain: bool = True) -> Any:
        """The harvested partial result, which keeps the first delivered id only when the tail is certain,
        with every delivered id under ``DELIVERED_IDS``."""
        from .harvested.adapter_outbound_legacy import OutboundLegacyMixin

        result = OutboundLegacyMixin._with_partial_send(result, undelivered, delivered, tail_certain=tail_certain)
        if delivered and isinstance(result.raw_response, dict):
            result.raw_response[DELIVERED_IDS] = tuple(delivered)
        return result

    async def _send_control_message(self, chat_id: str, text: str, *, thread_id: Optional[str],
                                    **kwargs: Any) -> Any:
        sent = await super()._send_control_message(chat_id, text, thread_id=thread_id, **kwargs)
        self._remember_bot_messages(chat_id, sent, self._delivered_thread(thread_id, sent), kind="prompt")
        return sent

    # --- polls --------------------------------------------------------------------------------------------

    async def send_poll(self, chat_id: str, question: str, options: list[str], metadata: Optional[dict[str, Any]],
                        *, quiz_answer: Optional[int] = None, multiple: bool = False, profile: str = "") -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text

        kwargs: dict[str, Any] = {"question": question, "options": options, "is_anonymous": False}
        if quiz_answer is not None:
            # Bot API 9.x replaced correct_option_id with correct_option_ids; PTB 22.8 knows the old name only.
            kwargs.update(type="quiz", api_kwargs={"correct_option_ids": [quiz_answer]})
        elif multiple:
            kwargs["allows_multiple_answers"] = True
        self.__dict__["_hermes_telegram_polls_in_flight"] = self.__dict__.get("_hermes_telegram_polls_in_flight", 0) + 1
        try:
            msg = await self._send_media(self._bot.send_poll, chat_id, None, metadata, "poll", **kwargs)
        except Exception as exc:
            return self._send_failed(exc, _redact_telegram_error_text(exc))
        finally:
            self.__dict__["_hermes_telegram_polls_in_flight"] -= 1
        poll = getattr(msg, "poll", None)
        if poll is not None:
            try:
                self._store("polls", _POLL_MEMORY).put(str(poll.id), {
                    "chat": str(chat_id), "thread": self._metadata_thread_id(metadata),
                    "message_id": str(msg.message_id), "question": question, "options": options,
                    "profile": profile or ""})
            except StoreError:
                logger.error("[%s] a sent poll could not be recorded: its answers will not reach the agent",
                             self.name, exc_info=True)
        return SendResult(success=True, message_id=str(msg.message_id))

    async def _on_poll_answer(self, update: Any, context: Any) -> None:
        answer = getattr(update, "poll_answer", None)
        if answer is None or answer.user is None:
            return
        self._accept_update()
        polls = self._store("polls", _POLL_MEMORY)
        poll = polls.data.get(str(answer.poll_id))
        deadline = time.monotonic() + _POLL_WAIT_SECS
        while poll is None and self.__dict__.get("_hermes_telegram_polls_in_flight") and time.monotonic() < deadline:
            await asyncio.sleep(0.1)  # the vote came before sendPoll returned: wait for its record
            poll = polls.data.get(str(answer.poll_id))
        if poll is None or not answer.option_ids:  # not this bot's poll, or a vote retracted
            return
        if not self._actor_admitted(answer.user, poll["chat"], poll.get("thread")):
            return
        chosen = [poll["options"][i] for i in answer.option_ids if 0 <= i < len(poll["options"])]
        event = self._action_event(poll["chat"], poll.get("thread"), answer.user,
                                   poll_event_text(poll["question"], chosen), f"pa{update.update_id}",
                                   poll["message_id"])
        if (event.source.profile or "") != (poll.get("profile") or ""):
            return  # a vote routed to another profile than the poll's
        await self.handle_message(event)

    # --- reactions -----------------------------------------------------------------------------------------

    async def react(self, chat_id: str, message_id: str, emoji: str) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.set_message_reaction(chat_id=normalize_telegram_chat_id(chat_id),
                                                 message_id=int(message_id), reaction=emoji)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        chosen = self.__dict__.setdefault("_hermes_telegram_agent_reactions", OrderedDict())
        chosen[(str(chat_id), str(message_id))] = emoji
        while len(chosen) > _AGENT_REACTION_MEMORY:
            chosen.popitem(last=False)
        return SendResult(success=True, message_id=str(message_id))

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: Optional[str]) -> bool:
        """Hermes' processing reactions (``extra.reactions``: in progress, done, failed, cleared on a cancel)
        leave alone a message the agent reacted to: its reaction is the one that stays."""
        if (str(chat_id), str(message_id)) in self.__dict__.get("_hermes_telegram_agent_reactions", {}):
            return True
        return await super()._set_reaction(chat_id, message_id, emoji)

    async def _on_message_reaction(self, update: Any, context: Any) -> None:
        changed = getattr(update, "message_reaction", None)
        if changed is None or changed.user is None or str(getattr(changed.chat, "type", "")) != "private":
            return
        chat_id, message_id = str(changed.chat.id), str(changed.message_id)
        mine = self._bot_message(chat_id, message_id)
        if mine is None:
            return  # the agent hears of reactions to its own messages only
        old = {getattr(r, "emoji", None) for r in changed.old_reaction or ()}
        added = [r.emoji for r in changed.new_reaction or () if getattr(r, "emoji", None) and r.emoji not in old]
        if not added or not self._actor_admitted(changed.user, chat_id, mine.get("thread")):
            return
        self._accept_update()
        await self.handle_message(self._action_event(
            chat_id, mine.get("thread"), changed.user, reaction_event_text(" ".join(added), message_id),
            f"re{update.update_id}", message_id))

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
        profile = getattr(getattr(event, "source", None), "profile", None) or ""
        try:
            self._store("stickers", _STICKER_MEMORY).put(f"{profile}:{sticker.file_unique_id}", {
                "sticker_id": sticker.file_id, "emoji": sticker.emoji or "", "description": description,
                "set": sticker.set_name or "", "profile": profile})
        except StoreError:
            logger.warning("[%s] a received sticker could not be recorded", self.name, exc_info=True)

    async def sticker_catalog(self, profile: str = "") -> list[dict[str, Any]]:
        """The stickers users of ``profile`` sent, then those of the configured sticker sets. A set Telegram
        failed to return is asked for again after ``_SET_RETRY_SECS``."""
        own = [s for s in self._store("stickers", _STICKER_MEMORY).data.values()
               if (s.get("profile") or "") == (profile or "")]
        cache: dict[str, Any] = self.__dict__.setdefault("_hermes_telegram_sticker_sets", {})
        names = (self.config.extra or {}).get("sticker_sets") or []
        now = time.monotonic()
        for name in names if isinstance(names, list) else []:
            entry = cache.get(str(name))
            if entry is not None and (entry["stickers"] is not None or now - entry["failed_at"] < _SET_RETRY_SECS):
                continue
            try:
                found = await self._bot.get_sticker_set(name=str(name))
                cache[str(name)] = {"stickers": [
                    {"sticker_id": s.file_id, "emoji": s.emoji or "", "description": "", "set": found.name,
                     "set_title": found.title} for s in found.stickers], "failed_at": 0.0}
            except Exception:
                logger.warning("[%s] sticker set %s could not be read; trying again later", self.name, name,
                               exc_info=True)
                cache[str(name)] = {"stickers": None, "failed_at": now}
        return own + [s for entry in cache.values() for s in entry["stickers"] or ()]

    async def send_sticker(self, chat_id: str, sticker_id: str, metadata: Optional[dict[str, Any]],
                           profile: str = "") -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text

        if sticker_id not in {s["sticker_id"] for s in await self.sticker_catalog(profile)}:
            return SendResult(success=False, error="not a sticker of the catalog: find one first")
        try:
            msg = await self._send_media(self._bot.send_sticker, chat_id, None, metadata, "sticker", sticker=sticker_id)
        except Exception as exc:
            return self._send_failed(exc, _redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(msg.message_id))

    # --- handlers ------------------------------------------------------------------------------------------

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram.ext import MessageReactionHandler, PollAnswerHandler

        app.add_handler(PollAnswerHandler(self._on_poll_answer), group=-9)
        app.add_handler(MessageReactionHandler(self._on_message_reaction), group=-8)
