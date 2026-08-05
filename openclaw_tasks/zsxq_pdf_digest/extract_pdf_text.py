#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


WATERMARK_PATTERNS = ["知识星球", "前沿信息收录", "ＶＸ", "VX", "FCCNN88"]
INLINE_WATERMARK_RE = re.compile(
    r"知识星球[:：]?\s*前沿信息收录|(?:VX|ＶＸ|微信)[:：]?\s*FCCNN88|FCCNN88"
)
TEXT_MAX_CHARS = int(os.environ.get("TEXT_EXTRACT_MAX_CHARS", os.environ.get("OCR_TEXT_MAX_CHARS", "120000")))
MAX_TEXT_LINE_CHARS = 1200
LOCAL_OCR_FALLBACK_ENABLE = os.environ.get("LOCAL_OCR_FALLBACK_ENABLE", "true").strip().lower() not in {"0", "false", "no", "off"}
CACHE_VERSION = "2026-07-14-v2"
EXTRACTOR_PROFILE = "ocr-geometry-v2"
OCR_BASE_DPI = int(os.environ.get("OCR_BASE_DPI", "200"))
OCR_MIN_DPI = max(96, int(os.environ.get("OCR_MIN_DPI", "96")))
OCR_MAX_PAGE_PIXELS = int(os.environ.get("OCR_MAX_PAGE_PIXELS", "25000000"))
OCR_MAX_IMAGE_DIMENSION = int(os.environ.get("OCR_MAX_IMAGE_DIMENSION", "30000"))
OCR_TILE_HEIGHT = int(os.environ.get("OCR_TILE_HEIGHT", "12000"))
OCR_TILE_OVERLAP = int(os.environ.get("OCR_TILE_OVERLAP", "240"))
OCR_MAX_TILES_PER_PAGE = int(os.environ.get("OCR_MAX_TILES_PER_PAGE", "24"))
OCR_MAX_DOCUMENT_PIXELS = int(os.environ.get("OCR_MAX_DOCUMENT_PIXELS", "1200000000"))
OCR_DOCUMENT_TIMEOUT_SECONDS = int(os.environ.get("OCR_DOCUMENT_TIMEOUT_SECONDS", "360"))
OCR_RENDER_TIMEOUT_SECONDS = int(os.environ.get("OCR_RENDER_TIMEOUT_SECONDS", "90"))
OCR_TESSERACT_TIMEOUT_SECONDS = int(os.environ.get("OCR_TESSERACT_TIMEOUT_SECONDS", "120"))
OCR_PDFINFO_TIMEOUT_SECONDS = int(os.environ.get("OCR_PDFINFO_TIMEOUT_SECONDS", "20"))
# 这里故意不 resolve，这样脚本通过软链接运行时，默认运行目录还是任务目录。
TASK_DIR = Path(os.path.abspath(__file__)).parent
TEXT_CACHE_DIR = Path(os.environ.get("TEXT_EXTRACT_CACHE_DIR", str(TASK_DIR / "text_cache"))).expanduser()


def ensure_tmpdir() -> str:
    current = os.environ.get("TMPDIR", "").strip()
    if current and Path(current).exists():
        tempfile.tempdir = current
        return current

    fallback = ""
    try:
        proc = subprocess.run(
            ["getconf", "DARWIN_USER_TEMP_DIR"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            fallback = proc.stdout.strip()
    except Exception:
        fallback = ""

    if not fallback:
        fallback = "/tmp"

    Path(fallback).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = fallback
    tempfile.tempdir = fallback
    return fallback


TMP_ROOT = ensure_tmpdir()


@dataclass
class TextQuality:
    char_count: int
    nonempty_lines: int
    repeated_ratio: float
    watermark_ratio: float
    meaningful_lines: int
    weird_token_count: int
    acceptable: bool
    summary: str


@dataclass(frozen=True)
class PageGeometry:
    page: int
    width_pt: float
    height_pt: float


@dataclass(frozen=True)
class PageOcrPlan:
    page: int
    width_pt: float
    height_pt: float
    strategy: str
    dpi: int
    width_px: int
    height_px: int
    estimated_pixels: int
    tile_height: int = 0
    tile_overlap: int = 0
    tile_count: int = 1


class UnsupportedPageGeometryError(RuntimeError):
    """The PDF can be parsed, but no bounded OCR geometry can process it safely."""


def contains_watermark(line: str) -> bool:
    return any(pattern in line for pattern in WATERMARK_PATTERNS)


def remove_inline_watermarks(text: str) -> str:
    cleaned = INLINE_WATERMARK_RE.sub(" ", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def is_watermark_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not contains_watermark(stripped):
        return False
    cleaned = remove_inline_watermarks(stripped)
    return len(stripped) <= 80 or len(cleaned) < 20


def safe_tail(text: str, max_lines: int = 6, max_chars: int = 320) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    joined = " | ".join(lines[-max_lines:])
    return joined[:max_chars]


def decode_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def process_error(label: str, proc: subprocess.CompletedProcess[bytes]) -> str:
    stderr = safe_tail(decode_output(proc.stderr))
    stdout = safe_tail(decode_output(proc.stdout))
    detail = stderr or stdout or f"rc={proc.returncode}"
    return f"{label}: {detail}"


def parse_pdfinfo_geometries(output: str) -> list[PageGeometry]:
    page_count_match = re.search(r"^Pages:\s*(\d+)\s*$", output, flags=re.MULTILINE)
    page_count = int(page_count_match.group(1)) if page_count_match else 0
    specific: dict[int, PageGeometry] = {}
    for match in re.finditer(
        r"^Page\s+(\d+)\s+size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
        output,
        flags=re.MULTILINE,
    ):
        page = int(match.group(1))
        specific[page] = PageGeometry(page, float(match.group(2)), float(match.group(3)))

    if page_count and len(specific) == page_count:
        return [specific[page] for page in range(1, page_count + 1)]

    generic_match = re.search(
        r"^Page size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
        output,
        flags=re.MULTILINE,
    )
    if page_count == 1 and generic_match:
        return [PageGeometry(1, float(generic_match.group(1)), float(generic_match.group(2)))]
    return []


def probe_pdf_geometry(pdf_path: Path) -> list[PageGeometry]:
    if shutil.which("pdfinfo") is None:
        raise RuntimeError("pdfinfo_failed: command not found")
    try:
        first = subprocess.run(
            ["pdfinfo", "-box", str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=OCR_PDFINFO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pdfinfo_failed: timeout after {OCR_PDFINFO_TIMEOUT_SECONDS}s") from exc
    if first.returncode != 0:
        raise RuntimeError(process_error("pdfinfo_failed", first))

    first_output = decode_output(first.stdout)
    geometries = parse_pdfinfo_geometries(first_output)
    if geometries:
        return geometries

    page_count_match = re.search(r"^Pages:\s*(\d+)\s*$", first_output, flags=re.MULTILINE)
    page_count = int(page_count_match.group(1)) if page_count_match else 0
    if page_count <= 0:
        raise RuntimeError("pdfinfo_failed: page count or page geometry missing")

    try:
        detailed = subprocess.run(
            ["pdfinfo", "-box", "-f", "1", "-l", str(page_count), str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=OCR_PDFINFO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pdfinfo_failed: timeout after {OCR_PDFINFO_TIMEOUT_SECONDS}s") from exc
    if detailed.returncode != 0:
        raise RuntimeError(process_error("pdfinfo_failed", detailed))
    geometries = parse_pdfinfo_geometries(decode_output(detailed.stdout))
    if len(geometries) != page_count:
        raise RuntimeError(
            f"pdfinfo_failed: expected {page_count} page geometries, got {len(geometries)}"
        )
    return geometries


def rendered_dimensions(geometry: PageGeometry, dpi: int) -> tuple[int, int]:
    return (
        max(1, math.ceil(geometry.width_pt * dpi / 72.0)),
        max(1, math.ceil(geometry.height_pt * dpi / 72.0)),
    )


def geometry_fits_single_image(geometry: PageGeometry, dpi: int) -> bool:
    width_px, height_px = rendered_dimensions(geometry, dpi)
    return (
        width_px <= OCR_MAX_IMAGE_DIMENSION
        and height_px <= OCR_MAX_IMAGE_DIMENSION
        and width_px * height_px <= OCR_MAX_PAGE_PIXELS
    )


def plan_page_ocr(
    geometry: PageGeometry,
    *,
    force_safe: bool = False,
    force_tiled: bool = False,
) -> PageOcrPlan:
    if geometry.width_pt <= 0 or geometry.height_pt <= 0:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: page={geometry.page}, invalid_points={geometry.width_pt}x{geometry.height_pt}"
        )
    if OCR_MIN_DPI <= 0 or OCR_BASE_DPI < OCR_MIN_DPI:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: invalid_dpi_profile={OCR_BASE_DPI}/{OCR_MIN_DPI}"
        )

    candidate_dpis = sorted(
        {dpi for dpi in (OCR_BASE_DPI, 150, 120, OCR_MIN_DPI) if OCR_MIN_DPI <= dpi <= OCR_BASE_DPI},
        reverse=True,
    )
    if force_safe:
        candidate_dpis = [OCR_MIN_DPI]

    if not force_tiled:
        for dpi in candidate_dpis:
            if not geometry_fits_single_image(geometry, dpi):
                continue
            width_px, height_px = rendered_dimensions(geometry, dpi)
            strategy = "standard" if dpi == OCR_BASE_DPI and not force_safe else "adaptive"
            return PageOcrPlan(
                page=geometry.page,
                width_pt=geometry.width_pt,
                height_pt=geometry.height_pt,
                strategy=strategy,
                dpi=dpi,
                width_px=width_px,
                height_px=height_px,
                estimated_pixels=width_px * height_px,
            )

    width_px, height_px = rendered_dimensions(geometry, OCR_MIN_DPI)
    if width_px > OCR_MAX_IMAGE_DIMENSION:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: page={geometry.page}, width_px={width_px} exceeds "
            f"limit={OCR_MAX_IMAGE_DIMENSION} at dpi={OCR_MIN_DPI}"
        )

    max_height_by_pixels = OCR_MAX_PAGE_PIXELS // max(width_px, 1)
    tile_height = min(OCR_TILE_HEIGHT, OCR_MAX_IMAGE_DIMENSION, max_height_by_pixels)
    if tile_height <= OCR_TILE_OVERLAP * 2:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: page={geometry.page}, safe_tile_height={tile_height}, "
            f"overlap={OCR_TILE_OVERLAP}"
        )
    stride = tile_height - OCR_TILE_OVERLAP
    tile_count = max(1, math.ceil(max(height_px - OCR_TILE_OVERLAP, 1) / stride))
    if tile_count > OCR_MAX_TILES_PER_PAGE:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: page={geometry.page}, tile_count={tile_count} exceeds "
            f"limit={OCR_MAX_TILES_PER_PAGE}"
        )
    return PageOcrPlan(
        page=geometry.page,
        width_pt=geometry.width_pt,
        height_pt=geometry.height_pt,
        strategy="tiled",
        dpi=OCR_MIN_DPI,
        width_px=width_px,
        height_px=height_px,
        estimated_pixels=width_px * height_px,
        tile_height=tile_height,
        tile_overlap=OCR_TILE_OVERLAP,
        tile_count=tile_count,
    )


def plan_document_ocr(
    geometries: list[PageGeometry],
    *,
    force_safe: bool = False,
    force_tiled: bool = False,
) -> list[PageOcrPlan]:
    if not geometries:
        raise UnsupportedPageGeometryError("unsupported_page_geometry: no pages")
    plans = [
        plan_page_ocr(geometry, force_safe=force_safe, force_tiled=force_tiled)
        for geometry in geometries
    ]
    projected_pixels = 0
    for plan in plans:
        projected_pixels += plan.estimated_pixels
        if plan.strategy == "tiled" and plan.tile_count > 1:
            projected_pixels += plan.width_px * plan.tile_overlap * (plan.tile_count - 1)
    if projected_pixels > OCR_MAX_DOCUMENT_PIXELS:
        raise UnsupportedPageGeometryError(
            f"unsupported_page_geometry: projected_pixels={projected_pixels} exceeds "
            f"document_limit={OCR_MAX_DOCUMENT_PIXELS}"
        )
    return plans


def plan_diagnostics(plans: list[PageOcrPlan]) -> dict[str, object]:
    strategies = {plan.strategy for plan in plans}
    if len(strategies) == 1:
        strategy = next(iter(strategies))
    else:
        strategy = "mixed"
    return {
        "extractor_profile": EXTRACTOR_PROFILE,
        "strategy": strategy,
        "base_dpi": OCR_BASE_DPI,
        "min_dpi": OCR_MIN_DPI,
        "page_count": len(plans),
        "tile_count": sum(plan.tile_count for plan in plans if plan.strategy == "tiled"),
        "estimated_pixels": sum(plan.estimated_pixels for plan in plans),
        "pages": [
            {
                "page": plan.page,
                "width_pt": plan.width_pt,
                "height_pt": plan.height_pt,
                "strategy": plan.strategy,
                "dpi": plan.dpi,
                "width_px": plan.width_px,
                "height_px": plan.height_px,
                "estimated_pixels": plan.estimated_pixels,
                "tile_count": plan.tile_count,
                "tile_height": plan.tile_height,
                "tile_overlap": plan.tile_overlap,
            }
            for plan in plans
        ],
    }


def is_geometry_limit_error(message: str) -> bool:
    lowered = str(message or "").casefold()
    return any(
        token in lowered
        for token in [
            "image too large",
            "image dimensions are too large",
            "image size exceeds",
            "maximum image dimension",
            "unsupported_page_geometry",
            "_encode_tile",
            "pix too large",
        ]
    )


def merge_overlapping_ocr_chunks(chunks: list[str], max_overlap_lines: int = 24) -> str:
    merged: list[str] = []
    for chunk in chunks:
        incoming = [line.rstrip() for line in decode_output(chunk).splitlines()]
        while incoming and not incoming[0].strip():
            incoming.pop(0)
        while incoming and not incoming[-1].strip():
            incoming.pop()
        if not incoming:
            continue
        if not merged:
            merged.extend(incoming)
            continue

        overlap = 0
        max_overlap = min(max_overlap_lines, len(merged), len(incoming))
        for size in range(max_overlap, 0, -1):
            tail = [re.sub(r"\s+", " ", line).strip().casefold() for line in merged[-size:]]
            head = [re.sub(r"\s+", " ", line).strip().casefold() for line in incoming[:size]]
            # 只去掉边界处完全一致的 OCR 行。研报里的表格/要点往往高度相似，
            # 模糊匹配会误删诸如「Section 05」和「Section 10」这样的真实正文。
            if all(left == right for left, right in zip(tail, head)):
                overlap = size
                break
        if merged and merged[-1].strip() and incoming[overlap:]:
            merged.append("")
        merged.extend(incoming[overlap:])
    return "\n".join(merged).strip()


def is_short_chart_label(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 8:
        return True
    if re.fullmatch(r"[-+]?\d+(\.\d+)?%?", stripped):
        return True
    if re.fullmatch(r"\d{1,2}/\d{2}", stripped):
        return True
    return False


def repeated_line_ratio(text: str) -> float:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not is_short_chart_label(line)
    ]
    if not lines:
        return 1.0
    counts = Counter(lines)
    repeated = sum(count for _, count in counts.items() if count > 1)
    return repeated / max(len(lines), 1)


def watermark_line_ratio(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 1.0
    hit_count = sum(1 for line in lines if is_watermark_noise_line(line))
    return hit_count / len(lines)


def count_meaningful_lines(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 20:
            continue
        if is_watermark_noise_line(stripped):
            continue
        stripped = remove_inline_watermarks(stripped)
        if re.fullmatch(r"[\W_]+", stripped):
            continue
        letterish = sum(
            1
            for ch in stripped
            if ch.isalpha() or ("\u4e00" <= ch <= "\u9fff")
        )
        if letterish >= max(8, int(len(stripped) * 0.25)):
            count += 1
    return count


def count_weird_tokens(text: str) -> int:
    tokens = {
        token
        for token in re.findall(r"[A-Za-z0-9]{20,}", text)
        if not re.fullmatch(r"[A-Fa-f0-9]{24,}", token)
    }
    return len(tokens)


def wrap_long_line(line: str, max_chars: int = MAX_TEXT_LINE_CHARS) -> list[str]:
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


def wrap_long_lines(text: str) -> str:
    wrapped: list[str] = []
    for line in text.split("\n"):
        if not line:
            wrapped.append(line)
            continue
        wrapped.extend(wrap_long_line(line))
    return "\n".join(wrapped)


def max_line_length(text: str) -> int:
    return max((len(line) for line in str(text or "").splitlines()), default=0)


def allowed_weird_token_count(char_count: int, meaningful_lines: int) -> int:
    limit = 8
    # 长篇、图表多的研报里，长 token 会自然变多，固定阈值容易误杀。
    if char_count >= 60000 or meaningful_lines >= 500:
        limit = 12
    if char_count >= 100000 or meaningful_lines >= 1000:
        limit = 16
    # OCR 会把脚注、证券代码、图表标签识别成少量超长 token。
    # 正文越长，允许的噪声也应该按比例放宽，但仍保留上限，避免接受整篇乱码。
    scaled_limit = max(char_count // 2000, meaningful_lines // 20)
    if scaled_limit > limit:
        limit = min(scaled_limit, 80)
    return limit


def analyze_text(text: str) -> TextQuality:
    stripped = text.strip()
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    char_count = len(stripped)
    rep_ratio = repeated_line_ratio(stripped)
    wm_ratio = watermark_line_ratio(stripped)
    meaningful_lines = count_meaningful_lines(stripped)
    weird_tokens = count_weird_tokens(stripped)
    weird_token_limit = allowed_weird_token_count(char_count, meaningful_lines)

    letterish_total = sum(
        1
        for ch in stripped
        if ch.isalpha() or ("\u4e00" <= ch <= "\u9fff")
    )
    long_single_line_acceptable = (
        char_count >= 1800
        and letterish_total >= 500
        and letterish_total >= int(char_count * 0.25)
    )

    acceptable = (
        char_count >= 1800
        and rep_ratio < 0.45
        and wm_ratio < 0.18
        and (meaningful_lines >= 10 or long_single_line_acceptable)
        and weird_tokens <= weird_token_limit
    )

    summary = (
        f"chars={char_count}, lines={len(lines)}, repeated_ratio={rep_ratio:.2f}, "
        f"watermark_ratio={wm_ratio:.2f}, meaningful_lines={meaningful_lines}, "
        f"weird_tokens={weird_tokens}, weird_limit={weird_token_limit}"
    )
    return TextQuality(
        char_count=char_count,
        nonempty_lines=len(lines),
        repeated_ratio=rep_ratio,
        watermark_ratio=wm_ratio,
        meaningful_lines=meaningful_lines,
        weird_token_count=weird_tokens,
        acceptable=acceptable,
        summary=summary,
    )


def sanitize_text(text: str, max_chars: int, aggressive: bool = False) -> tuple[str, bool]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    kept: list[str] = []
    used = 0
    truncated = False
    last_blank = True

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if not last_blank and used + 1 <= max_chars:
                kept.append("")
                used += 1
            last_blank = True
            continue
        if is_watermark_noise_line(stripped):
            continue
        cleaned_line = remove_inline_watermarks(raw.rstrip())
        if aggressive:
            if len(stripped) <= 2:
                continue
            if re.fullmatch(r"[\W_]+", stripped):
                continue
            if re.search(r"[A-Za-z0-9]{1,4}(\s+[A-Za-z0-9]{1,4}){6,}", stripped):
                continue
            if sum(ch.isdigit() for ch in stripped) > max(len(stripped) * 0.45, 12):
                continue

        line = cleaned_line
        extra = len(line) + (0 if last_blank else 1)
        if used + extra > max_chars:
            truncated = True
            break
        if not last_blank:
            used += 1
        kept.append(line)
        used += len(line)
        last_blank = False

    compacted = "\n".join(kept).strip()
    if not compacted:
        compacted = text[:max_chars].strip()
        truncated = len(text) > len(compacted)
    compacted = wrap_long_lines(compacted)
    return compacted, truncated


def detect_tesseract_languages() -> set[str]:
    if shutil.which("tesseract") is None:
        return set()
    proc = subprocess.run(
        ["tesseract", "--list-langs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    langs: set[str] = set()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of available languages"):
            continue
        langs.add(stripped)
    return langs


TESSERACT_LANGS = detect_tesseract_languages()
TESSERACT_OCR_LANG = "eng+chi_sim" if {"eng", "chi_sim"}.issubset(TESSERACT_LANGS) else ("eng" if "eng" in TESSERACT_LANGS else "osd")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def build_ocrmypdf_command(pdf_path: Path, sidecar_path: Path, output_pdf: Path | None) -> list[str]:
    command = ["ocrmypdf", "--force-ocr"]
    if TESSERACT_OCR_LANG:
        command.extend(["-l", TESSERACT_OCR_LANG])
    command.extend(["--sidecar", str(sidecar_path)])
    if output_pdf is None:
        command.extend(["--output-type", "none", str(pdf_path), "-"])
    else:
        command.extend([str(pdf_path), str(output_pdf)])
    return command


def build_preflight_report() -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, severity: str, detail: str, code: str) -> None:
        checks.append(
            {
                "name": name,
                "ok": ok,
                "severity": severity,
                "code": code,
                "detail": detail,
            }
        )

    record(
        "pdftotext",
        command_exists("pdftotext"),
        "fatal",
        "pdftotext available" if command_exists("pdftotext") else "pdftotext command not found",
        "pdftotext_missing",
    )
    record(
        "pdfinfo",
        command_exists("pdfinfo"),
        "fatal",
        "pdfinfo available" if command_exists("pdfinfo") else "pdfinfo command not found",
        "pdfinfo_missing",
    )

    ocrmypdf_ok = command_exists("ocrmypdf")
    pdftoppm_ok = command_exists("pdftoppm")
    tesseract_ok = command_exists("tesseract")
    ocr_fallback_ok = ocrmypdf_ok or (pdftoppm_ok and tesseract_ok)

    record(
        "ocr_fallback",
        ocr_fallback_ok,
        "fatal",
        (
            "at least one OCR fallback is available"
            if ocr_fallback_ok
            else "ocrmypdf is missing and pdftoppm+tesseract is not fully available"
        ),
        "ocr_fallback_unavailable",
    )
    record(
        "ocrmypdf",
        ocrmypdf_ok,
        "warning",
        "ocrmypdf available" if ocrmypdf_ok else "ocrmypdf command not found",
        "ocrmypdf_missing",
    )
    record(
        "pdftoppm",
        pdftoppm_ok,
        "warning",
        "pdftoppm available" if pdftoppm_ok else "pdftoppm command not found",
        "pdftoppm_missing",
    )
    record(
        "tesseract",
        tesseract_ok,
        "warning",
        "tesseract available" if tesseract_ok else "tesseract command not found",
        "tesseract_missing",
    )

    tmp_root = Path(TMP_ROOT).expanduser()
    tmp_ok = True
    tmp_detail = f"tmp root is writable: {tmp_root}"
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=tmp_root, delete=True):
            pass
    except Exception as exc:
        tmp_ok = False
        tmp_detail = f"tmp root is not writable: {tmp_root} ({exc})"
    record("tmpdir", tmp_ok, "fatal", tmp_detail, "tmpdir_unwritable")

    tesseract_langs = sorted(TESSERACT_LANGS)
    if tesseract_ok:
        record(
            "tesseract_langs",
            bool(tesseract_langs),
            "fatal",
            (
                f"usable tesseract languages: {', '.join(tesseract_langs)}"
                if tesseract_langs
                else "tesseract is installed but no language data was found"
            ),
            "tesseract_no_languages",
        )
        record(
            "chi_sim",
            "chi_sim" in TESSERACT_LANGS,
            "warning",
            (
                "chi_sim installed"
                if "chi_sim" in TESSERACT_LANGS
                else f"chi_sim missing, OCR will use {TESSERACT_OCR_LANG or 'no language'}"
            ),
            "chi_sim_missing",
        )

    fatal_failures = [check for check in checks if not bool(check["ok"]) and check["severity"] == "fatal"]
    warnings = [check for check in checks if not bool(check["ok"]) and check["severity"] == "warning"]
    return {
        "ok": not fatal_failures,
        "checks": checks,
        "fatal_failures": fatal_failures,
        "warnings": warnings,
        "tmpdir": str(tmp_root),
        "tesseract_languages": tesseract_langs,
        "tesseract_ocr_lang": TESSERACT_OCR_LANG,
        "cache_dir": str(TEXT_CACHE_DIR),
        "cache_version": CACHE_VERSION,
        "extractor_profile": EXTRACTOR_PROFILE,
        "ocr_geometry_limits": {
            "base_dpi": OCR_BASE_DPI,
            "min_dpi": OCR_MIN_DPI,
            "max_page_pixels": OCR_MAX_PAGE_PIXELS,
            "max_image_dimension": OCR_MAX_IMAGE_DIMENSION,
            "tile_height": OCR_TILE_HEIGHT,
            "tile_overlap": OCR_TILE_OVERLAP,
            "max_tiles_per_page": OCR_MAX_TILES_PER_PAGE,
            "max_document_pixels": OCR_MAX_DOCUMENT_PIXELS,
        },
    }


def compute_pdf_fingerprint(pdf_path: Path) -> str:
    hasher = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def cache_paths(cache_key: str) -> tuple[Path, Path]:
    meta_path = TEXT_CACHE_DIR / "meta" / f"{cache_key}.json"
    text_path = TEXT_CACHE_DIR / "text" / f"{cache_key}.txt"
    return meta_path, text_path


def load_success_cache(cache_key: str) -> dict[str, object] | None:
    meta_path, text_path = cache_paths(cache_key)
    if not meta_path.exists() or not text_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(payload.get("cache_version", "")).strip() != CACHE_VERSION:
        return None
    if str(payload.get("extractor_profile", "")).strip() != EXTRACTOR_PROFILE:
        return None
    if str(payload.get("status", "")).strip() != "success":
        return None
    if str(payload.get("text_path", "")).strip() != str(text_path):
        return None
    if int(payload.get("text_chars", 0) or 0) <= 0:
        return None
    return payload


def load_failure_cache(cache_key: str) -> dict[str, object] | None:
    meta_path, _ = cache_paths(cache_key)
    if not meta_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(payload.get("cache_version", "")).strip() != CACHE_VERSION:
        return None
    if str(payload.get("extractor_profile", "")).strip() != EXTRACTOR_PROFILE:
        return None
    if str(payload.get("status", "")).strip() != "failed":
        return None
    if str(payload.get("error_type", "")).strip() != "content_failure":
        return None
    if bool(payload.get("retryable", True)):
        return None
    return payload


def save_success_cache(
    cache_key: str,
    pdf_path: Path,
    text: str,
    source: str,
    warnings: list[str],
    diagnostics: dict[str, object] | None = None,
) -> Path:
    meta_path, text_path = cache_paths(cache_key)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = text.strip()
    text_path.write_text(cleaned + "\n", encoding="utf-8")
    payload = {
        "cache_version": CACHE_VERSION,
        "extractor_profile": EXTRACTOR_PROFILE,
        "status": "success",
        "cache_key": cache_key,
        "pdf_path": str(pdf_path),
        "text_path": str(text_path),
        "text_chars": len(cleaned),
        "text_source": source,
        "text_extract_warning": "; ".join(item for item in warnings if str(item).strip()),
        "text_extract_diagnostics": diagnostics or {},
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return text_path


def save_failure_cache(
    cache_key: str,
    pdf_path: Path,
    *,
    message: str,
    error_type: str,
    error_code: str,
    retryable: bool,
    warnings: list[str],
    diagnostics: dict[str, object] | None = None,
) -> None:
    if error_type != "content_failure" or retryable:
        return
    meta_path, _ = cache_paths(cache_key)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_version": CACHE_VERSION,
        "extractor_profile": EXTRACTOR_PROFILE,
        "status": "failed",
        "cache_key": cache_key,
        "pdf_path": str(pdf_path),
        "error": message,
        "error_type": error_type,
        "error_code": error_code,
        "retryable": retryable,
        "text_extract_warning": "; ".join(item for item in warnings if str(item).strip()),
        "text_extract_diagnostics": diagnostics or {},
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_runtime_failure(messages: list[str]) -> tuple[str, str, bool] | None:
    combined = " ; ".join(str(item or "").strip() for item in messages if str(item or "").strip())
    if not combined:
        return None

    lowered = combined.casefold()

    if "command not found" in lowered:
        if "pdftotext_failed" in lowered:
            return "env_failure", "pdftotext_missing", False
        if "pdfinfo_failed" in lowered:
            return "env_failure", "pdfinfo_missing", False
        if "ocrmypdf_failed" in lowered:
            return "env_failure", "ocrmypdf_missing", False
        if "pdftoppm_failed" in lowered:
            return "env_failure", "pdftoppm_missing", False
        if "tesseract_failed" in lowered:
            return "env_failure", "tesseract_missing", False
        return "env_failure", "missing_dependency", False

    if "no usable language installed" in lowered or "no language data was found" in lowered:
        return "env_failure", "tesseract_language_missing", False

    if is_geometry_limit_error(combined):
        return "content_failure", "unsupported_page_geometry", False

    if any(token in lowered for token in ["permission denied", "operation not permitted", "read-only file system"]):
        return "env_failure", "filesystem_permission_error", False

    if any(
        token in lowered
        for token in [
            "image file not found",
            "failed to open locally with tail",
            "pixread",
            "sidecar missing",
            "sidecar empty",
            "tmpdir",
            "temporary",
            "temp file",
        ]
    ):
        return "env_failure", "ocr_tempfile_failure", False

    if any(
        token in lowered
        for token in [
            "invalid pdf",
            "unable to get page count",
            "couldn't find trailer dictionary",
            "xref",
            "syntax error",
            "malformed",
        ]
    ):
        return "content_failure", "pdf_parse_failure", False

    if any(token in lowered for token in ["timeout", "timed out", "temporarily unavailable", "resource busy", "interrupted"]):
        return "transient_failure", "ocr_runtime_interrupted", True

    return "transient_failure", "ocr_runtime_failure", True


def reset_item_fields(item: dict) -> None:
    for key in [
        "extracted_text_path",
        "extracted_text_chars",
        "text_source",
        "text_extract_warning",
        "text_extract_error",
        "text_extract_error_type",
        "text_extract_error_code",
        "text_extract_retryable",
        "text_extract_status",
        "text_extract_cached",
        "text_extract_cache_key",
        "text_extract_profile",
        "text_extract_diagnostics",
    ]:
        item.pop(key, None)


def set_failure(
    item: dict,
    message: str,
    *,
    error_type: str,
    error_code: str,
    retryable: bool,
    warnings: list[str],
    cache_key: str,
    pdf_path: Path | None = None,
    diagnostics: dict[str, object] | None = None,
    cached: bool = False,
) -> dict:
    item["text_extract_status"] = "failed"
    item["text_extract_error"] = message
    item["text_extract_error_type"] = error_type
    item["text_extract_error_code"] = error_code
    item["text_extract_retryable"] = retryable
    item["text_extract_cached"] = cached
    item["text_extract_cache_key"] = cache_key
    item["text_extract_profile"] = EXTRACTOR_PROFILE
    item["text_extract_diagnostics"] = diagnostics or {}
    for warning in warnings:
        set_warning(item, warning)
    if cache_key and pdf_path is not None and not cached:
        save_failure_cache(
            cache_key,
            pdf_path,
            message=message,
            error_type=error_type,
            error_code=error_code,
            retryable=retryable,
            warnings=warnings,
            diagnostics=diagnostics,
        )
    return item


def direct_extract(pdf_path: Path) -> tuple[str, str]:
    if not command_exists("pdftotext"):
        return "", "pdftotext_failed: command not found"
    proc = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if proc.returncode != 0:
        return "", process_error("pdftotext_failed", proc)
    return decode_output(proc.stdout), ""


def ocrmypdf_extract(pdf_path: Path, output_txt: Path) -> tuple[str, str, bool]:
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError("ocrmypdf_failed: command not found")

    tmpdir = Path(tempfile.mkdtemp(prefix="zsxq_pdf_ocrmypdf_"))
    try:
        sidecar_path = tmpdir / "sidecar.txt"
        fast_proc = subprocess.run(
            build_ocrmypdf_command(pdf_path, sidecar_path, output_pdf=None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=OCR_DOCUMENT_TIMEOUT_SECONDS,
        )
        if fast_proc.returncode != 0:
            fast_error = process_error("ocrmypdf_failed", fast_proc)
            lowered = fast_error.casefold()
            if "--output-type" in lowered or "stdout" in lowered:
                output_pdf = tmpdir / "ocr.pdf"
                legacy_proc = subprocess.run(
                    build_ocrmypdf_command(pdf_path, sidecar_path, output_pdf=output_pdf),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                    timeout=OCR_DOCUMENT_TIMEOUT_SECONDS,
                )
                if legacy_proc.returncode != 0:
                    raise RuntimeError(process_error("ocrmypdf_failed", legacy_proc))
            else:
                raise RuntimeError(fast_error)
        if not sidecar_path.exists():
            raise RuntimeError("ocrmypdf_failed: sidecar missing")

        text = sidecar_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise RuntimeError("ocrmypdf_failed: sidecar empty")

        compacted, truncated = sanitize_text(text, TEXT_MAX_CHARS, aggressive=True)
        output_txt.write_text(compacted + "\n", encoding="utf-8")
        return compacted, "ocr_ocrmypdf_sidecar", truncated
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def remaining_command_timeout(deadline: float, command_limit: int) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise RuntimeError(f"ocr_runtime_timeout: document budget {OCR_DOCUMENT_TIMEOUT_SECONDS}s exhausted")
    return max(1, min(command_limit, remaining))


def run_pdftoppm(command: list[str], *, deadline: float, label: str) -> None:
    try:
        render = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=False,
            timeout=remaining_command_timeout(deadline, OCR_RENDER_TIMEOUT_SECONDS),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}: timeout after {OCR_RENDER_TIMEOUT_SECONDS}s") from exc
    if render.returncode != 0:
        raise RuntimeError(process_error(label, render))


def run_tesseract(png: Path, *, deadline: float) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(png), "stdout", "-l", TESSERACT_OCR_LANG],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=remaining_command_timeout(deadline, OCR_TESSERACT_TIMEOUT_SECONDS),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"tesseract_failed:{png.name}: timeout after {OCR_TESSERACT_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(process_error(f"tesseract_failed:{png.name}", proc))
    return decode_output(proc.stdout)


def rendered_page_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"-(\d+)\.png$", path.name)
    return (int(match.group(1)) if match else 0, path.name)


def execute_pdftoppm_plan(
    pdf_path: Path,
    tmpdir: Path,
    plans: list[PageOcrPlan],
    *,
    deadline: float,
    attempt: str,
    diagnostics: dict[str, object],
) -> list[str]:
    attempt_dir = tmpdir / attempt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    page_texts: list[str] = []
    executed_tiles = 0

    if plans and all(plan.strategy == "standard" and plan.dpi == OCR_BASE_DPI for plan in plans):
        prefix = attempt_dir / "page"
        run_pdftoppm(
            ["pdftoppm", "-r", str(OCR_BASE_DPI), "-png", str(pdf_path), str(prefix)],
            deadline=deadline,
            label="pdftoppm_failed",
        )
        pngs = sorted(attempt_dir.glob("page-*.png"), key=rendered_page_sort_key)
        if len(pngs) != len(plans):
            raise RuntimeError(
                f"pdftoppm_failed: expected {len(plans)} rendered pages, got {len(pngs)}"
            )
        for png in pngs:
            page_texts.append(run_tesseract(png, deadline=deadline))
            png.unlink(missing_ok=True)
        diagnostics["rendered_pages"] = len(page_texts)
        diagnostics["executed_tiles"] = 0
        return page_texts

    for plan in plans:
        if plan.strategy != "tiled":
            prefix = attempt_dir / f"page-{plan.page}-single"
            run_pdftoppm(
                [
                    "pdftoppm",
                    "-f",
                    str(plan.page),
                    "-l",
                    str(plan.page),
                    "-singlefile",
                    "-r",
                    str(plan.dpi),
                    "-png",
                    str(pdf_path),
                    str(prefix),
                ],
                deadline=deadline,
                label=f"pdftoppm_failed:page-{plan.page}",
            )
            png = prefix.with_suffix(".png")
            if not png.exists():
                raise RuntimeError(f"pdftoppm_failed: page-{plan.page} image missing")
            page_texts.append(run_tesseract(png, deadline=deadline))
            png.unlink(missing_ok=True)
            continue

        tile_chunks: list[str] = []
        stride = plan.tile_height - plan.tile_overlap
        for tile_index in range(plan.tile_count):
            y = tile_index * stride
            height = min(plan.tile_height, plan.height_px - y)
            if height <= 0:
                break
            prefix = attempt_dir / f"page-{plan.page}-tile-{tile_index + 1}"
            run_pdftoppm(
                [
                    "pdftoppm",
                    "-f",
                    str(plan.page),
                    "-l",
                    str(plan.page),
                    "-singlefile",
                    "-r",
                    str(plan.dpi),
                    "-png",
                    "-x",
                    "0",
                    "-y",
                    str(y),
                    "-W",
                    str(plan.width_px),
                    "-H",
                    str(height),
                    str(pdf_path),
                    str(prefix),
                ],
                deadline=deadline,
                label=f"pdftoppm_failed:page-{plan.page}-tile-{tile_index + 1}",
            )
            png = prefix.with_suffix(".png")
            if not png.exists():
                raise RuntimeError(
                    f"pdftoppm_failed: page-{plan.page}-tile-{tile_index + 1} image missing"
                )
            tile_chunks.append(run_tesseract(png, deadline=deadline))
            executed_tiles += 1
            png.unlink(missing_ok=True)
        page_texts.append(merge_overlapping_ocr_chunks(tile_chunks))

    diagnostics["rendered_pages"] = len(page_texts)
    diagnostics["executed_tiles"] = executed_tiles
    return page_texts


def pdftoppm_tesseract_extract(
    pdf_path: Path,
    output_txt: Path,
    *,
    plans: list[PageOcrPlan] | None = None,
    diagnostics: dict[str, object] | None = None,
) -> tuple[str, str, bool]:
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm_failed: command not found")
    if shutil.which("tesseract") is None:
        raise RuntimeError("tesseract_failed: command not found")
    if not TESSERACT_OCR_LANG:
        raise RuntimeError("tesseract_failed: no usable language installed")

    ocr_diagnostics = diagnostics if diagnostics is not None else {}
    geometries: list[PageGeometry]
    if plans is None:
        geometries = probe_pdf_geometry(pdf_path)
        plans = plan_document_ocr(geometries)
    else:
        geometries = [PageGeometry(plan.page, plan.width_pt, plan.height_pt) for plan in plans]
    ocr_diagnostics.update(plan_diagnostics(plans))

    tmpdir = Path(tempfile.mkdtemp(prefix="zsxq_pdf_ocr_"))
    deadline = time.monotonic() + OCR_DOCUMENT_TIMEOUT_SECONDS
    started_at = time.monotonic()
    try:
        try:
            chunks = execute_pdftoppm_plan(
                pdf_path,
                tmpdir,
                plans,
                deadline=deadline,
                attempt="planned",
                diagnostics=ocr_diagnostics,
            )
        except Exception as exc:
            if not is_geometry_limit_error(str(exc)):
                raise
            fallbacks = list(ocr_diagnostics.get("fallbacks", []))
            fallbacks.append(f"planned_geometry_failed: {safe_tail(str(exc))}")
            ocr_diagnostics["fallbacks"] = fallbacks
            try:
                fallback_plans = plan_document_ocr(
                    geometries,
                    force_safe=True,
                    force_tiled=True,
                )
                fallback_diagnostics = plan_diagnostics(fallback_plans)
                fallback_diagnostics["initial_strategy"] = ocr_diagnostics.get("strategy", "unknown")
                fallback_diagnostics["fallbacks"] = fallbacks
                ocr_diagnostics.update(fallback_diagnostics)
                chunks = execute_pdftoppm_plan(
                    pdf_path,
                    tmpdir,
                    fallback_plans,
                    deadline=deadline,
                    attempt="geometry-fallback",
                    diagnostics=ocr_diagnostics,
                )
                plans = fallback_plans
            except Exception as fallback_exc:
                raise UnsupportedPageGeometryError(
                    "unsupported_page_geometry: adaptive/tiled OCR exhausted; "
                    f"planned={safe_tail(str(exc))}; fallback={safe_tail(str(fallback_exc))}"
                ) from fallback_exc

        text = "\n\n".join(chunks).strip()
        compacted, truncated = sanitize_text(text, TEXT_MAX_CHARS, aggressive=True)
        output_txt.parent.mkdir(parents=True, exist_ok=True)
        output_txt.write_text(compacted + "\n", encoding="utf-8")
        strategies = {plan.strategy for plan in plans}
        if "tiled" in strategies:
            source = "ocr_pdftoppm_tesseract_tiled"
        elif "adaptive" in strategies:
            source = "ocr_pdftoppm_tesseract_adaptive"
        else:
            source = "ocr_pdftoppm_tesseract"
        ocr_diagnostics["duration_ms"] = int((time.monotonic() - started_at) * 1000)
        return compacted, source, truncated
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def local_ocr_extract(
    pdf_path: Path,
    output_txt: Path,
    *,
    diagnostics: dict[str, object] | None = None,
) -> tuple[str, str, bool, list[str]]:
    failures: list[str] = []
    ocr_diagnostics = diagnostics if diagnostics is not None else {}
    plans: list[PageOcrPlan] | None = None

    try:
        geometries = probe_pdf_geometry(pdf_path)
        plans = plan_document_ocr(geometries)
        ocr_diagnostics.update(plan_diagnostics(plans))
    except UnsupportedPageGeometryError as exc:
        ocr_diagnostics["strategy"] = "unsupported"
        ocr_diagnostics["plan_error"] = str(exc)
        raise
    except Exception as exc:
        failures.append(str(exc))
        ocr_diagnostics["geometry_probe_error"] = str(exc)

    nonstandard_geometry = bool(plans) and any(plan.strategy != "standard" for plan in plans)
    if nonstandard_geometry:
        failures.append("ocrmypdf_skipped_nonstandard_geometry")
    else:
        try:
            text, source, truncated = ocrmypdf_extract(pdf_path, output_txt)
            ocr_diagnostics["strategy"] = "ocrmypdf"
            ocr_diagnostics["fallbacks"] = list(failures)
            return text, source, truncated, failures
        except subprocess.TimeoutExpired:
            failures.append(f"ocrmypdf_failed: timeout after {OCR_DOCUMENT_TIMEOUT_SECONDS}s")
        except Exception as exc:
            failures.append(str(exc))

    try:
        ocr_diagnostics["fallbacks"] = list(failures)
        text, source, truncated = pdftoppm_tesseract_extract(
            pdf_path,
            output_txt,
            plans=plans,
            diagnostics=ocr_diagnostics,
        )
        return text, source, truncated, failures
    except Exception as exc:
        failures.append(str(exc))
        if not is_geometry_limit_error(str(exc)):
            raise RuntimeError(str(exc)) from exc

    raise RuntimeError("; ".join(failures))


def set_warning(item: dict, message: str) -> None:
    message = str(message or "").strip()
    if not message:
        return
    existing = str(item.get("text_extract_warning", "")).strip()
    if existing:
        item["text_extract_warning"] = f"{existing}; {message}"
    else:
        item["text_extract_warning"] = message


def finalize_text(
    item: dict,
    txt_path: Path,
    text: str,
    source: str,
    truncated: bool,
    warnings: list[str],
    *,
    cache_key: str,
    cached: bool,
    pdf_path: Path,
    diagnostics: dict[str, object] | None = None,
) -> dict:
    cleaned, clipped = sanitize_text(text, TEXT_MAX_CHARS, aggressive=False)
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(cleaned + "\n", encoding="utf-8")
    item["extracted_text_path"] = str(txt_path.resolve())
    item["extracted_text_chars"] = len(cleaned)
    item["text_source"] = source
    item["text_extract_status"] = "success"
    item["text_extract_cached"] = cached
    item["text_extract_cache_key"] = cache_key
    item["text_extract_retryable"] = False
    item["text_extract_profile"] = EXTRACTOR_PROFILE
    item["text_extract_diagnostics"] = diagnostics or {}
    if truncated or clipped:
        warnings.append(f"text_truncated_to_{TEXT_MAX_CHARS}_chars")
    for warning in warnings:
        set_warning(item, warning)
    if not cached:
        save_success_cache(
            cache_key,
            pdf_path,
            cleaned,
            source,
            warnings,
            diagnostics=diagnostics,
        )
    return item


def ensure_text_for_item(item: dict, output_dir: Path) -> dict:
    prepared_text_path = Path(str(item.get("extracted_text_path", "") or "")).expanduser()
    prepared_source = str(item.get("text_source", "") or "").strip()
    if prepared_source == "markitdown_clean" and prepared_text_path.exists():
        pdf_path = Path(str(item.get("path", ""))).expanduser().resolve()
        try:
            text = prepared_text_path.read_text(encoding="utf-8", errors="replace")
            longest_line = max_line_length(text)
            if longest_line > MAX_TEXT_LINE_CHARS:
                set_warning(
                    item,
                    f"markitdown_clean_format_invalid: max_line_chars={longest_line}, limit={MAX_TEXT_LINE_CHARS}",
                )
            else:
                quality = analyze_text(text)
                markitdown_acceptable = (
                    quality.acceptable
                    or (
                        quality.char_count >= 1800
                        and quality.repeated_ratio < 0.45
                        and quality.watermark_ratio < 0.18
                        and quality.meaningful_lines >= 10
                    )
                )
                if markitdown_acceptable:
                    cache_key = str(item.get("text_extract_cache_key", "") or "").strip()
                    if not cache_key and pdf_path.exists():
                        cache_key = compute_pdf_fingerprint(pdf_path)
                    item["extracted_text_path"] = str(prepared_text_path.resolve())
                    item["extracted_text_chars"] = len(text.strip())
                    item["text_source"] = prepared_source
                    item["text_extract_status"] = "success"
                    item["text_extract_cached"] = False
                    item["text_extract_cache_key"] = cache_key
                    item["text_extract_retryable"] = False
                    item["text_extract_profile"] = EXTRACTOR_PROFILE
                    item["text_extract_diagnostics"] = {"strategy": "markitdown_clean"}
                    return item
                set_warning(item, f"markitdown_clean_low_quality: {quality.summary}")
        except Exception as exc:
            set_warning(item, f"markitdown_clean_unreadable: {exc}")

    reset_item_fields(item)

    pdf_path = Path(str(item.get("path", ""))).expanduser().resolve()
    if not pdf_path.exists():
        return set_failure(
            item,
            f"pdf not found: {pdf_path}",
            error_type="env_failure",
            error_code="pdf_not_found",
            retryable=False,
            warnings=[],
            cache_key="",
        )

    try:
        cache_key = compute_pdf_fingerprint(pdf_path)
    except Exception as exc:
        return set_failure(
            item,
            f"无法计算文件指纹：{exc}",
            error_type="env_failure",
            error_code="fingerprint_failed",
            retryable=False,
            warnings=[],
            cache_key="",
        )

    safe_name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", pdf_path.stem)[:120]
    txt_path = output_dir / f"{safe_name}.txt"
    warnings: list[str] = []

    cached_payload = load_success_cache(cache_key)
    if cached_payload is not None:
        cached_text_path = Path(str(cached_payload.get("text_path", "")).strip()).expanduser()
        if cached_text_path.exists():
            cached_warnings: list[str] = []
            cached_warning_text = str(cached_payload.get("text_extract_warning", "")).strip()
            if cached_warning_text:
                cached_warnings.append(cached_warning_text)
            return finalize_text(
                item=item,
                txt_path=cached_text_path,
                text=cached_text_path.read_text(encoding="utf-8", errors="replace"),
                source=str(cached_payload.get("text_source", "cached_text")).strip() or "cached_text",
                truncated=False,
                warnings=cached_warnings,
                cache_key=cache_key,
                cached=True,
                pdf_path=pdf_path,
                diagnostics=(
                    cached_payload.get("text_extract_diagnostics", {})
                    if isinstance(cached_payload.get("text_extract_diagnostics", {}), dict)
                    else {}
                ),
            )

    cached_failure = load_failure_cache(cache_key)
    if cached_failure is not None:
        cached_warning = str(cached_failure.get("text_extract_warning", "")).strip()
        cached_diagnostics = cached_failure.get("text_extract_diagnostics", {})
        return set_failure(
            item,
            str(cached_failure.get("error", "文本抽取失败：命中确定性失败缓存")),
            error_type=str(cached_failure.get("error_type", "content_failure")),
            error_code=str(cached_failure.get("error_code", "no_usable_text")),
            retryable=False,
            warnings=[cached_warning] if cached_warning else [],
            cache_key=cache_key,
            pdf_path=pdf_path,
            diagnostics=cached_diagnostics if isinstance(cached_diagnostics, dict) else {},
            cached=True,
        )

    direct_raw, direct_error = direct_extract(pdf_path)
    if direct_error:
        warnings.append(direct_error)
    direct_quality = analyze_text(direct_raw)
    direct_clean, direct_truncated = sanitize_text(direct_raw, TEXT_MAX_CHARS, aggressive=False)

    if direct_quality.acceptable and direct_clean:
        return finalize_text(
            item=item,
            txt_path=txt_path,
            text=direct_clean,
            source="pdftotext_fastpath",
            truncated=direct_truncated,
            warnings=warnings,
            cache_key=cache_key,
            cached=False,
            pdf_path=pdf_path,
            diagnostics={"strategy": "pdftotext", "extractor_profile": EXTRACTOR_PROFILE},
        )

    warnings.append(f"direct_probe_low_quality: {direct_quality.summary}")

    if not LOCAL_OCR_FALLBACK_ENABLE:
        return set_failure(
            item,
            "文本抽取失败：pdftotext 结果质量不足，且本地 OCR 已关闭",
            error_type="env_failure",
            error_code="ocr_fallback_disabled",
            retryable=False,
            warnings=warnings,
            cache_key=cache_key,
        )

    ocr_failures: list[str] = []
    ocr_diagnostics: dict[str, object] = {}
    try:
        ocr_text, ocr_source, ocr_truncated, ocr_warnings = local_ocr_extract(
            pdf_path,
            txt_path,
            diagnostics=ocr_diagnostics,
        )
        warnings.extend(ocr_warnings)
        ocr_quality = analyze_text(ocr_text)
        if ocr_quality.acceptable and ocr_text.strip():
            return finalize_text(
                item=item,
                txt_path=txt_path,
                text=ocr_text,
                source=ocr_source,
                truncated=ocr_truncated,
                warnings=warnings,
                cache_key=cache_key,
                cached=False,
                pdf_path=pdf_path,
                diagnostics=ocr_diagnostics,
            )
        warnings.append(f"ocr_output_low_quality: {ocr_quality.summary}")
    except Exception as exc:
        ocr_failures.append(str(exc))
        warnings.append(str(exc))

    if direct_clean and direct_quality.char_count >= 1200 and direct_quality.meaningful_lines >= 8 and direct_quality.repeated_ratio < 0.50 and direct_quality.watermark_ratio < 0.20:
        warnings.append("fallback_to_direct_text_after_ocr_failure")
        return finalize_text(
            item=item,
            txt_path=txt_path,
            text=direct_clean,
            source="pdftotext_degraded_fallback",
            truncated=direct_truncated,
            warnings=warnings,
            cache_key=cache_key,
            cached=False,
            pdf_path=pdf_path,
            diagnostics={"strategy": "pdftotext_degraded", "ocr": ocr_diagnostics},
        )

    classified = classify_runtime_failure([direct_error, *ocr_failures])
    if classified is not None:
        error_type, error_code, retryable = classified
        if error_type == "env_failure":
            error_message = "文本抽取失败：系统环境异常，OCR fallback 未能正常运行"
        elif error_type == "transient_failure":
            error_message = "文本抽取失败：OCR 子流程异常中断，当前结果不可靠"
        else:
            error_message = "文本抽取失败：PDF 内容结构异常，OCR fallback 未恢复出可读正文"
        return set_failure(
            item,
            error_message,
            error_type=error_type,
            error_code=error_code,
            retryable=retryable,
            warnings=warnings,
            cache_key=cache_key,
            pdf_path=pdf_path,
            diagnostics=ocr_diagnostics,
        )

    return set_failure(
        item,
        "文本抽取失败：pdftotext 低质量，OCR 未拿到可用正文",
        error_type="content_failure",
        error_code="no_usable_text",
        retryable=False,
        warnings=warnings,
        cache_key=cache_key,
        pdf_path=pdf_path,
        diagnostics=ocr_diagnostics,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-file", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    if args.preflight_only:
        report = build_preflight_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if bool(report.get("ok", False)) else 1

    if not args.batch_file or not args.output_dir:
        raise SystemExit("--batch-file and --output-dir are required unless --preflight-only is used")

    batch_path = Path(args.batch_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(batch_path.read_text(encoding="utf-8"))
    files = data.get("files", [])
    if not isinstance(files, list):
        raise SystemExit("invalid batch json: files must be a list")

    updated = [ensure_text_for_item(dict(item), output_dir) for item in files]
    data["files"] = updated
    batch_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"batch_file": str(batch_path), "count": len(updated)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
