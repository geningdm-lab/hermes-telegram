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
