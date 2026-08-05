#!/usr/bin/env python3
"""Build clean Markdown and expose it as extracted_text_path when usable."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import re
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

WATERMARK_PATTERNS = ["知识星球", "前沿信息收录", "FCCNN88", "VX", "ＶＸ"]
INLINE_WATERMARK_RE = re.compile(
    r"知识星球[:：]?\s*前沿信息收录|(?:VX|ＶＸ|微信)[:：]?\s*FCCNN88|FCCNN88"
)
MIN_CLEAN_CHARS = 1800
MAX_CLEAN_LINE_CHARS = 1200
MIN_LANGUAGE_CHARS = 500
MIN_LANGUAGE_RATIO = 0.20
MIN_MEANINGFUL_LINES = 10
MAX_REPEATED_LINE_RATIO = 0.45
MAX_WATERMARK_LINE_RATIO = 0.18
MAX_NOISE_LINE_RATIO = 0.35

CLEAN_MARKDOWN_RULES = (
    "只做格式整理和明显水印清理",
    "不总结、不改写、不删正文判断",
    "保留标题、段落、页码、数字和表格文本",
    f"任何单行最长不超过 {MAX_CLEAN_LINE_CHARS} 字，中文无空格也要硬拆行",
    "文件太短、乱码太多、正文不足时保留 clean.md，但不作为摘要主输入",
)


def try_record_event(db_path: Path, payload: dict[str, Any]) -> None:
    try:
        record_event(db_path, payload)
    except Exception:
        # Trace events are metadata only; they must not change clean.md results.
        pass


@dataclass(frozen=True)
class CleanTextQuality:
    char_count: int
    nonempty_lines: int
    max_line_chars: int
    repeated_line_ratio: float
    watermark_line_ratio: float
    noise_line_ratio: float
    language_chars: int
    language_ratio: float
    meaningful_lines: int
    acceptable: bool
    summary: str


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


def clean_markdown_path(library_root: Path, item: dict[str, Any], pdf_path: Path) -> Path:
    batch_id = artifact_batch_id(item, pdf_path, library_root)
    stem = artifact_stem(item, pdf_path)
    return library_root / "markdown" / "clean" / batch_id / f"{stem}.clean.md"


def line_is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    cleaned = remove_inline_watermarks(stripped)
    if any(pattern in stripped for pattern in WATERMARK_PATTERNS) and (
        len(stripped) <= 80 or len(cleaned) < 20
    ):
        return True
    if re.fullmatch(r"[-=_*#\s]{3,}", stripped):
        return True
    return False


def remove_inline_watermarks(text: str) -> str:
    cleaned = INLINE_WATERMARK_RE.sub(" ", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def wrap_long_line(line: str, max_chars: int = MAX_CLEAN_LINE_CHARS) -> list[str]:
    if len(line) <= max_chars:
        return [line]

    chunks: list[str] = []
    remaining = line
    while len(remaining) > max_chars:
        split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def repeated_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 8]
    if not lines:
        return 1.0
    counts = Counter(lines)
    repeated = sum(count for _, count in counts.items() if count > 1)
    return repeated / max(len(lines), 1)


def watermark_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 1.0
    hit_count = sum(1 for line in lines if line_is_noise(line) and any(pattern in line for pattern in WATERMARK_PATTERNS))
    return hit_count / len(lines)


def count_language_chars(text: str) -> int:
    return sum(1 for char in text if char.isalpha() or ("\u4e00" <= char <= "\u9fff"))


def is_noisy_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "\ufffd" in stripped and stripped.count("\ufffd") >= max(3, int(len(stripped) * 0.10)):
        return True
    language_or_digit = sum(
        1
        for char in stripped
        if char.isalnum() or ("\u4e00" <= char <= "\u9fff")
    )
    if len(stripped) >= 20 and language_or_digit < max(3, int(len(stripped) * 0.10)):
        return True
    return False


def noise_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 1.0
    noisy = sum(1 for line in lines if is_noisy_line(line))
    return noisy / len(lines)


def count_meaningful_lines(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 20:
            continue
        if is_noisy_line(stripped):
            continue
        language_chars = count_language_chars(stripped)
        if language_chars >= max(8, int(len(stripped) * MIN_LANGUAGE_RATIO)):
            count += 1
    return count


def analyze_clean_text(text: str) -> CleanTextQuality:
    stripped = text.strip()
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    char_count = len(stripped)
    max_line_chars = max((len(line) for line in stripped.splitlines()), default=0)
    repeated_ratio = repeated_line_ratio(stripped)
    wm_ratio = watermark_line_ratio(stripped)
    noisy_ratio = noise_line_ratio(stripped)
    language_chars = count_language_chars(stripped)
    language_ratio = language_chars / max(char_count, 1)
    meaningful_lines = count_meaningful_lines(stripped)
    long_wrapped_body_ok = char_count >= 3500 and meaningful_lines >= 3

    acceptable = (
        char_count >= MIN_CLEAN_CHARS
        and max_line_chars <= MAX_CLEAN_LINE_CHARS
        and repeated_ratio < MAX_REPEATED_LINE_RATIO
        and wm_ratio < MAX_WATERMARK_LINE_RATIO
        and noisy_ratio < MAX_NOISE_LINE_RATIO
        and language_chars >= MIN_LANGUAGE_CHARS
        and language_ratio >= MIN_LANGUAGE_RATIO
        and (meaningful_lines >= MIN_MEANINGFUL_LINES or long_wrapped_body_ok)
    )

    summary = (
        f"chars={char_count}, lines={len(lines)}, max_line={max_line_chars}, "
        f"repeated_ratio={repeated_ratio:.2f}, watermark_ratio={wm_ratio:.2f}, "
        f"noise_ratio={noisy_ratio:.2f}, language_ratio={language_ratio:.2f}, "
        f"language_chars={language_chars}, meaningful_lines={meaningful_lines}"
    )
    return CleanTextQuality(
        char_count=char_count,
        nonempty_lines=len(lines),
        max_line_chars=max_line_chars,
        repeated_line_ratio=repeated_ratio,
        watermark_line_ratio=wm_ratio,
        noise_line_ratio=noisy_ratio,
        language_chars=language_chars,
        language_ratio=language_ratio,
        meaningful_lines=meaningful_lines,
        acceptable=acceptable,
        summary=summary,
    )


def clean_raw_markdown(text: str) -> str:
    lines = []
    last_blank = True
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.strip()
        if line_is_noise(stripped):
            continue
        if not stripped:
            if not last_blank:
                lines.append("")
            last_blank = True
            continue
        cleaned = remove_inline_watermarks(raw.rstrip())
        lines.extend(wrap_long_line(cleaned))
        last_blank = False
    return "\n".join(lines).strip()


def is_usable_clean_text(text: str) -> bool:
    return analyze_clean_text(text).acceptable


def process_item(item: dict[str, Any], library_root: Path) -> dict[str, Any]:
    updated = dict(item)
    raw_path_value = str(updated.get("raw_md_path", "") or "").strip()
    raw_path = Path(raw_path_value).expanduser() if raw_path_value else None
    pdf_path = Path(str(updated.get("path", "") or updated.get("pdf_path", ""))).expanduser()
    if raw_path is None or not raw_path.is_file():
        updated["clean_md_error"] = "raw markdown missing"
        try:
            pdf_sha256 = str(updated.get("pdf_sha256", "") or "").strip()
            if not pdf_sha256 and pdf_path.exists():
                pdf_sha256 = compute_sha256(pdf_path)
            report_id = str(updated.get("report_id", "") or build_report_id(pdf_sha256)).strip()
            updated["report_id"] = report_id
            updated["pdf_sha256"] = pdf_sha256
            db_path = db_path_for_library(library_root)
            upsert_report(
                db_path,
                {
                    "report_id": report_id,
                    "pdf_sha256": pdf_sha256,
                    "title": Path(str(updated.get("filename", ""))).stem,
                    "pdf_path": str(pdf_path),
                    "raw_md_path": raw_path_value,
                    "index_status": "clean_md_failed",
                    "error_message": "raw markdown missing",
                },
            )
            try_record_event(
                db_path,
                {
                    "report_id": report_id,
                    "pdf_sha256": pdf_sha256,
                    "batch_id": str(updated.get("batch_id", "") or ""),
                    "status": "clean_md_failed",
                    "artifact_path": raw_path_value or str(pdf_path),
                    "error_message": "raw markdown missing",
                },
            )
        except Exception:
            pass
        return updated

    try:
        pdf_sha256 = str(updated.get("pdf_sha256", "") or "").strip()
        if not pdf_sha256 and pdf_path.exists():
            pdf_sha256 = compute_sha256(pdf_path)
        report_id = str(updated.get("report_id", "") or build_report_id(pdf_sha256)).strip()
        updated["batch_id"] = artifact_batch_id(updated, pdf_path, library_root)
        cleaned = clean_raw_markdown(raw_path.read_text(encoding="utf-8", errors="replace"))
        output_path = clean_markdown_path(library_root, updated, pdf_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(cleaned + "\n", encoding="utf-8")

        updated["report_id"] = report_id
        updated["pdf_sha256"] = pdf_sha256
        updated["clean_md_path"] = str(output_path)
        updated["clean_md_chars"] = len(cleaned)
        updated.pop("clean_md_error", None)

        quality = analyze_clean_text(cleaned)
        clean_usable = quality.acceptable
        if clean_usable:
            updated["extracted_text_path"] = str(output_path)
            updated["extracted_text_chars"] = len(cleaned)
            updated["text_source"] = "markitdown_clean"
            updated["text_extract_status"] = "success"
            updated["text_extract_cached"] = False
            updated["text_extract_cache_key"] = pdf_sha256
            updated["text_extract_retryable"] = False
        else:
            updated["clean_md_warning"] = f"clean markdown unusable; fallback to existing extractor ({quality.summary})"

        db_path = db_path_for_library(library_root)
        event_status = "clean_md_created" if clean_usable else "clean_md_unusable"
        event_error = "" if clean_usable else f"clean markdown unusable: {quality.summary}"
        upsert_report(
            db_path,
            {
                "report_id": report_id,
                "pdf_sha256": pdf_sha256,
                "title": Path(str(updated.get("filename", ""))).stem,
                "pdf_path": str(pdf_path),
                "raw_md_path": str(raw_path),
                "clean_md_path": str(output_path),
                "downloaded_at": str(updated.get("modified_at", "") or ""),
                "index_status": "clean_md_created" if clean_usable else "clean_md_failed",
                "error_message": event_error,
            },
        )
        try_record_event(
            db_path,
            {
                "report_id": report_id,
                "pdf_sha256": pdf_sha256,
                "batch_id": str(updated.get("batch_id", "") or ""),
                "status": event_status,
                "artifact_path": str(output_path),
                "error_message": event_error,
            },
        )
    except Exception as exc:
        updated["clean_md_error"] = str(exc)
        try:
            db_path = db_path_for_library(library_root)
            upsert_report(
                db_path,
                {
                    "report_id": str(updated.get("report_id", "")),
                    "pdf_sha256": str(updated.get("pdf_sha256", "")),
                    "pdf_path": str(pdf_path),
                    "raw_md_path": str(raw_path) if str(raw_path) != "." else "",
                    "index_status": "clean_md_failed",
                    "error_message": str(exc),
                },
            )
            try_record_event(
                db_path,
                {
                    "report_id": str(updated.get("report_id", "")),
                    "pdf_sha256": str(updated.get("pdf_sha256", "")),
                    "batch_id": str(updated.get("batch_id", "") or ""),
                    "status": "clean_md_failed",
                    "artifact_path": str(raw_path) if str(raw_path) != "." else str(pdf_path),
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
        "clean_ready_count": sum(1 for item in updated_files if isinstance(item, dict) and item.get("text_source") == "markitdown_clean"),
        "clean_failed_count": sum(1 for item in updated_files if isinstance(item, dict) and item.get("clean_md_error")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build clean Markdown from raw MarkItDown output.")
    parser.add_argument("--batch-file", required=True)
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = process_batch(
        batch_file=Path(args.batch_file).expanduser().resolve(),
        library_root=Path(args.library_root).expanduser(),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
