import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "skills/task-workflow/scripts/run_check.py"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "records"

    def invoke(self, command, options=()):
        return subprocess.run(
            [sys.executable, "-B", str(RUNNER), "--output-dir", str(self.logs),
             *options, "--", *command],
            cwd=self.root, capture_output=True, timeout=10,
        )

    def record(self):
        folders = list(self.logs.iterdir())
        self.assertEqual(len(folders), 1)
        folder = folders[0]
        return json.loads((folder / "result.json").read_text(encoding="utf-8")), folder

    def test_success_preserves_raw_stdout_and_stderr(self):
        command = [sys.executable, "-c", "import os; os.write(1, b'\\x00\\xffok'); os.write(2, b'err\\xfe')"]
        result = self.invoke(command)
        self.assertEqual(result.returncode, 0)
        metadata, folder = self.record()
        self.assertEqual(metadata["status"], "passed")
        self.assertEqual(metadata["command"], command)
        self.assertEqual(metadata["exit_code"], 0)
        self.assertEqual(metadata["child_returncode"], 0)
        self.assertGreaterEqual(metadata["duration_seconds"], 0)
        self.assertTrue(metadata["started_at"].endswith("+00:00"))
        self.assertTrue(metadata["finished_at"].endswith("+00:00"))
        self.assertEqual((folder / "stdout.log").read_bytes(), b"\x00\xffok")
        self.assertEqual((folder / "stderr.log").read_bytes(), b"err\xfe")

    def test_failure_keeps_original_exit_code_despite_success_text(self):
        result = self.invoke([sys.executable, "-c", "print('OK'); raise SystemExit(7)"])
        self.assertEqual(result.returncode, 7)
        metadata, _ = self.record()
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["child_returncode"], 7)
        self.assertEqual(metadata["exit_code"], 7)

    def test_status_is_not_inferred_from_output_text(self):
        result = self.invoke([sys.executable, "-c", "print('FAILED')"])
        self.assertEqual(result.returncode, 0)
        metadata, _ = self.record()
        self.assertEqual(metadata["status"], "passed")

    def test_timeout_records_partial_output_and_exit_124(self):
        command = [sys.executable, "-c", "import os,time; os.write(1,b'ready'); time.sleep(30)"]
        result = self.invoke(command, ("--timeout", "0.3"))
        self.assertEqual(result.returncode, 124)
        metadata, folder = self.record()
        self.assertEqual(metadata["status"], "timed_out")
        self.assertEqual(metadata["exit_code"], 124)
        self.assertNotEqual(metadata["child_returncode"], 0)
        self.assertEqual((folder / "stdout.log").read_bytes(), b"ready")

    def test_missing_executable_records_spawn_error(self):
        result = self.invoke([str(self.root / "not-an-executable")])
        self.assertEqual(result.returncode, 127)
        metadata, folder = self.record()
        self.assertEqual(metadata["status"], "spawn_error")
        self.assertIsNone(metadata["child_returncode"])
        self.assertEqual(metadata["exit_code"], 127)
        self.assertTrue((folder / "stderr.log").read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX executable permissions")
    def test_nonexecutable_file_returns_126(self):
        path = self.root / "not-executable"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o600)
        result = self.invoke([str(path)])
        self.assertEqual(result.returncode, 126)
        metadata, _ = self.record()
        self.assertEqual(metadata["status"], "spawn_error")

    def test_arguments_are_not_interpreted_by_a_shell(self):
        literal = ["two words", "$(touch accidental-file)", ";", "&&", "*.txt", "$HOME"]
        result = self.invoke([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *literal])
        self.assertEqual(result.returncode, 0)
        _, folder = self.record()
        self.assertEqual(json.loads((folder / "stdout.log").read_bytes()), literal)
        self.assertFalse((self.root / "accidental-file").exists())

    def test_explicit_cwd_and_output_location(self):
        work = self.root / "task"
        work.mkdir()
        (work / "input.txt").write_text("fixture", encoding="utf-8")
        result = self.invoke(
            [sys.executable, "-c", "from pathlib import Path; print(Path('input.txt').read_text())"],
            ("--cwd", str(work)),
        )
        self.assertEqual(result.returncode, 0)
        metadata, folder = self.record()
        self.assertEqual(Path(metadata["cwd"]), work.resolve())
        self.assertEqual((folder / "stdout.log").read_text().strip(), "fixture")
        self.assertFalse((work / "records").exists())

    def test_repeated_labels_do_not_overwrite_logs(self):
        self.assertEqual(self.invoke([sys.executable, "-c", "print('first')"]).returncode, 0)
        original, folder = self.record()
        self.assertEqual(self.invoke([sys.executable, "-c", "raise SystemExit(3)"]).returncode, 3)
        self.assertEqual(len(list(self.logs.iterdir())), 2)
        self.assertEqual(json.loads((folder / "result.json").read_text()), original)
        self.assertEqual((folder / "stdout.log").read_bytes(), b"first\n")

    def test_invalid_options_fail_before_execution(self):
        marker = self.root / "should-not-exist"
        command = [sys.executable, "-c", "from pathlib import Path; Path('should-not-exist').touch()"]
        cases = [("--timeout", value) for value in ("0", "-1", "nan", "inf", "bad")]
        cases += [("--label", "../escape"), ("--cwd", str(self.root / "missing"))]
        for options in cases:
            with self.subTest(options=options):
                self.assertEqual(self.invoke(command, options).returncode, 2)
                self.assertFalse(marker.exists())
                self.assertFalse(self.logs.exists())

    def test_missing_command_or_unwritable_log_destination(self):
        self.assertEqual(self.invoke([]).returncode, 2)
        self.assertFalse(self.logs.exists())
        self.logs.write_text("a file blocks directory creation", encoding="utf-8")
        result = self.invoke([sys.executable, "-c", "raise SystemExit(0)"])
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr)

    def test_stdin_is_closed_for_noninteractive_commands(self):
        result = self.invoke([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"])
        self.assertEqual(result.returncode, 0)
        _, folder = self.record()
        self.assertEqual((folder / "stdout.log").read_bytes(), b"0\n")

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_child_signal_is_recorded_and_mapped(self):
        result = self.invoke([sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"])
        self.assertEqual(result.returncode, 143)
        metadata, _ = self.record()
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["child_returncode"], -signal.SIGTERM)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_timeout_stops_same_group_descendant(self):
        marker = self.root / "descendant-finished"
        descendant = "import time; from pathlib import Path; time.sleep(0.7); Path('descendant-finished').touch()"
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{descendant!r}]); time.sleep(30)"
        result = self.invoke([sys.executable, "-c", code], ("--timeout", "0.3"))
        self.assertEqual(result.returncode, 124)
        time.sleep(0.8)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX interrupt handling")
    def test_user_interrupt_records_130(self):
        process = subprocess.Popen(
            [sys.executable, "-B", str(RUNNER), "--output-dir", str(self.logs), "--",
             sys.executable, "-c", "import os,time; os.write(1,b'ready'); time.sleep(30)"],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            ready = False
            while time.monotonic() < deadline:
                files = list(self.logs.glob("*/stdout.log"))
                if files and files[0].read_bytes() == b"ready":
                    ready = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(ready, "child did not become ready")
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            metadata, _ = self.record()
            self.assertEqual(metadata["status"], "interrupted")
            self.assertNotEqual(metadata["child_returncode"], 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
