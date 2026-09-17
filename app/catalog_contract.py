"""Pure-Python data contract for internal catalog EPC node paths.

This module intentionally has no database or HTTP dependencies.  It accepts
already-queried ``epc_nodes`` rows and turns their ``node_path`` strings into a
deterministic hierarchy suitable for JSON reporting or later catalog indexes.

Assumptions
-----------
* ``node_path`` uses ``>`` as the path separator.  Whitespace around each
  segment is not significant and is removed.
* ``series_code`` and ``model_code`` together form one model namespace.
* ``obj_code`` identifies the source row in ``epc_nodes``.  Every unique
  ``(series_code, model_code, normalized path, obj_code)`` identity is kept as
  its own source node.  A derived prefix is represented with ``obj_code``
  equal to ``None``; the contract never fabricates a source ``obj_code``.
* ``node_key`` uses separate ``source`` and ``derived`` namespaces.  Source
  keys include the model scope, normalized path key, and ``obj_code``;
  derived keys include the model scope and normalized path key.  Each
  component is percent-encoded so delimiters in source data cannot collide.
* A path conflict exists when the same model and normalized path are produced
  by more than one source ``obj_code``.  It is a non-blocking structural
  warning because EPC variants can legitimately share a display path.
* ``child_count`` and ``descendant_part_count`` are path-level aggregates and
  are therefore shared by source variants at one path.  ``child_count`` is
  the number of distinct direct child paths.  ``descendant_part_count`` is the
  sum of retained source variants' direct part counts in strict path
  descendants; node paths do not prove which variant owns a child.
  ``path_variant_count`` records how many source identities share a path.
  A source node with no direct parts but with descendants is therefore
  retained and has a nonzero descendant count.
* Quantity parsing only converts integer-valued strings.  Decimal, fractional,
  ranged, unit, and free-text values are kept verbatim as ``quantity_raw``
  rather than guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

_INTEGER_PATTERN = re.compile(r"[+-]?\d+")
_PATH_SEPARATOR = ">"


class NodePathContractError(ValueError):
    """Base structured error raised by the node-path contract."""

    kind = "node_path_contract"

    def __init__(
        self,
        message: str,
        *,
        kind: str | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind or self.kind
        self.fields: dict[str, Any] = fields

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this error."""
        result: dict[str, Any] = {
            "kind": self.kind,
            "message": self.message,
        }
        result.update(self.fields)
        return result


class EmptyNodePathError(NodePathContractError):
    """Raised when a node path has no non-empty segments."""

    kind = "empty_node_path"

    def __init__(self, raw_path: str) -> None:
        super().__init__(
            "node_path is empty or contains only separators",
            node_path=raw_path,
        )


class PathConflictError(NodePathContractError):
    """Structured, non-blocking record for one model/path conflict."""

    kind = "path_conflict"

    def __init__(
        self,
        *,
        series_code: str,
        model_code: str,
        path_key: str,
        node_path: str,
        obj_codes: Sequence[str],
    ) -> None:
        super().__init__(
            "node path maps to more than one source obj_code",
            series_code=series_code,
            model_code=model_code,
            path_key=path_key,
            node_path=node_path,
            obj_codes=sorted(obj_codes),
        )


@dataclass(frozen=True)
class _SourceKey:
    series_code: str
    model_code: str
    path_key: str
    obj_code: str


def parse_node_path(node_path: str | None) -> tuple[str, ...]:
    """Split and trim a ``node_path`` into its non-empty segments.

    Raises :class:`EmptyNodePathError` when no segment remains.
    """
    raw = "" if node_path is None else str(node_path)
    parts = tuple(piece.strip() for piece in raw.split(_PATH_SEPARATOR) if piece.strip())
    if not parts:
        raise EmptyNodePathError(raw)
    return parts


def make_path_key(parts: Sequence[str]) -> str:
    """Build a stable, collision-resistant key for a sequence of segments.

    Every segment is percent-encoded with an empty safe set, including ``%``
    and ``>``.  The encoded segments are then joined with ``>``.  This avoids
    ambiguity between a literal ``>``/``%`` inside a segment and the path
    separator/key encoding.
    """
    return _PATH_SEPARATOR.join(quote(str(part), safe="") for part in parts)


def _make_node_key(
    namespace: str,
    *,
    series_code: str,
    model_code: str,
    path_key: str,
    obj_code: str | None = None,
) -> str:
    """Build a collision-safe key for one source or derived output node."""
    components = [series_code, model_code, path_key]
    if namespace == "source":
        if obj_code is None:
            raise ValueError("source node keys require obj_code")
        components.append(obj_code)
    encoded = ":".join(quote(str(component), safe="") for component in components)
    return f"{namespace}:{encoded}"


def parse_quantity(value: Any) -> dict[str, Any]:
    """Parse an integer quantity without guessing non-integer values.

    Returns ``{"quantity": int | None, "quantity_raw": str | None}``.  Only a
    canonical integer string is converted to ``quantity``; everything else is
    preserved as ``quantity_raw``.
    """
    if value is None:
        return {"quantity": None, "quantity_raw": None}
    raw = str(value).strip()
    if not raw:
        return {"quantity": None, "quantity_raw": None}
    parsed: int | None = None
    if _INTEGER_PATTERN.fullmatch(raw):
        try:
            parsed = int(raw)
        except ValueError:
            parsed = None
    return {"quantity": parsed, "quantity_raw": raw}


def _string_field(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _int_field(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _sorted_errors(
    errors: list[NodePathContractError],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return sorted((error.to_dict() for error in errors), key=lambda item: _sortable(item)), sorted(
        warnings, key=lambda item: _sortable(item)
    )


def _sortable(item: Mapping[str, Any]) -> tuple[str, ...]:
    return (json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str),)


def build_catalog_report(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a deterministic node-hierarchy report from source rows.

    ``rows`` are mappings that should contain at least ``series_code``,
    ``model_code``, ``obj_code``, and ``node_path``.  ``direct_part_count`` and
    ``tree_key`` are optional but recommended.
    """
    warnings: list[dict[str, Any]] = []
    blocking_errors: list[NodePathContractError] = []
    source_observations: list[dict[str, Any]] = []

    for row in rows:
        data = dict(row)
        series_code = _string_field(data, "series_code")
        model_code = _string_field(data, "model_code")
        obj_code = _string_field(data, "obj_code")
        raw_path = data.get("node_path")
        raw_path_text = "" if raw_path is None else str(raw_path)

        if not series_code or not model_code:
            blocking_errors.append(
                NodePathContractError(
                    "source row is missing series_code or model_code",
                    kind="missing_model_scope",
                    series_code=series_code,
                    model_code=model_code,
                    node_path=raw_path_text,
                )
            )
            continue
        if not obj_code:
            blocking_errors.append(
                NodePathContractError(
                    "source row is missing obj_code",
                    kind="missing_obj_code",
                    series_code=series_code,
                    model_code=model_code,
                    node_path=raw_path_text,
                )
            )
            continue

        try:
            parts = parse_node_path(raw_path)
        except EmptyNodePathError as exc:
            blocking_errors.append(exc)
            continue

        direct_text = data.get("direct_part_count")
        if direct_text is not None:
            try:
                if isinstance(direct_text, str):
                    direct_text = direct_text.strip()
                int(direct_text)
            except (TypeError, ValueError):
                warnings.append(
                    {
                        "kind": "invalid_direct_part_count",
                        "message": "direct_part_count could not be interpreted as an integer; using zero",
                        "series_code": series_code,
                        "model_code": model_code,
                        "obj_code": obj_code,
                        "node_path": raw_path_text,
                        "value": direct_text,
                    }
                )

        source_observations.append(
            {
                "series_code": series_code,
                "model_code": model_code,
                "obj_code": obj_code,
                "node_path": _PATH_SEPARATOR.join(parts),
                "raw_node_path": raw_path_text,
                "path_parts": parts,
                "path_key": make_path_key(parts),
                "parent_key": make_path_key(parts[:-1]) if len(parts) > 1 else None,
                "depth": len(parts),
                "display_name": parts[-1],
                "tree_key": _json_text(data.get("tree_key")),
                "direct_part_count": _int_field(data, "direct_part_count"),
            }
        )

    source_observations.sort(
        key=lambda item: (
            item["series_code"],
            item["model_code"],
            item["path_parts"],
            item["obj_code"],
            item["direct_part_count"],
            item["tree_key"] or "",
            item["raw_node_path"],
        )
    )

    source_nodes: dict[_SourceKey, dict[str, Any]] = {}
    path_obj_codes: dict[tuple[str, str, str], set[str]] = {}

    for item in source_observations:
        key = _SourceKey(
            item["series_code"],
            item["model_code"],
            item["path_key"],
            item["obj_code"],
        )
        conflict_key = (item["series_code"], item["model_code"], item["path_key"])
        path_obj_codes.setdefault(conflict_key, set()).add(item["obj_code"])

        existing = source_nodes.get(key)
        if existing is None:
            source_nodes[key] = item
        else:
            existing["direct_part_count"] = max(existing["direct_part_count"], item["direct_part_count"])
            warnings.append(
                {
                    "kind": "duplicate_source_node",
                    "message": "duplicate source node observation collapsed with maximum direct_part_count",
                    "series_code": item["series_code"],
                    "model_code": item["model_code"],
                    "path_key": item["path_key"],
                    "node_path": item["node_path"],
                    "obj_code": item["obj_code"],
                }
            )

    structural_conflicts: list[dict[str, Any]] = []
    for (series_code, model_code, path_key), obj_codes in sorted(path_obj_codes.items()):
        if len(obj_codes) <= 1:
            continue
        source_item = next(
            item
            for item in source_nodes.values()
            if item["series_code"] == series_code
            and item["model_code"] == model_code
            and item["path_key"] == path_key
        )
        conflict = PathConflictError(
            series_code=series_code,
            model_code=model_code,
            path_key=path_key,
            node_path=source_item["node_path"],
            obj_codes=obj_codes,
        )
        structural_conflicts.append(conflict.to_dict())

    # One path may have several retained source variants.  Keep a separate
    # path index for hierarchy aggregates; it must never be used as the node
    # identity because it would collapse those variants.
    source_path_ids = {
        (item["series_code"], item["model_code"], item["path_key"])
        for item in source_nodes.values()
    }
    path_variant_counts: dict[tuple[str, str, str], int] = {}
    for item in source_nodes.values():
        path_id = (item["series_code"], item["model_code"], item["path_key"])
        path_variant_counts[path_id] = path_variant_counts.get(path_id, 0) + 1

    model_source_keys: dict[tuple[str, str], set[str]] = {}
    for key, item in source_nodes.items():
        model_key = (key.series_code, key.model_code)
        model_source_keys.setdefault(model_key, set()).add(key.path_key)

    nodes_by_path: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for key, item in source_nodes.items():
        model_key = (key.series_code, key.model_code)
        path_id = (key.series_code, key.model_code, key.path_key)
        node = {
            "series_code": key.series_code,
            "model_code": key.model_code,
            "obj_code": key.obj_code,
            "node_key": _make_node_key(
                "source",
                series_code=key.series_code,
                model_code=key.model_code,
                path_key=key.path_key,
                obj_code=key.obj_code,
            ),
            "is_derived": False,
            "tree_key": item["tree_key"],
            "node_path": item["node_path"],
            "path_key": key.path_key,
            "parent_key": item["parent_key"],
            "depth": item["depth"],
            "display_name": item["display_name"],
            "child_count": 0,
            "direct_part_count": item["direct_part_count"],
            "descendant_part_count": 0,
            "path_variant_count": path_variant_counts[path_id],
        }
        nodes_by_path.setdefault(path_id, []).append(node)
        model_source_keys.setdefault(model_key, set()).add(key.path_key)

    # Add explicit derived parents for every missing prefix.  The parent keeps
    # no obj_code because no epc_nodes row existed for that prefix.
    derived_by_path: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, item in source_nodes.items():
        parts = tuple(segment for segment in item["node_path"].split(_PATH_SEPARATOR) if segment)
        for prefix_length in range(1, len(parts)):
            prefix_parts = parts[:prefix_length]
            prefix_key = make_path_key(prefix_parts)
            path_id = (key.series_code, key.model_code, prefix_key)
            if path_id in source_path_ids:
                continue
            if path_id not in derived_by_path:
                derived_by_path[path_id] = {
                    "series_code": key.series_code,
                    "model_code": key.model_code,
                    "obj_code": None,
                    "node_key": _make_node_key(
                        "derived",
                        series_code=key.series_code,
                        model_code=key.model_code,
                        path_key=prefix_key,
                    ),
                    "is_derived": True,
                    "tree_key": None,
                    "node_path": _PATH_SEPARATOR.join(prefix_parts),
                    "path_key": prefix_key,
                    "parent_key": make_path_key(prefix_parts[:-1]) if len(prefix_parts) > 1 else None,
                    "depth": len(prefix_parts),
                    "display_name": prefix_parts[-1],
                    "child_count": 0,
                    "direct_part_count": 0,
                    "descendant_part_count": 0,
                    "path_variant_count": 0,
                }
    for path_id, node in derived_by_path.items():
        nodes_by_path.setdefault(path_id, []).append(node)

    # Index children by path.  Multiple source rows that share a conflicting
    # path are counted once for hierarchy purposes.
    children: dict[tuple[str, str, str], set[str]] = {}
    for path_id, path_nodes in nodes_by_path.items():
        parent_key = path_nodes[0]["parent_key"]
        if parent_key is not None:
            parent_id = (path_id[0], path_id[1], parent_key)
            children.setdefault(parent_id, set()).add(path_id[2])

    for path_id, path_nodes in nodes_by_path.items():
        child_count = len(children.get(path_id, set()))
        for node in path_nodes:
            node["child_count"] = child_count

    # Compute path-level subtree totals deepest-first.  All retained source
    # variants at one path contribute to that path aggregate, which is then
    # shared by every node at the path because child ownership is unknown.
    direct_totals = {
        path_id: sum(node["direct_part_count"] for node in path_nodes)
        for path_id, path_nodes in nodes_by_path.items()
    }

    path_ids = list(nodes_by_path)
    path_ids.sort(key=lambda path_id: (-nodes_by_path[path_id][0]["depth"], path_id))
    subtree_totals: dict[tuple[str, str, str], int] = {}
    for path_id in path_ids:
        direct = direct_totals.get(path_id, 0)
        child_total = sum(
            subtree_totals.get((path_id[0], path_id[1], child_key), 0)
            for child_key in children.get(path_id, set())
        )
        subtree_totals[path_id] = direct + child_total

    for path_id, path_nodes in nodes_by_path.items():
        descendant_part_count = subtree_totals.get(path_id, 0) - direct_totals.get(path_id, 0)
        for node in path_nodes:
            node["descendant_part_count"] = descendant_part_count

    nodes = [node for path_nodes in nodes_by_path.values() for node in path_nodes]
    nodes.sort(
        key=lambda node: (
            node["series_code"],
            node["model_code"],
            node["depth"],
            node["path_key"],
            node["obj_code"] is None,
            node["obj_code"] or "",
            node["node_key"],
        )
    )

    warning_entries = [*warnings, *structural_conflicts]
    blocking_dicts, warning_dicts = _sorted_errors(blocking_errors, warning_entries)
    structural_conflict_dicts = sorted(structural_conflicts, key=lambda item: _sortable(item))
    summary = {
        "source_row_count": len(source_observations),
        "source_node_count": len(source_nodes),
        "derived_parent_count": len(derived_by_path),
        "node_count": len(nodes),
        "model_count": len(model_source_keys),
        "leaf_node_count": sum(1 for node in nodes if node["child_count"] == 0),
        "zero_direct_part_retained_count": sum(
            1 for node in nodes if node["direct_part_count"] == 0 and node["descendant_part_count"] > 0
        ),
        "total_direct_part_count": sum(node["direct_part_count"] for node in nodes),
        "blocking_error_count": len(blocking_dicts),
        "warning_count": len(warning_dicts),
        "path_conflict_count": len(structural_conflict_dicts),
    }

    return {
        "nodes": nodes,
        "warnings": warning_dicts,
        "structural_conflicts": structural_conflict_dicts,
        "blocking_errors": blocking_dicts,
        "summary": summary,
    }
