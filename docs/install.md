# Installing the plugin

Infrastructure (`homelab`) installs a reviewed version on the host. This page states what an install must do; it deploys nothing.

## Prerequisite: plugins load in-process

The profile must load plugins in the gateway process, which is Hermes' default (`plugins.isolation` unset or `in-process`). Under `plugins.isolation: host`, a user plugin runs in the plugin host, where `ctx.register_platform` is refused (`hermes_cli/plugins_loader.py:655`, `hermes_cli/plugin_isolation.py:38`). The installed copy would still shadow the bundled one, and Telegram would be left without an adapter. A host-isolated profile keeps the bundled adapter; do not weaken its isolation for this plugin. The `homelab` Hermes configuration sets no `plugins.isolation`.

## Steps

1. Copy the `hermes_telegram/` directory of a reviewed commit to `$HERMES_HOME/plugins/platforms/telegram/` of the profile that owns the Telegram bot. The directory name matters: `platforms/telegram` is the bundled plugin's key, and only a user plugin with the same key replaces it (`hermes_cli/plugins_discovery.py:222`). Copy it; do not link it.
2. Enable it in that profile's `config.yaml`:

   ```yaml
   plugins:
     enabled:
       - platforms/telegram
   ```

3. Restart the gateway and check Hermes' INFO log, `$HERMES_HOME/logs/gateway.log` (the console shows only warnings), for the line the plugin writes when it builds the adapter:

   ```text
   hermes-telegram 0.1.0: Telegram adapter HermesTelegramAdapter from <HERMES_HOME>/plugins/platforms/telegram
   ```

   Only this line shows the plugin serves Telegram. Discovery's `shadows the bundled copy` line is written before the plugin is enabled or registered, and it also appears when Telegram ends up with no adapter.

The plugin's Telegram configuration is the bundled one: the same `telegram:` keys, environment variables and `gateway.profile_routes` apply unchanged.

## Install and enable together

An installed plugin that is not enabled still shadows the bundled one, and Hermes then has no Telegram adapter at all: the bot goes silent. `checks/test_install.py` shows all three cases on the pinned Hermes:

| Installed | Enabled | Telegram adapter |
|---|---|---|
| yes | yes | this plugin's `HermesTelegramAdapter` |
| no | no | the bundled `TelegramAdapter` |
| yes | no | none |

To go back to the bundled adapter, remove the directory, not only the `plugins.enabled` entry.

## Compatibility

The plugin targets the Hermes commit in `HERMES_PIN`. Its harvested code imports Hermes internals, so a Hermes upgrade on the host comes with a re-harvest and a test run here first (see README, Upgrading Hermes).
