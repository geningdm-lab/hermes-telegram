"""End-to-end checks need the Hermes source at the pin and import it while pytest collects them.

A run that deselects them (``-m "not e2e"``) does not collect this directory, so it runs against the
installed Hermes alone and does not depend on that checkout's state.
"""

from pathlib import Path


def pytest_ignore_collect(collection_path: Path, config: object) -> bool | None:
    markexpr = " ".join(str(getattr(config, "option").markexpr or "").replace("(", " ").split())  # type: ignore[attr-defined]
    if collection_path.suffix == ".py" and "not e2e" in markexpr:
        return True
    return None
