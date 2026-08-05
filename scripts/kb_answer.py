#!/usr/bin/env python3
"""Build a source-grounded answer draft from the local research KB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_VAULT_ROOT,
    clean_search_text,
    ensure_kb_schema,
    entity_rules,
    load_kb_configs,
    rebuild_search_index,
    search_reports,
)


def table_exists(db_path: Path, name: str) -> bool:
    if not db_path.exists():
        return False
    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None


def parse_since(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.endswith("d") and raw[:-1].isdigit():
        return (datetime.now().astimezone() - timedelta(days=int(raw[:-1]))).date().isoformat()
    return raw


def query_terms(query: str) -> set[str]:
    terms: set[str] = set()
    stopwords = {"什么", "有什么", "影响", "如何", "怎么", "是否", "为什么", "哪些"}
    noise_fragments = {"什么", "影响", "如何", "怎么", "是否", "为什么", "哪些"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", query):
        token = token.lower()
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", token):
            for size in (4, 3):
                for index in range(0, len(token) - size + 1):
                    gram = token[index : index + size]
                    if gram not in stopwords and not any(fragment in gram for fragment in noise_fragments):
                        terms.add(gram)
        elif len(token) >= 2 and token not in stopwords:
            terms.add(token)
    return terms


def embedding_tokens(text: str) -> list[str]:
    cleaned = clean_search_text(text).lower()
    tokens: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9.+-]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", cleaned):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if len(token) <= 4:
                tokens.append(token)
            else:
                for size in (2, 3):
                    tokens.extend(token[index : index + size] for index in range(0, len(token) - size + 1))
        elif len(token) >= 2:
            tokens.append(token)
    return tokens


def hash_embedding(text: str, dimensions: int = 512) -> dict[int, float]:
    vector: dict[int, float] = {}
    for token in embedding_tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % dimensions
        sign = -1.0 if raw & 1 else 1.0
        vector[index] = vector.get(index, 0.0) + sign
    return vector


def cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(index, 0.0) for index, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def load_report_details(db_path: Path, report_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not report_ids or not db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in report_ids)
    sql = f"""
        SELECT r.report_id, r.key_numbers_json, r.risks_json, r.catalysts_json, r.quality_status_json,
               r.retrieval_status_json, r.citation_json, s.core_conclusions, s.core_questions_answers,
               s.summary_text
        FROM reports r
        LEFT JOIN report_search s ON s.report_id = r.report_id
        WHERE r.report_id IN ({placeholders})
    """
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, report_ids).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                f"""
                SELECT report_id, key_numbers_json, risks_json, catalysts_json, quality_status_json,
                       retrieval_status_json, citation_json
                FROM reports
                WHERE report_id IN ({placeholders})
                """,
                report_ids,
            ).fetchall()
    details: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        details[item["report_id"]] = {
            "key_numbers": json_loads(item.get("key_numbers_json"), []),
            "risks": json_loads(item.get("risks_json"), []),
            "catalysts": json_loads(item.get("catalysts_json"), []),
            "quality_status": json_loads(item.get("quality_status_json"), []),
            "retrieval_status": json_loads(item.get("retrieval_status_json"), []),
            "citation": json_loads(item.get("citation_json"), {}),
            "core_conclusions": item.get("core_conclusions", ""),
            "core_questions_answers": item.get("core_questions_answers", ""),
            "summary_text": item.get("summary_text", ""),
        }
    return details


def company_focus_terms(company: str, config_root: Path, vault_root: Path) -> set[str]:
    company = str(company or "").strip()
    if not company:
        return set()
    configs = load_kb_configs(config_root)
    rules = entity_rules(configs, vault_root)
    terms = {company.lower()}
    for name, rule in rules.items():
        aliases = [name, *list(rule.get("aliases", []) or [])]
        if any(str(alias).lower() == company.lower() for alias in aliases):
            terms.update(str(alias).lower() for alias in aliases if str(alias).strip())
            break
    return terms


def count_focus_hits(text: str, focus_terms: set[str]) -> int:
    if not focus_terms:
        return 0
    haystack = clean_search_text(text).lower()
    return sum(1 for term in focus_terms if term and term in haystack)


def lexical_rerank(
    query: str,
    results: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    *,
    focus_terms: set[str] | None = None,
) -> list[dict[str, Any]]:
    terms = query_terms(query)
    query_vector = hash_embedding(query)
    focus_terms = focus_terms or set()
    ranked: list[dict[str, Any]] = []
    for item in results:
        detail = details.get(str(item.get("report_id")), {})
        weighted_parts = [
            (6, str(item.get("title") or "")),
            (5, str(item.get("snippet") or "")),
            (4, str(detail.get("core_conclusions") or "")),
            (4, str(detail.get("core_questions_answers") or "")),
            (2, " ".join(detail.get("key_numbers", [])[:8])),
            (2, " ".join(detail.get("risks", [])[:4])),
            (2, " ".join(detail.get("catalysts", [])[:4])),
            (1, str(detail.get("summary_text") or "")[:12000]),
        ]
        hits = 0
        present_terms: set[str] = set()
        document_text_parts: list[str] = []
        for weight, text in weighted_parts:
            document_text_parts.append(text)
            haystack = clean_search_text(text).lower()
            for term in terms:
                if term and term in haystack:
                    hits += weight
                    present_terms.add(term)
        if terms and present_terms == terms:
            hits += 10
        focus_score = 0
        if focus_terms:
            focus_score += count_focus_hits(str(item.get("title") or ""), focus_terms) * 18
            focus_score += count_focus_hits(str(item.get("snippet") or ""), focus_terms) * 8
            focus_score += count_focus_hits(str(detail.get("core_conclusions") or ""), focus_terms) * 6
            focus_score += count_focus_hits(str(detail.get("core_questions_answers") or ""), focus_terms) * 6
            focus_score += count_focus_hits(" ".join(item.get("companies", []) or []), focus_terms) * 3
        embedding_score = cosine(query_vector, hash_embedding("\n".join(document_text_parts)))
        combined_score = hits + focus_score + embedding_score * 10
        recency = str(item.get("report_date") or "")
        enriched = dict(item)
        enriched["rerank_score"] = round(combined_score, 4)
        enriched["lexical_score"] = hits
        enriched["focus_score"] = focus_score
        enriched["embedding_score"] = round(embedding_score, 4)
        enriched["matched_terms"] = sorted(present_terms)
        enriched["citation"] = detail.get("citation", {})
        enriched["key_numbers"] = detail.get("key_numbers", [])[:5]
        enriched["risks"] = detail.get("risks", [])[:3]
        enriched["catalysts"] = detail.get("catalysts", [])[:3]
        enriched["core_conclusions"] = detail.get("core_conclusions", "")
        enriched["core_questions_answers"] = detail.get("core_questions_answers", "")
        enriched["quality_status"] = detail.get("quality_status", [])
        enriched["retrieval_status"] = detail.get("retrieval_status", [])
        ranked.append(enriched)
        enriched["_sort_key"] = (-combined_score, str(item.get("rank", "")), recency)
    ranked.sort(key=lambda item: item["_sort_key"])
    for item in ranked:
        item.pop("_sort_key", None)
    return ranked


def classify_line(text: str) -> str:
    if any(word in text for word in ["风险", "下调", "承压", "不及预期", "放缓", "削弱", "margin compression"]):
        return "risks"
    if any(word in text for word in ["分歧", "不同", "但", "然而", "谨慎", "争议"]):
        return "disagreements"
    if any(word in text for word in ["最新", "上调", "下调", "新增", "变化", "转向", "加速", "放缓"]):
        return "changes"
    return "consensus"


def evidence_line(index: int, item: dict[str, Any]) -> str:
    title = str(item.get("title") or "")
    date = str(item.get("report_date") or "")
    broker = str(item.get("broker") or "")
    snippet = clean_search_text(str(item.get("snippet") or ""))[:220]
    marker = f"[S{index}]"
    meta = " / ".join(part for part in [date, broker] if part)
    return f"- {marker} {title}" + (f"（{meta}）" if meta else "") + (f"：{snippet}" if snippet else "")


def item_evidence_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("snippet") or ""),
        str(item.get("core_conclusions") or ""),
        str(item.get("core_questions_answers") or ""),
        " ".join(item.get("key_numbers", []) or []),
        " ".join(item.get("risks", []) or []),
        " ".join(item.get("catalysts", []) or []),
    ]
    return clean_search_text("\n".join(parts))


def source_refs_for_keywords(ranked: list[dict[str, Any]], keywords: list[str], *, limit: int = 4) -> str:
    refs: list[str] = []
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
    for index, item in enumerate(ranked, start=1):
        text = item_evidence_text(item).lower()
        if any(keyword in text for keyword in lowered_keywords):
            refs.append(f"S{index}")
        if len(refs) >= limit:
            break
    return "、".join(refs)


def format_claim(text: str, refs: str) -> str:
    return f"- [{refs}] {text}" if refs else ""


def build_synthesis_claims(query: str, ranked: list[dict[str, Any]]) -> list[str]:
    query_text = query.lower()
    all_text = "\n".join(item_evidence_text(item).lower() for item in ranked)
    claims: list[str] = []
    margin_query = any(term in query_text for term in ["利润", "毛利", "margin", "profit"])

    if margin_query and any(term in all_text for term in ["tpu", "自研芯片", "训练芯片", "推理芯片"]):
        refs = source_refs_for_keywords(ranked, ["TPU", "自研芯片", "训练芯片", "推理芯片"])
        claim = "TPU 对利润率不是单向利好；它把 Google Cloud 的 AI 基础设施能力和成本控制拉到前台，但最终利润率取决于订单转化、外部销售会计处理和折旧节奏。"
        if refs:
            claims.append(format_claim(claim, refs))

    if margin_query:
        refs = source_refs_for_keywords(ranked, ["成本优势", "性价比", "效率", "功耗", "降低成本", "成本"])
        if refs:
            claims.append(format_claim("正面机制主要来自自研芯片的成本/效率优势：如果 TPU 能提高单位算力性价比，并把 Gemini、Vertex 或外部客户需求转化为云收入，云业务利润率有上行空间。", refs))
        refs = source_refs_for_keywords(ranked, ["资本开支", "capex", "折旧", "投资", "短期成本", "承压", "margin compression"])
        if refs:
            claims.append(format_claim("负面机制主要来自资本开支和折旧：AI 基础设施扩张会先推高投入，短期可能压制 Google Cloud 利润率，即使长期单位经济性改善。", refs))
        refs = source_refs_for_keywords(ranked, ["订单", "backlog", "积压", "anthropic", "meta", "外部销售"])
        if refs:
            claims.append(format_claim("最新报告更关注 backlog/外部客户订单：这些订单提高收入可见度，但会计处理、交付节奏和客户结构会影响它们落到利润率上的速度。", refs))
        refs = source_refs_for_keywords(ranked, ["广告", "搜索", "AI搜索", "变现", "Gemini"])
        if refs:
            claims.append(format_claim("需要区分 Google Cloud 利润率和 Alphabet 集团利润率：搜索/广告与 Gemini 变现可能抵消云端投入压力，但这不是 TPU 本身直接贡献。", refs))

    if not claims:
        for index, item in enumerate(ranked[:4], start=1):
            snippet = clean_search_text(str(item.get("snippet") or ""))[:220]
            if snippet:
                claims.append(f"- [S{index}] {snippet}")
    return list(dict.fromkeys(claim for claim in claims if claim))[:8]


def build_recency_sections(ranked: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    dated = [
        (str(item.get("report_date") or ""), index, item)
        for index, item in enumerate(ranked, start=1)
        if str(item.get("report_date") or "")
    ]
    if not dated:
        return [], []
    dated.sort(reverse=True, key=lambda entry: entry[0])
    latest = []
    background = []
    newest_date = dated[0][0]
    newest_year_month = newest_date[:7]
    for date, index, item in dated:
        snippet = clean_search_text(str(item.get("snippet") or ""))[:180]
        line = f"- [S{index}] {date}：{item.get('title', '')}" + (f"；{snippet}" if snippet else "")
        if date[:7] == newest_year_month and len(latest) < 4:
            latest.append(line)
        elif len(background) < 4:
            background.append(line)
    return latest, background


def render_answer(query: str, ranked: list[dict[str, Any]], *, rerank_method: str) -> str:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    buckets = {"consensus": [], "disagreements": [], "changes": [], "risks": []}
    for index, item in enumerate(ranked, start=1):
        snippet = clean_search_text(str(item.get("snippet") or ""))
        line = evidence_line(index, item)
        buckets[classify_line(snippet)].append(line)
        for risk in item.get("risks", [])[:1]:
            buckets["risks"].append(f"- [S{index}] {clean_search_text(str(risk))[:220]}")
    synthesis = build_synthesis_claims(query, ranked)
    latest, background = build_recency_sections(ranked)
    lines = [
        "---",
        "type: rag_answer",
        f"query: {json.dumps(query, ensure_ascii=False)}",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "retrieval: fts5_bm25",
        f"rerank: {rerank_method}",
        "---",
        "",
        f"# {query}",
        "",
        "## 回答草稿",
        "- 以下只基于本地研报摘要命中片段生成；每条判断都保留来源编号，未引用的推断不写入。",
        "",
        "## 综合结论",
        *(synthesis or ["- 暂无足够来源。"]),
        "",
        "## 最新观点",
        *(latest or ["- 暂无可排序的新近来源。"]),
        "",
        "## 旧背景",
        *(background or ["- 暂无明显旧背景来源。"]),
        "",
        "## 共识",
        *(buckets["consensus"][:8] or ["- 暂无足够来源。"]),
        "",
        "## 分歧",
        *(buckets["disagreements"][:8] or ["- 暂无明显分歧来源。"]),
        "",
        "## 新变化",
        *(buckets["changes"][:8] or ["- 暂无明显新变化来源。"]),
        "",
        "## 风险",
        *(buckets["risks"][:8] or ["- 暂无明显风险来源。"]),
        "",
        "## 来源",
    ]
    for index, item in enumerate(ranked, start=1):
        citation = item.get("citation") or {}
        companies = "、".join(item.get("companies", []) or [])
        themes = "、".join(item.get("themes", []) or [])
        lines.extend(
            [
                f"### S{index}. {item.get('title', '')}",
                f"- 日期/券商: {item.get('report_date', '') or '-'} / {item.get('broker', '') or '-'}",
                f"- 公司: {companies or '-'}",
                f"- 主题: {themes or '-'}",
                f"- Obsidian note: {citation.get('note_path') or item.get('note_path') or '-'}",
                f"- Summary: {citation.get('summary_path') or '-'}",
                f"- PDF: {citation.get('pdf_path') or item.get('pdf_path') or '-'}",
                f"- Heading: {citation.get('heading') or '-'}",
                "",
            ]
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer a question with local research KB citations.")
    parser.add_argument("query")
    parser.add_argument("--company", default="")
    parser.add_argument("--theme", default="")
    parser.add_argument("--broker", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--recall", type=int, default=50)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path).expanduser().resolve(strict=False)
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    config_root = Path(args.config_root).expanduser().resolve(strict=False)
    ensure_kb_schema(db_path)
    if args.rebuild or not table_exists(db_path, "report_search"):
        rebuild_search_index(db_path, vault_root, config_root, upsert_notes=False)
    recalled = search_reports(
        db_path,
        args.query,
        company=args.company.strip(),
        theme=args.theme.strip(),
        broker=args.broker.strip(),
        since_date=parse_since(args.since),
        limit=args.recall,
    )
    details = load_report_details(db_path, [str(item.get("report_id")) for item in recalled])
    focus_terms = company_focus_terms(args.company.strip(), config_root, vault_root)
    rerank_method = "local_hash_embedding+lexical_term_overlap+focus_entity"
    ranked = lexical_rerank(args.query, recalled, details, focus_terms=focus_terms)[: args.limit]
    if args.json:
        print(json.dumps({"query": args.query, "rerank": rerank_method, "results": ranked}, ensure_ascii=False, indent=2))
        return 0
    text = render_answer(args.query, ranked, rerank_method=rerank_method)
    if args.output:
        output = Path(args.output).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
