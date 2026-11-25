"""hermes-telegram: the Telegram platform plugin for Hermes Agent.

Hermes imports this directory as a plugin package and calls ``register(ctx)``. Platform plugins load
lazily, on the first use of the platform, so importing the adapter here is not on the CLI's start path.
"""

from .plugin import register

__all__ = ["register"]
