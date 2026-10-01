"""Local trusted-shell boundary checks; no Docker, dependency installation or SDK calls."""
from pathlib import Path
import hashlib
import json
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import quality


class QualityBoundaryTest(unittest.TestCase):
    def test_source_copy_excludes_private_git_but_preserves_application_dotfiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, work = root / "source", root / "work"
            source.mkdir(); work.mkdir()
            (source / ".git").mkdir()
            (source / ".git/private-metadata").write_text("synthetic metadata")
            (source / "one file.py").write_text("print('fixture')")
            (source / ".project-config").write_text("fixture=true")
            command = quality.docker_command(source, {"path": ".", "commands": [], "image": "unused"}, "unused", "unused")
            script = command[-1].replace("/source", shlex.quote(str(source))).replace("/work", shlex.quote(str(work)))
            script = script.replace("/tmp/home", shlex.quote(str(root / "home")))
            result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual({"one file.py", ".project-config"}, {p.name for p in work.iterdir()})

    def test_resolution_files_are_bounded_hashed_and_private(self):
        files = {name: '{}' for name in ('app-requirements.txt', 'overlay.in', 'resolved-environment.json')}
        files['provenance.json'] = json.dumps({'artifacts': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}})
        line = lambda data: 'RAILSHOT_PYTHON_ARTIFACTS=' + json.dumps(data)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = quality.save_python_artifacts(line(files), root, 0, required=True)
            self.assertEqual((Path(saved['directory']) / 'overlay.in').stat().st_mode & 0o777, 0o600)
            for bad in ({**files, '../escape': 'bad'}, {**files, 'overlay.in': 'changed'}):
                with self.assertRaises(ValueError):
                    quality.save_python_artifacts(line(bad), root, 1, required=True)
            for text in ('', line(files) + '\n' + line(files)):
                with self.assertRaises(ValueError):
                    quality.save_python_artifacts(text, root, 1, required=True)
            self.assertFalse((root / 'python-1').exists())

    def shell(self, commands, setup=""):
        return subprocess.run(["sh", "-c", quality.quality_script(commands, setup)], capture_output=True, text=True, timeout=5)

    def test_child_cannot_select_stage_through_output_or_reserved_exit_code(self):
        for stage, expected in quality.STAGE_CODES.items():
            for child_code in (1, 201, 202, 203, 204, 205):
                with self.subTest(stage=stage, child_code=child_code):
                    result = self.shell([(stage, f"sh -c 'printf \"RAILSHOT_STAGE=unit\\n\"; exit {child_code}'")])
                    self.assertEqual(expected, result.returncode)
                    failure = quality.quality_failure(result.stdout, result.returncode)
                    self.assertEqual(stage in {"lint", "type", "unit"}, failure.get("source_repair_eligible", False))

    def test_success_and_trusted_exports_survive_between_commands(self):
        result = self.shell([("prepare", "export RAILSHOT_BOUNDARY_VALUE=present"),
                             ("lint", "sh -c 'test \"$RAILSHOT_BOUNDARY_VALUE\" = present'"),
                             ("report", "printf 'RAILSHOT_TESTS=1\\n'")])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, quality.test_count(result.stdout, "python"))

    def test_setup_and_parent_builtin_failures_have_prepare_provenance(self):
        for setup in ("false", "cd /railshot-boundary-nonexistent-directory", "printf '%s' \"$RAILSHOT_UNSET_28C11\""):
            with self.subTest(setup=setup):
                result = self.shell([("lint", "true")], setup)
                self.assertEqual(201, result.returncode)
                self.assertTrue(quality.quality_failure(result.stderr, result.returncode).get("blocked"))

    def test_missing_command_no_tests_and_signals_never_become_repairs(self):
        for command in ("railshot_boundary_nonexistent_command", "sh -c 'exit 5'", "sh -c 'kill -TERM $$'", "kill -TERM $$"):
            with self.subTest(command=command):
                result = self.shell([("unit", command)])
                self.assertEqual(200, result.returncode)
                self.assertTrue(quality.quality_failure(result.stdout, result.returncode).get("blocked"))

    def test_only_platform_codes_are_stage_evidence(self):
        for code in (1, 2, 5, 125, 126, 127, 128, 137, 143, 200, 255, -9, -15):
            with self.subTest(code=code):
                failure = quality.quality_failure("RAILSHOT_STAGE=unit\nassertion failed", code)
                self.assertTrue(failure.get("blocked"))
                self.assertFalse(failure.get("source_repair_eligible", False))
        failure = quality.quality_failure("RAILSHOT_STAGE=prepare\nsrc/a.ts: error TS2322", 203)
        self.assertEqual("type", failure["check"])
        self.assertTrue(failure["source_repair_eligible"])

    def test_report_zero_tests_and_environment_failures_still_block(self):
        for text, code in (("RAILSHOT_STAGE=unit", 205), ("collected 0 items", 204),
                           ("ETIMEDOUT registry", 202), ("API_KEY required", 204),
                           ("Failed to validate Maven distribution SHA", 204),
                           ("Dependency verification failed for configuration", 204),
                           ("sh: railshot_checker_not_installed: not found", 202),
                           ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package vite", 204), ("", 0)):
            with self.subTest(text=text, code=code):
                self.assertTrue(quality.quality_failure(text, code).get("blocked"))

    def test_fingerprint_keeps_diagnostics_project_and_checker_distinct(self):
        failure = quality.quality_failure('RAILSHOT_PYTHON_PROVENANCE={"sha":"a047429bef"}\nunused import os', 202)
        self.assertTrue(failure['source_repair_eligible'])
        self.assertTrue(quality.quality_failure('HTTPError: 429 Too Many Requests', 202).get('blocked'))
        first = quality.quality_failure("src/a.ts:10:2: undefined variable foo", 202, "frontend")
        different = quality.quality_failure("src/b.ts:11:3: unused import bar", 202, "frontend")
        other_project = quality.quality_failure("src/a.ts:10:2: undefined variable foo", 202, "backend")
        other_checker = quality.quality_failure("src/a.ts:10:2: undefined variable foo", 203, "frontend")
        self.assertEqual(4, len({item["failure_signature"] for item in (first, different, other_project, other_checker)}))

    def test_fingerprint_normalizes_color_line_numbers_whitespace_and_duration(self):
        first = quality.quality_failure("\x1b[31msrc/a.ts:10:2: error TS2322\x1b[0m\nfinished in 1.2s", 203, "frontend")
        second = quality.quality_failure("RAILSHOT_STAGE=lint\nsrc/a.ts:40:5:  error TS2322\nfinished  in 9.8s", 203, "frontend")
        self.assertEqual(first["failure_signature"], second["failure_signature"])
        first = quality.diagnostic_fingerprint('"startTime":179000000,"duration":2.3; time="0.23" AssertionError expected ready', 'unit', '.')
        second = quality.diagnostic_fingerprint('"startTime":179000400,"duration":7.9; time="1.21" AssertionError expected ready', 'unit', '.')
        self.assertEqual(first, second)

    def test_run_quality_exposes_signature_and_project_fingerprint(self):
        plan = {"path": "frontend", "stack": "javascript", "image": "node:22", "commands": ["npm run lint"]}
        def execute(args, **kwargs):
            if args[:2] == ["docker", "rm"]:
                return SimpleNamespace(returncode=0)
            kwargs["stdout"].write(b"RAILSHOT_STAGE=unit\nsrc/a.js:2: lint error\n")
            return SimpleNamespace(returncode=202)
        with tempfile.TemporaryDirectory() as directory, patch.object(quality, "discover", return_value=[plan]), \
                patch.object(quality.subprocess, "run", side_effect=execute):
            result = quality.run_quality(Path(directory), Path(directory) / "run", network="trusted-quality")
        self.assertEqual("lint", result["check"])
        self.assertTrue(result["failure_signature"].startswith("Q:QUALITY:lint:"))
        self.assertEqual(result["failure_signature"].rsplit(":", 1)[-1], result["projects"][0]["fingerprint"])


if __name__ == "__main__":
    unittest.main()
