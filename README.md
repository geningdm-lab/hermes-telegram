# hermes-telegram

A Telegram platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that replaces the bundled Telegram adapter and gives the agent the Bot API's presentation surface: rich messages with tables, streamed previews with a Stop button, inline widgets for diagrams, charts and prototypes, buttons, polls, stickers, reactions and reliable file delivery.

## Status

Slice 0 of [docs/design.md](docs/design.md): the plugin loads in place of the bundled adapter and behaves as it does. It is built from the bundled adapter's code, harvested into one module per concern; the new agent surface comes in slices 1 to 6, one pull request each.

This repository is the Claude Code counterpart of the Prism attempt in `geningdm-lab/lumi-telegram`. Both work to the same acceptance rows.

## Layout

- `hermes_telegram/`: the plugin directory Hermes loads (`plugin.yaml`, `register`).
  - `adapter.py`: `HermesTelegramAdapter`, composed from the harvested mixins. New behaviour goes into classes listed before them.
  - `plugin.py`: the platform registration.
  - `harvested/`: generated from the bundled adapter by `scripts/harvest.py`. Never edited by hand.
- `checks/`: tests. `checks/e2e/` runs Hermes' own Telegram contract suite against this plugin.
- `docs/`: design and install notes.

## Development

Tools come from mise (`mise.toml`): Python, uv, ruff.

```sh
mise install
mise exec -- uv sync
scripts/hermes-src.sh
.venv/bin/pytest
```

`uv sync` installs the pinned Hermes with its Telegram extra as a development dependency. `scripts/hermes-src.sh` fetches the Hermes source at the same commit into `.cache/hermes-agent`: the harvest reads it, and `checks/e2e/` imports Hermes' test rig from it. The end-to-end checks start real gateway processes and take a few minutes; `.venv/bin/pytest -m "not e2e"` skips them.

## Upgrading Hermes

1. Change the pin in `scripts/harvest.py` (`PIN`), in `pyproject.toml` (the `hermes-agent` URL and the mirrored `[tool.uv]` overrides) and run `mise exec -- uv sync`.
2. `scripts/hermes-src.sh`, then `.venv/bin/python scripts/harvest.py --src .cache/hermes-agent`. A method the upstream class added, removed or renamed stops the harvest until it is assigned in `METHOD_GROUPS`.
3. Review the diff of `hermes_telegram/harvested/` and run the checks.

## Boundaries

- Code harvested from the bundled Telegram adapter (MIT, Nous Research) keeps its notice and names its upstream path and commit.
- Deployment belongs to `homelab`, which installs a reviewed version of this plugin ([docs/install.md](docs/install.md)). Nothing here deploys, changes a live profile, or sends to production Telegram.
- Bot tokens, auth stores, transcripts and runtime state stay out of Git. Tests use synthetic data.
