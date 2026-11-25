"""Files the agent sends: delivered as their kind, or named with the reason they were not (slice 2).

Rows 3, 4, 5 and 15 of docs/design.md. The harvested senders already deliver each kind natively and send
photos as albums of up to 10. This mixin adds what the rows need on top of them:

- A document is a document: ``disable_content_type_detection`` keeps Telegram from turning a video or a
  GIF sent "as a file" into a playable message (core.telegram.org/bots/api/methods/senddocument.md).
- A file over Telegram's upload limit for bots is not uploaded: the chat gets a notice naming it and its
  size against the limit (50 MB; 2000 MB with a local Bot API server, ``local_mode``; api.md, "Sending
  files").
- A failed upload's notice names the file and Telegram's reason; the bundled notice named neither.
- A ``MEDIA:`` path Hermes would drop because it is not on this host, or because its delivery policy
  denies it, is named too. Hermes only logs those (``_validated_delivery_path``); the chat saw nothing.
  The path is swapped for a small notice file that Hermes does deliver, to ``send_document``, which then
  posts the notice instead of uploading.
"""

import contextvars
import logging
import os
from collections import OrderedDict
from typing import Any, Optional

from gateway.platforms.base import SendResult, cache_document_from_bytes, validate_media_delivery_path

from .harvested.adapter_module import _redact_telegram_error_text

logger = logging.getLogger(__name__)

MB = 1024 * 1024
UPLOAD_LIMIT = 50 * MB  # multipart upload limit of the Bot API for bots
LOCAL_SERVER_UPLOAD_LIMIT = 2000 * MB
_NOTICE_MEMORY = 512

# The last upload error in this task, so the final fallback notice can say why.
_last_upload_error: contextvars.ContextVar[str] = contextvars.ContextVar("hermes_telegram_upload_error", default="")


def _size_text(size: int) -> str:
    return f"{size / MB:.1f} MB"


class FilesMixin:
    """File delivery overrides; listed before the harvested mixins."""

    # --- limits and notices ------------------------------------------------------------------------------

    def _upload_limit(self) -> int:
        return LOCAL_SERVER_UPLOAD_LIMIT if self.config.extra.get("local_mode") else UPLOAD_LIMIT

    def _notified(self) -> "OrderedDict[str, None]":
        return self.__dict__.setdefault("_hermes_telegram_notified_paths", OrderedDict())

    def _placeholders(self) -> "OrderedDict[str, tuple[str, str]]":
        return self.__dict__.setdefault("_hermes_telegram_undelivered", OrderedDict())

    @staticmethod
    def _not_delivered_text(name: str, reason: str) -> str:
        return f"{name} was not delivered: {reason}."

    async def _not_delivered(self, chat_id: str, path: str, reason: str, metadata: Optional[dict[str, Any]],
                             *, name: Optional[str] = None) -> SendResult:
        """Tell the chat that a file did not arrive and why, once per file; the result is the failure."""
        name = name or os.path.basename(path)
        notified = self._notified()
        notified[path] = None
        while len(notified) > _NOTICE_MEMORY:
            notified.popitem(last=False)
        text = self._not_delivered_text(name, reason)
        logger.warning("[%s] %s", self.name, text)
        try:
            # Not emit_warning: that is a diagnostic a profile can switch off, and row 15 says a file that
            # did not arrive is never silence.
            await self.send(chat_id, text, metadata=metadata)
        except Exception:  # The notice is best effort; the failure result below still reaches the caller.
            logger.warning("[%s] could not send the not-delivered notice for %s", self.name, name, exc_info=True)
        return SendResult(success=False, error=text, retryable=False)

    def _too_large(self, path: str) -> Optional[str]:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None  # missing files are the harvested senders' to report
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
        error = _last_upload_error.get()
        reason = f"Telegram refused the upload ({error})" if error else "the upload to Telegram failed"
        return await self._not_delivered(chat_id, path, reason, metadata, name=file_name)

    async def _send_local_file(self, label: str, path: str, chat_id: Any, reply_to: Any, metadata: Any,
                               media_key: str, build_kwargs: Any, on_error: Any) -> SendResult:
        async def remember_then(error: Exception) -> SendResult:
            _last_upload_error.set(_redact_telegram_error_text(error))
            return await on_error(error)
        return await super()._send_local_file(label, path, chat_id, reply_to, metadata, media_key, build_kwargs,
                                              remember_then)

    # --- senders -----------------------------------------------------------------------------------------

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        placeholder = self._placeholders().pop(file_path, None)
        if placeholder is not None:
            name, reason = placeholder
            try:
                os.remove(file_path)
            except OSError:
                pass
            return await self._not_delivered(chat_id, file_path, reason, metadata, name=name)
        too_large = self._too_large(file_path)
        if too_large:
            return await self._not_delivered(chat_id, file_path, too_large, metadata, name=file_name)
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
        return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        too_large = self._too_large(audio_path)
        if too_large:
            return await self._not_delivered(chat_id, audio_path, too_large, metadata)
        return await super().send_voice(chat_id, audio_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        # A photo over 10 MB is compressed or sent as a document by the harvested sender; past the
        # document limit nothing can carry it.
        too_large = self._too_large(image_path)
        if too_large:
            return await self._not_delivered(chat_id, image_path, too_large, metadata)
        return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata, **kwargs)

    async def send_multiple_images(self, chat_id: str, images: list, metadata: Optional[dict[str, Any]] = None,
                                   human_delay: float = 0.0) -> Any:
        kept = []
        for image in images:
            source = image[0] if isinstance(image, (list, tuple)) else image
            path = str(source)[len("file://"):] if str(source).startswith("file://") else ""
            from urllib.parse import unquote
            too_large = self._too_large(unquote(path)) if path else None
            if too_large:
                await self._not_delivered(chat_id, unquote(path), too_large, metadata)
            else:
                kept.append(image)
        if not kept:
            return None
        if len(kept) == 1 and str(kept[0][0] if isinstance(kept[0], (list, tuple)) else kept[0]).startswith("file://"):
            # Hermes sends even one image through here, and Telegram refuses an album of one ("2-10
            # items"); the harvested sender then falls back per image. Send it as the photo it is.
            from urllib.parse import unquote

            source, caption = (kept[0][0], kept[0][1] if len(kept[0]) > 1 else None) if isinstance(
                kept[0], (list, tuple)) else (kept[0], None)
            return await self.send_image_file(chat_id, unquote(str(source)[len("file://"):]), caption or None,
                                              metadata=metadata)
        return await super().send_multiple_images(chat_id, kept, metadata, human_delay)

    # --- MEDIA paths Hermes would drop -------------------------------------------------------------------

    def extract_media(self, content: str) -> tuple[list[tuple[str, bool]], str]:
        """Hermes' extraction, with each path it would drop replaced by a notice file it will deliver.

        Only on a local terminal backend: with a remote sandbox (or Docker) Hermes may still fetch or
        translate a path this host does not see, so such paths are left to it.
        """
        media, cleaned = super().extract_media(content)
        from gateway.platforms.base import _docker_env_active, _normalize_media_tag_path, _tenv

        if _tenv("TERMINAL_ENV", "local").strip().lower() != "local" or _docker_env_active():
            return media, cleaned
        out = []
        for path, is_voice in media:
            if validate_media_delivery_path(path):
                out.append((path, is_voice))
                continue
            normalized = os.path.expanduser(_normalize_media_tag_path(path) or "")
            reason = ("Hermes' delivery policy does not allow sending it" if os.path.isfile(normalized)
                      else "the file does not exist on the Hermes host")
            name = os.path.basename(normalized) or path
            notice = cache_document_from_bytes(self._not_delivered_text(name, reason).encode(), "undelivered.txt")
            placeholders = self._placeholders()
            placeholders[notice] = (name, reason)
            while len(placeholders) > _NOTICE_MEMORY:
                stale, _ = placeholders.popitem(last=False)
                try:
                    os.remove(stale)
                except OSError:
                    pass
            out.append((notice, False))
        return out, cleaned
