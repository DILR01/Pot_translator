"""Entry point & orchestrator.

Wires the dictionary engine, selection monitor, translator and floating
window together and runs the Qt event loop.  The selection monitor runs in
its own daemon thread; translation work runs on a dedicated QThread so the
GUI never freezes on network calls.  Cross-thread hand-off is done via a
thread-safe ``queue.Queue`` that a 50ms QTimer drains on the GUI thread.

Usage:
    python -m pot_translator.main                 # default run
    python -m pot_translator.main --api none      # local-only
    python -m pot_translator.main --no-tray       # no system-tray icon
    python -m pot_translator.main -v              # verbose logging
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal

# Force XWayland so that QCursor.pos() and QWidget.move() work reliably.
# Under native Wayland these APIs are broken for frameless windows.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
import sys
import threading
from typing import Optional

from PyQt5.QtCore import QThread, QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QClipboard
from PyQt5.QtGui import QIcon, QPixmap, QColor, QPainter, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QAction,
    QMessageBox,
)

from . import __version__, config
from .dict_engine import DictionaryEngine
from .floating_window import FloatingWindow
from .selection_monitor import SelectionEvent, SelectionMonitor
from .translator import TranslationResult, Translator

log = logging.getLogger("pot_translator")


# --------------------------------------------------------------------------- #
# Worker: runs translation off the GUI thread
# --------------------------------------------------------------------------- #
class TranslationWorker(QObject):
    """Long-lived worker living on a background QThread.

    Requests are submitted via the thread-safe ``submit()`` method; results
    are returned through ``result_ready`` (auto-queued to the GUI thread).
    """

    result_ready = pyqtSignal(object, int, int)  # TranslationResult, x, y

    def __init__(self, translator: Translator) -> None:
        super().__init__()
        self.translator = translator
        self._queue: "queue.Queue[tuple[str, int, int]]" = queue.Queue()
        self._stop = threading.Event()

    def submit(self, text: str, x: int, y: int) -> None:
        """Thread-safe: enqueue a translation request."""
        self._queue.put((text, x, y))

    def stop(self) -> None:
        self._stop.set()

    @pyqtSlot()
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                text, x, y = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = self.translator.translate(text)
            except Exception as exc:  # never let the worker die
                log.exception("translation crashed")
                result = TranslationResult(query=text, source="none", error=str(exc))
            self.result_ready.emit(result, x, y)


# --------------------------------------------------------------------------- #
# Controller: bridges monitor thread -> Qt
# --------------------------------------------------------------------------- #
class Controller(QObject):
    """Owns every component and routes events between them."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        # apply CLI overrides
        if args.api:
            config.ONLINE_API = args.api
        if args.local_only:
            config.ONLINE_FALLBACK = False
        if args.no_local:
            config.PREFER_LOCAL = False

        self.engine = DictionaryEngine()
        self.translator = Translator(self.engine)
        self.window = FloatingWindow()

        # cross-thread bridge from the selection monitor (pure-Python thread)
        self._event_queue: "queue.Queue[SelectionEvent]" = queue.Queue()
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._drain_events)
        self._drain_timer.start()

        # translation worker on its own QThread
        self.worker = TranslationWorker(self.translator)
        self.worker_thread = QThread()
        self.worker_thread.setObjectName("TranslationWorkerThread")
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.result_ready.connect(self._on_result)
        self.worker_thread.start()

        # selection monitor
        self.monitor = SelectionMonitor(self._on_selection)
        self.monitor.start()

        # window signals
        self.window.save_word.connect(self._on_save_word)
        self.window.copy_requested.connect(self._on_copy)

        # tray
        self.tray: Optional[QSystemTrayIcon] = None
        if not args.no_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._build_tray()

    # ------------------------------------------------------------------ #
    # monitor -> GUI bridge
    # ------------------------------------------------------------------ #
    def _on_selection(self, event: SelectionEvent) -> None:
        # called on the monitor thread; just enqueue, never touch Qt here
        self._event_queue.put(event)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_selection(event)

    def _handle_selection(self, event: SelectionEvent) -> None:
        log.debug("selection: %r @ (%d,%d) word=%s",
                  event.text, event.mouse_x, event.mouse_y, event.is_word)
        # show a placeholder immediately so the user sees feedback
        placeholder = TranslationResult(
            query=event.text, translation="…", source="none"
        )
        self.window.show_result(placeholder, event.mouse_x, event.mouse_y)
        # submit the real translation
        self.worker.submit(event.text, event.mouse_x, event.mouse_y)

    @pyqtSlot(object, int, int)
    def _on_result(self, result: TranslationResult, x: int, y: int) -> None:
        # only refresh if the user hasn't selected something newer in between;
        # since the worker is FIFO this is always the right thing to show.
        self.window.show_result(result, x, y)

    # ------------------------------------------------------------------ #
    # window signal handlers
    # ------------------------------------------------------------------ #
    def _on_save_word(self, word: str, translation: str) -> None:
        try:
            self.engine.add_user_word(word=word, translation=translation)
            log.info("saved user word: %s", word)
            if self.tray:
                self.tray.showMessage(
                    "Pot Translator",
                    f"Saved '{word}' to user dictionary",
                    QSystemTrayIcon.Information,
                    2000,
                )
        except Exception as exc:
            log.error("save user word failed: %s", exc)

    def _on_copy(self, text: str) -> None:
        cb = QApplication.clipboard()
        if cb:
            cb.setText(text)
            log.debug("copied %d chars to clipboard", len(text))

    # ------------------------------------------------------------------ #
    # tray
    # ------------------------------------------------------------------ #
    def _build_tray(self) -> None:
        icon = self._make_icon()
        self.tray = QSystemTrayIcon(icon, parent=self)
        self.tray.setToolTip(f"Pot Translator {__version__}")
        menu = QMenu()

        act_translate = QAction("Translate clipboard", menu)
        act_translate.triggered.connect(self._translate_clipboard)
        menu.addAction(act_translate)

        act_search = QAction("Dictionary count", menu)
        act_search.triggered.connect(self._show_dict_info)
        menu.addAction(act_search)

        menu.addSeparator()
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    @staticmethod
    def _make_icon() -> QIcon:
        # render a simple "A→中" badge so we don't depend on an asset file
        pix = QPixmap(64, 64)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#bd93f9"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(2, 2, 60, 60, 14, 14)
        p.setPen(QColor("#1e1f29"))
        f = QFont("Noto Sans CJK SC", 20, QFont.Bold)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignCenter, "译")
        p.end()
        return QIcon(pix)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._translate_clipboard()

    def _translate_clipboard(self) -> None:
        cb = QApplication.clipboard()
        if not cb:
            return
        text = cb.text(QClipboard.Clipboard).strip()
        if text:
            self.window.place_near_cursor()
            self.worker.submit(text, -1, -1)

    def _show_dict_info(self) -> None:
        count = self.engine.count()
        msg = f"Local dictionary: {count:,} entries" if count else \
              "Local dictionary not installed.\nRun: python -m pot_translator.dict_manager --download-ecdict"
        if self.tray:
            self.tray.showMessage("Pot Translator", msg, QSystemTrayIcon.Information, 4000)
        else:
            QMessageBox.information(None, "Pot Translator", msg)

    # ------------------------------------------------------------------ #
    # teardown
    # ------------------------------------------------------------------ #
    def _quit(self) -> None:
        log.info("shutting down")
        self.monitor.stop()
        self.worker.stop()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        if self.tray:
            self.tray.hide()
        QApplication.quit()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pot_translator",
        description="Linux selection-translation tool (local ECDICT + online fallback).",
    )
    p.add_argument("--api", choices=["mymemory", "google", "none"], default=None,
                   help=f"override online API (default: {config.ONLINE_API})")
    p.add_argument("--local-only", action="store_true",
                   help="disable online fallback; only use the local dictionary")
    p.add_argument("--no-local", action="store_true",
                   help="skip the local dictionary; only use the online API")
    p.add_argument("--no-tray", action="store_true",
                   help="do not create a system-tray icon")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("Pot Translator %s starting", __version__)

    # High-DPI niceties
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Pot Translator")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)  # we live in the tray

    # let Ctrl-C kill us cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    controller = Controller(args)
    exit_code = app.exec_()
    return exit_code


# allow `python -m pot_translator.main`
if __name__ == "__main__":
    sys.exit(main())
