from __future__ import annotations

import os
import sys

# Set when a LogosTransport was stopped without destroying the native node (`fast_stop`).
# From then on the process must leave with `os._exit`: tearing the library down takes ~23 s,
# and letting the interpreter exit with the node alive can crash in the library (exit code 139).
_native_node_left = False


def mark_native_node_left() -> None:
    global _native_node_left
    _native_node_left = True


def exit_now(code: int = 0) -> None:
    """Leave at once, without running atexit handlers or joining threads. The caller must have
    written its outputs and closed its own state already."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def leave(code: int = 0) -> None:
    """End a command: `os._exit` when a native node was left running, else a normal exit."""
    if _native_node_left:
        exit_now(code)
        return
    if code:
        import typer

        raise typer.Exit(code=code)
