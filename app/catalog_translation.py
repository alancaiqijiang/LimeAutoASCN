"""Read-only catalog translation overlay with release fingerprint fencing.

The overlay is deliberately separate from the immutable catalog release. Runtime
lookups are fail-closed: an English translation is returned only when the
translation row is published, its source text matches, and its snapshot
fingerprint matches the currently configured catalog release.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

TRANSLATION_PATH_ENV = "LIMEAUTO_CATALOG_TRANSLATION_PATH"
RELEASE_PATH_ENV = "LIMEAUTO_CATALOG_RELEASE_PATH"

PUBLISHED_STATUS = "published"
SUPPORTED_LANG = "en"


class CatalogTranslationError(RuntimeError):
    """Raised when a translation overlay does not satisfy its read contract."""


def stable_term_id(term_kind: str, source_text: str) -> str:
    """Match the immutable manifest's stable ID algorithm exactly."""
    return f"{term_kind}:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()[:24]}"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _read_release_fingerprint(path: Path) -> str:
    if not path.is_file():
        raise CatalogTranslationError("configured release does not exist")
    uri = f"file:{path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT source_snapshot_fingerprint FROM catalog_releases"
            ).fetchone()
    except sqlite3.Error as exc:
        raise CatalogTranslationError("configured release metadata is unreadable") from exc
    if not row or not _text(row[0]):
        raise CatalogTranslationError("configured release fingerprint is missing")
    return _text(row[0])


class CatalogTranslationStore:
    """Read a translation overlay without ever opening it for writes."""

    def __init__(self, database_path: str | os.PathLike[str], *, expected_fingerprint: str):
        raw_path = _text(database_path)
        if not raw_path:
            raise CatalogTranslationError("translation path is required")
        self.database_path = Path(raw_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise CatalogTranslationError("translation overlay does not exist")
        self.expected_fingerprint = _text(expected_fingerprint)
        if not self.expected_fingerprint:
            raise CatalogTranslationError("expected release fingerprint is required")
        self._overlay_fingerprint = self._load_overlay_fingerprint()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.database_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _load_overlay_fingerprint(self) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value FROM translation_meta WHERE key = 'source_snapshot_fingerprint'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise CatalogTranslationError("translation overlay schema is unreadable") from exc
        return _text(row[0]) if row else ""

    @property
    def fingerprint_matches(self) -> bool:
        return bool(
            self._overlay_fingerprint
            and self.expected_fingerprint
            and self._overlay_fingerprint == self.expected_fingerprint
        )

    @lru_cache(maxsize=4096)
    def lookup(self, term_kind: str, source_text: str, lang: str = SUPPORTED_LANG) -> str | None:
        """Return a published translation or ``None`` for safe source fallback."""
        if lang != SUPPORTED_LANG or not self.fingerprint_matches:
            return None
        source = _text(source_text)
        kind = _text(term_kind)
        if not kind or not source:
            return None
        term_id = stable_term_id(kind, source)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT source_text, translated_text, status,
                           source_snapshot_fingerprint
                    FROM translation_terms
                    WHERE term_id = ? AND term_kind = ? AND lang = ?
                    """,
                    (term_id, kind, lang),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        if _text(row["source_text"]) != source:
            return None
        if _text(row["source_snapshot_fingerprint"]) != self.expected_fingerprint:
            return None
        if _text(row["status"]) != PUBLISHED_STATUS:
            return None
        translated = _text(row["translated_text"])
        return translated or None

    @lru_cache(maxsize=512)
    def resolve_sources(self, term_kind: str, query: str, limit: int) -> tuple[str, ...]:
        """Map an English search term back to the source strings it renders.

        The release tables only carry source text, so an English query has to be
        resolved to source strings before the release can be searched. Returns an
        empty tuple when the overlay is unusable, which callers must treat as
        "no English match" rather than "search everything".
        """
        if not self.fingerprint_matches:
            return ()
        kind = _text(term_kind)
        needle = _text(query)
        if not kind or not needle or limit < 1:
            return ()
        pattern = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT source_text
                    FROM translation_terms
                    WHERE term_kind = ? AND lang = ? AND status = ?
                      AND translated_text LIKE ? ESCAPE '\\'
                    ORDER BY
                      CASE
                        WHEN lower(translated_text) = lower(?) THEN 0
                        WHEN translated_text LIKE ? ESCAPE '\\' THEN 1
                        ELSE 2
                      END,
                      length(translated_text), source_text
                    LIMIT ?
                    """,
                    (
                        kind, SUPPORTED_LANG, PUBLISHED_STATUS,
                        f"%{pattern}%", needle, f"{pattern}%", limit,
                    ),
                ).fetchall()
        except sqlite3.Error:
            return ()
        return tuple(_text(row["source_text"]) for row in rows if _text(row["source_text"]))


@lru_cache(maxsize=4)
def _environment_store(
    translation_path: str,
    release_path: str,
    translation_mtime_ns: int,
    release_mtime_ns: int,
) -> CatalogTranslationStore | None:
    del translation_mtime_ns, release_mtime_ns
    try:
        fingerprint = _read_release_fingerprint(Path(release_path))
        return CatalogTranslationStore(translation_path, expected_fingerprint=fingerprint)
    except (CatalogTranslationError, OSError, ValueError):
        return None


def catalog_translation_store() -> CatalogTranslationStore | None:
    """Load the configured overlay, returning ``None`` on any safe-fallback error."""
    translation_path = _text(os.getenv(TRANSLATION_PATH_ENV))
    release_path = _text(os.getenv(RELEASE_PATH_ENV))
    if not translation_path or not release_path:
        return None
    try:
        translation_stat = Path(translation_path).expanduser().stat()
        release_stat = Path(release_path).expanduser().stat()
    except OSError:
        return None
    return _environment_store(
        str(Path(translation_path).expanduser().resolve()),
        str(Path(release_path).expanduser().resolve()),
        translation_stat.st_mtime_ns,
        release_stat.st_mtime_ns,
    )


def translated_display(
    term_kind: str,
    source_text: str,
    fallback: str,
    *,
    lang: str,
    store: CatalogTranslationStore | None = None,
) -> str:
    """Resolve a display label while preserving the source fallback contract."""
    if lang != SUPPORTED_LANG:
        return fallback
    active_store = store or catalog_translation_store()
    if active_store is None:
        return fallback
    return active_store.lookup(term_kind, source_text, lang) or fallback


TRANSLATION_STATUS_DISABLED = "disabled"
TRANSLATION_STATUS_UNAVAILABLE = "unavailable"
TRANSLATION_STATUS_MISMATCHED = "mismatched"
TRANSLATION_STATUS_MATCHED = "matched"


def catalog_translation_status() -> str:
    """One word describing whether the English overlay can serve the configured release.

    ``mismatched`` is the state that matters operationally: the glossary is healthy but was
    built for a different release snapshot, so every English label silently falls back to
    Chinese. It is reachable whenever a release is updated without rebuilding the glossary,
    and it must be observable rather than discovered by a user.
    """
    translation_path = _text(os.getenv(TRANSLATION_PATH_ENV))
    release_path = _text(os.getenv(RELEASE_PATH_ENV))
    if not translation_path or not release_path:
        return TRANSLATION_STATUS_DISABLED
    store = catalog_translation_store()
    if store is None:
        return TRANSLATION_STATUS_UNAVAILABLE
    if not store.fingerprint_matches:
        return TRANSLATION_STATUS_MISMATCHED
    return TRANSLATION_STATUS_MATCHED
