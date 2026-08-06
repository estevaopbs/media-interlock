from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "check-docs.py"
SPEC = importlib.util.spec_from_file_location("check_docs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_docs)


class DocumentationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "docs").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_index(
        self,
        documents: list[dict[str, object]],
        *,
        max_files: int = 12,
        max_bytes: int = 160 * 1024,
        program: dict[str, object] | None = None,
    ) -> None:
        payload = {
            "schema": "media-interlock.docs-index/v1",
            "project": {"name": "Test", "status": "test"},
            "budgets": {
                "permanent_markdown": {
                    "max_files": max_files,
                    "max_bytes": max_bytes,
                },
                "work_markdown": {
                    "max_active_by_kind": {"spec": 1, "slice": 1, "plan": 1},
                    "max_bytes_by_kind": {
                        "spec": 64 * 1024,
                        "slice": 24 * 1024,
                        "plan": 32 * 1024,
                    },
                },
            },
            "program": program,
            "documents": documents,
        }
        (self.root / "docs" / "index.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def write_markdown(self, relative: str, content: str = "# Document\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def entry(path: str, document_class: str, *, kind: str | None = None):
        result: dict[str, object] = {
            "path": path,
            "class": document_class,
            "owner": "test authority",
            "load_when": ["testing documentation routing"],
        }
        if kind is not None:
            result["kind"] = kind
        return result

    def test_accepts_an_exhaustive_bounded_index(self) -> None:
        self.write_markdown("README.md")
        self.write_markdown("docs/work/specs/design.md")
        self.write_markdown("docs/work/slices/catalog.md")
        self.write_index(
            [
                self.entry("README.md", "permanent"),
                self.entry("docs/work/specs/design.md", "work", kind="spec"),
                self.entry("docs/work/slices/catalog.md", "work", kind="slice"),
            ],
            program={
                "id": "TEST",
                "status": "active",
                "specification": "docs/work/specs/design.md",
                "slice_catalog": "docs/work/slices/catalog.md",
                "active_plan": None,
                "units": [],
            },
        )

        self.assertEqual([], check_docs.audit_repository(self.root))

    def test_rejects_unindexed_markdown_and_budget_overrun(self) -> None:
        self.write_markdown("README.md", "# Too large\n")
        self.write_markdown("UNTRACKED.md")
        self.write_index(
            [self.entry("README.md", "permanent")],
            max_files=1,
            max_bytes=4,
        )

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("unindexed Markdown: UNTRACKED.md" in error for error in errors))
        self.assertTrue(any("permanent Markdown byte budget exceeded" in error for error in errors))

    def test_rejects_current_links_to_transient_work_material(self) -> None:
        self.write_markdown(
            "docs/current/state.md",
            "# State\n\n[Do not depend on this](../work/plan.md)\n",
        )
        self.write_markdown("docs/work/plan.md")
        self.write_index(
            [
                self.entry("docs/current/state.md", "permanent"),
                self.entry("docs/work/plan.md", "work", kind="plan"),
            ]
        )

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(
            any("permanent document links to work material" in error for error in errors)
        )

    def test_rejects_duplicate_index_keys(self) -> None:
        (self.root / "docs" / "index.json").write_text(
            '{"schema":"media-interlock.docs-index/v1",'
            '"schema":"media-interlock.docs-index/v1","documents":[]}',
            encoding="utf-8",
        )

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("duplicate JSON key" in error for error in errors))

    def test_rejects_broken_links_and_symlinked_markdown(self) -> None:
        self.write_markdown(
            "README.md",
            "# Entry\n\n[Missing](docs/current/missing.md)\n"
            "[External](//example.com/path)\n"
            "[Unsafe](javascript:alert)\n"
            "[Mail](mailto:security@example.com)\n"
            '<a href="docs/work/specs/private.md">Raw</a>\n',
        )
        self.write_markdown("real.md")
        (self.root / "linked.md").symlink_to(self.root / "real.md")
        self.write_index(
            [
                self.entry("README.md", "permanent"),
                self.entry("real.md", "permanent"),
                self.entry("linked.md", "permanent"),
            ]
        )

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("broken local link" in error for error in errors))
        self.assertTrue(any("Markdown must not be a symlink" in error for error in errors))
        self.assertTrue(any("external links require an explicit" in error for error in errors))
        self.assertTrue(any("unsupported link scheme" in error for error in errors))
        self.assertTrue(any("raw HTML links are unsupported" in error for error in errors))
        self.assertFalse(any("security@example.com" in error for error in errors))

    def test_rejects_each_reference_link_form(self) -> None:
        cases = {
            "full reference": "[Reference][missing]\n",
            "shortcut definition": "[missing]\n[missing]: docs/current/missing.md\n",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                errors = check_docs._link_policy_errors("README.md", content)

                self.assertTrue(
                    any("reference-style links are unsupported" in error for error in errors)
                )

    def test_rejects_relaxing_the_hard_ratchet(self) -> None:
        self.write_markdown("README.md")
        cases = [
            (("permanent_markdown", "max_files"), 13),
            (("permanent_markdown", "max_bytes"), 160 * 1024 + 1),
            (("work_markdown", "max_active_by_kind", "spec"), 2),
            (("work_markdown", "max_active_by_kind", "slice"), 2),
            (("work_markdown", "max_active_by_kind", "plan"), 2),
            (("work_markdown", "max_bytes_by_kind", "spec"), 64 * 1024 + 1),
            (("work_markdown", "max_bytes_by_kind", "slice"), 24 * 1024 + 1),
            (("work_markdown", "max_bytes_by_kind", "plan"), 32 * 1024 + 1),
        ]
        for keys, value in cases:
            with self.subTest(keys=keys):
                self.write_index([self.entry("README.md", "permanent")])
                index_path = self.root / "docs" / "index.json"
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                target = payload["budgets"]
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                index_path.write_text(json.dumps(payload), encoding="utf-8")

                errors = check_docs.audit_repository(self.root)

                self.assertTrue(any("exceeds hard ceiling" in error for error in errors))

    def test_rejects_stale_program_routing_and_class_path_mismatch(self) -> None:
        self.write_markdown("README.md")
        self.write_markdown("docs/work/specs/design.md")
        self.write_markdown("docs/work/slices/catalog.md")
        self.write_index(
            [
                self.entry("README.md", "permanent"),
                self.entry("docs/work/specs/design.md", "permanent"),
                self.entry("docs/work/slices/catalog.md", "work", kind="slice"),
            ],
            program={
                "id": "TEST",
                "status": "active",
                "specification": "docs/work/specs/missing.md",
                "slice_catalog": "docs/work/slices/catalog.md",
                "active_plan": None,
                "units": [],
            },
        )

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("class/path mismatch" in error for error in errors))
        self.assertTrue(any("program specification" in error for error in errors))

    def test_rejects_unknown_schema_keys(self) -> None:
        self.write_markdown("README.md")
        self.write_index([self.entry("README.md", "permanent")])
        index_path = self.root / "docs" / "index.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        payload["documents"][0]["load_whem"] = ["typo"]
        payload["project"] = {"name": [], "status": None}
        index_path.write_text(json.dumps(payload), encoding="utf-8")

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("unknown keys" in error for error in errors))
        self.assertTrue(any("project.name" in error for error in errors))
        self.assertTrue(any("project.status" in error for error in errors))

    def test_ignores_markdown_excluded_by_git(self) -> None:
        subprocess.run(
            ["git", "init", "-q"], cwd=self.root, check=True, capture_output=True
        )
        (self.root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        self.write_markdown("README.md")
        self.write_markdown(".venv/IGNORED.md")
        self.write_index([self.entry("README.md", "permanent")])

        self.assertEqual([], check_docs.audit_repository(self.root))

    def test_reports_a_markdown_directory_without_crashing(self) -> None:
        (self.root / "directory.md").mkdir()
        self.write_index([self.entry("directory.md", "permanent")])

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("regular file" in error for error in errors))

    def test_reports_an_index_directory_without_crashing(self) -> None:
        (self.root / "docs" / "index.json").mkdir()

        errors = check_docs.audit_repository(self.root)

        self.assertTrue(any("documentation index" in error for error in errors))

    def test_rejects_invalid_program_unit_sequences(self) -> None:
        self.write_markdown("docs/work/specs/design.md")
        self.write_markdown("docs/work/slices/catalog.md")
        documents = [
            self.entry("docs/work/specs/design.md", "work", kind="spec"),
            self.entry("docs/work/slices/catalog.md", "work", kind="slice"),
        ]
        cases = {
            "duplicate program unit ID": [
                {"id": "A", "status": "completed"},
                {"id": "A", "status": "pending"},
            ],
            "program unit order is invalid": [
                {"id": "A", "status": "pending"},
                {"id": "B", "status": "completed"},
            ],
            "at most one active program unit": [
                {"id": "A", "status": "in_progress"},
                {"id": "B", "status": "blocked"},
            ],
        }
        for expected, units in cases.items():
            with self.subTest(expected=expected):
                self.write_index(
                    documents,
                    program={
                        "id": "TEST",
                        "status": "active",
                        "specification": "docs/work/specs/design.md",
                        "slice_catalog": "docs/work/slices/catalog.md",
                        "active_plan": None,
                        "units": units,
                    },
                )

                errors = check_docs.audit_repository(self.root)

                self.assertTrue(any(expected in error for error in errors))


if __name__ == "__main__":
    unittest.main()
