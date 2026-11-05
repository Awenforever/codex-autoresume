"""Best-effort Windows notification sounds.

The daemon runs under pythonw, so there is no console to print to when the
user is away from the panel. ``winsound`` system aliases are the
dependency-free way to get an audible cue; playback is asynchronous and
failures are swallowed (a missing sound device must never break a resume).
"""

from __future__ import annotations

import os
import threading

_WINDOWS = os.name == "nt"

_ALIASES = {
    "default": "SystemDefault",
    "asterisk": "SystemAsterisk",
    "exclamation": "SystemExclamation",
    "question": "SystemQuestion",
    "hand": "SystemHand",
    "beep": "SystemHand",
}


def _play(spec: str) -> None:
    if not _WINDOWS:
        return
    try:
        import winsound
        if os.path.isfile(spec):
            winsound.PlaySound(spec, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        alias = _ALIASES.get(spec.strip().lower(), spec)
        winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass


def play(spec: str) -> None:
    """Play *spec* on a daemon thread so the guard tick never blocks."""
    if not spec or not _WINDOWS:
        return
    threading.Thread(target=_play, args=(spec,), daemon=True).start()
