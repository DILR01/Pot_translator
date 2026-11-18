"""Central configuration & runtime paths for Pot Translator.

All tunable settings live here so the rest of the code stays declarative.
Paths resolve relative to the package directory so the tool is portable
and works regardless of the current working directory.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ECDICT-style SQLite dictionary (populated by dict_manager --import-ecdict)
DICT_DB_PATH: Path = DATA_DIR / "ecdict.db"
# User-defined additions / overrides (managed by dict_manager add/list/remove)
USER_DB_PATH: Path = DATA_DIR / "user_words.db"

# --------------------------------------------------------------------------- #
# Selection monitor
# --------------------------------------------------------------------------- #
# Poll interval for the PRIMARY (mouse-selection) buffer, in seconds.
# Smaller = snappier but more CPU; 0.25s is imperceptible on modern hardware.
SELECTION_POLL_INTERVAL: float = float(os.environ.get("POT_POLL_INTERVAL", "0.25"))
# Ignore selections longer than this many characters (avoids translating
# whole paragraphs the user accidentally selected).
SELECTION_MAX_LENGTH: int = 80
# Treat selection as a "word" only if it matches this regex (word + apostrophe
# / hyphen). Multi-word phrases still go through the online translator.
WORD_PATTERN: str = r"[A-Za-z][A-Za-z'-]*"
# If True, trigger translation automatically whenever the user selects text.
# If False, translation only fires when the configured HOTKEY is pressed.
AUTO_TRANSLATE: bool = True
# Global hotkey understood by xdotool (only used when AUTO_TRANSLATE=False).
# Example: "ctrl+alt+c" (use xdotool key names: ctrl, alt, shift, ctrl+alt+t ...).
HOTKEY: str | None = "ctrl+alt+t"
# Commands used to read selections. xclip works on XWayland apps (covers most
# GNOME-Wayland sessions). wl-paste is used if available for native Wayland.
PRIMARY_CMD: tuple[str, ...] = ("xclip", "-o", "-selection", "primary")
CLIPBOARD_CMD: tuple[str, ...] = ("xclip", "-o", "-selection", "clipboard")

# --------------------------------------------------------------------------- #
# Floating window
# --------------------------------------------------------------------------- #
WINDOW_HIDE_TIMEOUT_MS: int = 8000      # auto-hide after N ms of inactivity
WINDOW_OFFSET: tuple[int, int] = (18, 18)  # px offset from the mouse cursor
WINDOW_MAX_WIDTH: int = 440
WINDOW_MAX_HEIGHT: int = 360
WINDOW_FONT_FAMILY: str = "Noto Sans CJK SC, WenQuanYi Micro Hei, sans-serif"

# --------------------------------------------------------------------------- #
# Translation core
# --------------------------------------------------------------------------- #
PREFER_LOCAL: bool = True               # consult local dictionary first
ONLINE_FALLBACK: bool = True            # hit an online API on local miss
ONLINE_API: str = "mymemory"            # 'mymemory' | 'google' | 'none'

# MyMemory free public endpoint (no key required, ~5k words/day anonymous quota).
# Works reliably in CN networks where Google is unreachable.
MYMEMORY_ENDPOINT: str = (
    "https://api.mymemory.translated.net/get?q={query}&langpair=en|zh-CN"
)
# Google Translate free public endpoint (no key required, may be unreachable
# in CN without a proxy). Kept as an option for users with connectivity.
GOOGLE_ENDPOINT: str = (
    "https://translate.googleapis.com/translate_a/single"
    "?client=gtx&sl=en&tl=zh-CN&dt=t&q={query}"
)
# Network timeout for online fallback (seconds).
ONLINE_TIMEOUT: float = 5.0
# User-Agent sent with online requests.
USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ECDICT release URL (SQLite build, ~30 MB zip). Used by dict_manager.
ECDICT_URL: str = (
    "https://github.com/skywind3000/ECDICT/releases/download/1.0.28/"
    "ecdict-sqlite-28.zip"
)
