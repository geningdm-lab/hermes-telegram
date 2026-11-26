"""Rich answers: which answers go as Telegram rich messages, the Markdown they send, and how a long answer
is cut into several.

Telegram's rich Markdown is GFM-compatible and may contain HTML (core.telegram.org/bots/api/rich-messages,
"Rich Markdown style"). Limits per message: 32768 characters including formula source, 500 blocks counting
nested blocks, list items, table rows, quotation and details blocks, 16 nesting levels, 20 table columns
("Rich Message Limits"). Telegram does not publish how it counts blocks, so the cut here uses a
conservative estimate, and the sender cuts again a part Telegram still refuses.

The order is: ``prepare_markdown`` on the whole answer, then ``split_markdown`` on the prepared text, so a
cut sees the delimiters Telegram will read. ``prepare_markdown`` is idempotent, so a part prepared again on
its way out is unchanged.

Pure functions only: no Telegram or Hermes imports.
"""

import re
import secrets
from dataclasses import dataclass, replace

MAX_CHARS = 32768
MAX_BLOCKS = 500
# Headroom under the documented limits: the block count is an estimate.
CHAR_BUDGET = 32000
BLOCK_BUDGET = 400

_FENCE_RE = re.compile(r"^(?P<prefix>(?:[ \t]*>)*[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)$")
_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S")
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>")
_RULE_RE = re.compile(r"^[ \t]{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_LINK_RE = re.compile(r"\[[^\]\n]+\]\([^)\s]+(?:[ \t]+\"[^\"]*\")?\)")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:")
_MARK_RE = re.compile(r"==[^=\s](?:[^=\n]*[^=\s])?==")
# A code span may cross a line break but not a blank line (CommonMark).
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
_HTML_CODE_RE = re.compile(r"<(code|pre)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_MATH_BLOCK_LINE_RE = re.compile(r"^[ \t]*\$\$")
_DISPLAY_MATH_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)
_BRACKET_MATH_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_PAREN_MATH_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)

# HTML the agent may write inside a rich answer (rich-messages, "Rich HTML style"). Any other "<" is text
# and is escaped, so "List<String>" or "<<marker>>" are shown as written instead of being read as a tag.
# Buttons are not here on purpose: they come from the agent's tools, which own their callback data.
_ALLOWED_TAGS = (
    "details", "summary", "u", "ins", "mark", "sub", "sup", "s", "del", "strike", "b", "strong", "i", "em",
    "br", "cite", "aside", "footer", "code", "pre", "tg-spoiler", "tg-math", "tg-math-block", "tg-time",
    "tg-emoji",
)
_ALLOWED_TAG_RE = re.compile(
    r"</?(?:" + "|".join(re.escape(t) for t in _ALLOWED_TAGS) + r")(?=[\s/>])[^<>]*>", re.IGNORECASE)
_AUTOLINK_RE = re.compile(r"<(?:https?://|mailto:|tel:|tg://)[^\s<>]+>", re.IGNORECASE)


def needs_rich(text: str) -> bool:
    """True when the answer has structure a rich message shows natively and an ordinary message does not.

    Structure: headings, lists (task lists included), quotes, tables, code, links, footnotes, rules, maths,
    highlights and the HTML of ``_ALLOWED_TAGS``. Emphasis alone does not count: the ordinary message path
    already shows bold, italic, strikethrough and spoilers, and a plain short answer stays an ordinary
    message (row 16).
    """
    if not text or not text.strip():
        return False
    for line in text.splitlines():
        if (_HEADING_RE.match(line) or _LIST_ITEM_RE.match(line) or _QUOTE_RE.match(line)
                or _FENCE_RE.match(line) or _TABLE_SEPARATOR_RE.match(line) or _RULE_RE.match(line)
                or _MATH_BLOCK_LINE_RE.match(line)):
            return True
    if (_LINK_RE.search(text) or _FOOTNOTE_REF_RE.search(text) or _INLINE_CODE_RE.search(text)
            or _MARK_RE.search(text) or _ALLOWED_TAG_RE.search(text)):
        return True
    if "$$" in text or re.search(r"\\\(|\\\[", text):
        return True
    return _has_inline_math(text)


# --- preparing the Markdown --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    text: str
    is_code: bool


def _closes(match: re.Match[str] | None, fence: str) -> bool:
    return bool(match and match.group("fence")[0] == fence[0] and len(match.group("fence")) >= len(fence)
                and not match.group("info").strip())


_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")


def _segments(text: str) -> list[_Segment]:
    """``text`` cut into code (``is_code``) and the prose between them, losslessly.

    Code is a fenced block, or an indented code block: lines indented by four spaces or a tab after a
    blank line, outside a list (inside a list that indentation continues the item, CommonMark 5.2)."""
    lines = text.splitlines(keepends=True)
    out: list[_Segment] = []
    prose: list[str] = []

    def flush() -> None:
        if prose:
            out.append(_Segment("".join(prose), False))
            prose.clear()

    i, prev_blank, in_list = 0, True, False
    while i < len(lines):
        bare = lines[i].rstrip("\r\n")
        opening = _FENCE_RE.match(bare)
        if opening is not None:
            j = i + 1
            while j < len(lines) and not _closes(_FENCE_RE.match(lines[j].rstrip("\r\n")), opening.group("fence")):
                j += 1
            flush()
            out.append(_Segment("".join(lines[i:j + 1]), True))  # an unclosed fence runs to the end, as in GFM
            i, prev_blank = j + 1, False
            continue
        if prev_blank and not in_list and bare.strip() and _INDENTED_CODE_RE.match(bare):
            j = i
            while j < len(lines) and (not lines[j].strip() or _INDENTED_CODE_RE.match(lines[j])):
                j += 1
            while j > i and not lines[j - 1].strip():
                j -= 1  # trailing blank lines are not part of the block
            flush()
            out.append(_Segment("".join(lines[i:j]), True))
            i = j
            continue
        if bare.strip():
            if _LIST_ITEM_RE.match(bare):
                in_list = True
            elif not bare.startswith((" ", "\t")):
                in_list = False
        prev_blank = not bare.strip()
        prose.append(lines[i])
        i += 1
    flush()
    return out


class _Masks:
    """Verbatim pieces set aside while prose is rewritten, behind placeholders no input can contain."""

    def __init__(self) -> None:
        self._token = secrets.token_hex(8)
        self._store: list[str] = []

    def put(self, piece: str) -> str:
        self._store.append(piece)
        return f"\x00{self._token}:{len(self._store) - 1}\x00"

    def mask(self, text: str, pattern: re.Pattern[str]) -> str:
        return pattern.sub(lambda m: self.put(m.group(0)), text)

    def restore(self, text: str) -> str:
        # One pass per nesting level: a stored piece may hold placeholders stored before it.
        pattern = re.compile(f"\x00{self._token}:(\\d+)\x00")
        while pattern.search(text):
            text = pattern.sub(lambda m: self._store[int(m.group(1))], text)
        return text


def _is_closing_dollar(text: str, i: int) -> bool:
    before = text[i - 1] if i > 0 else " "
    after = text[i + 1] if i + 1 < len(text) else " "
    return not before.isspace() and after != "$" and not after.isdigit()


def _inline_math_spans(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    """Inline ``$...$`` spans and the stray ``$`` signs of ``text`` (no code, no ``$$`` in it).

    A span follows Pandoc's rule, which keeps prices apart from maths: the opening ``$`` has a non-space
    character right after it, the closing one a non-space character right before it and no digit right
    after it, and the span does not cross a blank line. Stricter than Pandoc, the first ``$`` after an
    opening one must close it, so a span never contains a ``$``: "$5 and $10, $x$" has the one span "$x$",
    where Pandoc would read "5 and $10, $x" as maths. An escaped ``\\$`` is neither.
    """
    spans: list[tuple[int, int]] = []
    stray: list[int] = []
    i = 0
    while i < len(text):
        if text[i] != "$" or (i > 0 and text[i - 1] == "\\"):
            i += 1
            continue
        end = -1
        if i + 1 < len(text) and not text[i + 1].isspace():
            limit = text.find("\n\n", i)
            limit = len(text) if limit == -1 else limit
            for j in range(i + 1, limit):
                if text[j] == "$" and text[j - 1] != "\\":
                    end = j if _is_closing_dollar(text, j) else -1
                    break
        if end == -1:
            stray.append(i)
            i += 1
        else:
            spans.append((i, end))
            i = end + 1
    return spans, stray


def _has_inline_math(text: str) -> bool:
    for segment in _segments(text):
        if not segment.is_code:
            prose = _DISPLAY_MATH_RE.sub("", _INLINE_CODE_RE.sub("", segment.text))
            if _inline_math_spans(prose)[0]:
                return True
    return False


def _protect_inline_maths(text: str, masks: _Masks) -> str:
    """Inline maths set aside whole; each stray ``$`` escaped."""
    spans, stray = _inline_math_spans(text)
    events = sorted([(s, e, "span") for s, e in spans] + [(i, i, "stray") for i in stray])
    out, pos = [], 0
    for start, end, kind in events:
        out.append(text[pos:start])
        out.append(masks.put(text[start:end + 1]) if kind == "span" else "\\$")
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def _table_lines(lines: list[str]) -> set[int]:
    """Indexes of the lines of pipe tables: a separator row and the pipe lines around it."""
    found: set[int] = set()
    for i, line in enumerate(lines):
        if i > 0 and _TABLE_SEPARATOR_RE.match(line) and "|" in lines[i - 1]:
            found.update((i - 1, i))
            j = i + 1
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                found.add(j)
                j += 1
    return found


def _hard_breaks(text: str) -> str:
    """A lone line break inside a paragraph becomes a hard break (two spaces), as Telegram shows the
    agent's line breaks only that way; table rows and lines that already end with one are left alone.
    Only prose reaches here, with code and maths behind placeholders that hold no line break."""
    lines = text.split("\n")
    tables = _table_lines(lines)
    for i in range(len(lines) - 1):
        line, nxt = lines[i], lines[i + 1]
        if (not line.strip() or not nxt.strip() or i in tables or i + 1 in tables
                or line.endswith(("  ", "\\"))):
            continue
        lines[i] = line + "  "
    return "\n".join(lines)


def _prepare_prose(text: str, masks: _Masks) -> str:
    text = masks.mask(text, _INLINE_CODE_RE)
    text = masks.mask(text, _HTML_CODE_RE)
    # Formulas the agent wrote with dollars first, so nothing inside them is rewritten.
    text = masks.mask(text, _DISPLAY_MATH_RE)
    text = _protect_inline_maths(text, masks)
    # LaTeX delimiters models often write, turned into the ones Telegram reads, and set aside whole.
    text = _BRACKET_MATH_RE.sub(lambda m: masks.put("$$" + m.group(1).strip() + "$$"), text)
    text = _PAREN_MATH_RE.sub(lambda m: masks.put("$" + m.group(1).strip() + "$"), text)
    text = masks.mask(text, _ALLOWED_TAG_RE)
    text = masks.mask(text, _AUTOLINK_RE)
    text = re.sub(r"(?<!\\)<", lambda _m: "\\<", text)
    return _hard_breaks(text)


def prepare_markdown(text: str) -> str:
    """The agent's Markdown as Telegram's rich Markdown should read it.

    Outside code (fenced blocks, code spans, ``<code>`` and ``<pre>``) and maths: ``\\(...\\)`` and
    ``\\[...\\]`` become ``$...$`` and ``$$...$$``; a ``$`` that opens no maths becomes ``\\$``; a ``<``
    that starts none of the allowed tags or an autolink becomes ``\\<``; a line break inside a paragraph
    becomes a hard break. Code and formula source are left exactly as written. Idempotent.
    """
    masks = _Masks()
    prepared = "".join(s.text if s.is_code else _prepare_prose(s.text, masks) for s in _segments(text))
    return masks.restore(prepared)


def _unescape_prose(text: str, escapes: tuple[str, ...]) -> str:
    masks = _Masks()
    out = []
    for segment in _segments(text):
        if segment.is_code:
            out.append(segment.text)
            continue
        prose = masks.mask(masks.mask(segment.text, _INLINE_CODE_RE), _HTML_CODE_RE)
        for escape in escapes:
            prose = prose.replace("\\" + escape, escape)
        out.append(prose)
    return masks.restore("".join(out))


def unescape_dollars(text: str) -> str:
    """``\\$`` written as ``$`` outside code, for the ordinary message path, which shows text as written.

    The hint tells the agent to escape a literal dollar; only rich Markdown reads that escape."""
    return _unescape_prose(text, ("$",))


def plain_source(prepared: str) -> str:
    """Prepared rich Markdown with the escapes preparation adds (``\\$``, ``\\<``) taken out again, for a
    part that has to go by the ordinary MarkdownV2 path after all."""
    return _unescape_prose(prepared, ("$", "<"))


# --- cutting a long answer -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    text: str  # with its line ending
    blocks: int  # estimated blocks this line adds
    fence_open: str = ""  # the opening line of the fenced block this line is inside, "" outside
    opens_fence: bool = False
    table_header: tuple[str, ...] = ()  # header and separator of the table this row is in, () outside
    no_cut_before: bool = False  # a cut just before this line would break its construct
    continues_item: bool = False  # an indented line that belongs to the list item above it


def _classify(text: str) -> list[_Line]:
    raw = text.splitlines(keepends=True)
    lines: list[_Line] = []
    fence, fence_line = "", ""
    table: tuple[str, ...] = ()
    prev_blank, in_math, details, in_list = True, False, 0, False
    for i, line in enumerate(raw):
        bare = line.rstrip("\r\n")
        match = _FENCE_RE.match(bare)
        if fence:
            lines.append(_Line(line, 0, fence_open=fence_line))
            if _closes(match, fence):
                fence, fence_line = "", ""
            prev_blank = False
            continue
        inside = in_math or details > 0
        indented = bool(re.match(r"^(?: {2,}|\t)\S", bare))
        continues = in_list and indented
        if match:
            fence, fence_line = match.group("fence"), line
            table = ()
            lines.append(_Line(line, 1, fence_open=line, opens_fence=True, no_cut_before=inside,
                               continues_item=continues))
            prev_blank = False
            continue
        opened = len(re.findall(r"<details\b", bare, re.IGNORECASE))
        details = max(details + opened - len(re.findall(r"</details>", bare, re.IGNORECASE)), 0)
        if not bare.strip():
            table = ()
            lines.append(_Line(line, 0, no_cut_before=inside))
            prev_blank = True
            continue
        if _MATH_BLOCK_LINE_RE.match(bare) or (in_math and bare.strip().endswith("$$")):
            stripped = bare.strip()
            one_line = stripped != "$$" and stripped.startswith("$$") and stripped.endswith("$$")
            lines.append(_Line(line, 0 if in_math else 1, no_cut_before=inside, continues_item=continues))
            if not one_line:
                in_math = not in_math
            prev_blank = False
            continue
        if in_math:
            lines.append(_Line(line, 0, no_cut_before=True))
            continue
        nxt = raw[i + 1].rstrip("\r\n") if i + 1 < len(raw) else ""
        if not table and "|" in bare and _TABLE_SEPARATOR_RE.match(nxt):
            table = (line, raw[i + 1])
            lines.append(_Line(line, 2, no_cut_before=inside))  # the table and its header row
        elif table and _TABLE_SEPARATOR_RE.match(bare):
            lines.append(_Line(line, 0, table_header=table, no_cut_before=True))
        elif table and "|" in bare:
            lines.append(_Line(line, 1, table_header=table, no_cut_before=inside))
        else:
            table = ()
            item = _LIST_ITEM_RE.match(bare)
            if item:
                blocks = 2 + len(item.group("indent").expandtabs(4)) // 2  # item, paragraph, nesting
                in_list = True
            elif _QUOTE_RE.match(bare):
                blocks = 1 + bare.lstrip().count(">")
                in_list = False
            else:
                blocks = 1 if prev_blank or _HEADING_RE.match(bare) else 0
                in_list = in_list and indented
            lines.append(_Line(line, blocks, no_cut_before=inside, continues_item=continues))
        prev_blank = False
    return lines


def _cut_priority(lines: list[_Line], i: int) -> int:
    """How good a cut is just before ``lines[i]``: 3 after a blank line, 2 before a table row, top-level
    list item, quote line or heading, 1 inside a paragraph or inside a list item (an indented
    continuation or nested item), 0 inside a fenced block (closed and reopened around the cut), -1 where
    the cut would break a construct (header from separator, maths block, details block)."""
    prev, cur = lines[i - 1], lines[i]
    if cur.fence_open and not cur.opens_fence:
        return 0
    if cur.no_cut_before:
        return -1
    if cur.continues_item:
        return 1
    if not prev.text.strip():
        return 3
    bare = cur.text.rstrip("\r\n")
    if cur.table_header or _LIST_ITEM_RE.match(bare) or _QUOTE_RE.match(bare) or _HEADING_RE.match(bare):
        return 2
    return 1


def estimate_blocks(text: str) -> int:
    """Upper-bound estimate of the blocks Telegram counts for ``text``."""
    return sum(line.blocks for line in _classify(text))


def _split_long_line(line: str, char_budget: int, *, verbatim: bool) -> list[str]:
    """``line`` in pieces of at most ``char_budget`` characters, cut at spaces where possible.

    ``verbatim`` (code) keeps every character: no space is dropped at a cut."""
    char_budget = max(1, char_budget)
    ending = line[len(line.rstrip("\r\n")):]
    body = line[: len(line) - len(ending)]
    pieces = []
    while len(body) > char_budget:
        cut = body.rfind(" ", 0, char_budget)
        cut = cut if cut > char_budget // 2 else char_budget
        pieces.append(body[:cut] + "\n")
        body = body[cut:] if verbatim else body[cut:].lstrip(" ")
    pieces.append(body + ending)
    return pieces


def _closing_fence(fence_open: str) -> str:
    fence = _FENCE_RE.match(fence_open.rstrip("\r\n"))
    assert fence is not None  # fence_open is always a line _FENCE_RE matched
    return fence.group("prefix") + fence.group("fence") + "\n"


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _footnote_definitions(text: str) -> dict[str, str]:
    """Footnote definitions outside code, each with its indented continuation lines, by id."""
    out: dict[str, str] = {}
    for segment in _segments(text):
        if segment.is_code:
            continue
        lines = segment.text.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            found = _FOOTNOTE_DEF_RE.match(lines[i])
            if not found:
                i += 1
                continue
            j = i + 1
            while j < len(lines) and lines[j].strip() and lines[j].startswith((" ", "\t")):
                j += 1
            out.setdefault(found.group(1), _with_newline("".join(lines[i:j])))
            i = j
    return out


def split_markdown(text: str, char_budget: int = CHAR_BUDGET, block_budget: int = BLOCK_BUDGET) -> list[str]:
    """Cut prepared Markdown into parts that each fit one rich message, in order.

    A cut prefers a blank line, then a boundary before a table row, list item, quote line or heading, then
    any line boundary, and goes inside a fenced block only when nothing else fits: the block is then closed
    at the end of the part and reopened with its info string at the start of the next. A part that starts
    inside a table repeats the table's header when it still fits beside the next row. A construct (a maths
    or details block, a single long line) over the budget but within Telegram's limit is kept whole. A part
    whose footnote references are defined in another part gets a copy of those definitions, and the budget
    leaves room for them. A line over Telegram's limit is cut at spaces.
    """
    if len(text) <= char_budget and estimate_blocks(text) <= block_budget:
        return [text]
    definitions = _footnote_definitions(text) if _FOOTNOTE_REF_RE.search(text) else {}
    char_budget -= min(sum(len(d) for d in definitions.values()), char_budget // 4)
    lines = _classify(text)
    parts: list[str] = []
    start, carry = 0, ""
    while start < len(lines):
        line = lines[start]
        if carry and line.fence_open and not line.opens_fence and carry == _with_newline(line.fence_open):
            opening = _FENCE_RE.match(line.fence_open.rstrip("\r\n"))
            if opening and _closes(_FENCE_RE.match(line.text.rstrip("\r\n")), opening.group("fence")):
                # The reopened block would close at once: the part before already closed it.
                carry, start = "", start + 1
                continue
        if carry and not _FENCE_RE.match(carry.splitlines()[0]) and len(carry) + len(line.text) > char_budget:
            # A repeated table header that would leave no room for the row: repeat it no more, keep making
            # progress. (A reopened fence is never dropped: the code after it needs it.)
            carry = ""
        if len(carry) + len(line.text) > char_budget:
            in_code = bool(line.fence_open) and not line.opens_fence
            closing = _closing_fence(line.fence_open) if in_code else ""
            if len(carry) + len(line.text) + len(closing) <= MAX_CHARS:
                # Over the working budget but within Telegram's limit: one part of its own, whole.
                parts.append(carry + line.text + closing)
                carry = _with_newline(line.fence_open) if in_code else ""
                start += 1
                continue
            # Over Telegram's limit: cut the line into pieces that fit.
            first, *rest = _split_long_line(line.text, char_budget - len(carry) - len(closing), verbatim=in_code)
            parts.append(carry + first + closing)
            lines[start:start + 1] = [replace(line, text=piece, blocks=0, opens_fence=False, no_cut_before=False)
                                      for piece in rest]
            carry = _with_newline(line.fence_open) if in_code else ""
            continue
        size, blocks, end = len(carry), estimate_blocks(carry), start
        while end < len(lines):
            line = lines[end]
            if end > start and (size + len(line.text) > char_budget or blocks + line.blocks > block_budget):
                break
            size, blocks, end = size + len(line.text), blocks + line.blocks, end + 1
        if end == len(lines):
            parts.append(carry + "".join(line.text for line in lines[start:end]))
            break
        if end == start + 1 and lines[start].opens_fence:
            # The part would hold only a fence's opening line: carry it into the next part instead of
            # sending an empty code block.
            carry += lines[start].text
            start += 1
            continue
        cut = max(range(start + 1, end + 1), key=lambda i: (_cut_priority(lines, i), i))
        if _cut_priority(lines, cut) < 0:
            # Every cut would break a construct: keep it whole if it fits Telegram's own limits.
            j = end
            while j < len(lines) and _cut_priority(lines, j) < 0:
                size, blocks, j = size + len(lines[j].text), blocks + lines[j].blocks, j + 1
            if size <= MAX_CHARS and blocks <= MAX_BLOCKS:
                cut = j
            if cut == len(lines):
                parts.append(carry + "".join(line.text for line in lines[start:]))
                break
        body = carry + "".join(line.text for line in lines[start:cut])
        nxt, carry = lines[cut], ""
        if nxt.fence_open and not nxt.opens_fence:
            body = _with_newline(body) + _closing_fence(nxt.fence_open)
            carry = _with_newline(nxt.fence_open)
        elif nxt.table_header:
            carry = "".join(nxt.table_header)
        parts.append(body)
        start = cut
    return _carry_footnotes([part for part in parts if part.strip()], definitions)


def _carry_footnotes(parts: list[str], definitions: dict[str, str]) -> list[str]:
    """Each part gets copies of the footnote definitions its references need but another part holds."""
    if len(parts) < 2 or not definitions:
        return parts
    out = []
    for part in parts:
        defined = set(_footnote_definitions(part))
        needed = [i for i in dict.fromkeys(_FOOTNOTE_REF_RE.findall(part)) if i in definitions and i not in defined]
        extra = "".join(definitions[i] for i in needed)
        out.append(_with_newline(part) + "\n" + extra if extra and len(part) + len(extra) + 2 <= MAX_CHARS
                   else part)
    return out
