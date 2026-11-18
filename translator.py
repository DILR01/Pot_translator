"""Translation core.

Coordinates local dictionary lookup with an online fallback.  The pipeline
for a single selection is:

    raw text
      |
      v
    +--> normalize
      |
      v
    +--> single English word?  -- local ECDICT lookup (with lemma fallback)
      |        |
      |        +-- hit  -> TranslationResult(source='local')
      |        +-- miss --+
      v                   |
    +--> online API <-----+
      |
      v
    TranslationResult(source='google' | 'youdao' | 'none')

The translator is fully synchronous and intended to be called from a worker
thread (the main orchestrator does this).  It never touches Qt directly.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests

from . import config
from .dict_engine import DictEntry, DictionaryEngine

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
@dataclass
class TranslationResult:
    """Everything the floating window needs to render one translation."""

    query: str
    translation: str = ""          # Chinese translation / definition
    phonetic: str = ""             # IPA, if known
    definition: str = ""           # English definition, if known
    pos: str = ""                  # part of speech
    examples: list[str] = field(default_factory=list)
    source: str = "none"           # 'local' | 'google' | 'youdao' | 'none'
    error: str = ""                # populated when source == 'none' on miss
    extra: str = ""                # free-form extra info (collins/oxford, etc.)

    @property
    def ok(self) -> bool:
        return bool(self.translation) or bool(self.definition)

    @property
    def is_local(self) -> bool:
        return self.source == "local"


# --------------------------------------------------------------------------- #
# Translator
# --------------------------------------------------------------------------- #
class Translator:
    """High-level fa&ccedil;ade wrapping the local engine + online APIs."""

    def __init__(self, engine: Optional[DictionaryEngine] = None) -> None:
        self.engine = engine or DictionaryEngine()

    # ------------------------------------------------------------------ #
    # public entry point
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> TranslationResult:
        text = (text or "").strip()
        if not text:
            return TranslationResult(query="", error="empty input")

        # local dictionary first (single word only -- the dictionary has no
        # phrase table worth speaking of)
        if config.PREFER_LOCAL and self._looks_like_word(text):
            entry = self.engine.lookup(text)
            if entry and not entry.is_empty:
                return self._from_entry(text, entry)

        # online fallback
        if config.ONLINE_FALLBACK and config.ONLINE_API != "none":
            return self._translate_online(text)
        return TranslationResult(
            query=text,
            source="none",
            error="no local entry and online fallback disabled",
        )

    # ------------------------------------------------------------------ #
    # local
    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_word(text: str) -> bool:
        return text.isascii() and text.replace("-", "").replace("'", "").isalpha()

    def _from_entry(self, query: str, entry: DictEntry) -> TranslationResult:
        extras: list[str] = []
        if entry.collins:
            extras.append(f"Collins {'*' * entry.collins}")
        if entry.oxford:
            extras.append("Oxford 3000")
        if entry.tag:
            extras.append(entry.tag)
        if entry.frq:
            extras.append(f"frq:{entry.frq}")
        return TranslationResult(
            query=query,
            translation=entry.translation,
            phonetic=entry.phonetic,
            definition=entry.definition,
            pos=entry.pos,
            source="local",
            extra="  ".join(extras),
        )

    # ------------------------------------------------------------------ #
    # online
    # ------------------------------------------------------------------ #
    def _translate_online(self, text: str) -> TranslationResult:
        api = config.ONLINE_API
        try:
            if api == "google":
                return self._google(text)
            if api == "mymemory":
                return self._mymemory(text)
        except requests.RequestException as exc:
            log.warning("online %s failed: %s", api, exc)
            return TranslationResult(query=text, source="none", error=str(exc))
        except Exception as exc:  # JSON errors, etc.
            log.exception("online %s parse failed", api)
            return TranslationResult(query=text, source="none", error=str(exc))
        return TranslationResult(
            query=text, source="none", error=f"unknown api '{api}'"
        )

    def _google(self, text: str) -> TranslationResult:
        url = config.GOOGLE_ENDPOINT.format(
            query=urllib.parse.quote(text, safe="")
        )
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.ONLINE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Google returns a nested list: [[["你好","hello",null,...], ...], ...]
        sentences: list[str] = []
        if isinstance(data, list) and data and isinstance(data[0], list):
            for chunk in data[0]:
                if isinstance(chunk, list) and chunk:
                    sentences.append(chunk[0])
        translation = "".join(sentences).strip()
        # try to recover a phonetic from the third-level list
        phonetic = ""
        try:
            if len(data) > 1 and data[1] and isinstance(data[1][0], list):
                phonetic = data[1][0][-1] or ""
        except (IndexError, TypeError):
            pass
        return TranslationResult(
            query=text,
            translation=translation,
            phonetic=phonetic,
            source="google",
        )

    def _mymemory(self, text: str) -> TranslationResult:
        """MyMemory free translation API.

        Response shape:
            {"responseData": {"translatedText": "...", "match": 0.98},
             "responseStatus": 200, ...}
        The endpoint occasionally returns an English error string in
        ``translatedText`` (e.g. "PLEASE SELECT TWO LANGUAGE") -- we detect
        those and surface them as an error instead of a translation.
        """
        url = config.MYMEMORY_ENDPOINT.format(
            query=urllib.parse.quote(text, safe="")
        )
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.ONLINE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        status = int(data.get("responseStatus", 0))
        body = data.get("responseData") or {}
        translation = (body.get("translatedText") or "").strip()
        # MyMemory signals quota / language errors via an English notice in
        # translatedText; treat non-CJK ASCII results as failures so we don't
        # show "PLEASE SELECT TWO LANGUAGE" as a "translation".
        if translation and not _has_cjk(translation) and text.isascii():
            # allow short ascii mirror (e.g. proper nouns) but flag notices
            upper = translation.upper()
            notices = (
                "PLEASE SELECT", "MYMEMORY WARNING", "QUERY LENGTH LIMIT",
                "INVALID", "ERROR",
            )
            if any(n in upper for n in notices) or status not in (200, 403):
                raise RuntimeError(
                    f"mymemory: {translation[:120]} (status={status})"
                )
        return TranslationResult(
            query=text, translation=translation, source="mymemory",
        )


def _has_cjk(text: str) -> bool:
    """True if ``text`` contains any CJK Unified Ideograph."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)
