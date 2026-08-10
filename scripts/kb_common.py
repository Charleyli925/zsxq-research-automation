#!/usr/bin/env python3
"""Shared helpers for the local research knowledge base."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

try:
    from runtime_paths import (
        DEFAULT_CONFIG_ROOT,
        DEFAULT_LIBRARY_ROOT,
        DEFAULT_VAULT_ROOT,
    )
except ModuleNotFoundError:  # pragma: no cover
    from scripts.runtime_paths import (
        DEFAULT_CONFIG_ROOT,
        DEFAULT_LIBRARY_ROOT,
        DEFAULT_VAULT_ROOT,
    )

DEFAULT_DB_PATH = DEFAULT_LIBRARY_ROOT / "state" / "processed_files.sqlite"

REPORT_DIR_NAME = "10_Reports"
THEME_DIR_NAME = "20_Themes"
COMPANY_DIR_NAME = "30_Companies"

METADATA_FIELDS = [
    "broker",
    "report_date",
    "companies",
    "tickers",
    "themes",
    "subthemes",
    "regions",
    "industries",
    "key_numbers",
    "ratings",
    "target_prices",
    "risks",
    "catalysts",
    "report_type",
    "company_scope",
    "quality_status",
    "retrieval_status",
]

LIST_FIELDS = {
    "companies",
    "tickers",
    "themes",
    "subthemes",
    "regions",
    "industries",
    "key_numbers",
    "ratings",
    "target_prices",
    "risks",
    "catalysts",
    "quality_status",
    "retrieval_status",
}

NOISE_PATTERNS = [
    re.compile(r"file:///\S+", re.I),
    re.compile(r"pdf_path\s*:\s*\S+", re.I),
    re.compile(r"raw_md_path\s*:\s*\S+", re.I),
    re.compile(r"clean_md_path\s*:\s*\S+", re.I),
    re.compile(r"summary_md_path\s*:\s*\S+", re.I),
    re.compile(r"(?:PDF|Raw Markdown|Clean Markdown|Summary Markdown)\s*:\s*\[[^\]]+\]\([^)]*\)", re.I),
]

BROKER_ALIASES = {
    "高盛": ["高盛", "Goldman", "Goldman Sachs", "GS"],
    "摩根士丹利": ["摩根士丹利", "Morgan Stanley", "MS"],
    "摩根大通": ["摩根大通", "J.P. Morgan", "JPMorgan", "JPM"],
    "美银证券": ["美银", "美银证券", "Bank of America", "BofA", "BAML"],
    "瑞银": ["瑞银", "UBS"],
    "花旗": ["花旗", "Citi", "Citigroup"],
    "中金公司": ["中金", "中金公司", "CICC"],
    "伯恩斯坦": ["伯恩斯坦", "Bernstein"],
    "巴克莱": ["巴克莱", "Barclays"],
    "野村": ["野村", "Nomura"],
    "汇丰": ["汇丰", "HSBC"],
    "德意志银行": ["德意志", "Deutsche"],
    "杰富瑞": ["杰富瑞", "Jefferies"],
    "Semianalysis": ["Semianalysis", "SemiAnalysis"],
    "TMT Breakout": ["TMTB", "TMT Breakout"],
}


@dataclass(frozen=True)
class NoteRecord:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    title: str
    display_title: str
    link_target: str


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text_if_changed(path: Path, text: str) -> bool:
    old = read_text(path) if path.exists() else ""
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not match:
        return {}, text
    raw = match.group(1)
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, text[match.end() :]


def render_frontmatter(frontmatter: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    ).strip()
    return f"---\n{dumped}\n---\n\n{body.lstrip()}"


def first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def first_summary_heading(body: str) -> str:
    match = re.search(r"^##\s*摘要\s*$", body, re.M)
    if not match:
        return ""
    for line in body[match.end() :].splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def clean_alias(value: str) -> str:
    return " ".join(str(value or "").replace("|", "-").split())


def display_title_for_note(title: str, body: str) -> str:
    if "�" not in title:
        return title
    summary_title = first_summary_heading(body)
    if summary_title and "�" not in summary_title:
        return summary_title
    return title


def obsidian_target(note_path: Path, vault_root: Path) -> str:
    relative = note_path.resolve(strict=False).relative_to(vault_root.resolve(strict=False))
    if relative.suffix == ".md":
        relative = relative.with_suffix("")
    return relative.as_posix()


def obsidian_link(note: NoteRecord) -> str:
    return f"[[{note.link_target}|{clean_alias(note.display_title)}]]"


def load_note(note_path: Path, vault_root: Path) -> NoteRecord:
    text = read_text(note_path)
    frontmatter, body = split_frontmatter(text)
    title = str(frontmatter.get("title") or first_heading(body) or note_path.stem).strip()
    return NoteRecord(
        path=note_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        display_title=display_title_for_note(title, body),
        link_target=obsidian_target(note_path, vault_root),
    )


def load_report_notes(vault_root: Path) -> list[NoteRecord]:
    report_root = vault_root / REPORT_DIR_NAME
    notes: list[NoteRecord] = []
    for path in sorted(report_root.rglob("*.md")):
        try:
            note = load_note(path, vault_root)
        except ValueError:
            continue
        if note.link_target.startswith(f"{REPORT_DIR_NAME}/"):
            notes.append(note)
    return notes


def path_from_uri(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return raw


def file_uri(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    if value.startswith("file://"):
        return value
    return Path(value).expanduser().resolve(strict=False).as_uri()


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(read_text(path)) or {}
    return data if isinstance(data, dict) else {}


def load_kb_configs(config_root: Path = DEFAULT_CONFIG_ROOT) -> dict[str, Any]:
    return {
        "schema": load_yaml_file(config_root / "report_metadata_schema.yml"),
        "entities": load_yaml_file(config_root / "entities.yml"),
        "themes": load_yaml_file(config_root / "themes.yml"),
    }


def contains_keyword(text: str, keyword: str) -> bool:
    keyword = str(keyword or "").strip()
    if not keyword:
        return False
    lower_text = text.lower()
    lower_keyword = keyword.lower()
    if re.search(r"[a-z0-9]", lower_keyword):
        return re.search(rf"(?<![a-z0-9]){re.escape(lower_keyword)}(?![a-z0-9])", lower_text) is not None
    return lower_keyword in lower_text


def count_keyword_mentions(text: str, keywords: list[str]) -> int:
    count = 0
    lower_text = text.lower()
    for keyword in keywords:
        keyword = str(keyword or "").strip()
        if not keyword:
            continue
        lower_keyword = keyword.lower()
        if re.search(r"[a-z0-9]", lower_keyword):
            count += len(re.findall(rf"(?<![a-z0-9]){re.escape(lower_keyword)}(?![a-z0-9])", lower_text))
        else:
            count += lower_text.count(lower_keyword)
    return count


ENTITY_ALIAS_FALSE_FOLLOWERS = {
    "高通": {"胀", "膨", "量", "道"},
}


def count_entity_alias_mentions(text: str, aliases: list[str]) -> int:
    count = 0
    lower_text = text.lower()
    for alias in aliases:
        alias = str(alias or "").strip()
        if not alias:
            continue
        lower_alias = alias.lower()
        if re.search(r"[a-z0-9]", lower_alias):
            count += len(re.findall(rf"(?<![a-z0-9]){re.escape(lower_alias)}(?![a-z0-9])", lower_text))
            continue
        start = 0
        while True:
            index = lower_text.find(lower_alias, start)
            if index < 0:
                break
            end = index + len(lower_alias)
            next_char = text[end : end + 1]
            if next_char not in ENTITY_ALIAS_FALSE_FOLLOWERS.get(alias, set()):
                count += 1
            start = end
    return count


def parse_alias_line(text: str, page_stem: str) -> list[str]:
    aliases = [page_stem]
    match = re.search(r"^##\s*别名\s*$", text, re.M)
    if not match:
        return aliases
    next_match = re.search(r"^##\s+", text[match.end() :], re.M)
    section_end = match.end() + next_match.start() if next_match else len(text)
    section = text[match.end() : section_end]
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        for item in re.split(r"[、,，/]", stripped[2:]):
            alias = item.strip()
            if alias and alias not in aliases:
                aliases.append(alias)
    return aliases


def entity_rules(configs: dict[str, Any], vault_root: Path | None = None) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for company in (configs.get("entities", {}).get("companies") or []):
        if not isinstance(company, dict):
            continue
        name = str(company.get("name") or "").strip()
        if not name:
            continue
        aliases = [name, *[str(item) for item in company.get("aliases", []) or []]]
        rules[name] = {
            "aliases": list(dict.fromkeys(alias for alias in aliases if alias)),
            "tickers": [str(item) for item in company.get("tickers", []) or []],
            "regions": [str(item) for item in company.get("regions", []) or []],
            "industries": [str(item) for item in company.get("industries", []) or []],
        }
    if vault_root is not None:
        company_dir = vault_root / COMPANY_DIR_NAME
        for page_path in sorted(company_dir.glob("*.md")):
            if page_path.name == "公司索引.md":
                continue
            aliases = parse_alias_line(read_text(page_path), page_path.stem)
            item = rules.setdefault(page_path.stem, {"aliases": [], "tickers": [], "regions": [], "industries": []})
            item["aliases"] = list(dict.fromkeys([*item.get("aliases", []), *aliases]))
    return rules


def theme_rules(configs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for theme in (configs.get("themes", {}).get("themes") or []):
        if not isinstance(theme, dict):
            continue
        name = str(theme.get("name") or "").strip()
        if not name:
            continue
        subthemes: dict[str, list[str]] = {}
        for subtheme in theme.get("subthemes", []) or []:
            if not isinstance(subtheme, dict):
                continue
            sub_name = str(subtheme.get("name") or "").strip()
            if sub_name:
                subthemes[sub_name] = [str(item) for item in subtheme.get("keywords", []) or []]
        rules[name] = {
            "keywords": [str(item) for item in theme.get("keywords", []) or []],
            "subthemes": subthemes,
            "regions": [str(item) for item in theme.get("regions", []) or []],
            "industries": [str(item) for item in theme.get("industries", []) or []],
        }
    return rules


def extract_broker(text: str) -> str:
    head = text[:500]
    for broker, aliases in BROKER_ALIASES.items():
        if any(contains_keyword(head, alias) for alias in aliases):
            return broker
    return ""


def normalize_report_date(year: str, month: str, day: str) -> str:
    try:
        parsed = datetime(int(year), int(month), int(day))
    except ValueError:
        return ""
    current_year = datetime.now().astimezone().year
    if parsed.year < 2000 or parsed.year > current_year + 1:
        return ""
    return parsed.date().isoformat()


def extract_report_date(frontmatter: dict[str, Any], text: str, note_path: Path | None = None) -> str:
    raw = str(frontmatter.get("report_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) and normalize_report_date(raw[:4], raw[5:7], raw[8:10]):
        return raw
    explicit_candidates = [text[:500]]
    fallback_candidates: list[str] = []
    if note_path is not None:
        explicit_candidates.append(note_path.name)
        fallback_candidates.append(note_path.parent.name)
    for haystack in ("\n".join(explicit_candidates), "\n".join(fallback_candidates)):
        if not haystack:
            continue
        match = re.search(r"(?<!\d)(20\d{2})[-./年]?(0[1-9]|1[0-2])[-./月]?(0[1-9]|[12]\d|3[01])", haystack)
        if match:
            normalized = normalize_report_date(match.group(1), match.group(2), match.group(3))
            if normalized:
                return normalized
        match = re.search(r"(?<!\d)(2\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", haystack)
        if match:
            year = 2000 + int(match.group(1))
            normalized = normalize_report_date(str(year), match.group(2), match.group(3))
            if normalized:
                return normalized
    for key in ("downloaded_at", "created", "updated"):
        value = str(frontmatter.get(key) or "").strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return ""


def text_for_matching(note: NoteRecord, max_body_chars: int = 20000) -> tuple[str, str, str]:
    title_text = f"{note.title}\n{note.display_title}"
    summary_text = extract_note_summary_text(note)
    body_text = summary_text[:max_body_chars] if summary_text else note.body[:max_body_chars]
    return title_text, body_text, f"{title_text}\n{body_text}"


def extract_summary_text(body: str) -> str:
    match = re.search(r"^##\s*摘要\s*$", body, re.M)
    if match:
        return body[match.end() :].strip()
    return body.strip()


def compact_title_for_match(value: str) -> str:
    text = clean_search_text(value).lower()
    text = re.sub(r"\.(pdf|md)$", "", text)
    text = re.sub(r"(?:带图片|页眉页脚|加密|副本).*$", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def report_section_matches(summary: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^##\s*报告\d+\s*[：:]\s*(.+?)\s*$", summary, re.M))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(summary)
        sections.append((match.group(1), summary[match.start() : section_end].strip()))
    return sections


def title_match_score(target: str, candidate: str) -> float:
    target_norm = compact_title_for_match(target)
    candidate_norm = compact_title_for_match(candidate)
    if not target_norm or not candidate_norm:
        return 0.0
    if target_norm in candidate_norm or candidate_norm in target_norm:
        return 1.0
    target_chars = set(target_norm)
    candidate_chars = set(candidate_norm)
    char_score = len(target_chars & candidate_chars) / max(len(target_chars), 1)
    target_grams = {target_norm[index : index + 3] for index in range(0, max(len(target_norm) - 2, 0))}
    candidate_grams = {candidate_norm[index : index + 3] for index in range(0, max(len(candidate_norm) - 2, 0))}
    gram_score = len(target_grams & candidate_grams) / max(len(target_grams), 1) if target_grams else 0.0
    return char_score * 0.35 + gram_score * 0.65


def extract_note_summary_text(note: NoteRecord) -> str:
    summary = extract_summary_text(note.body)
    sections = report_section_matches(summary)
    if not sections:
        return summary
    candidates = [(title_match_score(note.display_title or note.title, heading), section) for heading, section in sections]
    best_score, best_section = max(candidates, key=lambda item: item[0])
    return best_section if best_score >= 0.35 else summary


def extract_section(text: str, heading_pattern: str) -> str:
    match = re.search(heading_pattern, text, re.M)
    if not match:
        return ""
    next_match = re.search(r"^##\s+", text[match.end() :], re.M)
    section_end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end() : section_end].strip()


def clean_search_text(text: str) -> str:
    cleaned = str(text or "")
    for pattern in NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_list_value(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in re.split(r"[、,，;；]", stripped) if item.strip()]
    return [str(value).strip()]


def existing_or_extracted(frontmatter: dict[str, Any], key: str, extracted: Any) -> Any:
    current = frontmatter.get(key)
    if key in LIST_FIELDS:
        current_list = extract_list_value(current)
        if current_list:
            return current_list
        return extracted
    if current not in (None, ""):
        return current
    return extracted


NON_COMPANY_TITLE_TOKENS = {
    "AI",
    "AIDC",
    "ASIC",
    "CPO",
    "CPU",
    "ETF",
    "GPU",
    "HBM",
    "HPC",
    "LLM",
    "MLCC",
    "ODM",
    "PCB",
    "PMI",
    "TPU",
}


def strip_company_title_prefix(value: str) -> str:
    candidate = clean_search_text(value).strip(" _-—:：()（）[]【】")
    candidate = re.sub(r"^(?:20)?\d{6}[-_ ]+", "", candidate)
    candidate = re.sub(r"^(?:GS|MS|JPM|UBS|CITI|BofA|Nomura|Bernstein)[-_ ]+", "", candidate, flags=re.I)
    broker_aliases = sorted({alias for aliases in BROKER_ALIASES.values() for alias in aliases}, key=len, reverse=True)
    for alias in broker_aliases:
        alias = str(alias or "").strip()
        if alias and candidate.startswith(alias):
            candidate = candidate[len(alias) :].lstrip(" _-—:：")
            break
    return candidate.strip(" _-—:：()（）[]【】")


def clean_inferred_company(value: str) -> str:
    candidate = clean_search_text(value)
    candidate = strip_company_title_prefix(candidate)
    if re.match(r"^(?:20)?\d{6}[-_ ]", candidate):
        candidate = re.sub(r"^(?:20)?\d{6}[-_ ]+", "", candidate)
    candidate = re.sub(r"[~～][A-Za-z]$", "", candidate)
    candidate = re.sub(r"(?<=[\u4e00-\u9fff])[-_][A-Z]$", "", candidate)
    candidate = re.sub(r"(?:Next\s*)?20\d{2}$", "", candidate, flags=re.I)
    candidate = re.sub(r"(?:[1-4]Q\d{2}|[一二三四]季度|\d+月季度)$", "", candidate)
    candidate = candidate.strip(" _-—:：()（）[]【】")
    generic_terms = [
        "行业",
        "板块",
        "策略",
        "宏观",
        "周报",
        "月报",
        "日报",
        "市场",
        "经济",
        "报告",
        "更新",
        "追踪",
        "跟踪",
        "观点",
        "洞察",
        "月度",
        "估值",
    ]
    generic_exact = {
        "互联网",
        "半导体",
        "光模块",
        "人工智能",
        "AI",
        "科技",
        "软件",
        "银行",
        "全球软件",
        "全球半导体",
        "全球AI光模块",
        "AI半导体与科技",
        "全球超大规模企业",
    }
    if len(candidate) < 2 or len(candidate) > 30:
        return ""
    if candidate in generic_exact:
        return ""
    if any(term in candidate for term in generic_terms):
        return ""
    market_prefixes = ("中国", "美国", "全球", "亚洲", "亚太", "北美", "欧洲", "日本", "韩国", "台湾", "香港", "大中华", "大中华区", "AI")
    sector_heads = (
        "互联网",
        "半导体",
        "模拟半导体",
        "科技",
        "软件",
        "光模块",
        "能源",
        "企业",
        "黄金",
        "房地产",
        "消费",
        "汽车",
        "航空公司",
        "航空",
        "白酒",
        "智能手机",
        "医疗",
        "医药",
        "银行业",
        "数据中心",
        "外汇",
        "油气",
        "油轮",
        "本地市场",
        "生活必需品",
    )
    company_suffixes = ("有限公司", "控股", "股份", "电子", "半导体", "集团", "银行", "保险", "药业", "生物")
    if candidate.startswith(market_prefixes) and any(head in candidate for head in sector_heads):
        if not candidate.endswith(company_suffixes):
            return ""
    if re.fullmatch(r"[A-Z0-9._ -]+", candidate):
        token = candidate.strip().upper().replace(" ", "")
        if token in NON_COMPANY_TITLE_TOKENS or len(token) > 8:
            return ""
    return candidate


def strong_company_name_candidate(value: str) -> bool:
    return bool(re.search(r"(科技|股份|电子|半导体|药业|银行|控股|集团|公司|医药|生物|电缆|技术)$", value))


def infer_companies_from_title(title: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·~～-]{1,50})[（(][A-Za-z0-9]{1,10}(?:[.\-][A-Za-z0-9]{1,6})?[）)]",
        r"[：:]\s*([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·~～-]{1,40})[（(][A-Za-z0-9]{1,10}(?:[.\-][A-Za-z0-9]{1,6})?[）)]",
        r"[-–—]([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·-]{1,50})[-_（(](?:\d{4,6}|[A-Za-z]{1,8})(?:[.\-](?:US|HK|SH|SZ|TW|KS|TWO|DE|O|N|SS|T))",
        r"[-–—]([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·-]{1,40})[-–—](?:\d{4,6})[-–—]",
        r"[-–—]([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·-]{1,50})[-–—]([A-Z]{2,8})[-–—]",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, title):
            company = clean_inferred_company(match.group(1))
            if company:
                candidates.append(company)
    signal_terms = (
        "一季度",
        "二季度",
        "三季度",
        "四季度",
        "业绩",
        "财报",
        "营收",
        "利润",
        "毛利率",
        "盈利",
        "评级",
        "买入",
        "超配",
        "中性",
        "目标价",
        "首次覆盖",
        "初评",
        "重申",
        "维持",
        "下调",
        "上调",
        "展望",
        "跌幅",
        "预览",
        "回顾",
        "电话会",
        "要点",
        "订单",
        "需求",
        "发布",
        "增长",
        "受益",
        "支撑",
        "承压",
        "低于",
        "高增",
        "放量",
        "销售",
        "收入",
        "业务",
        "龙头",
        "增量",
        "上市",
        "回购",
        "领跑",
    )
    prefix = strip_company_title_prefix(title)
    colon_match = re.match(r"^([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff& .·~～-]{1,24})[:：](.{0,120})", prefix)
    if colon_match and (any(term in colon_match.group(2) for term in signal_terms) or re.search(r"20\d{2}年", colon_match.group(2))):
        company = clean_inferred_company(colon_match.group(1))
        if company and (strong_company_name_candidate(company) or any(term in colon_match.group(2) for term in signal_terms)):
            candidates.append(company)
    start_match = re.match(
        r"^([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff .·~～-]{1,24})(?:20\d{2}年|1Q\d{2}|2Q\d{2}|3Q\d{2}|4Q\d{2}|"
        + "|".join(re.escape(term) for term in signal_terms)
        + r")",
        prefix,
    )
    if start_match:
        company = clean_inferred_company(start_match.group(1))
        if company:
            candidates.append(company)
    return list(dict.fromkeys(candidates))


def candidate_aliases_from_title(title: str) -> list[str]:
    """Return conservative candidates for the alias review queue."""
    candidates: list[str] = []
    prefix = strip_company_title_prefix(title)
    colon_match = re.match(r"^([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff& .·~～-]{1,24})[:：]", prefix)
    if colon_match:
        company = clean_inferred_company(colon_match.group(1))
        if company and (
            strong_company_name_candidate(company)
            or re.fullmatch(r"[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)*", company)
            or re.fullmatch(r"[A-Z]{2,8}", company)
        ):
            candidates.append(company)
    for match in re.finditer(r"[-–—]([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff& .·-]{1,40})[-–—](?:\d{4,6})[-–—]", title):
        company = clean_inferred_company(match.group(1))
        if company:
            candidates.append(company)
    return list(dict.fromkeys(candidates))


def extract_entities(note: NoteRecord, configs: dict[str, Any], vault_root: Path | None = None) -> dict[str, list[str]]:
    title_text, body_text, all_text = text_for_matching(note)
    rules = entity_rules(configs, vault_root)
    matched: list[tuple[str, int]] = []
    tickers: list[str] = []
    regions: list[str] = []
    industries: list[str] = []
    for name, rule in rules.items():
        aliases = rule.get("aliases", [])
        title_hits = count_entity_alias_mentions(title_text, aliases)
        body_hits = count_entity_alias_mentions(body_text, aliases)
        score = title_hits * 4 + body_hits
        if title_hits > 0 or body_hits >= 2:
            matched.append((name, score))
            tickers.extend(rule.get("tickers", []))
            regions.extend(rule.get("regions", []))
            industries.extend(rule.get("industries", []))
    matched_names = {name for name, _ in matched}
    for company in infer_companies_from_title(note.title):
        if any(company in (rules.get(name, {}).get("aliases") or []) for name in matched_names):
            continue
        if company not in matched_names:
            matched.append((company, 3))
            matched_names.add(company)
    matched.sort(key=lambda item: (-item[1], item[0]))
    return {
        "companies": [name for name, _ in matched[:12]],
        "tickers": list(dict.fromkeys(tickers)),
        "regions": list(dict.fromkeys(regions)),
        "industries": list(dict.fromkeys(industries)),
    }


def extract_themes(note: NoteRecord, configs: dict[str, Any]) -> dict[str, list[str]]:
    title_text, body_text, _ = text_for_matching(note)
    rules = theme_rules(configs)
    theme_scores: list[tuple[str, int]] = []
    subtheme_scores: list[tuple[str, int]] = []
    regions: list[str] = []
    industries: list[str] = []
    for name, rule in rules.items():
        keywords = rule.get("keywords", [])
        score = count_keyword_mentions(title_text, keywords) * 4 + count_keyword_mentions(body_text, keywords)
        for subtheme, sub_keywords in (rule.get("subthemes") or {}).items():
            sub_score = count_keyword_mentions(title_text, sub_keywords) * 4 + count_keyword_mentions(body_text, sub_keywords)
            if sub_score > 0:
                score += sub_score
                subtheme_scores.append((subtheme, sub_score))
        if score >= 2 or count_keyword_mentions(title_text, keywords) > 0:
            theme_scores.append((name, score))
            regions.extend(rule.get("regions", []))
            industries.extend(rule.get("industries", []))
    theme_scores.sort(key=lambda item: (-item[1], item[0]))
    subtheme_scores.sort(key=lambda item: (-item[1], item[0]))
    return {
        "themes": [name for name, _ in theme_scores[:8]],
        "subthemes": list(dict.fromkeys([name for name, _ in subtheme_scores[:12]])),
        "regions": list(dict.fromkeys(regions)),
        "industries": list(dict.fromkeys(industries)),
    }


def extract_key_numbers(text: str, limit: int = 12) -> list[str]:
    number_re = re.compile(
        r"(?:(?:\d+(?:\.\d+)?\s?(?:%|亿美元|亿元|万亿|GWh|GW|MW|美元|人民币|倍|bps|bp|页|个|家|万吨|美元/桶))|(?:\$\s?\d+(?:\.\d+)?\s?[BMK]?))"
    )
    results: list[str] = []
    for match in number_re.finditer(text[:20000]):
        start = max(0, match.start() - 24)
        end = min(len(text), match.end() + 24)
        snippet = clean_search_text(text[start:end])
        if snippet and snippet not in results:
            results.append(snippet)
        if len(results) >= limit:
            break
    return results


def extract_bullet_like(text: str, keywords: list[str], limit: int = 8) -> list[str]:
    results: list[str] = []
    for line in text.splitlines():
        stripped = line.strip("-* \t")
        if not stripped:
            continue
        if any(keyword in stripped for keyword in keywords):
            cleaned = clean_search_text(stripped)
            if cleaned and cleaned not in results:
                results.append(cleaned[:220])
        if len(results) >= limit:
            break
    return results


def infer_report_type(note: NoteRecord, metadata: dict[str, Any]) -> str:
    if metadata.get("companies"):
        return "company"
    title_text, body_text, _ = text_for_matching(note)
    head = f"{title_text}\n{body_text[:3000]}"
    if count_keyword_mentions(
        head,
        [
            "宏观",
            "经济",
            "CPI",
            "PPI",
            "PMI",
            "GDP",
            "通胀",
            "利率",
            "美联储",
            "央行",
            "财政",
            "信贷",
            "外汇",
            "汇率",
            "非农",
            "关税",
        ],
    ) >= 2:
        return "macro"
    if count_keyword_mentions(
        head,
        [
            "策略",
            "周报",
            "日报",
            "月报",
            "市场",
            "股市",
            "资金流",
            "仓位",
            "基金经理调查",
            "投资者",
            "全球市场",
        ],
    ) >= 2:
        return "strategy"
    if count_keyword_mentions(
        head,
        [
            "行业",
            "板块",
            "半导体",
            "科技",
            "房地产",
            "医药",
            "医疗",
            "生物科技",
            "油气",
            "能源",
            "电力",
            "消费",
            "银行",
            "保险",
            "汽车",
            "航空",
            "机械",
            "矿业",
            "黄金",
            "软件",
            "硬件",
        ],
    ) >= 2:
        return "industry"
    if count_keyword_mentions(head, ["会议", "峰会", "专家", "访谈", "调研", "考察", "入门", "问答", "FAQ"]) >= 2:
        return "event"
    return "unknown"


def infer_company_scope(metadata: dict[str, Any]) -> str:
    if metadata.get("companies"):
        return "matched"
    if metadata.get("report_type") in {"macro", "strategy", "industry", "event"}:
        return "not_applicable"
    return "needs_entity"


def infer_quality_status(note: NoteRecord, metadata: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    if str(note.frontmatter.get("status") or ""):
        statuses.append(str(note.frontmatter.get("status")))
    if "�" in note.title or "�" in note.path.name:
        statuses.append("dirty_title")
    if not metadata.get("companies") and metadata.get("company_scope") == "needs_entity":
        statuses.append("unmatched_company")
    if not metadata.get("companies") and metadata.get("company_scope") == "not_applicable":
        statuses.append("company_not_applicable")
    if not metadata.get("themes"):
        statuses.append("unmatched_theme")
    if metadata.get("metadata_confidence", 0) < 0.65:
        statuses.append("metadata_low_confidence")
    if not statuses:
        statuses.append("ok")
    return list(dict.fromkeys(statuses))


def infer_retrieval_status(note: NoteRecord) -> list[str]:
    statuses: list[str] = []
    fm = note.frontmatter
    if not path_from_uri(str(fm.get("pdf_path") or "")):
        statuses.append("missing_pdf")
    if not path_from_uri(str(fm.get("summary_md_path") or "")):
        statuses.append("missing_summary")
    if not path_from_uri(str(fm.get("raw_md_path") or "")):
        statuses.append("missing_raw")
    if not path_from_uri(str(fm.get("clean_md_path") or "")):
        statuses.append("missing_clean")
    if "summary_failed" in str(fm.get("status") or ""):
        statuses.append("summary_failed")
    if not statuses:
        statuses.append("searchable")
    return statuses


def metadata_confidence(metadata: dict[str, Any]) -> float:
    score = 0.0
    total = 4.0
    if metadata.get("broker"):
        score += 1.0
    if metadata.get("report_date"):
        score += 1.0
    if metadata.get("themes"):
        score += 1.0
    if metadata.get("companies") or metadata.get("company_scope") == "not_applicable":
        score += 1.0
    return round(score / total, 2)


def extract_report_metadata(note: NoteRecord, configs: dict[str, Any], vault_root: Path | None = None) -> dict[str, Any]:
    title_text, body_text, all_text = text_for_matching(note)
    entity_data = extract_entities(note, configs, vault_root)
    theme_data = extract_themes(note, configs)
    summary_text = extract_note_summary_text(note)
    core_conclusions = extract_section(summary_text, r"^##\s*核心结论\s*$")
    core_qa = extract_section(summary_text, r"^##\s*核心问题与回答\s*$")

    regions = list(dict.fromkeys([*entity_data["regions"], *theme_data["regions"]]))
    industries = list(dict.fromkeys([*entity_data["industries"], *theme_data["industries"]]))
    metadata: dict[str, Any] = {
        "broker": extract_broker(all_text),
        "report_date": extract_report_date(note.frontmatter, all_text, note.path),
        "companies": entity_data["companies"],
        "tickers": entity_data["tickers"],
        "themes": theme_data["themes"],
        "subthemes": theme_data["subthemes"],
        "regions": regions,
        "industries": industries,
        "key_numbers": extract_key_numbers(summary_text or body_text),
        "ratings": extract_bullet_like(summary_text, ["买入", "超配", "中性", "减持", "卖出", "Buy", "Neutral", "Overweight"]),
        "target_prices": extract_bullet_like(summary_text, ["目标价", "target price", "TP", "上调至", "下调至"]),
        "risks": extract_bullet_like(summary_text, ["风险", "risk", "不及预期", "低于预期"]),
        "catalysts": extract_bullet_like(summary_text, ["催化", "catalyst", "驱动", "受益", "上调"]),
        "core_conclusions": clean_search_text(core_conclusions),
        "core_questions_answers": clean_search_text(core_qa),
        "summary_text": clean_search_text(summary_text),
    }
    metadata["report_type"] = infer_report_type(note, metadata)
    metadata["company_scope"] = infer_company_scope(metadata)
    metadata["metadata_confidence"] = metadata_confidence(metadata)
    metadata["quality_status"] = infer_quality_status(note, metadata)
    metadata["retrieval_status"] = infer_retrieval_status(note)
    metadata["metadata_status"] = "metadata_low_confidence" if metadata["metadata_confidence"] < 0.65 else "metadata_ready"
    return metadata


def merge_metadata_frontmatter(
    frontmatter: dict[str, Any],
    metadata: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    merged = dict(frontmatter)
    changed: list[str] = []
    for key in METADATA_FIELDS:
        if key not in metadata:
            continue
        value = metadata[key]
        if key in LIST_FIELDS:
            value = extract_list_value(value)
            current = extract_list_value(merged.get(key))
            should_write = force or not current
        else:
            current = str(merged.get(key) or "").strip()
            should_write = force or not current
        if should_write:
            merged[key] = value
            changed.append(key)
    for key in ("metadata_confidence", "metadata_status"):
        value = metadata.get(key)
        if force or str(merged.get(key) or "").strip() == "":
            merged[key] = value
            changed.append(key)
    return merged, changed


def stable_report_id(note: NoteRecord) -> str:
    report_id = str(note.frontmatter.get("report_id") or "").strip()
    if report_id:
        return report_id
    pdf_path = str(note.frontmatter.get("pdf_path") or "").strip()
    if pdf_path:
        digest = hashlib.sha256(pdf_path.encode("utf-8")).hexdigest()
        return f"local_{digest[:16]}"
    digest = hashlib.sha256(str(note.path).encode("utf-8")).hexdigest()
    return f"note_{digest[:16]}"


def json_list(value: Any) -> str:
    return json.dumps(extract_list_value(value), ensure_ascii=False)


def ensure_kb_schema(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db_path))) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                  report_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL DEFAULT 'zsxq',
                  source_url TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL DEFAULT '',
                  batch_id TEXT NOT NULL DEFAULT '',
                  pdf_path TEXT NOT NULL DEFAULT '',
                  pdf_sha256 TEXT NOT NULL DEFAULT '',
                  downloaded_at TEXT NOT NULL DEFAULT '',
                  raw_md_path TEXT NOT NULL DEFAULT '',
                  clean_md_path TEXT NOT NULL DEFAULT '',
                  summary_md_path TEXT NOT NULL DEFAULT '',
                  obsidian_note_path TEXT NOT NULL DEFAULT '',
                  feishu_doc_url TEXT NOT NULL DEFAULT '',
                  index_status TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
            additions = {
                "broker": "TEXT NOT NULL DEFAULT ''",
                "report_date": "TEXT NOT NULL DEFAULT ''",
                "companies_json": "TEXT NOT NULL DEFAULT '[]'",
                "tickers_json": "TEXT NOT NULL DEFAULT '[]'",
                "themes_json": "TEXT NOT NULL DEFAULT '[]'",
                "subthemes_json": "TEXT NOT NULL DEFAULT '[]'",
                "regions_json": "TEXT NOT NULL DEFAULT '[]'",
                "industries_json": "TEXT NOT NULL DEFAULT '[]'",
                "key_numbers_json": "TEXT NOT NULL DEFAULT '[]'",
                "ratings_json": "TEXT NOT NULL DEFAULT '[]'",
                "target_prices_json": "TEXT NOT NULL DEFAULT '[]'",
                "risks_json": "TEXT NOT NULL DEFAULT '[]'",
                "catalysts_json": "TEXT NOT NULL DEFAULT '[]'",
                "quality_status_json": "TEXT NOT NULL DEFAULT '[]'",
                "retrieval_status_json": "TEXT NOT NULL DEFAULT '[]'",
                "metadata_confidence": "REAL NOT NULL DEFAULT 0",
                "metadata_status": "TEXT NOT NULL DEFAULT ''",
                "report_type": "TEXT NOT NULL DEFAULT ''",
                "company_scope": "TEXT NOT NULL DEFAULT ''",
                "citation_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, ddl in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE reports ADD COLUMN {column} {ddl}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_metadata_events (
                  event_id TEXT PRIMARY KEY,
                  report_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 0,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  source TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_metadata_events_report_id
                ON report_metadata_events(report_id, created_at)
                """
            )


def citation_for_note(note: NoteRecord) -> dict[str, str]:
    fm = note.frontmatter
    return {
        "report_id": stable_report_id(note),
        "note_path": str(note.path),
        "summary_path": path_from_uri(str(fm.get("summary_md_path") or "")),
        "pdf_path": path_from_uri(str(fm.get("pdf_path") or "")),
        "heading": "摘要",
        "snippet": "",
    }


def metadata_db_payload(note: NoteRecord, metadata: dict[str, Any]) -> dict[str, Any]:
    citation = citation_for_note(note)
    return {
        "report_id": stable_report_id(note),
        "title": note.display_title,
        "broker": str(metadata.get("broker") or ""),
        "report_date": str(metadata.get("report_date") or ""),
        "companies_json": json_list(metadata.get("companies")),
        "tickers_json": json_list(metadata.get("tickers")),
        "themes_json": json_list(metadata.get("themes")),
        "subthemes_json": json_list(metadata.get("subthemes")),
        "regions_json": json_list(metadata.get("regions")),
        "industries_json": json_list(metadata.get("industries")),
        "key_numbers_json": json_list(metadata.get("key_numbers")),
        "ratings_json": json_list(metadata.get("ratings")),
        "target_prices_json": json_list(metadata.get("target_prices")),
        "risks_json": json_list(metadata.get("risks")),
        "catalysts_json": json_list(metadata.get("catalysts")),
        "quality_status_json": json_list(metadata.get("quality_status")),
        "retrieval_status_json": json_list(metadata.get("retrieval_status")),
        "metadata_confidence": float(metadata.get("metadata_confidence") or 0),
        "metadata_status": str(metadata.get("metadata_status") or ""),
        "report_type": str(metadata.get("report_type") or ""),
        "company_scope": str(metadata.get("company_scope") or ""),
        "citation_json": json.dumps(citation, ensure_ascii=False),
        "obsidian_note_path": str(note.path),
        "pdf_path": citation["pdf_path"],
        "summary_md_path": citation["summary_path"],
        "feishu_doc_url": str(note.frontmatter.get("feishu_doc_url") or ""),
        "updated_at": now_iso(),
    }


def upsert_metadata(db_path: Path, note: NoteRecord, metadata: dict[str, Any], source: str) -> None:
    ensure_kb_schema(db_path)
    payload = metadata_db_payload(note, metadata)
    with closing(sqlite3.connect(str(db_path))) as conn:
        with conn:
            existing = conn.execute("SELECT report_id FROM reports WHERE report_id = ?", (payload["report_id"],)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO reports(report_id, title, obsidian_note_path, pdf_path, summary_md_path, feishu_doc_url, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["report_id"],
                        payload["title"],
                        payload["obsidian_note_path"],
                        payload["pdf_path"],
                        payload["summary_md_path"],
                        payload["feishu_doc_url"],
                        payload["updated_at"],
                    ),
                )
            assignments = ", ".join(f"{key}=?" for key in payload if key != "report_id")
            values = [payload[key] for key in payload if key != "report_id"]
            conn.execute(f"UPDATE reports SET {assignments} WHERE report_id = ?", [*values, payload["report_id"]])
            conn.execute(
                """
                INSERT INTO report_metadata_events(event_id, report_id, event_type, confidence, metadata_json, source, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    payload["report_id"],
                    "metadata_extracted",
                    float(metadata.get("metadata_confidence") or 0),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    source,
                    now_iso(),
                ),
            )


def ensure_search_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS report_search")
    conn.execute(
        """
        CREATE VIRTUAL TABLE report_search USING fts5(
          report_id UNINDEXED,
          title,
          broker,
          report_date UNINDEXED,
          companies,
          themes,
          subthemes,
          core_conclusions,
          core_questions_answers,
          summary_text,
          note_path UNINDEXED,
          pdf_path UNINDEXED,
          feishu_doc_url UNINDEXED,
          tokenize='trigram'
        )
        """
    )


def rebuild_search_index(
    db_path: Path,
    vault_root: Path,
    config_root: Path = DEFAULT_CONFIG_ROOT,
    *,
    upsert_notes: bool = True,
) -> dict[str, int]:
    configs = load_kb_configs(config_root)
    notes = load_report_notes(vault_root)
    ensure_kb_schema(db_path)
    rows: list[tuple[Any, ...]] = []
    for note in notes:
        metadata = extract_report_metadata(note, configs, vault_root)
        if upsert_notes:
            upsert_metadata(db_path, note, metadata, "kb_search_rebuild")
        citation = citation_for_note(note)
        rows.append(
            (
                stable_report_id(note),
                clean_search_text(note.display_title),
                str(metadata.get("broker") or ""),
                str(metadata.get("report_date") or ""),
                " ".join(extract_list_value(metadata.get("companies"))),
                " ".join(extract_list_value(metadata.get("themes"))),
                " ".join(extract_list_value(metadata.get("subthemes"))),
                clean_search_text(metadata.get("core_conclusions") or ""),
                clean_search_text(metadata.get("core_questions_answers") or ""),
                clean_search_text(metadata.get("summary_text") or ""),
                citation["note_path"],
                citation["pdf_path"],
                str(note.frontmatter.get("feishu_doc_url") or ""),
            )
        )
    with closing(sqlite3.connect(str(db_path))) as conn:
        with conn:
            ensure_search_table(conn)
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO report_search(
                      report_id, title, broker, report_date, companies, themes, subthemes,
                      core_conclusions, core_questions_answers, summary_text,
                      note_path, pdf_path, feishu_doc_url
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
    return {"indexed": len(rows)}


def sqlite_json_contains(column: str, value: str) -> str:
    escaped = value.replace("'", "''")
    return f"EXISTS (SELECT 1 FROM json_each({column}) WHERE value = '{escaped}')"


def fallback_fts_query(query: str) -> str:
    terms: list[str] = []
    stopwords = {"什么", "有什么", "影响", "如何", "怎么", "是否", "为什么", "哪些"}
    noise_fragments = {"什么", "影响", "如何", "怎么", "是否", "为什么", "哪些"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", query):
        token = token.strip()
        if not token:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 4:
            for size in (4, 3):
                for index in range(0, len(token) - size + 1):
                    gram = token[index : index + size]
                    if gram not in stopwords and not any(fragment in gram for fragment in noise_fragments):
                        terms.append(gram)
        elif token not in stopwords:
            terms.append(token)
    unique = list(dict.fromkeys(term for term in terms if len(term) >= 2))
    if not unique:
        return query
    return " OR ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in unique[:24])


def fetch_search_rows(conn: sqlite3.Connection, sql: str, params: list[Any]) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def search_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", query):
        token = token.strip()
        if len(token) >= 2:
            terms.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 4:
            terms.extend(token[index : index + 3] for index in range(0, len(token) - 2))
    return list(dict.fromkeys(terms))


def first_term_index(text: str, terms: list[str]) -> int:
    lower_text = text.lower()
    indexes = []
    for term in terms:
        lower_term = term.lower()
        if not lower_term:
            continue
        index = lower_text.find(lower_term)
        if index >= 0:
            indexes.append(index)
    return min(indexes) if indexes else -1


def highlight_search_terms(text: str, terms: list[str]) -> str:
    highlighted = text
    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        pattern = re.compile(re.escape(term), re.I if re.search(r"[A-Za-z]", term) else 0)
        highlighted = pattern.sub(lambda match: f"[{match.group(0)}]", highlighted, count=2)
    return highlighted


def clean_snippet_source(text: str) -> str:
    cleaned = clean_search_text(text)
    cleaned = re.sub(r"\b(?:pdf|raw|clean|summary)_md_path\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\S+\.pdf\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\S+\.raw\.md\b|\S+\.clean\.md\b|\S+\.summary\.md\b", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip()


def best_search_snippet(row: sqlite3.Row, query_terms: list[str], width: int = 180) -> str:
    source_fields = [
        "core_conclusions",
        "core_questions_answers",
        "summary_text",
        "title",
        "companies",
        "themes",
        "subthemes",
    ]
    candidates: list[tuple[int, int, str]] = []
    for field_index, field in enumerate(source_fields):
        text = clean_snippet_source(str(row[field] or ""))
        if not text:
            continue
        match_index = first_term_index(text, query_terms)
        if match_index >= 0:
            start = max(0, match_index - width // 3)
            end = min(len(text), start + width)
            snippet = text[start:end].strip()
            candidates.append((field_index, match_index, snippet))
    if not candidates:
        fallback = clean_snippet_source(str(row["core_conclusions"] or row["summary_text"] or row["title"] or ""))
        return fallback[:width].strip()
    _, _, best = min(candidates, key=lambda item: (item[0], item[1]))
    return highlight_search_terms(best, query_terms)


def parse_report_date_ordinal(value: str) -> int:
    try:
        return datetime.fromisoformat(str(value or "")[:10]).date().toordinal()
    except ValueError:
        return 0


def search_result_score(
    row: sqlite3.Row,
    query: str,
    query_terms: list[str],
    *,
    company: str,
    row_index: int,
    min_date_ordinal: int,
    max_date_ordinal: int,
) -> float:
    title = clean_search_text(str(row["title"] or ""))
    companies = clean_search_text(str(row["companies"] or ""))
    body = " ".join(
        clean_snippet_source(str(row[field] or ""))
        for field in ("core_conclusions", "core_questions_answers", "summary_text")
    )
    score = max(0.0, 18.0 - row_index * 0.3)

    date_ordinal = parse_report_date_ordinal(str(row["report_date"] or ""))
    if date_ordinal and max_date_ordinal > min_date_ordinal:
        score += 18.0 * (date_ordinal - min_date_ordinal) / (max_date_ordinal - min_date_ordinal)
    elif date_ordinal:
        score += 9.0

    if company:
        if contains_keyword(companies, company):
            score += 35.0
        if contains_keyword(title, company):
            score += 18.0
        if contains_keyword(body, company):
            score += 8.0
    else:
        score += min(20.0, count_keyword_mentions(companies, query_terms) * 6.0)

    score += min(30.0, count_keyword_mentions(title, query_terms) * 5.0)
    score += min(24.0, count_keyword_mentions(body, query_terms) * 1.2)
    if query.strip() and contains_keyword(title, query.strip()):
        score += 14.0
    return score


def search_reports(
    db_path: Path,
    query: str,
    *,
    company: str = "",
    theme: str = "",
    broker: str = "",
    since_date: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    ensure_kb_schema(db_path)
    where = ["report_search MATCH ?"]
    params: list[Any] = [query]
    if company:
        where.append(sqlite_json_contains("r.companies_json", company))
    if theme:
        where.append(sqlite_json_contains("r.themes_json", theme))
    if broker:
        where.append("r.broker = ?")
        params.append(broker)
    if since_date:
        where.append("r.report_date >= ?")
        params.append(since_date)
    candidate_limit = max(limit * 6, 30)
    params.append(candidate_limit)
    sql = f"""
        SELECT
          s.report_id,
          s.title,
          s.broker,
          s.report_date,
          s.companies,
          s.themes,
          s.subthemes,
          s.core_conclusions,
          s.core_questions_answers,
          s.summary_text,
          r.companies_json,
          r.themes_json,
          r.subthemes_json,
          s.note_path,
          s.pdf_path,
          s.feishu_doc_url,
          bm25(report_search) AS rank
        FROM report_search s
        LEFT JOIN reports r ON r.report_id = s.report_id
        WHERE {" AND ".join(where)}
        ORDER BY rank
        LIMIT ?
    """
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        rows = fetch_search_rows(conn, sql, params)
        fallback = fallback_fts_query(query)
        if fallback != query:
            fallback_params = [fallback, *params[1:]]
            fallback_rows = fetch_search_rows(conn, sql, fallback_params)
            seen_report_ids = {str(row["report_id"] or "") for row in rows}
            for row in fallback_rows:
                report_id = str(row["report_id"] or "")
                if report_id and report_id not in seen_report_ids:
                    rows.append(row)
                    seen_report_ids.add(report_id)
    query_terms = search_query_terms(query)
    date_ordinals = [parse_report_date_ordinal(str(row["report_date"] or "")) for row in rows]
    nonzero_dates = [value for value in date_ordinals if value]
    min_date_ordinal = min(nonzero_dates) if nonzero_dates else 0
    max_date_ordinal = max(nonzero_dates) if nonzero_dates else 0
    scored_rows = sorted(
        enumerate(rows),
        key=lambda item: (
            -search_result_score(
                item[1],
                query,
                query_terms,
                company=company,
                row_index=item[0],
                min_date_ordinal=min_date_ordinal,
                max_date_ordinal=max_date_ordinal,
            ),
            float(item[1]["rank"] or 0),
        ),
    )
    results: list[dict[str, Any]] = []
    for _, row in scored_rows[:limit]:
        result = dict(row)
        for key in ("companies_json", "themes_json", "subthemes_json"):
            try:
                result[key.replace("_json", "")] = json.loads(result.get(key) or "[]")
            except json.JSONDecodeError:
                result[key.replace("_json", "")] = []
            result.pop(key, None)
        for key in ("core_conclusions", "core_questions_answers", "summary_text"):
            result.pop(key, None)
        result["snippet"] = best_search_snippet(row, query_terms)
        results.append(result)
    return results


def parse_note_datetime(note: NoteRecord) -> datetime:
    for key in ("report_date", "downloaded_at", "created", "updated"):
        raw = str(note.frontmatter.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
        except ValueError:
            try:
                return datetime.fromisoformat(raw + "T00:00:00").astimezone()
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(note.path.stat().st_mtime).astimezone()
    except OSError:
        return datetime.fromtimestamp(0).astimezone()


def sorted_notes(notes: list[NoteRecord]) -> list[NoteRecord]:
    return sorted(notes, key=lambda note: (parse_note_datetime(note), note.display_title), reverse=True)


def dashboard_focus_score(note: NoteRecord, terms: list[str]) -> int:
    if not terms:
        return 0
    summary = extract_note_summary_text(note)
    core_conclusions = extract_section(summary, r"^##\s*核心结论\s*$")
    core_qa = extract_section(summary, r"^##\s*核心问题与回答\s*$")
    title_text = f"{note.title}\n{note.display_title}"
    return (
        count_keyword_mentions(title_text, terms) * 20
        + count_keyword_mentions(core_conclusions, terms) * 8
        + count_keyword_mentions(core_qa, terms) * 6
        + count_keyword_mentions(summary[:8000], terms)
    )


def sorted_notes_for_focus(notes: list[NoteRecord], terms: list[str]) -> list[NoteRecord]:
    if not terms:
        return sorted_notes(notes)
    return sorted(
        notes,
        key=lambda note: (dashboard_focus_score(note, terms), parse_note_datetime(note), note.display_title),
        reverse=True,
    )


def theme_focus_terms(theme: str) -> list[str]:
    configs = load_kb_configs(DEFAULT_CONFIG_ROOT)
    rules = theme_rules(configs)
    rule = rules.get(theme, {})
    terms = [theme, *list(rule.get("keywords", []) or [])]
    for subtheme, keywords in (rule.get("subthemes") or {}).items():
        terms.extend([subtheme, *list(keywords or [])])
    return list(dict.fromkeys(str(term) for term in terms if str(term).strip()))


def company_focus_terms(company: str) -> list[str]:
    configs = load_kb_configs(DEFAULT_CONFIG_ROOT)
    rules = entity_rules(configs, DEFAULT_VAULT_ROOT)
    rule = rules.get(company, {})
    terms = [company, *list(rule.get("aliases", []) or [])]
    return list(dict.fromkeys(str(term) for term in terms if str(term).strip()))


def subtheme_distribution(notes: list[NoteRecord]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for note in notes:
        for value in extract_list_value(note.frontmatter.get("subthemes")):
            counter[value] += 1
    return counter.most_common(12)


def property_distribution(notes: list[NoteRecord], key: str, limit: int = 12) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for note in notes:
        for value in extract_list_value(note.frontmatter.get(key)):
            counter[value] += 1
    return counter.most_common(limit)


def recent_report_lines(notes: list[NoteRecord], limit: int = 12, focus_terms: list[str] | None = None) -> list[str]:
    lines: list[str] = []
    if focus_terms:
        selected = sorted(
            notes,
            key=lambda note: (parse_note_datetime(note), dashboard_focus_score(note, focus_terms), note.display_title),
            reverse=True,
        )
    else:
        selected = sorted_notes(notes)
    for note in selected[:limit]:
        date = str(note.frontmatter.get("report_date") or "")[:10]
        broker = str(note.frontmatter.get("broker") or "")
        prefix = " · ".join(item for item in [date, broker] if item)
        lines.append(f"- {obsidian_link(note)}" + (f"（{prefix}）" if prefix else ""))
    return lines or ["- 暂无"]


def conclusion_lines(notes: list[NoteRecord], limit: int = 6, focus_terms: list[str] | None = None) -> list[str]:
    lines: list[str] = []
    selected = sorted_notes_for_focus(notes, focus_terms or []) if focus_terms else sorted_notes(notes)
    for note in selected:
        summary = extract_note_summary_text(note)
        section = extract_section(summary, r"^##\s*核心结论\s*$")
        bullet = ""
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                bullet = clean_search_text(stripped[2:])[:180]
                break
        if bullet:
            lines.append(f"- {obsidian_link(note)}：{bullet}")
        if len(lines) >= limit:
            break
    return lines or ["- 暂无"]


def render_theme_dashboard(theme: str, notes: list[NoteRecord], all_notes: list[NoteRecord]) -> str:
    low_conf = [note for note in notes if "metadata_low_confidence" in extract_list_value(note.frontmatter.get("quality_status"))]
    companies = property_distribution(notes, "companies", 12)
    subthemes = subtheme_distribution(notes)
    focus_terms = theme_focus_terms(theme)
    lines = [
        "## 最近新增",
        *recent_report_lines(notes, 10, focus_terms),
        "",
        "## 高相关报告",
        *conclusion_lines(notes, 8, focus_terms),
        "",
        "## 子主题分布",
        *([f"- {name}: {count}" for name, count in subthemes] or ["- 暂无"]),
        "",
        "## 关键公司",
        *([f"- [[{COMPANY_DIR_NAME}/{name}|{name}]]（{count}）" for name, count in companies] or ["- 暂无"]),
        "",
        "## 关键问题",
        f"- `python3 scripts/kb_search.py \"{theme}\" --theme \"{theme}\" --limit 20`",
        "",
        "## 观点变化",
        *conclusion_lines(notes, 5, focus_terms),
        "",
        "## 未分类/低置信度",
        *recent_report_lines(low_conf, 10),
        "",
        "## 全量检索入口",
        "- [[Reports.base|Reports Base]]",
        f"- `python3 scripts/kb_search.py \"{theme}\" --theme \"{theme}\"`",
        "",
        f"## 自动收录报告（{len(notes)}）",
        *recent_report_lines(notes, 200),
        "",
    ]
    return "\n".join(lines)


def render_company_dashboard(company: str, notes: list[NoteRecord]) -> str:
    themes = property_distribution(notes, "themes", 12)
    focus_terms = company_focus_terms(company)
    ranked_notes = sorted_notes_for_focus(notes, focus_terms)
    key_numbers: list[str] = []
    for note in ranked_notes:
        for item in extract_list_value(note.frontmatter.get("key_numbers")):
            if item not in key_numbers:
                key_numbers.append(item)
            if len(key_numbers) >= 12:
                break
        if len(key_numbers) >= 12:
            break
    lines = [
        "## 最近报告",
        *recent_report_lines(notes, 12, focus_terms),
        "",
        "## 最新观点变化",
        *conclusion_lines(notes, 8, focus_terms),
        "",
        "## 关键数字",
        *([f"- {item}" for item in key_numbers] or ["- 暂无"]),
        "",
        "## 多空分歧",
        *extract_bull_bear_lines(notes, focus_terms),
        "",
        "## 上游/下游/竞品",
        "- 待从实体关系配置和新增报告中继续沉淀。",
        "",
        "## 相关主题",
        *([f"- [[{THEME_DIR_NAME}/{name}|{name}]]（{count}）" for name, count in themes] or ["- 暂无"]),
        "",
        "## 待确认问题",
        f"- `python3 scripts/kb_search.py \"{company}\" --company \"{company}\" --limit 20`",
        "",
        "## 全量检索入口",
        "- [[Reports.base|Reports Base]]",
        f"- `python3 scripts/kb_search.py \"{company}\" --company \"{company}\"`",
        "",
        f"## 相关报告（{len(notes)}）",
        *recent_report_lines(notes, 200),
        "",
    ]
    return "\n".join(lines)


def extract_bull_bear_lines(notes: list[NoteRecord], focus_terms: list[str] | None = None) -> list[str]:
    bullish: list[str] = []
    cautious: list[str] = []
    selected = sorted_notes_for_focus(notes, focus_terms or []) if focus_terms else sorted_notes(notes)
    for note in selected:
        summary = extract_note_summary_text(note)
        section = extract_section(summary, r"^##\s*核心结论\s*$")
        for line in section.splitlines():
            cleaned = clean_search_text(line.strip("- "))
            if not cleaned:
                continue
            if any(word in cleaned for word in ["买入", "上调", "受益", "看好", "超配", "强劲", "扩张"]):
                bullish.append(f"{obsidian_link(note)}：{cleaned[:160]}")
            if any(word in cleaned for word in ["风险", "下调", "承压", "低于预期", "谨慎", "削弱", "放缓"]):
                cautious.append(f"{obsidian_link(note)}：{cleaned[:160]}")
        if len(bullish) >= 4 and len(cautious) >= 4:
            break
    lines: list[str] = []
    lines.append("- 偏多：")
    lines.extend([f"  - {item}" for item in bullish[:4]] or ["  - 暂无"])
    lines.append("- 偏空/谨慎：")
    lines.extend([f"  - {item}" for item in cautious[:4]] or ["  - 暂无"])
    return lines


def replace_dashboard_block(text: str, heading_re: re.Pattern[str], replacement: str) -> str:
    match = heading_re.search(text)
    if not match:
        base = text.rstrip()
        return f"{base}\n\n{replacement}\n"
    prefix = text[: match.start()].rstrip()
    return f"{prefix}\n\n{replacement}\n"


def render_reports_base() -> str:
    order = [
        "file.name",
        "report_date",
        "broker",
        "companies",
        "themes",
        "subthemes",
        "report_type",
        "company_scope",
        "quality_status",
        "retrieval_status",
        "feishu_doc_url",
    ]
    base = {
        "filters": {"and": ['type == "report"', 'file.inFolder("10_Reports")']},
        "properties": {
            "file.name": {"displayName": "报告"},
            "report_date": {"displayName": "日期"},
            "broker": {"displayName": "券商"},
            "companies": {"displayName": "公司"},
            "themes": {"displayName": "主题"},
            "subthemes": {"displayName": "子主题"},
            "report_type": {"displayName": "类型"},
            "company_scope": {"displayName": "公司范围"},
            "quality_status": {"displayName": "质量"},
            "retrieval_status": {"displayName": "检索"},
            "feishu_doc_url": {"displayName": "Feishu"},
        },
        "views": [
            {"type": "table", "name": "最近新增", "limit": 200, "order": order, "sort": [{"property": "report_date", "direction": "DESC"}]},
            {"type": "table", "name": "按公司", "groupBy": {"property": "companies", "direction": "ASC"}, "order": order},
            {"type": "table", "name": "按主题", "groupBy": {"property": "themes", "direction": "ASC"}, "order": order},
            {"type": "table", "name": "按券商", "groupBy": {"property": "broker", "direction": "ASC"}, "order": order},
            {"type": "table", "name": "需要处理", "filters": {"or": ['quality_status.contains("needs_review")', 'quality_status.contains("metadata_low_confidence")', 'company_scope == "needs_entity"', 'retrieval_status.contains("missing_raw")', 'retrieval_status.contains("missing_clean")']}, "order": order},
            {"type": "table", "name": "低质量/失败", "filters": {"or": ['quality_status.contains("dirty_title")', 'quality_status.contains("unmatched_company")', 'quality_status.contains("unmatched_theme")', 'retrieval_status.contains("summary_failed")']}, "order": order},
            {"type": "table", "name": "需补实体", "filters": {"or": ['company_scope == "needs_entity"', 'quality_status.contains("unmatched_company")']}, "order": order},
            {"type": "table", "name": "AI 算力", "filters": {"or": ['themes.contains("AI算力与基础设施")', 'subthemes.contains("GPU/ASIC/TPU")']}, "order": order},
            {"type": "table", "name": "半导体", "filters": {"or": ['themes.contains("半导体周期")', 'industries.contains("半导体")']}, "order": order},
            {"type": "table", "name": "中国互联网", "filters": {"or": ['themes.contains("中国互联网平台")', 'industries.contains("互联网")']}, "order": order},
            {"type": "table", "name": "PCB/电子制造", "filters": {"or": ['themes.contains("PCB与电子制造")', 'subthemes.contains("PCB/ABF/HDI")']}, "order": order},
        ],
    }
    return yaml.safe_dump(base, allow_unicode=True, sort_keys=False, width=120)


def group_notes_by_property(notes: list[NoteRecord], key: str) -> dict[str, list[NoteRecord]]:
    grouped: dict[str, list[NoteRecord]] = defaultdict(list)
    for note in notes:
        for value in extract_list_value(note.frontmatter.get(key)):
            grouped[value].append(note)
    return grouped
