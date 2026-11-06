"""Small GUI helpers."""

from __future__ import annotations

import threading
from typing import Callable


def run_threaded(widget, work: Callable, on_done: Callable | None = None,
                 on_error: Callable | None = None) -> None:
    """Run ``work()`` on a daemon thread; marshal the result back to the UI.

    ``on_done(result)`` / ``on_error(exc)`` run on the Tk main thread.
    """
    def _run():
        try:
            res = work()
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                try:
                    widget.after(0, lambda e=exc: on_error(e))
                except Exception:  # noqa: BLE001 - window closed
                    pass
            return
        if on_done is not None:
            try:
                widget.after(0, lambda: on_done(res))
            except Exception:  # noqa: BLE001 - window closed
                pass

    threading.Thread(target=_run, daemon=True).start()
