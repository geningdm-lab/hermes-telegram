# Design: Business inbox

Status: design approved (PR #15, 2026-11-27); slice 1 built. Date: 2026-11-27. A feature of its own, next to the presentation design in [design.md](design.md), and not a Prism attempt.

## Why

The owner wants Lumi to keep the messages people send to the owner's personal Telegram account, without answering them, so that the owner's knowledge base can be filled from them later. The owner's words:

> Мне нужно что бы у меня все сообщения не от владельца но в режиме ДМ форвардились в отдельную очередь не обрабатывались и могли быть разобраны профилем.

> я хочу по факту именно складывать сообщения в очередь себе и наполнять базу знаний. Не что бы Люми отвечала

The owner split the work into four flows. This document is the first one only:

1. This plugin collects a short-lived base of the owner's private chats through Telegram Business. No model reads it here.
2. `hermes-mail`: one gate for mail access.
3. A fact aggregator that reads the base (and mail) and extracts facts with a model that has no tools.
4. `proactive-assistance` (in `geningdm-lab/lumi`), which already turns Telegram DMs and Gmail into wiki topics.

Flows 2 to 4 have no settled scope yet. What this flow owes them is a stable read contract, so they never depend on how the base is stored.

## What the owner sees

- The owner connects the bot to their own account in Telegram (Settings, Business, Chatbots) and picks the chats it may see.
- Messages in those chats, both the owner's and the other person's, are kept for at most 72 hours and at most 1000 per chat, with their edits and deletions. Photos, voice and files are described, never downloaded.
- Lumi never answers, reads, edits or deletes anything in those chats, and nothing about them appears in any chat. Direct chats with the bot, the owner's and those routed to another profile, work exactly as before.
- The base is read only with `hermes telegram inbox ...` on the host, which prints JSON. The agent has no tool for it.

## Acceptance

Offline means the checks in `checks/` against the pinned Hermes and PTB with Telegram replaced by the Bot API stand-in. Live means the owner's account connected to `vtadevbot`, production Telegram, one chosen chat. A passing test is not acceptance.

| Row | Behaviour | Proved by |
|---|---|---|
| B1 | A text from the other person in a connected chat is stored as `author: contact`; no agent turn starts and the bot sends nothing | Offline, live |
| B2 | A message the owner writes in that chat is stored as `author: owner`; no agent turn | Live (Telegram does not document that it delivers the owner's own messages; see What Telegram offers) |
| B3 | An edit adds a version; a deletion marks the record deleted and keeps its text until it expires | Offline, live |
| B4 | A photo, video, voice, document or sticker is stored as metadata; the file is never fetched (no `getFile`) | Offline |
| B5 | No record older than `retention_hours` (72) survives a purge; a chat holds at most `max_messages_per_chat` (1000); both come from config, and an environment variable overrides each | Offline |
| B6 | Updates of a connection whose owner is not configured store nothing | Offline |
| B7 | On the business path the bot calls no Bot API method but `getBusinessConnection` | Offline |
| B8 | Every `hermes telegram inbox` command prints JSON with `contract_version: 1`; `changes` reports `gap: true` when a purge removed changes after the caller's cursor | Offline |
| B9 | The same update delivered twice leaves one record | Offline |
| B10 | Direct messages to the bot, routes to other profiles, taps, polls and the Stop button behave as before: the existing suite passes unchanged | Offline |

## What exists today

Verified at `hermes-agent@28ef6e4` and this repository's `main` (`v0.2.0`, 8254915), which `homelab` deploys (`nix/flake.lock` at homelab `ac6764d`):

- Hermes drops business updates before any handler acts on them: `effective_update_message` returns nothing for `business_message` and `edited_business_message` (`hermes-agent@28ef6e4:plugins/platforms/telegram/telegram_intake.py:6`). Its docstring gives the reason: answering such a message as an owner command would send the reply to the other person. Every harvested inbound handler reads the message through it (`harvested/adapter_inbound.py`, `harvested/telegram_ingress.py` and the album, dice and photo-pairing helpers). Today a business update reaches no agent and is stored nowhere.
- The bot already asks Telegram for business updates. `updates.allowed_updates()` is PTB 22.8's `Update.ALL_TYPES` plus this plugin's extra kinds (`hermes_telegram/updates.py:35`), and in PTB 22.8 that list includes `business_connection`, `business_message`, `edited_business_message` and `deleted_business_messages` (checked in this repository's environment).
- The plugin adds its own PTB handlers before the harvested ones: `_register_handlers` puts poll answers in group -9 and reactions in group -8 (`hermes_telegram/actions.py:471`). Its stores are files under the adapter's Hermes home (`self._update_receipt_dir`, `harvested/adapter_core.py:124`), and its settings come from the platform's `extra` (`sticker_sets`, `hermes_telegram/actions.py:438`).
- One bot serves two profiles. The adapter belongs to `default`; `profile_routes` sends one other sender to a second, full profile, and both profiles enable this plugin (homelab's Hermes runbooks).
- A plugin's `hermes <command>` is built at CLI start (`hermes_cli/main.py:3032`). A deferred platform plugin is imported only when the command's name is the platform's name (`_resolve_deferred_platform_cli_command`, `hermes_cli/main.py:2575`), so this plugin's command must be `hermes telegram`. No built-in `telegram` command exists at the pin. Under plugin isolation the command is skipped (`hermes_cli/plugin_isolation.py:56`); homelab asserts in-process plugins (`nix/hermes/telegram/default.nix:49`).

## What Telegram offers

From core.telegram.org, read 2026-11-27.

- Updates (bots/api, Update): `business_connection` (the bot "was connected to or disconnected from a business account", or the connection was edited), `business_message` ("New message from a connected business account"), `edited_business_message`, `deleted_business_messages`.
- `BusinessConnection` (bots/api): `id`, `user` (the account that connected the bot), `user_chat_id`, `date`, `rights`, `is_enabled`. `getBusinessConnection(business_connection_id)` returns it.
- `BusinessBotRights` (bots/api): `can_reply` lets the bot "send and edit messages in the private chats that had incoming messages in the last 24 hours"; `can_read_messages` lets it mark messages read. Neither is needed here. Whether updates still arrive with every right off is not stated; live acceptance runs with them off.
- `BusinessMessagesDeleted` (bots/api): connection id, chat and `message_ids`. It does not say who deleted them, so a record says "deleted", never "deleted by the other person".
- `Message` (bots/api): `business_connection_id`; `sender_business_bot`, "Available only for outgoing messages sent on behalf of the connected business account"; `is_from_offline` for away, greeting and scheduled messages; `edit_date`.
- Not stated anywhere: that the owner's own messages arrive as `business_message`, or in which numbering a business message's `message_id` is. Hermes reads them as arriving with the owner as sender (`telegram_intake.py:6`, docstring); that is Hermes' reading, so B2 is live acceptance.
- Which chats: "The account owner can specify which chats your bot can access" (bots/features, Business bots): existing chats, new chats, contacts, non-contacts, with exclusions (api/bots/connected-business-bots).
- "Connecting a business bot does not require Telegram Premium", and "Currently just one business bot may be connected to a user account" (api/bots/connected-business-bots). Connecting `vtadevbot` for acceptance replaces any other connected bot.
- Message links exist for groups and channels only (api/links, Message links). A link to a message in a private chat between two users is not documented.

## Model

Terms:

- **Owner**: the Telegram account that connected the bot, `BusinessConnection.user.id`. Only owners named in config count.
- **Connection**: one `BusinessConnection`, with its owner, rights and whether it is enabled. A reconnect can bring a new connection id for the same owner.
- **Chat**: the private chat between the owner and one other person (`chat.id`, a user id).
- **Record**: one message in a chat, identified by owner, chat and `message_id`, not by connection id, so a reconnect does not duplicate it. That the numbering survives a reconnect is an inference from "identifiers of deleted messages in the chat of the business account" (bots/api, BusinessMessagesDeleted); live acceptance confirms it.
- **Author**: `owner` when the sender is the connection's owner, `contact` otherwise. The code sets it from the ids; nothing in the message text can change it. `offline` marks the owner's automatic messages.
- **Version**: the text, caption and attachments of a record as of one edit. The first version is the message as received. Telegram's `edit_date` is in seconds, so two edits of one second are two versions when their content differs; the newest edit says what the record carries now, and an older edit arriving late is kept as history only.

States of a record: `present`, then any number of edits, then possibly `deleted`, then `purged`. `purged` means the row is gone; it is the only terminal state. A deleted record keeps its text until it is purged (owner decision 6). A deletion for a message the base never had is ignored. An edit of a message the base does not hold is stored as a new record if it is within retention.

States of a connection: `enabled`, `disabled`, or `ignored` when its owner is not in config. A disabled connection stores nothing new; its records expire on the normal schedule. A connection kept in the file counts only while its owner is the configured one, so changing the owner in config stops intake for the old one. Whether a connection is enabled is checked again inside each write. A message whose connection is unknown is resolved with one `getBusinessConnection` call, one at a time per connection, so the reply cannot overwrite a connection update that came meanwhile and two chats ask once; if that fails, the message is not stored, and the failure is counted, without ids or text. This happens after an update missed while the gateway was down, and also in normal running: updates of different chats run concurrently (`PerChatUpdateProcessor`, `harvested/update_admission.py`), and a connection update has no chat, so a message can be handled before it (found by the end-to-end check). A connection whose owner is not configured is remembered in memory only, so the file holds nothing of it and Telegram is asked once per process.

Change sequence: every insert, edit and deletion gets the next number of one per-store sequence. Readers follow it with a cursor. A record keeps its newest 51 changes; older ones go, like a purged record's, and readers learn of it as a gap.

One writer of records: the adapter in the gateway process. The CLI only reads, except `purge`, which deletes what has expired by the same rule the gateway applies. Running it twice, or alongside the gateway, ends in the same state.

## Intake

- A handler in group -20, ahead of the plugin's other handlers and the harvested ones, takes every update with one of the four business fields and stops it there (`ApplicationHandlerStop`). It matches business updates only and stays first in its group, also after a native plugin is wired (those are wired before the adapter's own handlers), since PTB runs only a group's first matching handler. Group -10 is the Stop button's `TypeHandler(Update)` (`preview.py`). If Hermes ever stopped dropping business updates, they would still reach no agent. Without a usable `business_inbox` the handler still stops them and keeps nothing.
- Each update is written in one transaction, which also records the update's id (with the bot's id) for the retention period. An update applied once is not applied again, also after a restart: the harvested admission writes a receipt only for updates a core handler or another plugin served, so a business update, served by the adapter's own handler outside group 0, is not receipted and may be delivered again. The same content under another update id changes nothing either: a record's identity and a version's edit time and content are unique.
- A record keeps at most 50 versions: the first and the newest edits, so a burst of edits cannot grow it without limit.
- Only private chats are kept. A business update for any other chat type is dropped.
- A record holds: author, sender's display name, date, text or caption, the record it replies to, whether it was forwarded, `offline`, and for each attachment its kind, MIME type, size, file name, duration, width and height (a photo's largest size). Not `file_id` (open question 2). A contact, location, venue, poll, dice and the like keep their kind only: a phone number or a place is content, not metadata of a file. A message a bot sent on the owner's behalf (`sender_business_bot`) is the owner's.
- Nothing on this path calls a Bot API method except `getBusinessConnection`. No send, edit, read mark or deletion, also not when `can_reply` is on; `status` shows the rights so the owner can turn them off.
- Business updates never reach `handle_message`, so authorization, pairing, `profile_routes` and the existing routes are untouched. The inbox routes by connection owner, which is new; it does not change how anything else is routed.
- Logs count updates per kind and outcome. They carry no text, names or contact ids. python-telegram-bot logs every `getUpdates` result and webhook body at DEBUG before any handler runs, and the reply of `getBusinessConnection` names the owner; a filter drops those records when they carry a business update or connection, as Hermes does for a pending vault code (`harvested/telegram_vault_code.py`).

## Storage and retention

- One SQLite file, `telegram_business_inbox.sqlite3`, in the Hermes home of the profile the owner maps to, mode 0600. In this version that must be the adapter's own profile, `default`; a mapping to any other profile, like any unusable setting, is refused when the adapter connects: the inbox stays off and the gateway log says why.
- `retention_hours` (default 72) counts from the message's Telegram date, not from when it arrived. `max_messages_per_chat` (default 1000) counts records, not versions; the 1001st record of a chat purges that chat's oldest by Telegram date, then message id. A message older than every record a full chat keeps is not kept at all. Either limit is a whole number, at most ten years of hours and a million records.
- A purge runs with every message kept, before every CLI command answers, and every 10 minutes while the adapter's Application runs; that task ends with it. A file that exists keeps expiring when intake is off or its settings are refused: the purge then applies the limits the file was last written under. `hermes telegram inbox purge` lets the host run it on a timer when the gateway is down.
- Purged rows must not linger in the file: `secure_delete` is on and the journal is not left in WAL. A file of another schema, or of something else, is refused before anything is written to it. Backups of the host volume are outside this plugin; a backup taken while records exist keeps them past 72 hours.
- Settings live in the platform's `extra` as `business_inbox: {owners: {<user id>: <profile>}, retention_hours, max_messages_per_chat}`, with exactly one owner in this version (decision 5); `HERMES_TELEGRAM_INBOX_RETENTION_HOURS` and `HERMES_TELEGRAM_INBOX_MAX_PER_CHAT` override the two limits. Owner ids come from homelab's configuration; this public repository holds none.

## Read contract

`hermes telegram inbox <command> [options]` prints one JSON object with `contract_version: 1`. It reads the base of the profile it runs in (`HERMES_HOME`, `-p`); no command takes a profile or an owner. The schema of the file is private to the plugin.

| Command | Prints |
|---|---|
| `status` | Connections (owner, enabled, rights), record and chat counts, the oldest record's age, the limits in effect. No content |
| `chats` | Each chat with a reference, the other person's display name, the last message's date and the record count |
| `changes --since <cursor> [--limit N]` | Inserts, edits and deletions after the cursor, in order, and `next_cursor`. `gap: true` when a purge removed changes the caller has not read |
| `window --chat <ref> [--before <ref>] [--limit N]` | Records of one chat, oldest first |
| `purge` | What it removed, as counts |

A record in the output has `ref` and `chat` (opaque strings), `author`, `name`, `date` (UTC, ISO 8601), `text`, `reply_to`, `forwarded`, `offline`, `media`, `versions`, `deleted` and `link`.

`link` is `{kind, url}`. `kind` is `exact` for a link to the message itself, `chat` for one to the other person (the Bot API's mention link, `tg://user?id=<id>`), or `none`. No exact form is documented for private chats, so this version gives `chat`. A candidate for `exact` is `tg://resolve?domain=<username>&post=<message_id>`: Telegram Desktop's `resolve` handler parses `post` whatever kind of peer the username names (`Telegram/SourceFiles/core/local_url_handlers.cpp`, tdesktop `master` read 2026-11-27). Whether it then opens a message in a private chat, on Desktop or on iOS, is unknown, and it needs the other person to have a username. The base keeps the ids a link is built from, so a form that passes a live test changes only how the link is built.

## Slices

Each slice is one pull request to `main`, reviewed by the owner.

1. Intake and storage: B1, B3 to B7, B9, B10. The Bot API stand-in learns to deliver the four business updates and to answer `getBusinessConnection`. Proved on the pin: the group -20 handler sees business updates through the harvested `TelegramApplication`, and no later group, and no agent, sees them (`checks/test_business_inbox.py`, `checks/e2e/test_business_inbox.py`). Built in `hermes_telegram/inbox.py`.
2. Read contract: B8. It first proves that `hermes telegram` registers from this plugin on the pin.
3. Live acceptance of B1 to B3 on `vtadevbot`, and the link test.

## Decisions and open questions

Decided by the owner in chat, 2026-11-27:

1. The feature is Telegram Business: the bot connected to the owner's account. Lumi does not answer in those chats ("Не что бы Люми отвечала").
2. Retention is a time limit plus a per-chat count: "Срок хранения + max_messages", "max_messages на чат, плюс отруб по времени, давай не хранить ничего старше 3 дней (энва / конфиг)".
3. `max_messages_per_chat` is 1000 ("max_messages давай 1000").
4. Media as metadata only ("медиа метаданные только").
5. One owner only in this version; the second person on the bot is not included yet. The owner map stays in config, so adding someone later is a setting plus that profile's own store.
6. A deleted message is kept until it expires ("сообщение удаленное - хранить").
7. The contract is a command, not the storage schema ("контракт проще через тул какой то сделать что бы мы не были завязанны на схему"), and a CLI only, with no agent tool ("только CLI").
8. Built in this repository, as a separate design, outside Prism ("мы делаем не через призм. И делаем в hermes-telegram", "разделим дизайн на разные доки, это отдельная фича").

Open:

1. A global bound. Retention bounds each chat, but not the number of chats: a burst of new chats in 72 hours grows the file without limit. A cap on chats or on file size, and what to drop first, is the owner's choice.
2. `file_id`. Storing it costs nothing and would let a later flow fetch a file while it is still on Telegram's servers; not storing it keeps "metadata only" strict.
3. Whether the owner's own messages arrive (B2) and whether updates arrive with every right off. Telegram does not say; slice 3 answers both.
4. A host timer for `purge` while the gateway is down: a homelab change, outside this repository.

## Out of scope

Replies, drafts and any send in business chats; notifications; model calls, facts and wiki writes (flow 3); an agent tool; owners other than the configured one; group chats; downloading media; `hermes-mail` (flow 2); `proactive-assistance` (flow 4); deployment, which is a homelab change after a reviewed release.
