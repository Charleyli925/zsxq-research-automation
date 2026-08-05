#!/usr/bin/env python3
"""Guarded local branch, verification, and Draft-PR workflow.

The script automates repeatable Git mechanics while retaining two human gates:
PR review/merge and release deployment.  It never stages every file blindly;
``publish`` requires an explicit, exact file list.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


class WorkflowError(RuntimeError):
    """A safe workflow precondition was not met."""


BRANCH_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
    announce: bool = True,
) -> subprocess.CompletedProcess[str]:
    if announce:
        print("+ " + shlex.join([str(item) for item in command]), flush=True)
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        details = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        raise WorkflowError(
            f"command failed ({completed.returncode}): {shlex.join([str(item) for item in command])}\n{details}"
        )
    return completed


def resolve_repo(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    completed = run_command(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=candidate,
        capture=True,
        announce=False,
    )
    return Path(completed.stdout.strip()).resolve()


def git(repo: Path, *args: str, capture: bool = False, check: bool = True, announce: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], cwd=repo, capture=capture, check=check, announce=announce)


def git_output(repo: Path, *args: str) -> str:
    return git(repo, *args, capture=True, announce=False).stdout.strip()


def git_null_paths(repo: Path, *args: str) -> set[str]:
    completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if completed.returncode != 0:
        raise WorkflowError(f"could not inspect changed files: {shlex.join(['git', *args])}")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    }


def current_branch(repo: Path) -> str:
    branch = git_output(repo, "branch", "--show-current")
    if not branch:
        raise WorkflowError("HEAD is detached; switch to a named branch before using this workflow")
    return branch


def require_clean_worktree(repo: Path) -> None:
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=all", capture=True, announce=False).stdout
    if status.strip():
        raise WorkflowError(
            "working tree is not clean. Commit, stash, or explicitly resolve these files before continuing:\n"
            + status.rstrip()
        )


def default_branch(repo: Path, configured: str | None) -> str:
    if configured:
        return configured
    result = git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", capture=True, check=False, announce=False)
    if result.returncode == 0 and result.stdout.strip().startswith("refs/remotes/origin/"):
        return result.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def ensure_slug(slug: str) -> str:
    normalized = slug.strip().lower()
    if not BRANCH_SLUG.fullmatch(normalized):
        raise WorkflowError("slug must use lowercase letters, digits, and hyphens (2-64 characters)")
    return normalized


def branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False, announce=False).returncode == 0


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False, announce=False).returncode == 0


def remote_branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False, announce=False).returncode == 0


def normalize_include_path(raw_path: str) -> str:
    value = raw_path.strip().replace(os.sep, "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkflowError(f"--include must be a repository-relative file path: {raw_path!r}")
    return path.as_posix()


def validate_exact_scope(repo: Path, raw_paths: Iterable[str]) -> list[str]:
    """Require the requested paths to match the complete non-ignored change set."""
    requested = {normalize_include_path(path) for path in raw_paths}
    if not requested:
        raise WorkflowError("publish needs at least one --include path")

    staged = git_null_paths(repo, "diff", "--cached", "--name-only", "-z")
    if staged:
        raise WorkflowError(
            "the index already contains staged changes; unstage/review them before using publish:\n"
            + "\n".join(sorted(staged))
        )

    changed = git_null_paths(repo, "diff", "--name-only", "-z")
    changed |= git_null_paths(repo, "ls-files", "--others", "--exclude-standard", "-z")
    unselected = changed - requested
    missing = requested - changed
    if unselected or missing:
        messages: list[str] = ["--include must exactly match every non-ignored changed file."]
        if unselected:
            messages.append("changed but not selected:\n" + "\n".join(sorted(unselected)))
        if missing:
            messages.append("selected but not changed:\n" + "\n".join(sorted(missing)))
        raise WorkflowError("\n".join(messages))

    for path in sorted(requested):
        ignored = git(repo, "check-ignore", "-q", "--", path, check=False, announce=False).returncode == 0
        if ignored:
            raise WorkflowError(f"refusing to stage ignored local file: {path}")
    return sorted(requested)


def chosen_python(repo: Path, configured: str | None) -> str:
    if configured:
        return configured
    virtualenv_python = repo / ".venv" / "bin" / "python"
    if virtualenv_python.is_file() and os.access(virtualenv_python, os.X_OK):
        return str(virtualenv_python)
    return sys.executable


def select_test_targets(repo: Path, changed_paths: Iterable[str]) -> list[str]:
    """Choose the smallest relevant test set; CI remains the full-suite gate."""
    changed = set(changed_paths)
    explicit_tests = sorted(
        path for path in changed if path.startswith("tests/") and path.endswith(".py") and (repo / path).is_file()
    )
    if explicit_tests:
        return explicit_tests

    targets: set[str] = set()
    require_full_suite = False
    for path in changed:
        if path in {"scripts/git_workflow.py", "scripts/check_repository_hygiene.py", "AGENTS.md", "docs/development-workflow.md"}:
            targets.add("tests/test_git_workflow.py")
        elif path.startswith(("deploy/", "openclaw_tasks/zsxq_download/")):
            targets.add("tests/test_local_runtime_deployment.py")
        elif path.startswith("openclaw_tasks/zsxq_pdf_digest/"):
            targets.add("tests/test_zsxq_pdf_digest_run.py")
        elif path in {"scripts/run_zsxq_task_via_codex.sh", "scripts/run_zsxq_domestic_cicc_task_via_codex.sh"}:
            targets.add("tests/test_zsxq_download_launcher_runtime.py")
        elif path == "scripts/zsxq_autodownload_result.py":
            targets.add("tests/test_zsxq_autodownload_result.py")
        elif path.endswith((".py", ".sh")):
            candidate = f"tests/test_{Path(path).stem}.py"
            if (repo / candidate).is_file():
                targets.add(candidate)
            else:
                require_full_suite = True

    if require_full_suite:
        return ["-q"]
    return sorted(target for target in targets if (repo / target).is_file())


def run_local_checks(repo: Path, python_bin: str, changed_paths: Iterable[str]) -> None:
    shell_files = sorted(path for path in changed_paths if path.endswith(".sh") and (repo / path).is_file())
    if shell_files:
        run_command(["bash", "-n", *shell_files], cwd=repo)
    run_command([python_bin, "scripts/check_repository_hygiene.py"], cwd=repo)
    test_targets = select_test_targets(repo, changed_paths)
    if not test_targets:
        print("No behavior test is needed for this documentation-only change.")
        return
    if test_targets == ["-q"]:
        print("No focused test mapping is available; running the full suite.")
        run_command([python_bin, "-m", "pytest", "-q"], cwd=repo)
        return
    run_command([python_bin, "-m", "pytest", "-q", *test_targets], cwd=repo)


def repository_name(repo: Path) -> str:
    remote = git_output(repo, "remote", "get-url", "origin")
    match = re.search(r"github\.com[/:]([^/]+)/(.+?)(?:\.git)?$", remote)
    if not match:
        raise WorkflowError(f"cannot derive GitHub repository from origin remote: {remote}")
    return f"{match.group(1)}/{match.group(2)}"


def existing_pr(repo: Path, repo_name: str, branch: str) -> dict[str, object] | None:
    result = run_command(
        ["gh", "pr", "list", "--repo", repo_name, "--head", branch, "--state", "open", "--json", "number,url,isDraft"],
        cwd=repo,
        capture=True,
    )
    try:
        records = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"GitHub CLI returned invalid PR data: {exc}") from exc
    return records[0] if records else None


def begin(args: argparse.Namespace, repo: Path) -> None:
    require_clean_worktree(repo)
    base = default_branch(repo, args.base)
    desired_branch = f"codex/{ensure_slug(args.slug)}"
    git(repo, "fetch", "origin", "--prune")
    branch = current_branch(repo)
    if branch == desired_branch:
        print(f"Already on {desired_branch}; continuing this feature branch.")
        return
    if branch_exists(repo, desired_branch):
        raise WorkflowError(f"branch already exists: {desired_branch}; switch to it explicitly to resume its work")
    if branch != base:
        git(repo, "switch", base)
    git(repo, "pull", "--ff-only", "origin", base)
    git(repo, "switch", "-c", desired_branch)
    print(f"Created {desired_branch} from the latest origin/{base}.")


def sync_main(args: argparse.Namespace, repo: Path) -> None:
    require_clean_worktree(repo)
    base = default_branch(repo, args.base)
    git(repo, "fetch", "origin", "--prune")
    if current_branch(repo) != base:
        git(repo, "switch", base)
    git(repo, "pull", "--ff-only", "origin", base)
    if git_output(repo, "rev-parse", "HEAD") != git_output(repo, "rev-parse", f"origin/{base}"):
        raise WorkflowError(f"local {base} is not identical to origin/{base} after fast-forward sync")
    print(f"Local {base} is clean and current with origin/{base}.")


def publish(args: argparse.Namespace, repo: Path) -> None:
    branch = current_branch(repo)
    if not branch.startswith("codex/"):
        raise WorkflowError("publish only runs from a codex/* feature branch")
    base = default_branch(repo, args.base)
    git(repo, "fetch", "origin", "--prune")
    include_paths = validate_exact_scope(repo, args.include)

    python_bin = chosen_python(repo, args.python)
    if args.skip_checks:
        print("WARNING: local verification skipped by explicit --skip-checks.")
    else:
        run_local_checks(repo, python_bin, include_paths)

    git(repo, "add", "--", *include_paths)
    git(repo, "diff", "--cached", "--check")
    if git(repo, "diff", "--cached", "--quiet", check=False, announce=False).returncode == 0:
        raise WorkflowError("nothing was staged after validating the requested scope")
    git(repo, "commit", "-m", args.title)

    git(repo, "fetch", "origin", "--prune")
    rebased = False
    if not is_ancestor(repo, f"origin/{base}", "HEAD"):
        git(repo, "rebase", f"origin/{base}")
        rebased = True
    if rebased and not args.skip_checks:
        print("Rebase completed; re-running local verification against the latest base.")
        run_local_checks(repo, python_bin, include_paths)

    push_args = ("push", "-u", "origin", branch)
    if rebased and remote_branch_exists(repo, branch):
        # Rewriting a Draft PR after incorporating the latest main is safe only
        # with a lease: never overwrite remote work we did not fetch.
        push_args = ("push", "--force-with-lease", "-u", "origin", branch)
    git(repo, *push_args)
    run_command(["gh", "auth", "status"], cwd=repo)
    repo_name = repository_name(repo)
    pr = existing_pr(repo, repo_name, branch)
    if pr:
        print(f"Existing PR retained: {pr.get('url')} (draft={pr.get('isDraft')})")
        return

    body = (
        Path(args.body_file).read_text(encoding="utf-8")
        if args.body_file
        else "\n".join(
            [
                "## Summary",
                "",
                args.title,
                "",
                "## Validation",
                "",
                "- Focused local shell syntax, repository hygiene, and relevant pytest checks passed.",
                "- GitHub CI runs the complete test suite before human review and merge.",
                "",
                "## Review",
                "",
                "This is a Draft PR. Merge only after GitHub CI passes and a human review approves it.",
            ]
        )
    )
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo_name,
                "--base",
                base,
                "--head",
                branch,
                "--draft",
                "--title",
                args.title,
                "--body-file",
                str(body_path),
            ],
            cwd=repo,
        )
    finally:
        body_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded local Git and Draft-PR workflow.")
    parser.add_argument("--repo", default=".", help="repository directory (default: current directory)")
    parser.add_argument("--base", default=None, help="base branch (default: origin's default branch, or main)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin_parser = subparsers.add_parser("begin", help="sync the base branch and create/resume a codex/* branch")
    begin_parser.add_argument("--slug", required=True, help="feature slug; branch becomes codex/<slug>")

    subparsers.add_parser("sync-main", help="fast-forward the local base branch after an approved merge")

    publish_parser = subparsers.add_parser("publish", help="verify, exactly stage, commit, push, and open a Draft PR")
    publish_parser.add_argument("--title", required=True, help="commit and PR title")
    publish_parser.add_argument(
        "--include",
        action="append",
        required=True,
        help="one exact repository-relative changed file; repeat for every file",
    )
    publish_parser.add_argument("--body-file", help="optional Markdown file for the Draft PR body")
    publish_parser.add_argument("--python", help="Python interpreter for local checks (default: .venv/bin/python)")
    publish_parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="emergency escape hatch; normal development must not use this",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        repo = resolve_repo(Path(args.repo))
        if args.command == "begin":
            begin(args, repo)
        elif args.command == "sync-main":
            sync_main(args, repo)
        else:
            publish(args, repo)
    except WorkflowError as exc:
        print(f"git_workflow: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
