"""The acceptance rows: the 17 rows of the PRD (lumi-telegram@16ccae4, docs/prd/1-...md) and the owner's
additions (polls, reactions, stickers, edits and pins, quotes and effects), as live runs on vtadevbot.

The owner sends each row's code (``r2``) to the bot from the iOS app. The agent carries out that code's steps
from its playbook (``playbook()``, the acceptance home's SOUL.md). The run then checks the record of what the
bot sent and what Telegram made of it (``recorder``) and the agent's log, prints that record, and asks the
owner whether iOS shows the same. What only the owner's eyes can tell (how it looks, a screen recording, a
tap inside a widget, the Stop button) is asked at that moment. Both go into the report.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from hermes_telegram.messages import EFFECTS
from hermes_telegram.widgets import check_widget

from . import samples as S
from .evidence import (agent_log, deleted, frames, incoming, messages, of_type, shown, table_cells, timeline,
                       uploads)
from .home import Gateway
from .recorder import Call, Recorder, Update

Steps = Union[str, Callable[[dict[str, Path]], str]]


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


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


@dataclass
class Run:
    rec: Recorder
    owner_id: int
    files: dict[str, Path]
    gateway: Gateway
    ask: Callable[[str], Awaitable[bool]]
    pause: Callable[[str], Awaitable[None]]
    say: Callable[[str], None] = print
    result: Result = None  # type: ignore[assignment]
    sha: dict[str, str] = field(default_factory=dict)

    def check(self, what: str, ok: Any, detail: Any = "") -> bool:
        self.result.checks.append((what, bool(ok), str(detail)[:300]))
        return bool(ok)

    async def observe(self, question: str) -> bool:
        ok = await self.ask(question)
        self.result.observed.append((question, ok))
        return ok

    def _owners(self, update: Update) -> bool:
        # A Stop (MessageGenerationStopped) names only the chat; in the private chat its id is the owner's.
        body = update.body
        return (body.get("from") or body.get("user") or body.get("chat") or {}).get("id") == self.owner_id

    async def expect(self, kind: str, instruction: str, since: float, match: Callable[[Update], bool] = lambda u: True,
                     timeout: float = 600.0) -> Update:
        """Tell the owner what to do on iOS; the update it makes Telegram deliver."""
        self.say(f"\n>>> On iOS: {instruction}")
        return await self.rec.wait_for(
            lambda: next((u for u in self.rec.delivered(since, kind) if self._owners(u) and match(u)), None),
            timeout=timeout, what=instruction)

    async def send(self, code: str, note: str = "") -> Update:
        """The owner sends ``code`` to the bot; the message as it reached the bot (``.at`` starts the row)."""
        return await self.expect("message", f"send the bot:  {code}" + (f"\n    {note}" if note else ""),
                                 time.time(), lambda u: _norm(u.body.get("text")) == _norm(code))

    async def reply(self, since: float, marker: str, *, timeout: float = 300.0,
                    quiet: float = 6.0) -> list[tuple[Call, dict[str, Any]]]:
        """The bot's messages since ``since`` once one shows ``marker``, then ``quiet`` s with no new call."""
        await self.rec.wait_for(lambda: any(marker in shown(m) for _, m in messages(self.rec.sent(since))),
                                timeout=timeout, what=f"the bot's message with {marker!r}")
        await self.settle(since, quiet)
        return messages(self.rec.sent(since))

    async def settle(self, since: float, quiet: float, cap: float = 90.0) -> None:
        deadline = time.monotonic() + cap
        while quiet and time.monotonic() < deadline:
            count = len(self.rec.sent(since))
            await asyncio.sleep(quiet)
            if len(self.rec.sent(since)) == count:
                return

    async def compare(self, since: float, question: str) -> bool:
        """Print what the record holds since ``since``; ask the owner whether iOS shows the same."""
        self.say("\n--- What the bot sent and what Telegram made of it:")
        for line in timeline(self.rec.sent(since), self.rec.delivered(since)):
            self.say("  " + line)
        return await self.observe(question + " Does iOS show what the record above says?")

    def named(self, sha: str) -> str:
        return next((name for name, value in self.sha.items() if value == sha), "?")


ROWS: dict[str, tuple[str, Callable[[Run], Awaitable[None]]]] = {}
PLAYBOOK: dict[str, Steps] = {}


def row(key: str, title: str, **codes: Steps) -> Callable[[Callable[[Run], Awaitable[None]]], Callable[[Run], Awaitable[None]]]:
    def register(fn: Callable[[Run], Awaitable[None]]) -> Callable[[Run], Awaitable[None]]:
        ROWS[key] = (title, fn)
        PLAYBOOK.update(codes)
        return fn
    return register


_HEAD = """# Acceptance agent

You are the agent in an acceptance run of a Telegram plugin, in the private chat with its owner. The owner
sends a row code from the list below, alone in a message or at the start of it (for example `r2`); codes are
case-insensitive. For a code, carry out its steps exactly as written, in order, with nothing added: no
greeting, no comment, no summary, no question. Text between the lines BEGIN and END is copied exactly,
without those two lines. Where a step names a tool, call that tool with those arguments. File paths are on
this computer. A step that says what to do when a message comes back applies to the next such message. For
anything else the owner writes, answer in one short sentence.
"""


def playbook(files: dict[str, Path]) -> str:
    """The agent's SOUL.md: every code and its steps."""
    parts = [_HEAD]
    for code, steps in PLAYBOOK.items():
        parts.append(f"## {code}\n\n{steps(files) if callable(steps) else steps}\n")
    return "\n".join(parts)


def _exact(text: str, done: str) -> str:
    return ("Reply with exactly the Markdown between the lines BEGIN and END, unchanged and complete, then a last "
            f"line `{done}`.\nBEGIN\n{text}\nEND")


def _files(names: list[str], done: str, *, as_files: bool = False) -> Steps:
    return lambda f: (f"Call the telegram_send tool once with files {json.dumps([str(f[n]) for n in names])}"
                      + (" and as_files true" if as_files else "") + f". Then reply `{done}`.")


# --- rows 1, 2, 16: answers ------------------------------------------------------------------------------------

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
# Longer than one rich message: Telegram takes 500 blocks in one (list items and nested blocks count), and
# a part is about 20; six parts fit in one message.
LONG_PARTS = 30
LONG = "\n\n".join(f"## Part {n} of {LONG_PARTS}\n\n{SAMPLE}" for n in range(1, LONG_PARTS + 1))
_LITERAL = re.compile(r"\*\*|__|~~|^#{1,6} |^\s*\||<u>|\$\$", re.MULTILINE)
_ELEMENTS = ("headings, bold, italic, underline, strikethrough, nested emphasis, both lists, the quote, the link, "
             "inline and block code, the formula")


@row("1", "Every formatting element, short and longer than one message",
     r1=_exact(SAMPLE, "R1 short done"),
     r1l=(f"Reply with the Markdown of r1 (between its BEGIN and END) {LONG_PARTS} times, unchanged and complete each "
          f"time, each copy under its own line `## Part <n> of {LONG_PARTS}` with n from 1 to {LONG_PARTS}, a blank "
          "line between the copies, then a last line `R1 long done`."))
async def row_1(run: Run) -> None:
    for code, size in (("r1", "short"), ("r1l", "long")):
        asked = await run.send(code)
        sent = await run.reply(asked.at, f"R1 {size} done")
        text = "\n".join(shown(m) for _, m in sent)
        run.check(f"{size}: Telegram's parse leaves no literal markup", not _LITERAL.search(text),
                  _LITERAL.findall(text)[:5])
        maths = [str(n.get("expression", "")) for _, m in sent for n in of_type(m, "mathematical_expression")]
        run.check(f"{size}: a formula whose LaTeX source is the agent's, character for character",
                  maths and all(FORMULA in s for s in maths), maths[:2])
        if size == "long":
            order = [int(n) for n in re.findall(rf"Part (\d+) of {LONG_PARTS}", text)]
            run.check(f"long: several messages, all {LONG_PARTS} parts, in order",
                      len(sent) > 1 and order == list(range(1, LONG_PARTS + 1)), f"{len(sent)} messages, parts {order}")
        await run.compare(asked.at, f"Row 1, {size} answer: is every element formatted ({_ELEMENTS}), with no "
                                    "literal markup? Take the screenshots.")


TABLE_ROWS = [["Planet", "Moons", "Day (h)", "Ring"], ["Mercury", "0", "4222.6", "no"], ["Venus", "0", "2802.0", "no"],
              ["Earth", "1", "24.0", "no"], ["Mars", "2", "24.7", "no"], ["Jupiter", "95", "9.9", "yes"],
              ["Saturn", "146", "10.7", "yes"]]
TABLE = "\n".join(["| " + " | ".join(TABLE_ROWS[0]) + " |", "|---|---|---|---|"]
                  + ["| " + " | ".join(r) + " |" for r in TABLE_ROWS[1:]])


@row("2", "A table of 4 columns and 6 rows", r2=_exact(TABLE, "R2 done"))
async def row_2(run: Run) -> None:
    asked = await run.send("r2")
    sent = await run.reply(asked.at, "R2 done")
    tables = [t for _, m in sent for t in of_type(m, "table")]
    cells = table_cells(tables[0]) if tables else []
    run.check("one native table, cell by cell equal to the source", len(tables) == 1 and cells == TABLE_ROWS, cells)
    run.check("no literal | or dashes", not re.search(r"\||---", "\n".join(shown(m) for _, m in sent)))
    await run.compare(asked.at, "Row 2: does the table show 4 columns and 6 rows, each value in its cell? Screenshot.")


SENTENCE = "Hello from the acceptance run."


@row("16", "A short plain answer is one ordinary message",
     r16=f"Reply with exactly this sentence and nothing else: {SENTENCE}")
async def row_16(run: Run) -> None:
    asked = await run.send("r16")
    sent = await run.reply(asked.at, SENTENCE)
    run.check("one message", len(sent) == 1, len(sent))
    run.check("an ordinary text message (sendMessage, no rich content) reading the sentence",
              all(c.method == "sendMessage" and not m.get("rich_message") and m.get("text", "").strip() == SENTENCE
                  for c, m in sent), [(c.method, shown(m)) for c, m in sent])
    await run.compare(asked.at, "Row 16: is it one plain message, laid out as plain messages were before the plugin?")


# --- rows 3, 4, 5, 15: files ---------------------------------------------------------------------------------

_KIND_METHOD = {"document": "sendDocument", "photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio",
                "voice": "sendVoice"}


def _carrying(sent: list[tuple[Call, dict[str, Any]]], sha: str) -> list[tuple[Call, dict[str, Any]]]:
    return [(c, m) for c, m in sent if any(u["sha256"] == sha for u in uploads(c))]


async def _served(run: Run, message: dict[str, Any], kind: str) -> str:
    """The SHA-256 of the file as Telegram serves it to the chat."""
    return S.sha256(await run.rec.download(message[kind]["file_id"]))


@row("3", "One file of each kind arrives as that kind",
     r3=_files(["document", "photo", "video", "audio", "voice"], "R3 done"))
async def row_3(run: Run) -> None:
    asked = await run.send("r3")
    sent = await run.reply(asked.at, "R3 done", quiet=8)
    for kind, method in _KIND_METHOD.items():
        found = _carrying(sent, run.sha[kind])
        run.check(f"the {kind} is uploaded once ({method}) and Telegram stored it as a {kind}",
                  len(found) == 1 and found[0][0].method == method and kind in found[0][1],
                  [(c.method, sorted(m)) for c, m in found])
        if kind == "document" and len(found) == 1 and "document" in found[0][1]:
            message = found[0][1]
            run.check("the document keeps its file name", message["document"].get("file_name") == run.files[kind].name,
                      message["document"].get("file_name"))
            run.check("the document's bytes, as Telegram serves them, are the sample's",
                      await _served(run, message, "document") == run.sha[kind])
    await run.compare(asked.at, "Row 3: does the photo show, and do the video, audio and voice play in the chat?")


def _albums(run: Run, since: float) -> list[Call]:
    return [c for c in run.rec.sent(since, {"sendMediaGroup"}) if c.ok]


# code: (the marker the agent ends with, the photos, the albums Telegram should show)
_ALBUMS = {"r4a": ("R4a done", 3, [list(range(1, 4))]), "r4b": ("R4b done", 12, [list(range(1, 11)), [11, 12]])}


@row("4", "2 to 10 photos are one album; 12 are albums of 10 and 2, in order",
     **{code: _files([f"photo-{n:02d}" for n in range(1, count + 1)], done)
        for code, (done, count, _) in _ALBUMS.items()})
async def row_4(run: Run) -> None:
    for code, (done, count, expected) in _ALBUMS.items():
        asked = await run.send(code)
        await run.reply(asked.at, done, quiet=8)
        albums = _albums(run, asked.at)
        order = [[run.named(u["sha256"]) for u in uploads(c)] for c in albums]
        want = [[f"photo-{n:02d}" for n in group] for group in expected]
        run.check(f"{count} photos: albums of {', '.join(str(len(g)) for g in want)}, in order", order == want, order)
        run.check(f"{count} photos: Telegram made each album one group of photos, in order",
                  all(isinstance(c.result, list) and all("photo" in m for m in c.result)
                      and len({m.get("media_group_id") for m in c.result}) == 1 for c in albums))
        colours = ", ".join(S.COLOUR_NAMES[n - 1] for group in expected for n in group)
        await run.compare(asked.at, f"Row 4, {count} photos: are they albums as above, in this order of colours: "
                                    f"{colours}?")


@row("5", "A photo, video, audio and voice \"as a file\" arrive as documents, unchanged",
     r5=_files(["photo", "video", "audio", "voice"], "R5 done", as_files=True))
async def row_5(run: Run) -> None:
    asked = await run.send("r5")
    sent = await run.reply(asked.at, "R5 done", quiet=8)
    for kind in ("photo", "video", "audio", "voice"):
        found = _carrying(sent, run.sha[kind])
        message = found[0][1] if len(found) == 1 else {}
        ok = (len(found) == 1 and found[0][0].method == "sendDocument" and "document" in message
              and not any(k in message for k in ("photo", "video", "audio", "voice"))
              and message["document"].get("file_name") == run.files[kind].name)
        run.check(f"the {kind}: one document with its file name, not a {kind}", ok,
                  [(c.method, sorted(m)) for c, m in found])
        if ok:
            run.check(f"the {kind}: its bytes, as Telegram serves them, are the sample's",
                      await _served(run, message, "document") == run.sha[kind])
    await run.compare(asked.at, "Row 5: are the four shown as downloadable files, not as a photo or a player?")


@row("15", "A file that cannot be delivered is named with the reason", r15=_files(["big", "empty"], "R15 done"))
async def row_15(run: Run) -> None:
    asked = await run.send("r15")
    sent = await run.reply(asked.at, "R15 done", quiet=8)
    text = "\n".join(shown(m) for _, m in sent)
    big, empty = run.files["big"].name, run.files["empty"].name
    run.check("the file over 50 MB is named, with the limit", big in text and "50" in text, text[:300])
    run.check("the empty file is named, with a reason", empty in text, text[:300])
    # The plugin refuses the file over the Bot API's limit before uploading it; the empty one is Telegram's to
    # refuse, and its reason is what the message names.
    run.check("the file over 50 MB was not uploaded", not any(u["upload"] == big for c in run.rec.sent(asked.at)
                                                             for u in uploads(c)))
    await run.compare(asked.at, "Row 15: does a message name each file and say why it did not arrive?")


# --- rows 6, 7, 8, 9, 10: the preview ---------------------------------------------------------------------------

@row("6", "The preview reads Contemplating..., then each status the agent sets", r6=(
    "1. Call telegram_status with text \"Reading the samples...\".\n2. Run `sleep 8` in the terminal.\n"
    "3. Call telegram_status with text \"Writing the answer...\".\n4. Run `sleep 8` in the terminal.\n"
    "5. Reply `R6 done`."))
async def row_6(run: Run) -> None:
    await run.pause("Row 6: start a screen recording of the chat on iOS, then press Enter.")
    asked = await run.send("r6")
    sent = await run.reply(asked.at, "R6 done")
    shown_frames = frames(run.rec.sent(asked.at))
    texts = [f.text for f in shown_frames]
    firsts = [next((i for i, t in enumerate(texts) if s in t), None)
              for s in ("Contemplating...", "Reading the samples...", "Writing the answer...")]
    run.check("the preview reads Contemplating..., then each status, in order",
              None not in firsts and firsts == sorted(firsts), firsts)
    run.check("every frame has the Stop button", shown_frames and all(f.can_stop for f in shown_frames),
              sum(not f.can_stop for f in shown_frames))
    run.check("row 8: one answer message and nothing else from the bot",
              len(sent) == 1 and "R6 done" in shown(sent[0][1]), [shown(m) for _, m in sent])
    final_done = sent[-1][0].done if sent else time.time()
    late = [f for f in shown_frames if f.at > final_done + 3]
    run.check("row 8: no frame after the final answer", not late, len(late))
    await run.compare(asked.at, "Rows 6 and 8: does the recording show Contemplating..., then each status, and after "
                                "the answer no preview left? Stop the recording and keep it.")


@row("7", "The preview shows the answer growing until the final replaces it", r7=(
    "Write the numbers from one to three hundred in English words, one per line, starting with `one`, then a "
    "last line `R7 done`."))
async def row_7(run: Run) -> None:
    await run.pause("Row 7: start a screen recording of the chat on iOS, then press Enter.")
    asked = await run.send("r7")
    sent = await run.reply(asked.at, "R7 done", timeout=420)
    partial = [f for f in frames(run.rec.sent(asked.at)) if "one" in f.text.split()]
    growing = any(len(b.text) > len(a.text) for a, b in zip(partial, partial[1:]))
    run.check("the preview held the beginning of the answer at least twice, longer the second time",
              len(partial) >= 2 and growing, [len(f.text) for f in partial][:10])
    run.check("the final answer arrived", bool(sent))
    await run.compare(asked.at, "Row 7: does the recording show the text growing in the preview before the final "
                                "answer?")


@row("9", "Stop stops the work within 5 seconds", r9=lambda f: (
    "1. Run `sleep 20` in the terminal.\n"
    f"2. Call telegram_send with files {json.dumps([str(f['stop'])])}.\n"
    "3. Run `sleep 15` in the terminal.\n4. Reply `R9 done`."))
async def row_9(run: Run) -> None:
    await run.pause("Row 9: start a screen recording of the chat on iOS, then press Enter.")
    asked = await run.send("r9")
    await run.rec.wait_for(lambda: any(f.can_stop for f in frames(run.rec.sent(asked.at))), timeout=60,
                           what="a preview with a Stop button")
    await asyncio.sleep(5)
    stop = await run.expect("stopped_message_generation", "tap Stop on the preview now.", asked.at, timeout=120)
    tapped = stop.at
    run.say("    Stop reached the bot; watching the chat for 60 s.")
    await asyncio.sleep(60)
    calls = run.rec.sent(asked.at)
    after = [m for c, m in messages(calls) if c.at >= tapped]
    run.check("a persisted Stopped message", any("stopped" in shown(m).lower() for m in after),
              [shown(m) for m in after][:4])
    run.check("neither the file nor the answer arrives in the 60 s after",
              not any(u["upload"] == run.files["stop"].name for c in calls for u in uploads(c))
              and not any("R9 done" in shown(m) for _, m in messages(calls)))
    late = [f for f in frames(calls) if f.at > tapped + 5]
    run.check("the preview stops changing within 5 s of the tap", not late, len(late))
    steps = [m for m in agent_log(run.owner_id, asked.at) if m["role"] in ("assistant", "tool")]
    last = max((m["at"] for m in steps), default=asked.at)
    run.check("the agent's last step is within 5 s of the tap", last <= tapped + 5,
              f"last step {last - tapped:+.1f} s from the tap")
    await run.compare(asked.at, "Row 9: does the recording show the preview frozen within 5 s of the tap, then "
                                "Stopped and the partial result, and nothing else for a minute?")


@row("10", "A failed answer leaves one message saying so and no preview", r10="Reply `hello`.")
async def row_10(run: Run) -> None:
    run.say("    Restarting the gateway with a model key the provider refuses...")
    await asyncio.to_thread(run.gateway.start, broken=True)
    try:
        asked = await run.send("r10")
        await run.rec.wait_for(lambda: messages(run.rec.sent(asked.at)), timeout=180, what="the bot's answer")
        await run.settle(asked.at, 20)
        sent = messages(run.rec.sent(asked.at))
        run.check("one message from the bot", len(sent) == 1, [shown(m) for _, m in sent])
        final_done = sent[-1][0].done if sent else time.time()
        run.check("no frame after it", not [f for f in frames(run.rec.sent(asked.at)) if f.at > final_done + 3])
        await run.compare(asked.at, "Row 10: one message saying the answer failed, and no preview?")
    finally:
        run.say("    Restarting the gateway normally...")
        await asyncio.to_thread(run.gateway.start)


# --- row 11 and the owner's additions: actions -----------------------------------------------------------------

@row("11", "A choice reaches the agent once; a second tap says already answered", r11=(
    "Call telegram_send with text \"Pick a city\" and three buttons Paris, Rome and Oslo with values paris, rome and "
    "oslo. Then reply `R11 offered`. When a [button] message comes back, reply `You chose ` and the city."))
async def row_11(run: Run) -> None:
    asked = await run.send("r11")
    await run.reply(asked.at, "R11 offered", quiet=3)
    first = await run.expect("callback_query", "tap Rome under \"Pick a city\".", asked.at)
    await run.reply(asked.at, "You chose", quiet=5)
    second = await run.expect("callback_query", "now tap Paris under \"Pick a city\".", first.at + 0.001)
    await asyncio.sleep(20)
    calls = run.rec.sent(asked.at)
    chosen = [shown(m) for _, m in messages(calls) if "You chose" in shown(m)]
    run.check("one answer, naming the tapped choice", len(chosen) == 1 and "Rome" in chosen[0], chosen)
    answers = [c for c in calls if c.method == "answerCallbackQuery"
               and str(c.params.get("callback_query_id")) == str(second.body.get("id"))]
    run.check("the second tap is answered Already answered",
              any("Already answered" in str(c.params.get("text", "")) for c in answers), [c.params for c in answers])
    taps = incoming(run.owner_id, asked.at, "[button]")
    run.check("the agent's log shows exactly one incoming choice, the tapped one",
              len(taps) == 1 and '"Rome"' in taps[0], taps)
    await run.compare(asked.at, "Row 11: were the three buttons shown, and did the second tap show the notice?")


@row("X1", "A poll's answer reaches the agent", x1=(
    "Call telegram_poll with question \"Capital of Italy?\" and options Paris and Rome. Then reply `X1 asked`. "
    "When a [poll] message comes back, reply `You voted ` and the option."))
async def row_x1(run: Run) -> None:
    asked = await run.send("x1")
    await run.reply(asked.at, "X1 asked", quiet=3)
    await run.expect("poll_answer", "vote Rome in the poll.", asked.at)
    sent = await run.reply(asked.at, "You voted", quiet=3)
    run.check("the agent names the option voted", any("Rome" in shown(m) for _, m in sent if "You voted" in shown(m)))
    run.check("the agent's log shows the vote", any('"Rome"' in t for t in incoming(run.owner_id, asked.at, "[poll]")))
    await run.compare(asked.at, "X1: was the poll shown with both options, and the answer after your vote?")


def _reactions_on(calls: list[Call], message_id: int) -> list[list[str]]:
    return [[str(r.get("emoji")) for r in c.params.get("reaction") or () if isinstance(r, dict)]
            for c in calls if c.ok and c.method == "setMessageReaction"
            and str(c.params.get("message_id")) == str(message_id)]


@row("X2", "Reactions both ways", x2="Call telegram_react with emoji 👍 on my message. Then reply `X2 reacted`.")
async def row_x2(run: Run) -> None:
    asked = await run.send("x2")
    await run.reply(asked.at, "X2 reacted", quiet=3)
    await run.expect("message_reaction", "react 🔥 to the bot's \"X2 reacted\" message.", asked.at,
                     lambda u: any(r.get("emoji") == "🔥" for r in u.body.get("new_reaction") or ()))
    await asyncio.sleep(20)
    set_on_mine = _reactions_on(run.rec.sent(asked.at), asked.body["message_id"])
    run.check("the bot's 👍 is on the owner's message and still the last reaction it set there",
              set_on_mine and "👍" in set_on_mine[-1], set_on_mine)
    run.check("the owner's 🔥 on the bot's answer reaches the agent",
              any("🔥" in t for t in incoming(run.owner_id, asked.at, "[reaction]")))
    await run.compare(asked.at, "X2: is 👍 on your x2 message, and did your 🔥 stay on the bot's message?")


@row("X3", "The agent finds the sticker the owner sent and sends it back", x3=(
    "Call telegram_sticker with op find and query set to the emoji of the sticker I sent you last, then "
    "telegram_sticker with op send and the first candidate's sticker_id. Then reply `X3 sent`."))
async def row_x3(run: Run) -> None:
    since = time.time()
    mine = await run.expect("message", "send the bot any sticker.", since, lambda u: bool(u.body.get("sticker")))
    run.say("    Waiting 20 s while Hermes describes it once...")
    await asyncio.sleep(20)
    asked = await run.send("x3")
    sent = await run.reply(asked.at, "X3 sent", quiet=3)
    unique = mine.body["sticker"].get("file_unique_id")
    run.check("the sticker sent back is the owner's", any((m.get("sticker") or {}).get("file_unique_id") == unique
                                                         for _, m in sent),
              [(m.get("sticker") or {}).get("file_unique_id") for _, m in sent])
    await run.compare(since, "X3: did the same sticker come back?")


@row("X4", "The agent edits, pins and deletes its own messages", x4=(
    "1. Call telegram_send with text \"X4 draft\".\n2. Call telegram_send with text \"X4 to delete\".\n"
    "3. Call telegram_message with op edit on the first message's id and text \"X4 final\".\n"
    "4. Call telegram_message with op pin on the first message's id.\n"
    "5. Call telegram_message with op delete on the second message's id.\n6. Reply `X4 done`."))
async def row_x4(run: Run) -> None:
    asked = await run.send("x4")
    sent = await run.reply(asked.at, "X4 done", quiet=5)
    calls = run.rec.sent(asked.at)
    first = next((m["message_id"] for _, m in sent if shown(m) == "X4 draft"), None)
    second = next((m["message_id"] for _, m in sent if shown(m) == "X4 to delete"), None)
    edits = [c for c in calls if c.ok and c.method.startswith("edit") and str(c.params.get("message_id")) == str(first)]
    run.check("the first message now reads X4 final (Telegram's edited message)",
              first is not None and edits and shown(edits[-1].result if isinstance(edits[-1].result, dict) else {})
              == "X4 final", [c.result for c in edits])
    run.check("it is pinned", any(c.ok and c.method == "pinChatMessage" and str(c.params.get("message_id")) == str(first)
                                  for c in calls))
    run.check("the second is deleted", second is not None and second in deleted(calls))
    await run.compare(asked.at, "X4: is X4 final pinned at the top, and X4 to delete gone?")


@row("X5", "A reply quoting the owner's words, with an effect", x5=(
    "My message starts with x5 and goes on. Call telegram_send with text \"Noted\", quote \"the plan\" and "
    "effect 🎉. Then reply `X5 done`."))
async def row_x5(run: Run) -> None:
    asked = await run.send("x5 the plan is ready")
    sent = await run.reply(asked.at, "X5 done", quiet=3)
    noted = next(((c, m) for c, m in sent if shown(m).strip() == "Noted"), None)
    reply = (noted[0].params.get("reply_parameters") or {}) if noted else {}
    run.check("it replies to the owner's message quoting \"the plan\"",
              noted is not None and str(reply.get("message_id")) == str(asked.body["message_id"])
              and reply.get("quote") == "the plan" and (noted[1].get("quote") or {}).get("text") == "the plan",
              (reply, noted[1].get("quote") if noted else None))
    run.check("Telegram applied the 🎉 effect (the id the plugin ships is real)",
              noted is not None and str(noted[1].get("effect_id")) == EFFECTS["🎉"],
              noted[1].get("effect_id") if noted else None)
    await run.compare(asked.at, "X5: does Noted quote \"the plan\" from your message, and did the 🎉 effect play?")


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


@row("12", "A diagram, a chart, an explanation and a prototype, each a widget that draws with no network",
     r12="\n".join(f"{n}. Call telegram_widget with {what}, with caption \"R12 {kind}\"."
                   for n, (kind, what) in enumerate(WIDGETS.items(), start=1)) + "\n5. Reply `R12 done`.")
async def row_12(run: Run) -> None:
    asked = await run.send("r12")
    await run.reply(asked.at, "R12 done", timeout=600, quiet=5)
    widgets = [c for c in run.rec.sent(asked.at, {"sendWidget"})]
    for kind in WIDGETS:
        mine = [c for c in widgets if c.params.get("caption") == f"R12 {kind}"]
        run.check(f"{kind}: a widget Telegram accepted", len(mine) == 1 and mine[0].ok,
                  [(c.ok, c.error) for c in mine])
        if mine:
            verdict = check_widget(str(mine[0].params.get("html", "")))
            run.check(f"{kind}: its HTML calls no network API and loads nothing from outside", verdict is None, verdict)
    await run.compare(asked.at, "Row 12: does each of the four widgets draw fully in the chat? Screenshot each.")


@row("13", "Interaction inside a widget changes it and reaches nobody")
async def row_13(run: Run) -> None:
    start = time.time()
    await run.pause("Row 13: on iOS, record the screen while you tap Next in the explanation, tap a bar of the chart "
                    "and switch the prototype's screen; then press Enter.")
    reached = [u.kind for u in run.rec.delivered(start) if u.kind in ("widget_query", "callback_query", "message")]
    run.check("Telegram delivered nothing to the bot", not reached, reached)
    run.check("the agent's log shows nothing new from the chat", not incoming(run.owner_id, start),
              incoming(run.owner_id, start))
    await run.observe("Row 13: did each widget change on each interaction?")


@row("14", "A widget action that needs the agent reaches it with its widget", r14=(
    "Call telegram_widget with a small card showing \"Step 2: compare\" and a button \"Ask Lumi\" whose click calls "
    "TelegramWidget.request(\"explain step 2\"), caption \"R14\". Then reply `R14 sent`. When a [widget] message "
    "comes back, reply naming its data and its widget id."))
async def row_14(run: Run) -> None:
    asked = await run.send("r14")
    await run.reply(asked.at, "R14 sent", quiet=3)
    widget = next((m for c, m in messages(run.rec.sent(asked.at, {"sendWidget"}))), None)
    query = await run.expect("widget_query", "tap Ask Lumi in the R14 widget.", asked.at)
    answer = await run.reply(query.at, "explain step 2", timeout=180, quiet=5)
    answered = [c for c in run.rec.sent(query.at, {"answerWidgetQuery"})
                if str(c.params.get("widget_query_id")) == str(query.body.get("id"))]
    run.check("the widget's request is answered within 15 s", answered and answered[0].ok
              and answered[0].at - query.at <= 15, [(c.ok, round(c.at - query.at, 1)) for c in answered])
    widget_id = str((widget or {}).get("message_id"))
    actions = incoming(run.owner_id, query.at, "[widget]")
    run.check("the agent's log shows one incoming action with that data and that widget",
              widget is not None and len(actions) == 1 and "explain step 2" in actions[0] and widget_id in actions[0],
              actions)
    run.check("the answer names both", widget is not None and any(
        "explain step 2" in shown(m) and widget_id in shown(m) for _, m in answer))
    await run.compare(asked.at, "Row 14: did the widget's tap bring an answer naming the request and the widget?")


# --- row 17: cron reports and approvals during a preview --------------------------------------------------------

@row("17", "A cron report and an approval during a previewed turn",
     r17a=("Create a one-time cron job that runs 2 minutes from now and delivers exactly the text `R17 cron report` "
           "to this chat. Then reply `R17 scheduled`."),
     r17b=("1. Run `rm -rf /tmp/hermes-acceptance-row-17` in the terminal.\n2. Run `sleep 90` in the terminal.\n"
           "3. Reply `R17 done`."))
async def row_17(run: Run) -> None:
    scheduled = await run.send("r17a")
    await run.reply(scheduled.at, "R17 scheduled", quiet=2)
    await run.pause("Wait about 30 s, then press Enter (the report is due 2 minutes after r17a).")
    work = await run.send("r17b")
    await run.rec.wait_for(lambda: next((m for _, m in messages(run.rec.sent(work.at)) if m.get("reply_markup")),
                                        None), timeout=120, what="the approval prompt")
    approval = next(m for _, m in messages(run.rec.sent(work.at)) if m.get("reply_markup"))
    await run.expect("callback_query", "on the approval prompt, tap the button that allows the command once.",
                     work.at)
    sent = await run.reply(work.at, "R17 done", timeout=300, quiet=5)
    calls = run.rec.sent(scheduled.at)
    report = next((c for c, m in messages(calls) if "R17 cron report" in shown(m)), None)
    run.check("the cron report arrived in the chat", report is not None)
    turn = frames(run.rec.sent(work.at))
    run.check("it arrived while the turn's preview ran", report is not None and turn
              and turn[0].at <= report.at <= turn[-1].at + 3,
              f"report {report.at - work.at:+.0f} s, frames {turn[0].at - work.at:+.0f}..{turn[-1].at - work.at:+.0f} s"
              if report and turn else "")
    run.check("the approval prompt is still there", approval["message_id"] not in deleted(calls))
    run.check("the work went on after one tap", any("R17 done" in shown(m) for _, m in sent))
    await run.compare(scheduled.at, "Row 17: did the report arrive while the preview ran, and does the approval "
                                    "prompt stay visible with its choice resolved?")
