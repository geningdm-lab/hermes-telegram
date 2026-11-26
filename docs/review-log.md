# Review log

Every slice gets an independent review before it is offered for merge: Codex with GPT-6.2-Astra at effort `high`, run through `llm-bridge` against the slice's branch. Accepted findings are fixed in the slice's pull request and listed here in one line each. Findings this repository disputes are recorded in full, with the evidence and what would reopen them.

## Slice 0, PR #2

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:main` at `68fdbdc`, 2026-11-25. Twelve findings.

### Accepted and fixed

| Finding | Fix |
|---|---|
| [P1] SDK names rebound by a lazy install (`check_telegram_requirements`) do not reach the mixins, so `connect()` refuses to start | `harvested/sdk_names.py` (generated) lists them; `ensure_deps_fn` and the factory copy them into the harvested modules; `checks/test_registration.py::test_lazy_install_reaches_the_mixins_that_use_the_sdk` |
| [P1] The pytest parent imports Hermes with the developer's real home | `checks/conftest.py` gives the session a throwaway `HOME` and `HERMES_HOME` before any import. The earlier runs had created `~/.hermes` with Hermes' default scaffolding (no user data) on the development machine. |
| [P2] `plugins.isolation: host` silently loses Telegram | `docs/install.md`, prerequisite section |
| [P2] The contract suite does not verify the Hermes checkout it runs | `checks/e2e/rig.py` requires `HEAD` at the pin and a clean tree |
| [P2] The parity test misses a lost base class and extra members | `checks/test_harvest.py`: base order and member sets in both directions |
| [P2] The adapter check passes on a restart from the first process's log | `checks/e2e/rig.py` reads only what a start adds to Hermes' INFO log (`logs/gateway.log`; the rig's `log_path` is the child's console) and waits for this plugin's adapter line |
| [P2] Registration imports python-telegram-bot and the adapter in every process | `hermes_telegram/plugin.py` registers lazy hooks; `test_registration_imports_neither_the_sdk_nor_the_adapter` |
| [P2] The upgrade steps fetch the old pin | `HERMES_PIN` is the one input; `test_pin_is_one_commit_everywhere` |
| [P2] The harvest reads working files, not the pinned commit | `scripts/harvest.py` reads `git show PIN:path` |
| [P3] Logger names changed | the harvest keeps the upstream logger names |
| [P3] The install check names a line written before registration | the plugin logs its adapter line when it builds the adapter; `docs/install.md` names it |

### Disputed

**[P3] Resolve adapter type references after splitting the class** (`scripts/harvest.py:316-320`). Finding: `typing.get_type_hints(HermesTelegramAdapter._replacement_telegram_adapter)` raises `NameError`, because the string annotation `"Optional[TelegramAdapter]"` names a class no harvested module defines; `harvested/telegram_context.py` imports `.adapter` under `TYPE_CHECKING`.

Position: no behaviour depends on it. In hermes-agent@c419cf7 the runtime callers of `get_type_hints` are `hermes_cli/plugin_host_wire.py:175,214,226` and `hermes_cli/web_read_coalescing.py:32`. The first serves plugins isolated in the plugin host, which never runs platform plugins (`hermes_cli/plugins_loader.py:655`), and it catches `NameError` itself. The second wraps web-read functions. Nothing introspects adapter methods. The `TYPE_CHECKING` import never executes. Making the names resolve would mean rewriting harvested annotations or importing the composed class into every mixin module, which is a circular import, for no runtime effect.

Reopen if a Hermes upgrade adds a runtime `get_type_hints` on adapter methods, or if a type checker starts running over `harvested/`.

### Re-review of the fixes

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:68fdbdc` (the four fix commits), 2026-11-25. Nine findings, all accepted. The reviewer reproduced the disputed `NameError` and agreed that no pinned runtime caller inspects that method, so the dispute stands.

| Finding | Fix |
|---|---|
| [P1] A `.env` in the Hermes source root reaches the test gateway (`hermes_cli/main.py:715` loads it as a development fallback) | `checks/e2e/rig.py` refuses to run while that file exists |
| [P2] A config load still imports the SDK, through `is_connected` and `check_fn` (`gateway/config_env.py:206`) | the harvest writes the hooks Hermes calls outside the gateway, verbatim, into a light `harvested/registration.py`; `check_fn` probes with `find_spec` until the adapter module is loaded; `test_plugin_discovery_and_config_load_import_neither_the_sdk_nor_the_adapter` runs the real discovery and config load, with Telegram configured and not |
| [P2] The parity test ignores an override earlier in the MRO | `test_the_composed_adapter_runs_the_harvested_member_or_an_intended_override`, with an explicit `INTENDED_OVERRIDES` table |
| [P2] Lazy wrappers changed the hooks' signatures | no wrappers left: the hooks are the harvested functions; `test_registered_callables_have_the_bundled_signatures` compares parameters and sync/async |
| [P2] `git show` output decoded with the locale | decoded as UTF-8 |
| [P2] A restart can return before the new process connects | the driver's `connected` waits for a `getUpdates` newer than the start |
| [P3] Sibling loggers lost their absolute upstream names | they log as `plugins.platforms.telegram.<module>`; `test_module_loggers_keep_their_upstream_names` compares real names |
| [P3] Log rotation breaks the per-start check | the rig compares file identity and reads a replacement file from its start |
| [P3] The clean-tree check aborts non-e2e runs | `checks/e2e/conftest.py` skips collecting the directory under `-m "not e2e"` |

## Slice 1, PR #3

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:ba1bb2e` at `0e0f16d`, 2026-11-25. Twenty-four findings: twenty-one accepted and fixed, two accepted and moved to slice 3, one accepted in part.

### Accepted and fixed

| Finding | Fix |
|---|---|
| [P1] A literal NUL stalls preparation | placeholders carry a random token and are restored newest first, once each; `test_a_literal_nul_does_not_stall_preparation` |
| [P1] A repeated header that fills the budget stalls the cut | a header over half the budget is not repeated; line pieces never get a budget under 1; `test_a_header_that_fills_the_budget_does_not_stall_the_cut` |
| [P1] Inline formula source escaped (`$x \< y$`) | inline maths is set aside whole before escaping |
| [P1] Hard breaks added inside `$$` blocks and `~~~` fences | `rich.prepare_markdown` adds hard breaks itself, in prose only and not in table rows; the harvested normalizer is no longer used |
| [P1] A part resumed after an ambiguous failure may be shown twice | a failed part is resumable only when it was certainly not sent (a flood wait, or no connection); `test_only_a_certainly_unsent_part_is_kept_for_resuming` |
| [P1] A fenced line longer than a message loses its fence and spaces | each piece of the line is closed and reopened in its fence, every character kept |
| [P2] A forbidden cut is still taken when all are forbidden | a maths or details block over the budget but within Telegram's limit is kept whole |
| [P2] `\[...\]` is not seen by the cut | the answer is prepared first and the prepared text is cut |
| [P2] A loose list item's second paragraph becomes its own message | indented continuations and nested items rank as inside the item |
| [P2] A footnote reference and its definition end up in different messages | each part gets copies of the definitions its references need |
| [P2] Code contents escaped inside `<code>`, `<pre>` and multi-line code spans | those are set aside before escaping |
| [P2] The capability latch is ignored inside a multipart send | once rich messages are latched off, the remaining parts go as MarkdownV2 without further rich calls |
| [P2] A flood refusal of a rich part does not arm the chat cooldown | `_record_send_flood_cooldown` on a rich `retry_after` |
| [P2] A refused one-part answer goes straight to MarkdownV2 | every rich answer goes through `_send_items` and is cut smaller on refusal |
| [P2] MarkdownV2 fallback chunks carry unescaped `(1/2)` counters | the harvested sender's counter escaping is reused |
| [P2] The thread fallback state is dropped between chunks | carried through the send and its resume, and reported |
| [P2] The ordinary path shows the hint's `\$` with its backslash | `format_message` undoes `\$` outside code before MarkdownV2 |
| [P2] `==highlight==` alone does not go rich | `needs_rich` recognizes it |
| [P2] The stand-in's column limit fires on separator-shaped lines outside tables | it checks only a header row followed by its separator, outside code |
| [P2] The refusal test passes for a retry of the same payload | the refusal now fires every time the answer is sent whole |
| [P3] The table test does not check header, separator or order | it checks the whole table verbatim |
| [P3] The stand-in returns an input shape as `Message.rich_message` | it returns a `RichMessage` (`blocks`) |

### Accepted, moved to slice 3

Two findings are about streamed answers. Slice 3 rebuilds the streaming lifecycle (drafts, previews, Stop), so they are fixed there. Neither is a regression: before slice 1, streamed answers took the MarkdownV2 path in every case.

- [P1] An answer finalized by editing a preview (DM topics, edit-based streaming) over one rich message's limits is formatted as MarkdownV2 instead of being sent as rich parts.
- [P1] A private-chat answer streaming past the overflow limit is cut by Hermes' stream consumer with the ordinary truncator (`gateway/stream_consumer.py:804`), and its sealed heads go as MarkdownV2 with `expect_edits=True`.

### Disputed in part

**[P2] Protect code contents, not just their HTML tags.** `<code>`, `<pre>` and multi-line code spans are fixed. Not fixed: GFM indented code blocks (four spaces) are still treated as prose. The reason is that inside a list, the same indentation is an item's continuation (CommonMark), and the cut already gives that meaning to indented lines (`continues_item`). Telling the two apart needs a full CommonMark block parser. The agent writes fenced code, which the hint asks for. Reopen if live answers show an indented code block with escaped `$` or `<`.

### Re-review of the fixes

Review: Codex `gpt-6.2-astra`, effort `high`, the fix commits after `c2ab3c7` up to `1857b92`, 2026-11-25. Sixteen findings: fifteen accepted and fixed, one accepted in part. The reviewer agreed with moving the two streaming findings to slice 3, provided neither counts as row 1 coverage until slice 3 lands; they do not. On the indented-code dispute the reviewer showed that a top-level block indented four spaces after a blank line is unambiguous, so it is now handled and the dispute is closed.

| Finding | Fix |
|---|---|
| [P1] An ambiguous first rich send lets the base retry send the whole answer again | a part that may have been shown marks the result `ambiguous_delivery`, and `_send_retry_is_final` returns it as it is; `checks/e2e/test_sender.py::test_a_one_part_answer_is_shown_exactly_once_whatever_the_connection_failure` |
| [P2] A PTB flood wait given as a `timedelta` crashes the cooldown | counted in seconds before the cooldown and the result; `test_a_flood_wait_given_as_a_timedelta_is_waited_out_and_the_answer_completes` |
| [P2] A part that never got a pooled connection is not resumed | `_certainly_unsent` counts PTB's pool timeout as not sent, and the result is retryable; the `pool` cases of the two connection-failure tests |
| [P2] Rich parts after a topic fallback go to the deleted topic again; a resume in cooldown forgets the fallback | rich parts after the fallback leave the topic (`_without_thread`); the cooldown branch of `_resume_partial_send` keeps the flag; `test_after_leaving_a_deleted_forum_topic_the_rest_of_the_answer_still_goes_as_rich` |
| [P2] A part falling back to MarkdownV2 shows the escapes preparation added (`List\<String>`) | `rich.plain_source` takes them out first; `test_a_part_falling_back_to_markdownv2_loses_only_the_escapes_preparation_added`, `test_a_part_sent_as_markdownv2_after_all_shows_the_agents_own_text` |
| [P2] A one-line `$$` formula over the budget but within Telegram's limit is split | a line within Telegram's limit is kept whole; `test_a_line_over_the_budget_but_within_telegrams_limit_is_sent_whole` |
| [P2] `\(...\)` inside an existing formula is rewritten | formulas are set aside before delimiters are converted; `test_preparation_leaves_formula_and_indented_code_source_alone` |
| [P2] A large table header is dropped although the next row fits beside it | the header is repeated while it and the next row fit; `test_a_large_table_header_is_repeated_while_the_next_row_still_fits` |
| [P2] Footnote definitions taken from code examples, and without their continuations | definitions come from prose only, with indented continuations; `test_footnote_definitions_come_from_prose_with_their_continuations` |
| [P2] A definition does not fit beside its reference in an almost full part | the cut reserves room for the definitions a part needs; `test_the_cut_leaves_room_for_the_definitions_a_part_needs` |
| [P2] The ordinary path unescapes `\$` inside `<code>` and `<pre>` | it uses the same verbatim regions as preparation; `test_ordinary_path_keeps_escapes_inside_html_code` |
| [P2] Restoring masked spans rescans the answer once per span | one pass per nesting level; the review's case (16,000 code spans in 64 KB) takes 0.016 s, down from 0.9 s. No timing test, because timings are not stable across machines. |
| [P2] Sender failures are tested against a scripted bot, not PTB | `checks/e2e/test_sender.py`: a real PTB `Bot` against the stand-in, with Bot API refusals from the stand-in and connection failures from httpx's own transport |
| [P3] A code line longer than a message is preceded by an empty code block | the fence opener is carried to the first piece; `test_a_code_line_longer_than_a_message_keeps_its_fence_and_every_character` |
| [P3] The stand-in's table check ignores fence length | a fence closes only with the same character, at least as long; `test_a_wide_table_shown_as_code_inside_a_longer_fence_is_not_refused` |

#### Accepted in part

**[P1] Apply the non-delivery check to MarkdownV2 fallback chunks.** Fixed in this plugin's sender. A fallback chunk that may have been shown is no longer queued for resuming, because `_certainly_unsent` decides that, not the classifier's `retryable`. The base retry no longer resends it either: the chunk is marked `ambiguous_delivery`.

Not changed: the harvested chunk sender (`_send_chunk_with_retries`, hermes-agent@c419cf7 `plugins/platforms/telegram/adapter.py:3551`) retries a non-timeout `NetworkError` such as `httpx.ReadError` up to three times on its own. Every MarkdownV2 message goes through that helper, not just the fallback chunks of a rich answer. Changing it would change the bundled delivery policy for every ordinary message: after a dropped connection, the choice is between a possible duplicate and a possible loss. That decision belongs to the ordinary path as a whole, not to slice 1.

Reopen in slice 3, which owns delivery failures and the failure message, or sooner if live use shows duplicated chunks after a network drop.
