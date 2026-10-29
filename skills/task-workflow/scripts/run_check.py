#!/usr/bin/env python3
"""Record an explicitly chosen non-interactive command without a shell or network client."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


def positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return seconds


def label_value(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("label must be 1-64 letters, digits, underscores or hyphens")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def stop_process(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait()


def run(args, command, cwd):
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=args.label + "-", dir=output_root))
    started = time.monotonic()
    record = {
        "schema_version": 1,
        "label": args.label,
        "command": command,
        "cwd": str(cwd),
        "started_at": utc_now(),
        "timeout_seconds": args.timeout,
        "status": "not_started",
        "child_returncode": None,
        "exit_code": None,
    }
    with (run_dir / "stdout.log").open("wb") as stdout, (run_dir / "stderr.log").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            record["status"] = "spawn_error"
            record["exit_code"] = 127 if isinstance(exc, FileNotFoundError) else 126
            stderr.write((str(exc) + "\n").encode("utf-8", errors="replace"))
        else:
            try:
                code = process.wait(timeout=args.timeout)
                record["status"] = "passed" if code == 0 else "failed"
                record["exit_code"] = code if code >= 0 else 128 - code
            except subprocess.TimeoutExpired:
                stop_process(process)
                record["status"] = "timed_out"
                record["exit_code"] = 124
            except KeyboardInterrupt:
                stop_process(process)
                record["status"] = "interrupted"
                record["exit_code"] = 130
            record["child_returncode"] = process.returncode

    record["finished_at"] = utc_now()
    record["duration_seconds"] = round(time.monotonic() - started, 6)
    (run_dir / "result.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{record['status']}: exit={record['exit_code']} logs={run_dir}")
    return record["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", type=label_value, default="check")
    parser.add_argument("--timeout", type=positive_seconds, default=120.0)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", default="artifacts/checks")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --")
    cwd = args.cwd.resolve()
    if not cwd.is_dir():
        parser.error("--cwd must be an existing directory")
    try:
        return run(args, command, cwd)
    except OSError as exc:
        print(f"Log or execution error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
