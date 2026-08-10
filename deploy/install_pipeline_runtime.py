#!/usr/bin/env python3
"""Install one clean detached release for the unified ZSXQ pipeline.

This installer deliberately has no default production side effect: ``install``
requires ``--apply``, and retiring legacy schedulers additionally requires
``--cutover``.  It copies no credentials or runtime artifacts; configuration
is referenced in place and only its SHA-256 is recorded in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


class InstallError(RuntimeError):
    """An install precondition failed before a safe activation boundary."""


def _run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise InstallError(f"command failed: {Path(argv[0]).name}")
    return completed


def _absolute_existing(value: str, *, field: str, directory: bool = False) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{field} must be an absolute path")
    resolved = path.resolve(strict=True)
    if directory and not resolved.is_dir():
        raise InstallError(f"{field} must be a directory")
    if not directory and not resolved.is_file():
        raise InstallError(f"{field} must be a file")
    return resolved


def _absolute_path(value: str, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def _release_sha(release_root: Path) -> str:
    if _run(["git", "-C", str(release_root), "rev-parse", "--is-inside-work-tree"]).stdout.strip() != "true":
        raise InstallError("release root is not a Git worktree")
    if _run(["git", "-C", str(release_root), "status", "--porcelain"]).stdout.strip():
        raise InstallError("release checkout is dirty")
    symbolic = _run(["git", "-C", str(release_root), "symbolic-ref", "-q", "HEAD"], check=False)
    if symbolic.returncode == 0:
        raise InstallError("release checkout must be detached at a reviewed tag or SHA")
    sha = _run(["git", "-C", str(release_root), "rev-parse", "HEAD"]).stdout.strip().lower()
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise InstallError("release checkout did not resolve to a full commit SHA")
    return sha


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _configured_runtime_root(config: Path) -> Path:
    try:
        with config.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallError("pipeline configuration is not valid TOML") from exc
    runtime = payload.get("runtime") if isinstance(payload, dict) else None
    root = runtime.get("root") if isinstance(runtime, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise InstallError("pipeline configuration has no runtime.root")
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise InstallError("pipeline configuration runtime.root must be absolute")
    return candidate.resolve(strict=False)


def _copy_release(release_root: Path, destination: Path) -> None:
    if destination.exists():
        manifest = destination / "deployment-manifest.json"
        if not manifest.is_file():
            raise InstallError("release destination already exists without a completed deployment manifest")
        return
    ignored = shutil.ignore_patterns(".git", ".venv", ".pytest_cache", ".mypy_cache", "__pycache__", ".runtime")
    shutil.copytree(release_root, destination, ignore=ignored, symlinks=True)


def _runtime_lock_available(runtime_root: Path, *, create: bool = False) -> bool:
    # Import only from the release's dependency-free source tree.  The helper
    # has no repository or credential side effect and uses the same flock
    # semantics as the entrypoint itself.
    import fcntl

    if not runtime_root.exists():
        if not create:
            return True
        runtime_root.mkdir(parents=True, exist_ok=True)
    if not runtime_root.is_dir():
        return False
    lock_path = runtime_root / ".pipeline.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _legacy_runtime_busy(root: Path) -> bool:
    """Read, never delete, a legacy PID marker when an operator supplied it."""

    marker = root / ".run.pid"
    if not marker.is_file():
        return False
    try:
        pid = int(marker.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_crontab(command: str) -> str:
    completed = subprocess.run([command, "-l"], capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return completed.stdout
    if completed.returncode == 1 and "no crontab" in (completed.stderr or "").lower():
        return ""
    raise InstallError("unable to read the current user crontab")


def _validated_crontab_lines(values: list[str], current: str) -> list[str]:
    expected: list[str] = []
    for raw in values:
        line = str(raw).rstrip("\n")
        if not line.strip() or "\n" in line or "\r" in line:
            raise InstallError("--legacy-crontab-line must contain one non-empty exact line")
        if line in expected:
            raise InstallError("--legacy-crontab-line must not be repeated")
        expected.append(line)
    actual = current.splitlines()
    for line in expected:
        if actual.count(line) != 1:
            raise InstallError("an approved legacy crontab line is absent or duplicated")
    return expected


def _without_crontab_lines(current: str, expected: list[str]) -> str:
    filtered = [line for line in current.splitlines() if line not in set(expected)]
    return "\n".join(filtered) + ("\n" if filtered else "")


def _write_crontab(command: str, value: str) -> None:
    completed = subprocess.run([command, "-"], input=value, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise InstallError("unable to install the updated user crontab")


def _preflight_legacy(args: argparse.Namespace) -> tuple[list[Path], list[Path], list[Path], list[str], str]:
    plists = [_absolute_existing(value, field="--legacy-plist") for value in args.legacy_plist]
    cron_files = [_absolute_existing(value, field="--legacy-cron-file") for value in args.legacy_cron_file]
    wrappers = [_absolute_existing(value, field="--legacy-wrapper") for value in args.legacy_wrapper]
    runtime_roots = [_absolute_existing(value, field="--legacy-runtime-root", directory=True) for value in args.legacy_runtime_root]
    crontab = _read_crontab(args.crontab_command) if args.legacy_crontab_line else ""
    crontab_lines = _validated_crontab_lines(args.legacy_crontab_line, crontab)
    busy = [root for root in runtime_roots if _legacy_runtime_busy(root)]
    if busy:
        raise InstallError("a legacy runtime reports an active PID; wait for idle before cutover")
    if (plists or cron_files or wrappers or crontab_lines) and not args.cutover:
        raise InstallError("legacy scheduler files are still present; pass --cutover only for an approved production switch")
    return plists, cron_files, wrappers, crontab_lines, crontab


def _doctor(release: Path, config: Path, python: str) -> dict[str, Any]:
    environment = os.environ.copy()
    pythonpath = str(release / "src")
    if environment.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = pythonpath
    migrated = subprocess.run(
        [python, "-m", "zsxq_pipeline.cli", "db", "migrate", "--config", str(config)],
        cwd=str(release),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if migrated.returncode != 0:
        raise InstallError("release schema migration failed before activation")
    completed = subprocess.run(
        [python, "-m", "zsxq_pipeline.cli", "doctor", "--config", str(config)],
        cwd=str(release),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise InstallError("release doctor failed before activation")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError("release doctor returned invalid JSON") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise InstallError("release doctor reported an unhealthy runtime")
    return payload


def _command_available(command: str) -> bool:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or "/" in command:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def _manifest(
    *,
    sha: str,
    config: Path,
    python: str,
    doctor: dict[str, Any],
    previous_release: str | None,
) -> dict[str, Any]:
    checks = doctor.get("checks") if isinstance(doctor.get("checks"), dict) else {}
    source_checks = checks.get("sources") if isinstance(checks.get("sources"), dict) else {}
    cft_available = all(
        bool(item.get("cft_pair_valid")) and bool(item.get("cft_executable_available"))
        for item in source_checks.values()
        if isinstance(item, dict) and item.get("scheduled")
    )
    return {
        "schema_version": 1,
        "release_sha": sha,
        "pipeline_schema_version": doctor.get("schema_version"),
        "installed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python": str(python),
        "config_sha256": _sha256(config),
        "previous_release": previous_release,
        "capabilities": {
            "playwright": bool(shutil.which("playwright")),
            "codex": bool(checks.get("codex_command_available")),
            "lark_cli": bool(checks.get("lark_command_available")),
            "cft": cft_available,
        },
        "migration_debt": list(doctor.get("migration_debt") or []),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_symlink(destination: Path, target: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, destination)


def _render_plist(release: Path, *, label: str, runtime_root: Path, config: Path, python: str, home: str) -> bytes:
    template = release / "deploy" / "launchd" / "zsxq-pipeline.plist.template"
    if not template.is_file():
        raise InstallError("unified LaunchAgent template is missing from release")
    values = {
        "__LABEL__": label,
        "__PYTHON__": python,
        "__RELEASE_CURRENT__": str(runtime_root / "current"),
        "__CONFIG_PATH__": str(config),
        "__RUNTIME_ROOT__": str(runtime_root),
        "__HOME__": home,
        "__PATH__": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "__LOG_DIR__": str(runtime_root / "logs"),
    }
    rendered = template.read_text(encoding="utf-8")
    for marker, value in values.items():
        rendered = rendered.replace(marker, escape(value))
    try:
        plistlib.loads(rendered.encode("utf-8"))
    except Exception as exc:
        raise InstallError("rendered LaunchAgent plist is invalid") from exc
    return rendered.encode("utf-8")


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _backup(paths: list[Path], destination: Path) -> list[str]:
    copied: list[str] = []
    for index, path in enumerate(paths, start=1):
        if not path.exists():
            continue
        target = destination / f"legacy-{index:03d}-{path.name}"
        shutil.copy2(path, target)
        copied.append(target.name)
    return copied


def _reload_launchd(plist: Path, *, label: str) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, text=True, check=False)
    loaded = _run(["launchctl", "bootstrap", domain, str(plist)])
    if loaded.returncode != 0:  # pragma: no cover - _run raises first, kept for clarity
        raise InstallError("launchd bootstrap failed")


def _bootout_legacy(plists: list[Path]) -> list[Path]:
    domain = f"gui/{os.getuid()}"
    unloaded: list[Path] = []
    for plist in plists:
        completed = subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise InstallError("unable to unload an approved legacy LaunchAgent")
        unloaded.append(plist)
    return unloaded


def _restore_legacy_launchd(plists: list[Path]) -> None:
    domain = f"gui/{os.getuid()}"
    for plist in plists:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True, check=False)


def _install(args: argparse.Namespace) -> dict[str, Any]:
    release_root = _absolute_existing(args.release_root, field="--release-root", directory=True)
    runtime_root = _absolute_path(args.runtime_root, field="--runtime-root")
    config = _absolute_existing(args.config, field="--config")
    if _is_within(config, release_root):
        raise InstallError("--config must remain outside the release checkout")
    if _configured_runtime_root(config) != runtime_root:
        raise InstallError("--runtime-root must match pipeline configuration runtime.root")
    sha = _release_sha(release_root)
    plists, cron_files, wrappers, crontab_lines, original_crontab = _preflight_legacy(args)
    if not _runtime_lock_available(runtime_root, create=bool(args.apply)):
        raise InstallError("unified pipeline runtime is active; wait for idle before switching")
    release = runtime_root / "releases" / sha
    current = runtime_root / "current"
    previous_release = os.readlink(current) if current.is_symlink() else None
    if not args.apply:
        return {"planned": True, "release_sha": sha, "cutover": bool(args.cutover), "activated": False}

    if args.skip_launchd and args.cutover:
        raise InstallError("--skip-launchd cannot be combined with --cutover")
    _copy_release(release_root, release)
    doctor = _doctor(release, config, args.python)
    # Recheck after potentially long copy/doctor work, before the irreversible
    # entrypoint switch.  ``flock`` itself remains held only for this check.
    if not _runtime_lock_available(runtime_root, create=True):
        raise InstallError("unified pipeline became active during pre-activation checks")
    backup_root = runtime_root / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_names = _backup([*plists, *cron_files, *wrappers], backup_root)
    if crontab_lines:
        crontab_backup = backup_root / "user-crontab.before"
        _write_bytes(crontab_backup, original_crontab.encode("utf-8"))
        crontab_backup.chmod(0o600)
        backup_names.append(crontab_backup.name)
    unloaded_legacy: list[Path] = []
    disabled_crons: list[tuple[Path, Path]] = []
    crontab_attempted = False
    runtime_manifest_path = runtime_root / "deployment-manifest.json"
    previous_manifest = runtime_manifest_path.read_bytes() if runtime_manifest_path.is_file() else None
    manifest = _manifest(
        sha=sha,
        config=config,
        python=args.python,
        doctor=doctor,
        previous_release=previous_release,
    )
    _write_json(release / "deployment-manifest.json", manifest)
    plist_path = _absolute_path(args.launch_agents_dir, field="--launch-agents-dir") / f"{args.label}.plist"
    if not args.skip_launchd:
        rendered = _render_plist(
            release,
            label=args.label,
            runtime_root=runtime_root,
            config=config,
            python=args.python,
            home=args.home,
        )
        _write_bytes(plist_path, rendered)
    try:
        if args.cutover and plists and not args.skip_launchd:
            unloaded_legacy = _bootout_legacy(plists)
        if args.cutover and crontab_lines:
            if _read_crontab(args.crontab_command) != original_crontab:
                raise InstallError("user crontab changed after cutover preflight")
            crontab_attempted = True
            _write_crontab(
                args.crontab_command,
                _without_crontab_lines(original_crontab, crontab_lines),
            )
            active_lines = _read_crontab(args.crontab_command).splitlines()
            if any(line in active_lines for line in crontab_lines):
                raise InstallError("a legacy crontab line remained active after retirement")
        if args.cutover:
            for cron in cron_files:
                disabled = cron.with_name(f"{cron.name}.disabled-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
                os.replace(cron, disabled)
                disabled_crons.append((cron, disabled))
        _write_json(runtime_manifest_path, manifest)
        _atomic_symlink(current, release)
        if not args.skip_launchd:
            _reload_launchd(plist_path, label=args.label)
    except Exception:
        if previous_release:
            _atomic_symlink(current, Path(previous_release))
        elif current.is_symlink() or current.is_file():
            current.unlink(missing_ok=True)
        for original, disabled in reversed(disabled_crons):
            if disabled.exists():
                os.replace(disabled, original)
        if crontab_attempted:
            try:
                _write_crontab(args.crontab_command, original_crontab)
            except InstallError:
                pass
        if unloaded_legacy and not args.skip_launchd:
            _restore_legacy_launchd(unloaded_legacy)
        if previous_manifest is not None:
            _write_bytes(runtime_manifest_path, previous_manifest)
        else:
            runtime_manifest_path.unlink(missing_ok=True)
        raise
    return {
        "planned": False,
        "release_sha": sha,
        "cutover": bool(args.cutover),
        "activated": True,
        "legacy_backup_count": len(backup_names),
    }


def _rollback(args: argparse.Namespace) -> dict[str, Any]:
    runtime_root = _absolute_path(args.runtime_root, field="--runtime-root")
    manifest_path = runtime_root / "deployment-manifest.json"
    if not manifest_path.is_file():
        raise InstallError("no deployment manifest exists to roll back")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("deployment manifest is invalid") from exc
    previous = str(manifest.get("previous_release") or "").strip()
    if not previous:
        raise InstallError("deployment manifest has no prior release for rollback")
    target = Path(previous)
    if not target.is_absolute():
        target = (runtime_root / target).resolve(strict=False)
    if not target.is_dir():
        raise InstallError("previous release is unavailable; refusing partial rollback")
    if not _runtime_lock_available(runtime_root, create=bool(args.apply)):
        raise InstallError("unified pipeline runtime is active; wait for idle before rollback")
    if not args.apply:
        return {"planned": True, "activated": False, "rollback_to": target.name}
    _atomic_symlink(runtime_root / "current", target)
    if not args.skip_launchd:
        plist = _absolute_path(args.launch_agents_dir, field="--launch-agents-dir") / f"{args.label}.plist"
        _reload_launchd(plist, label=args.label)
    return {"planned": False, "activated": True, "rollback_to": target.name}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install-pipeline-runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install", help="Copy and activate a clean detached release after doctor passes.")
    install.add_argument("--release-root", required=True)
    install.add_argument("--runtime-root", required=True)
    install.add_argument("--config", required=True)
    install.add_argument("--launch-agents-dir", default=str(Path.home() / "Library" / "LaunchAgents"))
    install.add_argument("--label", default="com.zsxq-research-automation.pipeline")
    install.add_argument("--python", default=sys.executable)
    install.add_argument("--home", default=str(Path.home()))
    install.add_argument("--legacy-plist", action="append", default=[])
    install.add_argument("--legacy-cron-file", action="append", default=[])
    install.add_argument("--legacy-crontab-line", action="append", default=[])
    install.add_argument("--crontab-command", default="crontab")
    install.add_argument("--legacy-wrapper", action="append", default=[])
    install.add_argument("--legacy-runtime-root", action="append", default=[])
    install.add_argument("--cutover", action="store_true", help="Approved switch only: unload supplied old agents after backup.")
    install.add_argument("--skip-launchd", action="store_true", help="Test-only code installation; does not activate a scheduler.")
    install.add_argument("--apply", action="store_true", help="Required before release copy, migration, or activation.")

    rollback = commands.add_parser("rollback", help="Atomically restore the prior release entrypoint only.")
    rollback.add_argument("--runtime-root", required=True)
    rollback.add_argument("--launch-agents-dir", default=str(Path.home() / "Library" / "LaunchAgents"))
    rollback.add_argument("--label", default="com.zsxq-research-automation.pipeline")
    rollback.add_argument("--skip-launchd", action="store_true", help="Test-only code rollback; does not reload a scheduler.")
    rollback.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _install(args) if args.command == "install" else _rollback(args)
    except InstallError as exc:
        print(f"install-pipeline-runtime: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
