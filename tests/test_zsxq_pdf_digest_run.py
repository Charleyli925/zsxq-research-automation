"""This file checks the summary task shell entry with a small fake runtime task dir.

Relation to other files:
- It runs `openclaw_tasks/zsxq_pdf_digest/run.sh` through a symlinked workspace folder.
- The test focuses on the path fallback added in `run.sh`.
- It uses the real helper script, but a tiny fake `extract_pdf_text.py` to keep the test stable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.sh"
RUN_WORKER_SCRIPT = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.worker.sh"
RUN_CRON_SAFE_SCRIPT = ROOT / "openclaw_tasks" / "zsxq_pdf_digest" / "run.cron-safe.sh"
HELPER_SCRIPT = ROOT / "scripts" / "manage_zsxq_digest_batch.py"
SCANNER_SCRIPT = ROOT / "scripts" / "scan_new_zsxq_pdfs.py"


class ZsxqPdfDigestRunTests(unittest.TestCase):
    def write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def make_executable(self, path: Path) -> None:
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IXUSR)

    def write_runtime_config(
        self,
        path: Path,
        *,
        watch_root: Path,
        download_task_dir: Path,
        quiet_window_minutes: int = 0,
        reset_agent_session: str = "false",
        summary_agent_timeout_seconds: int = 210,
        summary_timeout_retry_count: int = 1,
        lark_cli_notifications: str = "false",
        text_extract_retry_count: int = 0,
    ) -> None:
        lark_cli_config_dir = path.parent / "lark_cli_openclaw_config"
        self.write_file(
            path,
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                TARGET_CHAT_ID="test"
                WATCH_ROOT="{watch_root}"
                SCANNER_SCRIPT_PATH="{SCANNER_SCRIPT}"
                HELPER_SCRIPT_PATH="{HELPER_SCRIPT}"
                DOWNLOAD_TASK_DIR="{download_task_dir}"
                SUMMARY_AGENT_ID="zsxq_pdf_digest_test_summary"
                RESET_AGENT_SESSION_ON_RUN="{reset_agent_session}"
                LOG_FILE="cron.log"
                STATE_FILE="watch_state.json"
                BATCH_JSON="pending_batch.json"
                RESULT_JSON="last_result.json"
                RESULT_MD="last_result.md"
                RUN_STATUS_JSON="run_status.json"
                USAGE_JSON="last_usage_summary.json"
                NOTIFICATION_JSONL="notification_messages.jsonl"
                FAILURE_STATE_JSON="failure_backoff.json"
                PREFLIGHT_JSON="last_preflight.json"
                QUARANTINE_JSON="quarantine.json"
                QUARANTINE_REPORT_MD="quarantine_report.md"
                TEXT_CACHE_DIR="text_cache"
                SUMMARY_CACHE_DIR="summary_cache"
                PYTHON_BIN="{sys.executable}"
                SUMMARY_AGENT_TIMEOUT_SECONDS="{summary_agent_timeout_seconds}"
                SUMMARY_TIMEOUT_RETRY_COUNT="{summary_timeout_retry_count}"
                QUIET_WINDOW_MINUTES="{quiet_window_minutes}"
                BATCH_CHUNK_SIZE="1"
                DOC_GROUP_SIZE="10"
                DOC_GROUP_THRESHOLD="15"
                SEND_PROGRESS_EACH_FILE="false"
                LARK_CLI_NOTIFICATIONS="{lark_cli_notifications}"
                LARK_CLI_SEND_AS="bot"
                LARKSUITE_CLI_CONFIG_DIR="{lark_cli_config_dir}"
                PUBLISH_LARK_CLI_AS="user"
                OCR_TEXT_MAX_CHARS="120000"
                TEXT_EXTRACT_MAX_CHARS="120000"
                TEXT_EXTRACT_RETRY_COUNT="{text_extract_retry_count}"
                LOCAL_OCR_FALLBACK_ENABLE="false"
                CHUNK_RETRY_COUNT="0"
                AUTO_RETRY_MAX_SAME_BATCH="3"
                AUTO_RETRY_BASE_MINUTES="30"
                AUTO_RETRY_MAX_COOLDOWN_MINUTES="180"
                AUTO_RETRY_TRANSIENT_MAX_SAME_BATCH="4"
                AUTO_RETRY_TRANSIENT_BASE_MINUTES="5"
                AUTO_RETRY_TRANSIENT_MAX_COOLDOWN_MINUTES="20"
                """
            ),
        )

    def create_openclaw_stub(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        openclaw_path = bin_dir / "openclaw"
        self.write_file(
            openclaw_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                log_path = os.environ.get("OPENCLAW_STUB_LOG", "")
                if log_path:
                    path = Path(log_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"argv": sys.argv[1:]}, ensure_ascii=False) + "\\n")
                """
            ),
        )
        self.make_executable(openclaw_path)
        return openclaw_path

    def create_obsidian_index_stub(self, script_path: Path) -> Path:
        self.write_file(
            script_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path


                def flag_value(args, flag):
                    try:
                        return args[args.index(flag) + 1]
                    except (ValueError, IndexError):
                        return ""


                args = sys.argv[1:]
                record = {"argv": args}
                if "--rebuild-all" in args:
                    status_path = Path(os.environ["OBSIDIAN_INDEX_STUB_RUN_STATUS"])
                    record["run_status"] = json.loads(status_path.read_text(encoding="utf-8"))

                log_path = Path(os.environ["OBSIDIAN_INDEX_STUB_LOG"])
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\\n")

                if "--rebuild-all" in args and os.environ.get("OBSIDIAN_INDEX_STUB_FAIL_REBUILD") == "1":
                    print("stub full rebuild failed", file=sys.stderr)
                    raise SystemExit(7)

                result_path = flag_value(args, "--result-file")
                if result_path:
                    path = Path(result_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"ok": True}) + "\\n", encoding="utf-8")
                print(json.dumps({"ok": True}))
                """
            ),
        )
        self.make_executable(script_path)
        return script_path

    def create_lark_cli_stub(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        lark_path = bin_dir / "lark-cli"
        self.write_file(
            lark_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import hashlib
                import json
                import os
                import sys
                from pathlib import Path


                def get_flag(args, flag):
                    for index, value in enumerate(args):
                        if value == flag and index + 1 < len(args):
                            return args[index + 1]
                    return ""


                def docs_state_path():
                    explicit = os.environ.get("LARK_CLI_STUB_DOCS_STATE", "")
                    if explicit:
                        return explicit
                    docs_log_path = os.environ.get("LARK_CLI_STUB_DOCS_LOG", "")
                    if docs_log_path:
                        return f"{docs_log_path}.state.json"
                    home = os.environ.get("HOME", "")
                    return str(Path(home) / ".lark-cli-stub-docs-state.json") if home else ""


                def read_docs_state():
                    state_path = docs_state_path()
                    if not state_path:
                        return {}
                    path = Path(state_path)
                    if not path.exists():
                        return {}
                    return json.loads(path.read_text(encoding="utf-8"))


                def write_docs_state(state):
                    state_path = docs_state_path()
                    if not state_path:
                        return
                    path = Path(state_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")


                def log_docs_call(payload):
                    docs_log_path = os.environ.get("LARK_CLI_STUB_DOCS_LOG", "")
                    if not docs_log_path:
                        return
                    path = Path(docs_log_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")


                args = sys.argv[1:]
                if args[:2] == ["docs", "+create"]:
                    content = sys.stdin.read()
                    document_id = "doc_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:24]
                    url = f"https://www.feishu.cn/docx/{document_id}"
                    log_docs_call(
                        {
                            "argv": args,
                            "content": content,
                            "url": url,
                            "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                        }
                    )
                    state = read_docs_state()
                    state[document_id] = {"title": "Untitled", "url": url, "content": content}
                    write_docs_state(state)
                    if os.environ.get("LARK_CLI_STUB_DOCS_NETWORK_EOF_FAIL") == "1":
                        print(
                            json.dumps(
                                {
                                    "ok": False,
                                    "identity": get_flag(args, "--as") or "user",
                                    "error": {
                                        "type": "network",
                                        "subtype": "transport",
                                        "message": 'API call failed: Post "https://open.feishu.cn/open-apis/docs_ai/v1/documents": EOF',
                                    },
                                    "_notice": {
                                        "update": {
                                            "command": "lark-cli update",
                                            "current": "1.0.63",
                                            "latest": "1.0.68",
                                            "message": "lark-cli 1.0.68 available, current 1.0.63, run: lark-cli update",
                                        }
                                    },
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            file=sys.stderr,
                        )
                        raise SystemExit(2)
                    if os.environ.get("LARK_CLI_STUB_DOCS_KEYCHAIN_FAIL") == "1":
                        print(
                            json.dumps(
                                {
                                    "ok": False,
                                    "identity": get_flag(args, "--as") or "user",
                                    "error": {
                                        "type": "config",
                                        "message": "keychain Get failed: keychain not initialized",
                                        "hint": "run lark-cli config keychain-downgrade",
                                    },
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            file=sys.stderr,
                        )
                        raise SystemExit(2)
                    if os.environ.get("LARK_CLI_STUB_DOCS_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "docs stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "identity": get_flag(args, "--as") or "bot",
                                "data": {
                                    "document": {
                                        "document_id": document_id,
                                        "revision_id": 1,
                                        "url": url,
                                    }
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    raise SystemExit(0)

                if args[:2] == ["docs", "+update"]:
                    content = sys.stdin.read()
                    doc = get_flag(args, "--doc")
                    log_docs_call(
                        {
                            "argv": args,
                            "content": content,
                            "url": doc,
                            "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                        }
                    )
                    if os.environ.get("LARK_CLI_STUB_DOCS_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "docs stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    document_id = doc.rsplit("/", 1)[-1]
                    state = read_docs_state()
                    state.setdefault(document_id, {"url": doc, "title": "Untitled", "content": ""})
                    existing_content = state[document_id].get("content", "")
                    state[document_id]["content"] = (existing_content + "\\n\\n" + content).strip()
                    write_docs_state(state)
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "identity": get_flag(args, "--as") or "bot",
                                "data": {
                                    "document": {
                                        "revision_id": 2,
                                        "url": doc,
                                    },
                                    "result": "success",
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    raise SystemExit(0)

                if args[:2] == ["docs", "+fetch"]:
                    doc = get_flag(args, "--doc")
                    if os.environ.get("LARK_CLI_STUB_DOCS_LOG_FETCH") == "1":
                        log_docs_call(
                            {
                                "argv": args,
                                "url": doc,
                                "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                            }
                        )
                    if os.environ.get("LARK_CLI_STUB_DOCS_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "docs fetch stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    document_id = doc.rsplit("/", 1)[-1]
                    state = read_docs_state()
                    title = state.get(document_id, {}).get("title", "stub")
                    if os.environ.get("LARK_CLI_STUB_DOCS_EMPTY_FETCH") == "1":
                        content = f"<title>{title}</title>"
                    else:
                        content = state.get(document_id, {}).get("content", "") or f"<title>{title}</title>"
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "identity": get_flag(args, "--as") or "bot",
                                "data": {
                                    "document": {
                                        "document_id": document_id,
                                        "revision_id": 2,
                                        "content": content,
                                    }
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    raise SystemExit(0)

                if args[:3] == ["drive", "files", "patch"]:
                    params = json.loads(get_flag(args, "--params") or "{}")
                    data = json.loads(get_flag(args, "--data") or "{}")
                    document_id = params.get("file_token", "")
                    new_title = data.get("new_title", "")
                    log_docs_call(
                        {
                            "argv": args,
                            "params": get_flag(args, "--params"),
                            "data": get_flag(args, "--data"),
                            "url": f"https://www.feishu.cn/docx/{document_id}",
                            "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                        }
                    )
                    if os.environ.get("LARK_CLI_STUB_DOCS_TITLE_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "title stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    state = read_docs_state()
                    state.setdefault(document_id, {"url": f"https://www.feishu.cn/docx/{document_id}"})
                    state[document_id]["title"] = new_title
                    write_docs_state(state)
                    print(json.dumps({"code": 0, "msg": "Success"}, ensure_ascii=False))
                    raise SystemExit(0)

                if args[:2] == ["drive", "+inspect"]:
                    doc = get_flag(args, "--url")
                    document_id = doc.rsplit("/", 1)[-1]
                    state = read_docs_state()
                    title = state.get(document_id, {}).get("title", "Untitled")
                    log_docs_call(
                        {
                            "argv": args,
                            "url": doc,
                            "title": title,
                            "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                        }
                    )
                    if os.environ.get("LARK_CLI_STUB_DOCS_INSPECT_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "inspect stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "type": "docx",
                                "token": document_id,
                                "title": title,
                                "url": doc,
                            },
                            ensure_ascii=False,
                        )
                    )
                    raise SystemExit(0)

                if args[:3] == ["drive", "permission.members", "create"]:
                    log_docs_call(
                        {
                            "argv": args,
                            "params": get_flag(args, "--params"),
                            "data": get_flag(args, "--data"),
                            "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                        }
                    )
                    if os.environ.get("LARK_CLI_STUB_DOCS_PERMISSION_FAIL") == "1":
                        print(json.dumps({"ok": False, "error": "permission stub failure"}, ensure_ascii=False), file=sys.stderr)
                        raise SystemExit(2)
                    print(
                        json.dumps(
                            {
                                "code": 0,
                                "data": {
                                    "member": {
                                        "member_id": "test",
                                        "member_type": "openchat",
                                        "perm": "view",
                                        "type": "chat",
                                    }
                                },
                                "msg": "Success",
                            },
                            ensure_ascii=False,
                        )
                    )
                    raise SystemExit(0)

                key = get_flag(args, "--idempotency-key")
                chat_id = get_flag(args, "--chat-id")
                text = get_flag(args, "--text")
                markdown = get_flag(args, "--markdown")
                message_format = "markdown" if markdown else "text"
                message_body = markdown or text
                message_id = "om_" + hashlib.sha1((key or message_body).encode("utf-8")).hexdigest()[:24]

                log_path = os.environ.get("LARK_CLI_STUB_LOG", "")
                if log_path:
                    path = Path(log_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "argv": args,
                                    "chat_id": chat_id,
                                    "format": message_format,
                                    "idempotency_key": key,
                                    "message": message_body,
                                    "message_id": message_id,
                                    "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR", ""),
                                },
                                ensure_ascii=False,
                            )
                            + "\\n"
                        )

                if os.environ.get("LARK_CLI_STUB_FAIL") == "1":
                    print(json.dumps({"ok": False, "error": "stub failure"}, ensure_ascii=False), file=sys.stderr)
                    raise SystemExit(2)

                print(json.dumps({"message_id": message_id, "chat_id": chat_id, "create_time": "1"}, ensure_ascii=False))
                """
            ),
        )
        self.make_executable(lark_path)
        return lark_path

    def test_preflight_syncs_openai_codex_auth_from_main_to_summary_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            watch_root.mkdir()
            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_openclaw_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    raise SystemExit("not used")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            home_dir = base / "home"
            main_auth = {
                "version": 1,
                "profiles": {
                    "openai-codex:analyst@example.com": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "fresh-access",
                        "refresh": "fresh-refresh",
                        "expires": 4102444800000,
                        "email": "analyst@example.com",
                    },
                    "moonshot:default": {"apiKey": "main-moonshot-key"},
                },
            }
            stale_target_auth = {
                "version": 1,
                "lastGood": {"provider": "keep-this"},
                "profiles": {
                    "openai-codex:analyst@example.com": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "stale-access",
                        "refresh": "stale-refresh",
                        "expires": 0,
                    },
                    "moonshot:default": {"apiKey": "target-moonshot-key"},
                },
            }
            main_path = home_dir / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
            summary_path = home_dir / ".openclaw" / "agents" / "zsxq_pdf_digest_test_summary" / "agent" / "auth-profiles.json"
            self.write_file(main_path, json.dumps(main_auth, ensure_ascii=False, indent=2) + "\n")
            self.write_file(summary_path, json.dumps(stale_target_auth, ensure_ascii=False, indent=2) + "\n")

            env = os.environ.copy()
            env["HOME"] = str(home_dir)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--preflight-only"],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "preflight should pass after auth sync.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
            )

            expected_openai_profile = main_auth["profiles"]["openai-codex:analyst@example.com"]
            target = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(
                target["profiles"]["openai-codex:analyst@example.com"],
                expected_openai_profile,
            )
            self.assertEqual(target["profiles"]["moonshot:default"]["apiKey"], "target-moonshot-key")
            self.assertEqual(target["lastGood"], {"provider": "keep-this"})

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("[auth-sync] synced openai-codex auth from main to 1 agent file(s)", log_text)

    def test_preflight_fails_when_parallel_summary_workers_are_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            watch_root.mkdir()
            download_task_dir = base / "download_task"
            download_task_dir.mkdir()

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    raise SystemExit("not used")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            config_path = runtime_dir / "config.env"
            self.write_runtime_config(
                config_path,
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            with config_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nSUMMARY_PARALLEL_ENABLED="true"\n'
                    'SUMMARY_WORKER_COUNT="2"\n'
                    'SUMMARY_WORKER_AGENT_ID_PREFIX="zsxq_pdf_digest_test_summary_w"\n'
                )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            home_dir = base / "home"
            self.write_file(
                home_dir / ".openclaw" / "openclaw.json",
                json.dumps(
                    {
                        "agents": {
                            "list": [
                                {"id": "main"},
                                {"id": "zsxq_pdf_digest_test_summary"},
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            self.write_file(
                home_dir / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
                json.dumps({"profiles": {}}, ensure_ascii=False) + "\n",
            )
            bin_dir = base / "bin"
            self.write_file(bin_dir / "openclaw", "#!/usr/bin/env bash\nexit 0\n")
            self.make_executable(bin_dir / "openclaw")

            env = os.environ.copy()
            env["HOME"] = str(home_dir)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--preflight-only"],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            preflight_payload = json.loads((runtime_dir / "last_preflight.json").read_text(encoding="utf-8"))
            checks = {item["name"]: item for item in preflight_payload.get("checks", [])}
            self.assertEqual(checks["summary_agents_registered"]["code"], "summary_agent_unregistered")
            self.assertIn("zsxq_pdf_digest_test_summary_w1", checks["summary_agents_registered"]["detail"])
            self.assertIn("zsxq_pdf_digest_test_summary_w2", checks["summary_agents_registered"]["detail"])
            result_text = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("summary worker agent(s) not registered", result_text)

    def create_session_reuse_openclaw_stub(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        openclaw_path = bin_dir / "openclaw"
        self.write_file(
            openclaw_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                import time
                import uuid
                from pathlib import Path


                def get_flag_value(args, flag):
                    for index, value in enumerate(args):
                        if value == flag and index + 1 < len(args):
                            return args[index + 1]
                    return ""


                def build_summary_markdown(filename):
                    return (
                        f"# 测试标题\\n"
                        f"> 原始文件名：{filename}\\n\\n"
                        "## 核心结论\\n"
                        "- 结论一。\\n"
                        "- 结论二。\\n"
                        "- 结论三。\\n\\n"
                        "## 核心问题与回答\\n\\n"
                        "### 1. 问题一\\n"
                        "回答一。\\n\\n"
                        "### 2. 问题二\\n"
                        "回答二。\\n\\n"
                        "### 3. 问题三\\n"
                        "回答三。"
                    )


                args = sys.argv[1:]
                if "agent" not in args:
                    raise SystemExit(0)

                agent_id = get_flag_value(args, "--agent") or "main"
                prompt_text = get_flag_value(args, "--message")
                home_dir = Path(os.environ["HOME"]).expanduser()
                sessions_dir = home_dir / ".openclaw" / "agents" / agent_id / "sessions"
                sessions_dir.mkdir(parents=True, exist_ok=True)
                store_path = sessions_dir / "sessions.json"
                try:
                    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
                except Exception:
                    store = {}
                if not isinstance(store, dict):
                    store = {}

                session_key = f"agent:{agent_id}:main"
                existing_entry = store.get(session_key)
                reused = isinstance(existing_entry, dict) and str(existing_entry.get("sessionId") or "").strip() != ""
                if reused:
                    session_id = str(existing_entry.get("sessionId") or "").strip()
                else:
                    session_id = str(uuid.uuid4())

                store[session_key] = {
                    "sessionId": session_id,
                    "sessionFile": str(sessions_dir / f"{session_id}.jsonl"),
                    "status": "running",
                }
                store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                (sessions_dir / f"{session_id}.jsonl").write_text('{"type":"session"}\\n', encoding="utf-8")

                batch_match = re.search(r"批次文件：`([^`]+)`", prompt_text)
                if not batch_match:
                    raise SystemExit("missing batch file in stub prompt")
                batch_path = Path(batch_match.group(1))
                batch_payload = json.loads(batch_path.read_text(encoding="utf-8"))
                files = list(batch_payload.get("files", []))
                handled_paths = [str(item.get("path", "")).strip() for item in files if str(item.get("path", "")).strip()]
                filenames = [str(item.get("filename", "")).strip() or Path(path).name for item, path in zip(files, handled_paths)]
                delay_by_filename = {}
                delay_config = os.environ.get("OPENCLAW_STUB_DELAY_BY_FILENAME_JSON", "")
                if delay_config:
                    try:
                        delay_by_filename = json.loads(delay_config)
                    except Exception:
                        delay_by_filename = {}
                if filenames:
                    delay_seconds = float(delay_by_filename.get(filenames[0], 0) or 0)
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)

                stage = "summary"
                summaries = [
                    {
                        "path": path,
                        "filename": filename,
                        "title": "测试标题",
                        "quality_hint": "",
                        "markdown": build_summary_markdown(filename),
                    }
                    for path, filename in zip(handled_paths, filenames)
                ]
                payload_text = (
                    "【ZSXQ PDF 本地摘要完成】\\n"
                    f"处理数量：{len(handled_paths)}\\n"
                    "文件：\\n"
                    + "".join(f"- {filename}\\n" for filename in filenames)
                    + "\\nZSXQ_SUMMARY_JSON: "
                    + json.dumps(
                        {
                            "status": "success",
                            "handled_count": len(handled_paths),
                            "handled_paths": handled_paths,
                            "summaries": summaries,
                        },
                        ensure_ascii=False,
                    )
                )

                agent_output = {
                    "result": {
                        "payloads": [{"text": payload_text}],
                        "meta": {
                            "agentMeta": {
                                "sessionId": session_id,
                                "provider": "stub-provider",
                                "model": "stub-model",
                                "promptTokens": 2000 if reused else 1000,
                                "lastCallUsage": {
                                    "input": 100,
                                    "output": 50,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                    "total": 150,
                                },
                            },
                            "systemPromptReport": {
                                "workspaceDir": str(home_dir),
                                "systemPrompt": {"chars": 10},
                                "skills": {"promptChars": 0},
                                "tools": {"listChars": 0, "schemaChars": 0},
                            },
                        },
                    }
                }
                log_path = Path(os.environ["OPENCLAW_STUB_LOG"]).expanduser()
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "stage": stage,
                                "agent_id": agent_id,
                                "session_id": session_id,
                                "reused": reused,
                            },
                            ensure_ascii=False,
                        )
                        + "\\n"
                    )
                print(json.dumps(agent_output, ensure_ascii=False))
                """
            ),
        )
        self.make_executable(openclaw_path)
        return openclaw_path

    def create_summary_timeout_then_success_openclaw_stub(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        openclaw_path = bin_dir / "openclaw"
        self.write_file(
            openclaw_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                import time
                import uuid
                from pathlib import Path


                def get_flag_value(args, flag):
                    for index, value in enumerate(args):
                        if value == flag and index + 1 < len(args):
                            return args[index + 1]
                    return ""


                def build_summary_markdown(filename):
                    return (
                        f"# 超时重试测试标题\\n"
                        f"> 原始文件名：{filename}\\n\\n"
                        "## 核心结论\\n"
                        "- 第一条。\\n"
                        "- 第二条。\\n"
                        "- 第三条。\\n\\n"
                        "## 核心问题与回答\\n\\n"
                        "### 1. 问题一\\n"
                        "回答一。\\n\\n"
                        "### 2. 问题二\\n"
                        "回答二。\\n\\n"
                        "### 3. 问题三\\n"
                        "回答三。"
                    )


                args = sys.argv[1:]
                if "agent" not in args:
                    raise SystemExit(0)

                agent_id = get_flag_value(args, "--agent") or "main"
                prompt_text = get_flag_value(args, "--message")
                batch_match = re.search(r"批次文件：`([^`]+)`", prompt_text)
                if not batch_match:
                    raise SystemExit("missing batch file in stub prompt")
                batch_path = Path(batch_match.group(1))
                batch_payload = json.loads(batch_path.read_text(encoding="utf-8"))
                files = list(batch_payload.get("files", []))
                handled_paths = [str(item.get("path", "")).strip() for item in files if str(item.get("path", "")).strip()]
                filenames = [str(item.get("filename", "")).strip() or Path(path).name for item, path in zip(files, handled_paths)]

                stage = "summary"
                state_path = Path(os.environ["OPENCLAW_STUB_STATE"]).expanduser()
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"summary_calls": 0}
                state["summary_calls"] = int(state.get("summary_calls", 0) or 0) + 1
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                attempt_no = int(state.get("summary_calls", 0) or 0)

                log_path = Path(os.environ["OPENCLAW_STUB_LOG"]).expanduser()
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "stage": stage,
                                "agent_id": agent_id,
                                "attempt_no": attempt_no,
                            },
                            ensure_ascii=False,
                        )
                        + "\\n"
                    )

                if attempt_no == 1:
                    timeout_seconds = int(get_flag_value(args, "--timeout") or "1")
                    time.sleep(timeout_seconds + 2)
                    raise SystemExit(0)

                payload_text = (
                    "【ZSXQ PDF 本地摘要完成】\\n"
                    f"处理数量：{len(handled_paths)}\\n"
                    "文件：\\n"
                    + "".join(f"- {filename}\\n" for filename in filenames)
                    + "\\nZSXQ_SUMMARY_JSON: "
                    + json.dumps(
                        {
                            "status": "success",
                            "handled_count": len(handled_paths),
                            "handled_paths": handled_paths,
                            "summaries": [
                                {
                                    "path": path,
                                    "filename": filename,
                                    "title": "超时重试测试标题",
                                    "quality_hint": "",
                                    "markdown": build_summary_markdown(filename),
                                }
                                for path, filename in zip(handled_paths, filenames)
                            ],
                        },
                        ensure_ascii=False,
                    )
                )

                print(
                    json.dumps(
                        {
                            "result": {
                                "payloads": [{"text": payload_text}],
                                "meta": {
                                    "agentMeta": {
                                        "sessionId": str(uuid.uuid4()),
                                        "provider": "stub-provider",
                                        "model": "stub-model",
                                        "promptTokens": 1000,
                                        "lastCallUsage": {
                                            "input": 100,
                                            "output": 50,
                                            "cacheRead": 0,
                                            "cacheWrite": 0,
                                            "total": 150,
                                        },
                                    },
                                    "systemPromptReport": {
                                        "workspaceDir": str(Path.cwd()),
                                        "systemPrompt": {"chars": 10},
                                        "skills": {"promptChars": 0},
                                        "tools": {"listChars": 0, "schemaChars": 0},
                                    },
                                },
                            }
                        },
                        ensure_ascii=False,
                    )
                )
                """
            ),
        )
        self.make_executable(openclaw_path)
        return openclaw_path

    def create_summary_token_broken_openclaw_stub(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        openclaw_path = bin_dir / "openclaw"
        self.write_file(
            openclaw_path,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                from pathlib import Path


                args = sys.argv[1:]
                if "agent" not in args:
                    raise SystemExit(0)

                def get_flag_value(flag):
                    for index, value in enumerate(args):
                        if value == flag and index + 1 < len(args):
                            return args[index + 1]
                    return ""

                prompt_text = get_flag_value("--message")
                batch_match = re.search(r"批次文件：`([^`]+)`", prompt_text)
                filename = ""
                if batch_match:
                    batch_payload = json.loads(Path(batch_match.group(1)).read_text(encoding="utf-8"))
                    files = batch_payload.get("files", [])
                    if files:
                        filename = str(files[0].get("filename") or Path(str(files[0].get("path", ""))).name)

                log_path = os.environ.get("OPENCLAW_STUB_LOG", "")
                if log_path:
                    with Path(log_path).open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"stage": "summary", "filename": filename}, ensure_ascii=False) + "\\n")

                print(
                    json.dumps(
                        {
                            "status": "error",
                            "summary": (
                                "Gateway agent failed; falling back to embedded: "
                                "GatewayClientRequestError: FailoverError: OAuth token refresh failed "
                                "for openai-codex: Failed to refresh OpenAI Codex token. "
                                "Please try again or re-authenticate. code: refresh_token_reused"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                raise SystemExit(1)
                """
            ),
        )
        self.make_executable(openclaw_path)
        return openclaw_path

    def test_dry_run_uses_repo_summary_prompt_when_workspace_copy_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "workspace_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "示例研报.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()

            openclaw_bin_dir = base / "bin"
            self.create_openclaw_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))

                    for item in payload.get("files", []):
                        filename = str(item.get("filename", "")).strip() or "sample.pdf"
                        text_path = output_dir / f"{Path(filename).stem}.txt"
                        text_body = "这是测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""

                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )

            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)
            self.write_file(
                runtime_dir / "run_status.json",
                json.dumps({"run_started_at": "2026-03-29T14:00:01+08:00"}, ensure_ascii=False) + "\n",
            )

            env = os.environ.copy()
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--dry-run", "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "run.sh dry-run should succeed when only the workspace copy of summary_prompt.md is missing.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "dry_run")
            self.assertEqual(result_payload["operational_state"], "dry_run")
            self.assertEqual(result_payload["status_from_exit_code"], "success")
            self.assertEqual(result_payload["run_status_json_path"], str(runtime_dir / "run_status.json"))

            preflight_payload = json.loads((runtime_dir / "last_preflight.json").read_text(encoding="utf-8"))
            checks = {item["name"]: item for item in preflight_payload.get("checks", [])}
            self.assertTrue(checks["summary_prompt"]["ok"])
            self.assertTrue(checks["summary_system_prompt"]["ok"])
            self.assertFalse(any(name == "publish" + "_prompt" for name in checks))
            self.assertFalse((runtime_dir / "summary_prompt.md").exists())

            run_status_payload = json.loads((runtime_dir / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(run_status_payload["status"], "dry_run")
            self.assertEqual(run_status_payload["phase"], "completed")
            self.assertEqual(run_status_payload["operational_state"], "dry_run")
            self.assertEqual(run_status_payload["run_started_at"], result_payload["execute_time"])
            self.assertEqual(run_status_payload["new_pdf_count"], 1)

    def test_run_snapshot_includes_kb_common_for_obsidian_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            scripts_dir = base / "scripts"
            runtime_dir.mkdir()
            scripts_dir.mkdir()

            self.write_file(
                scripts_dir / "archive_to_obsidian.py",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import kb_common
                    print(kb_common.SENTINEL)
                    """
                ),
            )
            self.write_file(scripts_dir / "kb_common.py", 'SENTINEL = "kb-common-loaded"\n')
            self.write_file(
                runtime_dir / "config.env",
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    HELPER_SCRIPT_PATH=""
                    SCANNER_SCRIPT_PATH=""
                    RESEARCH_LIBRARY_INDEX_SCRIPT_PATH=""
                    MARKITDOWN_SCRIPT_PATH=""
                    CLEAN_MARKDOWN_SCRIPT_PATH=""
                    OBSIDIAN_ARCHIVE_SCRIPT_PATH="{scripts_dir / "archive_to_obsidian.py"}"
                    OBSIDIAN_INDEX_SCRIPT_PATH=""
                    PYTHON_BIN="{sys.executable}"
                    SUMMARY_AGENT_ID="zsxq_pdf_digest_test_summary"
                    """
                ),
            )
            self.write_file(
                runtime_dir / "run.worker.sh",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    source "$ZSXQ_CONFIG_PATH"
                    "$PYTHON_BIN" "$ZSXQ_OBSIDIAN_ARCHIVE_SCRIPT_PATH"
                    test -f "$ZSXQ_KB_COMMON_SCRIPT_PATH"
                    """
                ),
            )
            self.make_executable(runtime_dir / "run.worker.sh")
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("kb-common-loaded", completed.stdout)

    def test_run_snapshot_includes_runtime_paths_for_index_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            scripts_dir = base / "scripts"
            runtime_dir.mkdir()
            scripts_dir.mkdir()

            self.write_file(
                scripts_dir / "research_library_index.py",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import runtime_paths
                    print(runtime_paths.SENTINEL)
                    """
                ),
            )
            self.write_file(scripts_dir / "runtime_paths.py", 'SENTINEL = "runtime-paths-loaded"\n')
            self.write_file(
                runtime_dir / "config.env",
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    HELPER_SCRIPT_PATH=""
                    SCANNER_SCRIPT_PATH=""
                    RESEARCH_LIBRARY_INDEX_SCRIPT_PATH="{scripts_dir / 'research_library_index.py'}"
                    MARKITDOWN_SCRIPT_PATH=""
                    CLEAN_MARKDOWN_SCRIPT_PATH=""
                    OBSIDIAN_ARCHIVE_SCRIPT_PATH=""
                    OBSIDIAN_INDEX_SCRIPT_PATH=""
                    PYTHON_BIN="{sys.executable}"
                    SUMMARY_AGENT_ID="zsxq_pdf_digest_test_summary"
                    """
                ),
            )
            self.write_file(
                runtime_dir / "run.worker.sh",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    source "$ZSXQ_CONFIG_PATH"
                    "$PYTHON_BIN" "$ZSXQ_RESEARCH_LIBRARY_INDEX_SCRIPT_PATH"
                    test -f "$ZSXQ_RUNTIME_PATHS_SCRIPT_PATH"
                    """
                ),
            )
            self.make_executable(runtime_dir / "run.worker.sh")
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("runtime-paths-loaded", completed.stdout)

    def test_cron_wrapper_falls_back_to_source_files_and_logs_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            source_dir = base / "source_task"
            runtime_dir = base / "runtime_task"
            source_dir.mkdir()
            runtime_dir.mkdir()

            source_wrapper = source_dir / "run.cron-safe.sh"
            source_wrapper.write_text(RUN_CRON_SAFE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.make_executable(source_wrapper)

            source_run = source_dir / "run.sh"
            self.write_file(
                source_run,
                "#!/usr/bin/env bash\n"
                "echo child-run-start\n"
                "exit 7\n",
            )
            self.make_executable(source_run)

            self.write_file(
                source_dir / "config.env",
                "#!/usr/bin/env bash\n"
                "LOG_FILE=\"cron.log\"\n",
            )

            (runtime_dir / "run.cron-safe.sh").symlink_to(source_wrapper)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.cron-safe.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 7)
            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("cron run start", log_text)
            self.assertIn("child-run-start", log_text)
            self.assertIn("cron run end rc=7", log_text)

    def test_obsidian_index_stages_have_process_group_deadlines(self) -> None:
        worker_text = RUN_WORKER_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'OBSIDIAN_INDEX_TIMEOUT_SECONDS="${OBSIDIAN_INDEX_TIMEOUT_SECONDS:-900}"',
            worker_text,
        )
        self.assertEqual(worker_text.count('"$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" exec-timeout'), 2)
        self.assertEqual(worker_text.count('--timeout-seconds "$OBSIDIAN_INDEX_TIMEOUT_SECONDS"'), 2)

    def test_cron_wrapper_rotates_oversized_log_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            source_dir = base / "source_task"
            runtime_dir = base / "runtime_task"
            source_dir.mkdir()
            runtime_dir.mkdir()

            source_wrapper = source_dir / "run.cron-safe.sh"
            source_wrapper.write_text(RUN_CRON_SAFE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.make_executable(source_wrapper)
            source_run = source_dir / "run.sh"
            self.write_file(source_run, "#!/usr/bin/env bash\necho child-run-start\n")
            self.make_executable(source_run)
            self.write_file(
                source_dir / "config.env",
                "#!/usr/bin/env bash\n"
                'LOG_FILE="cron.log"\n'
                'LOG_MAX_BYTES="10"\n'
                'LOG_BACKUP_COUNT="2"\n',
            )
            old_log = "old-log-content-that-is-too-large\n"
            self.write_file(runtime_dir / "cron.log", old_log)
            (runtime_dir / "run.cron-safe.sh").symlink_to(source_wrapper)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.cron-safe.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((runtime_dir / "cron.log.1").read_text(encoding="utf-8"), old_log)
            current_log = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("cron run start", current_log)
            self.assertIn("child-run-start", current_log)

    def test_cron_wrapper_skips_when_active_run_status_heartbeat_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            source_dir = base / "source_task"
            runtime_dir = base / "runtime_task"
            source_dir.mkdir()
            runtime_dir.mkdir()

            source_wrapper = source_dir / "run.cron-safe.sh"
            source_wrapper.write_text(RUN_CRON_SAFE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.make_executable(source_wrapper)

            source_run = source_dir / "run.sh"
            self.write_file(
                source_run,
                "#!/usr/bin/env bash\n"
                "echo child-run-start\n"
                "exit 7\n",
            )
            self.make_executable(source_run)

            self.write_file(
                source_dir / "config.env",
                "#!/usr/bin/env bash\n"
                "LOG_FILE=\"cron.log\"\n"
                "LOG_MAX_BYTES=\"10\"\n",
            )
            old_log = "active-run-log-that-must-not-be-rotated\n"
            self.write_file(runtime_dir / "cron.log", old_log)
            self.write_file(
                runtime_dir / "run_status.json",
                json.dumps(
                    {
                        "status": "running",
                        "last_heartbeat_at": datetime.now(timezone.utc).astimezone().isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )

            (runtime_dir / "run.cron-safe.sh").symlink_to(source_wrapper)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.cron-safe.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0)
            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertTrue(log_text.startswith(old_log))
            self.assertIn("cron run skipped: summary task already running", log_text)
            self.assertNotIn("child-run-start", log_text)
            self.assertFalse((runtime_dir / "cron.log.1").exists())

    def test_cron_wrapper_delegates_pid_lock_to_worker_for_busy_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            source_dir = base / "source_task"
            runtime_dir = base / "runtime_task"
            source_dir.mkdir()
            runtime_dir.mkdir()

            source_wrapper = source_dir / "run.cron-safe.sh"
            source_wrapper.write_text(RUN_CRON_SAFE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.make_executable(source_wrapper)

            source_run = source_dir / "run.sh"
            self.write_file(
                source_run,
                "#!/usr/bin/env bash\n"
                "echo child-run-start\n"
                "exit 0\n",
            )
            self.make_executable(source_run)

            self.write_file(
                source_dir / "config.env",
                "#!/usr/bin/env bash\n"
                "LOG_FILE=\"cron.log\"\n",
            )
            (runtime_dir / "run.cron-safe.sh").symlink_to(source_wrapper)

            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                (runtime_dir / ".run.pid").write_text(str(sleeper.pid), encoding="utf-8")
                completed = subprocess.run(
                    ["bash", str(runtime_dir / "run.cron-safe.sh")],
                    cwd=runtime_dir,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            finally:
                sleeper.terminate()
                try:
                    sleeper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sleeper.kill()
                    sleeper.wait(timeout=5)

            self.assertEqual(completed.returncode, 0)
            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("cron run found active pid; delegating to worker for busy notification", log_text)
            self.assertIn("child-run-start", log_text)
            self.assertIn("cron run end rc=0", log_text)

    def test_run_launcher_snapshots_config_code_and_summary_prompt_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            source_dir = base / "source_task"
            runtime_dir = base / "runtime_task"
            source_dir.mkdir()
            runtime_dir.mkdir()

            source_run = source_dir / "run.sh"
            source_run.write_text(RUN_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.make_executable(source_run)

            helper_path = base / "manage_zsxq_digest_batch.py"
            scanner_path = base / "scan_new_zsxq_pdfs.py"
            helper_old_content = "helper-old\n"
            scanner_old_content = "scanner-old\n"
            summary_old_content = "summary-old\n"
            summary_system_old_content = "summary-system-old\n"
            extract_old_content = "extract-old\n"
            config_old_content = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                HELPER_SCRIPT_PATH="{helper_path}"
                SCANNER_SCRIPT_PATH="{scanner_path}"
                TARGET_CHAT_ID="config-old"
                """
            )
            helper_path.write_text(helper_old_content, encoding="utf-8")
            scanner_path.write_text(scanner_old_content, encoding="utf-8")
            (source_dir / "summary_prompt.md").write_text(summary_old_content, encoding="utf-8")
            (source_dir / "summary_system_prompt.md").write_text(summary_system_old_content, encoding="utf-8")
            (source_dir / "extract_pdf_text.py").write_text(extract_old_content, encoding="utf-8")

            self.write_file(
                source_dir / "run.worker.sh",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    source "$ZSXQ_CONFIG_PATH"

                    printf '%s' "$ZSXQ_CONFIG_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/config_snapshot_path.txt"
                    cat "$ZSXQ_CONFIG_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/config_snapshot_content.txt"
                    printf '%s' "$TARGET_CHAT_ID" > "$ZSXQ_RUNTIME_TASK_DIR/config_snapshot_value.txt"
                    printf '%s' "$ZSXQ_WORKFLOW_FINGERPRINT_MANIFEST_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/workflow_manifest_path.txt"
                    cat "$ZSXQ_WORKFLOW_FINGERPRINT_MANIFEST_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/workflow_manifest.json"
                    printf '%s' "$ZSXQ_HELPER_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/helper_snapshot_path.txt"
                    printf '%s' "$ZSXQ_SCANNER_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/scanner_snapshot_path.txt"
                    printf '%s' "$ZSXQ_SUMMARY_PROMPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/summary_prompt_snapshot_path.txt"
                    printf '%s' "$ZSXQ_SUMMARY_SYSTEM_PROMPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/summary_system_prompt_snapshot_path.txt"
                    printf '%s' "$ZSXQ_EXTRACT_TEXT_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/extract_snapshot_path.txt"

                    cat <<'EOF' > "$ZSXQ_RUNTIME_TASK_DIR/config.env"
                    #!/usr/bin/env bash
                    HELPER_SCRIPT_PATH="/tmp/helper-new.py"
                    SCANNER_SCRIPT_PATH="/tmp/scanner-new.py"
                    TARGET_CHAT_ID="config-new"
                    EOF
                    printf 'helper-new\\n' > "$HELPER_SCRIPT_PATH"
                    printf 'scanner-new\\n' > "$SCANNER_SCRIPT_PATH"
                    printf 'summary-new\\n' > "$ZSXQ_SOURCE_TASK_DIR/summary_prompt.md"
                    printf 'summary-system-new\\n' > "$ZSXQ_SOURCE_TASK_DIR/summary_system_prompt.md"
                    printf 'extract-new\\n' > "$ZSXQ_SOURCE_TASK_DIR/extract_pdf_text.py"

                    cat "$ZSXQ_HELPER_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/helper_snapshot_content.txt"
                    cat "$ZSXQ_SCANNER_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/scanner_snapshot_content.txt"
                    cat "$ZSXQ_SUMMARY_PROMPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/summary_prompt_snapshot_content.txt"
                    cat "$ZSXQ_SUMMARY_SYSTEM_PROMPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/summary_system_prompt_snapshot_content.txt"
                    cat "$ZSXQ_EXTRACT_TEXT_SCRIPT_PATH" > "$ZSXQ_RUNTIME_TASK_DIR/extract_snapshot_content.txt"
                    """
                ),
            )
            self.make_executable(source_dir / "run.worker.sh")

            self.write_file(runtime_dir / "config.env", config_old_content)

            (runtime_dir / "run.sh").symlink_to(source_run)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            def sha256_text(value: str) -> str:
                return hashlib.sha256(value.encode("utf-8")).hexdigest()

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((runtime_dir / "config_snapshot_content.txt").read_text(encoding="utf-8"), config_old_content)
            self.assertEqual((runtime_dir / "config_snapshot_value.txt").read_text(encoding="utf-8"), "config-old")
            self.assertEqual((runtime_dir / "helper_snapshot_content.txt").read_text(encoding="utf-8"), helper_old_content)
            self.assertEqual((runtime_dir / "scanner_snapshot_content.txt").read_text(encoding="utf-8"), scanner_old_content)
            self.assertEqual((runtime_dir / "summary_prompt_snapshot_content.txt").read_text(encoding="utf-8"), summary_old_content)
            self.assertEqual((runtime_dir / "summary_system_prompt_snapshot_content.txt").read_text(encoding="utf-8"), summary_system_old_content)
            self.assertEqual((runtime_dir / "extract_snapshot_content.txt").read_text(encoding="utf-8"), extract_old_content)
            self.assertNotEqual((runtime_dir / "config_snapshot_path.txt").read_text(encoding="utf-8"), str(runtime_dir / "config.env"))
            self.assertNotEqual((runtime_dir / "helper_snapshot_path.txt").read_text(encoding="utf-8"), str(helper_path))
            self.assertNotEqual((runtime_dir / "scanner_snapshot_path.txt").read_text(encoding="utf-8"), str(scanner_path))

            manifest_payload = json.loads((runtime_dir / "workflow_manifest.json").read_text(encoding="utf-8"))
            records = {item["label"]: item for item in manifest_payload["records"]}
            self.assertEqual(records["config"]["path"], str((runtime_dir / "config.env").resolve()))
            self.assertEqual(records["config"]["sha256"], sha256_text(config_old_content))
            self.assertEqual(records["helper"]["path"], str(helper_path.resolve()))
            self.assertEqual(records["helper"]["sha256"], sha256_text(helper_old_content))
            self.assertEqual(records["scanner"]["path"], str(scanner_path.resolve()))
            self.assertEqual(records["scanner"]["sha256"], sha256_text(scanner_old_content))
            self.assertEqual(records["summary_prompt"]["sha256"], sha256_text(summary_old_content))
            self.assertEqual(records["summary_system_prompt"]["sha256"], sha256_text(summary_system_old_content))
            self.assertEqual(records["extract_text"]["sha256"], sha256_text(extract_old_content))

    def test_auto_mode_waiting_quiet_window_reports_waiting_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            watch_root.mkdir()
            pdf_path = watch_root / "fresh.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=15,
            )
            self.write_file(
                runtime_dir / "watch_state.json",
                json.dumps({"known_files": {}, "pending_files": {}, "updated_at": "2026-03-29T00:00:00+08:00"}, ensure_ascii=False),
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0)
            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "waiting")
            self.assertEqual(result_payload["operational_state"], "waiting_quiet_window")
            self.assertEqual(result_payload["phase"], "waiting_quiet_window")
            self.assertEqual(result_payload["waiting_reason"], "quiet_window")
            self.assertEqual(result_payload["status_from_exit_code"], "success")

            run_status_payload = json.loads((runtime_dir / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(run_status_payload["status"], "waiting")
            self.assertEqual(run_status_payload["phase"], "waiting_quiet_window")
            self.assertEqual(run_status_payload["operational_state"], "waiting_quiet_window")
            self.assertEqual(run_status_payload["waiting_reason"], "quiet_window")
            self.assertEqual(run_status_payload["new_pdf_count"], 1)

    def test_run_status_exposes_running_chunk_while_text_extract_is_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "slow.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            openclaw_bin_dir = base / "bin"
            self.create_openclaw_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    import time
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    time.sleep(2)
                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "slow.txt"
                        text_path.write_text("这是慢速测试正文。", encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = 8
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            self.write_file(
                runtime_dir / "watch_state.json",
                json.dumps({"known_files": {}, "pending_files": {}, "updated_at": "2026-03-29T00:00:00+08:00"}, ensure_ascii=False),
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"

            process = subprocess.Popen(
                ["bash", str(runtime_dir / "run.sh"), "--dry-run", "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            observed_running_payload = None
            deadline = time.time() + 8
            status_path = runtime_dir / "run_status.json"
            while time.time() < deadline:
                if status_path.exists():
                    payload = json.loads(status_path.read_text(encoding="utf-8"))
                    if payload.get("status") == "running" and payload.get("phase") == "text_extract":
                        observed_running_payload = payload
                        break
                time.sleep(0.1)

            stdout, stderr = process.communicate(timeout=20)
            if process.returncode != 0:
                self.fail(f"run.sh should finish successfully.\nstdout:\n{stdout}\n\nstderr:\n{stderr}")

            self.assertIsNotNone(observed_running_payload)
            self.assertEqual(observed_running_payload["operational_state"], "text_extract")
            self.assertEqual(observed_running_payload["current_chunk_index"], 1)
            self.assertEqual(observed_running_payload["current_chunk_total"], 1)
            self.assertEqual(observed_running_payload["current_file"], "slow.pdf")

    def test_agent_session_is_reset_before_summary_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "session-test.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            openclaw_bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(openclaw_bin_dir)
            self.create_lark_cli_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "session-test.txt"
                        text_body = "这是会话隔离测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "session-test-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                reset_agent_session="true",
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "run.sh should succeed with the session-aware openclaw stub.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            call_lines = (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
            calls = [json.loads(line) for line in call_lines if line.strip()]
            self.assertEqual([item["stage"] for item in calls], ["summary"])
            self.assertEqual([item["agent_id"] for item in calls], ["zsxq_pdf_digest_test_summary"])
            self.assertEqual([item["reused"] for item in calls], [False])

            for agent_id in ("zsxq_pdf_digest_test_summary",):
                sessions_store = Path(env["HOME"]) / ".openclaw" / "agents" / agent_id / "sessions" / "sessions.json"
                if sessions_store.exists():
                    self.assertEqual(json.loads(sessions_store.read_text(encoding="utf-8")), {})
                session_logs = list(sessions_store.parent.glob("*.jsonl")) if sessions_store.exists() else []
                self.assertEqual(session_logs, [])

            usage_payload = json.loads((runtime_dir / "last_usage_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(usage_payload["chunk_count"], 1)
            self.assertEqual(set(usage_payload["agent_ids"]), {"zsxq_pdf_digest_test_summary"})
            self.assertEqual(len({item["session_id"] for item in usage_payload["chunks"]}), 1)

            docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(docs_calls), 4)
            self.assertEqual(docs_calls[0]["argv"][:2], ["docs", "+create"])
            self.assertEqual(docs_calls[0]["argv"][docs_calls[0]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[0]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertNotIn("# 知识星球研报总结", docs_calls[0]["content"])
            self.assertEqual(docs_calls[1]["argv"][:3], ["drive", "files", "patch"])
            self.assertEqual(docs_calls[1]["argv"][docs_calls[1]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[1]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            doc_title = json.loads(docs_calls[1]["data"])["new_title"]
            self.assertTrue(doc_title.startswith("知识星球研报总结（"))
            self.assertEqual(docs_calls[2]["argv"][:2], ["drive", "+inspect"])
            self.assertEqual(docs_calls[2]["argv"][docs_calls[2]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[2]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertEqual(docs_calls[2]["title"], doc_title)
            self.assertEqual(docs_calls[3]["argv"][:3], ["drive", "permission.members", "create"])
            self.assertEqual(docs_calls[3]["argv"][docs_calls[3]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[3]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertEqual(json.loads(docs_calls[3]["data"])["member_id"], "test")

    def test_lark_cli_permission_failure_resumes_remote_written_without_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "permission-fallback.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "permission-fallback.txt"
                        text_body = "这是授权失败测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "permission-fallback-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG_FETCH"] = "1"
            env["LARK_CLI_STUB_DOCS_PERMISSION_FAIL"] = "1"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(docs_calls[0]["argv"][:2], ["docs", "+create"])
            self.assertEqual(docs_calls[0]["argv"][docs_calls[0]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[1]["argv"][:3], ["drive", "files", "patch"])
            self.assertEqual(docs_calls[1]["argv"][docs_calls[1]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[2]["argv"][:2], ["drive", "+inspect"])
            self.assertEqual(docs_calls[2]["argv"][docs_calls[2]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[3]["argv"][:2], ["docs", "+fetch"])
            self.assertEqual(docs_calls[3]["argv"][docs_calls[3]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[4]["argv"][:3], ["drive", "permission.members", "create"])
            self.assertEqual(docs_calls[4]["argv"][docs_calls[4]["argv"].index("--as") + 1], "user")

            openclaw_calls = [
                json.loads(line)
                for line in (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([item["stage"] for item in openclaw_calls], ["summary"])

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertNotEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["published_count"], 0)

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("lark-cli 文档授权目标群失败", log_text)

            first_run_call_count = len(docs_calls)
            retry_env = env.copy()
            retry_env.pop("LARK_CLI_STUB_DOCS_PERMISSION_FAIL")
            retried = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=retry_env,
                check=False,
                capture_output=True,
                text=True,
            )

            if retried.returncode != 0:
                self.fail(
                    "remote_written retry should finish without a duplicate remote write.\n"
                    f"stdout:\n{retried.stdout}\n\nstderr:\n{retried.stderr}"
                )

            all_docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            retry_calls = all_docs_calls[first_run_call_count:]
            self.assertEqual(retry_calls[0]["argv"][:2], ["docs", "+fetch"])
            self.assertEqual(
                sum(1 for item in all_docs_calls if item["argv"][:2] == ["docs", "+create"]),
                1,
            )
            self.assertEqual(
                sum(1 for item in all_docs_calls if item["argv"][:2] == ["docs", "+update"]),
                0,
            )

            publish_records = [
                json.loads(line)
                for line in (runtime_dir / "publish_records.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [record["status"] for record in publish_records],
                ["intent", "remote_written", "success"],
            )
            self.assertEqual(len({record["publish_key"] for record in publish_records}), 1)
            self.assertTrue(all(record["file_count"] == 1 for record in publish_records))
            self.assertTrue(all(record["report_date"] for record in publish_records))

            retry_result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(retry_result["status"], "success")
            self.assertEqual(retry_result["published_count"], 1)

            cache_versions = []
            for cache_file in (runtime_dir / "summary_cache").rglob("*.json"):
                cache_payload = json.loads(cache_file.read_text(encoding="utf-8"))
                version = str(cache_payload.get("summary_cache_version", ""))
                if version:
                    cache_versions.append(version)
            self.assertTrue(cache_versions)
            self.assertTrue(all(re.fullmatch(r"summary-v2:[0-9a-f]{64}", version) for version in cache_versions))

    def test_lark_cli_publish_fails_when_fetched_doc_body_misses_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "blank-doc-guard.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "blank-doc-guard.txt"
                        text_body = "这是飞书空白文档防护测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "blank-doc-guard-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_EMPTY_FETCH"] = "1"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertNotEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["published_count"], 0)

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("lark-cli docs +fetch 正文校验失败", log_text)
            self.assertIn("matched_anchors=0", log_text)
            self.assertNotIn("lark-cli 文档授权目标群成功", log_text)

    def test_lark_cli_keychain_failure_summary_is_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "keychain-failure.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "keychain-failure.txt"
                        text_body = "这是 keychain 失败摘要测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "keychain-failure-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_KEYCHAIN_FAIL"] = "1"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            failure_reason = result_payload["message"]
            self.assertIn("lark-cli 读不到本机密钥", failure_reason)
            self.assertNotIn("} x", failure_reason)
            self.assertNotIn("权限失败：{", failure_reason)

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("lark-cli docs create 失败：lark-cli 配置失败", log_text)

    def test_lark_cli_network_eof_uses_fast_backoff_and_keeps_real_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "network-eof.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "network-eof.txt"
                        text_body = "这是飞书网络 EOF 快速退避测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "network-eof-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            runtime_markitdown_script = runtime_dir / "convert_with_markitdown.py"
            self.write_file(
                runtime_markitdown_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys

                    if "--preflight-only" in sys.argv:
                        print(json.dumps({"ok": True, "checks": []}, ensure_ascii=False))
                        raise SystemExit(0)
                    raise SystemExit(1)
                    """
                ),
            )
            self.make_executable(runtime_markitdown_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            with (runtime_dir / "config.env").open("a", encoding="utf-8") as handle:
                handle.write(f'MARKITDOWN_SCRIPT_PATH="{runtime_markitdown_script}"\n')
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_NETWORK_EOF_FAIL"] = "1"

            expected_delays = (5, 10, 20)
            for failure_count, expected_delay in enumerate(expected_delays, start=1):
                completed = subprocess.run(
                    ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                    cwd=runtime_dir,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)

                result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
                self.assertNotEqual(
                    result_payload["message"],
                    "系统环境预检未通过",
                    (runtime_dir / "last_preflight.json").read_text(encoding="utf-8")
                    + "\n"
                    + (runtime_dir / "cron.log").read_text(encoding="utf-8")
                    + "\n"
                    + completed.stdout
                    + completed.stderr,
                )
                self.assertIn("飞书 API 网络连接中断（EOF）", result_payload["message"])
                self.assertNotIn("未知错误", result_payload["message"])

                retry_ledger = json.loads((runtime_dir / "stage_retry_ledger.json").read_text(encoding="utf-8"))
                publish_entry = next(entry for entry in retry_ledger["entries"].values() if entry["stage"] == "publish")
                self.assertEqual(publish_entry["failure_count"], failure_count)
                self.assertEqual(publish_entry["max_attempts"], 4)
                self.assertEqual(publish_entry["status"], "retry_pending")
                retry_at = datetime.fromisoformat(publish_entry["next_retry_at"])
                failed_at = datetime.fromisoformat(publish_entry["last_failed_at"])
                self.assertEqual(int((retry_at - failed_at).total_seconds()), expected_delay * 60)

                result_text = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
                self.assertIn("## ❌ 知识星球研报｜本轮失败", result_text)
                self.assertIn("重试：已排队", result_text)

            final_attempt = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(final_attempt.returncode, 0, final_attempt.stdout + final_attempt.stderr)
            final_ledger = json.loads((runtime_dir / "stage_retry_ledger.json").read_text(encoding="utf-8"))
            final_publish_entry = next(entry for entry in final_ledger["entries"].values() if entry["stage"] == "publish")
            self.assertEqual(final_publish_entry["failure_count"], 4)
            self.assertEqual(final_publish_entry["status"], "retry_exhausted")
            self.assertIsNone(final_publish_entry["next_retry_at"])
            final_text = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("## ❌ 知识星球研报｜本轮失败", final_text)

    def test_lark_cli_notifications_record_message_ids_for_key_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "notify-test.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "notify-test.txt"
                        text_body = "这是通知链路测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "notify-test-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                reset_agent_session="true",
                lark_cli_notifications="true",
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_LOG"] = str(runtime_dir / "lark_calls.jsonl")
            env["ZSXQ_RUN_AT_OVERRIDE"] = "2026-07-14T12:00:00+08:00"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "run.sh should succeed and send notifications through lark-cli.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            lark_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([item["format"] for item in lark_calls], ["markdown", "markdown"])
            self.assertTrue(all(item["argv"][item["argv"].index("--as") + 1] == "bot" for item in lark_calls))
            self.assertTrue(
                all(item["config_dir"] == str(runtime_dir / "lark_cli_openclaw_config") for item in lark_calls)
            )
            self.assertTrue(lark_calls[0]["idempotency_key"].startswith("zsxq-pdf-digest-doc-completed-"))
            self.assertTrue(lark_calls[1]["idempotency_key"].startswith("zsxq-pdf-digest-completed-"))
            self.assertTrue(all(len(item["idempotency_key"]) <= 50 for item in lark_calls))
            self.assertIn("## ✅ 知识星球研报｜文档 1/1 已发布", lark_calls[0]["message"])
            self.assertIn("本批 **1** 篇｜累计发布 **1/1**", lark_calls[0]["message"])
            self.assertIn("[立即查看飞书文档](https://www.feishu.cn/docx/", lark_calls[0]["message"])
            self.assertIn("## ✅ 知识星球研报｜本轮完成", lark_calls[1]["message"])
            self.assertIn("下载/待处理 **1**｜总结 **1**｜发布 **1**｜异常 **0**", lark_calls[1]["message"])
            self.assertIn("[飞书文档 1](https://www.feishu.cn/docx/", lark_calls[1]["message"])
            self.assertTrue(all(str(runtime_dir) not in item["message"] for item in lark_calls))

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("通知发送记录：channel=lark-cli", log_text)
            self.assertIn("message_id=om_", log_text)

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            notifications = result_payload.get("notification_messages", [])
            self.assertEqual([item["event"] for item in notifications], ["doc-completed", "completed"])
            self.assertTrue(all(item["channel"] == "lark-cli" for item in notifications))
            self.assertTrue(all(str(item.get("message_id") or "").startswith("om_") for item in notifications))
            self.assertEqual(result_payload["last_notification_message_id"], notifications[-1]["message_id"])

            outbox_before_retry = json.loads(
                (runtime_dir / "notification_outbox.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(outbox_before_retry["items"]), 2)
            self.assertTrue(all(item["status"] == "sent" for item in outbox_before_retry["items"].values()))

            retry_env = env.copy()
            retry_env["ZSXQ_RUN_AT_OVERRIDE"] = "2026-07-14T12:30:00+08:00"
            retried = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=retry_env,
                check=False,
                capture_output=True,
                text=True,
            )
            if retried.returncode != 0:
                self.fail(
                    "same-file retry should reuse sent document and terminal notifications.\n"
                    f"stdout:\n{retried.stdout}\n\nstderr:\n{retried.stderr}"
                )

            lark_calls_after_retry = [
                json.loads(line)
                for line in (runtime_dir / "lark_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(lark_calls_after_retry, lark_calls)
            notification_records = [
                json.loads(line)
                for line in (runtime_dir / "notification_messages.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([item["event"] for item in notification_records], ["doc-completed", "completed"])
            outbox_after_retry = json.loads(
                (runtime_dir / "notification_outbox.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(outbox_after_retry["items"]), set(outbox_before_retry["items"]))
            self.assertTrue(all(item["status"] == "sent" for item in outbox_after_retry["items"].values()))
            retry_result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(retry_result["status"], "success")
            self.assertEqual(retry_result.get("notification_messages"), [])

    def test_one_nonretryable_bad_pdf_does_not_block_good_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            (pdf_dir / "bad-geometry.pdf").write_bytes(b"bad")
            (pdf_dir / "good-report.pdf").write_bytes(b"good")
            self.write_file(
                runtime_dir / "watch_state.json",
                json.dumps(
                    {
                        "schema_version": 2,
                        "known_files": {},
                        "pending_files": {},
                        "known_sha256s": {},
                        "pending_sha256s": {},
                    }
                )
                + "\n",
            )
            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    import os
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()
                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}))
                        raise SystemExit(0)
                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        filename = item.get("filename", "")
                        log_path = Path(os.environ["EXTRACT_STUB_LOG"])
                        with log_path.open("a", encoding="utf-8") as handle:
                            handle.write(filename + "\\n")
                        if filename.startswith("bad"):
                            item["text_extract_error"] = "unsupported page geometry"
                            item["text_extract_error_type"] = "content_failure"
                            item["text_extract_error_code"] = "unsupported_page_geometry"
                            item["text_extract_retryable"] = False
                            item["text_extract_profile"] = "ocr-geometry-v2"
                            continue
                        text_path = output_dir / (Path(filename).stem + ".txt")
                        body = "这是可正常总结的研报正文。"
                        text_path.write_text(body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(body)
                        item["text_extract_cache_key"] = "a" * 64
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)
            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                text_extract_retry_count=2,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["EXTRACT_STUB_LOG"] = str(runtime_dir / "extract_calls.txt")

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            extract_calls = (runtime_dir / "extract_calls.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(extract_calls.count("bad-geometry.pdf"), 1)
            self.assertEqual(extract_calls.count("good-report.pdf"), 1)

            result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "partial_success")
            self.assertEqual(result["run_outcome"], "partial_success")
            self.assertEqual(result["pipeline_health"], "degraded")
            self.assertEqual(result["summary_ready_count"], 1)
            self.assertEqual(result["published_count"], 1)
            self.assertEqual(result["quarantined_count"], 1)
            self.assertFalse((runtime_dir / "failure_backoff.json").exists())

            ledger = json.loads((runtime_dir / "stage_retry_ledger.json").read_text(encoding="utf-8"))
            entry = next(item for item in ledger["entries"].values() if item["filename"] == "bad-geometry.pdf")
            self.assertEqual(entry["status"], "needs_transform")
            self.assertEqual(entry["failure_count"], 1)
            quarantine = json.loads((runtime_dir / "quarantine.json").read_text(encoding="utf-8"))
            self.assertEqual(quarantine["entries"][0]["status"], "needs_transform")

            result_md = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("## ⚠️ 知识星球研报｜本轮部分完成", result_md)
            self.assertIn("下载/待处理 **2**｜总结 **1**｜发布 **1**｜异常 **1**", result_md)
            self.assertIn("`bad-geometry.pdf`", result_md)
            self.assertNotIn(str(runtime_dir), result_md)

            (pdf_dir / "bad-only.pdf").write_bytes(b"bad-only")
            second = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(second_result["status"], "completed_with_quarantine")
            self.assertNotEqual(second_result["status"], "partial_success")
            self.assertEqual(second_result["run_outcome"], "completed_with_quarantine")
            self.assertEqual(second_result["summary_ready_count"], 0)
            self.assertEqual(second_result["published_count"], 0)
            self.assertEqual(second_result["quarantined_count"], 1)
            second_result_md = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("## 🟠 知识星球研报｜本轮未发布", second_result_md)
            self.assertNotIn("本轮部分完成", second_result_md)

    def test_long_batch_publishes_completed_groups_before_all_summaries_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_paths = []
            for index in range(12):
                pdf_path = pdf_dir / f"report-{index + 1:02d}.pdf"
                pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")
                pdf_paths.append(pdf_path)

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)
            obsidian_index_script = self.create_obsidian_index_stub(runtime_dir / "update_obsidian_indexes.py")

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        filename = str(item.get("filename") or "report.pdf")
                        text_path = output_dir / f"{Path(filename).stem}.txt"
                        text_body = f"这是 {filename} 的测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = f"cache-{Path(filename).stem}"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            config_path = runtime_dir / "config.env"
            self.write_runtime_config(
                config_path,
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                lark_cli_notifications="true",
            )
            with config_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nDOC_GROUP_SIZE="5"\n'
                    'DOC_GROUP_THRESHOLD="10"\n'
                    f'OBSIDIAN_INDEX_SCRIPT_PATH="{obsidian_index_script}"\n'
                    'OBSIDIAN_INDEX_RESULT_JSON="state/obsidian_index_result.json"\n'
                )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["LARK_CLI_STUB_LOG"] = str(runtime_dir / "lark_calls.jsonl")
            env["OBSIDIAN_INDEX_STUB_LOG"] = str(runtime_dir / "obsidian_index_calls.jsonl")
            env["OBSIDIAN_INDEX_STUB_RUN_STATUS"] = str(runtime_dir / "run_status.json")
            env["OBSIDIAN_INDEX_STUB_FAIL_REBUILD"] = "1"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--folder", str(pdf_dir)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "long manual batch should succeed with incremental publishing.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            first_publish = log_text.index("第 1/3 组 lark-cli docs 发布完成")
            sixth_extract = log_text.index("第 6/12 批开始文本提取")
            second_publish = log_text.index("第 2/3 组 lark-cli docs 发布完成")
            eleventh_extract = log_text.index("第 11/12 批开始文本提取")
            doc_notice_marker = "通知发送记录：channel=lark-cli event=doc-completed status=success"
            doc_notice_positions = [match.start() for match in re.finditer(doc_notice_marker, log_text)]
            self.assertEqual(len(doc_notice_positions), 3)
            self.assertLess(first_publish, sixth_extract)
            self.assertLess(second_publish, eleventh_extract)
            self.assertLess(doc_notice_positions[0], sixth_extract)
            self.assertLess(doc_notice_positions[1], eleventh_extract)

            docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            create_calls = [item for item in docs_calls if item["argv"][:2] == ["docs", "+create"]]
            title_calls = [item for item in docs_calls if item["argv"][:3] == ["drive", "files", "patch"]]
            self.assertEqual(len(create_calls), 3)
            titles = [json.loads(item["data"])["new_title"] for item in title_calls]
            self.assertIn("知识星球研报总结（", titles[0])
            self.assertTrue(titles[0].endswith("5 篇 1/3"))
            self.assertTrue(titles[1].endswith("5 篇 2/3"))
            self.assertTrue(titles[2].endswith("2 篇 3/3"))

            lark_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lark_calls), 4)
            self.assertEqual([item["format"] for item in lark_calls], ["markdown"] * 4)
            self.assertTrue(all(len(item["idempotency_key"]) <= 50 for item in lark_calls))
            self.assertEqual(len({item["idempotency_key"] for item in lark_calls}), 4)
            for index, expected_batch_count, expected_cumulative in (
                (1, 5, 5),
                (2, 5, 10),
                (3, 2, 12),
            ):
                message = lark_calls[index - 1]["message"]
                self.assertIn(f"## ✅ 知识星球研报｜文档 {index}/3 已发布", message)
                self.assertIn(
                    f"本批 **{expected_batch_count}** 篇｜累计发布 **{expected_cumulative}/12**",
                    message,
                )
                self.assertIn("[立即查看飞书文档](https://www.feishu.cn/docx/", message)
                self.assertTrue(
                    lark_calls[index - 1]["idempotency_key"].startswith(
                        "zsxq-pdf-digest-doc-completed-"
                    )
                )
            self.assertIn("## ✅ 知识星球研报｜本轮完成", lark_calls[-1]["message"])
            self.assertIn(
                "下载/待处理 **12**｜总结 **12**｜发布 **12**｜异常 **0**",
                lark_calls[-1]["message"],
            )
            self.assertEqual(lark_calls[-1]["message"].count("https://www.feishu.cn/docx/"), 3)
            self.assertTrue(lark_calls[-1]["idempotency_key"].startswith("zsxq-pdf-digest-completed-"))

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["published_count"], 12)
            self.assertEqual(len(result_payload["doc_urls"]), 3)
            notifications = result_payload.get("notification_messages", [])
            self.assertEqual(
                [item["event"] for item in notifications],
                ["doc-completed", "doc-completed", "doc-completed", "completed"],
            )
            self.assertTrue(all(item["status"] == "success" for item in notifications))
            self.assertEqual(result_payload["last_notification_message_id"], notifications[-1]["message_id"])

            index_calls = [
                json.loads(line)
                for line in (runtime_dir / "obsidian_index_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            incremental_calls = [item for item in index_calls if "--incremental-only" in item["argv"]]
            rebuild_calls = [item for item in index_calls if "--rebuild-all" in item["argv"]]
            self.assertEqual(len(incremental_calls), 3)
            self.assertTrue(all("--batch-file" in item["argv"] for item in incremental_calls))
            self.assertEqual(len(rebuild_calls), 1)
            self.assertEqual(index_calls[-1], rebuild_calls[0])
            rebuild_status = rebuild_calls[0]["run_status"]
            self.assertEqual(rebuild_status["phase"], "obsidian_index")
            self.assertEqual(rebuild_status["operational_state"], "obsidian_index")
            self.assertEqual(rebuild_status["published_count"], 12)
            self.assertIn("Obsidian 整批全量索引更新 warning：rc=7", log_text)

    def test_parallel_summary_workers_keep_publish_order_serial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            for index in range(4):
                (pdf_dir / f"report-{index + 1:02d}.pdf").write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            bin_dir = base / "bin"
            self.create_session_reuse_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        filename = str(item.get("filename") or "report.pdf")
                        text_path = output_dir / f"{Path(filename).stem}.txt"
                        text_body = f"这是 {filename} 的测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = f"cache-{Path(filename).stem}"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            config_path = runtime_dir / "config.env"
            self.write_runtime_config(
                config_path,
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            with config_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nSUMMARY_PARALLEL_ENABLED="true"\n'
                    'SUMMARY_WORKER_COUNT="2"\n'
                    'SUMMARY_WORKER_AGENT_ID_PREFIX="zsxq_pdf_digest_test_summary_w"\n'
                    'DOC_GROUP_SIZE="4"\n'
                    'DOC_GROUP_THRESHOLD="1"\n'
                )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["HOME"] = str(base / "home")
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")
            env["OPENCLAW_STUB_DELAY_BY_FILENAME_JSON"] = json.dumps({"report-01.pdf": 1.0})

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--folder", str(pdf_dir)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "parallel summary batch should succeed.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            openclaw_calls = [
                json.loads(line)
                for line in (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            agent_ids = [item["agent_id"] for item in openclaw_calls]
            self.assertEqual(set(agent_ids), {"zsxq_pdf_digest_test_summary_w1", "zsxq_pdf_digest_test_summary_w2"})
            self.assertNotIn("zsxq_pdf_digest_test_summary", agent_ids)

            docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            create_calls = [item for item in docs_calls if item["argv"][:2] == ["docs", "+create"]]
            update_calls = [item for item in docs_calls if item["argv"][:2] == ["docs", "+update"]]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(update_calls, [])
            content = create_calls[0]["content"]
            self.assertLess(content.index("report-01.pdf"), content.index("report-02.pdf"))
            self.assertLess(content.index("report-02.pdf"), content.index("report-03.pdf"))
            self.assertLess(content.index("report-03.pdf"), content.index("report-04.pdf"))

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["published_count"], 4)
            run_status = json.loads((runtime_dir / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(run_status.get("active_workers"), [])

    def test_idle_no_new_pdf_writes_result_without_sending_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            watch_root.mkdir()
            download_task_dir = base / "download_task"
            download_task_dir.mkdir()

            bin_dir = base / "bin"
            self.create_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)
            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                lark_cli_notifications="true",
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["LARK_CLI_STUB_LOG"] = str(runtime_dir / "lark_calls.jsonl")
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_FAIL"] = "1"
            env["ZSXQ_RUN_AT_OVERRIDE"] = "2026-07-14T12:00:00+08:00"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("没有新增 PDF", completed.stdout)
            self.assertFalse((runtime_dir / "lark_calls.jsonl").exists())
            self.assertFalse((runtime_dir / "openclaw_calls.jsonl").exists())
            self.assertFalse((runtime_dir / "notification_messages.jsonl").exists())

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["message"], "没有新增 PDF")
            self.assertEqual(result_payload["operational_state"], "idle_no_new_pdf")
            self.assertEqual(result_payload["run_outcome"], "noop")
            self.assertEqual(result_payload["pipeline_health"], "healthy")
            first_run_id = result_payload["run_id"]
            self.assertEqual(str(uuid.UUID(first_run_id)), first_run_id)
            self.assertNotIn("notification_messages", result_payload)

            second = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(second_payload["execute_time"], result_payload["execute_time"])
            self.assertNotEqual(second_payload["run_id"], first_run_id)
            self.assertEqual(str(uuid.UUID(second_payload["run_id"])), second_payload["run_id"])

    def test_waiting_quiet_window_does_not_enqueue_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            watch_root.mkdir()
            pdf_path = watch_root / "waiting-notification.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")
            download_task_dir = base / "download_task"
            download_task_dir.mkdir()

            self.write_file(
                runtime_dir / "watch_state.json",
                json.dumps(
                    {
                        "schema_version": 2,
                        "known_files": {},
                        "pending_files": {},
                        "known_sha256s": {},
                        "pending_sha256s": {},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )

            bin_dir = base / "bin"
            self.create_openclaw_stub(bin_dir)
            self.create_lark_cli_stub(bin_dir)
            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=60,
                lark_cli_notifications="true",
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["LARK_CLI_STUB_LOG"] = str(runtime_dir / "lark_calls.jsonl")
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["LARK_CLI_STUB_FAIL"] = "1"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            self.assertFalse((runtime_dir / "lark_calls.jsonl").exists())
            self.assertFalse((runtime_dir / "notification_outbox.json").exists())

            self.assertFalse((runtime_dir / "openclaw_calls.jsonl").exists())

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertNotIn("notification_messages", result_payload)
            self.assertNotIn("last_notification_message_id", result_payload)

            second = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse((runtime_dir / "lark_calls.jsonl").exists())
            self.assertFalse((runtime_dir / "openclaw_calls.jsonl").exists())

            second_result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertNotIn("notification_messages", second_result_payload)

    def test_summary_timeout_triggers_local_retry_without_touching_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "timeout-test.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            openclaw_bin_dir = base / "bin"
            self.create_summary_timeout_then_success_openclaw_stub(openclaw_bin_dir)
            self.create_lark_cli_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "timeout-test.txt"
                        text_body = "这是超时重试测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "timeout-test-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
                summary_agent_timeout_seconds=2,
                summary_timeout_retry_count=1,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")
            env["OPENCLAW_STUB_STATE"] = str(runtime_dir / "openclaw_state.json")
            env["LARK_CLI_STUB_DOCS_LOG"] = str(runtime_dir / "lark_docs_calls.jsonl")

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            if completed.returncode != 0:
                self.fail(
                    "run.sh should succeed after summary timeout retry.\n"
                    f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
                )

            call_lines = (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
            calls = [json.loads(line) for line in call_lines if line.strip()]
            self.assertEqual(
                [(item["stage"], item["attempt_no"]) for item in calls],
                [("summary", 1), ("summary", 2)],
            )

            log_text = (runtime_dir / "cron.log").read_text(encoding="utf-8")
            self.assertIn("本地摘要命中超时兜底，准备重试", log_text)
            self.assertIn("local timeout after 2s", log_text)

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "success")
            self.assertEqual(result_payload["published_count"], 1)
            docs_calls = [
                json.loads(line)
                for line in (runtime_dir / "lark_docs_calls.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(docs_calls[0]["argv"][:2], ["docs", "+create"])
            self.assertEqual(docs_calls[1]["argv"][:3], ["drive", "files", "patch"])
            self.assertEqual(docs_calls[2]["argv"][:2], ["drive", "+inspect"])
            self.assertEqual(docs_calls[3]["argv"][:3], ["drive", "permission.members", "create"])
            self.assertEqual(docs_calls[0]["argv"][docs_calls[0]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[1]["argv"][docs_calls[1]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[2]["argv"][docs_calls[2]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[3]["argv"][docs_calls[3]["argv"].index("--as") + 1], "user")
            self.assertEqual(docs_calls[0]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertEqual(docs_calls[1]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertEqual(docs_calls[2]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))
            self.assertEqual(docs_calls[3]["config_dir"], str(runtime_dir / "lark_cli_openclaw_config"))

    def test_summary_token_failure_is_shown_as_login_token_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            pdf_path = pdf_dir / "token-failure.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            openclaw_bin_dir = base / "bin"
            self.create_summary_token_broken_openclaw_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        text_path = output_dir / "token-failure.txt"
                        text_body = "这是令牌失效测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = "token-failure-cache-key"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            env = os.environ.copy()
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"

            completed = subprocess.run(
                ["bash", str(runtime_dir / "run.sh"), "--file", str(pdf_path)],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 1)

            result_payload = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "env_failed")
            self.assertEqual(result_payload["message"], "登录令牌坏了，需要重新登录 OpenAI Codex")
            self.assertEqual(result_payload["summary_ready_count"], 0)
            self.assertEqual(result_payload["published_count"], 0)

            result_text = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("## ❌ 知识星球研报｜本轮失败", result_text)
            self.assertIn("原因：登录令牌坏了，需要重新登录 OpenAI Codex", result_text)

    def test_summary_token_failure_stops_batch_and_honors_retry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            runtime_dir = base / "runtime_task"
            runtime_dir.mkdir()
            watch_root = base / "watch_root"
            pdf_dir = watch_root / "batch-001"
            pdf_dir.mkdir(parents=True)
            for filename in ("token-failure-a.pdf", "token-failure-b.pdf"):
                (pdf_dir / filename).write_bytes(b"%PDF-1.4\n%stub\n")

            download_task_dir = base / "download_task"
            download_task_dir.mkdir()
            openclaw_bin_dir = base / "bin"
            self.create_summary_token_broken_openclaw_stub(openclaw_bin_dir)

            runtime_extract_script = runtime_dir / "extract_pdf_text.py"
            self.write_file(
                runtime_extract_script,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse
                    import json
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--batch-file", default="")
                    parser.add_argument("--output-dir", default="")
                    parser.add_argument("--preflight-only", action="store_true")
                    args = parser.parse_args()

                    if args.preflight_only:
                        print(json.dumps({"checks": [], "ok": True}, ensure_ascii=False))
                        raise SystemExit(0)

                    batch_path = Path(args.batch_file)
                    output_dir = Path(args.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(batch_path.read_text(encoding="utf-8"))
                    for index, item in enumerate(payload.get("files", []), start=1):
                        text_path = output_dir / f"token-failure-{index}.txt"
                        text_body = f"这是第 {index} 篇令牌失效测试正文。"
                        text_path.write_text(text_body, encoding="utf-8")
                        item["extracted_text_path"] = str(text_path)
                        item["extracted_text_chars"] = len(text_body)
                        item["text_extract_cache_key"] = f"token-failure-cache-key-{index}"
                        item["text_source"] = "stub_extract"
                        item["text_extract_error"] = ""
                        item["text_extract_warning"] = ""
                    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
                    """
                ),
            )
            self.make_executable(runtime_extract_script)

            self.write_runtime_config(
                runtime_dir / "config.env",
                watch_root=watch_root,
                download_task_dir=download_task_dir,
                quiet_window_minutes=0,
            )
            self.write_file(
                runtime_dir / "watch_state.json",
                json.dumps({"known_files": {}, "pending_files": {}, "updated_at": "2026-03-29T00:00:00+08:00"}, ensure_ascii=False),
            )
            (runtime_dir / "run.sh").symlink_to(RUN_SCRIPT)

            home_dir = base / "home"
            auth_payload = {
                "profiles": {
                    "openai-codex:analyst@example.com": {
                        "access": "expired-access-1",
                        "refresh": "expired-refresh-1",
                        "expires": 0,
                    }
                }
            }
            for auth_path in (
                home_dir / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
                home_dir / ".openclaw" / "agents" / "zsxq_pdf_digest_test_summary" / "agent" / "auth-profiles.json",
                home_dir / ".openclaw" / "agents" / "zsxq_pdf_digest_test" / "agent" / "auth-profiles.json",
            ):
                self.write_file(auth_path, json.dumps(auth_payload, ensure_ascii=False, indent=2) + "\n")
            self.write_file(
                home_dir / ".openclaw" / "identity" / "device-auth.json",
                json.dumps({"device": "before"}, ensure_ascii=False) + "\n",
            )

            env = os.environ.copy()
            env["HOME"] = str(home_dir)
            env["PATH"] = f"{openclaw_bin_dir}:{env.get('PATH', '')}"
            env["OPENCLAW_STUB_LOG"] = str(runtime_dir / "openclaw_calls.jsonl")

            first = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)

            first_result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(first_result["status"], "env_failed")
            self.assertEqual(first_result["new_pdf_count"], 2)
            self.assertEqual(first_result["next_retry_at"] is not None, True)
            first_text = (runtime_dir / "last_result.md").read_text(encoding="utf-8")
            self.assertIn("下载/待处理 **2**｜总结 **0**｜发布 **0**｜异常 **2**", first_text)

            call_lines = (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
            calls = [json.loads(line) for line in call_lines if line.strip()]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["filename"], "token-failure-a.pdf")

            churned_auth_payload = {
                "profiles": {
                    "openai-codex:analyst@example.com": {
                        "access": "expired-access-2",
                        "refresh": "expired-refresh-2",
                        "expires": 0,
                    }
                }
            }
            for auth_path in (
                home_dir / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
                home_dir / ".openclaw" / "agents" / "zsxq_pdf_digest_test_summary" / "agent" / "auth-profiles.json",
                home_dir / ".openclaw" / "agents" / "zsxq_pdf_digest_test" / "agent" / "auth-profiles.json",
            ):
                self.write_file(auth_path, json.dumps(churned_auth_payload, ensure_ascii=False, indent=2) + "\n")
            self.write_file(
                home_dir / ".openclaw" / "identity" / "device-auth.json",
                json.dumps({"device": "after"}, ensure_ascii=False) + "\n",
            )

            second = subprocess.run(
                ["bash", str(runtime_dir / "run.sh")],
                cwd=runtime_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            second_result = json.loads((runtime_dir / "last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(second_result["status"], "paused")
            self.assertEqual(second_result["operational_state"], "backoff_cooldown")
            self.assertEqual(second_result["next_retry_at"], first_result["next_retry_at"])

            call_lines = (runtime_dir / "openclaw_calls.jsonl").read_text(encoding="utf-8").splitlines()
            calls = [json.loads(line) for line in call_lines if line.strip()]
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
