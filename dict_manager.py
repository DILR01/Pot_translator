"""Dictionary management CLI.

Sub-commands:
    info                          show installed dictionary stats
    download-ecdict               fetch & install the official ECDICT SQLite db
    import-csv FILE               import a custom CSV word list
    import-sqlite FILE            install an arbitrary SQLite file as the dict
    add WORD TRANSLATION [...]    add (or override) a user-word entry
    remove WORD                   delete a user-word entry
    list [--pattern PATTERN]      list user words (optionally filtered)
    lookup WORD                   test local lookup against the engine
    search PREFIX                 prefix-search the main dictionary

Examples:
    python -m pot_translator.dict_manager download-ecdict
    python -m pot_translator.dict_manager add hello "你好" --phonetic "/həˈloʊ/"
    python -m pot_translator.dict_manager lookup running
    python -m pot_translator.dict_manager import-csv my-words.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import config
from .dict_engine import DictionaryEngine, ECDICT_SCHEMA

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #
def cmd_info(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    print("Pot Translator - dictionary info")
    print("-" * 50)
    print(f"  ECDICT path : {config.DICT_DB_PATH}")
    print(f"  Installed   : {'yes' if engine.available else 'NO'}")
    if engine.available:
        size = config.DICT_DB_PATH.stat().st_size
        print(f"  Size        : {size / 1_048_576:.1f} MB")
        print(f"  Entries     : {engine.count():,}")
    print(f"  User db path: {config.USER_DB_PATH}")
    user_words = engine.list_user_words()
    print(f"  User words  : {len(user_words)}")
    if user_words and args.verbose:
        print("  -- user words --")
        for w in user_words[:20]:
            print(f"     {w.word:20s}  {w.translation[:40]}")
    return 0


# --------------------------------------------------------------------------- #
# download-ecdict
# --------------------------------------------------------------------------- #
def cmd_download_ecdict(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    """Download & install the official ECDICT SQLite database."""
    dest = config.DICT_DB_PATH
    if dest.exists() and not args.force:
        print(f"Dictionary already installed at {dest}")
        print(f"  Use --force to re-download.")
        return 0

    import requests  # local import; requests is a project dependency

    url = config.ECDICT_URL
    partial = config.DATA_DIR / "ecdict.zip.part"
    tmp_zip = config.DATA_DIR / "ecdict.zip"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading ECDICT (resumable) from:\n  {url}")
    print(f"  -> {partial}")

    max_attempts = 40
    total = None
    for attempt in range(1, max_attempts + 1):
        have = partial.stat().st_size if partial.exists() else 0
        if total and have >= total:
            break
        headers = {"User-Agent": config.USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with requests.get(
                url, headers=headers, stream=True, timeout=(12, 25)
            ) as resp:
                if resp.status_code not in (200, 206):
                    print(f"  attempt {attempt}: HTTP {resp.status_code}; retrying",
                          file=sys.stderr)
                    time.sleep(2)
                    continue
                if total is None:
                    # 206 responses carry Content-Range; 200 carries Content-Length
                    cr = resp.headers.get("Content-Range", "")
                    if have and cr.endswith("/"):
                        try:
                            total = int(cr.rsplit("/", 1)[1])
                        except (ValueError, IndexError):
                            total = int(resp.headers.get("Content-Length", "0")) + have
                    else:
                        total = int(resp.headers.get("Content-Length", "0")) + have
                mode = "ab" if have else "wb"
                with open(partial, mode) as out:
                    last_print = time.monotonic()
                    for chunk in resp.iter_content(65536):
                        if not chunk:
                            break
                        out.write(chunk)
                        have += len(chunk)
                        now = time.monotonic()
                        if now - last_print >= 1.0:
                            pct = (have * 100 // total) if total else 0
                            mb = have / 1_048_576
                            tmb = (total / 1_048_576) if total else 0
                            sys.stdout.write(
                                f"\r  {mb:.1f} / {tmb:.1f} MB ({pct}%)"
                                + " " * 8
                            )
                            sys.stdout.flush()
                            last_print = now
            if total and have >= total:
                print()
                break
            print(f"\n  attempt {attempt}: paused at "
                  f"{have / 1_048_576:.1f} MB; resuming...", file=sys.stderr)
        except (requests.RequestException, OSError) as exc:
            print(f"\n  attempt {attempt}: {type(exc).__name__}: {str(exc)[:80]}",
                  file=sys.stderr)
            time.sleep(1.5)
    else:
        print("Download failed after all retries.", file=sys.stderr)
        return 1

    have = partial.stat().st_size
    if total and have < total:
        print(f"Download incomplete: {have} / {total} bytes", file=sys.stderr)
        return 1
    print(f"Download complete: {have:,} bytes")
    partial.replace(tmp_zip)

    print("Extracting...")
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            # find the .db file inside the archive
            db_name = next(
                (n for n in names if n.lower().endswith(".db")), None
            )
            if not db_name:
                print("No .db file inside the archive!", file=sys.stderr)
                return 1
            print(f"  found {db_name}")
            with zf.open(db_name) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
    finally:
        tmp_zip.unlink(missing_ok=True)

    # sanity-check schema (accept either the ec_dict or stardict layout)
    try:
        with sqlite3.connect(dest) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        if not (tables & {"ec_dict", "stardict"}):
            print(f"Warning: no 'ec_dict'/'stardict' table in {dest}",
                  file=sys.stderr)
            print(f"  Tables present: {sorted(tables)}", file=sys.stderr)
    except sqlite3.Error as exc:
        print(f"Schema check failed: {exc}", file=sys.stderr)

    size = dest.stat().st_size
    print(f"Installed: {dest}  ({size / 1_048_576:.1f} MB)")
    print("Run `python -m pot_translator.dict_manager info` to verify.")
    return 0


# --------------------------------------------------------------------------- #
# import-sqlite
# --------------------------------------------------------------------------- #
def cmd_import_sqlite(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(f"File not found: {src}", file=sys.stderr)
        return 1
    dest = config.DICT_DB_PATH
    print(f"Installing {src.name} as the main dictionary...")
    # validate it has the right table
    try:
        with sqlite3.connect(src) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    except sqlite3.Error as exc:
        print(f"Cannot open source db: {exc}", file=sys.stderr)
        return 1
    if "ec_dict" not in tables:
        if args.remap:
            print(f"  'ec_dict' not found; tables={sorted(tables)}")
            print("  Attempting schema migration from candidate table...")
            if not _migrate_to_ecdict(src, dest):
                return 1
        else:
            print(f"Source has no 'ec_dict' table (found: {sorted(tables)})",
                  file=sys.stderr)
            print("Pass --remap to attempt migration.", file=sys.stderr)
            return 1
    else:
        shutil.copyfile(src, dest)
    print(f"Installed: {dest}")
    return 0


def _migrate_to_ecdict(src: Path, dest: Path) -> bool:
    """Best-effort migration: copy any table with ECDICT-like columns."""
    try:
        with sqlite3.connect(src) as conn:
            cols_by_table: dict[str, list[str]] = {}
            for (tname,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall():
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{tname}')").fetchall()]
                cols_by_table[tname] = cols
        # pick the table with the most ECDICT columns present
        wanted = {"word", "phonetic", "definition", "translation", "pos"}
        best, best_score = None, 0
        for tname, cols in cols_by_table.items():
            score = len(wanted & set(cols))
            if score > best_score:
                best, best_score = tname, score
        if best is None or best_score < 3:
            print("  No candidate table with enough ECDICT-like columns.",
                  file=sys.stderr)
            return False
        print(f"  Migrating table '{best}' (score={best_score}) -> ec_dict")
        # build a fresh db with the canonical schema, then INSERT...SELECT
        if dest.exists():
            dest.unlink()
        with sqlite3.connect(dest) as out:
            out.executescript(ECDICT_SCHEMA)
            out.commit()
            with sqlite3.connect(src) as src_conn:
                src_cols = [r[1] for r in src_conn.execute(f"PRAGMA table_info('{best}')").fetchall()]
                # intersect with our canonical column list
                target_cols = [
                    "word", "phonetic", "definition", "translation", "pos",
                    "collins", "oxford", "tag", "bnc", "frq", "exchange",
                ]
                present = [c for c in target_cols if c in src_cols]
                col_list = ",".join(present)
                rows = src_conn.execute(f"SELECT {col_list} FROM {best}").fetchall()
                placeholders = ",".join("?" * len(present))
                out.executemany(
                    f"INSERT OR IGNORE INTO ec_dict ({col_list}) VALUES ({placeholders})",
                    rows,
                )
                out.commit()
        return True
    except sqlite3.Error as exc:
        print(f"  Migration failed: {exc}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# import-csv
# --------------------------------------------------------------------------- #
def cmd_import_csv(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    """Import user words from a CSV file.

    Recognised header columns (case-insensitive, any subset):
        word, phonetic, definition, translation, pos, note
    If no header is present, the file is treated as `word,translation` rows.
    """
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(f"File not found: {src}", file=sys.stderr)
        return 1

    # detect encoding
    raw = src.read_bytes()[:8192]
    encoding = "utf-8"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "gbk"

    added = 0
    skipped = 0
    with open(src, encoding=encoding, newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        header: Optional[list[str]] = None
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            if header is None:
                lower = [c.strip().lower() for c in row]
                if "word" in lower and ("translation" in lower or "trans" in lower):
                    header = lower
                    continue
                # no header -> assume word,translation
                header = ["word", "translation"]
            # pad short rows
            cells = list(row) + [""] * (len(header) - len(row))
            record = dict(zip(header, cells))
            word = (record.get("word") or "").strip()
            translation = (record.get("translation") or record.get("trans") or "").strip()
            if not word:
                skipped += 1
                continue
            engine.add_user_word(
                word=word,
                translation=translation,
                definition=(record.get("definition") or "").strip(),
                phonetic=(record.get("phonetic") or "").strip(),
                pos=(record.get("pos") or "").strip(),
                note=(record.get("note") or "").strip(),
            )
            added += 1
    print(f"Imported {added} user word(s); skipped {skipped}.")
    return 0


# --------------------------------------------------------------------------- #
# add / remove / list
# --------------------------------------------------------------------------- #
def cmd_add(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    engine.add_user_word(
        word=args.word,
        translation=args.translation,
        definition=args.definition or "",
        phonetic=args.phonetic or "",
        pos=args.pos or "",
        note=args.note or "",
    )
    print(f"Added: {args.word} -> {args.translation}")
    return 0


def cmd_remove(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    n = engine.remove_user_word(args.word)
    if n:
        print(f"Removed '{args.word}' ({n} row)")
        return 0
    print(f"'{args.word}' was not in the user dictionary")
    return 1


def cmd_list(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    words = engine.list_user_words()
    if args.pattern:
        pat = args.pattern.lower()
        words = [w for w in words if pat in w.word.lower()]
    if not words:
        print("(no user words)")
        return 0
    print(f"{'WORD':<24}  {'PHONETIC':<18}  TRANSLATION")
    print("-" * 70)
    for w in words:
        print(f"{w.word:<24}  {w.phonetic:<18}  {w.translation}")
    print(f"\n{len(words)} user word(s).")
    return 0


# --------------------------------------------------------------------------- #
# lookup / search
# --------------------------------------------------------------------------- #
def cmd_lookup(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    entry = engine.lookup(args.word)
    if not entry or entry.is_empty:
        print(f"'{args.word}': not found in local dictionary")
        return 1
    print(f"word         : {entry.word}")
    print(f"phonetic     : {entry.phonetic or '-'}")
    print(f"pos          : {entry.pos or '-'}")
    print(f"translation  : {entry.translation or '-'}")
    print(f"definition   : {entry.definition or '-'}")
    print(f"source       : {entry.source}")
    extras = []
    if entry.collins:
        extras.append(f"Collins {'*' * entry.collins}")
    if entry.oxford:
        extras.append("Oxford 3000")
    if entry.tag:
        extras.append(entry.tag)
    if entry.frq:
        extras.append(f"frq:{entry.frq}")
    print(f"extra        : {'  '.join(extras) or '-'}")
    return 0


def cmd_search(args: argparse.Namespace, engine: DictionaryEngine) -> int:
    results = engine.search(args.prefix, limit=args.limit)
    if not results:
        print(f"No matches for prefix '{args.prefix}'")
        return 1
    print(f"{'WORD':<24}  {'FRQ':>8}  TRANSLATION")
    print("-" * 70)
    for r in results:
        tr = (r.translation or "").split("\n")[0][:50]
        print(f"{r.word:<24}  {r.frq:>8}  {tr}")
    print(f"\n{len(results)} match(es).")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pot_translator.dict_manager",
        description="Manage the local dictionary & user words for Pot Translator.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("info", help="show dictionary stats")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("download-ecdict", help="download & install ECDICT")
    sp.add_argument("--force", action="store_true", help="re-download even if present")
    sp.set_defaults(func=cmd_download_ecdict)

    sp = sub.add_parser("import-csv", help="import user words from CSV")
    sp.add_argument("file", help="CSV file path")
    sp.set_defaults(func=cmd_import_csv)

    sp = sub.add_parser("import-sqlite", help="install a SQLite file as the dict")
    sp.add_argument("file", help="SQLite database path")
    sp.add_argument("--remap", action="store_true",
                    help="attempt to migrate tables that aren't named ec_dict")
    sp.set_defaults(func=cmd_import_sqlite)

    sp = sub.add_parser("add", help="add/override a user word")
    sp.add_argument("word")
    sp.add_argument("translation")
    sp.add_argument("--phonetic", default="")
    sp.add_argument("--definition", default="")
    sp.add_argument("--pos", default="")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="remove a user word")
    sp.add_argument("word")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("list", help="list user words")
    sp.add_argument("--pattern", default="", help="substring filter")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("lookup", help="lookup a word in the local dictionary")
    sp.add_argument("word")
    sp.set_defaults(func=cmd_lookup)

    sp = sub.add_parser("search", help="prefix-search the main dictionary")
    sp.add_argument("prefix")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    engine = DictionaryEngine()
    return args.func(args, engine)


if __name__ == "__main__":
    sys.exit(main())
