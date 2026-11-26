"""The acceptance gateway: a throwaway Hermes home with this plugin and an acceptance profile, and the real
``hermes gateway run`` serving ``vtadevbot`` from it.

Nothing here touches an existing Hermes home or profile. Everything lives under ``STATE`` (outside the
repository): the Hermes home, the samples, the record of the bot's calls, and the reports. Secrets never
pass through this code as values written to disk: the bot token and the model key come from the
environment ``op run`` builds from 1Password references (``acceptance/op.env``).

The gateway reaches Telegram through the recording proxy (``recorder``): ``extra.base_url`` points
python-telegram-bot at it. With a ``base_url`` set, the harvested adapter assumes a local Bot API server
for inbound downloads (``_max_doc_bytes``, 2 GB instead of 20 MB); no row sends the bot a file that large.

A git install of Hermes ships without its translations (its setup.py); the host's Nix wrapper points
``HERMES_BUNDLED_LOCALES`` at them, and this gateway gets the pinned source tree's, so its own messages
(the failure notice of row 10) read as on the host instead of as catalog keys.
"""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("HERMES_TELEGRAM_ACCEPTANCE_DIR",
                            "~/.local/state/hermes-telegram-acceptance")).expanduser()
HOME = STATE / "hermes"
HERMES_SRC = REPO / ".cache" / "hermes-agent"
# The SDK a provider's client needs; the gateway refuses to install one lazily.
_PROVIDER_SDK = {"anthropic": "anthropic"}
ADAPTER_LINE = "Telegram adapter HermesTelegramAdapter from "
# An answer whose model call fails (row 10): the provider refuses this key.
BROKEN_KEY = "invalid-key-for-the-acceptance-failure-row"
_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
             "NOUS_API_KEY", "DEEPINFRA_API_KEY")


def bundled_locales() -> Path:
    """Hermes' translations at the pin (``HERMES_PIN``), from its source tree."""
    pin = (REPO / "HERMES_PIN").read_text().strip()
    head = subprocess.run(["git", "-C", str(HERMES_SRC), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    if head != pin or not (HERMES_SRC / "locales" / "en.yaml").is_file():
        raise SystemExit(f"Hermes' translations come from its source at the pin {pin[:7]} ({HERMES_SRC}): "
                         "run scripts/hermes-src.sh")
    return HERMES_SRC / "locales"


def prepare(owner_id: int, *, provider: str, model: str, api: str, playbook: str) -> Path:
    """The acceptance profile's home, with this working tree's plugin installed as docs/install.md says, the
    bot's calls going through the proxy at ``api``, and the rows' playbook as the agent's SOUL.md."""
    sdk = _PROVIDER_SDK.get(provider)
    if sdk and importlib.util.find_spec(sdk) is None:
        raise SystemExit(f"The {provider} provider needs the {sdk} package in this environment: "
                         "run `mise exec -- uv sync --all-groups`.")
    HOME.mkdir(parents=True, exist_ok=True)
    plugin = HOME / "plugins" / "platforms" / "telegram"
    shutil.rmtree(plugin, ignore_errors=True)
    shutil.copytree(REPO / "hermes_telegram", plugin, ignore=shutil.ignore_patterns("__pycache__"))
    config = {
        "model": {"provider": provider, "default": model},
        "plugins": {"enabled": ["platforms/telegram"]},
        "platforms": {"telegram": {"enabled": True, "extra": {"base_url": f"{api}/bot",
                                                               "base_file_url": f"{api}/file/bot"}}},
        # Row 10's acceptance profile: the failure message is the only one the chat shows.
        "display": {"suppress_warning_notifications": True},
        # Row 17: a dangerous command waits for the owner's tap.
        "approvals": {"mode": "manual"},
        "updates": {"check": False},
    }
    with open(HOME / "config.yaml", "w", encoding="utf-8") as out:
        YAML().dump(config, out)
    (HOME / "SOUL.md").write_text(playbook, encoding="utf-8")
    return HOME


class Gateway:
    """The gateway child serving the bot; ``broken`` starts it with a model key the provider refuses."""

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.proc: Optional[subprocess.Popen] = None
        self.console = STATE / "gateway-console.log"

    def _env(self, broken: bool) -> dict[str, str]:
        env = dict(os.environ)
        missing = [k for k in ("TELEGRAM_BOT_TOKEN",) if not env.get(k)]
        if missing or not any(env.get(k) for k in _KEY_VARS):
            raise SystemExit("Run under `op run --env-file acceptance/op.env`: the bot token and a model key "
                             "come from 1Password (acceptance/README.md).")
        env.update(HERMES_HOME=str(HOME), TELEGRAM_ALLOWED_USERS=str(self.owner_id),
                   TELEGRAM_HOME_CHANNEL=str(self.owner_id),
                   # The proxy is the only address: no fallback IPs, no system proxy in front of it.
                   HERMES_TELEGRAM_DISABLE_FALLBACK_IPS="1", HERMES_BUNDLED_LOCALES=str(bundled_locales()),
                   NO_PROXY=",".join(filter(None, ("127.0.0.1", "localhost", env.get("NO_PROXY")))))
        if broken:
            for key in _KEY_VARS:
                if env.get(key):
                    env[key] = BROKEN_KEY
        return env

    def start(self, *, broken: bool = False, timeout: float = 90.0) -> None:
        self.stop()
        log_path = HOME / "logs" / "gateway.log"
        before = log_path.stat().st_size if log_path.exists() else 0
        with open(self.console, "a", encoding="utf-8") as console:
            self.proc = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "gateway", "run"], cwd=str(HOME),
                                         env=self._env(broken), stdin=subprocess.DEVNULL, stdout=console,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"The gateway exited ({self.proc.returncode}); see {self.console}")
            text = log_path.read_bytes()[before:].decode("utf-8", "replace") if log_path.exists() else ""
            if "terminated by other getUpdates request" in text or "Conflict" in text:
                self.stop()
                raise SystemExit("Another process polls vtadevbot (Telegram says Conflict): stop the host's "
                                 "vtadevbot gateway for the run.")
            if ADAPTER_LINE + str(HOME / "plugins" / "platforms" / "telegram") in text:
                time.sleep(3.0)  # the first poll
                return
            time.sleep(0.5)
        self.stop()
        raise SystemExit(f"The plugin's adapter did not start within {timeout:.0f} s; see {log_path}")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
