# Live acceptance on vtadevbot

The PRD's acceptance (`docs/design.md`, Acceptance) is a live run of each row on `vtadevbot`, on production Telegram, from the owner's own account, observed on the official iOS client. This kit runs it from this Mac. The owner sends each row's code from iOS. The run shows what the bot sent and what Telegram made of it, checks that record, and asks the owner whether iOS shows the same.

- `acceptance/home.py` builds a throwaway Hermes home under `~/.local/state/hermes-telegram-acceptance/`, installs this working tree's plugin as `docs/install.md` says, and starts the real `hermes gateway run` for `vtadevbot`. It touches no existing Hermes home or profile. The agent's `SOUL.md` there is the rows' playbook: each code (`r2`) and its steps (`python -m acceptance playbook` prints it).
- `acceptance/recorder.py` is a proxy between that gateway and api.telegram.org (the plugin's `extra.base_url`). It passes every call through unchanged and records it: the method, its fields, uploads by name, size and SHA-256, and Telegram's answer. A send's answer is the message as Telegram stored it: a rich message's parsed blocks (tables cell by cell, formulas by their LaTeX source), the effect it applied, the quote. The updates Telegram delivers are what the owner did: messages, taps, votes, reactions, Stop, widget requests. The token, which is in the request path, is never recorded.
- `acceptance/rows.py` asks the owner to send each row's code, then checks the record and the agent's log (`state.db` of that home). It prints the record for the owner to compare with the iOS screen. It also asks at that moment for what only eyes on iOS can show: how it looks, screenshots and screen recordings, taps inside widgets, the Stop button.
- The report goes to `~/.local/state/hermes-telegram-acceptance/reports/` and the record to `records/` next to it, outside Git: they hold what the chat said.

## Before the first run

1. Bring the environment to the pin: `mise exec -- uv sync --all-groups` (Hermes with its Telegram and Anthropic extras) and `scripts/hermes-src.sh` (Hermes' source at the pin, whose translations the gateway reads, as the host's Nix wrapper gives them).
2. Stop the host's `vtadevbot` gateway. Telegram lets one process poll a bot; the run refuses to start while another does.
3. Copy `acceptance/op.env.example` to `acceptance/op.env` and fill in the 1Password references (`op://vault/item/field`) of the bot token and the model key. The file holds references only and is ignored by Git; `op run` resolves them into the run's environment, so no secret is written to disk or shown.
4. The first run asks you to send the bot any message from iOS and to confirm the account it came from; it keeps that user id for later runs (`owner-id` in the state directory). `TELEGRAM_OWNER_ID` in `op.env` sets it instead.

## A run

```bash
op run --env-file acceptance/op.env -- mise exec -- .venv/bin/python -m acceptance run
```

Everything runs by default; give row keys to run some (`run 2 11 X5`); `rows` lists them. `--model` and `--provider` choose the acceptance agent's model (default `anthropic` `claude-sonnet-5-5`; the key variable in `op.env` must be that provider's). Keep the iOS app open on the chat with `vtadevbot` and this terminal in view. A line starting with `>>> On iOS:` says what to send or tap; after each row the run prints the record and asks whether iOS shows the same.

| Row | The owner sends | What the run checks in the record | What the owner does on iOS |
|---|---|---|---|
| 1, 2, 16 | `r1`, `r1l`, `r2`, `r16` | no literal markup in Telegram's parse, the formula's source, the table's cells, one ordinary message | compares, screenshots |
| 3, 4, 5, 15 | `r3`, `r4a`, `r4b`, `r5`, `r15` | methods and the kinds Telegram stored, albums of 3, 10 and 2 in order (by checksum), names and checksums as Telegram serves the files, the notices | opens and plays the media, checks the albums' colours |
| 6, 8 | `r6` | `Contemplating...` then each status in the preview's frames, the Stop button, one answer, no frame after it | screen recording |
| 7 | `r7` | the preview held the beginning of the answer twice, growing | screen recording |
| 9 | `r9` | Stopped, no file or answer for 60 s, no frame 5 s after the Stop, the agent's last step | taps Stop, screen recording |
| 10 | `r10` | the gateway restarted with a model key the provider refuses: one message, no preview; then restarted normally | says whether the message says the answer failed |
| 11, X1, X2 | `r11`, `x1`, `x2` | one incoming choice and "Already answered" on the second tap, the vote, the reactions both ways, the agent's log | taps, votes, reacts |
| X3, X4, X5 | a sticker, `x3`, `x4`, `x5 the plan is ready` | the same sticker back, the edit, pin and delete, the quote and the 🎉 effect id Telegram applied | says whether the effect played |
| 12, 13, 14 | `r12`, `r14` | four widgets Telegram accepted whose HTML needs no network; nothing delivered while the owner plays with them; one action with its widget, answered within 15 s | screenshots, interacts inside the widgets, taps Ask Lumi |
| 17 | `r17a`, `r17b` | the cron report during the next turn's preview, the approval prompt still there, the work going on after one tap | taps the approval once |

Rows 12 to 14 cannot pass for now: since 2026-11-13 Telegram refuses widgets from general bots (`INPUT_MEDIA_WIDGET_NOT_ALLOWED`; `docs/design.md`, Acceptance).

## The Business inbox rows

Rows B1 to BL are slice 3 of `docs/business-inbox.md`: the bot connected to the owner's own account, kept chats read back through `hermes telegram inbox`. The acceptance home sets `business_inbox` for the owner's id. They need a second Telegram account the owner holds (another phone number, added in the iOS app's account switcher): the "other person" of the chat. The bot is given only the chat with that account. One business bot may be connected to an account at a time (core.telegram.org/api/bots/connected-business-bots), so connecting vtadevbot replaces any other.

| Row | The owner does | What the run checks |
|---|---|---|
| B1 | connects vtadevbot (Settings > Telegram Business > Chatbots: only the chat with the second account, every right off), then sends a code from the second account | the connection grants no reply; nothing the bot asked of Telegram touched that chat; no agent turn saw the text; `window` keeps it as `contact`; `status` shows the connection |
| B2 | sends a code from the main account in that chat | whether Telegram delivers the owner's own message at all (it is not documented), and then that it is kept as `owner`, with no call and no turn |
| B3 | edits the B1 message from the second account, then deletes it for both | both versions kept, the newest as the text; the record marked deleted with its text |
| BL | sends one more code, then opens each printed link from Saved Messages | the contract's link is the chat link; for each candidate (`tg://user`, `tg://openmessage`, and with a username `tg://resolve?...&post=` and `t.me/<username>/<id>`), whether iOS opened the message itself |

Run them in order (`run B1 B2 B3 BL`): B2 to BL use what B1 learned. Afterwards remove vtadevbot from the account's chatbots. What BL finds goes into `docs/business-inbox.md` (Read contract, `link`): a form that opens the message becomes `exact`.

## After the runs

Restart the host's `vtadevbot` gateway. The state directory can then be deleted: `~/.local/state/hermes-telegram-acceptance/`.

With a `base_url` set, the harvested adapter assumes a local Bot API server for inbound downloads (2 GB instead of 20 MB); no row sends the bot a file that large.
