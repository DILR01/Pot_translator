"""Local SQLite dictionary engine.

The engine is compatible with the ECDICT schema (https://github.com/skywind3000/ECDICT)
so the official ~770k-entry ECDICT database can be dropped in unchanged.
It also maintains a small user-words database for personal additions and
overrides which are merged on top of the ECDICT results.

Schema (ECDICT ``ec_dict`` table):
    word         TEXT PRIMARY KEY   -- lemma form
    phonetic     TEXT               -- IPA, e.g. /həˈloʊ/
    definition   TEXT               -- English definition
    translation  TEXT               -- Chinese translation (newline-separated)
    pos          TEXT               -- part of speech tags
    collins      INTEGER            -- Collins star rating 0-5
    oxford       INTEGER            -- 1 if in Oxford 3000
    tag          TEXT               -- frequency/origin tags
    bnc          INTEGER            -- BNC frequency rank
    frq          INTEGER            -- American frequency rank
    exchange     TEXT               -- inflection exchange string
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import config

log = logging.getLogger(__name__)

# emit the "dictionary not installed" hint at most once per process so the
# CLI output stays readable when the engine is constructed repeatedly.
_missing_dict_warned = False


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DictEntry:
    """One dictionary lookup result."""

    word: str
    phonetic: str = ""
    definition: str = ""
    translation: str = ""
    pos: str = ""
    collins: int = 0
    oxford: int = 0
    tag: str = ""
    frq: int = 0
    source: str = "ecdict"  # 'ecdict' | 'user'

    @property
    def is_empty(self) -> bool:
        return not (self.translation or self.definition)


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #
ECDICT_SCHEMA = """
CREATE TABLE IF NOT EXISTS ec_dict (
    word         TEXT PRIMARY KEY,
    phonetic     TEXT,
    definition   TEXT,
    translation  TEXT,
    pos          TEXT,
    collins      INTEGER DEFAULT 0,
    oxford       INTEGER DEFAULT 0,
    tag          TEXT,
    bnc          INTEGER DEFAULT 0,
    frq          INTEGER DEFAULT 0,
    exchange     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ec_word_lower ON ec_dict(LOWER(word));
"""

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_words (
    word         TEXT PRIMARY KEY,
    phonetic     TEXT,
    definition   TEXT,
    translation  TEXT,
    pos          TEXT,
    note         TEXT,
    added_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_word_lower ON user_words(LOWER(word));
"""

# Columns shared between ec_dict and user_words that we can SELECT uniformly.
SELECT_COLUMNS = (
    "word, phonetic, definition, translation, pos, "
    "COALESCE(collins,0) AS collins, COALESCE(oxford,0) AS oxford, "
    "tag, COALESCE(frq,0) AS frq"
)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class DictionaryEngine:
    """Thread-safe read access to the ECDICT + user-word databases.

    Two SQLite files are opened in read-only mode for the main dictionary and
    read-write for the user dictionary.  A per-file lock guards writes; reads
    use ``check_same_thread=False`` plus short-lived transactions so the UI
    thread and the selection monitor can both query safely.
    """

    def __init__(
        self,
        dict_path: Path = config.DICT_DB_PATH,
        user_path: Path = config.USER_DB_PATH,
    ) -> None:
        self.dict_path = Path(dict_path)
        self.user_path = Path(user_path)
        self._user_lock = threading.Lock()
        self._ensure_user_db()
        self._available = self.dict_path.exists()
        self._ecdict_table = self._detect_table()
        global _missing_dict_warned
        if not self._available and not _missing_dict_warned:
            _missing_dict_warned = True
            log.warning(
                "Dictionary DB not found at %s. Run "
                "`python -m pot_translator.dict_manager download-ecdict` "
                "to install the ECDICT database. Online fallback still works.",
                self.dict_path,
            )

    def _detect_table(self) -> str:
        """Detect the main dictionary table name.

        ECDICT ships the table as ``ec_dict`` in some builds and ``stardict``
        in the official 1.0.28 SQLite release; both share the same column
        layout. Returns ``"ec_dict"`` as a safe default when the DB is absent
        or no known table is found.
        """
        if not self._available:
            return "ec_dict"
        try:
            with self._connect(self.dict_path, read_only=True) as conn:
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            if "ec_dict" in names:
                return "ec_dict"
            if "stardict" in names:
                return "stardict"
        except sqlite3.Error as exc:
            log.error("table detection failed: %s", exc)
        return "ec_dict"

    # ------------------------------------------------------------------ #
    # connection helpers
    # ------------------------------------------------------------------ #
    @contextmanager
    def _connect(self, path: Path, *, read_only: bool) -> Iterator[sqlite3.Connection]:
        uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_user_db(self) -> None:
        """Create the user-words database & schema if missing."""
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.user_path) as conn:
            conn.executescript(USER_SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """True if the main ECDICT database is installed."""
        return self._available

    def count(self) -> int:
        """Number of entries in the main dictionary (0 if not installed)."""
        if not self._available:
            return 0
        try:
            with self._connect(self.dict_path, read_only=True) as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {self._ecdict_table}"
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as exc:
            log.error("count failed: %s", exc)
            return 0

    def lookup(self, word: str) -> DictEntry | None:
        """Look up a single word.

        Resolution order:
          1. user-words database (so user overrides win)
          2. exact case-sensitive match in ECDICT
          3. case-insensitive match in ECDICT
          4. simple morphological fallback (``-s``, ``-es``, ``-ed``, ``-ing``,
             ``-ly``, ``-er``, ``-est``)
        """
        word = (word or "").strip()
        if not word:
            return None

        # 1. user overrides
        entry = self._lookup_user(word)
        if entry and not entry.is_empty:
            return entry

        # 2/3. ECDICT exact then case-insensitive
        entry = self._lookup_ecdict(word) or self._lookup_ecdict(word, lower=True)
        if entry and not entry.is_empty:
            return entry

        # 4. morphological fallback
        for candidate in self._lemmatize(word):
            entry = (
                self._lookup_user(candidate)
                or self._lookup_ecdict(candidate)
                or self._lookup_ecdict(candidate, lower=True)
            )
            if entry and not entry.is_empty:
                return entry
        return None

    def search(self, pattern: str, limit: int = 20) -> list[DictEntry]:
        """Prefix search across the dictionary (for the manager UI)."""
        pattern = (pattern or "").strip().lower().replace("%", r"\%")
        if not pattern:
            return []
        like = f"{pattern}%"
        results: list[DictEntry] = []
        if self._available:
            with self._connect(self.dict_path, read_only=True) as conn:
                rows = conn.execute(
                    f"SELECT {SELECT_COLUMNS} FROM {self._ecdict_table} "
                    "WHERE word LIKE ? ESCAPE '\\' "
                    "ORDER BY frq DESC LIMIT ?",
                    (like, limit),
                ).fetchall()
                results.extend(self._row_to_entry(r, "ecdict") for r in rows)
        return results

    # ------------------------------------------------------------------ #
    # user-word management (used by dict_manager)
    # ------------------------------------------------------------------ #
    def add_user_word(
        self,
        word: str,
        translation: str,
        definition: str = "",
        phonetic: str = "",
        pos: str = "",
        note: str = "",
    ) -> None:
        with self._user_lock, self._connect(self.user_path, read_only=False) as conn:
            conn.execute(
                "INSERT INTO user_words (word, phonetic, definition, translation, pos, note) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(word) DO UPDATE SET "
                "phonetic=excluded.phonetic, definition=excluded.definition, "
                "translation=excluded.translation, pos=excluded.pos, note=excluded.note",
                (word.strip(), phonetic, definition, translation, pos, note),
            )
            conn.commit()

    def remove_user_word(self, word: str) -> int:
        with self._user_lock, self._connect(self.user_path, read_only=False) as conn:
            cur = conn.execute(
                "DELETE FROM user_words WHERE word = ?", (word.strip(),)
            )
            conn.commit()
            return cur.rowcount

    def list_user_words(self) -> list[DictEntry]:
        with self._connect(self.user_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT word, phonetic, definition, translation, pos, "
                "0 AS collins, 0 AS oxford, note AS tag, 0 AS frq "
                "FROM user_words ORDER BY added_at DESC"
            ).fetchall()
            return [self._row_to_entry(r, "user") for r in rows]

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _lookup_user(self, word: str) -> DictEntry | None:
        try:
            with self._connect(self.user_path, read_only=True) as conn:
                row = conn.execute(
                    "SELECT word, phonetic, definition, translation, pos, "
                    "0 AS collins, 0 AS oxford, note AS tag, 0 AS frq "
                    "FROM user_words WHERE word = ?",
                    (word,),
                ).fetchone()
                return self._row_to_entry(row, "user") if row else None
        except sqlite3.Error as exc:
            log.error("user lookup failed: %s", exc)
            return None

    def _lookup_ecdict(self, word: str, *, lower: bool = False) -> DictEntry | None:
        if not self._available:
            return None
        try:
            with self._connect(self.dict_path, read_only=True) as conn:
                if lower:
                    row = conn.execute(
                        f"SELECT {SELECT_COLUMNS} FROM {self._ecdict_table} "
                        "WHERE word = ? COLLATE NOCASE LIMIT 1",
                        (word,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT {SELECT_COLUMNS} FROM {self._ecdict_table} "
                        "WHERE word = ? LIMIT 1",
                        (word,),
                    ).fetchone()
                return self._row_to_entry(row, "ecdict") if row else None
        except sqlite3.Error as exc:
            log.error("ecdict lookup failed: %s", exc)
            return None

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, source: str) -> DictEntry:
        return DictEntry(
            word=row["word"],
            phonetic=row["phonetic"] or "",
            definition=row["definition"] or "",
            translation=row["translation"] or "",
            pos=row["pos"] or "",
            collins=int(row["collins"] or 0),
            oxford=int(row["oxford"] or 0),
            tag=row["tag"] or "",
            frq=int(row["frq"] or 0),
            source=source,
        )

    @staticmethod
    def _lemmatize(word: str) -> list[str]:
        """Yield candidate lemmas for an inflected English word.

        Conservative rules covering the common English inflections. We do not
        attempt to be a full lemmatizer -- ECDICT's own ``exchange`` field is
        the authoritative source, but it's only reachable once we have a hit,
        so these heuristics help on the initial miss.
        """
        w = word.lower()
        candidates: list[str] = []
        # ordered so the most specific suffix is tried first
        suffix_rules = (
            ("ies", "y"),   # stories -> story
            ("ied", "y"),   # studied -> study
            ("ying", "y"),  # lying -> lie  (handled below, leave for safety)
            ("ing", ""),    # walking -> walk
            ("ing", "e"),   # making -> make
            ("ed", ""),     # walked -> walk
            ("ed", "e"),    # baked -> bake
            ("ied", "y"),   # (dup-safe)
            ("es", ""),     # boxes -> box
            ("es", ""),     # watches -> watch
            ("s", ""),      # cats -> cat
            ("ly", ""),     # quickly -> quick
            ("er", ""),     # taller -> tall
            ("er", "e"),    # writer -> write (loose)
            ("est", ""),    # tallest -> tall
            ("est", "e"),   # largest -> large
            ("'s", ""),     # possessive
            ("s'", ""),     # possessive plural
        )
        for suffix, repl in suffix_rules:
            if w.endswith(suffix):
                candidate = w[: -len(suffix)] + repl
                if len(candidate) >= 2 and candidate not in candidates:
                    candidates.append(candidate)
        # also drop a trailing apostrophe (rolin' -> rolin)
        if w.endswith("'"):
            candidates.append(w[:-1])
        # dedupe-preserving order, drop the original token
        seen: set[str] = set()
        unique: list[str] = []
        for c in candidates:
            if c and c != w and c not in seen:
                seen.add(c)
                unique.append(c)
        return unique


# --------------------------------------------------------------------------- #
# module-level helpers
# --------------------------------------------------------------------------- #
_NON_ALPHA = re.compile(r"[^A-Za-z'-]")


def normalize_selection(text: str) -> str:
    """Trim a raw selection down to a single candidate word/phrase.

    Used by the translator before it decides whether to do a dictionary
    lookup or to send the text to the online API.
    """
    text = (text or "").strip().strip(".,!?;:\"'`()[]{}")
    return text
