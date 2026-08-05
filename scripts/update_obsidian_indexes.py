#!/usr/bin/env python3
"""Incrementally append new report links to existing Obsidian index pages."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import kb_common as kb
except ModuleNotFoundError:  # pragma: no cover
    from scripts import kb_common as kb

try:
    from runtime_paths import DEFAULT_VAULT_ROOT
except ModuleNotFoundError:  # pragma: no cover
    from scripts.runtime_paths import DEFAULT_VAULT_ROOT

DEFAULT_RESULT_PATH = Path("state/obsidian_index_update_last_result.json")
THEME_DIR_NAME = "20_Themes"
COMPANY_DIR_NAME = "30_Companies"
REPORT_DIR_NAME = "10_Reports"
MAINTENANCE_DIR_NAME = "99_维护"
THEME_SECTION_RE = re.compile(r"^##\s*自动收录报告(?:（[^）]*）)?\s*$", re.M)
COMPANY_SECTION_RE = re.compile(r"^##\s*(?:相关报告|自动收录报告)(?:（[^）]*）)?\s*$", re.M)
THEME_DASHBOARD_RE = re.compile(r"^##\s*(?:最近新增|自动收录报告(?:（[^）]*）)?)\s*$", re.M)
COMPANY_DASHBOARD_RE = re.compile(r"^##\s*(?:最近报告|相关报告|自动收录报告)(?:（[^）]*）)?\s*$", re.M)
LINK_TARGET_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]")
REPLACEMENT_CHAR = "�"


THEME_KEYWORDS: dict[str, list[str]] = {
    "AI算力与基础设施": [
        "AI",
        "人工智能",
        "算力",
        "GPU",
        "数据中心",
        "AI服务器",
        "服务器",
        "光模块",
        "CPO",
        "液冷",
        "推理",
        "训练",
        "Blackwell",
        "Rubin",
        "GTC",
        "NVIDIA",
        "英伟达",
    ],
    "云厂商资本开支": [
        "云厂商",
        "资本开支",
        "Capex",
        "Azure",
        "AWS",
        "Google Cloud",
        "谷歌云",
        "亚马逊云",
        "微软云",
        "超大规模",
        "Hyperscaler",
        "云计算",
    ],
    "半导体周期": [
        "半导体",
        "芯片",
        "晶圆",
        "晶圆代工",
        "台积电",
        "中芯国际",
        "ASML",
        "设备",
        "封装",
        "先进制程",
        "模拟芯片",
        "测试",
    ],
    "存储周期": [
        "存储",
        "内存",
        "DRAM",
        "NAND",
        "HBM",
        "DDR5",
        "SSD",
        "HDD",
        "美光",
        "三星",
        "海力士",
        "长江存储",
    ],
    "中国互联网平台": [
        "中国互联网",
        "腾讯",
        "阿里",
        "阿里巴巴",
        "美团",
        "京东",
        "拼多多",
        "百度",
        "快手",
        "字节",
        "抖音",
        "电商",
        "广告",
        "游戏",
        "外卖",
        "本地生活",
    ],
    "消费与零售": [
        "消费",
        "零售",
        "餐饮",
        "旅游",
        "酒店",
        "化妆品",
        "白酒",
        "食品饮料",
        "奶茶",
        "泡泡玛特",
        "名创优品",
        "携程",
        "同店",
        "消费者",
    ],
    "日本与韩国市场": [
        "日本",
        "韩国",
        "韩股",
        "日股",
        "Korea",
        "Japan",
        "KOSPI",
        "日经",
        "三星",
        "海力士",
    ],
    "港股与中概": [
        "港股",
        "中概",
        "香港",
        "H股",
        "ADR",
        "恒生",
        "阿里巴巴",
        "腾讯",
        "美团",
        "小米",
        "小鹏",
        "蔚来",
        "理想",
    ],
    "油气与大宗商品": [
        "油气",
        "原油",
        "石油",
        "天然气",
        "LNG",
        "煤炭",
        "铜",
        "铝",
        "黄金",
        "大宗",
        "商品",
        "紫金",
        "中国石油",
    ],
    "电力与能源": [
        "电力",
        "电网",
        "能源",
        "发电",
        "用电",
        "供电",
        "电源",
        "光伏",
        "储能",
        "核电",
        "燃气轮机",
        "数据中心电力",
        "AIDC",
    ],
    "新能源与电动车": [
        "新能源",
        "电动车",
        "EV",
        "汽车",
        "动力电池",
        "锂电",
        "宁德",
        "比亚迪",
        "特斯拉",
        "小鹏",
        "蔚来",
        "理想",
        "光伏",
        "储能",
        "风电",
    ],
    "金融与券商银行": [
        "银行",
        "券商",
        "保险",
        "金融",
        "证券",
        "资管",
        "基金",
        "利率",
        "信贷",
        "净息差",
    ],
    "宏观流动性": [
        "宏观",
        "流动性",
        "利率",
        "通胀",
        "CPI",
        "PPI",
        "非农",
        "央行",
        "美联储",
        "财政",
        "信贷",
        "汇率",
        "关税",
        "GDP",
        "PMI",
        "降息",
        "加息",
    ],
    "医药与医疗科技": [
        "医药",
        "医疗",
        "药",
        "生物",
        "制药",
        "临床",
        "医保",
        "医疗器械",
        "药明",
        "创新药",
        "GLP-1",
    ],
    "机器人与智能制造": [
        "机器人",
        "人形",
        "智能制造",
        "工业自动化",
        "自动化",
        "机械",
        "机器视觉",
        "宇树",
        "制造业",
    ],
    "房地产与基建": [
        "房地产",
        "地产",
        "基建",
        "物业",
        "住宅",
        "写字楼",
        "REIT",
        "土地",
        "水泥",
        "建筑",
        "工程",
        "新房",
        "二手房",
    ],
}


@dataclass(frozen=True)
class ReportNote:
    path: Path
    title: str
    display_title: str
    body: str
    frontmatter: dict[str, str]
    link_target: str
    link_text: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[text.find("\n", end + 1) + 1 :]
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            data[key] = value
    return data, body


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


def obsidian_target(note_path: Path, vault_root: Path) -> str:
    relative = note_path.resolve(strict=False).relative_to(vault_root.resolve(strict=False))
    if relative.suffix == ".md":
        relative = relative.with_suffix("")
    return relative.as_posix()


def clean_alias(value: str) -> str:
    return " ".join(value.replace("|", "-").split())


def display_title_for_note(title: str, body: str) -> str:
    if REPLACEMENT_CHAR not in title:
        return title
    summary_title = first_summary_heading(body)
    if summary_title and REPLACEMENT_CHAR not in summary_title:
        return summary_title
    return title


def load_report_note(note_path: Path, vault_root: Path) -> ReportNote:
    text = read_text(note_path)
    frontmatter, body = split_frontmatter(text)
    title = frontmatter.get("title") or first_heading(body) or note_path.stem
    display_title = display_title_for_note(title, body)
    target = obsidian_target(note_path, vault_root)
    link_text = f"[[{target}|{clean_alias(display_title)}]]"
    return ReportNote(
        path=note_path,
        title=title,
        display_title=display_title,
        body=body,
        frontmatter=frontmatter,
        link_target=target,
        link_text=link_text,
    )


def contains_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return False
    lower_text = text.lower()
    lower_keyword = keyword.lower()
    if re.search(r"[a-z0-9]", lower_keyword):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(lower_keyword)}(?![a-z0-9])",
            lower_text,
        ) is not None
    return lower_keyword in lower_text


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    return sum(1 for keyword in keywords if contains_keyword(text, keyword))


def count_keyword_mentions(text: str, keywords: list[str]) -> int:
    count = 0
    lower_text = text.lower()
    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            continue
        lower_keyword = keyword.lower()
        if re.search(r"[a-z0-9]", lower_keyword):
            count += len(
                re.findall(
                    rf"(?<![a-z0-9]){re.escape(lower_keyword)}(?![a-z0-9])",
                    lower_text,
                )
            )
        else:
            count += lower_text.count(lower_keyword)
    return count


def parse_note_datetime(note: ReportNote) -> datetime:
    for key in ("downloaded_at", "created", "updated"):
        raw = str(note.frontmatter.get(key, "") or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.astimezone()
        return parsed.astimezone()
    try:
        return datetime.fromtimestamp(note.path.stat().st_mtime).astimezone()
    except OSError:
        return datetime.fromtimestamp(0).astimezone()


def report_identity(note: ReportNote) -> tuple[str, str]:
    report_id = str(note.frontmatter.get("report_id", "") or "").strip()
    if report_id:
        return "report_id", report_id
    pdf_path = str(note.frontmatter.get("pdf_path", "") or "").strip()
    if pdf_path:
        return "pdf_path", pdf_path
    return "path", note.link_target


def report_identity_keys(note: ReportNote) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    report_id = str(note.frontmatter.get("report_id", "") or "").strip()
    if report_id:
        keys.append(("report_id", report_id))
    pdf_path = str(note.frontmatter.get("pdf_path", "") or "").strip()
    if pdf_path:
        keys.append(("pdf_path", pdf_path))
    if not keys:
        keys.append(("path", note.link_target))
    return keys


def dedupe_report_notes(notes: list[ReportNote]) -> tuple[list[ReportNote], list[dict[str, Any]]]:
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(key, key)
        if parent[key] != key:
            parent[key] = find(parent[key])
        return parent[key]

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    note_keys: list[tuple[ReportNote, list[tuple[str, str]]]] = []
    for note in notes:
        keys = report_identity_keys(note)
        note_keys.append((note, keys))
        for key in keys:
            find(key)
        for key in keys[1:]:
            union(keys[0], key)

    grouped: dict[tuple[str, str], list[ReportNote]] = {}
    group_labels: dict[tuple[str, str], tuple[str, str]] = {}
    for note, keys in note_keys:
        root = find(keys[0])
        grouped.setdefault(root, []).append(note)
        label = next((key for key in keys if key[0] == "report_id"), None)
        if label is None:
            label = next((key for key in keys if key[0] == "pdf_path"), keys[0])
        group_labels.setdefault(root, label)

    unique: list[ReportNote] = []
    duplicate_groups: list[dict[str, Any]] = []
    for root, group in grouped.items():
        ordered = sorted(
            group,
            key=lambda note: (parse_note_datetime(note), str(note.path)),
            reverse=True,
        )
        kept = ordered[0]
        unique.append(kept)
        if len(ordered) > 1:
            identity = group_labels[root]
            duplicate_groups.append(
                {
                    "identity_type": identity[0],
                    "identity": identity[1],
                    "kept": kept,
                    "duplicates": ordered[1:],
                }
            )
    unique.sort(key=lambda note: (parse_note_datetime(note), note.display_title), reverse=True)
    return unique, duplicate_groups


def load_existing_theme_rules(vault_root: Path) -> dict[str, list[str]]:
    theme_dir = vault_root / THEME_DIR_NAME
    rules: dict[str, list[str]] = {}
    for name, keywords in THEME_KEYWORDS.items():
        if (theme_dir / f"{name}.md").exists():
            rules[name] = keywords
    return rules


def parse_company_aliases(page_path: Path) -> list[str]:
    text = read_text(page_path)
    aliases = [page_path.stem]
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
        raw_aliases = stripped[2:]
        for item in re.split(r"[、,，/]", raw_aliases):
            alias = item.strip()
            if alias and alias not in aliases:
                aliases.append(alias)
    return aliases


def company_names_from_index(index_path: Path) -> set[str]:
    if not index_path.exists():
        return set()
    text = read_text(index_path)
    names: set[str] = set()
    for target in LINK_TARGET_RE.findall(text):
        if target.startswith(f"{COMPANY_DIR_NAME}/"):
            name = Path(target).name
            if name:
                names.add(name)
    return names


def load_company_rules(vault_root: Path) -> dict[str, list[str]]:
    company_dir = vault_root / COMPANY_DIR_NAME
    index_names = company_names_from_index(company_dir / "公司索引.md")
    rules: dict[str, list[str]] = {}
    for page_path in sorted(company_dir.glob("*.md")):
        if page_path.name == "公司索引.md":
            continue
        if index_names and page_path.stem not in index_names:
            continue
        rules[page_path.stem] = parse_company_aliases(page_path)
    return rules


def classify_themes(note: ReportNote, rules: dict[str, list[str]]) -> list[str]:
    title_matched: list[str] = []
    title = f"{note.title}\n{note.display_title}"
    body = note.body
    for name, keywords in rules.items():
        if count_keyword_hits(title, keywords) > 0:
            title_matched.append(name)
    if title_matched:
        return title_matched

    matched: list[str] = []
    for name, keywords in rules.items():
        if count_keyword_hits(body, keywords) >= 2:
            matched.append(name)
    return matched


def classify_companies(note: ReportNote, rules: dict[str, list[str]]) -> list[str]:
    title_matched: list[str] = []
    title = f"{note.title}\n{note.display_title}"
    body = note.body
    for name, aliases in rules.items():
        if count_keyword_hits(title, aliases) > 0:
            title_matched.append(name)
    if title_matched:
        return title_matched

    matched: list[str] = []
    for name, aliases in rules.items():
        if count_keyword_mentions(body, aliases) >= 2:
            matched.append(name)
    return matched


def classify_with_gpt_if_needed(note: ReportNote) -> tuple[list[str], list[str]]:
    # 预留接口：当前仓库没有稳定、低风险的单篇 GPT 分类调用，所以这里先不接入主流程。
    return [], []


def find_section_insert_at(text: str, section_re: re.Pattern[str]) -> int | None:
    match = section_re.search(text)
    if not match:
        return None
    next_match = re.search(r"^##\s+", text[match.end() :], re.M)
    if next_match:
        return match.end() + next_match.start()
    return len(text)


def has_link_target(text: str, target: str) -> bool:
    for existing_target in LINK_TARGET_RE.findall(text):
        if existing_target == target:
            return True
    return False


def replace_or_add_frontmatter_field(text: str, key: str, value: str) -> str:
    if not text.startswith("---\n"):
        return f"---\n{key}: {value}\n---\n\n{text}"
    end = text.find("\n---", 4)
    if end < 0:
        return text
    raw = text[4:end]
    lines = raw.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.split(":", 1)[0].strip() == key:
            lines[index] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + text[end:]


def replace_auto_section(
    text: str,
    section_re: re.Pattern[str],
    heading: str,
    link_lines: list[str],
) -> str:
    match = section_re.search(text)
    section_body = "\n".join(link_lines)
    replacement = f"{heading}\n"
    if section_body:
        replacement += f"{section_body}\n"
    if not match:
        if not text.endswith("\n"):
            text += "\n"
        return f"{text}\n{replacement}"
    next_match = re.search(r"^##\s+", text[match.end() :], re.M)
    section_end = match.end() + next_match.start() if next_match else len(text)
    prefix = text[: match.start()].rstrip() + "\n\n"
    suffix = text[section_end:].lstrip("\n")
    if suffix:
        return prefix + replacement + "\n" + suffix
    return prefix + replacement


def obsidian_link(note: ReportNote) -> str:
    return f"- {note.link_text}"


def sorted_notes(notes: list[ReportNote]) -> list[ReportNote]:
    return sorted(notes, key=lambda note: (parse_note_datetime(note), note.display_title), reverse=True)


def load_all_report_notes(vault_root: Path) -> list[ReportNote]:
    report_root = vault_root / REPORT_DIR_NAME
    notes: list[ReportNote] = []
    for note_path in sorted(report_root.rglob("*.md")):
        try:
            note = load_report_note(note_path, vault_root)
        except ValueError:
            continue
        if note.link_target.startswith(f"{REPORT_DIR_NAME}/"):
            notes.append(note)
    return notes


def write_if_changed(path: Path, text: str, dry_run: bool) -> bool:
    old_text = read_text(path) if path.exists() else ""
    if old_text == text:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return True


def load_dashboard_notes(vault_root: Path) -> list[kb.NoteRecord]:
    configs = kb.load_kb_configs(kb.DEFAULT_CONFIG_ROOT)
    notes: list[kb.NoteRecord] = []
    for note in kb.load_report_notes(vault_root):
        metadata = kb.extract_report_metadata(note, configs, vault_root)
        merged = dict(note.frontmatter)
        for field in kb.METADATA_FIELDS:
            if field in kb.LIST_FIELDS:
                if not kb.extract_list_value(merged.get(field)):
                    merged[field] = kb.extract_list_value(metadata.get(field))
            elif not str(merged.get(field) or "").strip():
                merged[field] = metadata.get(field, "")
        for field in ("metadata_confidence", "metadata_status"):
            if not str(merged.get(field) or "").strip():
                merged[field] = metadata.get(field, "")
        notes.append(
            kb.NoteRecord(
                path=note.path,
                frontmatter=merged,
                body=note.body,
                title=note.title,
                display_title=note.display_title,
                link_target=note.link_target,
            )
        )
    return notes


def write_dashboard_pages(vault_root: Path, dry_run: bool) -> int:
    changed = 0
    dashboard_notes = load_dashboard_notes(vault_root)
    theme_groups = kb.group_notes_by_property(dashboard_notes, "themes")
    company_groups = kb.group_notes_by_property(dashboard_notes, "companies")
    for theme, notes in theme_groups.items():
        page_path = vault_root / THEME_DIR_NAME / f"{theme}.md"
        if not page_path.exists():
            continue
        new_text = kb.replace_dashboard_block(
            read_text(page_path),
            THEME_DASHBOARD_RE,
            kb.render_theme_dashboard(theme, notes, dashboard_notes),
        )
        if write_if_changed(page_path, new_text, dry_run):
            changed += 1
    for company, notes in company_groups.items():
        page_path = vault_root / COMPANY_DIR_NAME / f"{company}.md"
        if not page_path.exists():
            continue
        new_text = kb.replace_dashboard_block(
            read_text(page_path),
            COMPANY_DASHBOARD_RE,
            kb.render_company_dashboard(company, notes),
        )
        if write_if_changed(page_path, new_text, dry_run):
            changed += 1
    if write_if_changed(vault_root / "Reports.base", kb.render_reports_base(), dry_run):
        changed += 1
    return changed


def render_report_list(notes: list[ReportNote], limit: int | None = None) -> list[str]:
    selected = sorted_notes(notes)
    if limit is not None:
        selected = selected[:limit]
    return [obsidian_link(note) for note in selected]


def render_count_links(items: list[tuple[str, int]], dir_name: str) -> list[str]:
    return [f"- [[{dir_name}/{name}|{name}]]（{count}）" for name, count in items]


def render_recent_count_links(items: list[tuple[str, int, int]], dir_name: str) -> list[str]:
    lines: list[str] = []
    for name, total_count, recent_count in items:
        lines.append(f"- [[{dir_name}/{name}|{name}]]（总计 {total_count}，近 7 天 {recent_count}）")
    return lines


def render_theme_index(theme_counts: list[tuple[str, int]], recent_counts: dict[str, int], generated_at: str) -> str:
    sorted_recent = [
        (name, count, recent_counts.get(name, 0))
        for name, count in theme_counts
        if recent_counts.get(name, 0) > 0
    ]
    lines = [
        "---",
        "type: theme_index",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "generated_by: update_obsidian_indexes_rebuild",
        "---",
        "",
        "# 主题索引",
        "",
        "## 最近 7 天新增",
    ]
    lines.extend(render_recent_count_links(sorted_recent, THEME_DIR_NAME) or ["- 暂无"])
    lines.extend(["", "## 主题"])
    lines.extend(render_count_links(theme_counts, THEME_DIR_NAME) or ["- 暂无"])
    lines.append("")
    return "\n".join(lines)


def render_company_index(company_counts: list[tuple[str, int]], recent_counts: dict[str, int], generated_at: str) -> str:
    sorted_recent = [
        (name, count, recent_counts.get(name, 0))
        for name, count in company_counts
        if recent_counts.get(name, 0) > 0
    ]
    lines = [
        "---",
        "type: company_index",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "generated_by: update_obsidian_indexes_rebuild",
        "---",
        "",
        "# 公司索引",
        "",
        f"共 {len(company_counts)} 个公司页。",
        "",
        "## 最近 7 天新增",
    ]
    lines.extend(render_recent_count_links(sorted_recent, COMPANY_DIR_NAME) or ["- 暂无"])
    lines.extend(["", "## 公司"])
    lines.extend(render_count_links(company_counts, COMPANY_DIR_NAME) or ["- 暂无"])
    lines.append("")
    return "\n".join(lines)


def render_home(
    theme_counts: list[tuple[str, int]],
    company_counts: list[tuple[str, int]],
    recent_notes: list[ReportNote],
    generated_at: str,
    maintenance_report_exists: bool,
) -> str:
    lines = [
        "---",
        "type: home",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "generated_by: update_obsidian_indexes_rebuild",
        "---",
        "",
        "# ResearchVault 首页",
        "",
        "## 主要入口",
        "- [[20_Themes/主题索引|主题索引]]",
        "- [[30_Companies/公司索引|公司索引]]",
        "- [[10_Reports|全部报告]]",
    ]
    if maintenance_report_exists:
        lines.append("- [[99_维护/数据质量清单|数据质量清单]]")
    lines.extend(["", "## 最近 7 天新增"])
    if recent_notes:
        lines.append(f"共 {len(recent_notes)} 篇，下面显示最新 30 篇。")
        lines.append("")
        lines.extend(render_report_list(recent_notes, limit=30))
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 常用主题"])
    lines.extend(render_count_links(theme_counts[:20], THEME_DIR_NAME) or ["- 暂无"])
    lines.extend(["", "## 高频公司"])
    lines.extend(render_count_links(company_counts[:30], COMPANY_DIR_NAME) or ["- 暂无"])
    lines.extend(
        [
            "",
            "## 说明",
            "- 主题页和公司页由脚本按标题与摘要关键词重建。",
            "- 自动收录列表会按 report_id 或 PDF 路径去重。",
            "- “我的当前判断”等人工区域不会被自动重写。",
            "",
        ]
    )
    return "\n".join(lines)


def render_maintenance_report(
    duplicate_groups: list[dict[str, Any]],
    dirty_title_notes: list[ReportNote],
    generated_at: str,
) -> str:
    lines = [
        "---",
        "type: maintenance",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "generated_by: update_obsidian_indexes_rebuild",
        "---",
        "",
        "# 数据质量清单",
        "",
        "## 重复报告",
    ]
    if duplicate_groups:
        for group in duplicate_groups:
            kept: ReportNote = group["kept"]
            lines.append(f"- {group['identity_type']} `{group['identity']}`")
            lines.append(f"  - 保留在索引：{kept.link_text}")
            for duplicate in group["duplicates"]:
                lines.append(f"  - 未进入索引：{duplicate.link_text}")
    else:
        lines.append("- 暂无")

    lines.extend(["", f"## 标题含 `{REPLACEMENT_CHAR}` 的报告"])
    if dirty_title_notes:
        for note in sorted_notes(dirty_title_notes):
            if note.display_title != note.title:
                lines.append(f"- {note.link_text}：索引显示已改用摘要标题；原标题 `{clean_alias(note.title)}`")
            else:
                lines.append(f"- {note.link_text}：待人工确认完整标题")
    else:
        lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def rebuild_all_indexes(vault_root: Path, dry_run: bool, recent_days: int) -> dict[str, Any]:
    theme_rules = load_existing_theme_rules(vault_root)
    company_rules = load_company_rules(vault_root)
    all_notes = load_all_report_notes(vault_root)
    unique_notes, duplicate_groups = dedupe_report_notes(all_notes)

    theme_matches: dict[str, list[ReportNote]] = {name: [] for name in theme_rules}
    company_matches: dict[str, list[ReportNote]] = {name: [] for name in company_rules}
    warnings: list[str] = []

    for note in unique_notes:
        themes = classify_themes(note, theme_rules)
        companies = classify_companies(note, company_rules)
        if not themes:
            warnings.append(f"无法判断主题：{note.display_title}")
        if not companies:
            warnings.append(f"未命中已有公司页：{note.display_title}")
        for theme in themes:
            if theme in theme_matches:
                theme_matches[theme].append(note)
        for company in companies:
            if company in company_matches:
                company_matches[company].append(note)

    changed_pages = 0
    theme_dir = vault_root / THEME_DIR_NAME
    company_dir = vault_root / COMPANY_DIR_NAME
    for theme, notes in theme_matches.items():
        page_path = theme_dir / f"{theme}.md"
        if not page_path.exists():
            continue
        old_text = read_text(page_path)
        new_text = replace_or_add_frontmatter_field(old_text, "report_count", str(len(notes)))
        new_text = replace_auto_section(
            new_text,
            THEME_SECTION_RE,
            f"## 自动收录报告（{len(notes)}）",
            render_report_list(notes),
        )
        if write_if_changed(page_path, new_text, dry_run):
            changed_pages += 1

    for company, notes in company_matches.items():
        page_path = company_dir / f"{company}.md"
        if not page_path.exists():
            continue
        old_text = read_text(page_path)
        new_text = replace_or_add_frontmatter_field(old_text, "report_count", str(len(notes)))
        new_text = replace_auto_section(
            new_text,
            COMPANY_SECTION_RE,
            f"## 相关报告（{len(notes)}）",
            render_report_list(notes),
        )
        if write_if_changed(page_path, new_text, dry_run):
            changed_pages += 1

    now = datetime.now().astimezone()
    generated_at = now.isoformat(timespec="seconds")
    recent_cutoff = now - timedelta(days=recent_days)
    recent_notes = [note for note in unique_notes if parse_note_datetime(note) >= recent_cutoff]
    recent_theme_counts = {
        theme: sum(1 for note in notes if note in recent_notes)
        for theme, notes in theme_matches.items()
    }
    recent_company_counts = {
        company: sum(1 for note in notes if note in recent_notes)
        for company, notes in company_matches.items()
    }
    theme_counts = sorted(
        ((name, len(notes)) for name, notes in theme_matches.items()),
        key=lambda item: (-item[1], item[0]),
    )
    company_counts = sorted(
        ((name, len(notes)) for name, notes in company_matches.items()),
        key=lambda item: (-item[1], item[0]),
    )
    dirty_title_notes = [note for note in unique_notes if REPLACEMENT_CHAR in note.title]

    maintenance_path = vault_root / MAINTENANCE_DIR_NAME / "数据质量清单.md"
    maintenance_text = render_maintenance_report(duplicate_groups, dirty_title_notes, generated_at)
    if write_if_changed(maintenance_path, maintenance_text, dry_run):
        changed_pages += 1

    index_text = render_theme_index(theme_counts, recent_theme_counts, generated_at)
    if write_if_changed(theme_dir / "主题索引.md", index_text, dry_run):
        changed_pages += 1
    company_index_text = render_company_index(company_counts, recent_company_counts, generated_at)
    if write_if_changed(company_dir / "公司索引.md", company_index_text, dry_run):
        changed_pages += 1
    home_text = render_home(theme_counts, company_counts, recent_notes, generated_at, True)
    if write_if_changed(vault_root / "00_首页.md", home_text, dry_run):
        changed_pages += 1
    changed_pages += write_dashboard_pages(vault_root, dry_run)

    return {
        "updated_at": generated_at,
        "dry_run": dry_run,
        "report_note_count": len(all_notes),
        "unique_report_note_count": len(unique_notes),
        "duplicate_group_count": len(duplicate_groups),
        "dirty_title_count": len(dirty_title_notes),
        "recent_days": recent_days,
        "recent_report_count": len(recent_notes),
        "theme_count": len(theme_counts),
        "company_count": len(company_counts),
        "changed_page_count": changed_pages,
        "warnings": warnings[:200],
        "warnings_truncated": len(warnings) > 200,
    }


def append_link_to_page(
    page_path: Path,
    section_re: re.Pattern[str],
    link_target: str,
    link_text: str,
    dry_run: bool,
) -> tuple[bool, str | None]:
    text = read_text(page_path)
    if has_link_target(text, link_target):
        return False, None
    insert_at = find_section_insert_at(text, section_re)
    if insert_at is None:
        return False, f"{page_path} 找不到固定追加区域"
    prefix = "\n" if insert_at > 0 and not text[:insert_at].endswith("\n") else ""
    suffix = "" if insert_at == len(text) or text[insert_at : insert_at + 1] == "\n" else "\n"
    insertion = f"{prefix}- {link_text}\n{suffix}"
    if not dry_run:
        page_path.write_text(text[:insert_at] + insertion + text[insert_at:], encoding="utf-8")
    return True, None


def note_paths_from_batch(batch_file: Path) -> list[Path]:
    data = json.loads(read_text(batch_file))
    paths: list[Path] = []
    for item in data.get("files", []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("obsidian_note_path") or "").strip()
        if raw:
            paths.append(Path(raw).expanduser())
    return paths


def note_paths_from_file(notes_file: Path) -> list[Path]:
    text = read_text(notes_file).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [Path(line.strip()).expanduser() for line in text.splitlines() if line.strip()]
    if isinstance(data, list):
        return [Path(str(item)).expanduser() for item in data if str(item).strip()]
    if isinstance(data, dict):
        values = data.get("notes") or data.get("note_paths") or []
        if isinstance(values, list):
            return [Path(str(item)).expanduser() for item in values if str(item).strip()]
    return []


def collect_note_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    for raw in args.note:
        paths.append(Path(raw).expanduser())
    if args.notes_file:
        paths.extend(note_paths_from_file(Path(args.notes_file).expanduser()))
    if args.batch_file:
        paths.extend(note_paths_from_batch(Path(args.batch_file).expanduser()))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def update_indexes(
    note_paths: list[Path],
    vault_root: Path,
    dry_run: bool,
) -> dict[str, Any]:
    theme_rules = load_existing_theme_rules(vault_root)
    company_rules = load_company_rules(vault_root)
    theme_dir = vault_root / THEME_DIR_NAME
    company_dir = vault_root / COMPANY_DIR_NAME
    warnings: list[str] = []
    plans: list[dict[str, Any]] = []
    processed = 0
    skipped = 0
    theme_appended = 0
    company_appended = 0

    for raw_path in note_paths:
        note_path = raw_path.expanduser().resolve(strict=False)
        if not note_path.exists():
            skipped += 1
            warnings.append(f"note 不存在，已跳过：{note_path}")
            continue
        try:
            note = load_report_note(note_path, vault_root)
        except ValueError:
            skipped += 1
            warnings.append(f"note 不在 vault 内，已跳过：{note_path}")
            continue
        if not note.link_target.startswith(f"{REPORT_DIR_NAME}/"):
            skipped += 1
            warnings.append(f"不是 10_Reports 下的单篇报告，已跳过：{note_path}")
            continue

        processed += 1
        themes = classify_themes(note, theme_rules)
        companies = classify_companies(note, company_rules)
        if not themes and not companies:
            gpt_themes, gpt_companies = classify_with_gpt_if_needed(note)
            themes.extend(name for name in gpt_themes if name in theme_rules)
            companies.extend(name for name in gpt_companies if name in company_rules)

        if not themes:
            warnings.append(f"无法判断主题，已跳过主题页：{note.title}")
        if not companies:
            warnings.append(f"未命中已有公司页：{note.title}")

        note_plan = {
            "note_path": str(note.path),
            "title": note.title,
            "link": note.link_text,
            "themes": themes,
            "companies": companies,
            "planned_theme_pages": [],
            "planned_company_pages": [],
        }

        for theme in themes:
            page_path = theme_dir / f"{theme}.md"
            changed, warning = append_link_to_page(
                page_path=page_path,
                section_re=THEME_SECTION_RE,
                link_target=note.link_target,
                link_text=note.link_text,
                dry_run=dry_run,
            )
            if warning:
                warnings.append(warning)
            if changed:
                theme_appended += 1
                note_plan["planned_theme_pages"].append(str(page_path))

        for company in companies:
            page_path = company_dir / f"{company}.md"
            changed, warning = append_link_to_page(
                page_path=page_path,
                section_re=COMPANY_SECTION_RE,
                link_target=note.link_target,
                link_text=note.link_text,
                dry_run=dry_run,
            )
            if warning:
                warnings.append(warning)
            if changed:
                company_appended += 1
                note_plan["planned_company_pages"].append(str(page_path))

        if not themes and not companies:
            skipped += 1
        plans.append(note_plan)

    return {
        "updated_at": datetime.now().astimezone().isoformat(),
        "dry_run": dry_run,
        "processed_note_count": processed,
        "theme_links_appended": theme_appended,
        "company_links_appended": company_appended,
        "skipped_note_count": skipped,
        "warnings": warnings,
        "plans": plans,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Obsidian theme/company indexes for new report notes.")
    parser.add_argument("--batch-file", default="")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--notes-file", default="")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--result-file", default=str(DEFAULT_RESULT_PATH))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rebuild-all", action="store_true")
    mode.add_argument(
        "--incremental-only",
        "--skip-full-rebuild",
        dest="incremental_only",
        action="store_true",
        help="Update only the supplied notes; do not scan and rebuild the full vault.",
    )
    parser.add_argument("--recent-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    if args.rebuild_all:
        result = rebuild_all_indexes(vault_root=vault_root, dry_run=args.dry_run, recent_days=args.recent_days)
    else:
        note_paths = collect_note_paths(args)
        result = update_indexes(note_paths=note_paths, vault_root=vault_root, dry_run=args.dry_run)
        if not args.dry_run and not args.incremental_only:
            result["full_rebuild"] = rebuild_all_indexes(
                vault_root=vault_root,
                dry_run=False,
                recent_days=args.recent_days,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.dry_run and args.result_file:
        result_path = Path(args.result_file).expanduser()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
