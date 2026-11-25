# Design: hermes-telegram

Status: draft for owner review, all questions answered. Date: 2026-11-25.

## Why

The owner talks to the Hermes agent Lumi mostly in a private Telegram chat. Hermes talks to Telegram through a narrow protocol: the agent writes Markdown, adds `MEDIA:/path` lines for files and `[[audio_as_voice]]` for voice, and the adapter converts that to MarkdownV2. Telegram's Bot API (10.5, 2026-11-22) offers much more: structured rich messages with tables, streamed drafts with a native Stop button, inline HTML widgets, buttons, polls and reactions. The owner's words:

> Использовать куда более широкие возможности форматирования телеграмм, чем то что есть сейчас. Сейчас мы даже файлы не можем отправить. [...] Есть миниаппы, встроеные в сообщения, таблицы, временное сообщение как процесс обработки, и тд. Виджеты еще можно для графики использовать. Схемы, прототипы.

This repository builds a new Hermes platform plugin that replaces the bundled Telegram adapter and gives the agent that whole surface. It runs next to the Prism attempt in `geningdm-lab/lumi-telegram`, against the same acceptance rows, so the two approaches can be compared.

## Acceptance

Acceptance is the 17 rows of the Prism PRD, unchanged, together with the owner's answers Q1 to Q19 recorded there: [PRD at 16ccae4](https://github.com/geningdm-lab/lumi-telegram/blob/16ccae42022da5615ed858f194f2b813f734707d/docs/prd/1-native-telegram-presentation-for-hermes.md). In short: live runs on `vtadevbot`, production Telegram, the official iOS client, the owner's account, private chat, native forms only, no fallbacks. A passing test is not acceptance.

| Row | Behaviour (short form; the PRD wording governs) | Slice |
|---|---|---|
| 1 | Formatted answer, short and longer than one message, every element shown, no literal markup | 1 |
| 2 | 4x6 table shown as a table | 1 |
| 3 | Document, photo, video, audio, voice each arrive as their kind | 2 |
| 4 | 2-10 photos as one album; 12 as albums of 10 and 2, in order | 2 |
| 5 | Media "as a file" arrives as a document with name and bytes unchanged | 2 |
| 6 | Preview shows `Contemplating...`, then the status the agent set last | 3 |
| 7 | Preview shows the growing partial answer | 3 |
| 8 | Final answer persists; preview gone; no stray bot messages | 3 |
| 9 | Stop on the preview stops the work within 5 s; "Stopped" plus partial result persists | 3 |
| 10 | Failure replaces the preview with one failure message | 3 |
| 11 | Choice buttons: one tap reaches the agent once; a second tap shows "already answered" | 4 |
| 12 | Diagram, chart, step-by-step explanation and UI prototype drawn as widgets, no network | 5 |
| 13 | Local widget interaction changes the widget and reaches nobody | 5 |
| 14 | A widget action that needs the agent reaches it with the widget it came from | 5 |
| 15 | An undeliverable file is named with the reason, never silence | 2 |
| 16 | Short plain answer stays one ordinary text message | 1 |
| 17 | Cron reports and approvals keep working, also during a preview and after a Stop | 3 |

Amendment, owner decision on PR #1 (2026-11-25): in row 1 the mathematical expression is rendered as a formula by Telegram, not shown in a code block. Its LaTeX source must equal the agent's. This departs from the Prism row on purpose; compare row 1 with that in mind.

## What exists today

Verified in the source of the pinned Hermes, `NousResearch/hermes-agent@c419cf7`, which `homelab/nix/flake.lock` deploys:

- The Telegram adapter is itself a bundled plugin, `plugins/platforms/telegram/`: 14 files, 8692 lines, 7380 of them in `adapter.py`. It is built on python-telegram-bot 22.8 (`uv.lock:5597`).
- Its prompt hint tells the agent "no tables" and describes only Markdown, `MEDIA:` and `[[audio_as_voice]]` (`plugins/platforms/telegram/adapter_hint.py:8`).
- Rich messages (`sendRichMessage`, Bot API 10.1) are implemented but off by default: `rich_messages` defaults to `False` (`adapter.py:608`).
- Drafts are streamed in private chats (`adapter.py:4084`), without `can_stop`, so there is no Stop button. `stopped_message_generation` appears nowhere in the adapter.
- Inline keyboards exist only for Hermes' own prompts: approvals, slash confirmations, clarify, model and choice pickers (`adapter.py:4250-4626`). The agent cannot send buttons, polls, widgets or reactions, edit or pin a message, or set an effect.
- The deployment multiplexes profiles on one bot: `gateway.multiplex_profiles = true` with a `profile_routes` entry that sends one Telegram user to the `family` profile, and Telegram keys `dm_policy`, `allow_from`, `group_policy`, `allow_admin_from`, `user_allowed_commands` (`homelab/nix/hermes/family-contact/default.nix:70,204-214`). The deployed Hermes is a patched build (`family-contact/policy/core-tool-boundary.patch`).

Not verified: why file sends fail today (PRD Q12: "было много фейлов при отправке. Надо копать чаты"). That needs the gateway logs on the host.

## What Telegram offers

From core.telegram.org, read 2026-11-25. "Prod" means production Telegram in a private chat with the bot. PTB 22.8 models Bot API up to 10.0 (its changelog), so everything from 10.1 on goes through raw calls.

| Capability | Telegram | Bundled adapter | This plugin |
|---|---|---|---|
| Rich message: headings, tables (20 columns), task lists, details, footnotes, math, media blocks, buttons inside; 32768 chars, 500 blocks, 16 levels (10.1-10.5) | Prod, any chat | Off by default; hint forbids tables | Default for formatted answers |
| Draft: streamed preview, ~30 s life, `<tg-thinking>` placeholder (9.3, rich 10.1) | Prod, private only | Text drafts | Rich draft with status and partial text |
| Stop on a draft: `can_stop`, update `stopped_message_generation` (10.3) | Prod, private only | No | Stop button mapped to `/stop` |
| Widget: one self-contained HTML document, QuickJS, no network, 32768 bytes and 4096 bytes after zlib level 9; `widget_query` back to the bot, 64 bytes, answered within 15 s; bot-written state up to 1024 bytes (10.4) | Prod, private only, not forwardable | No | Agent tool, validated before sending |
| Inline buttons: callback (64 bytes), url, copy_text, colour `style` (9.4) | Prod | System prompts only | Agent tool; taps come back as events |
| Media: photo 10 MB, other files 50 MB by upload; albums of 2-10; captions 1024 | Prod | Via `MEDIA:` | Kept, plus explicit send, albums and as-file |
| Polls and quizzes (12 options) | Prod | No | Agent tool; answers come back as events |
| Stickers: send by `file_id`; `getStickerSet` lists a set's stickers with their emoji | Prod | Inbound only: described once with vision and cached (`gateway/sticker_cache.py`) | Agent tool over a sticker catalog |
| Reactions, message effects, pin, edit, delete, quote replies | Prod | Reactions as status only | Agent tools |
| Topics in private chats (9.3-10.5) | Prod | Yes | Kept |
| Ephemeral messages, guest mode, bot-to-bot, business, checklists | Groups, business or opt-in | No | Out of scope |

## Architecture

### A new plugin that harvests the current one

The plugin is a new platform adapter, `HermesTelegramAdapter`, on the public `BasePlatformAdapter`, registered under the platform name `telegram`. Hermes supports replacing a bundled plugin: user plugins override bundled ones with the same key (`hermes_cli/plugins_discovery.py:222`), and the platform registry keeps the last registration of a name (`gateway/platform_registry.py:406`). The bundled plugin's key is `platforms/telegram`, so the plugin installs at `$HERMES_HOME/plugins/platforms/telegram/`. Slice 0 proves this on the pinned build.

We do not copy the bundled adapter whole. Its transport, authorization and routing code solves real problems, so we take those solutions into this repository where they fit (MIT, with notice and provenance: upstream path and commit in each harvested file's header). Its presentation layer, which is the narrow protocol, we write anew.

| Upstream piece | What it solves | Plan |
|---|---|---|
| `telegram_network.py` | Fallback transport to known IPs when `api.telegram.org` is blocked, DoH discovery | Take as is |
| `telegram_ids.py`, `telegram_entities.py`, `telegram_context.py` | Chat id forms, `text_link` expansion, group addressing | Take as is |
| `update_admission.py`, `telegram_held_inbound.py` | Update dedup across restarts; holding acked updates during reconnects | Take, extended to the new update kinds |
| `telegram_topic_routing.py`, `telegram_topic_transfer.py` | DM topics and their routing | Take as is |
| Polling, webhook and reconnect logic in `adapter.py` | Conflict handling, stalls, pool timeouts | Extract into our transport module |
| Inbound handlers in `adapter.py` | Text batching, photo and album batching, media caching, voice, stickers, locations | Extract |
| Authorization, pairing, profile routing in `adapter.py` | `allow_from`, `dm_policy`, admin commands, `profile_routes` | Extract; this is a compatibility contract |
| Approval, clarify, slash confirm, model picker, `inline_picker.py` | Hermes' own button flows | Extract |
| Send, edit, draft, MarkdownV2 conversion, hint | The narrow protocol | Rewrite |

How slice 0 did it:

- `scripts/harvest.py` generates `hermes_telegram/harvested/` from the pinned source and is the only writer of that directory. Sibling modules are copied whole. The `TelegramAdapter` class (6697 lines, 344 methods) is split into ten mixins, one per concern: core, access, topics, transport, events, prompts, pacing, groups, inbound, and outbound_legacy. Each method lands in exactly one mixin, with its body unchanged.
- The only rewrites are mechanical, and each file's header lists them. Sibling imports become relative. Four references to the class name point at the owning mixin. One `sys.path.insert` is dropped: upstream it added the repository root to the path, but from a user plugin directory it would add `$HERMES_HOME/plugins`.
- `HermesTelegramAdapter` composes the mixins on the bundled class's own bases. Its members equal the upstream class's one for one. `checks/test_harvest.py` compares every member and module with the installed Hermes by AST.
- On a re-harvest after a Hermes upgrade, a method the map does not name stops the run, so a change upstream is assigned deliberately.
- `outbound_legacy` holds today's narrow protocol (about 1700 lines). It stays only so the plugin works at parity from slice 0. Slices 1 to 3 override its public methods with new ones, and the harvest drops what is no longer called.

The measured split by concern: about 5000 of the class's lines are transport, authorization, topics, group gating, inbound handling and Hermes' own prompts, which are kept. About 1700 are the outbound protocol, which is replaced.

### Transport

The plugin stays on python-telegram-bot 22.8, which the Hermes environment already ships, so the harvested code keeps working and other plugins' `register_telegram_handler` handlers, which are PTB handlers (`hermes_cli/plugins.py:848`), keep receiving updates. Methods from Bot API 10.1 on go through `Bot.do_api_request`, as the bundled adapter already does for `sendRichMessage` (`adapter.py:1509`), with our own typed payload builders. Update kinds PTB does not model (`widget_query`, `stopped_message_generation`) are read from the raw update. Verified in slice 0 on PTB 22.8: `Update.de_json` keeps an unknown update kind in `Update.api_kwargs`, and an unknown field of a known type (`Message.rich_message`) in that object's `api_kwargs`. `TypeHandler(Update)` receives such updates, but `effective_chat` is `None` for them, so the adapter reads chat and sender from the raw payload. The subscription asks for those update kinds explicitly in `allowed_updates`.

The PTB version is not ours to choose, and there is nothing newer to choose:

- 22.8 is the latest release on PyPI (2026-06-12). Upstream support for Bot API 10.1 is still the open issue python-telegram-bot#5261, and the 10.3 pull request #5359 was closed unmerged.
- The plugin runs inside the Hermes gateway process and imports the same `telegram` package. Hermes pins it exactly, `python-telegram-bot[webhooks]==22.8` (`pyproject.toml:271`), and the host's environment is built from Hermes' lock file by uv2nix. A different version would have to override Hermes' own dependency in `homelab`, for the bundled code and every other plugin too.
- When a PTB release models Bot API 10.x and Hermes moves to it, our raw payload builders can give way to PTB methods one by one. Until then, raw calls are the only path either way.

### What stays coupled

- Harvested code imports about 40 names from Hermes core, some private, for example `gateway.run.cfg_get` and `tools.send_message_tool._send_telegram`. A Hermes upgrade can break them. The integration suite runs against the pinned commit, and an upgrade is a deliberate step: bump the pin, run the suite.
- Sends outside the gateway process (`hermes send`, cron in a separate process) go through the bundled code by import path (`tools/send_message_senders.py:131,253`), not through this plugin. They keep the old formatting. Cron reports delivered by the live gateway use this plugin.
- Installing the plugin without enabling it shadows the bundled plugin and leaves Telegram with no adapter (`checks/test_install.py`, `docs/install.md`). Deployment installs and enables in one step.

## Agent interface

The agent gets three things: answers that Telegram renders natively, tools that act in the current chat, and events that bring the user's actions back.

### Answers

The agent keeps writing ordinary Markdown. The adapter chooses the form:

- An answer with structure (headings, tables, lists, quotes, code, links) is sent with `sendRichMessage` in `markdown` mode. Telegram's rich Markdown is GFM-compatible, so the agent's text passes nearly unchanged instead of the lossy MarkdownV2 conversion.
- A short answer with no structure is one ordinary `sendMessage` (row 16).
- An answer over 32768 characters or 500 blocks is cut at top-level block boundaries into consecutive rich messages, in order (row 1).
- Maths is written in LaTeX, `$...$` inline and `$$...$$` as a block, and Telegram renders it. Because `$` starts inline maths, a literal dollar must not become a formula: "between $5 and $10" is a case slice 1 tests, and the hint tells the agent to escape a literal `$`.
- `MEDIA:` lines and `[[audio_as_voice]]` keep working, because other tools and skills emit them.
- The prompt hint is rewritten: tables, task lists and the tools below are allowed and described.

How slice 1 does it (`hermes_telegram/rich.py`, `hermes_telegram/answers.py`):

- It overrides the predicates and payload of the harvested rich path, and sends every rich answer through its own sender, `_send_items`. The harvested path keeps reply anchors (`reply_parameters`), topics, flood pacing and its MarkdownV2 fallback. Rich is on unless a profile sets `rich_messages: false`; the bundled default was off.
- "Structure" means headings, lists, task lists, quotes, tables, code, links, footnotes, rules, maths, highlights and the allowed HTML tags. Emphasis alone stays an ordinary message, which already shows bold, italic, strikethrough and spoilers.
- The answer is prepared once, then cut. Preparation works outside code (fences, code spans, `<code>`, `<pre>`) and outside maths. `\(...\)` and `\[...\]` become `$...$` and `$$...$$`. A `$` that opens no maths becomes `\$`: the rule is Pandoc's, made stricter so that the first `$` after an opening one must close it, which means "$5 and $10" stays two prices. A `<` that starts none of the allowed tags (`<u>`, `<details>`, `<tg-spoiler>` and the like) or an autolink becomes `\<`. A line break inside a paragraph becomes a hard break. Code and formula source arrive exactly as written. `<tg-button>` is not allowed in text, because buttons come from the tools, which own their callback data. On the ordinary MarkdownV2 path, the hint's `\$` is turned back into `$`.
- The cut keeps fenced code (closing and reopening it if it must cut inside), table headers (repeated), maths and details blocks, list items and footnote definitions (copied to the part that references them). It works within 32000 characters and an estimated 400 blocks; Telegram does not publish its block count.
- A part Telegram refuses was not delivered. It is cut in half and resent, twice at most, then sent as MarkdownV2 chunks rather than lost. Once the rich endpoint is gone (the capability latch), the remaining parts go as MarkdownV2.
- A failure after some parts landed returns Hermes' `partial_overflow` result. The unsent parts are kept for resuming only when the failed part was certainly not sent: a flood wait (which also arms the chat's cooldown), or a connection that never opened. After a timeout or a lost response nothing is resent, as Hermes' resume contract requires.
- Streamed answers still take the harvested path when the gateway edits a preview into the final answer, or seals heads of a long answer while it streams. Slice 3 owns those (docs/review-log.md, slice 1).
- The bundled guards for Telegram Desktop stay. Text with CJK characters, and maths inside `<details>`, still take the MarkdownV2 path (tdesktop#30808, Hermes #47653). This plugin does not prove those client bugs; it keeps the bundled behaviour.

### Tools

Toolset `telegram`. Every tool acts on the chat and topic of the turn that calls it, read from the turn's session context (`gateway/session_context.py:131`); none takes a chat id. A profile gets them only if its toolsets include `telegram`; the `family` profile lists its toolsets explicitly, so it gets none until the owner adds it.

Hermes forbids plugins to give the model a tool that lets it send messages on its own (`website/docs/developer-guide/adding-platform-adapters.md:330`). Its send engine takes any platform and chat as the target, so a model holding it could be talked by injected content into writing to other people, or could write when nobody asked. These tools keep both out of the model's hands:

- The model never names the recipient. The target is the chat the user wrote from, the same one its final answer goes to.
- The tools exist only during a turn that a user's message, tap or widget request started in a Telegram chat. They are unavailable in cron runs, the CLI and other surfaces.
- A turn may send at most a bounded number of extra messages (a configured cap, 10 by default). Past it, the tool returns an error to the agent and sends nothing.

| Tool | Does | Returns |
|---|---|---|
| `telegram_status(text)` | Sets the preview's status line; the default is `Contemplating...` | ok |
| `telegram_send(text?, files?, as_files?, buttons?, reply_to?)` | Sends one message now: a rich body, attachments (several photos go as one album, more than 10 as consecutive albums), buttons | message ids |
| `telegram_widget(html, caption, height?)` | Sends a widget after checking its size, its compressed size and the sandbox rules (no network APIs, no external resources) | message id, or the reason it was refused |
| `telegram_poll(question, options, quiz_answer?, multiple?)` | Sends a poll or a quiz | message id |
| `telegram_react(emoji, message_id?)` | Reacts to a message, by default the user's current one | ok |
| `telegram_sticker(op, query? \| sticker_id?)` | `find` searches the sticker catalog by emoji or description; `send` sends one sticker from it | candidates, or a message id |
| `telegram_message(op, message_id, ...)` | Edits, deletes, pins or unpins one of the bot's messages in this chat, or updates a widget's bot-written state | ok |

A button is `{label, value?, url?, copy?, style?}`. A button with `value` is an action: its tap comes back to the agent. `url` opens a link, `copy` copies text.

The sticker catalog has two sources. One is the stickers users sent to the bot: today Hermes stores only their vision description, emoji and set name (`gateway/sticker_cache.py:60`), so the plugin also records each one's `file_id`. The other is sticker sets the profile names in its configuration, read with `getStickerSet`; their stickers carry an emoji and the set title, but no description. A sticker id the agent gets from `find` is valid only for this bot.

### Events

A user's action becomes an ordinary inbound message in the same session, from the user who acted, in a fixed form the hint documents:

- `[button] "<label>" value="<value>" on message <id>`
- `[widget] data="<data>" on widget <id>`
- `[poll] "<question>": "<option>"[, ...]`
- `[reaction] <emoji> on message <id>`, for reactions to the bot's messages only

Rules:

- An action is authorized and routed exactly like a text message from that user in that chat, through the same admission path. A tap by the routed `family` user reaches the `family` profile, not the default one.
- `callback_data` holds at most 64 bytes, so the adapter gives each button a short token and keeps the label and value under it. Tokens are stored under the profile's Hermes home, bounded in number, so a tap on an old message still resolves after a restart. A token it does not know gets an "expired" notice.
- Each callback, widget query and update is handled once: a redelivered copy is dropped. A button set marked single-use answers its second tap with "already answered" and sends nothing to the agent (row 11).
- A widget query is answered at once with an acknowledgement, because Telegram allows 15 s. The agent's real answer arrives as a chat message, or as new widget state through `telegram_message`.
- Pressing Stop is not an event for the agent: it stops the turn (next section).

## Turn lifecycle

States of the preview of one turn in a private chat: `thinking`, then `streaming`, then exactly one of `final`, `stopped` or `failed`.

- `thinking`: when processing starts, the adapter sends a rich draft with a `<tg-thinking>` block, the status text (`Contemplating...` or the last `telegram_status`) and `can_stop`.
- `streaming`: the gateway's stream consumer calls `send_draft` with the partial answer; the draft shows the status and the partial text (row 7). A draft lives about 30 s, so the adapter refreshes it before that while the turn runs, even with nothing new.
- `final`: the answer is sent as described above. Sending a message removes the draft (row 8).
- `stopped`: Telegram sends `stopped_message_generation` with the chat, topic and `draft_id`. The adapter maps the draft to its session and dispatches `/stop` through the gateway's normal command path, which interrupts a running or hung agent (`gateway/slash_commands.py:430`). Then it sends "Stopped" and the partial text, if any (row 9).
- `failed`: on a failed processing outcome the adapter sends one failure message (row 10).

Any message the bot sends during a turn, such as an approval prompt or a cron report, removes the draft. The adapter shows the draft again after it, so the preview and the other message both stay (row 17).

Drafts exist only in private chats. Groups and channels keep the typing indicator and the final answer; they are out of scope (PRD).

## Testing

- Unit tests cover pure logic: Markdown splitting within Telegram's limits, payload builders, widget validation, button tokens.
- Integration tests run the real Hermes at `c419cf7` and the real PTB 22.8, installed as development dependencies at those pins. The adapter is built through Hermes' own plugin loading. There are no hand-written stand-ins for Hermes: six open findings of the Prism attempt are drift between its stand-ins and the real code (lumi-telegram issues #13, #15, #20, #30, #31, #46).
- End-to-end checks reuse Hermes' own messaging-adapter contract (`tests/e2e/core/platforms/` at the pin). It starts a real `hermes gateway run` child with a fake model and Hermes' Bot API stand-in (`tests/fakes/platforms/telegram_standin.py`) behind the PTB base URL. `checks/e2e/` runs its scenarios unchanged against this plugin. The stand-in answers only the methods the bundled adapter calls and returns 404 for anything else. The slices that add methods (`sendRichMessage`, `sendRichMessageDraft`, `sendWidget`, `answerWidgetQuery`, `sendPoll`) extend it in a subclass in this repository, with each method checked against its documented limits.
- Live acceptance is done by the owner on `vtadevbot`, with a checklist per slice. The bot token stays in 1Password and on the host, never in this repository or in a test.

## Slices

Each slice is one PR to `main`, reviewed by the owner.

0. Skeleton and parity. The plugin loads as `telegram` and shadows the bundled copy. Harvested transport, authorization, routing and Hermes' own button flows work. The integration harness exists. Checked by: harvested code equal to upstream member by member, the registration equal to the bundled one, Hermes' plugin discovery picking this plugin, and Hermes' Telegram contract suite against it. Clarify, cron delivery and the `family` route have no scenario in that suite. Their code is harvested unchanged, so they get end-to-end checks in the slices that change the paths they use (3 and 4).
1. Rich answers: rows 1, 2, 16.
2. Files: rows 3, 4, 5, 15, and the cause of today's failed sends.
3. Lifecycle: rows 6 to 10, 17, and streamed answers as rich messages: block-aware heads while a long answer streams, and a rich final in parts where the gateway edits a preview.
4. Buttons, polls, reactions, stickers and their events: row 11.
5. Widgets: rows 12 to 14.
6. The rest of the agent surface: effects, pins, edits, link previews, quote replies.

## Decisions and open questions

Decided by the owner on PR #1, 2026-11-25:

1. Maths renders as LaTeX ("1 - LaTeX"). Row 1 is amended accordingly.
2. The tool set stands, plus stickers ("Норм, я бы еще стикеры добавил бы").
3. The three guards under Tools (no recipient chosen by the model, only in user-started Telegram turns, a cap per turn) are the accepted reading of Hermes' rule against a general send tool ("Yes, guards are fine").

No questions are open.

## Out of scope

As in the PRD: group chats and channels, business checklists, a hosted Mini App, a general send tool, new restrictions or routing, fallbacks for clients that cannot show a native form, and deployment. Infrastructure installs a reviewed version of this plugin on the host.
