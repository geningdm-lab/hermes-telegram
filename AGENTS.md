# Working in hermes-telegram

Read README.md and the design of the feature you change before changing anything: docs/design.md for the presentation surface, docs/business-inbox.md for the Business inbox. A design's acceptance rows and the owner's decisions it records (for docs/design.md, also the answers in the linked PRD) are the requirements; do not narrow or widen them silently. A change that departs from a design updates that design in the same pull request.

## Rules

- Target the pinned Hermes, `NousResearch/hermes-agent@28ef6e4` (`HERMES_PIN`). Cite Hermes as `hermes-agent@28ef6e4:path:line`. Upstream docs of a newer Hermes are not evidence for the pinned one.
- Cite Telegram behaviour from core.telegram.org with the page. Production support is what counts; the test server is not acceptance. A passing test is not a live delivery.
- Harvested upstream code: keep the MIT notice, name the upstream path and commit in the file header, and mark every local change to it.
- Test against the real pinned Hermes and python-telegram-bot, with Telegram replaced by a local HTTP server. Do not write stand-ins for Hermes modules.
- Keep owner and profile authorization, `profile_routes` and restricted-contact policy as they are today. Buttons, widgets and polls go through the same admission as a text message.
- Agent tools act only in the chat and topic of the calling turn.
- No tokens, auth stores, transcripts, personal records or runtime databases in Git. Use synthetic fixtures.
- Owner review happens on GitHub pull requests. Never merge without the owner's approval.

## Per slice

- One slice of a feature's design per pull request, in order.
- When a slice is ready, run an independent review: Codex with `gpt-6.2-astra` at effort `high` through `llm-bridge` against the slice's branch. Fix the findings you accept. Record each finding you dispute in docs/review-log.md with the evidence and what would reopen it.
- Product questions (what the owner sees or decides, not how the code does it) go to the owner as a push notification, not into code as a guess.
- Tests run in `checks/` (`.venv/bin/pytest`; `-m "not e2e"` skips the gateway runs). The session's Hermes home is a throwaway one (checks/conftest.py); never point a test at a real profile.
