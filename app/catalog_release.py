"""Read-only query facade for an immutable SQLite catalog release.

This module is the runtime read facade for one immutable catalog release.
It never opens an external source or any application/sample database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from .i18n import current_lang, tr
from .catalog_translation import CatalogTranslationStore, translated_display


RELEASE_DEFAULT_PARTS_LIMIT = 50
RELEASE_MAX_PARTS_LIMIT = 250
RELEASE_MAX_PARTS_OFFSET = 1_000_000
# Rows shown per level in the name-search navigation groups. Measured cost is ~200-300ms
# for the node level regardless of how many names match, because only the small tables are
# read; the cap is about page length, not query cost.
RELEASE_ENTITY_MATCH_LIMIT = 30
# The translation glossary is keyed on `_text(description)`, i.e. Python's str.strip(),
# while the release stores the raw column. SQLite's default TRIM() removes only U+0020, so
# a trailing tab or NBSP made the English search miss a row whose English name displayed
# fine. Measured on the staging release: 34 of 148,077 part rows carry edge whitespace,
# the characters being TAB, SPACE and NBSP (series/model/node names carry none). The full
# Unicode space set is trimmed so a later release cannot quietly reintroduce the miss.
NAME_TRIM_CHARS = (
    " \t\n\r\x0b\x0c\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
PUBLISHABLE_ASSET_STATUSES = frozenset({"ready", "done", "available"})
DISPLAY_ASSET_TYPES = frozenset({"material_image", "epc_drawing", "thumbnail"})

MISSING_SERIES_NAME = "车系名称待补充"
MISSING_MODEL_NAME = "车型名称待补充"
MISSING_NODE_NAME = "系统名称待补充"
MISSING_PART_NAME = "物料名称待补充"
DISPLAY_NAME_PLACEHOLDERS = frozenset(
    {
        "unknown",
        "null",
        "none",
        "n/a",
        "na",
        "nil",
        "test",
        "default",
        "unnamed",
        "no name",
        "暂无",
        "未知",
        "未命名",
        "无名称",
    }
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_placeholder(value: Any) -> bool:
    return _text(value).casefold() in DISPLAY_NAME_PLACEHOLDERS


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _material_code_keys(value: str) -> list[str]:
    """Return stored-key candidates for an operator-typed part number.

    ASCII case variants stay equality matches so SQLite can use
    fitments_material_idx. COLLATE NOCASE on the column cannot.
    """
    code = _text(value)
    if not code:
        return []
    return list(dict.fromkeys([code, code.upper(), code.lower()]))


def _translated_catalog_label(
    term_kind: str,
    source_text: str,
    fallback: str,
    *,
    translation_store: CatalogTranslationStore | None = None,
) -> str:
    """Use the English overlay only after the language and release gates pass."""
    return translated_display(
        term_kind,
        source_text,
        fallback,
        lang=current_lang(),
        store=translation_store,
    )


def catalog_series_display_name(
    value: Any, *, translation_store: CatalogTranslationStore | None = None
) -> str:
    """Return a primary series label without replacing short real names."""
    display = _text(value)
    if not display or _is_placeholder(display):
        return tr("missing_series_name")
    return _translated_catalog_label("series", display, display, translation_store=translation_store)


def catalog_model_display_name(
    value: Any,
    model_code: Any,
    *,
    translation_store: CatalogTranslationStore | None = None,
) -> str:
    """Return a model label; an exact code-only source value is not a name."""
    display = _text(value)
    code = _text(model_code)
    if not display or _is_placeholder(display) or (code and display.casefold() == code.casefold()):
        return tr("missing_model_name")
    return _translated_catalog_label("model", display, display, translation_store=translation_store)


def catalog_node_display_name(
    node: dict[str, Any], *, translation_store: CatalogTranslationStore | None = None
) -> str:
    """Choose a human node label and never fall back to source identifiers."""
    identifiers = {
        candidate.casefold()
        for candidate in (_text(node.get("source_obj_code")), _text(node.get("source_tree_key")))
        if candidate
    }
    source_text = _text(node.get("name_source"))
    for field in ("display_name", "name_source"):
        display = _text(node.get(field))
        if display and not _is_placeholder(display) and display.casefold() not in identifiers:
            if source_text and source_text.casefold() not in identifiers:
                return _translated_catalog_label(
                    "node", source_text, display, translation_store=translation_store
                )
            return display
    return tr("missing_node_name")


def catalog_part_display_name(
    part: dict[str, Any], *, translation_store: CatalogTranslationStore | None = None
) -> str:
    """Choose a human part label and keep the material code as an identifier."""
    material_code = _text(part.get("material_code")).casefold()
    source_text = _text(part.get("description"))
    for field in ("description", "display_name_source"):
        display = _text(part.get(field))
        if display and not _is_placeholder(display) and display.casefold() != material_code:
            if source_text and source_text.casefold() != material_code:
                return _translated_catalog_label(
                    "part", source_text, display, translation_store=translation_store
                )
            return display
    return tr("missing_part_name")


def normalize_parts_pagination(limit: Any, offset: Any) -> tuple[int, int]:
    """Return bounded pagination values before they reach SQLite.

    Invalid and negative values are rejected by the read contract. Very large
    non-negative values are capped so a caller cannot bind an integer outside
    SQLite's INTEGER range (or ask for an unreasonably large page).
    """

    try:
        requested_limit = int(limit)
        requested_offset = int(offset)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    if requested_limit < 1:
        raise ValueError("limit must be at least 1")
    if requested_offset < 0:
        raise ValueError("offset must not be negative")
    return (
        min(requested_limit, RELEASE_MAX_PARTS_LIMIT),
        min(requested_offset, RELEASE_MAX_PARTS_OFFSET),
    )


class CatalogReleaseError(RuntimeError):
    """Raised when a release artifact cannot satisfy the read contract."""


class CatalogReleaseStore:
    """Read one release artifact without exposing source paths or global parts."""

    def __init__(
        self,
        database_path: str | os.PathLike[str] | None = None,
        *,
        release_no: str | None = None,
        allow_draft: bool = False,
    ) -> None:
        raw_path = database_path or os.getenv("LIMEAUTO_CATALOG_RELEASE_PATH", "")
        if not raw_path:
            raise CatalogReleaseError("LIMEAUTO_CATALOG_RELEASE_PATH is required")
        self.database_path = Path(raw_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise CatalogReleaseError(f"catalog release does not exist: {self.database_path}")
        self.release_no = release_no
        self.allow_draft = allow_draft
        self._metadata = self._load_metadata()
        if self.release_no is not None and self._metadata["release_no"] != self.release_no:
            raise CatalogReleaseError(
                f"release number mismatch: expected {self.release_no}, "
                f"got {self._metadata['release_no']}"
            )
        if not allow_draft and self._metadata["status"] not in {"validated", "published"}:
            raise CatalogReleaseError(
                f"release {self._metadata['release_no']} is not validated/published"
            )

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.database_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _load_metadata(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM catalog_releases").fetchall()
        if len(rows) != 1:
            raise CatalogReleaseError(f"expected one release row, got {len(rows)}")
        return dict(rows[0])

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def release_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def catalog_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "release_models",
                    "system_nodes",
                    "catalog_parts",
                    "fitments",
                    "catalog_assets",
                )
            }

    def list_series(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_code,
                       MIN(series_name_source) AS series_name,
                       COUNT(*) AS model_count
                FROM release_models
                WHERE release_id = ?
                GROUP BY series_code
                ORDER BY series_name, series_code
                """,
                (self._metadata["release_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_named_entities(
        self,
        *,
        name: str,
        english_series: Sequence[str] = (),
        english_models: Sequence[str] = (),
        english_nodes: Sequence[str] = (),
        series_code: str = "",
        model_code: str = "",
        limit: int = RELEASE_ENTITY_MATCH_LIMIT,
    ) -> dict[str, Any]:
        """Match a typed name against series / model / system-node names.

        Names live on the small tables (4,046 models, 361,593 nodes), so this never touches
        `fitments`: expanding a node name to its parts costs 14-19s because the planner
        abandons `fitments_node_idx` for a large key set (measured), and a group node such
        as 底盘组 owns no parts at all. Returning the entities to navigate to is both
        correct and cheap.

        English works by resolution, not by translation: the caller resolves an English
        query back to the source strings it renders and passes them here, so the match
        itself stays an exact `IN (...)` on source text — the same shape the Chinese `LIKE`
        path uses. That keeps this facade release-only and unknown to translations.

        Node matches are returned as individual occurrences with their own series/model
        context, and `series_code`/`model_code` restrict them the way they restrict the
        parts table. A named group is therefore never a link to a vehicle the operator did
        not ask for.
        """
        query = _text(name)
        if not query:
            return {"series": [], "models": [], "nodes": [], "limit": limit, "truncated": {}}

        # The form's series/model selects already narrow the parts table, so they have to
        # narrow the navigation groups too: otherwise picking a vehicle and then clicking a
        # matched system lands the operator on a different vehicle.
        node_scope = ""
        node_scope_params: list[str] = []
        if _text(series_code):
            node_scope += " AND n.series_code = ?"
            node_scope_params.append(_text(series_code))
        if _text(model_code):
            node_scope += " AND n.model_code = ?"
            node_scope_params.append(_text(model_code))

        pattern = _like_pattern(query)
        release_id = self._metadata["release_id"]

        def match_clause(column: str, resolved: Sequence[str]) -> tuple[str, list[Any]]:
            sources = [_text(item) for item in resolved if _text(item)]
            clause = f"({column} LIKE ? ESCAPE '\\' COLLATE NOCASE"
            params: list[Any] = [pattern]
            if sources:
                clause += f" OR {column} IN ({', '.join('?' * len(sources))})"
                params.extend(sources)
            return clause + ")", params

        series_clause, series_params = match_clause("series_name_source", english_series)
        model_clause, model_params = match_clause("model_name_source", english_models)
        node_clause, node_params = match_clause("name_source", english_nodes)

        with self._connect() as connection:
            series_rows = connection.execute(
                f"""
                SELECT series_code,
                       MIN(series_name_source) AS series_name_source,
                       COUNT(*) AS model_count
                FROM release_models
                WHERE release_id = ? AND {series_clause}
                GROUP BY series_code
                ORDER BY series_name_source, series_code
                LIMIT ?
                """,
                [release_id, *series_params, limit + 1],
            ).fetchall()

            model_rows = connection.execute(
                f"""
                SELECT series_code, model_code, series_name_source, model_name_source
                FROM release_models
                WHERE release_id = ? AND {model_clause}
                ORDER BY model_name_source, series_name_source, model_code
                LIMIT ?
                """,
                [release_id, *model_params, limit + 1],
            ).fetchall()

            # One row per real (series, model, node) occurrence, carrying its own vehicle
            # context. The previous form collapsed each name onto MIN(node_key), which
            # silently picked an arbitrary vehicle: with a series/model selected the
            # operator was sent to a different one, and without a selection a name with
            # 3,458 occurrences offered exactly one, unlabelled, destination.
            node_rows = connection.execute(
                f"""
                SELECT n.name_source, n.series_code, n.model_code, n.node_key, n.child_count,
                       rm.series_name_source, rm.model_name_source
                FROM system_nodes n
                LEFT JOIN release_models rm
                  ON rm.release_id = n.release_id
                 AND rm.series_code = n.series_code
                 AND rm.model_code = n.model_code
                WHERE n.release_id = ? AND {node_clause}{node_scope}
                ORDER BY n.name_source, n.series_code, n.model_code, n.node_key
                LIMIT ?
                """,
                [release_id, *node_params, *node_scope_params, limit + 1],
            ).fetchall()

        def bounded(rows: list[Any]) -> tuple[list[dict[str, Any]], bool]:
            truncated = len(rows) > limit
            return [dict(row) for row in rows[:limit]], truncated

        series_items, series_more = bounded(series_rows)
        model_items, model_more = bounded(model_rows)
        node_items, node_more = bounded(node_rows)
        return {
            "series": series_items,
            "models": model_items,
            "nodes": node_items,
            "limit": limit,
            "truncated": {
                "series": series_more,
                "models": model_more,
                "nodes": node_more,
            },
        }

    def list_models(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_code, model_code, series_name_source,
                       model_name_source, publish_status
                FROM release_models
                WHERE release_id = ?
                ORDER BY series_name_source, model_name_source, model_code
                """,
                (self._metadata["release_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def models_for_series(self, series_code: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT series_code, model_code, series_name_source,
                       model_name_source, publish_status
                FROM release_models
                WHERE release_id = ? AND series_code = ?
                ORDER BY model_name_source, model_code
                """,
                (self._metadata["release_id"], series_code),
            ).fetchall()
        return [dict(row) for row in rows]

    def model_by_codes(self, series_code: str, model_code: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT series_code, model_code, series_name_source,
                       model_name_source, publish_status
                FROM release_models
                WHERE release_id = ? AND series_code = ? AND model_code = ?
                """,
                (self._metadata["release_id"], series_code, model_code),
            ).fetchone()
        return self._dict(row)

    def node_by_key(
        self, series_code: str, model_code: str, node_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM system_nodes
                WHERE release_id = ? AND series_code = ?
                  AND model_code = ? AND node_key = ?
                """,
                (self._metadata["release_id"], series_code, model_code, node_key),
            ).fetchone()
        return self._dict(row)

    def root_nodes_for_model(self, series_code: str, model_code: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM system_nodes
                WHERE release_id = ? AND series_code = ?
                  AND model_code = ? AND parent_key IS NULL
                ORDER BY depth, path_key, node_key
                """,
                (self._metadata["release_id"], series_code, model_code),
            ).fetchall()
        return [dict(row) for row in rows]

    def model_nodes(self, series_code: str, model_code: str) -> list[dict[str, Any]]:
        """Return every node of one model in tree order (bounded per model)."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM system_nodes
                WHERE release_id = ? AND series_code = ? AND model_code = ?
                ORDER BY depth, path_key, node_key
                """,
                (self._metadata["release_id"], series_code, model_code),
            ).fetchall()
        return [dict(row) for row in rows]

    def children_for_node(
        self, series_code: str, model_code: str, node_key: str
    ) -> list[dict[str, Any]]:
        node = self.node_by_key(series_code, model_code, node_key)
        if node is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM system_nodes
                WHERE release_id = ? AND series_code = ?
                  AND model_code = ? AND parent_key = ?
                ORDER BY depth, path_key, node_key
                """,
                (self._metadata["release_id"], series_code, model_code, node["path_key"]),
            ).fetchall()
        return [dict(row) for row in rows]

    def parent_node(
        self, series_code: str, model_code: str, node_key: str
    ) -> dict[str, Any] | None:
        node = self.node_by_key(series_code, model_code, node_key)
        parent_path = _text(node.get("parent_key") if node else None)
        if not node or not parent_path:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM system_nodes
                WHERE release_id = ? AND series_code = ?
                  AND model_code = ? AND path_key = ?
                ORDER BY depth, node_key
                LIMIT 1
                """,
                (self._metadata["release_id"], series_code, model_code, parent_path),
            ).fetchone()
        return self._dict(row)

    def ancestor_nodes(
        self, series_code: str, model_code: str, node_key: str
    ) -> list[dict[str, Any]]:
        ancestors: list[dict[str, Any]] = []
        current_key = node_key
        seen: set[str] = set()
        while current_key and current_key not in seen:
            seen.add(current_key)
            parent = self.parent_node(series_code, model_code, current_key)
            if parent is None:
                break
            ancestors.append(parent)
            current_key = str(parent["node_key"])
        ancestors.reverse()
        return ancestors

    def search_parts(
        self,
        *,
        material_code: str = "",
        material_name: str = "",
        series_code: str = "",
        model_code: str = "",
        english_sources: Sequence[str] = (),
        limit: int = RELEASE_DEFAULT_PARTS_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit, safe_offset = normalize_parts_pagination(limit, offset)
        code = _text(material_code)
        name = _text(material_name)
        series = _text(series_code)
        model = _text(model_code)
        if not code and len(name) < 2:
            raise ValueError("A part number or name is required")
        release_id = self._metadata["release_id"]
        resolved = [source for source in (_text(item) for item in english_sources) if source]
        filters = ["f.release_id = ?"]
        params: list[Any] = [release_id]
        if code:
            keys = _material_code_keys(code)
            filters.append("f.material_code IN (" + ", ".join("?" * len(keys)) + ")")
            params.extend(keys)
        if name:
            # Match names on catalog_parts first (148k rows), then use
            # fitments_material_idx. Scanning 5M fitments with LIKE is too slow.
            # `english_sources` are the source strings a non-CJK query renders as;
            # they are resolved by the caller so this façade stays release-only.
            pattern = _like_pattern(name)
            if resolved:
                placeholders = ", ".join("?" * len(resolved))
                filters.append(
                    f"""f.material_code IN (
                        SELECT p2.material_code
                        FROM catalog_parts p2
                        WHERE p2.release_id = ?
                          AND (
                            TRIM(p2.description, ?) IN ({placeholders})
                            OR p2.description LIKE ? ESCAPE '\\' COLLATE NOCASE
                            OR p2.display_name_source LIKE ? ESCAPE '\\' COLLATE NOCASE
                          )
                    )"""
                )
                params.extend([release_id, NAME_TRIM_CHARS, *resolved, pattern, pattern])
            else:
                filters.append(
                    """f.material_code IN (
                        SELECT p2.material_code
                        FROM catalog_parts p2
                        WHERE p2.release_id = ?
                          AND (
                            p2.description LIKE ? ESCAPE '\\' COLLATE NOCASE
                            OR p2.display_name_source LIKE ? ESCAPE '\\' COLLATE NOCASE
                          )
                    )"""
                )
                params.extend([release_id, pattern, pattern])
        if series:
            filters.append("f.series_code = ?")
            params.append(series)
        if model:
            filters.append("f.model_code = ?")
            params.append(model)
        where = " AND ".join(filters)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM fitments f
                    JOIN catalog_parts p
                      ON p.release_id = f.release_id
                     AND p.material_code = f.material_code
                    WHERE {where}
                    """,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT f.series_code, f.model_code, f.node_key, f.material_code,
                       f.quantity, f.quantity_raw, f.manual_code, f.callout,
                       p.display_name_source, p.description,
                       n.display_name, n.name_source, n.source_obj_code,
                       rm.series_name_source, rm.model_name_source
                FROM fitments f
                JOIN catalog_parts p
                  ON p.release_id = f.release_id
                 AND p.material_code = f.material_code
                JOIN system_nodes n
                  ON n.release_id = f.release_id
                 AND n.node_key = f.node_key
                JOIN release_models rm
                  ON rm.release_id = f.release_id
                 AND rm.series_code = f.series_code
                 AND rm.model_code = f.model_code
                WHERE {where}
                ORDER BY f.material_code, rm.series_name_source, f.series_code,
                         rm.model_name_source, f.model_code, n.path_key
                LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "has_next": safe_offset + len(rows) < total,
        }

    def assets_for_node(
        self, series_code: str, model_code: str, node_key: str
    ) -> list[dict[str, Any]]:
        """Return public-safe EPC asset metadata bound to one release node."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.asset_key, a.asset_type
                FROM catalog_assets a
                JOIN system_nodes n
                  ON n.release_id = a.release_id
                 AND n.node_key = a.system_node_key
                WHERE a.release_id = ?
                  AND a.system_node_key = ?
                  AND n.series_code = ?
                  AND n.model_code = ?
                  AND n.node_key = ?
                  AND a.asset_type IN ('epc_drawing', 'thumbnail')
                  AND a.status IN ('ready', 'done', 'available')
                ORDER BY CASE a.asset_type
                             WHEN 'epc_drawing' THEN 0
                             ELSE 1
                         END,
                         a.asset_key
                """,
                (
                    self._metadata["release_id"],
                    node_key,
                    series_code,
                    model_code,
                    node_key,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def parts_for_node(
        self,
        series_code: str,
        model_code: str,
        node_key: str,
        *,
        limit: int = RELEASE_DEFAULT_PARTS_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit, safe_offset = normalize_parts_pagination(limit, offset)
        node = self.node_by_key(series_code, model_code, node_key)
        if node is None:
            return {
                "items": [],
                "total": 0,
                "limit": safe_limit,
                "offset": safe_offset,
                "has_next": False,
            }
        release_id = self._metadata["release_id"]
        with self._connect() as connection:
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM fitments
                    WHERE release_id = ? AND series_code = ?
                      AND model_code = ? AND node_key = ?
                    """,
                    (release_id, series_code, model_code, node_key),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT f.source_occurrence_key, f.callout, f.quantity,
                       f.quantity_raw, f.manual_code, f.fitment_note,
                       f.material_code, p.display_name_source, p.description,
                       p.source_detail_status,
                       a.asset_key AS material_asset_key,
                       a.status AS material_asset_status,
                       a.asset_type AS material_asset_type,
                       a.object_key AS material_object_key
                FROM fitments f
                JOIN catalog_parts p
                  ON p.release_id = f.release_id
                 AND p.material_code = f.material_code
                LEFT JOIN (
                    SELECT a.release_id, a.material_code, a.asset_key,
                           a.asset_type, a.object_key, a.status
                    FROM catalog_assets a
                    WHERE a.release_id = ?
                      AND a.asset_type IN ('material_image', 'epc_drawing', 'thumbnail')
                      AND a.status IN ('ready', 'done', 'available')
                      AND a.asset_key = (
                          SELECT first_asset.asset_key
                          FROM catalog_assets first_asset
                          WHERE first_asset.release_id = a.release_id
                            AND first_asset.material_code = a.material_code
                            AND first_asset.asset_type IN ('material_image', 'epc_drawing', 'thumbnail')
                            AND first_asset.status IN ('ready', 'done', 'available')
                          ORDER BY CASE first_asset.asset_type
                                       WHEN 'material_image' THEN 0
                                       WHEN 'thumbnail' THEN 1
                                       ELSE 2
                                   END,
                                   first_asset.asset_key
                          LIMIT 1
                      )
                ) a
                  ON a.release_id = f.release_id
                 AND a.material_code = f.material_code
                WHERE f.release_id = ? AND f.series_code = ?
                  AND f.model_code = ? AND f.node_key = ?
                ORDER BY f.callout, f.material_code, f.source_occurrence_key
                LIMIT ? OFFSET ?
                """,
                (
                    release_id,
                    release_id,
                    series_code,
                    model_code,
                    node_key,
                    safe_limit,
                    safe_offset,
                ),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "has_next": safe_offset + len(rows) < total,
        }

    def part_context(
        self,
        series_code: str,
        model_code: str,
        node_key: str,
        material_code: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT f.source_occurrence_key, f.series_code, f.model_code,
                       f.node_key, f.material_code, f.callout, f.quantity,
                       f.quantity_raw, f.manual_code, f.fitment_note,
                       p.display_name_source, p.description,
                       p.source_detail_status, n.path_key, n.display_name
                FROM fitments f
                JOIN catalog_parts p
                  ON p.release_id = f.release_id
                 AND p.material_code = f.material_code
                JOIN system_nodes n
                  ON n.release_id = f.release_id
                 AND n.node_key = f.node_key
                WHERE f.release_id = ? AND f.series_code = ?
                  AND f.model_code = ? AND f.node_key = ?
                  AND f.material_code = ?
                ORDER BY f.callout, f.source_occurrence_key
                LIMIT 1
                """,
                (
                    self._metadata["release_id"],
                    series_code,
                    model_code,
                    node_key,
                    material_code,
                ),
            ).fetchone()
        result = self._dict(row)
        if result is not None:
            asset = self.asset_for_part(material_code)
            if asset is not None:
                result["material_asset_key"] = asset["asset_key"]
                result["material_asset_status"] = asset["status"]
                result["material_asset_type"] = asset["asset_type"]
        return result

    def asset_by_key(self, asset_key: str) -> dict[str, Any] | None:
        """Fetch one asset from this release without touching its source store."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT asset_key, material_code, system_node_key, asset_type,
                       object_key, source_sha256, size_bytes, mime_type, status
                FROM catalog_assets
                WHERE release_id = ? AND asset_key = ?
                LIMIT 1
                """,
                (self._metadata["release_id"], asset_key),
            ).fetchone()
        return self._dict(row)

    def asset_for_part(self, material_code: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT asset_key, object_key, asset_type, source_sha256,
                       size_bytes, mime_type, status
                FROM catalog_assets
                WHERE release_id = ? AND material_code = ?
                  AND asset_type IN ('material_image', 'epc_drawing', 'thumbnail')
                  AND status IN ('ready', 'done', 'available')
                ORDER BY CASE asset_type
                             WHEN 'material_image' THEN 0
                             WHEN 'thumbnail' THEN 1
                             ELSE 2
                         END,
                         asset_key
                LIMIT 1
                """,
                (self._metadata["release_id"], material_code),
            ).fetchone()
        return self._dict(row)
