# Live acceptance on vtadevbot

The PRD's acceptance (`docs/design.md`, Acceptance) is a live run of each row on `vtadevbot`, on production Telegram, from the owner's own account, observed on the official iOS client. This kit runs it from this Mac:

- `acceptance/home.py` builds a throwaway Hermes home under `~/.local/state/hermes-telegram-acceptance/`, installs this working tree's plugin as `docs/install.md` says, and starts the real `hermes gateway run` for `vtadevbot`. It touches no existing Hermes home or profile.
- `acceptance/live.py` logs in as the owner's account (Telethon, MTProto layer 233, which carries rich messages, drafts and widgets) and reads the agent's log for the chat from that home's `state.db`.
- `acceptance/rows.py` runs each row's fixed prompt and checks what the owner's account received: native tables cell by cell, the formula's source, albums and their order, file names and checksums, the preview's frames (drafts reach the owner's client as typing updates), taps, votes, reactions, quotes, the effect Telegram applied, widgets and their HTML, the agent's log. What only eyes on iOS can show, the run asks the owner at that moment: how it looks, the screenshots and screen recordings, taps inside widgets, the Stop button.
- The report goes to `~/.local/state/hermes-telegram-acceptance/reports/`, outside Git: it holds what the chat said.

## Before the first run

1. Stop the host's `vtadevbot` gateway. Telegram lets one process poll a bot; the run refuses to start while another does.
2. Create the Telegram app of the owner's account at <https://my.telegram.org> (API development tools) if there is none, and keep its `api_id` and `api_hash` in 1Password.
3. Copy `acceptance/op.env.example` to `acceptance/op.env` and fill in the 1Password references (`op://vault/item/field`) of: the bot token, the model key, the `api_id` and the `api_hash`. The file holds references only and is ignored by Git; `op run` resolves them into the run's environment, so no secret is written to disk or shown.
4. Log in once; type the phone number, the code Telegram sends and the 2FA password yourself:

   ```bash
   op run --env-file acceptance/op.env -- mise exec -- .venv/bin/python -m acceptance login
   ```

## A run

```bash
op run --env-file acceptance/op.env -- mise exec -- .venv/bin/python -m acceptance run
```

Everything runs by default; give row keys to run some (`run 2 11 X5`); `rows` lists them. `--model` and `--provider` choose the acceptance agent's model (default `anthropic` `claude-sonnet-5-5`; the key variable in `op.env` must be that provider's). Have the iOS app open on the chat with `vtadevbot`; the run stops to ask before each recording, for each tap only a human can make, and for what each row should look like.

| Row | What the run does | What the owner does on iOS |
|---|---|---|
| 1, 2, 16 | asks for the fixed sample, table and sentence; checks no literal markup, the formula's source, the table's cells, one ordinary message | screenshots, says whether each element looks formatted |
| 3, 4, 5, 15 | asks the agent to send the samples; checks kinds, albums of 3, 10 and 2 in order (by colour), names and checksums, the notices | opens and plays the media |
| 6, 8 | sets two statuses with pauses; checks `Contemplating...` then each status in the preview's frames, one answer, no preview after it | screen recording |
| 7 | a long answer; checks the preview held the beginning of it twice, growing | screen recording |
| 9 | 20 s of work, then a file; checks Stopped, no file or answer for 60 s, frames frozen, the agent's last step | taps Stop on the preview, timed recording |
| 10 | restarts the gateway with a model key the provider refuses; checks one message and no preview; restarts it normally | says whether the message says the answer failed |
| 11, X1, X2 | taps buttons, votes, reacts from the owner's account; checks one incoming choice, "Already answered", the answers, the agent's log | looks at the buttons and the notice |
| X3, X4, X5 | sends a recent sticker; asks for edits, a pin, a delete, a quote with the 🎉 effect; checks the result and the effect id Telegram applied | says whether the effect played |
| 12, 13, 14 | asks for four widgets; checks each arrived and its HTML needs no network; checks nothing reaches the agent while the owner plays with them, then one action with its widget | screenshots, interacts inside the widgets, taps Ask Lumi |
| 17 | schedules a cron report, then a long turn with an approval it taps once; checks the report, the prompt still there, the work going on | says whether the report came while the preview ran |

## After the runs

Restart the host's `vtadevbot` gateway. To end the script's login to the owner's account:

```bash
op run --env-file acceptance/op.env -- mise exec -- .venv/bin/python -m acceptance logout
```

The state directory can then be deleted: `~/.local/state/hermes-telegram-acceptance/`.
