"""Test-session isolation from the developer's own Hermes.

Importing Hermes reads and initializes its home (``$HERMES_HOME``, else ``~/.hermes``): it creates
directories, reads ``.env`` and may back up ``config.yaml``. pytest imports this file before it collects
any test module, so the session gets a throwaway home before the first Hermes import. Gateway children in
the end-to-end checks get homes of their own as well.
"""

import os
import shutil
import tempfile
from pathlib import Path

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="hermes-telegram-checks-"))
for _name in list(os.environ):
    if _name.startswith(("HERMES_", "TELEGRAM_")):
        del os.environ[_name]
os.environ["HOME"] = str(_SESSION_HOME)
os.environ["HERMES_HOME"] = str(_SESSION_HOME / ".hermes")


def pytest_unconfigure(config: object) -> None:
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)
