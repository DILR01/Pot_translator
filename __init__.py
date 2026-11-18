"""Pot Translator - A Linux selection-translation tool.

Modules:
    config           - Central configuration & paths.
    dict_engine      - Local SQLite dictionary engine (ECDICT-compatible).
    selection_monitor- Global selection listener (xclip PRIMARY + clipboard).
    translator       - Translation core: local lookup with online fallback.
    floating_window  - PyQt5 frameless always-on-top result window.
    dict_manager     - CLI tool for dictionary import / management.
    main             - Entry point that wires everything together.
"""

__version__ = "1.0.0"
__author__ = "Pot Translator"
