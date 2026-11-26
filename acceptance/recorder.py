"""A recording proxy between the acceptance gateway and the Bot API.

The gateway's python-telegram-bot talks to ``http://127.0.0.1:<port>/bot<token>/<method>`` (the plugin's
``extra.base_url``). The proxy forwards each request unchanged to api.telegram.org, hands Telegram's answer
back unchanged, and records the call: the method, its fields, Telegram's result or refusal, and when.
Uploads are recorded as their file name, size and SHA-256. The bot's token is in the request path and is
never recorded.

The record is what the bot sent and what Telegram made of it: a send's result is the message as Telegram
stored it (a rich message's parsed blocks, the effect it applied, the quote). The results of getUpdates are
what the owner did on iOS (messages, taps, votes, reactions, Stop, widget requests) as Telegram delivered
them to the bot. Each run's record is also written as JSON lines to a file outside the repository.
"""

import asyncio
import hashlib
import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

UPSTREAM = "https://api.telegram.org"
# Python-telegram-bot sends a form; a field holding an object or a list is JSON in it.
_JSONISH = re.compile(r"^\s*[\[{]|^(true|false|null)$")
UPLOAD_CAP = 64 * 1024 * 1024  # above the Bot API's 50 MB, so an upload the plugin should refuse still shows


@dataclass
class Call:
    """One Bot API call of the bot, as the proxy saw it."""

    at: float
    method: str
    params: dict[str, Any]
    ok: bool
    result: Any = None
    error: str = ""
    done: float = 0.0


@dataclass
class Update:
    """One update Telegram delivered to the bot, at the moment the proxy handed it over."""

    at: float
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        return next((k for k in self.data if k != "update_id"), "")

    @property
    def body(self) -> dict[str, Any]:
        value = self.data.get(self.kind)
        return value if isinstance(value, dict) else {}


def _value(text: str) -> Any:
    if _JSONISH.match(text):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def _param(header: str, key: str) -> Optional[str]:
    found = re.search(rf'(?:^|;)\s*{key}="([^"]*)"', header) or re.search(rf"(?:^|;)\s*{key}=([^;\s]+)", header)
    return found.group(1) if found else None


def _multipart(content_type: str, body: bytes) -> dict[str, Any]:
    boundary = _param(content_type, "boundary")
    if not boundary:
        return {"_body": f"{len(body)} bytes of multipart without a boundary"}
    out: dict[str, Any] = {}
    # RFC 2046: each delimiter is CRLF "--" boundary; the CRLF before it belongs to the delimiter.
    for part in (b"\r\n" + body).split(b"\r\n--" + boundary.encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, data = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        disposition = next((line.split(b":", 1)[1].decode("utf-8", "replace") for line in head.split(b"\r\n")
                            if line.lower().startswith(b"content-disposition:")), "")
        name, filename = _param(disposition, "name"), _param(disposition, "filename")
        if name is None:
            continue
        if filename is None:
            out[name] = _value(data.decode("utf-8", "replace"))
        else:
            out[name] = {"upload": filename, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return out


def parse_params(content_type: str, body: bytes) -> dict[str, Any]:
    """A request's fields: JSON, a form or multipart; an upload as its name, size and SHA-256."""
    if not body:
        return {}
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "application/json":
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"_body": parsed}
    if kind == "application/x-www-form-urlencoded":
        return {k: _value(v) for k, v in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)}
    if kind == "multipart/form-data":
        return _multipart(content_type, body)
    return {"_body": f"{len(body)} bytes of {kind or 'unknown'}"}


class Recorder:
    """The proxy and its record. ``start`` returns the base for ``extra.base_url`` (append ``/bot``)."""

    def __init__(self, log: Path, token: str, *, upstream: str = UPSTREAM) -> None:
        self.log = log
        self.upstream = upstream.rstrip("/")
        self._token = token  # for ``download`` only; never written
        self.calls: list[Call] = []
        self.updates: list[Update] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> str:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=900))
        app = web.Application(client_max_size=UPLOAD_CAP)
        app.router.add_route("*", "/bot{token}/{method}", self._api)
        app.router.add_route("GET", "/file/bot{token}/{path:.+}", self._file)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        await web.SockSite(self._runner, sock).start()
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    async def _api(self, request: web.Request) -> web.StreamResponse:
        token, method = request.match_info["token"], request.match_info["method"]
        body = await request.read()
        content_type = request.headers.get("Content-Type", "")
        at = time.time()
        assert self._session is not None
        try:
            async with self._session.request(request.method, f"{self.upstream}/bot{token}/{method}",
                                             params=request.query, data=body or None,
                                             headers={"Content-Type": content_type} if content_type else None
                                             ) as upstream:
                payload, status = await upstream.read(), upstream.status
                reply_type = upstream.headers.get("Content-Type", "application/json")
        except (aiohttp.ClientError, TimeoutError) as exc:
            self._record_call(Call(at, method, parse_params(content_type, body), False,
                                   error=f"the proxy could not reach Telegram: {type(exc).__name__}",
                                   done=time.time()))
            return web.Response(status=502, text="Bad Gateway")
        try:
            reply = json.loads(payload)
        except ValueError:
            reply = {"ok": False, "description": f"HTTP {status}, not JSON"}
        done = time.time()
        if method == "getUpdates" and reply.get("ok"):
            for data in reply.get("result") or ():
                self.updates.append(Update(done, data))
                self._write({"update": data, "at": done})
        else:
            self._record_call(Call(at, method, parse_params(content_type, body), bool(reply.get("ok")),
                                   result=reply.get("result"), error=str(reply.get("description") or ""),
                                   done=done))
        return web.Response(body=payload, status=status, headers={"Content-Type": reply_type})

    async def _file(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        url = f"{self.upstream}/file/bot{request.match_info['token']}/{request.match_info['path']}"
        async with self._session.get(url) as upstream:
            return web.Response(body=await upstream.read(), status=upstream.status,
                                headers={"Content-Type": upstream.headers.get("Content-Type",
                                                                              "application/octet-stream")})

    def _record_call(self, call: Call) -> None:
        self.calls.append(call)
        self._write({"call": asdict(call)})

    def _write(self, entry: dict[str, Any]) -> None:
        with open(self.log, "a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- reading the record ------------------------------------------------------------------------------------

    def sent(self, since: float, methods: Optional[set[str]] = None) -> list[Call]:
        """The bot's calls since ``since``, oldest first, ``sendChatAction`` (typing) left out."""
        return [c for c in self.calls if c.at >= since and c.method != "sendChatAction"
                and (methods is None or c.method in methods)]

    def delivered(self, since: float, kind: Optional[str] = None) -> list[Update]:
        """What Telegram delivered to the bot since ``since``, optionally only one kind of update."""
        return [u for u in self.updates if u.at >= since and (kind is None or u.kind == kind)]

    async def wait_for(self, found: Callable[[], Any], *, timeout: float, what: str) -> Any:
        """``found()`` once it is truthy; ``TimeoutError`` naming ``what`` otherwise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = found()
            if value:
                return value
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{what}: nothing within {timeout:.0f} s")

    async def download(self, file_id: str) -> bytes:
        """A file as Telegram serves it (getFile, at most 20 MB), outside the record."""
        assert self._session is not None
        base = f"{self.upstream}/bot{self._token}"
        async with self._session.post(f"{base}/getFile", data={"file_id": file_id}) as reply:
            path = (await reply.json())["result"]["file_path"]
        async with self._session.get(f"{self.upstream}/file/bot{self._token}/{path}") as reply:
            reply.raise_for_status()
            return await reply.read()
