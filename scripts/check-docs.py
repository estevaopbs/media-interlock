#!/usr/bin/env python3
"""Validate MediaInterlock's bounded, indexed documentation surface."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote


SCHEMA = "media-interlock.docs-index/v1"
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REFERENCE_LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\[[^\]]*\]")
REFERENCE_DEFINITION_PATTERN = re.compile(r"(?m)^\s{0,3}\[[^\]]+\]:\s*\S")
RAW_HTML_LINK_PATTERN = re.compile(r"<a\s+[^>]*href\s*=", re.IGNORECASE)
URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
ALLOWED_EXTERNAL_SCHEMES = {"http", "https", "mailto"}
WORK_KINDS = {"spec", "slice", "plan"}
WORK_PREFIXES = {
    "spec": "docs/work/specs/",
    "slice": "docs/work/slices/",
    "plan": "docs/work/plans/",
}
HARD_PERMANENT_FILES = 12
HARD_PERMANENT_BYTES = 160 * 1024
HARD_WORK_ACTIVE = {kind: 1 for kind in WORK_KINDS}
HARD_WORK_BYTES = {"spec": 64 * 1024, "slice": 24 * 1024, "plan": 32 * 1024}

TOP_LEVEL_KEYS = {"schema", "project", "budgets", "program", "documents"}
PROJECT_KEYS = {"name", "status"}
BUDGET_KEYS = {"permanent_markdown", "work_markdown"}
PERMANENT_BUDGET_KEYS = {"max_files", "max_bytes"}
WORK_BUDGET_KEYS = {"max_active_by_kind", "max_bytes_by_kind"}
PROGRAM_KEYS = {
    "id",
    "status",
    "specification",
    "slice_catalog",
    "active_plan",
    "units",
}
UNIT_KEYS = {"id", "status"}
PERMANENT_DOCUMENT_KEYS = {"path", "class", "owner", "load_when"}
WORK_DOCUMENT_KEYS = {*PERMANENT_DOCUMENT_KEYS, "kind"}


class DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_index(root: Path, errors: list[str]) -> dict[str, Any] | None:
    path = root / "docs" / "index.json"
    try:
        if path.is_symlink() or not path.is_file():
            errors.append("documentation index must be a regular non-symlink file")
            return None
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except FileNotFoundError:
        errors.append("missing documentation index: docs/index.json")
        return None
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError, OSError) as exc:
        errors.append(f"invalid documentation index: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append("documentation index must be a JSON object")
        return None
    if value.get("schema") != SCHEMA:
        errors.append(f"documentation index schema must be {SCHEMA}")
        return None
    return value


def _check_keys(
    value: object, expected: set[str], label: str, errors: list[str]
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return False
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        errors.append(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        errors.append(f"{label} unknown keys: {', '.join(unknown)}")
    return not missing and not unknown


def _safe_markdown_path(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.endswith(".md"):
        return None
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts or raw != candidate.as_posix():
        return None
    return raw


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _budget(index: dict[str, Any], *keys: str) -> int | None:
    value: object = index.get("budgets")
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _positive_integer(value)


def _markdown_paths(root: Path) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.md",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return {line for line in result.stdout.splitlines() if line}
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.md")
        if ".git" not in path.relative_to(root).parts
    }


def _file_problem(root: Path, relative: str) -> str | None:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            return "Markdown path contains a symlink"
    try:
        if not current.exists():
            return "indexed Markdown does not exist"
        if not current.is_file():
            return "indexed Markdown must be a regular file"
    except OSError as exc:
        return f"cannot inspect indexed Markdown: {exc}"
    return None


def _local_links(path: Path, content: str, root: Path):
    for match in LINK_PATTERN.finditer(content):
        raw = match.group(1).strip()
        if raw.startswith("<") and ">" in raw:
            target = raw[1 : raw.index(">")]
        else:
            target = raw.split(maxsplit=1)[0]
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target or target.startswith(("#", "//")) or URI_SCHEME_PATTERN.match(target):
            continue
        resolved = (path.parent / unquote(target)).resolve()
        try:
            relative = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            yield target, None
            continue
        yield target, (relative, resolved)


def _links_to_work_material(path: Path, content: str, root: Path) -> bool:
    for _, result in _local_links(path, content, root):
        if result is None:
            continue
        relative, _ = result
        if relative.startswith("docs/work/"):
            return True
    return False


def _link_policy_errors(relative: str, content: str) -> list[str]:
    errors: list[str] = []
    if REFERENCE_LINK_PATTERN.search(content) or REFERENCE_DEFINITION_PATTERN.search(
        content
    ):
        errors.append(f"reference-style links are unsupported: {relative}")
    if RAW_HTML_LINK_PATTERN.search(content):
        errors.append(f"raw HTML links are unsupported: {relative}")
    for match in LINK_PATTERN.finditer(content):
        raw = match.group(1).strip()
        if raw.startswith("<") and ">" in raw:
            target = raw[1 : raw.index(">")]
        else:
            target = raw.split(maxsplit=1)[0]
        if target.startswith("//"):
            errors.append(
                f"external links require an explicit URI scheme: {relative} -> {target}"
            )
            continue
        scheme = URI_SCHEME_PATTERN.match(target)
        if scheme is not None:
            name = scheme.group(0)[:-1].lower()
            if name not in ALLOWED_EXTERNAL_SCHEMES:
                errors.append(
                    f"unsupported link scheme in {relative}: {name}"
                )
    return errors


def _validate_program(
    program: object,
    metadata: dict[str, tuple[str, str | None]],
    work: list[tuple[str, str, int]],
    errors: list[str],
) -> None:
    if program is None:
        if work:
            errors.append("work material requires an indexed active program")
        return
    if not _check_keys(program, PROGRAM_KEYS, "program", errors):
        if not isinstance(program, dict):
            return
    assert isinstance(program, dict)
    if not isinstance(program.get("id"), str) or not program["id"].strip():
        errors.append("program.id must be a non-empty string")
    if program.get("status") != "active":
        errors.append("program.status must be active while work material is indexed")

    pointer_kinds = {
        "specification": "spec",
        "slice_catalog": "slice",
        "active_plan": "plan",
    }
    routed: set[str] = set()
    for field, kind in pointer_kinds.items():
        value = program.get(field)
        if field == "active_plan" and value is None:
            continue
        if not isinstance(value, str) or not value:
            errors.append(f"program {field.replace('_', ' ')} must reference a path")
            continue
        routed.add(value)
        if metadata.get(value) != ("work", kind):
            errors.append(
                f"program {field.replace('_', ' ')} must reference indexed {kind} work: {value}"
            )

    work_paths = {relative for relative, _, _ in work}
    for relative in sorted(work_paths - routed):
        errors.append(f"work material is not routed by program: {relative}")

    units = program.get("units")
    if not isinstance(units, list):
        errors.append("program.units must be an array")
        return
    ids: list[str] = []
    active_units = 0
    phase = "completed"
    for position, unit in enumerate(units):
        label = f"program.units[{position}]"
        if not _check_keys(unit, UNIT_KEYS, label, errors):
            if not isinstance(unit, dict):
                continue
        assert isinstance(unit, dict)
        unit_id = unit.get("id")
        status = unit.get("status")
        if not isinstance(unit_id, str) or not unit_id:
            errors.append(f"{label}.id must be a non-empty string")
            continue
        ids.append(unit_id)
        if status not in {"pending", "in_progress", "completed", "blocked"}:
            errors.append(f"{label}.status is invalid")
            continue
        if status == "completed":
            if phase != "completed":
                errors.append(f"program unit order is invalid at {unit_id}")
        elif status in {"in_progress", "blocked"}:
            active_units += 1
            if phase != "completed":
                errors.append(f"program unit order is invalid at {unit_id}")
            phase = "active"
        else:
            phase = "pending"

    for duplicate, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate program unit ID: {duplicate}")
    if active_units > 1:
        errors.append("program may have at most one active program unit")
    active_plan = program.get("active_plan")
    if (active_plan is None and active_units) or (
        active_plan is not None and active_units != 1
    ):
        errors.append("active plan and active program unit must be present together")


def audit_repository(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    index = _load_index(root, errors)
    if index is None:
        return errors


    _check_keys(index, TOP_LEVEL_KEYS, "documentation index", errors)
    project = index.get("project")
    _check_keys(project, PROJECT_KEYS, "project", errors)
    if isinstance(project, dict):
        for field in sorted(PROJECT_KEYS):
            if not isinstance(project.get(field), str) or not project[field].strip():
                errors.append(f"project.{field} must be a non-empty string")
    budgets = index.get("budgets")
    if _check_keys(budgets, BUDGET_KEYS, "budgets", errors):
        assert isinstance(budgets, dict)
        permanent_budget = budgets.get("permanent_markdown")
        work_budget = budgets.get("work_markdown")
        _check_keys(
            permanent_budget,
            PERMANENT_BUDGET_KEYS,
            "budgets.permanent_markdown",
            errors,
        )
        if _check_keys(
            work_budget, WORK_BUDGET_KEYS, "budgets.work_markdown", errors
        ):
            assert isinstance(work_budget, dict)
            _check_keys(
                work_budget.get("max_active_by_kind"),
                WORK_KINDS,
                "budgets.work_markdown.max_active_by_kind",
                errors,
            )
            _check_keys(
                work_budget.get("max_bytes_by_kind"),
                WORK_KINDS,
                "budgets.work_markdown.max_bytes_by_kind",
                errors,
            )

    documents = index.get("documents")
    if not isinstance(documents, list):
        return [*errors, "documentation index documents must be an array"]

    indexed: list[str] = []
    permanent: list[tuple[str, int]] = []
    work: list[tuple[str, str, int]] = []
    metadata: dict[str, tuple[str, str | None]] = {}

    for position, entry in enumerate(documents):
        label = f"documents[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        document_class = entry.get("class")
        expected_keys = (
            WORK_DOCUMENT_KEYS if document_class == "work" else PERMANENT_DOCUMENT_KEYS
        )
        _check_keys(entry, expected_keys, label, errors)
        relative = _safe_markdown_path(entry.get("path"))
        if relative is None:
            errors.append(f"{label}.path must be a normalized relative Markdown path")
            continue
        indexed.append(relative)

        load_when = entry.get("load_when")
        if isinstance(load_when, list) and len(load_when) != len(
            {item for item in load_when if isinstance(item, str)}
        ):
            errors.append(f"{relative}: load_when entries must be unique")

        owner = entry.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            errors.append(f"{relative}: owner must be non-empty")
        if (
            not isinstance(load_when, list)
            or not load_when
            or not all(isinstance(item, str) and item.strip() for item in load_when)
        ):
            errors.append(f"{relative}: load_when must contain non-empty strings")

        path = root / relative
        problem = _file_problem(root, relative)
        if problem is not None:
            if "symlink" in problem:
                errors.append(f"Markdown must not be a symlink: {relative}")
            else:
                errors.append(f"{problem}: {relative}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"Markdown is not valid UTF-8: {relative}")
            continue
        except OSError as exc:
            errors.append(f"cannot read indexed Markdown {relative}: {exc}")
            continue

        size = len(content.encode("utf-8"))
        if document_class == "permanent":
            metadata[relative] = ("permanent", None)
            permanent.append((relative, size))
            if relative.startswith("docs/work/"):
                errors.append(f"class/path mismatch: {relative} is permanent under docs/work")
            if _links_to_work_material(path, content, root):
                errors.append(f"permanent document links to work material: {relative}")
        elif document_class == "work":
            kind = entry.get("kind")
            if kind not in WORK_KINDS:
                errors.append(f"{relative}: work kind must be spec, slice, or plan")
            else:
                metadata[relative] = ("work", kind)
                work.append((relative, kind, size))
                if not relative.startswith(WORK_PREFIXES[kind]):
                    errors.append(
                        f"class/path mismatch: {relative} is not under {WORK_PREFIXES[kind]}"
                    )
        else:
            errors.append(f"{relative}: class must be permanent or work")

        errors.extend(_link_policy_errors(relative, content))

        for target, result in _local_links(path, content, root):
            if result is None:
                errors.append(f"local link leaves repository: {relative} -> {target}")
                continue
            _, target_path = result
            if not target_path.exists():
                errors.append(f"broken local link: {relative} -> {target}")

    duplicates = sorted(path for path, count in Counter(indexed).items() if count > 1)
    for path in duplicates:
        errors.append(f"Markdown is indexed more than once: {path}")

    actual = _markdown_paths(root)
    indexed_set = set(indexed)
    for path in sorted(actual - indexed_set):
        errors.append(f"unindexed Markdown: {path}")
    for path in sorted(indexed_set - actual):
        errors.append(f"indexed Markdown does not exist: {path}")

    _validate_program(index.get("program"), metadata, work, errors)

    max_permanent_files = _budget(index, "permanent_markdown", "max_files")
    max_permanent_bytes = _budget(index, "permanent_markdown", "max_bytes")
    if max_permanent_files is None or max_permanent_bytes is None:
        errors.append("permanent Markdown budgets must be positive integers")
    else:
        if max_permanent_files > HARD_PERMANENT_FILES:
            errors.append(
                f"permanent Markdown file budget exceeds hard ceiling: "
                f"{max_permanent_files} > {HARD_PERMANENT_FILES}"
            )
        if max_permanent_bytes > HARD_PERMANENT_BYTES:
            errors.append(
                f"permanent Markdown byte budget exceeds hard ceiling: "
                f"{max_permanent_bytes} > {HARD_PERMANENT_BYTES}"
            )
        if len(permanent) > max_permanent_files:
            errors.append(
                f"permanent Markdown file budget exceeded: {len(permanent)} > {max_permanent_files}"
            )
        permanent_bytes = sum(size for _, size in permanent)
        if permanent_bytes > max_permanent_bytes:
            errors.append(
                "permanent Markdown byte budget exceeded: "
                f"{permanent_bytes} > {max_permanent_bytes}"
            )

    work_counts = Counter(kind for _, kind, _ in work)
    for kind in sorted(WORK_KINDS):
        max_active = _budget(index, "work_markdown", "max_active_by_kind", kind)
        max_bytes = _budget(index, "work_markdown", "max_bytes_by_kind", kind)
        if max_active is None or max_bytes is None:
            errors.append(f"work Markdown budgets for {kind} must be positive integers")
            continue
        if max_active > HARD_WORK_ACTIVE[kind]:
            errors.append(
                f"active {kind} budget exceeds hard ceiling: "
                f"{max_active} > {HARD_WORK_ACTIVE[kind]}"
            )
        if max_bytes > HARD_WORK_BYTES[kind]:
            errors.append(
                f"{kind} byte budget exceeds hard ceiling: "
                f"{max_bytes} > {HARD_WORK_BYTES[kind]}"
            )
        if work_counts[kind] > max_active:
            errors.append(
                f"active {kind} document budget exceeded: {work_counts[kind]} > {max_active}"
            )
        for relative, entry_kind, size in work:
            if entry_kind == kind and size > max_bytes:
                errors.append(
                    f"{kind} document byte budget exceeded: {relative} ({size} > {max_bytes})"
                )

    return errors


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("usage: check-docs.py [repository-root]", file=sys.stderr)
        return 2
    root = Path(argv[1]) if len(argv) == 2 else Path(__file__).parents[1]
    errors = audit_repository(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("documentation index and budgets are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
