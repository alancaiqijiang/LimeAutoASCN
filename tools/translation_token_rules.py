"""Shared source-token rules for LimeAuto catalog translation review.

Design rule (adjudicated 2026-09-10 by GPT-5.6-Luna, confidence high):

    The review gate exists to keep the *meaning* of the Chinese source and the
    English translation equal. Only true identifiers are preserved literally.
    Any letter/digit run that merely *describes* the part -- displacement,
    power, voltage/current, dimensions, position or trim index -- must be free
    to take normal English typography (spacing, unit casing, x/× signs).

Concretely, from the shipped corpus:

* ``BYD6420S5D``, ``BYDQ727Y2035``, ``L3-6102200B-00BW``, ``471Q-3707801``,
  ``M00666`` -- atomic identifiers, checked as exact substrings.
* ``1.5LDCT`` -> ``1.5L DCT`` -- compositional powderain shorthand, never one
  code.
* ``70kw`` -> ``70 kW`` -- a power specification, not a code. ``61HP`` looked
  like a unit in a naive scan but is only ever a fragment of a part number
  (``HA2HPA-10``); it is therefore *not* treated as a unit.
* ``20X35`` -> ``20 × 35`` -- dimensional information.
* ``BYDQ727Y2035_20X35`` and ``灰黑6_M00666`` -- an underscore separates
  independently classifiable fields; it never promotes the whole field to one
  identifier.
* ``1号`` -> ``No. 1`` -- a position number.

The gate is deliberately one-directional: when a run is ambiguous it falls
into the *softer* spec check rather than an exact-literal check, because a
losing a value is a real defect while re-spacing a value is not.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Maximal Latin/digit runs. An underscore always breaks a run, so
# underscore-separated fields are classified independently; hyphens and dots
# stay inside a run so that real identifiers such as ``L3-6102200B-00BW``
# survive whole.
RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")

# Chinese source catalogs commonly concatenate a displacement and transmission
# abbreviation, e.g. 1.5LDCT. Professional English typography is "1.5L DCT".
POWERTRAIN_RE = re.compile(
    r"(?P<displacement>\d+(?:\.\d+)?L)"
    r"(?P<transmission>DCT|CVT|AMT|AT|MT|IMT|ECT|EVT)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

DIMENSION_RE = re.compile(
    r"^(?P<first>\d+(?:\.\d+)?)(?:X|x|\u00d7|\*)(?P<second>\d+(?:\.\d+)?)$"
)

SPEC_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<letters>[A-Za-z]{1,3})$")

# A literal run is checked as an ordered sequence of its letter and number
# groups against the alphanumeric skeleton of the translation. Spacing, hyphen
# and case are therefore free -- `PLUS5G` -> `PLUS 5G`, `Y-19` -> `Y - 2019`,
# `Y55KM` -> `Y 55 km` -- while dropping a group or reordering them still fails.
GROUP_RE = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")
# Dots are kept: a numeric group may legitimately be a decimal (`3.0` in
# `dilink3.0` -> `DiLink3.0`), so stripping them would break the comparison.
NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9.]")
# A two-digit group in a Chinese catalog is frequently a model year written
# short (`19款` = 2019). Accept the century expansion, but nothing else, so a
# wrong year still fails.
YEAR_CENTURIES = ("19", "20")

# Documented designation equivalences: one material under two standards. A
# translated equivalent is accepted instead of the source spelling, and the
# pairing is listed here rather than handled ad hoc in a repair script.
DESIGNATION_ALIASES = {
    "fpm": ("fkm",),
    "fkm": ("fpm",),
}


def _group_candidates(group: str) -> list[str]:
    folded = group.casefold()
    candidates = [folded]
    if folded.isdigit() and len(folded) == 2:
        candidates.extend(f"{century}{folded}" for century in YEAR_CENTURIES)
    return candidates


def skeleton(text: str) -> str:
    """Case-folded alphanumeric skeleton, used for spacing-insensitive checks."""
    return NON_ALNUM_RE.sub("", text).casefold()


def ordered_groups_present(
    run: str, translated_text: str, *, strict_numeric_tail: bool = False
) -> bool:
    """Check a run's letter/number groups appear, in order, in the translation.

    ``strict_numeric_tail`` additionally forbids the final numeric group from
    being followed by another digit. It is used only when the source marks a
    model year (``Y-19款``), so that ``Y 2019`` passes while ``Y 1990`` fails;
    identifiers such as ``L3-6102200B`` keep the looser behaviour.
    """
    haystack = skeleton(translated_text)
    position = 0
    groups = GROUP_RE.findall(run)
    for index, group in enumerate(groups):
        found = -1
        matched = group.casefold()
        for candidate in _group_candidates(group):
            at = haystack.find(candidate, position)
            if at < 0 or (found >= 0 and at >= found):
                continue
            end = at + len(candidate)
            if (
                strict_numeric_tail
                and index == len(groups) - 1
                and end < len(haystack)
                and haystack[end].isdigit()
            ):
                continue
            found = at
            matched = candidate
        if found < 0:
            return False
        position = found + len(matched)
    return True

SEPARATOR = r"[\s\-\u2010-\u2015]*"

# `款`/`年` right after a run marks it as a model year (`Y-19款` = trim Y, 2019).
YEAR_MARKERS = "\u6b3e\u5e74"


def year_context_tokens(source_text: str) -> set[str]:
    """Runs that the source marks as a model year, checked with a tight tail."""
    tokens: set[str] = set()
    for match in RUN_RE.finditer(source_text):
        marker = source_text[match.end() : match.end() + 1]
        if marker and marker in YEAR_MARKERS:
            tokens.add(match.group(0))
    return tokens

# Spell-out aliases for units whose English form is legitimately written out.
UNIT_ALIASES = {
    "a": r"(?:a|amps?|amperes?)",
    "v": r"(?:v|volts?)",
    "w": r"(?:w|watts?)",
    "kw": r"(?:kw|kilowatts?)",
    "l": r"(?:l|liters?|litres?)",
    "ml": r"(?:ml|milliliters?|millilitres?)",
    "mm": r"(?:mm|millimeters?|millimetres?)",
    "cm": r"(?:cm|centimeters?|centimetres?)",
    "km": r"(?:km|kilometers?|kilometres?)",
    "g": r"(?:g|gb|gigabytes?|grams?)",
    "kg": r"(?:kg|kilograms?)",
    "ps": r"(?:ps|horsepower)",
    "mah": r"(?:mah|milliamp[\s-]?hours?)",
    "ma": r"(?:ma|milliamps?|milliamperes?)",
    "ah": r"(?:ah|amp[\s-]?hours?)",
}


def compositional_powertrains(source_text: str) -> list[tuple[str, str, tuple[int, int]]]:
    """Return (displacement, transmission, span) for concatenated powertrains."""
    return [
        (
            match.group("displacement"),
            match.group("transmission"),
            match.span(),
        )
        for match in POWERTRAIN_RE.finditer(source_text)
    ]


def _overlaps(span: tuple[int, int], other: tuple[int, int]) -> bool:
    return span[0] < other[1] and other[0] < span[1]


def _spec_pattern(number: str, letters: str) -> re.Pattern[str]:
    alias = UNIT_ALIASES.get(letters.casefold()) or re.escape(letters.casefold())
    # A trailing ``\b`` would fail against ``_`` because underscore is a word
    # character; exclude only real alphanumerics so "10mm_Carbon" still passes.
    return re.compile(
        rf"(?<![0-9]){re.escape(number)}{SEPARATOR}(?:{alias})(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def _dimension_pattern(first: str, second: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![0-9]){re.escape(first)}{SEPARATOR}(?:X|x|\u00d7|\*){SEPARATOR}"
        rf"{re.escape(second)}(?![0-9])",
        re.IGNORECASE,
    )


def _classify(
    run: str,
    span: tuple[int, int] | None,
    powertrain_spans: list[tuple[int, int]],
) -> tuple[str, object]:
    """Classify one run: returns ("literal"|"spec"|"dimension"|"skip", payload)."""
    if span is not None and any(_overlaps(span, s) for s in powertrain_spans):
        return "skip", None
    dimension = DIMENSION_RE.fullmatch(run)
    if dimension:
        return "dimension", _dimension_pattern(
            dimension.group("first"), dimension.group("second")
        )
    spec = SPEC_RE.fullmatch(run)
    if spec:
        return "spec", _spec_pattern(spec.group("number"), spec.group("letters"))
    if run.isdigit():
        # Trim/position indices ("灰黑6", "1号", "473") keep their value; a
        # translation that drops the index has changed the variant.
        return "literal", run
    if run.isalpha():
        # Material/technology/module abbreviations ("PP", "ABS", "NFC", "DMS")
        # are identifiers too. Single characters are excluded because a
        # one-letter substring test is not informative; they stay under
        # semantic review.
        return ("literal", run) if len(run) >= 2 else ("skip", None)
    return "literal", run


def token_requirements(
    source_text: str,
    qa_errors: Iterable[object] = (),
) -> tuple[list[str], list[tuple[str, re.Pattern[str]]]]:
    """Split a source string into literal identifiers and composed specs.

    Returns ``(literals, specs)`` where ``literals`` must appear verbatim in
    the translation and each ``specs`` entry is ``(label, pattern)`` describing
    a descriptive value that must survive semantically, not byte-for-byte.
    """
    literals: list[str] = []
    specs: list[tuple[str, re.Pattern[str]]] = []
    powertrains = compositional_powertrains(source_text)
    powertrain_spans = [span for _, _, span in powertrains]

    for match in RUN_RE.finditer(source_text):
        run = match.group(0)
        kind, payload = _classify(run, match.span(), powertrain_spans)
        if kind == "literal":
            literals.append(str(payload))
        elif kind in {"spec", "dimension"}:
            specs.append((run, payload))  # type: ignore[arg-type]

    for displacement, transmission, _ in powertrains:
        specs.append(
            (
                f"{displacement} displacement",
                _spec_pattern(displacement[: -1], displacement[-1]),
            )
        )
        literals.append(transmission)

    for error in qa_errors:
        if not isinstance(error, str):
            continue
        flagged = error.split(":", 1)[-1].strip() if ":" in error else error.strip()
        if not flagged or not any(char.isdigit() for char in flagged):
            continue
        if not RUN_RE.fullmatch(flagged):
            continue
        # A flagged fragment of a compositional powertrain (`5LDCT` from
        # `1.5LDCT`) is not an identifier; its components are checked above.
        folded = skeleton(flagged)
        if any(
            folded in skeleton(f"{displacement}{transmission}")
            for displacement, transmission, _ in powertrains
        ):
            continue
        kind, payload = _classify(flagged, None, powertrain_spans)
        if kind == "literal" and str(payload) not in literals:
            literals.append(str(payload))
        elif kind in {"spec", "dimension"} and (flagged, payload) not in specs:
            specs.append((flagged, payload))  # type: ignore[arg-type]

    return _dedupe(literals), _dedupe_specs(specs)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _dedupe_specs(
    values: list[tuple[str, re.Pattern[str]]],
) -> list[tuple[str, re.Pattern[str]]]:
    seen: set[str] = set()
    result: list[tuple[str, re.Pattern[str]]] = []
    for label, pattern in values:
        if label in seen:
            continue
        seen.add(label)
        result.append((label, pattern))
    return result


def protected_tokens(
    source_text: str,
    qa_errors: Iterable[object] = (),
) -> list[str]:
    """Return literal identifiers that must survive translation unchanged."""
    return token_requirements(source_text, qa_errors)[0]


def translation_token_errors(
    source_text: str,
    translated_text: str,
    qa_errors: Iterable[object] = (),
) -> list[str]:
    """Validate literal identifiers and composed value semantics."""
    literals, specs = token_requirements(source_text, qa_errors)
    year_tokens = year_context_tokens(source_text)
    errors: list[str] = []
    for token in literals:
        if ordered_groups_present(
            token, translated_text, strict_numeric_tail=token in year_tokens
        ):
            continue
        # A documented designation equivalence counts as preserved.
        if any(
            alias in skeleton(translated_text)
            for alias in DESIGNATION_ALIASES.get(token.casefold(), ())
        ):
            continue
        errors.append(f"protected token missing after review: {token}")
    for label, pattern in specs:
        if not pattern.search(translated_text):
            errors.append(f"composed value missing after review: {label}")
    return errors
