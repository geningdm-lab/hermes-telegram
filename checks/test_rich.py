"""Which answers go rich, the Markdown they send, and how a long one is cut (hermes_telegram/rich.py).

Expected values come from the rules in docs/design.md (Answers) and Telegram's documented limits, not from
runs of the code.
"""

import re

import pytest

from hermes_telegram import rich


@pytest.mark.parametrize(("text", "expected"), [
    ("Hello, how are you?", False),  # row 16: plain stays an ordinary message
    ("**bold** and *italic* only", False),  # emphasis alone: the ordinary path shows it
    ("costs $5 and $10", False),  # prices are not maths
    ("<<A-dm-1>> hello back (v1.2) - ok! #1 = {x} <<end-A-dm-1>>", False),  # Hermes' contract reply text
    ("# Title\ntext", True),
    ("- one\n- two", True),
    ("1. one\n2. two", True),
    ("> quoted", True),
    ("| a | b |\n|---|---|\n| 1 | 2 |", True),
    ("see [docs](https://example.org)", True),
    ("run `ls`", True),
    ("energy $E=mc^2$ here", True),
    ("$$E=mc^2$$", True),
    ("<u>under</u>", True),
])
def test_needs_rich(text: str, expected: bool):
    assert rich.needs_rich(text) is expected


@pytest.mark.parametrize(("text", "expected"), [
    ("between $5 and $10", r"between \$5 and \$10"),
    ("$5 and $10, $x^2$", r"\$5 and \$10, $x^2$"),  # the first $ after an opener must close it
    ("price $20,000 and $30,000", r"price \$20,000 and \$30,000"),
    ("$x^2$ and $y$", "$x^2$ and $y$"),
    (r"inline \(a+b\) here", "inline $a+b$ here"),
    (r"display \[\int x\,dx\]", r"display $$\int x\,dx$$"),
    ("$$a < b$$", "$$a < b$$"),  # maths is left as written
    ("a \\$5 b", "a \\$5 b"),  # already escaped
    ("`$5 <b>`", "`$5 <b>`"),  # code span untouched
    ("```\n$5 <x>\n```\n", "```\n$5 <x>\n```\n"),  # code block untouched
    ("List<String>", r"List\<String>"),
    ("<<marker>>", r"\<\<marker>>"),
    ("<u>x</u> <tg-spoiler>s</tg-spoiler>", "<u>x</u> <tg-spoiler>s</tg-spoiler>"),
    ("<https://t.me/x>", "<https://t.me/x>"),
    ('<tg-button callback="x">b</tg-button>', r'\<tg-button callback="x">b\</tg-button>'),  # tools own buttons
])
def test_prepare_markdown(text: str, expected: str):
    assert rich.prepare_markdown(text) == expected


def _words(text: str) -> list[str]:
    return re.findall(r"w\d{5}", text)


def test_short_answer_is_one_part_unchanged():
    text = "# Title\n\nsome text\n"
    assert rich.split_markdown(text) == [text]


def test_paragraphs_are_cut_at_blank_lines_and_rejoin_exactly():
    paragraphs = [" ".join(f"w{i * 100 + j:05d}" for j in range(140)) for i in range(40)]  # ~33.6k chars
    text = "\n\n".join(paragraphs) + "\n"
    parts = rich.split_markdown(text, char_budget=10000)
    assert len(parts) >= 4
    assert all(len(p) <= 10000 for p in parts)
    assert "".join(parts) == text  # cut only at blank lines: nothing added or lost


def test_a_long_table_repeats_its_header_in_every_part():
    header = "| n | name |\n|---|---|\n"
    rows = "".join(f"| {i} | w{i:05d} |\n" for i in range(900))
    parts = rich.split_markdown(header + rows, block_budget=400)
    assert len(parts) >= 3
    assert all(p.startswith(header) for p in parts)
    assert [w for p in parts for w in _words(p)] == [f"w{i:05d}" for i in range(900)]
    assert all(rich.estimate_blocks(p) <= 400 for p in parts)


def test_a_long_code_block_is_closed_and_reopened_with_its_info_string():
    code = "".join(f"print('w{i:05d}')\n" for i in range(3000))
    parts = rich.split_markdown("```python\n" + code + "```\n", char_budget=20000)
    assert len(parts) >= 3
    for part in parts:
        lines = part.splitlines()
        assert lines[0] == "```python" and lines[-1] == "```"
        assert len(part) <= 20000 + len("```\n")
    assert [w for p in parts for w in _words(p)] == [f"w{i:05d}" for i in range(3000)]


def test_a_blank_line_is_preferred_to_a_cut_inside_a_list():
    intro = "w00000 " * 1200 + "\n\n"  # ~8.4k chars, then a list that would overflow
    items = "".join(f"- w{i:05d} item\n" for i in range(1, 400))
    parts = rich.split_markdown(intro + items, char_budget=9000)
    assert parts[0] == intro


def test_a_maths_block_is_never_cut():
    # No blank line before the block: a cut inside it would be the latest equal-priority line boundary.
    intro = "w00001 text\n"
    block = "$$\n" + "".join(f"x_{{{i}}} +\n" for i in range(50)) + "1\n$$\n"
    text = intro + block + "w00002 after\n"
    parts = rich.split_markdown(text, char_budget=len(block) + 5)  # the block fits alone, not after the intro
    assert len(parts) >= 2
    assert all(part.count("$$") % 2 == 0 for part in parts)
    assert any(block in part for part in parts)


def test_a_table_header_is_never_cut_from_its_separator():
    intro = "w00001 " * 10 + "\n"
    table = "| a | b |\n|---|---|\n" + "".join(f"| {i} | w{i:05d} |\n" for i in range(2, 30))
    # The budget ends just after the header row: before the separator would be the best-ranked cut.
    parts = rich.split_markdown(intro + table, char_budget=len(intro) + len("| a | b |\n") + 3)
    for part in parts:
        # A header row always comes with its separator, and a part holds one table header at most: a cut
        # between them would leave "| a | b |" as text and give the next part two separators.
        assert part.count("| a | b |\n") == part.count("| a | b |\n|---|---|\n")
        assert part.count("|---|---|") <= 1


def test_a_single_line_longer_than_the_budget_is_cut_at_spaces():
    line = " ".join(f"w{i:05d}" for i in range(5000)) + "\n"  # 35k chars, no break
    parts = rich.split_markdown(line, char_budget=10000)
    assert all(len(p) <= 10000 for p in parts)
    assert [w for p in parts for w in _words(p)] == [f"w{i:05d}" for i in range(5000)]


# Regressions from the Codex review of slice 1 (docs/review-log.md).


def test_a_literal_nul_does_not_stall_preparation():
    # The placeholder restorer looped while any NUL remained, so a bare NUL hung the gateway.
    assert rich.prepare_markdown("# Result\n\na\x00b") == "# Result\n\na\x00b"


def test_a_header_that_fills_the_budget_does_not_stall_the_cut():
    header = "| " + "h" * 44 + " | b |\n|---|---|\n"  # 60 characters with its separator
    text = header + "".join(f"| {i} | w{i:05d} |\n" for i in range(20))
    parts = rich.split_markdown(text, char_budget=60)
    assert [w for p in parts for w in _words(p)] == [f"w{i:05d}" for i in range(20)]


@pytest.mark.parametrize(("text", "expected"), [
    ("$x < y$", "$x < y$"),  # formula source is raw LaTeX
    (r"\(a<b\)", "$a<b$"),
    ("<code>cost=$5</code>", "<code>cost=$5</code>"),
    ("<pre>if a < b: pay($5)</pre>", "<pre>if a < b: pay($5)</pre>"),
    ("`a\n$5 <b>`", "`a\n$5 <b>`"),  # a code span may cross a line break
    ("$$\na\nb\n$$", "$$\na\nb\n$$"),  # no hard breaks inside a formula
    ("~~~\na\nb\n~~~\n", "~~~\na\nb\n~~~\n"),  # nor inside a tilde fence
    ("line one\nline two", "line one  \nline two"),  # a lone break in prose is a hard break
    ("| a | b |\n|---|---|\n| 1 | 2 |", "| a | b |\n|---|---|\n| 1 | 2 |"),  # table rows stay rows
])
def test_preparation_leaves_code_and_maths_verbatim(text: str, expected: str):
    assert rich.prepare_markdown(text) == expected


@pytest.mark.parametrize("text", [
    "between $5 and $10, $x^2$ and \\(a+b\\)\nnext line", "List<String> <u>u</u>\n\n- a\n- b",
    "$$\\int x$$ and `code $5`", "a \\$5\nb",
])
def test_preparation_is_idempotent(text: str):
    once = rich.prepare_markdown(text)
    assert rich.prepare_markdown(once) == once


def test_a_code_line_longer_than_a_message_keeps_its_fence_and_every_character():
    line = "".join(f"w{i:05d} " for i in range(6000))  # 42,000 characters: over Telegram's 32,768
    parts = rich.split_markdown("```json\n" + line + "\n```\n")
    assert len(parts) >= 2
    for part in parts:
        lines = part.splitlines()
        assert lines[0] == "```json" and lines[-1] == "```" and len(lines) >= 3  # no empty code block
        assert len(part) <= rich.MAX_CHARS
    assert "".join(p.splitlines()[1] for p in parts) == line  # every character, spaces included


def test_a_line_over_the_budget_but_within_telegrams_limit_is_sent_whole():
    formula = "$$" + "x+" * 16010 + "1$$"  # 32,025 characters on one line
    parts = rich.split_markdown("intro\n\n" + formula + "\n\noutro\n")
    assert any(formula in part for part in parts)


def test_a_construct_over_the_budget_but_within_telegrams_limit_is_kept_whole():
    details = "<details>\n<summary>more</summary>\n\n" + "".join(f"w{i:05d} line\n" for i in range(12)) + "</details>\n"
    text = "intro\n\n" + details + "\noutro\n"
    parts = rich.split_markdown(text, char_budget=100)  # the block is ~170 characters
    assert any(details in part for part in parts)


def test_bracket_display_maths_is_never_cut_once_prepared():
    formula = "\\[\n" + "".join(f"x_{{{i}}} +\n" for i in range(40)) + "1\n\\]\n"
    prepared = rich.prepare_markdown("before\n" + formula + "after\n")
    for part in rich.split_markdown(prepared, char_budget=len(formula) + 20):
        assert part.count("$$") % 2 == 0


def test_a_loose_list_item_keeps_its_second_paragraph():
    # The item (28 characters) fits the budget; its inner blank line must not be taken as the best cut.
    text = "pre\n- first\n\n  second paragraph\n- next\n"
    parts = rich.split_markdown(text, char_budget=30)
    assert "- first\n\n  second paragraph\n" in parts


def test_a_part_gets_the_footnote_definitions_its_references_need():
    filler = "\n\n".join("w00000 " * 30 for _ in range(10))
    text = "Result[^1] here.\n\n" + filler + "\n\n[^1]: Source of the result.\n"
    parts = rich.split_markdown(text, char_budget=800)
    assert len(parts) >= 2
    assert "[^1]: Source of the result." in parts[0]


def test_highlight_needs_rich():
    assert rich.needs_rich("==important==") is True


@pytest.mark.parametrize(("text", "expected"), [
    (r"It costs \$5.", "It costs $5."),
    ("`\\$5` stays", "`\\$5` stays"),
    ("```\n\\$5\n```\n", "```\n\\$5\n```\n"),
])
def test_unescape_dollars_for_the_ordinary_path(text: str, expected: str):
    assert rich.unescape_dollars(text) == expected


# Regressions from the re-review of slice 1 (docs/review-log.md).


@pytest.mark.parametrize(("text", "expected"), [
    (r"$$\text{when \(x < y\), stop}$$", r"$$\text{when \(x < y\), stop}$$"),  # source inside a formula
    ("text\n\n    a < b and $5\n", "text\n\n    a < b and $5\n"),  # a top-level indented code block
    ("- item\n\n    more $5", "- item\n\n    more \\$5"),  # in a list, the same indentation continues the item
])
def test_preparation_leaves_formula_and_indented_code_source_alone(text: str, expected: str):
    assert rich.prepare_markdown(text) == expected


def test_a_large_table_header_is_repeated_while_the_next_row_still_fits():
    header = "| " + "h" * 16000 + " | b |\n|---|---|\n"
    rows = "".join(f"| {i} | w{i:05d} |\n" for i in range(600))
    parts = rich.split_markdown(header + rows)  # the block budget forces cuts, not the characters
    assert len(parts) >= 2
    assert all(part.startswith(header) for part in parts)


def test_footnote_definitions_come_from_prose_with_their_continuations():
    filler = "\n\n".join("w00000 " * 30 for _ in range(10))
    text = ("Result[^x] here.\n\n```\n[^x]: fake, in a code example\n```\n\n" + filler
            + "\n\n[^x]: real source\n    continued on the next line\n")
    parts = rich.split_markdown(text, char_budget=800)
    assert "[^x]: real source\n    continued on the next line\n" in parts[0]


def test_the_cut_leaves_room_for_the_definitions_a_part_needs():
    # A reference in an almost full part: its 1,206-character definition must still fit beside it.
    paragraphs = "\n\n".join("".join(f"w{p:02d}{i:03d} " for i in range(142)) for p in range(32))  # ~1 KB each
    text = "Ref[^n] here.\n\n" + paragraphs + "\n\n[^n]: " + "d" * 1200 + "\n"
    parts = rich.split_markdown(text)
    first = next(p for p in parts if "Ref[^n]" in p)
    assert "[^n]: " + "d" * 1200 in first and len(first) <= rich.MAX_CHARS


def test_ordinary_path_keeps_escapes_inside_html_code():
    assert rich.unescape_dollars(r"pay \$5 with <code>echo \$HOME</code>") == r"pay $5 with <code>echo \$HOME</code>"


def test_a_part_falling_back_to_markdownv2_loses_only_the_escapes_preparation_added():
    prepared = rich.prepare_markdown("List<String> costs $5, `a\\<b`")
    assert rich.plain_source(prepared) == "List<String> costs $5, `a\\<b`"


# Regressions from the Codex review of slice 2 (docs/review-log.md).


@pytest.mark.parametrize(("text", "expected"), [
    (r"\[\verb|$|\]", r"$$\verb|$|$$"),  # a dollar inside a bracket formula is its source, not a price
    (r"\(x = \$5\)", r"$x = \$5$"),
    (r"cost $5, then \[a+b\]", r"cost \$5, then $$a+b$$"),
])
def test_a_formula_body_is_never_touched_whatever_its_delimiter(text: str, expected: str):
    assert rich.prepare_markdown(text) == expected


def test_a_top_level_indented_code_block_goes_rich():
    assert rich.needs_rich("Run this:\n\n    a < b and $5\n") is True


def test_a_part_keeps_room_for_a_long_definition_its_reference_needs():
    paragraphs = "\n\n".join("".join(f"w{p:02d}{i:03d} " for i in range(142)) for p in range(32))
    definition = "[^n]: " + "d" * 10_000
    parts = rich.split_markdown("Ref[^n] here.\n\n" + paragraphs + "\n\n" + definition + "\n")
    first = next(p for p in parts if "Ref[^n]" in p)
    assert definition in first and len(first) <= rich.MAX_CHARS
