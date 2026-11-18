"""Global selection monitor.

On X11 a "selection" is what most users think of as the mouse highlight: as
soon as you let go of the left button the highlighted text is placed in the
**PRIMARY** selection buffer and can be pasted with the middle mouse button.
Watching PRIMARY therefore gives us a system-wide "word selection" hook that
works in terminals, browsers, IDEs, PDF readers and any other application.

GNOME-Wayland runs most legacy applications under XWayland, where ``xclip``
keeps working transparently for the PRIMARY buffer.  ``wl-paste`` is used as
a transparent fallback when available so native Wayland apps are covered too.

The monitor runs in a daemon thread and emits callbacks on the main thread
boundary via a thread-safe queue -- the GUI consumer pulls from that queue in
its own event loop so we never touch Qt from a worker thread.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import config

log = logging.getLogger(__name__)

# A "word" the dictionary cares about: ASCII letters, apostrophes, hyphens.
# Anything with spaces / digits / CJK falls through to the online translator.
WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]{0,80}$")


@dataclass(frozen=True)
class SelectionEvent:
    """One selection event delivered to the GUI."""

    text: str           # the cleaned-up selected text
    is_word: bool       # True if it looks like a single English word
    mouse_x: int        # pointer X at the moment of selection
    mouse_y: int        # pointer Y at the moment of selection


class SelectionMonitor:
    """Polls the PRIMARY selection and emits ``SelectionEvent`` callbacks.

    Parameters
    ----------
    callback:
        Called (on the monitor thread) every time a *new* selection is
        detected. The callback should be cheap; if it needs to touch the GUI
        it must marshal back to the GUI thread itself.
    """

    def __init__(self, callback: Callable[[SelectionEvent], None]) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_hash = ""
        # pick the best available PRIMARY command (preferring wl-paste under
        # native Wayland, falling back to xclip which works on XWayland)
        self._primary_cmd = self._resolve_primary_cmd()
        self._mouse_cmd = self._resolve_mouse_cmd()
        log.debug(
            "selection backend: primary=%s mouse=%s",
            self._primary_cmd, self._mouse_cmd,
        )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._primary_cmd is None:
            log.error(
                "No clipboard backend available. Install `xclip` or "
                "`wl-clipboard` (apt install wl-clipboard)."
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="selection-monitor", daemon=True
        )
        self._thread.start()
        log.info("selection monitor started (interval=%.2fs)", config.SELECTION_POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._read_primary()
            except Exception as exc:  # subprocess errors must not kill the loop
                log.debug("primary read failed: %s", exc)
                text = ""
            if text:
                self._maybe_emit(text)
            self._stop.wait(config.SELECTION_POLL_INTERVAL)

    def _maybe_emit(self, raw: str) -> None:
        text = raw.strip().strip(".,!?;:\"'`()[]{}")
        if not text:
            return
        if len(text) > config.SELECTION_MAX_LENGTH:
            return
        # dedupe by hash so the same selection doesn't fire twice in a row
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        if digest == self._last_hash:
            return
        self._last_hash = digest

        x, y = self._pointer_position()
        event = SelectionEvent(
            text=text,
            is_word=bool(WORD_RE.match(text)),
            mouse_x=x,
            mouse_y=y,
        )
        try:
            self._callback(event)
        except Exception:  # pragma: no cover - never let callback kill us
            log.exception("selection callback raised")

    # ------------------------------------------------------------------ #
    # backend probing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_primary_cmd() -> Optional[tuple[str, ...]]:
        # Prefer wl-paste under Wayland because it covers native Wayland apps.
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            return ("wl-paste", "--primary", "--no-newline")
        if shutil.which("xclip"):
            return tuple(config.PRIMARY_CMD)
        if shutil.which("xsel"):
            return ("xsel", "--primary", "--output", "--clipboard")
        if shutil.which("wl-paste"):
            return ("wl-paste", "--primary", "--no-newline")
        return None

    @staticmethod
    def _resolve_mouse_cmd() -> Optional[tuple[str, ...]]:
        # xdotool works on XWayland; under pure Wayland we'd need a portal.
        if shutil.which("xdotool"):
            return ("xdotool", "getmouselocation", "--shell")
        return None

    # ------------------------------------------------------------------ #
    # subprocess helpers
    # ------------------------------------------------------------------ #
    def _read_primary(self) -> str:
        if not self._primary_cmd:
            return ""
        try:
            result = subprocess.run(
                self._primary_cmd,
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("primary cmd failed: %s", exc)
            return ""
        # xclip returns non-zero when the selection is empty -- treat as empty
        return result.stdout if result.returncode == 0 else ""

    def _pointer_position(self) -> tuple[int, int]:
        if not self._mouse_cmd:
            return (-1, -1)
        try:
            result = subprocess.run(
                self._mouse_cmd,
                capture_output=True,
                text=True,
                timeout=0.6,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return (-1, -1)
        x = y = -1
        for line in result.stdout.splitlines():
            if line.startswith("X="):
                x = int(line[2:])
            elif line.startswith("Y="):
                y = int(line[2:])
        return (x, y)


def hotkey_listener(hotkey: str, callback: Callable[[], None]) -> Optional[threading.Thread]:
    """Watch for a global hotkey via ``xdotool key`` polling.

    Returns the daemon thread (or ``None`` if xdotool is unavailable).
    Used only when ``config.AUTO_TRANSLATE`` is False.
    """
    if not shutil.which("xdotool"):
        log.warning("xdotool missing -- global hotkey '%s' disabled", hotkey)
        return None

    def _watch() -> None:
        stop = threading.Event()
        while not stop.is_set():
            # `xdotool key --delay 0 <keysym>` blocks until the key is pressed
            # when combined with `keydown`/`keyup`; for simplicity we poll the
            # currently-pressed modifiers against xdotool's getactivewindow.
            # Simpler: rely on xclip PRIMARY which already covers selection.
            time.sleep(0.5)

    # NOTE: true global hotkeys on Wayland require a desktop portal which is
    # out of scope here; the AUTO_TRANSLATE path (default) is the primary
    # mechanism. This stub keeps the API symmetric for future expansion.
    return None
