# Working in hermes-telegram

Read README.md and docs/design.md before changing anything. The design's acceptance rows and the owner's answers in the linked PRD are the requirements; do not narrow or widen them silently. A change that departs from the design updates the design in the same pull request.

## Rules

- Target the pinned Hermes, `NousResearch/hermes-agent@c419cf7`. Cite Hermes as `hermes-agent@c419cf7:path:line`. Upstream docs of a newer Hermes are not evidence for the pinned one.
- Cite Telegram behaviour from core.telegram.org with the page. Production support is what counts; the test server is not acceptance. A passing test is not a live delivery.
- Harvested upstream code: keep the MIT notice, name the upstream path and commit in the file header, and mark every local change to it.
- Test against the real pinned Hermes and python-telegram-bot, with Telegram replaced by a local HTTP server. Do not write stand-ins for Hermes modules.
- Keep owner and profile authorization, `profile_routes` and restricted-contact policy as they are today. Buttons, widgets and polls go through the same admission as a text message.
- Agent tools act only in the chat and topic of the calling turn.
- No tokens, auth stores, transcripts, personal records or runtime databases in Git. Use synthetic fixtures.
- Owner review happens on GitHub pull requests. Never merge without the owner's approval.
