#!/usr/bin/env python3
"""Convert ZSXQ PDFs to raw Markdown with MarkItDown when available."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from research_library_index import build_report_id, compute_sha256, db_path_for_library, record_event, upsert_report
except ModuleNotFoundError:  # pragma: no cover
    from scripts.research_library_index import build_report_id, compute_sha256, db_path_for_library, record_event, upsert_report

try:
    from runtime_paths import DEFAULT_LIBRARY_ROOT
except ModuleNotFoundError:  # pragma: no cover
    from scripts.runtime_paths import DEFAULT_LIBRARY_ROOT

DEFAULT_MARKITDOWN_TIMEOUT_SECONDS = 300


class MarkItDownTimeoutError(RuntimeError):
    pass


def try_record_event(db_path: Path, payload: dict[str, Any]) -> None:
    try:
        record_event(db_path, payload)
    except Exception:
        # Trace events are metadata only; they must not change conversion results.
        pass


def artifact_batch_id(item: dict[str, Any], pdf_path: Path, library_root: Path) -> str:
    existing = str(item.get("batch_id", "") or "").strip()
    if existing:
        return existing
    try:
        relative = pdf_path.expanduser().resolve(strict=False).relative_to(
            (library_root / "pdfs").expanduser().resolve(strict=False)
        )
        if len(relative.parts) >= 2:
            return relative.parts[0]
    except ValueError:
        pass
    parent_name = pdf_path.parent.name
    if "__to__" in parent_name:
        return parent_name
    try:
        dt = datetime.fromisoformat(str(item.get("modified_at", "") or "").replace("Z", "+00:00"))
        return f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}__to__{dt.strftime('%Y-%m-%d_%H-%M-%S')}"
    except ValueError:
        return "manual"


def artifact_stem(item: dict[str, Any], pdf_path: Path) -> str:
    filename = str(item.get("filename", "") or pdf_path.name).strip()
    return Path(filename).stem or pdf_path.stem


def raw_markdown_path(library_root: Path, item: dict[str, Any], pdf_path: Path) -> Path:
    batch_id = artifact_batch_id(item, pdf_path, library_root)
    stem = artifact_stem(item, pdf_path)
    return library_root / "markdown" / "raw" / batch_id / f"{stem}.raw.md"


def markitdown_timeout_seconds() -> int:
    raw = os.environ.get("MARKITDOWN_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MARKITDOWN_TIMEOUT_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        return DEFAULT_MARKITDOWN_TIMEOUT_SECONDS
    return seconds if seconds > 0 else DEFAULT_MARKITDOWN_TIMEOUT_SECONDS


def run_python_markitdown(pdf_path: Path, timeout_seconds: int) -> str:
    code = (
        "from markitdown import MarkItDown\n"
        "import sys\n"
        "result = MarkItDown().convert(sys.argv[1])\n"
        "text = str(getattr(result, 'text_content', '') or '').strip()\n"
        "if text:\n"
        "    print(text)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise MarkItDownTimeoutError(f"MarkItDown timed out after {timeout_seconds}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
        raise RuntimeError(detail[:500])
    text = proc.stdout.strip()
    if not text:
        raise RuntimeError("MarkItDown returned empty output")
    return text


def convert_pdf(pdf_path: Path) -> str:
    try:
        text = run_python_markitdown(pdf_path, markitdown_timeout_seconds())
        if text:
            return text
    except MarkItDownTimeoutError:
        raise
    except Exception:
        pass

    timeout_seconds = markitdown_timeout_seconds()
    try:
        proc = subprocess.run(
            ["markitdown", str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise MarkItDownTimeoutError(f"MarkItDown timed out after {timeout_seconds}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
        raise RuntimeError(detail[:500])
    text = proc.stdout.strip()
    if not text:
        raise RuntimeError("MarkItDown returned empty output")
    return text


def build_smoke_pdf(path: Path) -> None:
    stream = "BT /F1 12 Tf 72 720 Td (MarkItDown preflight smoke) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode(),
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode())
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_start = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode())
    path.write_bytes(payload)


def preflight() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(name: str, ok: bool, detail: str, code: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "code": code})

    try:
        from markitdown import MarkItDown  # type: ignore

        add_check("markitdown_import", True, "MarkItDown Python package available", "markitdown_import_failed")
    except Exception as exc:
        add_check("markitdown_import", False, repr(exc), "markitdown_import_failed")
        return {"ok": False, "checks": checks}

    try:
        import pdfminer  # type: ignore  # noqa: F401
        import pdfplumber  # type: ignore  # noqa: F401

        add_check("markitdown_pdf_dependencies", True, "pdfminer and pdfplumber available", "markitdown_pdf_dependencies_missing")
    except Exception as exc:
        add_check("markitdown_pdf_dependencies", False, repr(exc), "markitdown_pdf_dependencies_missing")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "smoke.pdf"
            build_smoke_pdf(pdf_path)
            text = str(getattr(MarkItDown().convert(str(pdf_path)), "text_content", "") or "").strip()
            add_check(
                "markitdown_pdf_smoke",
                "MarkItDown preflight smoke" in text,
                "PDF smoke conversion succeeded" if "MarkItDown preflight smoke" in text else "PDF smoke conversion returned unexpected text",
                "markitdown_pdf_smoke_failed",
            )
    except Exception as exc:
        add_check("markitdown_pdf_smoke", False, repr(exc), "markitdown_pdf_smoke_failed")

    return {"ok": all(bool(check["ok"]) for check in checks), "checks": checks}


def process_item(item: dict[str, Any], library_root: Path) -> dict[str, Any]:
    updated = dict(item)
    pdf_path = Path(str(updated.get("path", "") or updated.get("pdf_path", ""))).expanduser()
    if not pdf_path.exists():
        updated["raw_md_error"] = f"pdf not found: {pdf_path}"
        return updated

    try:
        pdf_sha256 = str(updated.get("pdf_sha256", "") or compute_sha256(pdf_path)).strip()
        report_id = str(updated.get("report_id", "") or build_report_id(pdf_sha256)).strip()
        updated["report_id"] = report_id
        updated["pdf_sha256"] = pdf_sha256
        updated["batch_id"] = artifact_batch_id(updated, pdf_path, library_root)
        output_path = raw_markdown_path(library_root, updated, pdf_path)
        if output_path.exists() and output_path.read_text(encoding="utf-8", errors="replace").strip():
            raw_text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        else:
            raw_text = convert_pdf(pdf_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(raw_text.strip() + "\n", encoding="utf-8")

        updated["raw_md_path"] = str(output_path)
        updated["raw_md_chars"] = len(raw_text.strip())
        updated.pop("raw_md_error", None)
        db_path = db_path_for_library(library_root)
        upsert_report(
            db_path,
            {
                "report_id": report_id,
                "pdf_sha256": pdf_sha256,
                "title": Path(str(updated.get("filename", ""))).stem,
                "pdf_path": str(pdf_path),
                "raw_md_path": str(output_path),
                "downloaded_at": str(updated.get("modified_at", "") or ""),
                "index_status": "raw_md_created",
            },
        )
        try_record_event(
            db_path,
            {
                "report_id": report_id,
                "pdf_sha256": pdf_sha256,
                "batch_id": str(updated.get("batch_id", "") or ""),
                "status": "raw_md_created",
                "artifact_path": str(output_path),
            },
        )
    except Exception as exc:
        updated["raw_md_error"] = str(exc)
        try:
            if "report_id" in updated or "pdf_sha256" in updated:
                db_path = db_path_for_library(library_root)
                upsert_report(
                    db_path,
                    {
                        "report_id": str(updated.get("report_id", "")),
                        "pdf_sha256": str(updated.get("pdf_sha256", "")),
                        "pdf_path": str(pdf_path),
                        "index_status": "raw_md_failed",
                        "error_message": str(exc),
                    },
                )
                try_record_event(
                    db_path,
                    {
                        "report_id": str(updated.get("report_id", "")),
                        "pdf_sha256": str(updated.get("pdf_sha256", "")),
                        "batch_id": str(updated.get("batch_id", "") or ""),
                        "status": "raw_md_failed",
                        "artifact_path": str(pdf_path),
                        "error_message": str(exc),
                    },
                )
        except Exception:
            pass
    return updated


def process_batch(batch_file: Path, library_root: Path) -> dict[str, Any]:
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    files = batch.get("files", [])
    if not isinstance(files, list):
        raise SystemExit("invalid batch json: files must be a list")
    updated_files = [process_item(item, library_root) if isinstance(item, dict) else item for item in files]
    batch["files"] = updated_files
    batch_file.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "batch_file": str(batch_file),
        "count": len(updated_files),
        "raw_ready_count": sum(1 for item in updated_files if isinstance(item, dict) and item.get("raw_md_path")),
        "raw_failed_count": sum(1 for item in updated_files if isinstance(item, dict) and item.get("raw_md_error")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert batch PDFs to raw Markdown via MarkItDown.")
    parser.add_argument("--batch-file", default="")
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.preflight_only:
        report = preflight()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if bool(report.get("ok")) else 1

    if not args.batch_file:
        raise SystemExit("--batch-file is required unless --preflight-only is used")

    summary = process_batch(
        batch_file=Path(args.batch_file).expanduser().resolve(),
        library_root=Path(args.library_root).expanduser(),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
