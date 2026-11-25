# hermes-telegram

A Telegram platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that replaces the bundled Telegram adapter and gives the agent the Bot API's presentation surface: rich messages with tables, streamed previews with a Stop button, inline widgets for diagrams, charts and prototypes, buttons, polls, reactions and reliable file delivery.

## Status

Design only. No code yet. The design and its acceptance rows are in [docs/design.md](docs/design.md). Work proceeds in slices, one pull request each.

This repository is the Claude Code counterpart of the Prism attempt in `geningdm-lab/lumi-telegram`. Both work to the same acceptance rows.

## Boundaries

- The plugin targets the Hermes commit that `homelab` deploys: `NousResearch/hermes-agent@c419cf7`.
- Code harvested from the bundled Telegram adapter (MIT, Nous Research) keeps its notice and names its upstream path and commit.
- Deployment belongs to `homelab`, which installs a reviewed version of this plugin. Nothing here deploys, changes a live profile, or sends to production Telegram.
- Bot tokens, auth stores, transcripts and runtime state stay out of Git. Tests use synthetic data.

## Development

Tools come from mise (`mise.toml`): Python and uv.

```sh
mise install
```
