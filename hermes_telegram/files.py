"""Files the agent sends: delivered as their kind, or named with the reason they were not (slice 2).

Rows 3, 4, 5 and 15 of docs/design.md. The harvested senders already deliver each kind natively. This
mixin adds what the rows need on top of them:

- A document is a document: ``disable_content_type_detection`` keeps Telegram from turning a video or a
  GIF sent "as a file" into a playable message (core.telegram.org/bots/api/methods/senddocument.md).
- A file over Telegram's upload limit for bots is not uploaded: the chat gets a notice naming it and its
  size against the limit (50 MB; 2000 MB with a local Bot API server, ``local_mode``; api.md, "Sending
  files"). A photo is measured as it is uploaded, after the harvested sender's JPEG compression.
- A failed upload's notice names the file and Telegram's reason for that file; the bundled notice named
  neither. A file gone between the check and the upload is named too.
- Photos go as albums of 2 to 10 in order (sendMediaGroup takes 2-10 items), with each photo's message id.
  An album Telegram refused is sent photo by photo; an album whose reply was lost is not, because Telegram
  may show it already: the chat is told it may not have arrived.
- A ``MEDIA:`` path Hermes would drop, after its own resolution (Docker translation, a remote sandbox
  fetch), is named too. Hermes only logs those (``_validated_delivery_path``); the chat saw nothing. The
  path is swapped for a small notice file that Hermes does deliver, to ``send_document``, which then posts
  the notice instead of uploading.
- A notice is remembered as given only once it is sent; one refused by a flood wait is sent after it.
"""

import asyncio
import contextlib
import contextvars
import logging
import os
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import unquote

from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_document_from_bytes

from .delivery import NOTICES as _NOTICES
from .delivery import Receipt, album_chunks
from .harvested.adapter_module import _redact_telegram_error_text

logger = logging.getLogger(__name__)

MB = 1024 * 1024
UPLOAD_LIMIT = 50 * MB  # multipart upload limit of the Bot API for bots
LOCAL_SERVER_UPLOAD_LIMIT = 2000 * MB
_NOTICE_MEMORY = 512
GONE = "the file was no longer there when it was to be uploaded"
LOST_REPLY = "Telegram's reply was lost after it was sent, so it may or may not be in the chat"

# The send under way in this task, so a failure notice names that file's own error.
_UPLOAD: contextvars.ContextVar[Optional[dict[str, str]]] = contextvars.ContextVar(
    "hermes_telegram_upload", default=None)
# The session key Hermes resolves this response's MEDIA paths with ("" on the streamed path, as Hermes').
_MEDIA_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("hermes_telegram_media_session", default="")


def _size_text(size: int) -> str:
    return f"{size / MB:.1f} MB"


@contextlib.contextmanager
def _upload_scope() -> Iterator[None]:
    token = _UPLOAD.set({})
    try:
        yield
    finally:
        _UPLOAD.reset(token)


class FilesMixin:
    """File delivery overrides; listed before the harvested mixins."""

    # --- limits and notices ------------------------------------------------------------------------------

    def _upload_limit(self) -> int:
        return LOCAL_SERVER_UPLOAD_LIMIT if self.config.extra.get("local_mode") else UPLOAD_LIMIT

    def _notified(self) -> "OrderedDict[str, None]":
        return self.__dict__.setdefault("_hermes_telegram_notified_paths", OrderedDict())

    def _remember_notified(self, paths: Any) -> None:
        notified = self._notified()
        for path in paths:
            notified[path] = None
            notified.move_to_end(path)
        while len(notified) > _NOTICE_MEMORY:
            notified.popitem(last=False)

    def _placeholders(self) -> "OrderedDict[str, tuple[str, str]]":
        return self.__dict__.setdefault("_hermes_telegram_undelivered", OrderedDict())

    @staticmethod
    def _not_delivered_text(name: str, reason: str) -> str:
        return f"{name} was not delivered: {reason}."

    async def _tell(self, chat_id: str, paths: list[str], text: str, metadata: Optional[dict[str, Any]]) -> None:
        """Tell the chat about files that did not arrive: now, or collected for one message (``_NOTICES``)."""
        logger.warning("[%s] %s", self.name, text)
        collected = _NOTICES.get()
        if collected is not None:
            collected.append(text)
            self._remember_notified(paths)
            return
        await self.send_notice(chat_id, text, metadata, paths=paths)

    async def send_notice(self, chat_id: str, text: str, metadata: Optional[dict[str, Any]], *,
                          paths: Any = ()) -> bool:
        """Send a notice. True once it is sent, or set to be sent after the flood wait that refused it.

        Not ``emit_warning``: that is a diagnostic a profile can switch off, and row 15 says a file that did
        not arrive is never silence."""
        try:
            result = await self.send(chat_id, text, metadata=metadata)
        except Exception:
            logger.warning("[%s] could not send a notice", self.name, exc_info=True)
            return False
        if result.success:
            self._remember_notified(paths)
            return True
        wait = self._send_flood_cooldown_remaining(chat_id) or result.retry_after
        if not wait:
            return False  # a definite refusal: Hermes' own failure notice may still reach the chat
        self._remember_notified(paths)
        task = asyncio.get_running_loop().create_task(self._notice_after(float(wait), chat_id, text, metadata))
        tasks = self.__dict__.setdefault("_hermes_telegram_notice_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _notice_after(self, wait: float, chat_id: str, text: str, metadata: Optional[dict[str, Any]]) -> None:
        await asyncio.sleep(wait + 1)
        try:
            result = await self.send(chat_id, text, metadata=metadata)
        except Exception:
            result = None
        if result is None or not result.success:
            logger.warning("[%s] notice still not delivered after the flood wait: %s", self.name, text)

    async def _not_delivered(self, chat_id: str, path: str, reason: str, metadata: Optional[dict[str, Any]],
                             *, name: Optional[str] = None) -> SendResult:
        """Tell the chat that a file did not arrive and why; the result is the failure."""
        text = self._not_delivered_text(name or os.path.basename(path) or path, reason)
        await self._tell(chat_id, [path], text, metadata)
        return SendResult(success=False, error=text, retryable=False)

    def _too_large(self, path: str) -> Optional[str]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None  # missing files are the senders' to report
        limit = self._upload_limit()
        if size <= limit:
            return None
        return f"it is {_size_text(size)}, over Telegram's {limit // MB} MB upload limit for bots"

    async def _notify_media_delivery_failure(self, chat_id: str, media_path: str, *, is_voice: bool = False,
                                             metadata: Optional[dict[str, Any]] = None) -> None:
        # Hermes calls this after a failed send; the notice with the reason has gone already.
        if media_path in self._notified():
            return
        await super()._notify_media_delivery_failure(chat_id, media_path, is_voice=is_voice, metadata=metadata)

    async def _send_media_fallback_notice(
            self, method: str, kind: str, path: str, chat_id: str, caption: Optional[str], reply_to: Optional[str],
            metadata: Optional[dict[str, Any]], *, file_name: Optional[str] = None) -> SendResult:
        """The harvested senders' last resort after a failed upload: say which file and why."""
        error = (_UPLOAD.get() or {}).get("error", "")
        reason = f"Telegram refused the upload ({error})" if error else "the upload to Telegram failed"
        return await self._not_delivered(chat_id, path, reason, metadata, name=file_name)

    async def _send_media(self, send_fn: Any, chat_id: str, reply_to: Optional[str],
                          metadata: Optional[dict[str, Any]], media_label: str, reset_media: Optional[Any] = None,
                          **media_kwargs: Any) -> Any:
        """Every native upload passes here: its error is kept for the notice of the send under way."""
        try:
            return await super()._send_media(send_fn, chat_id, reply_to, metadata, media_label,
                                             reset_media=reset_media, **media_kwargs)
        except Exception as exc:
            upload = _UPLOAD.get()
            if upload is not None:
                upload["error"] = _redact_telegram_error_text(exc)
            raise

    async def _send_local_file(self, label: str, path: str, chat_id: Any, reply_to: Any, metadata: Any,
                               media_key: str, build_kwargs: Any, on_error: Any) -> SendResult:
        result = await super()._send_local_file(label, path, chat_id, reply_to, metadata, media_key, build_kwargs,
                                                on_error)
        if not result.success and not os.path.exists(path) and path not in self._notified():
            # The harvested shell returns this without calling on_error: nobody would name the file.
            return await self._not_delivered(chat_id, path, GONE, metadata)
        return result

    # --- senders -----------------------------------------------------------------------------------------

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        placeholder = self._placeholders().pop(os.path.realpath(file_path), None)
        if placeholder is not None:
            name, reason = placeholder
            with contextlib.suppress(OSError):
                os.remove(file_path)
            return await self._not_delivered(chat_id, file_path, reason, metadata, name=name)
        too_large = self._too_large(file_path)
        if too_large:
            return await self._not_delivered(chat_id, file_path, too_large, metadata, name=file_name)
        with _upload_scope():
            return await self._send_local_file(
                "File", file_path, chat_id, reply_to, metadata, "document",
                lambda f: {"document": f, "filename": file_name or os.path.basename(file_path),
                           "caption": self._caption_1024(caption), "disable_content_type_detection": True},
                lambda e: self._send_media_fallback_notice(
                    "send_document", "file", file_path, chat_id, caption, reply_to, metadata,
                    file_name=file_name or os.path.basename(file_path)))

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        too_large = self._too_large(video_path)
        if too_large:
            return await self._not_delivered(chat_id, video_path, too_large, metadata)
        with _upload_scope():
            return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        too_large = self._too_large(audio_path)
        if too_large:
            return await self._not_delivered(chat_id, audio_path, too_large, metadata)
        with _upload_scope():
            return await super().send_voice(chat_id, audio_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        # No size check here: the harvested sender compresses a large image first, and a photo it cannot
        # send goes as a document, which checks the size of what it actually uploads.
        with _upload_scope():
            return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_multiple_images(self, chat_id: str, images: list, metadata: Optional[dict[str, Any]] = None,
                                   human_delay: float = 0.0) -> SendResult:
        """Hermes' album send: local photos through ``send_photos``; URLs and animations as harvested."""
        local: list[tuple[str, Optional[str]]] = []
        other: list[Any] = []
        for image in images:
            url, alt = (image[0], image[1] if len(image) > 1 else None) if isinstance(image, (list, tuple)) \
                else (image, None)
            if str(url).startswith("file://"):
                local.append((unquote(str(url)[len("file://"):]), alt or None))
            else:
                other.append(image)
        delivered = False
        if other:
            result = await super().send_multiple_images(chat_id, other, metadata, human_delay)
            delivered = bool(getattr(result, "success", False))
        if local:
            receipts = await self.send_photos(chat_id, local, metadata, human_delay=human_delay)
            delivered = delivered or any(r.sent for r in receipts)
        return SendResult(success=delivered, error=None if delivered else "all images failed to send")

    async def send_photos(self, chat_id: str, photos: list[tuple[str, Optional[str]]],
                          metadata: Optional[dict[str, Any]], *, human_delay: float = 0.0,
                          charge: Optional[Callable[[], bool]] = None) -> list[Receipt]:
        """Local photos ``(path, caption)`` in order, as albums (``album_chunks``); a receipt per photo.

        ``charge()`` is asked before each extra message a refused album costs (a tool call's send budget);
        a photo it refuses is reported instead of sent."""
        receipts: list[Receipt] = []
        for n, chunk in enumerate(album_chunks(photos)):
            if n and human_delay > 0:
                await asyncio.sleep(human_delay)
            if len(chunk) == 1:
                path, caption = chunk[0]
                receipts.append(_receipt(path, await self.send_image_file(chat_id, path, caption, metadata=metadata)))
            else:
                receipts.extend(await self._send_album(chat_id, chunk, metadata, charge))
        return receipts

    async def _send_album(self, chat_id: str, chunk: list[tuple[str, Optional[str]]],
                          metadata: Optional[dict[str, Any]], charge: Optional[Callable[[], bool]]) -> list[Receipt]:
        from telegram import InputMediaPhoto

        receipts: dict[int, Receipt] = {}
        prepared: list[tuple[int, str, Optional[str], str]] = []  # (index, path, caption, file uploaded)
        temps: list[str] = []
        handles: list[Any] = []
        try:
            for i, (path, caption) in enumerate(chunk):
                if not os.path.isfile(path):
                    receipts[i] = _receipt(path, await self._not_delivered(chat_id, path, GONE, metadata))
                    continue
                upload = self._compress_image_to_jpeg(path) or path
                if upload != path:
                    temps.append(upload)
                too_large = self._too_large(upload)
                if too_large:
                    receipts[i] = _receipt(path, await self._not_delivered(chat_id, path, too_large, metadata))
                    continue
                prepared.append((i, path, caption, upload))
            if len(prepared) == 1:
                i, path, caption, _ = prepared[0]
                receipts[i] = _receipt(path, await self.send_image_file(chat_id, path, caption, metadata=metadata))
            elif prepared:
                media = []
                for _, _, caption, upload in prepared:
                    handle = open(upload, "rb")  # noqa: SIM115 -- closed in finally, after the upload
                    handles.append(handle)
                    media.append(InputMediaPhoto(media=handle, caption=self._caption_1024(caption)))
                reply_to_id, kwargs = self._media_send_kwargs(chat_id, None, metadata)

                def rewind() -> None:
                    for handle in handles:
                        handle.seek(0)

                try:
                    with _upload_scope():
                        messages = await self._send_with_dm_topic_reply_anchor_retry(
                            self._bot.send_media_group, {**kwargs, "media": media}, metadata, reply_to_id,
                            "media group", reset_media=rewind)
                except Exception as exc:
                    receipts.update(await self._album_failed(chat_id, prepared, exc, metadata, charge))
                else:
                    ids = [str(m.message_id) for m in messages or ()]
                    for n, (i, path, _, _) in enumerate(prepared):
                        receipts[i] = Receipt(path, message_id=ids[n] if n < len(ids) else None)
        finally:
            for handle in handles:
                with contextlib.suppress(Exception):
                    handle.close()
            for temp in temps:
                with contextlib.suppress(OSError):
                    os.remove(temp)
        return [receipts[i] for i in sorted(receipts)]

    async def _album_failed(self, chat_id: str, prepared: list[tuple[int, str, Optional[str], str]],
                            exc: Exception, metadata: Optional[dict[str, Any]],
                            charge: Optional[Callable[[], bool]]) -> dict[int, Receipt]:
        """An album that failed: sent photo by photo only when Telegram certainly did not show it."""
        paths = [path for _, path, _, _ in prepared]
        names = ", ".join(os.path.basename(p) for p in paths)
        wait = getattr(exc, "retry_after", None)
        if isinstance(wait, timedelta):
            wait = wait.total_seconds()
        if wait is not None:
            self._record_send_flood_cooldown(chat_id, float(wait))
            reason = "Telegram's rate limit refused it"
        elif self._is_bad_request_error(exc) or self._certainly_unsent(exc):
            out: dict[int, Receipt] = {}
            for n, (i, path, caption, _) in enumerate(prepared):
                if n and charge is not None and not charge():
                    out[i] = _receipt(path, await self._not_delivered(
                        chat_id, path, "the turn's limit of extra messages was reached", metadata))
                    continue
                out[i] = _receipt(path, await self.send_image_file(chat_id, path, caption, metadata=metadata))
            return out
        else:
            reason = LOST_REPLY
        logger.warning("[%s] album of %d not delivered: %s", self.name, len(paths), _redact_telegram_error_text(exc))
        await self._tell(chat_id, paths, f"The album of {len(paths)} photos ({names}) did not arrive: {reason}.",
                         metadata)
        return {i: Receipt(path, reason=reason) for i, path, _, _ in prepared}

    # --- MEDIA paths Hermes would drop -------------------------------------------------------------------

    async def _extract_response_content(self, response: str, event: Any, session_key: str, **kwargs: Any) -> Any:
        token = _MEDIA_SESSION.set(session_key or "")
        try:
            return await super()._extract_response_content(response, event, session_key, **kwargs)
        finally:
            _MEDIA_SESSION.reset(token)

    def extract_media(self, content: str) -> tuple[list[tuple[str, bool]], str]:
        """Hermes' extraction, each path resolved as Hermes will resolve it, and each path it would drop
        replaced by a notice file it will deliver.

        The resolution is Hermes' own (``filter_media_delivery_paths``: the host, Docker's mounts, then the
        active remote sandbox), with the session key Hermes uses for this response, so a path is never
        replaced that Hermes would have delivered. A resolved path is passed on resolved: Hermes' check
        afterwards accepts it again without a second fetch."""
        media, cleaned = super().extract_media(content)
        out = []
        for path, is_voice in media:
            dropped: list[dict[str, str]] = []
            kept = BasePlatformAdapter.filter_media_delivery_paths(
                [(path, is_voice)], session_key=_MEDIA_SESSION.get(), dropped=dropped)
            if kept:
                out.append(kept[0])
                continue
            why = dropped[0]["reason"] if dropped else ""
            reason = ("Hermes' delivery policy does not allow sending it" if "policy" in why
                      else "the file does not exist on the Hermes host")
            from gateway.platforms.base import _normalize_media_tag_path

            name = os.path.basename(os.path.expanduser(_normalize_media_tag_path(str(path)) or "")) or str(path)
            notice = cache_document_from_bytes(self._not_delivered_text(name, reason).encode(), "undelivered.txt")
            placeholders = self._placeholders()
            placeholders[os.path.realpath(notice)] = (name, reason)
            while len(placeholders) > _NOTICE_MEMORY:
                stale, _ = placeholders.popitem(last=False)
                with contextlib.suppress(OSError):
                    os.remove(stale)
            out.append((notice, False))
        return out, cleaned


def _receipt(path: str, result: Any) -> Receipt:
    if result is not None and getattr(result, "success", False):
        return Receipt(path, message_id=getattr(result, "message_id", None))
    return Receipt(path, reason=str(getattr(result, "error", None) or "not delivered"))
