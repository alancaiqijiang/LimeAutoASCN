#!/usr/bin/env python3
"""Resumable AI draft translation for the LimeAuto catalog.

The script reads an immutable-release manifest and writes a separate translation
run. It never changes the release database and never writes the API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5"
DEFAULT_BATCH_SIZE = 250
DEFAULT_BUDGET_CNY = 7.50
DEFAULT_INPUT_PRICE_CNY = 1.00
DEFAULT_OUTPUT_PRICE_CNY = 2.00
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]*\d[A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)*|\d+(?:\.\d+)?)(?![A-Za-z0-9])"
)

SYSTEM_PROMPT = """You are translating a Chinese automotive EPC and after-sales parts catalog into professional English.
Return ONLY one valid JSON object. The keys must be exactly the short numeric ids supplied in the batch; each value must be the direct English catalog label for that source.
Do not add explanations, markdown, comments, prefixes, suffixes, or extra keys.
Preserve product/model/material codes, Latin letters, digits, years, hyphens, slashes, Roman numerals, trim codes, colors, and distinctions such as front/rear, left/right, inner/outer, upper/lower.
Use natural automotive terminology: use LHD/RHD for 左舵/右舵, Assembly for 总成, Wiring Harness for 线束, Bumper for 保险杠, Subframe for 副车架, and Shock Absorber for 减振器 where applicable.
Translate Chinese model and brand names only when the recognized automotive English name is clear (for example Qin, Song, Tang, Han, Seal, Dolphin, Yuan); otherwise use a faithful concise transliteration rather than inventing a product fact.
Translate configuration words naturally (for example displacement, Luxury, Premium, Flagship, Honor Edition, interior, charging standard), while keeping the model code unchanged.
If a source is already an English/code label, return it unchanged except for harmless catalog spacing. Never return a blank value."""


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(f"MiMo HTTP {status}{(': ' + message) if message else ''}")


class BatchFormatError(ValueError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row is not an object at {path}:{line_number}")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write(
        path,
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
    )


def response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise BatchFormatError("response has no choices[0]")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise BatchFormatError("response message has no text content")
    return content.strip()


def parse_mapping(text: str) -> dict[str, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise BatchFormatError("response is not a JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BatchFormatError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise BatchFormatError("translation result is not an object")
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise BatchFormatError("translation keys and values must be strings")
        result[key] = value.strip()
    return result


def normalized_code(value: str) -> str:
    return re.sub(r"[\s\-_/]", "", value).casefold()


def qa_translation(source: str, translated: str) -> list[str]:
    errors: list[str] = []
    if not translated:
        errors.append("empty")
        return errors
    if "```" in translated or "\n" in translated or "\r" in translated:
        errors.append("formatting")
    if CJK_RE.search(translated):
        errors.append("cjk_remaining")
    for token in CODE_RE.findall(source):
        if normalized_code(token) not in normalized_code(translated):
            errors.append(f"protected_token_missing:{token}")
    return errors


def request_batch(
    *,
    base_url: str,
    api_key: str,
    model: str,
    batch: list[dict[str, Any]],
    max_completion_tokens: int,
    timeout: int,
) -> tuple[dict[str, str], dict[str, Any], str]:
    terms = [
        {
            "id": str(index),
            "kind": row.get("term_kind", ""),
            "source": row.get("source_text", ""),
            "usage_count": row.get("usage_count", 0),
            "context": row.get("context_samples", [])[:3],
        }
        for index, row in enumerate(batch)
    ]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps({"terms": terms}, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "max_completion_tokens": max_completion_tokens,
        "temperature": 0,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Do not persist or print the provider response: it can contain echoed input.
        raise ApiError(exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, "network error") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchFormatError("provider response is not JSON") from exc
    if not isinstance(result, dict):
        raise BatchFormatError("provider response is not an object")
    mapping = parse_mapping(response_text(result))
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return mapping, usage, str(result.get("id") or "")


def usage_cost(usage: dict[str, Any], input_price: float, output_price: float) -> float:
    try:
        prompt = float(usage.get("prompt_tokens") or 0)
        completion = float(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return 0.0
    return prompt / 1_000_000 * input_price + completion / 1_000_000 * output_price


def batch_path(batch_dir: Path, number: int) -> Path:
    return batch_dir / f"batch-{number:05d}.json"


def load_batch_rows(batch_dir: Path, number: int) -> dict[str, Any] | None:
    path = batch_path(batch_dir, number)
    if not path.exists():
        return None
    value = read_json(path, None)
    return value if isinstance(value, dict) else None


def rebuild_outputs(run_dir: Path, manifest: list[dict[str, Any]], batch_count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    batch_dir = run_dir / "batches"
    rows_by_id: dict[str, dict[str, Any]] = {}
    usage_rows: list[dict[str, Any]] = []
    manifest_index = {str(row["term_id"]): index for index, row in enumerate(manifest)}
    for number in range(1, batch_count + 1):
        saved = load_batch_rows(batch_dir, number)
        if not saved:
            continue
        for row in saved.get("rows", []):
            if not isinstance(row, dict):
                continue
            term_id = str(row.get("term_id"))
            if term_id in manifest_index and term_id not in rows_by_id:
                rows_by_id[term_id] = row
        usage = saved.get("usage")
        if isinstance(usage, dict):
            usage_rows.append(usage)
    rows = list(rows_by_id.values())
    rows.sort(key=lambda row: manifest_index.get(str(row.get("term_id")), len(manifest)))
    write_jsonl(run_dir / "ai_draft.jsonl", rows)
    write_jsonl(run_dir / "usage.jsonl", usage_rows)
    return rows, usage_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--api-key-env", default="MIMO_API_KEY")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--budget-cny", type=float, default=DEFAULT_BUDGET_CNY)
    parser.add_argument("--input-price-cny", type=float, default=DEFAULT_INPUT_PRICE_CNY)
    parser.add_argument("--output-price-cny", type=float, default=DEFAULT_OUTPUT_PRICE_CNY)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retry-limit", type=int, default=1)
    parser.add_argument("--max-batches", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size < 1 or args.budget_cny <= 0:
        raise SystemExit("batch-size and budget-cny must be positive")
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"missing API key environment variable: {args.api_key_env}")
    manifest = read_jsonl(args.manifest)
    if not manifest:
        raise SystemExit("manifest is empty")
    for row in manifest:
        for field in ("term_id", "term_kind", "source_text"):
            if not str(row.get(field, "")).strip():
                raise SystemExit(f"manifest row missing {field}")

    run_dir = args.run_dir
    batch_dir = run_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batches = [manifest[i : i + args.batch_size] for i in range(0, len(manifest), args.batch_size)]
    limit = min(len(batches), args.max_batches) if args.max_batches else len(batches)
    state_path = run_dir / "state.json"
    state = read_json(state_path, {})
    spent = float(state.get("spent_cny") or 0)
    if "spent_cny" not in state:
        spent = sum(
            usage_cost(row, args.input_price_cny, args.output_price_cny)
            for row in read_jsonl(run_dir / "usage.jsonl")
        )
    started_at = state.get("started_at") or now_iso()
    state.update(
        {
            "run_status": "running",
            "started_at": started_at,
            "updated_at": now_iso(),
            "engine": args.model,
            "base_url": args.base_url,
            "batch_size": args.batch_size,
            "manifest_count": len(manifest),
            "batch_count": len(batches),
            "budget_cny": args.budget_cny,
            "input_price_cny_per_million": args.input_price_cny,
            "output_price_cny_per_million": args.output_price_cny,
        }
    )
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    for number in range(1, limit + 1):
        if load_batch_rows(batch_dir, number):
            continue
        if spent >= args.budget_cny:
            state["run_status"] = "budget_cap_reached"
            break
        batch = batches[number - 1]
        mapping: dict[str, str] | None = None
        usage: dict[str, Any] = {}
        response_id = ""
        last_error = ""
        for attempt in range(args.retry_limit + 1):
            try:
                mapping, usage, response_id = request_batch(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    batch=batch,
                    max_completion_tokens=12000,
                    timeout=args.timeout,
                )
                break
            except ApiError as exc:
                last_error = str(exc)
                if exc.status in {401, 402, 403}:
                    state["run_status"] = "provider_stopped"
                    state["stop_reason"] = last_error
                    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                    rows, usage_rows = rebuild_outputs(run_dir, manifest, len(batches))
                    write_final_report(run_dir, state, rows, usage_rows, manifest, len(batches))
                    print(json.dumps({"status": state["run_status"], "reason": last_error, "completed": len(rows), "spent_cny": round(spent, 8)}, ensure_ascii=False), flush=True)
                    return 2
                if attempt < args.retry_limit:
                    time.sleep(min(30, 2 ** attempt))
            except (BatchFormatError, urllib.error.URLError, TimeoutError) as exc:
                last_error = type(exc).__name__ + ": " + str(exc)
                if attempt < args.retry_limit:
                    time.sleep(min(30, 2 ** attempt))
        if mapping is None:
            saved_rows = [
                {
                    **row,
                    "translated_text": "",
                    "engine": args.model,
                    "status": "rejected",
                    "structural_qa": "failed",
                    "qa_errors": [last_error or "batch_failed"],
                    "semantic_review": "required",
                    "batch": number,
                    "created_at": now_iso(),
                }
                for row in batch
            ]
            saved_usage = {"batch": number, "status": "rejected", "error": last_error or "batch_failed", "cost_cny": 0.0}
        else:
            expected = {str(index) for index in range(len(batch))}
            actual = set(mapping)
            saved_rows = []
            for index, row in enumerate(batch):
                translated = mapping.get(str(index), "")
                errors = qa_translation(str(row["source_text"]), translated)
                if str(index) not in actual:
                    errors.append("missing_id")
                saved_rows.append(
                    {
                        **row,
                        "translated_text": translated,
                        "engine": args.model,
                        "status": "ai_draft" if not errors else "human_review",
                        "structural_qa": "pass" if not errors else "failed",
                        "qa_errors": errors,
                        "semantic_review": "required",
                        "response_id": response_id,
                        "batch": number,
                        "created_at": now_iso(),
                    }
                )
            extra = sorted(actual - expected)
            cost = usage_cost(usage, args.input_price_cny, args.output_price_cny)
            saved_usage = {
                "batch": number,
                "status": "ok",
                "response_id": response_id,
                "model": args.model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "cost_cny": cost,
                "currency": "CNY",
                "extra_ids": extra,
                "created_at": now_iso(),
            }
        atomic_write(
            batch_path(batch_dir, number),
            json.dumps({"batch": number, "rows": saved_rows, "usage": saved_usage}, ensure_ascii=False, indent=2) + "\n",
        )
        spent += float(saved_usage.get("cost_cny") or 0)
        rows, usage_rows = rebuild_outputs(run_dir, manifest, len(batches))
        state.update(
            {
                "processed_batches": number,
                "completed_rows": len(rows),
                "spent_cny": spent,
                "updated_at": now_iso(),
                "last_batch_status": saved_usage.get("status"),
            }
        )
        atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "batch": number,
                    "batches": len(batches),
                    "rows": len(rows),
                    "batch_status": saved_usage.get("status"),
                    "batch_tokens": saved_usage.get("total_tokens"),
                    "batch_cost_cny": round(float(saved_usage.get("cost_cny") or 0), 8),
                    "spent_cny": round(spent, 8),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    rows, usage_rows = rebuild_outputs(run_dir, manifest, len(batches))
    state["run_status"] = "complete" if len(rows) == len(manifest) else state.get("run_status", "partial")
    state["completed_rows"] = len(rows)
    state["spent_cny"] = spent
    state["updated_at"] = now_iso()
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    write_final_report(run_dir, state, rows, usage_rows, manifest, len(batches))
    print(json.dumps({"status": state["run_status"], "completed": len(rows), "manifest": len(manifest), "spent_cny": round(spent, 8)}, ensure_ascii=False), flush=True)
    return 0


def write_final_report(
    run_dir: Path,
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    batch_count: int,
) -> None:
    by_status: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status") or "unknown")
        by_status[key] = by_status.get(key, 0) + 1
    processed_ids = {str(row.get("term_id")) for row in rows}
    report = {
        "run_id": run_dir.name,
        "run_status": state.get("run_status"),
        "engine": state.get("engine"),
        "manifest_count": len(manifest),
        "translated_rows_written": len(rows),
        "missing_rows": len(manifest) - len(processed_ids),
        "batch_count": batch_count,
        "usage_batches": len(usage_rows),
        "status_counts": by_status,
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usage_rows if str(row.get("total_tokens") or "").isdigit()),
        "spent_cny": state.get("spent_cny", 0),
        "budget_cny": state.get("budget_cny"),
        "semantic_review_required": len(rows),
        "created_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
    }
    atomic_write(run_dir / "qa_report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    review = [row for row in rows if row.get("status") != "published"]
    write_jsonl(run_dir / "review_queue.jsonl", review)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
