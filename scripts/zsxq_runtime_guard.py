#!/usr/bin/env python3
"""Process deadline and ownership-aware lock helpers for ZSXQ launchers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


BUSY_EXIT_CODE = 23
TIMEOUT_EXIT_CODE = 124
OWNER_FILENAME = "owner.json"


def now_local() -> str:
    return datetime.now().astimezone().isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_new_json(path: Path, payload: dict[str, Any]) -> None:
    """Create metadata inside a newly-owned lock without a temp-file race."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def process_start_signature(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def process_is_owner(owner: dict[str, Any]) -> bool:
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    expected_start = str(owner.get("process_start") or "").strip()
    if not expected_start:
        return True
    current_start = process_start_signature(pid)
    return bool(current_start) and current_start == expected_start


def lock_age_seconds(lock_dir: Path) -> float:
    try:
        return max(0.0, time.time() - lock_dir.stat().st_mtime)
    except OSError:
        return 0.0


def known_lock_contents(lock_dir: Path) -> bool:
    try:
        return {item.name for item in lock_dir.iterdir()} <= {OWNER_FILENAME}
    except OSError:
        return False


def is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def remove_known_lock_dir(lock_dir: Path) -> None:
    (lock_dir / OWNER_FILENAME).unlink(missing_ok=True)
    lock_dir.rmdir()


def acquire_lock(args: argparse.Namespace) -> int:
    lock_dir = Path(args.lock_dir).expanduser()
    owner_path = lock_dir / OWNER_FILENAME
    recovered = False

    for _ in range(3):
        try:
            lock_dir.mkdir(mode=0o700, parents=False)
        except FileExistsError:
            if not is_real_directory(lock_dir):
                print(
                    json.dumps(
                        {"status": "unsafe_lock_type", "lock_dir": str(lock_dir)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 2
            owner = load_json(owner_path)
            if owner and process_is_owner(owner):
                print(
                    json.dumps(
                        {"status": "active", "lock_dir": str(lock_dir), "owner": owner},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return BUSY_EXIT_CODE

            # A missing/corrupt owner file may be the tiny mkdir->metadata race.
            # Only reclaim that legacy shape after a configurable grace period.
            if not owner and lock_age_seconds(lock_dir) < args.stale_seconds:
                print(
                    json.dumps(
                        {"status": "active_unowned_grace", "lock_dir": str(lock_dir)},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return BUSY_EXIT_CODE
            if not known_lock_contents(lock_dir):
                print(
                    json.dumps(
                        {"status": "unsafe_lock_contents", "lock_dir": str(lock_dir)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 2

            suffix = hashlib.sha256(args.token.encode("utf-8")).hexdigest()[:12]
            quarantine = lock_dir.with_name(f"{lock_dir.name}.stale.{suffix}")
            try:
                os.replace(lock_dir, quarantine)
            except FileNotFoundError:
                continue
            except FileExistsError:
                continue
            try:
                remove_known_lock_dir(quarantine)
            except OSError as exc:
                print(
                    json.dumps(
                        {
                            "status": "stale_lock_cleanup_failed",
                            "lock_dir": str(lock_dir),
                            "quarantine": str(quarantine),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 2
            recovered = True
            continue
        except OSError as exc:
            print(
                json.dumps(
                    {"status": "lock_create_failed", "lock_dir": str(lock_dir), "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2

        owner = {
            "schema_version": 1,
            "token": args.token,
            "run_id": args.run_id,
            "pid": args.owner_pid,
            "process_start": process_start_signature(args.owner_pid),
            "created_at": now_local(),
            "hostname": socket.gethostname(),
            "task": args.task,
        }
        try:
            write_new_json(owner_path, owner)
        except Exception as exc:
            try:
                remove_known_lock_dir(lock_dir)
            except OSError:
                pass
            print(
                json.dumps(
                    {"status": "lock_metadata_failed", "lock_dir": str(lock_dir), "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                {"status": "acquired", "lock_dir": str(lock_dir), "recovered_stale": recovered, "owner": owner},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    print(
        json.dumps(
            {"status": "lock_race_exhausted", "lock_dir": str(lock_dir)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return BUSY_EXIT_CODE


def release_lock(args: argparse.Namespace) -> int:
    lock_dir = Path(args.lock_dir).expanduser()
    owner_path = lock_dir / OWNER_FILENAME
    try:
        lock_dir.lstat()
    except FileNotFoundError:
        print(json.dumps({"status": "already_missing", "lock_dir": str(lock_dir)}, sort_keys=True))
        return 0
    except OSError as exc:
        print(
            json.dumps(
                {"status": "lock_stat_failed", "lock_dir": str(lock_dir), "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    if not is_real_directory(lock_dir):
        print(
            json.dumps(
                {"status": "unsafe_lock_type", "lock_dir": str(lock_dir)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    owner = load_json(owner_path)
    if str(owner.get("token") or "") != args.token:
        print(
            json.dumps(
                {"status": "not_owner", "lock_dir": str(lock_dir), "owner": owner},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    if not known_lock_contents(lock_dir):
        print(
            json.dumps(
                {"status": "unsafe_lock_contents", "lock_dir": str(lock_dir)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    try:
        remove_known_lock_dir(lock_dir)
    except OSError as exc:
        print(
            json.dumps(
                {"status": "release_failed", "lock_dir": str(lock_dir), "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    print(json.dumps({"status": "released", "lock_dir": str(lock_dir)}, sort_keys=True))
    return 0


def semantic_version(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def codex_version(codex_bin: Path) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            [str(codex_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    version = semantic_version(output)
    if completed.returncode != 0 or not version:
        return "", output or f"codex --version exited {completed.returncode}"
    return version, ""


def prepare_model_cache(args: argparse.Namespace) -> int:
    """Quarantine only a cache proven incompatible with the selected CLI."""
    codex_bin = Path(args.codex_bin).expanduser()
    cache_path = Path(args.cache_file).expanduser()
    cli_version, error = codex_version(codex_bin)
    if not cli_version:
        print(
            json.dumps(
                {
                    "status": "codex_version_unavailable",
                    "codex_bin": str(codex_bin),
                    "error": error,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    for _ in range(3):
        try:
            before = cache_path.stat()
        except FileNotFoundError:
            print(
                json.dumps(
                    {
                        "status": "cache_missing",
                        "codex_version": cli_version,
                        "cache_file": str(cache_path),
                    },
                    sort_keys=True,
                )
            )
            return 0
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "status": "cache_stat_failed",
                        "cache_file": str(cache_path),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2

        payload = load_json(cache_path)
        cache_version = semantic_version(str(payload.get("client_version") or ""))
        if not cache_version:
            # Unknown/corrupt files are left untouched; this helper only acts on a
            # positively identified version mismatch.
            print(
                json.dumps(
                    {
                        "status": "cache_version_unknown",
                        "codex_version": cli_version,
                        "cache_file": str(cache_path),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if cache_version == cli_version:
            print(
                json.dumps(
                    {
                        "status": "compatible",
                        "codex_version": cli_version,
                        "cache_version": cache_version,
                        "cache_file": str(cache_path),
                    },
                    sort_keys=True,
                )
            )
            return 0

        try:
            after = cache_path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "status": "cache_recheck_failed",
                        "cache_file": str(cache_path),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            continue

        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        quarantine = cache_path.with_name(
            f"{cache_path.name}.incompatible.{cache_version}.for-{cli_version}.{timestamp}.{os.getpid()}"
        )
        try:
            os.replace(cache_path, quarantine)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "status": "cache_quarantine_failed",
                        "cache_file": str(cache_path),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "quarantined_incompatible",
                    "codex_version": cli_version,
                    "cache_version": cache_version,
                    "cache_file": str(cache_path),
                    "quarantine": str(quarantine),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "status": "cache_changed_during_check",
                "cache_file": str(cache_path),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 2


def write_bytes(data: bytes) -> None:
    if not data:
        return
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def pump_output(selector: selectors.BaseSelector, timeout: float) -> None:
    for key, _ in selector.select(max(0.0, timeout)):
        try:
            chunk = os.read(key.fd, 65536)
        except OSError:
            chunk = b""
        if chunk:
            write_bytes(chunk)
        else:
            try:
                selector.unregister(key.fd)
            except Exception:
                pass


def terminate_group(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def execute_with_timeout(args: argparse.Namespace) -> int:
    command: list[str] = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("missing command after --", file=sys.stderr)
        return 2

    stdin_handle = open(args.stdin_file, "rb") if args.stdin_file else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        if stdin_handle is not subprocess.DEVNULL:
            stdin_handle.close()
        print(f"failed to start command: {exc}", file=sys.stderr)
        return 127

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout.fileno(), selectors.EVENT_READ)
    received_signal: list[int] = []

    def forward_signal(signum: int, _frame: Any) -> None:
        received_signal.append(signum)
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward_signal)

    timed_out = False
    deadline = time.monotonic() + args.timeout_seconds
    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                marker = {
                    "timeout_seconds": args.timeout_seconds,
                    "terminate_grace_seconds": args.terminate_grace_seconds,
                    "pid": process.pid,
                }
                print(
                    f"ZSXQ_EXEC_TIMEOUT_JSON:{json.dumps(marker, sort_keys=True)}",
                    flush=True,
                )
                terminate_group(process, args.terminate_grace_seconds)
                break
            pump_output(selector, min(0.5, remaining))
            if received_signal:
                terminate_group(process, args.terminate_grace_seconds)
                break

        # Drain any final buffered bytes after normal exit or termination.
        drain_deadline = time.monotonic() + 2.0
        while selector.get_map() and time.monotonic() < drain_deadline:
            pump_output(selector, 0.05)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            terminate_group(process, 0)
            process.wait(timeout=1)
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        selector.close()
        process.stdout.close()
        if stdin_handle is not subprocess.DEVNULL:
            stdin_handle.close()

    if timed_out:
        return TIMEOUT_EXIT_CODE
    if received_signal:
        return 128 + received_signal[-1]
    return int(process.returncode or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    acquire = subparsers.add_parser("lock-acquire")
    acquire.add_argument("--lock-dir", required=True)
    acquire.add_argument("--token", required=True)
    acquire.add_argument("--run-id", required=True)
    acquire.add_argument("--owner-pid", type=int, required=True)
    acquire.add_argument("--task", default="")
    acquire.add_argument("--stale-seconds", type=int, default=300)
    acquire.set_defaults(handler=acquire_lock)

    release = subparsers.add_parser("lock-release")
    release.add_argument("--lock-dir", required=True)
    release.add_argument("--token", required=True)
    release.set_defaults(handler=release_lock)

    cache = subparsers.add_parser("prepare-model-cache")
    cache.add_argument("--codex-bin", required=True)
    cache.add_argument("--cache-file", required=True)
    cache.set_defaults(handler=prepare_model_cache)

    execute = subparsers.add_parser("exec-timeout")
    execute.add_argument("--timeout-seconds", type=float, required=True)
    execute.add_argument("--terminate-grace-seconds", type=float, default=10)
    execute.add_argument("--stdin-file")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.set_defaults(handler=execute_with_timeout)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout_seconds", 1) <= 0:
        parser.error("--timeout-seconds must be positive")
    if getattr(args, "terminate_grace_seconds", 0) < 0:
        parser.error("--terminate-grace-seconds must not be negative")
    if getattr(args, "stale_seconds", 0) < 0:
        parser.error("--stale-seconds must not be negative")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
