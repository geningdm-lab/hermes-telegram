"""Rich answers: which answers go as Telegram rich messages, the Markdown they send, and how a long answer
is cut into several.

Telegram's rich Markdown is GFM-compatible and may contain HTML (core.telegram.org/bots/api/rich-messages,
"Rich Markdown style"). Limits per message: 32768 characters including formula source, 500 blocks counting
nested blocks, list items, table rows, quotation and details blocks, 16 nesting levels, 20 table columns
("Rich Message Limits"). Telegram does not publish how it counts blocks, so the cut here uses a
conservative estimate, and the sender cuts again a part Telegram still refuses.

Pure functions only: no Telegram or Hermes imports.
"""

import re
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
_FOOTNOTE_RE = re.compile(r"\[\^[^\]\s]+\]")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
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
    and the HTML of ``_ALLOWED_TAGS``. Emphasis alone does not count: the ordinary message path already
    shows bold, italic and strikethrough, and a plain short answer stays an ordinary message (row 16).
    """
    if not text or not text.strip():
        return False
    for line in text.splitlines():
        if (_HEADING_RE.match(line) or _LIST_ITEM_RE.match(line) or _QUOTE_RE.match(line)
                or _FENCE_RE.match(line) or _TABLE_SEPARATOR_RE.match(line) or _RULE_RE.match(line)
                or _MATH_BLOCK_LINE_RE.match(line)):
            return True
    if _LINK_RE.search(text) or _FOOTNOTE_RE.search(text) or _INLINE_CODE_RE.search(text):
        return True
    if _ALLOWED_TAG_RE.search(text) or "$$" in text or re.search(r"\\\(|\\\[", text):
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


def _segments(text: str) -> list[_Segment]:
    """``text`` cut into fenced code blocks (``is_code``) and the prose between them, losslessly."""
    lines = text.splitlines(keepends=True)
    out: list[_Segment] = []
    prose: list[str] = []
    i = 0
    while i < len(lines):
        opening = _FENCE_RE.match(lines[i].rstrip("\r\n"))
        if opening is None:
            prose.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not _closes(_FENCE_RE.match(lines[j].rstrip("\r\n")), opening.group("fence")):
            j += 1
        if prose:
            out.append(_Segment("".join(prose), False))
            prose = []
        out.append(_Segment("".join(lines[i:j + 1]), True))  # an unclosed fence runs to the end, as in GFM
        i = j + 1
    if prose:
        out.append(_Segment("".join(prose), False))
    return out


def _mask(text: str, pattern: re.Pattern[str], store: list[str]) -> str:
    def keep(match: re.Match[str]) -> str:
        store.append(match.group(0))
        return f"\x00{len(store) - 1}\x00"
    return pattern.sub(keep, text)


def _unmask(text: str, store: list[str]) -> str:
    # Masks can nest (a maths span stored after a code span was masked), so undo until none is left.
    while "\x00" in text:
        text = re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], text)
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
    where Pandoc would read "5 and $10, $x" as maths.
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


def _escape_stray_dollars(text: str) -> str:
    """``text`` with each ``$`` that opens no inline maths escaped; maths spans are kept as written."""
    _, stray = _inline_math_spans(text)
    for i in reversed(stray):
        text = text[:i] + "\\" + text[i:]
    return text


def _prepare_prose(text: str) -> str:
    store: list[str] = []
    text = _mask(text, _INLINE_CODE_RE, store)
    # LaTeX delimiters models often write, turned into the ones Telegram reads.
    text = _BRACKET_MATH_RE.sub(lambda m: "$$" + m.group(1).strip() + "$$", text)
    text = _PAREN_MATH_RE.sub(lambda m: "$" + m.group(1).strip() + "$", text)
    text = _mask(text, _DISPLAY_MATH_RE, store)
    text = _escape_stray_dollars(text)
    text = _mask(text, _ALLOWED_TAG_RE, store)
    text = _mask(text, _AUTOLINK_RE, store)
    text = re.sub(r"(?<!\\)<", lambda _m: "\\<", text)
    return _unmask(text, store)


def prepare_markdown(text: str) -> str:
    """The agent's Markdown as Telegram's rich Markdown should read it.

    Outside code: ``\\(...\\)`` and ``\\[...\\]`` become ``$...$`` and ``$$...$$``; a ``$`` that opens no
    maths becomes ``\\$``; a ``<`` that starts none of the allowed tags or an autolink becomes ``\\<``.
    Code blocks and code spans are left exactly as written.
    """
    return "".join(s.text if s.is_code else _prepare_prose(s.text) for s in _segments(text))


# --- cutting a long answer -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    text: str  # with its line ending
    blocks: int  # estimated blocks this line adds
    fence_open: str = ""  # the opening line of the fenced block this line is inside, "" outside
    opens_fence: bool = False
    table_header: tuple[str, ...] = ()  # header and separator of the table this row is in, () outside
    no_cut_before: bool = False  # a cut just before this line would break its construct


def _classify(text: str) -> list[_Line]:
    raw = text.splitlines(keepends=True)
    lines: list[_Line] = []
    fence, fence_line = "", ""
    table: tuple[str, ...] = ()
    prev_blank, in_math, details = True, False, 0
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
        if match:
            fence, fence_line = match.group("fence"), line
            table = ()
            lines.append(_Line(line, 1, fence_open=line, opens_fence=True, no_cut_before=inside))
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
            lines.append(_Line(line, 0 if in_math else 1, no_cut_before=inside))
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
            elif _QUOTE_RE.match(bare):
                blocks = 1 + bare.lstrip().count(">")
            else:
                blocks = 1 if prev_blank or _HEADING_RE.match(bare) else 0
            lines.append(_Line(line, blocks, no_cut_before=inside))
        prev_blank = False
    return lines


def _cut_priority(lines: list[_Line], i: int) -> int:
    """How good a cut is just before ``lines[i]``: 3 after a blank line, 2 before a table row, list item,
    quote line or heading, 1 inside a paragraph, 0 inside a fenced block (closed and reopened around the
    cut), -1 where the cut would break a construct (header from separator, maths block, details block)."""
    prev, cur = lines[i - 1], lines[i]
    if cur.fence_open and not cur.opens_fence:
        return 0
    if cur.no_cut_before:
        return -1
    if not prev.text.strip():
        return 3
    bare = cur.text.rstrip("\r\n")
    if cur.table_header or _LIST_ITEM_RE.match(bare) or _QUOTE_RE.match(bare) or _HEADING_RE.match(bare):
        return 2
    return 1


def estimate_blocks(text: str) -> int:
    """Upper-bound estimate of the blocks Telegram counts for ``text``."""
    return sum(line.blocks for line in _classify(text))


def _split_long_line(line: str, char_budget: int) -> list[str]:
    """``line`` in pieces of at most ``char_budget`` characters, cut at spaces where possible."""
    ending = line[len(line.rstrip("\r\n")):]
    body = line[: len(line) - len(ending)]
    pieces = []
    while len(body) > char_budget:
        cut = body.rfind(" ", 0, char_budget)
        cut = cut if cut > char_budget // 2 else char_budget
        pieces.append(body[:cut] + "\n")
        body = body[cut:].lstrip(" ")
    pieces.append(body + ending)
    return pieces


def split_markdown(text: str, char_budget: int = CHAR_BUDGET, block_budget: int = BLOCK_BUDGET) -> list[str]:
    """Cut ``text`` into parts that each fit one rich message, in order.

    A cut prefers a blank line, then a boundary before a table row, list item, quote line or heading, then
    any line boundary, and goes inside a fenced block only when nothing else fits: the block is then closed
    at the end of the part and reopened with its info string at the start of the next. A part that starts
    inside a table repeats the table's header. Nothing is dropped; the only text added is those repeated
    fence and header lines. A single line longer than the budget is cut at spaces.
    """
    if len(text) <= char_budget and estimate_blocks(text) <= block_budget:
        return [text]
    lines = _classify(text)
    parts: list[str] = []
    start, carry = 0, ""
    while start < len(lines):
        if len(carry) + len(lines[start].text) > char_budget:
            # One line alone is over the budget: cut it into pieces that fit.
            first, *rest = _split_long_line(lines[start].text, char_budget - len(carry))
            parts.append(carry + first)
            lines[start:start + 1] = [replace(lines[start], text=piece, blocks=0, opens_fence=False,
                                              no_cut_before=False) for piece in rest]
            carry = ""
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
        cut = max(range(start + 1, end + 1), key=lambda i: (_cut_priority(lines, i), i))
        body = carry + "".join(line.text for line in lines[start:cut])
        nxt, carry = lines[cut], ""
        if nxt.fence_open and not nxt.opens_fence:
            fence = _FENCE_RE.match(nxt.fence_open.rstrip("\r\n"))
            assert fence is not None  # fence_open is always a line _FENCE_RE matched
            closing = fence.group("prefix") + fence.group("fence") + "\n"
            body = (body if body.endswith("\n") else body + "\n") + closing
            carry = nxt.fence_open if nxt.fence_open.endswith("\n") else nxt.fence_open + "\n"
        elif nxt.table_header:
            carry = "".join(nxt.table_header)
        parts.append(body)
        start = cut
    return [part for part in parts if part.strip()]
