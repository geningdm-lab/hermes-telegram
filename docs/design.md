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

Not verified: why file sends fail today (PRD Q12: "было много фейлов при отправке. Надо копать чаты"). That needs the gateway logs on the host; the likely cause is under Files.

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
- The answer is prepared once, then cut. Preparation works outside code (fences, code spans, `<code>`, `<pre>`, and a top-level block indented four spaces after a blank line) and outside maths. `\(...\)` and `\[...\]` become `$...$` and `$$...$$`. A `$` that opens no maths becomes `\$`: the rule is Pandoc's, made stricter so that the first `$` after an opening one must close it, which means "$5 and $10" stays two prices. A `<` that starts none of the allowed tags (`<u>`, `<details>`, `<tg-spoiler>` and the like) or an autolink becomes `\<`. A line break inside a paragraph becomes a hard break. Code and formula source arrive exactly as written. `<tg-button>` is not allowed in text, because buttons come from the tools, which own their callback data. On the ordinary MarkdownV2 path, the hint's `\$` is turned back into `$` outside code, and a part that falls back to MarkdownV2 loses the escapes preparation added.
- The cut keeps fenced code (closing and reopening it if it must cut inside), table headers (repeated), maths and details blocks, list items and footnote definitions (copied to the part that references them). It works within 32000 characters and an estimated 400 blocks; Telegram does not publish its block count.
- A part Telegram refuses was not delivered. It is cut in half and resent, twice at most, then sent as MarkdownV2 chunks rather than lost. Once the rich endpoint is gone (the capability latch), the remaining parts go as MarkdownV2.
- A failure after some parts landed returns Hermes' `partial_overflow` result. The unsent parts are kept for resuming only when the failed part was certainly not sent: a flood wait (which also arms the chat's cooldown), a connection that never opened, or no free pooled connection (PTB says such a request was not sent). A part that may have been shown (a timeout, a connection lost after the request went out) is never sent again: the result is marked `ambiguous_delivery`, and `_send_retry_is_final` stops the base retry from resending the whole answer or its plain-text fallback. A flood wait PTB gives as a `timedelta` is counted in seconds. After a chunk had to leave a forum topic Telegram no longer has, the rich parts after it leave the topic too.
- Streamed answers still take the harvested path when the gateway edits a preview into the final answer, or seals heads of a long answer while it streams. Slice 3 owns those (docs/review-log.md, slice 1).
- The bundled guards for Telegram Desktop stay. Text with CJK characters, and maths inside `<details>`, still take the MarkdownV2 path (tdesktop#30808, Hermes #47653). This plugin does not prove those client bugs; it keeps the bundled behaviour.

### Files

What the user sees (rows 3, 4, 5, 15):

- A document, photo, video, audio file or voice message arrives as its kind. Several photos arrive as one album, more than 10 as consecutive albums of up to 10, in order.
- A file sent "as a file" arrives as a document, with its name and bytes unchanged, even when it is a photo, a video or a GIF.
- A file that cannot be delivered is named in the chat with the reason: over Telegram's upload limit for bots (with its size against the limit), refused by Telegram (with Telegram's words), missing on the Hermes host, or not allowed by Hermes' delivery policy. Never silence.

How slice 2 does it (`hermes_telegram/files.py`, `hermes_telegram/delivery.py`, `hermes_telegram/tools.py`, `hermes_telegram/turn.py`):

- The harvested senders already deliver each kind natively. `FilesMixin` overrides them only to add what the rows need: `disable_content_type_detection` on documents, a size check against the upload limit for bots before uploading (50 MB, or 2000 MB with a local Bot API server, `local_mode`), and a notice naming the file and the reason when it is not delivered. A photo is measured as it is uploaded, after the harvested sender's JPEG compression. Each send keeps its own upload error, so a notice quotes that file's reason.
- A notice is sent with `send`, not Hermes' `emit_warning`, because a profile can turn warnings off (`warning_notifications_enabled`). It counts as given only once it is sent; one refused by a flood wait is sent after the wait.
- Photos go through this plugin's own album sender (`send_photos`): albums of 2 to 10 in order, 11 as 9 and 2, a lone photo as a photo, and a receipt per photo with its message id. An album Telegram refused, or that certainly never left, is sent photo by photo. An album whose reply was lost is not, because Telegram may show it already; the chat is told it may or may not have arrived.
- `MEDIA:` lines keep working. A path Hermes would drop, because it is not on this host, a Docker mount or the remote sandbox, or because its delivery policy denies it, is only logged by Hermes (`_validated_delivery_path`: "Skipping MEDIA directive path"); the chat saw nothing. `extract_media` resolves each path with Hermes' own `filter_media_delivery_paths` and the session key Hermes uses for the response, passes a resolved path on resolved, and swaps a dropped one for a small notice file that Hermes does deliver; `send_document` posts the notice instead of uploading it.
- `telegram_send(files, as_files?, caption?)` sends 1 to 20 files now, in the turn's chat and topic, in the order given, as their kind or, with `as_files`, as documents with the name the agent gave. It resolves paths as Hermes resolves `MEDIA:` paths. It reserves its whole plan (an album counts as one message) plus one notice against the turn's cap before sending anything, so a call the turn cannot hold sends nothing. Files that did not arrive are named in one message, and the result lists each file with its message id or reason.
- The turn guards: the adapter records each human Telegram event it processes (`HumanTurnsMixin.on_processing_start`), with the routing metadata Hermes builds for it, and forgets it when processing ends. A tool acts only for such a record, through its metadata, so a DM-topic send keeps the topic or fails; the cap is counted on the record. Loop and goal wakeups are internal events with no message id and get no record.
- The hint tells the agent to call `telegram_send` to send files now, or a photo, video or recording as a downloadable file, and keeps the bundled `MEDIA:` description for everything else.

Today's failed sends (PRD Q12) are not confirmed: that needs the gateway logs on the host, and the owner deferred it (Decisions and open questions, 4). The likely cause is the silent drop above: a `MEDIA:` path the agent wrote that is not on the gateway's host, or not allowed, is logged at INFO and never mentioned in the chat. Uploads over 50 MB would also fail, as a refusal from Telegram. Slice 2 names both in the chat, so the next failure says which it is.

### Tools

Toolset `telegram`. Every tool acts on the chat and topic of the turn that calls it, read from the turn's session context (`gateway/session_context.py:131`); none takes a chat id. A profile gets them only if it enables this plugin: plugin tools live in an overlay per profile, keyed by its Hermes home (`tools/registry.py:449`), and the `family` profile does not enable it. Within a profile that does, a plugin toolset is on by default, even next to an explicit toolset list, until `hermes tools` saves a list without it (`hermes_cli/tools_config.py:520`). The tools also check, on every call, that the turn is a Telegram chat turn.

Hermes' tool search (`tools.tool_search`, on by default) puts every plugin tool behind its `tool_search`, `tool_describe` and `tool_call` bridge (`tools/tool_search.py:150`). The model then sees a catalog entry and calls `tool_call` with the tool's name and arguments. Slice 2 keeps that default; whether the `telegram` tools should be listed directly, at the cost of their schemas in every prompt, is open (Decisions and open questions).

Hermes forbids plugins to give the model a tool that lets it send messages on its own (`website/docs/developer-guide/adding-platform-adapters.md:330`). Its send engine takes any platform and chat as the target, so a model holding it could be talked by injected content into writing to other people, or could write when nobody asked. These tools keep both out of the model's hands:

- The model never names the recipient. The target is the chat the user wrote from, the same one its final answer goes to.
- The tools exist only during a turn that a user's message, tap or widget request started in a Telegram chat. They are unavailable in cron runs, the CLI and other surfaces.
- A turn may send at most a bounded number of extra messages (a configured cap, 10 by default). Past it, the tool returns an error to the agent and sends nothing.

| Tool | Does | Returns |
|---|---|---|
| `telegram_status(text)` | Sets the preview's status line; the default is `Contemplating...` (slice 3) | whether a preview shows it |
| `telegram_send(text?, files?, as_files?, buttons?, reply_to?, quote?, link_preview?, effect?)` | Sends one message now: a rich body, attachments (several photos go as one album, more than 10 as consecutive albums), buttons; the text may reply with a quote, set link preview options and carry an effect (slice 6) | message ids |
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

How slice 6 does it (`hermes_telegram/messages.py`, `hermes_telegram/buttons.py`, `hermes_telegram/tools.py`):

- `telegram_message` changes only the bot's own messages in the turn's conversation, by the bot-message index of slice 4 (the chat and topic each was sent to), since Telegram's methods take a chat and a message but no topic. The index now also keeps each message's kind (`text`, `media`, `prompt`, `widget`) and a tool message's keyboard.
- `op=edit` replaces the text of a text message with `editMessageText`, decided as `send_single` decides (rich when an answer would be, else MarkdownV2, one message long). Its keyboard stays unless new buttons are given; new buttons retire the old set, whose taps then answer "expired". "message is not modified" is an edit made. Files, polls, stickers, widgets and Hermes' prompts are not edited.
- `op=delete` deletes the message and forgets it; `op=pin` pins it silently unless `notify`; `op=unpin` unpins it. None of these counts towards the cap.
- `telegram_send` with text: `reply_to` replies to the user's current message (the default for a `quote`) or one of the bot's messages in this conversation; `quote` is `ReplyParameters.quote`, which Telegram refuses unless it is an exact part of that message, and the refusal comes back to the agent. `link_preview` (disabled, url, small, large, above) becomes LinkPreviewOptions on a MarkdownV2 message, defaulting to the profile's `disable_link_previews`; `sendRichMessage` takes no link preview options, and the tool result says so when the text went rich. `effect` is `message_effect_id`, private chats only.
- Effects: the Bot API has no method that lists them. `EFFECTS` names six ids that Telegram's clients offer (🔥 👍 👎 ❤️ 🎉 💩); they are client evidence, not documentation, and their delivery is live acceptance. Any other numeric id is passed on, and a refusal comes back to the agent. Owner question, open: keep the six ids, or take raw ids only.

How slice 5 does it (`hermes_telegram/widgets.py`, `hermes_telegram/tools.py`):

- `telegram_widget(html, caption?, height?, state?)` sends a widget with `allow_queries`, after `check_widget` holds the HTML to Telegram's limits as the server counts them (at most 32768 bytes of UTF-8 and 4096 bytes after zlib at level 9, no NUL) and to the widget profile: no network API called by its code (string literals and comments do not count), no image but a `data:` PNG, JPEG, GIF or WebP or inline SVG, no external script, stylesheet, frame or media element. Text that names an API passes, and so do links, which open on a tap. A refusal goes back to the agent with the reason. `state` is NUL-free UTF-8 of at most 1024 bytes, checked the same way for a send and an edit; `height` 0 is Telegram's automatic height. Private chats only, as in production Telegram; a widget counts towards the cap, and an uncertain send keeps its slot as in slice 4.
- Each widget is kept with its topic and profile in `telegram_widgets.json`, which outlives a restart; WidgetQuery carries the chat and the message, not the topic.
- `widget_query` is asked for alongside the Stop button's update (`EXTRA_UPDATE_KINDS`). Each query is answered first, without data, since Telegram allows 15 s; a flood wait or a connection that never opened is answered again inside that time, a timeout after the request went out is not. Then, for one of this bot's widgets, in the private chat, from a user the adapter admits as it would admit their text (slice 4's `_actor_admitted`), it reaches the agent as `[widget] data="<data>" on widget <message id>` in the widget's topic, built as a tap is (an id of its own, the answer replying to the widget). Every query reaches the agent once: the same query id seen again is dropped, and a second query with the same data is a second action. Telegram marks no request as a background one, so the tool's description and the hint tell the agent to request on the user's action, never on a timer.
- `telegram_message(op=widget_state, message_id, state)` sets the chat state of one of the bot's widgets in the same conversation (`editMessageWidgetData`), which the widget redraws from. `state` is required; `""` removes it. A state the widget already has is a change made: Telegram's "message is not modified" is success.
- Row 13 (interaction inside a widget reaches nobody) holds by construction: only `TelegramWidget.request` leaves the device. How clients draw widgets is live acceptance.

How slice 4 does it (`hermes_telegram/buttons.py`, `hermes_telegram/actions.py`, `hermes_telegram/tools.py`, `hermes_telegram/turn.py`):

- `telegram_send` takes `text` and `buttons` (one per row; each a choice with `value`, a link with `url`, or `copy`; an optional `style`) and `single_use` (default true). The text is one message, never several (`send_single`): a rich message when the answer path would send it rich (the profile's `rich_messages` and Telegram Desktop's guards included), else MarkdownV2; a text longer than one message is refused unsent, so the cap's one slot holds. It follows the profile's notification policy.
- A choice's callback data is `tb:<token>`; the token's label, value and set are kept in `telegram_buttons.json` under the adapter's Hermes home, at most 4000 tokens. A set is bound, once sent, to its message, topic and the profile that sent it: a token presented with another message, topic or profile is "This choice has expired" (Telegram does not promise a query's message holds a button with that data). A store that cannot be saved stops the send or the tap; nothing in memory alone reaches the chat or the agent.
- Taps, votes and reactions are accepted in private chats only, from a user the adapter admits as it would admit their text: its `allow_from` when set, else the gateway's chain, failing closed. That check comes before the set changes. A tap is claimed with no await between the check and the mark, so two taps cannot both be first; a single-use set answers later taps with "Already answered". Telegram's acknowledgement of the tap is best effort; a tap whose hand-off to the agent fails is released.
- A tap, a poll answer or a reaction becomes an inbound message built with `build_source` from the user who acted, in the chat and topic acted in, with the topic's skill and prompt as a text there gets them, passed to `handle_message`. It has an id of its own (`cb<callback id>`, `pa<update id>`, `re<update id>`): reusing the bot message's id made Hermes drop a second tap. The answer still replies to the message acted on (`reply_anchor_override`). Quoted fields are JSON strings, so a quote in an option cannot read as two options.
- `telegram_poll` sends a non-anonymous poll, so Telegram reports who answered, or a quiz with `correct_option_ids` (Bot API 9.x renamed the field; python-telegram-bot 22.8 knows only the old name, so it goes as an extra argument). Polls are kept in `telegram_polls.json` with their chat, topic and profile; a vote that arrives before `sendPoll` has returned waits up to 5 s for that record.
- `telegram_react` sets an emoji reaction on the user's current message or on one of the bot's messages in the same conversation (Telegram takes only the chat and the message, so another topic's message is refused). Hermes' processing reactions (`extra.reactions`) leave a message the agent reacted to alone. Reactions to the bot's own messages come back to the agent, in the topic the message was sent to. The bot's messages are kept with their topic in `telegram_bot_messages.json` as they are sent, every delivered part of a long answer and every control prompt included, so this works after a restart. Telegram documents `message_reaction` for chats where the bot is an administrator; whether it arrives in a private chat is live acceptance.
- `telegram_sticker`: `find` searches the catalog (the stickers users of the caller's profile sent, recorded with their `file_id` after Hermes describes them, and the stickers of `extra.sticker_sets`, read with getStickerSet and read again a minute after Telegram failed) by emoji, description words and set name; `send` sends one of them. A sticker or a poll counts towards the cap; a reaction does not, as it is no message.
- A tool's send keeps the turn's topic: while it runs (`TOOL_SEND`), ordinary delivery's fallbacks to the root chat (a media send retried without its topic when the reply anchor or the topic is gone, a text sent without its forum topic) do not apply, and the send fails. A send that may have reached Telegram (a timeout after the request went out) keeps its slot and tells the agent not to send it again.

## Turn lifecycle

States of the preview of one turn in a private chat: `thinking`, then `streaming`, then exactly one of `final`, `stopped` or `failed`.

- `thinking`: when processing starts, the adapter sends a rich draft with a `<tg-thinking>` block, the status text (`Contemplating...` or the last `telegram_status`) and `can_stop`.
- `streaming`: the gateway's stream consumer calls `send_draft` with the partial answer; the draft shows the status and the partial text (row 7). A draft lives about 30 s, so the adapter refreshes it before that while the turn runs, even with nothing new.
- `final`: the answer is sent as described above. Sending a message removes the draft (row 8).
- `stopped`: Telegram sends `stopped_message_generation` with the chat, topic and `draft_id`. The adapter maps the draft to its session and dispatches `/stop` through the gateway's normal command path, which interrupts a running or hung agent (`gateway/slash_commands.py:430`). Then it sends "Stopped" and the partial text, if any (row 9).
- `failed`: on a failed processing outcome the adapter sends one failure message (row 10).

Any message the bot sends during a turn, such as an approval prompt or a cron report, removes the draft. The adapter shows the draft again after it, so the preview and the other message both stay (row 17).

Drafts exist only in private chats. Groups and channels keep the typing indicator and the final answer; they are out of scope (PRD).

How slice 3 does it (`hermes_telegram/preview.py`, `hermes_telegram/updates.py`):

- Telegram delivers only the update kinds a bot asks for, and the harvested poller asked for python-telegram-bot 22.8's list, which predates the Stop button. `UpdateKindsMixin` overrides the polling and webhook start with copies that also ask for `stopped_message_generation`; a check holds each copy equal to the harvested method apart from that one argument.
- `PreviewMixin` keeps one preview per private chat and topic, for a message from a user the adapter admits (the same check a button tap gets), from `on_processing_start` to `on_processing_complete`. A frame is the status in a `<tg-thinking>` block, then the part of the answer not yet in the chat, prepared as a rich message, sent with `sendRichMessageDraft` and `can_stop`. It is a plain `sendMessageDraft` for Telegram Desktop's guarded shapes (maths in `<details>`, CJK), or when Telegram refuses the rich frame. The frame is sent again every 20 s while the turn runs; a flood refusal holds every frame until it passes.
- `send_draft` (the stream consumer's partial answer) updates the frame. A frame Telegram did not take this time is shown by the next one; a refused plain frame means the topic takes no drafts, and `send_draft` then reports failure so Hermes streams by editing messages instead.
- The final: Hermes pauses typing, one flag per chat, right before it delivers a turn's final. That marks a pause pending on the chat's previews; the next message to a topic is that topic's final, waits for any frame on its way, and no frame of that preview follows it. A stream still sending frames is not the one finalizing. Any other message during the turn (a cron report, a tool's file, a notice, a part the stream sealed) shows the preview again 1 s later, without the part already in the chat. An approval or clarify prompt also pauses typing but is never the final: the preview comes back under it.
- Stop: the press of a turn's own draft, by a user admitted now, dispatches `/stop` as that user through `handle_message`, which sends Hermes' "Stopped" reply and then cancels the turn (`_dispatch_active_session_command`); that reply is kept even under `display.ephemeral_system_ttl`. The preview stops changing at once. Hermes' stream consumer drops a draft's partial text on cancel (`stream_consumer._on_cancelled`), so the adapter sends the part not yet in the chat itself, after "Stopped", with Hermes' send retry.
- Failure: Hermes' failure reply is the turn's final; Hermes also shows its own diagnostic line ("❌ ... rejected the request ...") unless the profile sets `display.suppress_warning_notifications`. Row 10's acceptance profile sets it; the end-to-end check runs with it.
- A long answer streaming past the rich overflow limit: the stream consumer cuts it with the adapter's `truncate_message`, which cuts a rich answer as `rich.split_markdown` does, in the consumer's length units, and leaves the parts as the agent wrote them (the consumer keeps appending to the last). The heads it seals are never edited afterwards and go as parts of a rich answer, recognised by their text, once.
- A private chat set to `streaming.transport: edit` streams by editing messages: when the stream sends a message it will edit, the preview stops, and the final is Hermes' edited message (docs/review-log.md, slice 3).
- `telegram_status(text)` sets the status line of the turn's preview; it sends no message and does not count towards the cap.

## Testing

- Unit tests cover pure logic: Markdown splitting within Telegram's limits, payload builders, widget validation, button tokens.
- Integration tests run the real Hermes at `c419cf7` and the real PTB 22.8, installed as development dependencies at those pins. The adapter is built through Hermes' own plugin loading. There are no hand-written stand-ins for Hermes: six open findings of the Prism attempt are drift between its stand-ins and the real code (lumi-telegram issues #13, #15, #20, #30, #31, #46).
- End-to-end checks reuse Hermes' own messaging-adapter contract (`tests/e2e/core/platforms/` at the pin). It starts a real `hermes gateway run` child with a fake model and Hermes' Bot API stand-in (`tests/fakes/platforms/telegram_standin.py`) behind the PTB base URL. `checks/e2e/` runs its scenarios unchanged against this plugin. The stand-in answers only the methods the bundled adapter calls and returns 404 for anything else. The slices that need methods it lacks (`sendRichMessage`, `sendVideo`, `sendAudio`, `sendVoice`, `sendAnimation`, `sendMediaGroup`, then `sendRichMessageDraft`, `sendWidget`, `answerWidgetQuery`, `sendPoll`) extend it in a subclass in this repository, with each method checked against its documented limits. The rich sender's failure handling runs a real PTB `Bot` against the stand-in in process, with connection failures injected in httpx's transport (`checks/e2e/test_sender.py`).
- Live acceptance is done by the owner on `vtadevbot`, with a checklist per slice. The bot token stays in 1Password and on the host, never in this repository or in a test.

## Slices

Each slice is one PR to `main`, reviewed by the owner.

0. Skeleton and parity. The plugin loads as `telegram` and shadows the bundled copy. Harvested transport, authorization, routing and Hermes' own button flows work. The integration harness exists. Checked by: harvested code equal to upstream member by member, the registration equal to the bundled one, Hermes' plugin discovery picking this plugin, and Hermes' Telegram contract suite against it. Clarify, cron delivery and the `family` route have no scenario in that suite. Their code is harvested unchanged, so they get end-to-end checks in the slices that change the paths they use (3 and 4).
1. Rich answers: rows 1, 2, 16.
2. Files: rows 3, 4, 5, 15, and the cause of today's failed sends.
3. Lifecycle: rows 6 to 10, 17, and streamed answers as rich messages: block-aware heads while a long answer streams. A final the gateway edits into a preview happens only where there are no drafts, in groups, which are out of scope. Clarify gets its end-to-end check here, as it uses the prompt sender this slice wraps; a cron report during a preview is checked at the adapter, since it is an ordinary send. The `family` route is checked in slice 4, which routes taps.
4. Buttons, polls, reactions, stickers and their events: row 11. The `family` route of a tap is checked at the adapter (the tap's source is built from the user who tapped, which is what the gateway's profile routes read); a run through a multiplexed gateway is live acceptance.
5. Widgets: rows 12 to 14.
6. The rest of the agent surface: effects, pins, edits, link previews, quote replies.

## Decisions and open questions

Decided by the owner on PR #1, 2026-11-25:

1. Maths renders as LaTeX ("1 - LaTeX"). Row 1 is amended accordingly.
2. The tool set stands, plus stickers ("Норм, я бы еще стикеры добавил бы").
3. The three guards under Tools (no recipient chosen by the model, only in user-started Telegram turns, a cap per turn) are the accepted reading of Hermes' rule against a general send tool ("Yes, guards are fine").

Deferred by the owner, 2026-11-25:

4. The cause of today's failed file sends (PRD Q12). The gateway logs are not available now ("Прямо сейчас доступ к логам не могу дать. Пока можешь конкретно эту проблему с файлами задеферить"). Slice 2 keeps the likely cause under Files as a hypothesis and makes either cause visible in the chat; confirming it waits for the host's gateway log (`Skipping MEDIA directive path`, or Telegram's refusal).

Decided by the owner in chat, 2026-11-25:

5. The `telegram` tools in a Telegram session. The owner: the session knows it is a Telegram one, so that knowledge should be in its context ("сессия знает когда она пользуется телеграмом и тогда у него должны были эти знания в контексте"). Hermes at the pin has no per-plugin switch to keep a plugin's tools out of tool search (`tools/tool_search.py:150-162`; the only switch is `tools.tool_search.enabled: off`, which makes every tool eager). In a Telegram session the knowledge is in context already: the platform hint names `telegram_send` and `telegram_status`, and the tool search listing carries each deferred tool's name and first sentence; only the full schema loads on the first call. Asked between keeping that, turning tool search off for the profile, and asking Hermes upstream for a per-plugin option, the owner chose to keep the bridge.
6. After Telegram's reply to a final answer is lost, Hermes' delivery ledger resends it later with a "may be a duplicate" marker. The owner: keep it, a resend is better ("согласен, лучше переотправить"). The finding disputed in the slice 2 review stays as it is (docs/review-log.md).

Open, asked 2026-11-25:

7. Message effects (slice 6). The Bot API documents `message_effect_id` but no list of effects. The plugin ships the six ids Telegram's clients offer for 🔥 👍 👎 ❤️ 🎉 💩, marked unverified until live acceptance, and also takes raw ids. The alternative is raw ids only, which leaves the agent guessing ids.

## Out of scope

As in the PRD: group chats and channels, business checklists, a hosted Mini App, a general send tool, new restrictions or routing, fallbacks for clients that cannot show a native form, and deployment. Infrastructure installs a reviewed version of this plugin on the host.
