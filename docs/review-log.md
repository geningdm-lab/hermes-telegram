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

## Slice 2, PR #4

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:1857b92` at `1552b36` (slice 2 and the slice 1 re-review fixes), 2026-11-25. Twenty-four findings: twenty-three accepted and fixed, one disputed. The reviewer found the reasoning sound for leaving the ordinary MarkdownV2 retry policy unchanged (slice 1, accepted in part), and showed that a rich answer's fallback chunks need not use it; that is now fixed.

### Accepted and fixed

The guards of `telegram_send`:

| Finding | Fix |
|---|---|
| [P1] A loop or goal wakeup (an internal event on the user's route) can call `telegram_send` | the adapter records each human Telegram event it processes (`HumanTurnsMixin.on_processing_start`, internal events excluded) and forgets it when processing ends; a tool acts only for a recorded event, and a turn without an inbound message id (a synthetic source, `gateway/run_goals.py:112`) is refused at once; `checks/test_tools.py::test_a_tool_acts_only_while_the_users_own_event_is_processed` |
| [P1] Notices and per-photo fallbacks escape the cap of 10 | the whole plan plus one combined notice is reserved before anything is sent, and a call the turn cannot hold sends nothing; each extra message of a refused album is charged; the notices of a call go as one message; `checks/e2e/test_file_sends.py::test_twenty_missing_photos_are_named_in_one_message`, `test_a_call_the_turn_cannot_hold_sends_nothing` |
| [P1] A tool notice in a DM topic can fall back outside the topic | sends use the routing metadata Hermes builds for the event (`_thread_metadata_for_event`), with the DM-topic marker and reply anchor, so a gone topic fails closed; `test_a_notice_in_a_dm_topic_never_leaves_the_topic` |
| [P2] Evicting old budgets can reset an active turn's count | the count lives on the turn's record and ends with it; no eviction of active turns short of 4096 at once |

Duplicates after a lost reply:

| Finding | Fix |
|---|---|
| [P1] A rich answer's MarkdownV2 fallback chunk is retried after a lost reply | `_send_chunk_markdown_or_plain` passes such an error on as `TimedOut`, which the harvested loop does not retry, for those chunks only; `checks/e2e/test_sender.py::test_a_markdownv2_fallback_chunk_whose_reply_was_lost_is_not_sent_again` |
| [P1] An album whose reply was lost is replayed photo by photo | albums go through this plugin's own sender (`FilesMixin.send_photos`); only a refused or certainly unsent album is sent photo by photo; a lost reply is reported as "may or may not be in the chat"; `test_an_album_whose_reply_was_lost_is_not_sent_again_photo_by_photo`, `test_an_album_telegram_refused_is_sent_photo_by_photo` |
| [P2] A `timedelta` flood wait on a fallback chunk crashes the harvested sender | passed on in seconds (`_FloodWait`), on every chunk; `test_a_timedelta_flood_wait_on_a_fallback_chunk_is_waited_out` |

Files that end up silent or wrongly reported:

| Finding | Fix |
|---|---|
| [P1] Batch success copied to every photo of an album | a receipt per photo, with its own message id or reason, through filtering, chunking and fallback; `test_each_photo_reports_its_own_message_id_and_an_oversize_one_its_reason` |
| [P1] A `MEDIA:` path a remote or Docker backend cannot resolve stays silent | `extract_media` resolves each path with Hermes' own `filter_media_delivery_paths` and the session key Hermes uses for the response, on every backend, and passes a resolved path on resolved; `test_a_missing_media_path_is_named_on_a_remote_backend_too` |
| [P1] A notice refused by a flood wait is marked as given and never sent | a notice counts as given once sent; one refused by a flood wait is sent after it; `test_a_notice_refused_by_a_flood_wait_arrives_after_it` |
| [P2] `telegram_send` does not use Hermes' remote fetch | it resolves paths with the same Hermes resolver, off the gateway loop, in the tool's context, which carries the turn's profile |
| [P2] Mixed files lose their order and the caption goes to the wrong file | the plan walks the files in order, only consecutive photos share an album, and the caption goes to the first send; `test_files_go_in_their_order_with_the_caption_on_the_first` |
| [P2] An audio refusal quotes another file's error | each send keeps its own upload error (`_send_media`, `_upload_scope`); `test_an_audio_refusal_names_its_own_error_not_an_earlier_files` |
| [P2] A file gone between the check and the upload is not named | `_send_local_file` names it when the harvested shell returns without calling `on_error`; `test_a_file_gone_before_its_upload_is_named` |
| [P2] A document reached through a link is named after the link's target | the name the agent gave is passed as `file_name`; `test_a_document_keeps_the_name_the_agent_gave_when_it_is_a_link` |
| [P2] A large photo is refused before the harvested sender compresses it | no size check before a photo send; an album measures the JPEG it uploads; a photo sent as a document measures the document |
| [P2] Eleven photos become an album of 10 and a lone photo | albums are rebalanced to 9 and 2 (`album_chunks`); `checks/test_tools.py::test_photos_are_cut_into_albums_telegram_accepts`, and the gateway album test for 11 |
| [P2] A placeholder is missed when Hermes hands back its resolved path | placeholders are keyed by the resolved path; `test_a_placeholder_notice_is_found_by_the_path_hermes_hands_back` |
| [P2] The row 5 test compares lengths, not bytes | the stand-in records a SHA-256 of each upload, and the tests compare it with the file's |

The cutter:

| Finding | Fix |
|---|---|
| [P2] A long footnote definition can still miss its reference's part | each new reference reserves its definition while a part fills; `checks/test_rich.py::test_a_part_keeps_room_for_a_long_definition_its_reference_needs` |
| [P2] A dollar inside `\[...\]` is escaped before the formula is recognized | one scan sets aside whichever formula opens first, whatever its delimiter (`_protect_maths`); `test_a_formula_body_is_never_touched_whatever_its_delimiter` |
| [P2] A top-level indented code block is sent as an ordinary message | `needs_rich` counts every code segment; `test_a_top_level_indented_code_block_goes_rich` |

### Disputed

**[P1] Carry ambiguous delivery through the final-response ledger** (`hermes_telegram/answers.py`, `_send_retry_is_final`). Finding: after a final answer's reply is lost, Hermes' delivery ledger marks the obligation failed and redelivers the whole answer later (`gateway/platforms/base.py:4195`, `gateway/run_startup.py:487`), so the user can see it twice.

Position: that resend is Hermes' recovery policy, and it is visible, not silent. The ledger's contract is "never silently resend an ambiguous send" (`gateway/delivery_ledger.py:1-10`): a failed or interrupted final is resent with a marker that says it may be a duplicate (`RECONNECTED_MARKER`, `FLOOD_MARKER`, `delivery_ledger.py:41-50`), and only after a backoff, with the last attempt kept for restart recovery (`retry_not_before`, `delivery_ledger.py:151-168`). This plugin removed the silent duplicate: the immediate, unmarked base retry (`_send_retry_is_final`). Suppressing the ledger's marked resend as well would trade a labelled possible duplicate for a possibly lost answer, which the ledger exists to prevent, and it would need the plugin to rewrite another module's delivery state.

Reopen if the owner prefers losing an answer whose delivery is unknown over a marked resend, or if the ledger starts resending without a marker.

Owner decision, 2026-11-25: keep the marked resend (docs/design.md, Decisions and open questions, 6).

## Slice 3, PR #5

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:a10a802` at `0507450`, 2026-11-25. Twenty-one findings: twenty accepted and fixed, one disputed in part. The slice 1 findings moved here are settled: the streamed overflow is fixed below; an edited final over the limits happens only where the stream edits messages (groups, and a private chat set to `streaming.transport: edit`, below).

### Accepted and fixed

Stop:

| Finding | Fix |
|---|---|
| [P1] Stop matches the first turn of the chat, not its own draft | a press acts only on the turn whose preview showed that draft (`Preview.draft_ids`), in that chat and topic; a stale press is ignored; `checks/e2e/test_preview.py::test_stop_on_another_turns_draft_is_ignored` |
| [P1] Stop skips the command's admission check | the presser is admitted again, now, before anything changes; `test_stop_from_a_user_no_longer_admitted_is_ignored` |
| [P1] Stop sends again a segment already in the chat | the segments the stream sealed are left out of the preview and of the partial sent after Stop (`Preview.sealed`); `test_a_segment_already_in_the_chat_is_neither_shown_again_nor_sent_again_after_stop` |
| [P2] A refused partial after Stop is lost | sent with Hermes' `_send_with_retry` (a certainly unsent send again, an ambiguous one not), and a failure is logged |
| [P2] "Stopped" is deleted under `display.ephemeral_system_ttl` | the deletion is skipped for the Stop button's own dispatch; `checks/e2e/test_lifecycle_profile.py::test_stopped_stays_in_the_chat_when_hermes_deletes_its_system_replies` |
| [P2] The Stop test times the acknowledgement, not the stop | a turn stopped during a tool never asks the model for its next step, and an approval works after it; `checks/e2e/test_lifecycle.py::test_stop_stops_the_work_itself_and_an_approval_works_after_it` |

The preview:

| Finding | Fix |
|---|---|
| [P1] An unadmitted sender gets "Contemplating..." and a Stop button | a preview starts only for a user the adapter admits (`_is_callback_user_authorized`, the gateway's own chain when it runs); `test_a_user_the_adapter_does_not_admit_gets_no_preview` |
| [P1] Rich drafts bypass Telegram Desktop's crash guards | guarded shapes (maths in `<details>`, CJK) go as the plain frame; `test_maths_inside_details_goes_as_a_plain_frame` |
| [P1] One topic finishing pauses another topic's preview | Hermes' pause is one flag per chat: it now marks a pause pending, which the next message to a topic turns into that topic's final; a stream still sending frames is not finalizing; `test_one_topic_finishing_does_not_stop_another_topics_preview` |
| [P1] A frame in flight can land after the final | the final waits for any frame on its way, and no frame starts after it; `test_a_frame_in_flight_lands_before_the_final_and_none_after` |
| [P1] A topic that refuses drafts gets success forever | a refused plain frame (short text, no markup) means drafts are refused here: `send_draft` reports it, so Hermes' stream falls back to edited messages; `test_a_topic_that_refuses_drafts_lets_the_stream_fall_back_to_edited_messages` |
| [P2] Flood waits are ignored by frames | a flood refusal holds every frame of the preview until it passes, with no plain retry; `test_a_flood_wait_on_a_frame_holds_every_frame_until_it_passes` |
| [P2] The cron check does not check the report arrives, nor after Stop | both: `test_a_cron_report_during_the_turn_arrives_and_the_preview_comes_back_under_it`, `test_after_stop_a_cron_report_arrives_and_no_preview_comes_back` |
| [P2] The row 10 check filters a message the user sees | the check runs under the acceptance profile's setting (`display.suppress_warning_notifications`) and asserts the only message the chat shows says the answer failed; `test_lifecycle_profile.py::test_a_failed_turn_leaves_one_message_saying_the_answer_failed_and_no_preview` |
| [P3] The refresh test replaces the production interval | `test_the_production_refresh_comes_before_telegram_lets_a_draft_expire` holds the production constants against the 30 s expiry |
| [P3] The copied transport methods lack their upstream notice | `hermes_telegram/updates.py` names the upstream file, commit and MIT licence |

A long answer streaming past the rich limit:

| Finding | Fix |
|---|---|
| [P1] The live tail is prepared before the stream appends to it | the cut returns the agent's Markdown; `\[ ... \]` blocks are now no-cut zones of the cutter, as `$$` blocks are; `checks/e2e/test_stream_heads.py::test_the_live_tail_stays_as_the_agent_wrote_it` |
| [P1] A head without structure of its own goes as MarkdownV2 chunks | a recognised head is a part of a rich answer whatever its own text; `test_a_sealed_head_without_structure_of_its_own_goes_as_one_rich_message` |
| [P2] The cut ignores the consumer's UTF-16 units | the budget is scaled to them; `test_the_cut_counts_in_the_consumers_units` |
| [P2] Head records outlive their stream | a record is used by the first send of that text, with or without `expect_edits`, and lapses after 120 s; `test_a_head_record_is_used_once`, `test_a_head_record_lapses` |

### Disputed in part

**[P2] Coordinate the preview with explicitly edit-based DM streaming.** Accepted: a private chat set to `streaming.transport: edit` no longer gets a draft on top of Hermes' edited messages. When the stream sends a message it will edit, the preview stops (`test_a_chat_streamed_by_editing_messages_gets_no_preview_on_top`).

Not done: a rich final in parts for that mode. Under `edit`, Hermes streams by editing one message and finalizes it in place (`stream_consumer_transport.py:128`), so an answer over one rich message's limits ends as the harvested MarkdownV2 edit. That mode is a profile's explicit choice against Telegram's drafts, which this plugin builds on; the default (`auto`) uses drafts in every private chat. Making the edited path split into rich parts would mean replacing Hermes' edit-finalize logic in the stream consumer, not the adapter's.

Reopen if a profile of the deployment sets `streaming.transport: edit` for Telegram.

## Slice 4, PR #6

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:slice/3-lifecycle` (`87e855e`) at `330e95a`, 2026-11-25. Nineteen findings: eighteen accepted and fixed, one disputed in part. The tests named are in `checks/test_action_events.py` unless another file is given; each was checked against a mutation of its fix.

### Accepted and fixed

Who may act, and where it lands:

| Finding | Fix |
|---|---|
| [P1] Taps, votes and reactions skip the text's admission | `_actor_admitted`: the adapter's `allow_from` when set, else the gateway's chain, failing closed, before any state changes; private chats only; `test_a_tap_by_a_user_the_adapter_does_not_admit_reaches_nobody_and_takes_nothing`, `test_a_tap_in_a_group_reaches_nobody`, `test_a_vote_by_a_user_the_adapter_does_not_admit_reaches_nobody`, `test_a_reaction_by_a_user_not_admitted_or_in_a_group_reaches_nobody` |
| [P1] A failed acknowledgement consumes the choice undelivered | the acknowledgement is best effort; a hand-off that fails releases the claim; `test_a_tap_reaches_the_agent_even_when_telegram_refuses_its_acknowledgement`, `test_a_tap_whose_hand_off_fails_leaves_the_set_open` |
| [P1] A token is not bound to the message that shows it | a set is bound to its message, topic and profile once sent (`ButtonStore.bind`), and a mismatch is "expired"; `test_a_token_presented_with_another_message_topic_or_chat_is_not_that_sets_tap` |
| [P1] Tool sends fall back out of the topic | `TOOL_SEND` turns off the media retry without topic and the root-chat text retry for a tool's sends (`HumanTurnsMixin`); text goes through `send_single`, which has no fallback; `test_a_tools_poll_fails_rather_than_leave_the_turns_topic`, `test_a_tools_notice_fails_rather_than_leave_a_forum_topic` |
| [P1] An explicit reaction target may be in another topic | only the current message or a bot message of the same chat and topic; `test_the_agent_reacts_only_in_its_own_conversation` |
| [P1] Reactions lose their topic, and the index its content on restart | the bot's messages are kept with their topic in `telegram_bot_messages.json`; `test_a_reaction_in_a_topic_reaches_that_topic_after_a_restart` |
| [P2] Action turns lack the topic's skill and prompt | `_action_event` resolves them as the text path does (`_resolve_topic_binding`, `resolve_channel_prompt`); `test_a_tap_is_routed_by_the_user_who_tapped_with_the_topics_skill_and_prompt` |

Sending:

| Finding | Fix |
|---|---|
| [P1] A long tool text can exceed the cap | the tool's text is one message (`send_single`); a longer one is refused unsent; `test_a_tool_text_longer_than_one_message_is_refused_unsent` |
| [P1] Button sends skip the rich safety checks | `send_single` uses `_rich_eligible_any_length`, the answer path's check; `test_a_tool_message_with_buttons_is_rich_exactly_when_an_answer_would_be` |
| [P2] Button sends ignore the notification policy | `send_single` applies `_notification_kwargs`; `test_a_tool_message_follows_the_notification_policy` |
| [P2] An uncertain poll or sticker is reported as not sent | `_send_failed` marks a timeout after the request went out `ambiguous_delivery`; the tool keeps the slot and tells the agent not to send it again; `test_an_uncertain_poll_keeps_its_slot_and_says_not_to_send_it_again` |

State:

| Finding | Fix |
|---|---|
| [P2] Store write failures report success | `StoreError` from both stores; a set that cannot be saved is not sent, a claim that cannot be saved is not taken; `checks/test_buttons.py::test_a_store_that_cannot_save_publishes_and_takes_nothing` |
| [P2] A vote before `sendPoll` returns is dropped | a vote for an unknown poll waits up to 5 s while a poll send is in flight; `test_a_vote_that_arrives_before_sendpoll_returns_reaches_the_agent` |
| [P2] Quoted fields are not escaped | JSON strings (`quoted`); `checks/test_buttons.py::test_a_quote_inside_a_field_cannot_read_as_another_field` |
| [P2] Processing reactions replace the agent's | `_set_reaction` leaves a message the agent reacted to alone; `test_hermes_processing_reactions_leave_the_agents_reaction` |
| [P2] Partial deliveries and control prompts are not recorded | every delivered id of a partial send (`DELIVERED_IDS`, also where Hermes keeps only the tail) and `_send_control_message`; `test_every_part_of_a_partly_delivered_answer_is_a_bot_message`, `test_a_control_prompt_is_a_bot_message` |
| [P2] A sticker set that failed once is gone until restart | read again a minute after a failure; `test_a_sticker_set_telegram_failed_to_return_is_asked_for_again_later` |
| [P2] Negative end-to-end checks sleep instead of synchronizing | `checks/e2e/test_actions.py`: the second tap is checked behind a same-chat barrier, the own-message reaction behind a later reaction that does reach the agent |

### Disputed in part

**[P1] Partition action state by routed profile on a shared bot.** Accepted: every record carries the profile that made it, and every read checks it. A set sent by one profile takes no tap routed to another (`test_a_set_the_owner_sent_does_not_take_a_tap_routed_to_another_profile`), a vote counts only for the poll's profile, and the sticker catalog holds only what users of the caller's profile sent (`test_a_sticker_the_owner_sent_is_not_in_another_profiles_catalog`).

Not done: separate files per profile home. The stores live in the adapter's home (`_update_receipt_dir`), next to the update receipts Hermes keeps there for the same bot (hermes-agent@c419cf7:plugins/platforms/telegram/adapter.py:595); one bot's state stays together, and the gateway process that owns the bot reads it either way. The configured sticker sets stay shared: they are public sets named in the adapter's own config.

Reopen if profiles of one bot run under separate OS users, or a profile's records must not be readable by the gateway that serves the others.

## Slice 5, PR #7

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:slice/4-actions` (`330e95a`) at `485d731`, 2026-11-25. Fourteen findings, all accepted and fixed, on top of the slice 4 fixes merged in (`784f395`). The tests named are in `checks/test_widget_events.py` unless another file is given; each was checked against a mutation of its fix.

### Accepted and fixed

A widget's requests:

| Finding | Fix |
|---|---|
| [P1] A widget's request skips the text's admission | `_actor_admitted` (slice 4) before the request reaches the agent; the answer to Telegram still comes first; `test_a_request_by_a_user_the_adapter_does_not_admit_is_answered_and_reaches_nobody` |
| [P1] Every request lands in the root chat | each widget is kept with its topic, and a request reaches that topic; only this bot's widgets, in the private chat, for the widget's profile; `test_a_request_reaches_the_topic_the_widget_is_in_after_a_restart`, `test_a_request_for_anything_but_this_bots_widget_reaches_nobody`, `test_a_widget_sent_by_one_profile_takes_no_request_routed_to_another` |
| [P1] The 10 s filter drops a real second press | deduplication by query id only; `test_two_presses_with_the_same_data_are_two_actions_and_a_repeated_query_is_one`. Telegram marks no request as a background one, so the tool's description and the hint tell the agent to request on the user's action, never on a timer. |
| [P2] A flood wait on the answer is not retried | answered again while Telegram certainly did not take it and the 15 s allow it; never after a timeout that may have reached it; `test_an_answer_telegram_certainly_did_not_take_is_sent_again_within_the_deadline` |
| [P2] The answer check counts other tests' answers | `checks/e2e/test_widgets.py` checks one answer per query id it pushed; `test_two_presses_with_the_same_data_reach_the_agent_twice_each_answered_once` |

State and sending:

| Finding | Fix |
|---|---|
| [P1] A state edit can change another topic's widget | only a widget of the same chat, topic and profile; `test_the_agent_changes_only_its_widgets_in_its_own_conversation` |
| [P2] Widget ownership is lost on restart | `telegram_widgets.json`, a store of its own, not the reaction index; `test_a_request_reaches_the_topic_the_widget_is_in_after_a_restart` |
| [P2] An uncertain widget send refunds its slot | `_send_failed` and `_sent_or_refused` from slice 4; `test_an_uncertain_widget_keeps_its_slot_and_says_not_to_send_it_again` |
| [P2] A missing state erases the widget's state | `state` is required, `""` clears it; `test_a_state_change_needs_a_state_and_an_empty_one_clears_it` |
| [P2] An unchanged state is reported as a failure | "message is not modified" is success, as bots/widgets says; the stand-in now answers it; `test_a_state_the_widget_already_has_is_a_change_made` |
| [P3] State with a NUL or a lone surrogate | `check_state` for the send and the edit; the stand-in refuses NUL; `checks/test_widgets.py::test_a_state_is_held_to_telegrams_limits`, `test_a_widget_telegram_would_refuse_is_refused_unsent` |
| [P3] Height 0 is refused | 0 is Telegram's automatic height; `test_height_zero_is_the_contents_own_height` |

The widget check:

| Finding | Fix |
|---|---|
| [P2] Text naming an API, and links, are refused | the network check reads only the widget's code, without its string literals and comments; `<a href>` is allowed; `checks/test_widgets.py::test_a_widget_that_only_mentions_the_network_or_links_out_passes` |
| [P2] Relative images and workers pass | every resource attribute and CSS `url()` must be a fragment or a `data:` PNG, JPEG, GIF or WebP; `Worker`, `SharedWorker`, `BroadcastChannel` and `RTCPeerConnection` are network APIs; media and frame elements are refused; `checks/test_widgets.py::test_a_widget_telegram_or_its_sandbox_would_refuse_is_named_with_the_reason` |

## Slice 6, PR #8

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:slice/5-widgets` (`7688fce`) at `0b91437`, 2026-11-25. Twelve findings, all accepted and fixed. The tests named are in `checks/test_message_ops.py`; each was checked against a mutation of its fix.

### Accepted and fixed

Whose message it is:

| Finding | Fix |
|---|---|
| [P1] Ownership is not bound to the bot that sent it | each record names the bot (the token's id); another bot's record authorizes nothing; `test_a_message_another_bot_sent_in_the_same_chat_is_not_this_bots` |
| [P1] The requested topic is recorded, not the delivered one | `_delivered_thread`: a send that fell back to the root chat (`thread_fallback`, a returned message without a topic) is recorded without the topic; `test_a_media_send_that_fell_back_to_the_root_chat_is_not_in_the_topic` |
| [P1] Message changes ignore the routed profile | each record names the profile the chat was routed to, checked against the turn's; `test_a_message_sent_for_another_profile_is_not_this_turns` |
| [P1] Records without a kind are edited as text | only a record whose kind is `text` is edited, so a prompt kept by slice 5 keeps its approval buttons; `test_a_message_recorded_before_kinds_existed_is_not_edited` |

Edits:

| Finding | Fix |
|---|---|
| [P2] The edit's form follows the new text, not the message | an ordinary message stays MarkdownV2 (with a note when the text has structure only rich shows), a rich one stays rich; `test_an_ordinary_message_is_edited_as_one_and_a_rich_one_as_rich` |
| [P2] Hermes' reply context is stale after a rich edit | a rich edit, and a tool's rich message, are recorded in `rich_sent_store`; same test |
| [P2] An unconfirmed edit trusts the old keyboard | the old set is retired, the new one bound and kept as the message's keyboard, and the agent is told the edit may not have been applied; `test_an_edit_telegram_did_not_confirm_retires_the_old_choices` |
| [P2] `buttons: []` keeps the keyboard | it removes the keyboard and retires its set; `test_an_empty_button_list_removes_the_keyboard` |
| [P2] A deleted message's taps, widget requests and votes still reach the agent | a delete retires its buttons, widget and poll; `test_a_deleted_messages_taps_widget_requests_and_votes_reach_nobody` |

Replies and effects:

| Finding | Fix |
|---|---|
| [P2] A quote loses the formatting Telegram matches | the turn keeps the user's text and entities; a quote carries the entities over its span (`quote_parts`); a quote of the bot's own messages, whose entities and rich blocks are not kept, is refused; `test_a_quote_carries_the_formatting_telegram_matches`, `test_a_quote_telegram_would_not_match_is_refused_before_anything_is_reserved` |
| [P2] A quote in a turn a tap started reserves slots and fails | refused before anything is reserved: such a turn has no message text; same test |
| [P2] An effect Telegram did not apply reads as success | `0` (no effect) is refused; a message returned without the effect asked for is reported; the stand-in returns `effect_id` as Telegram does; `test_an_effect_telegram_did_not_apply_is_reported_and_no_effect_is_refused` |

## Business inbox, slice 1

Review: Codex `gpt-6.2-astra`, effort `high`, scope `branch:origin/main` at `5d3f27f`, 2026-11-27 (docs/business-inbox.md). Twenty findings: nineteen accepted and fixed, one accepted in part. The tests named are in `checks/test_business_inbox.py`, or `checks/e2e/test_business_inbox.py` where marked e2e; each was checked against a mutation of its fix unless the row says otherwise.

### Accepted and fixed

Who may write:

| Finding | Fix |
|---|---|
| [P1] Reauthorize stored connections against the current owner map | a kept connection counts only while its owner is the configured one; `test_a_kept_connection_of_an_owner_no_longer_configured_keeps_nothing_new` |
| [P1] Order connection lookups, state updates, and message admission | one lookup or connection update at a time per connection (`_connection_lock`), and the connection checked enabled inside each write; `test_a_disconnect_during_a_lookup_is_not_undone_by_the_lookups_answer`, `test_a_connection_that_is_not_enabled_keeps_nothing` |
| [P1] Reserve the business handler's place in dispatch | the handler matches business updates only and is kept first in its group after registering and after a late plugin wire (`_wire_plugin_handlers`); `test_a_native_plugins_catch_all_in_the_business_group_does_not_get_business_updates` |
| [P3] Drop non-private deletions before resolving their connection | a deletion outside a private chat is skipped before any lookup; `test_a_connection_keeps_every_right_and_a_deletion_outside_a_private_chat_is_skipped` |

Retention and logs:

| Finding | Fix |
|---|---|
| [P1] Keep expiring existing records when intake is switched off | the purge task runs whenever the file exists, with the limits the file was last written under when intake is off; `test_a_file_keeps_expiring_after_intake_is_switched_off` |
| [P1] Redact business payloads before PTB logs them | a filter on python-telegram-bot's loggers drops DEBUG records that carry a business update or connection, installed when the plugin loads, whether intake is on or not; `test_debug_records_of_business_payloads_are_dropped` |
| [P2] Stop the purge worker when its adapter disconnects | the task belongs to one Application and returns at its next wake once the adapter's `_app` is another, before purging; a new Application starts its own; `test_the_purge_ends_with_the_adapters_application` |
| [P2] Evict the oldest message by Telegram date | the cap ranks by date, then message id, and records read in that order; `test_a_chat_keeps_its_newest_records_by_telegram_date_up_to_the_cap` |
| [P2] Do not report an insert that the cap immediately removes | a message older than every record a full chat keeps is not kept and moves no cursor; same test |

Records and replays:

| Finding | Fix |
|---|---|
| [P2] Distinguish different edits made in the same second | a version is keyed by edit time and a digest of its content; `test_edits_in_the_same_second_are_both_kept_and_a_late_older_edit_does_not_set_the_media` |
| [P2] Preserve the newest attachment metadata on late edits | only an edit at least as new as every kept one sets the record's media; same test |
| [P2] Detect replays before mutating inbox state | each write records the update's id with the bot's id, in its transaction, for the retention period; the chat's name changes only with a change; `test_an_update_applied_once_is_not_applied_again_after_a_restart` |
| [P2] Bound change history as well as version text | a record keeps its newest 51 changes; older ones advance the gap mark; `test_a_records_versions_and_changes_stay_bounded_under_many_edits` |
| [P2] Read records and versions from one SQLite snapshot | both reads in one transaction. No test: a purge between the two reads cannot be placed there from outside the store |
| [P2] Preserve all granted business rights | every `can_*` field of BusinessBotRights is kept; same test as the deletion row |
| [P2] Reject numeric settings that cannot be parsed or stored | ASCII digits only, bounded (ten years of hours, a million records, an owner id below 2^53); `test_unusable_settings_are_refused` |
| [P3] Check the schema version before modifying an existing file | an existing file's schema is read before anything is written; a file of another schema or of something else is refused unchanged; `test_a_file_of_another_schema_is_refused_unchanged` |

End to end:

| Finding | Fix |
|---|---|
| [P2] Check all Bot API calls in the business-only test interval | after the command menu registers, every call while the business updates are handled must be `getUpdates` or `getBusinessConnection`; a photo is among them, so a `getFile` would show (e2e) |
| [P2] Wait for each business lane before asserting absence of effects | each business update's handling is awaited by the admission's own log line for it, then the gateway is made idle by a barrier before the model's requests are checked (e2e); not mutation-checked |

### Accepted in part

**[P1] Enforce the single-owner, default-profile configuration.** Accepted: exactly one owner (decision 5), `test_unusable_settings_are_refused`. Disputed: requiring the literal profile name `default`. The design's rule is that the inbox lives in the home of the profile whose adapter receives the updates, never another profile's ("In this version that must be the adapter's own profile, `default`"). In the deployment the bot belongs to `default` (homelab's runbook for the second profile), so the check against the adapter's own profile is the same check there, and the literal name would only refuse that setup on a host where the bot's profile has another name. Reopens if the owner wants the inbox tied to the name `default` whatever profile owns the bot.
