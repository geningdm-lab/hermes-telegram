"""The acceptance rows: the 17 rows of the PRD (lumi-telegram@16ccae4, docs/prd/1-...md) and the owner's
additions (polls, reactions, stickers, edits and pins, quotes and effects), as live runs on vtadevbot.

Each row sends its fixed prompt from the owner's account, then checks what Telegram delivered to that account
and what the agent's log for the chat holds. What only the owner's eyes on the iOS client can show (how it
looks, a screenshot, a screen recording, a tap inside a widget, the Stop button) is asked of the owner at the
moment it happens, and the answer goes into the report with the checks.
"""

import asyncio
import re
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telethon.tl import functions, types

from hermes_telegram.messages import EFFECTS
from hermes_telegram.widgets import check_widget

from . import samples as S
from .live import Chat, answered, blocks, incoming, shown_text


@dataclass
class Result:
    row: str
    title: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    observed: list[tuple[str, bool]] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(ok for _, ok, _ in self.checks) and all(ok for _, ok in self.observed)


@dataclass
class Run:
    chat: Chat
    owner_id: int
    files: dict[str, Any]
    gateway: Any
    ask: Callable[[str], Awaitable[bool]]
    pause: Callable[[str], Awaitable[None]]
    result: Result = None  # type: ignore[assignment]

    def check(self, what: str, ok: bool, detail: Any = "") -> bool:
        self.result.checks.append((what, bool(ok), str(detail)[:300]))
        return bool(ok)

    async def observe(self, question: str) -> bool:
        ok = await self.ask(question)
        self.result.observed.append((question, ok))
        return ok


ROWS: dict[str, tuple[str, Callable[[Run], Awaitable[None]]]] = {}


def row(key: str, title: str) -> Callable[[Callable[[Run], Awaitable[None]]], Callable[[Run], Awaitable[None]]]:
    def register(fn: Callable[[Run], Awaitable[None]]) -> Callable[[Run], Awaitable[None]]:
        ROWS[key] = (title, fn)
        return fn
    return register


def _prompt(key: str, body: str) -> str:
    return f"Acceptance run, row {key}. Follow these steps exactly and do nothing else.\n{body}"


def _exact(key: str, text: str, done: str) -> str:
    return _prompt(key, "Reply with exactly the Markdown between the lines BEGIN and END, unchanged and complete, "
                        f"then a last line `{done}`. Do not add anything.\nBEGIN\n{text}\nEND")


async def _download(run: Run, message: Any) -> bytes:
    return await run.chat.tg.download_media(message, file=bytes)


def _filename(message: Any) -> str:
    for attribute in getattr(getattr(message, "document", None), "attributes", None) or ():
        if isinstance(attribute, types.DocumentAttributeFilename):
            return attribute.file_name
    return ""


# --- rows 1, 2, 16, 8: answers -------------------------------------------------------------------------------

FORMULA = r"\int_0^1 x^2\,dx = \frac{1}{3}"
SAMPLE = f"""# Heading one
## Heading two

Plain, **bold**, *italic*, <u>underline</u>, ~~strikethrough~~ and ***bold italic with <u>underline</u>***.

- first item
- second item with `inline code`
  - nested item

1. one
2. two

> A quote.

A [link](https://core.telegram.org/bots/api).

```python
print("block code")
```

$${FORMULA}$$"""
_LITERAL = re.compile(r"\*\*|__|~~|^#{1,6} |^\s*\||<u>|\$\$", re.MULTILINE)


async def _answer(run: Run, key: str, prompt: str, done: str, timeout: float = 300.0) -> tuple[Any, list[Any]]:
    sent = await run.chat.say(prompt)
    return sent, await run.chat.wait(sent.id, answered(done), timeout=timeout, quiet=6)


@row("1", "Every formatting element, short and longer than one message")
async def row_1(run: Run) -> None:
    long = "\n\n".join(f"## Part {n} of 6\n\n{SAMPLE}" for n in range(1, 7))
    for size, text in (("short", SAMPLE), ("long", long)):
        sent, messages = await _answer(run, "1", _exact("1", text, f"R1 {size} done"), f"R1 {size} done")
        shown = "\n".join(shown_text(m) for m in messages)
        run.check(f"{size}: no literal markup characters", not _LITERAL.search(shown), _LITERAL.findall(shown)[:5])
        maths = [b.source for m in messages for b in blocks(m) if isinstance(b, types.PageBlockMath)]
        run.check(f"{size}: the formula's source is kept character for character",
                  maths and all(FORMULA in s for s in maths), maths[:2])
        if size == "long":
            order = [int(n) for n in re.findall(r"Part (\d) of 6", shown)]
            run.check("long: several messages, all six parts, in order", len(messages) > 1 and order == list(range(1, 7)),
                      f"{len(messages)} messages, parts {order}")
            run.chat_long = (sent, messages)  # type: ignore[attr-defined]
    await run.observe("Row 1: on iOS, is every element formatted (headings, bold, italic, underline, strikethrough, "
                      "nested emphasis, lists, quote, link, inline and block code, the formula), with no literal "
                      "markup, in both answers? Take the screenshots now.")


TABLE_ROWS = [["Planet", "Moons", "Day (h)", "Ring"], ["Mercury", "0", "4222.6", "no"], ["Venus", "0", "2802.0", "no"],
              ["Earth", "1", "24.0", "no"], ["Mars", "2", "24.7", "no"], ["Jupiter", "95", "9.9", "yes"],
              ["Saturn", "146", "10.7", "yes"]]


@row("2", "A table of 4 columns and 6 rows")
async def row_2(run: Run) -> None:
    table = "\n".join(["| " + " | ".join(TABLE_ROWS[0]) + " |", "|---|---|---|---|"]
                      + ["| " + " | ".join(r) + " |" for r in TABLE_ROWS[1:]])
    _, messages = await _answer(run, "2", _exact("2", table, "R2 done"), "R2 done")
    tables = [b for m in messages for b in blocks(m) if isinstance(b, types.PageBlockTable)]
    cells = [[_cell(c) for c in r.cells] for r in tables[0].rows] if tables else []
    run.check("one native table, cell by cell equal to the source", cells == TABLE_ROWS, cells)
    run.check("no literal | or dashes", not re.search(r"\||---", "\n".join(shown_text(m) for m in messages)))
    await run.observe("Row 2: on iOS, does the table show 4 columns and 6 rows, each value in its cell? Screenshot.")


def _cell(cell: Any) -> str:
    from .live import plain
    return plain(cell.text).strip()


@row("16", "A short plain answer is one ordinary message")
async def row_16(run: Run) -> None:
    sent = await run.chat.say(_prompt("16", "Reply with exactly this sentence and nothing else: "
                                            "Hello from the acceptance run."))
    messages = await run.chat.wait(sent.id, answered("Hello from the acceptance run."), quiet=6)
    run.check("one message", len(messages) == 1, len(messages))
    run.check("an ordinary text message, not a rich one",
              all(getattr(m, "rich_message", None) is None and m.message.strip() == "Hello from the acceptance run."
                  for m in messages), [shown_text(m) for m in messages])
    await run.observe("Row 16: on iOS, is it one plain message laid out as plain messages were before the plugin?")


# --- rows 3, 4, 5, 15: files ---------------------------------------------------------------------------------

async def _send_files(run: Run, key: str, names: list[str], *, as_files: bool = False) -> list[Any]:
    paths = ", ".join(f'"{run.files[n]}"' for n in names)
    body = (f"Call the telegram_send tool once with files [{paths}]" + (" and as_files true" if as_files else "")
            + f". Then reply `R{key} done`.")
    sent = await run.chat.say(_prompt(key, body))
    return await run.chat.wait(sent.id, answered(f"R{key} done"), quiet=8)


@row("3", "One file of each kind arrives as that kind")
async def row_3(run: Run) -> None:
    messages = await _send_files(run, "3", ["document", "photo", "video", "audio", "voice"])
    docs = [m for m in messages if m.document and not (m.video or m.audio or m.voice) and _filename(m)]
    run.check("the document keeps its filename", any(_filename(m) == run.files["document"].name for m in docs),
              [_filename(m) for m in docs])
    for doc in docs:
        if _filename(doc) == run.files["document"].name:
            run.check("the document's bytes are unchanged",
                      S.sha256(await _download(run, doc)) == S.sha256(run.files["document"].read_bytes()))
    for kind in ("photo", "video", "audio", "voice"):
        run.check(f"a {kind} arrives as a {kind}", any(getattr(m, kind, None) for m in messages))
    await run.observe("Row 3: on iOS, does the photo show, and do the video, audio and voice play in the chat?")


async def _album(run: Run, count: int) -> list[list[str]]:
    names = [f"photo-{n:02d}" for n in range(1, count + 1)]
    messages = await _send_files(run, f"4-{count}", names)
    photos = [m for m in messages if m.photo]
    groups: list[list[Any]] = []
    for m in photos:
        if groups and m.grouped_id and groups[-1][0].grouped_id == m.grouped_id:
            groups[-1].append(m)
        else:
            groups.append([m])
    return [[S.colour_of(await _download(run, m)) for m in group] for group in groups]


@row("4", "2 to 10 photos are one album; 12 are albums of 10 and 2, in order")
async def row_4(run: Run) -> None:
    three = await _album(run, 3)
    run.check("3 photos: one album, in order", three == [S.COLOURS[:3]], three)
    twelve = await _album(run, 12)
    run.check("12 photos: albums of 10 and 2, in order", twelve == [S.COLOURS[:10], S.COLOURS[10:12]], twelve)
    await run.observe("Row 4: on iOS, one album of 3, then an album of 10 and one of 2, in order?")


@row("5", "A photo, video, audio and voice \"as a file\" arrive as documents, unchanged")
async def row_5(run: Run) -> None:
    names = ["photo", "video", "audio", "voice"]
    messages = await _send_files(run, "5", names, as_files=True)
    by_name = {_filename(m): m for m in messages if m.document}
    for name in names:
        path = run.files[name]
        doc = by_name.get(path.name)
        ok = (doc is not None and not (doc.photo or doc.video or doc.audio or doc.voice)
              and S.sha256(await _download(run, doc)) == S.sha256(path.read_bytes()))
        run.check(f"{path.name}: a document with its filename and bytes", ok)
    await run.observe("Row 5: on iOS, are the four shown as downloadable files, not as a photo or a player?")


@row("15", "A file that cannot be delivered is named with the reason")
async def row_15(run: Run) -> None:
    messages = await _send_files(run, "15", ["big", "empty"])
    text = "\n".join(shown_text(m) for m in messages)
    run.check("the file over 50 MB is named, with the limit", run.files["big"].name in text and "50" in text, text[:300])
    run.check("the refused file is named, with a reason", run.files["empty"].name in text, text[:300])
    await run.observe("Row 15: on iOS, does a message name each file and say why it did not arrive?")


# --- rows 6, 7, 8, 9, 10: the preview ---------------------------------------------------------------------------

@row("6", "The preview reads Contemplating..., then each status the agent sets")
async def row_6(run: Run) -> None:
    await run.pause("Row 6: start a screen recording of the chat on iOS, then press Enter.")
    start = time.time()
    _, messages = await _answer(run, "6", _prompt("6", (
        "1. Call telegram_status with text \"Reading the samples...\".\n2. Run `sleep 8` in the terminal.\n"
        "3. Call telegram_status with text \"Writing the answer...\".\n4. Run `sleep 8` in the terminal.\n"
        "5. Reply `R6 done`.")), "R6 done")
    frames = [f.text for f in run.chat.frames if f.at >= start]
    firsts = [next((i for i, t in enumerate(frames) if s in t), None)
              for s in ("Contemplating...", "Reading the samples...", "Writing the answer...")]
    run.check("Contemplating..., then each status, in order", None not in firsts and firsts == sorted(firsts), firsts)
    run.check("row 8: one answer message and nothing else from the bot",
              len(messages) == 1 and "R6 done" in shown_text(messages[0]), [shown_text(m) for m in messages])
    final_at = messages[0].date.timestamp() if messages else time.time()
    late = [f for f in run.chat.frames if f.at > final_at + 3]
    run.check("row 8: no preview after the final answer", not late, len(late))
    await run.observe("Rows 6 and 8: does the recording show Contemplating..., then each status, and after the "
                      "answer no preview left? Stop the recording and keep it.")


@row("7", "The preview shows the answer growing until the final replaces it")
async def row_7(run: Run) -> None:
    await run.pause("Row 7: start a screen recording of the chat on iOS, then press Enter.")
    start = time.time()
    _, messages = await _answer(run, "7", _prompt("7", (
        "Write the numbers from one to three hundred in English words, one per line, starting with `one`, then a "
        "last line `R7 done`.")), "R7 done", timeout=420)
    partial = [f for f in run.chat.frames if f.at >= start and "one" in f.text.split()]
    growing = any(len(b.text) > len(a.text) for a, b in zip(partial, partial[1:]))
    run.check("the preview held the beginning of the answer at least twice, longer the second time",
              len(partial) >= 2 and growing, [len(f.text) for f in partial][:10])
    run.check("the final answer arrived", bool(messages))
    await run.observe("Row 7: does the recording show the text growing in the preview before the final answer?")


@row("9", "Stop stops the work within 5 seconds")
async def row_9(run: Run) -> None:
    await run.pause("Row 9: start a timed screen recording on iOS, then press Enter. When asked, tap Stop on the "
                    "preview and press Enter right after.")
    start = time.time()
    sent = await run.chat.say(_prompt("9", (
        "1. Run `sleep 20` in the terminal.\n"
        f"2. Call telegram_send with files [\"{run.files['stop']}\"].\n"
        "3. Run `sleep 15` in the terminal.\n4. Reply `R9 done`.")))
    while not any(f.can_stop and f.at >= start for f in run.chat.frames):
        await asyncio.sleep(0.5)
        if time.time() - start > 60:
            run.check("a preview with a Stop button appears", False)
            return
    await asyncio.sleep(5)
    await run.pause("Tap Stop on the preview now, then press Enter.")
    tapped = time.time()
    await asyncio.sleep(60)
    messages = await run.chat.from_bot(sent.id)
    shown = [shown_text(m) for m in messages]
    run.check("a persisted Stopped message", any("Stopped" in t or "stopped" in t for t in shown), shown[:4])
    run.check("neither the file nor the answer arrives in the 60 s after",
              not any(_filename(m) == run.files["stop"].name for m in messages) and not any("R9 done" in t for t in shown))
    frozen = [f for f in run.chat.frames if f.at > tapped + 5]
    run.check("the preview stops changing within 5 s", not frozen, len(frozen))
    steps = [m for m in _log(run, start) if m["role"] in ("assistant", "tool")]
    last = max((m["at"] for m in steps), default=start)
    run.check("the agent's last step is within 5 s of the tap (Enter pressed after it)", last <= tapped + 5,
              f"last step {last - tapped:+.1f} s from the tap")
    await run.observe("Row 9: does the recording show the preview frozen within 5 s of the tap, then Stopped and "
                      "the partial result, and nothing else for a minute?")


def _log(run: Run, since: float) -> list[dict[str, Any]]:
    from .live import agent_log
    return agent_log(run.owner_id, since)


@row("10", "A failed answer leaves one message saying so and no preview")
async def row_10(run: Run) -> None:
    run.gateway.start(broken=True)  # the model key the provider refuses
    try:
        start = time.time()
        sent = await run.chat.say(_prompt("10", "Reply `hello`."))
        messages = await run.chat.wait(sent.id, lambda ms: bool(ms), timeout=180, quiet=20)
        run.check("one message from the bot", len(messages) == 1, [shown_text(m) for m in messages])
        final_at = messages[-1].date.timestamp() if messages else time.time()
        run.check("no preview after it", not [f for f in run.chat.frames if f.at > max(final_at, start) + 3])
        await run.observe("Row 10: on iOS, one message saying the answer failed, and no preview?")
    finally:
        run.gateway.start()


# --- row 11 and the owner's additions: actions -----------------------------------------------------------------

@row("11", "A choice reaches the agent once; a second tap says already answered")
async def row_11(run: Run) -> None:
    start = time.time()
    sent = await run.chat.say(_prompt("11", (
        "Call telegram_send with text \"Pick a city\" and three buttons Paris, Rome and Oslo with values paris, rome "
        "and oslo. Then reply `R11 offered`. When a [button] message comes back, reply `You chose ` and the city.")))
    messages = await run.chat.wait(sent.id, answered("R11 offered"), quiet=3)
    offer = next(m for m in messages if m.buttons)
    await offer.click(text="Rome")
    await run.chat.wait(sent.id, answered("You chose"), quiet=8)
    second = await offer.click(text="Paris")
    await asyncio.sleep(20)
    chosen = [shown_text(m) for m in await run.chat.from_bot(sent.id) if "You chose" in shown_text(m)]
    run.check("the answer names the tapped choice, once", chosen == [c for c in chosen if "Rome" in c] and len(chosen) == 1,
              chosen)
    run.check("the second tap shows Already answered", "Already answered" in str(getattr(second, "message", "")),
              getattr(second, "message", None))
    taps = incoming(run.owner_id, start, "[button]")
    run.check("the agent's log shows exactly one incoming choice, the tapped one",
              len(taps) == 1 and '"Rome"' in taps[0], taps)
    await run.observe("Row 11: on iOS, were the three buttons shown, and did the second tap show the notice?")


@row("X1", "A poll's answer reaches the agent")
async def row_x1(run: Run) -> None:
    start = time.time()
    sent = await run.chat.say(_prompt("X1", (
        "Call telegram_poll with question \"Capital of Italy?\" and options Paris and Rome. Then reply `X1 asked`. "
        "When a [poll] message comes back, reply `You voted ` and the option.")))
    messages = await run.chat.wait(sent.id, answered("X1 asked"), quiet=3)
    poll = next(m for m in messages if isinstance(m.media, types.MessageMediaPoll))
    option = poll.media.poll.answers[1].option
    await run.chat.tg(functions.messages.SendVoteRequest(peer=run.chat.bot, msg_id=poll.id, options=[option]))
    answer = await run.chat.wait(sent.id, answered("You voted"), quiet=3)
    run.check("the agent names the option voted", any("Rome" in shown_text(m) for m in answer if "You voted" in shown_text(m)))
    run.check("the agent's log shows the vote", any('"Rome"' in t for t in incoming(run.owner_id, start, "[poll]")))


@row("X2", "Reactions both ways")
async def row_x2(run: Run) -> None:
    start = time.time()
    sent = await run.chat.say(_prompt("X2", "Call telegram_react with emoji 👍 on my message. Then reply `X2 reacted`."))
    messages = await run.chat.wait(sent.id, answered("X2 reacted"), quiet=3)
    mine = await run.chat.message(sent.id)
    emojis = [getattr(r.reaction, "emoticon", None) for r in getattr(mine.reactions, "results", None) or ()]
    run.check("the bot's 👍 is on the owner's message and stays after the turn", "👍" in emojis, emojis)
    answer = next(m for m in messages if "X2 reacted" in shown_text(m))
    await run.chat.tg(functions.messages.SendReactionRequest(peer=run.chat.bot, msg_id=answer.id,
                                                             reaction=[types.ReactionEmoji(emoticon="🔥")]))
    await asyncio.sleep(20)
    run.check("the owner's 🔥 on the bot's answer reaches the agent",
              any("🔥" in t for t in incoming(run.owner_id, start, "[reaction]")))


@row("X3", "The agent finds the sticker the owner sent and sends it back")
async def row_x3(run: Run) -> None:
    recent = await run.chat.tg(functions.messages.GetRecentStickersRequest(hash=0))
    if not getattr(recent, "stickers", None):
        run.check("the owner's account has a recent sticker to send", False)
        return
    sticker = recent.stickers[0]
    emoji = next((a.alt for a in sticker.attributes if isinstance(a, types.DocumentAttributeSticker)), "")
    await run.chat.tg.send_file(run.chat.bot, sticker)
    await asyncio.sleep(20)  # Hermes describes it once (vision) and the plugin records it
    sent = await run.chat.say(_prompt("X3", (
        f"Call telegram_sticker with op find and query \"{emoji}\", then telegram_sticker with op send and the first "
        "candidate's sticker_id. Then reply `X3 sent`.")))
    messages = await run.chat.wait(sent.id, answered("X3 sent"), quiet=3)
    run.check("a sticker arrives", any(m.sticker for m in messages))


@row("X4", "The agent edits, pins and deletes its own messages")
async def row_x4(run: Run) -> None:
    sent = await run.chat.say(_prompt("X4", (
        "1. Call telegram_send with text \"X4 draft\".\n2. Call telegram_send with text \"X4 to delete\".\n"
        "3. Call telegram_message with op edit on the first message's id and text \"X4 final\".\n"
        "4. Call telegram_message with op pin on the first message's id.\n"
        "5. Call telegram_message with op delete on the second message's id.\n6. Reply `X4 done`.")))
    messages = await run.chat.wait(sent.id, answered("X4 done"), quiet=5)
    shown = [shown_text(m) for m in messages]
    run.check("the first message reads X4 final", any("X4 final" in t for t in shown) and not any("X4 draft" in t for t in shown),
              shown)
    run.check("it is pinned", any(m.pinned for m in messages if "X4 final" in shown_text(m)))
    run.check("the second is deleted", not any("X4 to delete" in t for t in shown))


@row("X5", "A reply quoting the owner's words, with an effect")
async def row_x5(run: Run) -> None:
    sent = await run.chat.say("X5: the plan is ready. " + _prompt("X5", (
        "Call telegram_send with text \"Noted\", quote \"the plan\" and effect 🎉. Then reply `X5 done`.")))
    messages = await run.chat.wait(sent.id, answered("X5 done"), quiet=3)
    noted = next((m for m in messages if shown_text(m).strip() == "Noted"), None)
    reply = getattr(noted, "reply_to", None)
    run.check("it replies to the owner's message quoting \"the plan\"",
              reply is not None and reply.reply_to_msg_id == sent.id and reply.quote_text == "the plan", reply)
    run.check("Telegram applied the 🎉 effect (the id the plugin ships is real)",
              noted is not None and str(getattr(noted, "effect", "")) == EFFECTS["🎉"], getattr(noted, "effect", None))
    await run.observe("X5: on iOS, did the 🎉 effect play on the Noted message?")


# --- rows 12, 13, 14: widgets ----------------------------------------------------------------------------------

WIDGETS = {
    "diagram": "a diagram of the request flow Telegram -> Hermes -> model -> tools -> answer, drawn with inline SVG",
    "chart": "a bar chart of the moons of Jupiter (95), Saturn (146), Uranus (28) and Neptune (16), in inline SVG, "
             "where tapping a bar shows its value",
    "explanation": "a step-by-step visual explanation of binary search on a sorted list of 8 numbers, with Next and "
                   "Back buttons that change the step inside the widget",
    "prototype": "a UI prototype of a two-screen settings app (a list screen and a detail screen), switching screens "
                 "inside the widget",
}


@row("12", "A diagram, a chart, an explanation and a prototype, each a widget that draws with no network")
async def row_12(run: Run) -> None:
    run.widgets = {}  # type: ignore[attr-defined]
    for kind, what in WIDGETS.items():
        sent = await run.chat.say(_prompt("12", (
            f"Call telegram_widget with {what}, with caption \"R12 {kind}\". Then reply `R12 {kind} done`.")))
        messages = await run.chat.wait(sent.id, answered(f"R12 {kind} done"), quiet=3)
        widget = next((m for m in messages if isinstance(m.media, types.MessageMediaWidget)), None)
        run.check(f"{kind}: a widget in the chat", widget is not None)
        if widget is not None:
            run.widgets[kind] = widget  # type: ignore[attr-defined]
            html = _widget_html(widget.media.payload)
            run.check(f"{kind}: its HTML calls no network API and loads nothing from outside",
                      html is not None and check_widget(html) is None, check_widget(html or "") if html else "unreadable")
    await run.observe("Row 12: on iOS, does each of the four widgets draw fully in the chat? Screenshot each.")


def _widget_html(payload: bytes) -> Any:
    for decode in (lambda b: zlib.decompress(b), lambda b: zlib.decompress(b, -15), lambda b: b):
        try:
            return decode(payload).decode("utf-8")
        except Exception:
            continue
    return None


@row("13", "Interaction inside a widget changes it and reaches nobody")
async def row_13(run: Run) -> None:
    start = time.time()
    await run.pause("Row 13: on iOS, tap Next in the explanation, tap a bar of the chart and switch the prototype's "
                    "screen (record the screen), then press Enter.")
    run.check("the agent's log shows nothing new from the chat", not incoming(run.owner_id, start),
              incoming(run.owner_id, start))
    await run.observe("Row 13: did each widget change on each interaction?")


@row("14", "A widget action that needs the agent reaches it with its widget")
async def row_14(run: Run) -> None:
    sent = await run.chat.say(_prompt("14", (
        "Call telegram_widget with a small card showing \"Step 2: compare\" and a button \"Ask Lumi\" whose click "
        "calls TelegramWidget.request(\"explain step 2\"), caption \"R14\". Then reply `R14 sent`. When a [widget] "
        "message comes back, reply naming its data and its widget id.")))
    messages = await run.chat.wait(sent.id, answered("R14 sent"), quiet=3)
    widget = next((m for m in messages if isinstance(m.media, types.MessageMediaWidget)), None)
    start = time.time()
    await run.pause("Row 14: on iOS, tap Ask Lumi in the widget, then press Enter.")
    answers = await run.chat.wait(sent.id, answered("explain step 2"), timeout=180, quiet=5)
    actions = incoming(run.owner_id, start, "[widget]")
    run.check("the agent's log shows one incoming action with that data and that widget",
              widget is not None and len(actions) == 1 and "explain step 2" in actions[0] and str(widget.id) in actions[0],
              actions)
    run.check("the answer names both", widget is not None and any(
        "explain step 2" in shown_text(m) and str(widget.id) in shown_text(m) for m in answers))


# --- row 17: cron reports and approvals during a preview --------------------------------------------------------

@row("17", "A cron report and an approval during a previewed turn")
async def row_17(run: Run) -> None:
    sent = await run.chat.say(_prompt("17", (
        "Create a one-time cron job that runs 2 minutes from now and delivers exactly the text `R17 cron report` to "
        "this chat. Then reply `R17 scheduled`.")))
    await run.chat.wait(sent.id, answered("R17 scheduled"), quiet=2)
    await asyncio.sleep(45)
    work = await run.chat.say(_prompt("17", (
        "1. Run `rm -rf /tmp/hermes-acceptance-row-17` in the terminal.\n2. Run `sleep 90` in the terminal.\n"
        "3. Reply `R17 done`.")))
    prompt = (await run.chat.wait(work.id, lambda ms: any(m.buttons for m in ms), timeout=120))
    approval = next(m for m in prompt if m.buttons)
    labels = [b.text for r in approval.buttons for b in r]
    once = next((t for t in labels if re.search(r"once|approve|allow", t, re.IGNORECASE)), labels[0])
    await approval.click(text=once)
    messages = await run.chat.wait(work.id, answered("R17 done"), timeout=300, quiet=5)
    shown = [shown_text(m) for m in messages]
    run.check("the cron report arrived in the chat", any("R17 cron report" in t for t in shown), shown)
    still = await run.chat.message(approval.id)
    run.check("the approval prompt is still visible", still is not None and not getattr(still, "deleted", False))
    run.check("the work went on after one tap (the terminal ran once)", any("R17 done" in t for t in shown))
    await run.observe("Row 17: on iOS, did the report arrive while the preview ran, and does the approval prompt stay "
                      "visible with its choice resolved?")
