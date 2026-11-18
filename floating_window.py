"""Floating result window (PyQt5).

A frameless, always-on-top, semi-transparent panel that pops up next to the
cursor whenever a new translation is ready.  Features:

* auto-positioned near the mouse with screen-edge awareness
* drag to relocate
* Esc / middle-click / click-away to dismiss
* auto-hide after ``WINDOW_HIDE_TIMEOUT_MS`` of inactivity
* graceful handling of long translations (rich-text with wrapping)
* pin button to keep it open until manually closed
* "save to user dict" action so users can curate their own entries

The window talks to the rest of the application through three signals:

* ``pinned``   -- the user toggled the pin
* ``save_word``-- the user asked to save the current query to the user dict
* ``copy``     -- the user asked to copy the translation

The orchestrator connects to these to drive the dictionary manager / clipboard.
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import (
    Qt,
    QEvent,
    QPoint,
    QTimer,
    QSize,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QCursor,
    QFont,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPalette,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import config
from .translator import TranslationResult

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #
STYLESHEET = f"""
QWidget#PotWindow {{
    background: rgb(28, 30, 38);
    border: 1px solid rgba(120, 160, 255, 110);
    border-radius: 10px;
}}
QLabel#header {{
    color: #8fb6ff;
    font-family: {config.WINDOW_FONT_FAMILY};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#word {{
    color: #ffffff;
    font-family: {config.WINDOW_FONT_FAMILY};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#phonetic {{
    color: #ffb86c;
    font-family: "DejaVu Sans", sans-serif;
    font-size: 13px;
    font-style: italic;
}}
QLabel#pos {{
    color: #bd93f9;
    font-family: {config.WINDOW_FONT_FAMILY};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#extra {{
    color: #6272a4;
    font-family: {config.WINDOW_FONT_FAMILY};
    font-size: 10px;
}}
QTextEdit#body {{
    background: transparent;
    color: #f8f8f2;
    font-family: {config.WINDOW_FONT_FAMILY};
    font-size: 14px;
    border: none;
    selection-background-color: #44475a;
}}
QPushButton#iconBtn {{
    background: transparent;
    border: none;
    color: #bd93f9;
    padding: 2px 4px;
    font-size: 13px;
}}
QPushButton#iconBtn:hover {{ color: #ffffff; }}
QLabel#sourceTag {{
    color: #50fa7b;
    font-size: 10px;
    font-family: monospace;
}}
QLabel#sourceTag.local   {{ color: #50fa7b; }}
QLabel#sourceTag.google  {{ color: #8be9fd; }}
QLabel#sourceTag.youdao  {{ color: #f1fa8c; }}
QLabel#sourceTag.none    {{ color: #ff5555; }}
"""


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
class FloatingWindow(QFrame):
    """The popup result window."""

    # -- signals -------------------------------------------------------------
    pinned = pyqtSignal(bool)          # is_pinned
    save_word = pyqtSignal(str, str)   # word, translation
    copy_requested = pyqtSignal(str)   # text

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PotWindow")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMaximumWidth(config.WINDOW_MAX_WIDTH)
        self.setMaximumHeight(config.WINDOW_MAX_HEIGHT)

        self._is_pinned = False
        self._drag_offset: Optional[QPoint] = None
        self._current: Optional[TranslationResult] = None

        self._build_ui()
        self.setStyleSheet(STYLESHEET)

        # auto-hide timer
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        # shortcuts
        QShortcut(QKeySequence("Esc"), self, activated=self.hide)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._emit_save)
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self._emit_copy)
        QShortcut(QKeySequence("Ctrl+Shift+P"), self, activated=self._toggle_pin)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(4)

        # ---- top row: word + phonetic ----
        top = QHBoxLayout()
        top.setSpacing(8)
        self.word_label = QLabel("")
        self.word_label.setObjectName("word")
        self.phonetic_label = QLabel("")
        self.phonetic_label.setObjectName("phonetic")
        top.addWidget(self.word_label)
        top.addWidget(self.phonetic_label, 1, Qt.AlignLeft | Qt.AlignVCenter)
        # toolbar (pin / copy / save)
        self.pin_btn = self._icon_button("\u25a3", "Pin (Ctrl+Shift+P)", self._toggle_pin)
        self.copy_btn = self._icon_button("\u2398", "Copy (Ctrl+C)", self._emit_copy)
        self.save_btn = self._icon_button("\u2605", "Save to user dict (Ctrl+S)", self._emit_save)
        for btn in (self.pin_btn, self.copy_btn, self.save_btn):
            top.addWidget(btn)
        root.addLayout(top)

        # ---- header row: pos + extra + source ----
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.pos_label = QLabel("")
        self.pos_label.setObjectName("pos")
        self.extra_label = QLabel("")
        self.extra_label.setObjectName("extra")
        self.source_label = QLabel("")
        self.source_label.setObjectName("sourceTag")
        meta.addWidget(self.pos_label)
        meta.addWidget(self.extra_label, 1)
        meta.addWidget(self.source_label, 0, Qt.AlignRight)
        root.addLayout(meta)

        # ---- body ----
        self.body = QTextEdit()
        self.body.setObjectName("body")
        self.body.setReadOnly(True)
        self.body.setFrameStyle(QFrame.NoFrame)
        self.body.setPlaceholderText("...")
        self.body.setViewportMargins(0, 0, 0, 0)
        root.addWidget(self.body, 1)

        # Install event filter on header labels so dragging them moves the
        # whole window (mouse events on children are swallowed otherwise).
        self._drag_labels = [
            self.word_label, self.phonetic_label,
            self.pos_label, self.extra_label, self.source_label,
        ]
        for lbl in self._drag_labels:
            lbl.setCursor(Qt.SizeAllCursor)
            lbl.installEventFilter(self)

    @staticmethod
    def _icon_button(text: str, tooltip: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("iconBtn")
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(24, 24)
        btn.setFlat(True)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.clicked.connect(slot)
        return btn

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def show_result(self, result: TranslationResult, x: int = -1, y: int = -1) -> None:
        """Populate the window with ``result`` and show it near (x, y)."""
        self._current = result
        self.word_label.setText(result.query or "")
        self.phonetic_label.setText(result.phonetic or "")
        self.pos_label.setText(result.pos or "")
        self.extra_label.setText(result.extra or "")
        self.source_label.setText(result.source.upper() if result.source else "")
        self.source_label.setProperty("class", result.source)
        # refresh stylesheet so the property-based source colour applies
        self.source_label.style().unpolish(self.source_label)
        self.source_label.style().polish(self.source_label)

        self.body.setHtml(self._render_body(result))
        self.body.document().setTextWidth(config.WINDOW_MAX_WIDTH - 24)

        self.adjustSize()
        self._place(x, y)
        if not self._is_pinned:
            self._hide_timer.start(config.WINDOW_HIDE_TIMEOUT_MS)
        self.show()
        self.raise_()

    def place_near_cursor(self) -> None:
        pos = QCursor.pos()
        self._place(pos.x(), pos.y())

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_body(result: TranslationResult) -> str:
        parts: list[str] = []
        if result.translation:
            parts.append(
                f'<div style="line-height:150%;">{result.translation}</div>'
            )
        if result.definition:
            parts.append(
                f'<div style="margin-top:6px;color:#8be9fd;font-size:12px;">'
                f'<b>EN:</b> {result.definition}</div>'
            )
        if result.examples:
            ex = "<br>".join(result.examples[:3])
            parts.append(
                f'<div style="margin-top:6px;color:#6272a4;font-size:12px;">'
                f'<b>EX:</b><br>{ex}</div>'
            )
        if result.error:
            parts.append(
                f'<div style="color:#ff5555;font-size:12px;">{result.error}</div>'
            )
        return "".join(parts) or "<i style='color:#6272a4'>no result</i>"

    # ------------------------------------------------------------------ #
    # placement / sizing
    # ------------------------------------------------------------------ #
    def _place(self, x: int, y: int) -> None:
        screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        w, h = self.width(), self.height()
        if x < 0 or y < 0:
            # fall back to the cursor location
            p = QCursor.pos()
            x, y = p.x(), p.y()
        px = x + config.WINDOW_OFFSET[0]
        py = y + config.WINDOW_OFFSET[1]
        if geo:
            if px + w > geo.right():
                px = max(geo.left(), x - w - config.WINDOW_OFFSET[0])
            if py + h > geo.bottom():
                py = max(geo.top(), y - h - config.WINDOW_OFFSET[1])
        self.move(px, py)

    def sizeHint(self) -> QSize:
        return QSize(
            min(config.WINDOW_MAX_WIDTH, 360),
            min(config.WINDOW_MAX_HEIGHT, 220),
        )

    # ------------------------------------------------------------------ #
    # interaction
    # ------------------------------------------------------------------ #
    def _toggle_pin(self) -> None:
        self._is_pinned = not self._is_pinned
        self.pin_btn.setText("\U0001F4CC" if self._is_pinned else "\u25a3")
        if self._is_pinned:
            self._hide_timer.stop()
        else:
            self._hide_timer.start(config.WINDOW_HIDE_TIMEOUT_MS)
        self.pinned.emit(self._is_pinned)

    def _emit_save(self) -> None:
        if self._current and self._current.query:
            self.save_word.emit(
                self._current.query, self._current.translation
            )

    def _emit_copy(self) -> None:
        if self._current:
            text = self._current.translation or self._current.definition
            if text:
                self.copy_requested.emit(text)

    # drag-to-move -----------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        event.accept()

    def eventFilter(self, obj, event):  # noqa: N802
        """Forward mouse press/move/release from header labels to parent."""
        if obj in getattr(self, "_drag_labels", ()):
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
                return True
            if etype == QEvent.MouseMove and self._drag_offset is not None:
                self.move(event.globalPos() - self._drag_offset)
                return True
            if etype == QEvent.MouseButtonRelease:
                self._drag_offset = None
                return True
        return super().eventFilter(obj, event)

    # middle-click dismiss ---------------------------------------------------
    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self.hide()
            event.accept()

    def enterEvent(self, event):  # noqa: N802
        # pause auto-hide while the user is interacting
        if not self._is_pinned:
            self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        if not self._is_pinned:
            self._hide_timer.start(config.WINDOW_HIDE_TIMEOUT_MS)
        super().leaveEvent(event)
