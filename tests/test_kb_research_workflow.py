from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import kb_answer  # noqa: E402
import kb_common as kb  # noqa: E402
import kb_health_report  # noqa: E402


class KBResearchWorkflowTests(unittest.TestCase):
    def make_workspace(self) -> tuple[Path, Path, Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        vault = tmp / "ResearchVault"
        library = tmp / "ResearchLibrary"
        config = library / "config"
        db_path = library / "state" / "processed_files.sqlite"
        reports = vault / "10_Reports" / "batch"
        reports.mkdir(parents=True)
        (vault / "20_Themes").mkdir(parents=True)
        (vault / "30_Companies").mkdir(parents=True)
        (library / "pdfs" / "batch").mkdir(parents=True)
        (library / "summaries" / "batch").mkdir(parents=True)
        config.mkdir(parents=True)
        (config / "entities.yml").write_text(
            """
version: 1
companies:
  - name: Google
    aliases: [Google, 谷歌, Alphabet, GOOGL, TPU, Google Cloud, Anthropic]
    tickers: [GOOGL.US]
    regions: [美国]
    industries: [云计算, AI基础设施]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (config / "themes.yml").write_text(
            """
version: 1
themes:
  - name: 云厂商资本开支
    keywords: [云厂商, 资本开支, Google Cloud, TPU, backlog, 利润率]
    regions: [美国]
    industries: [云计算, AI基础设施]
    subthemes:
      - name: TPU/backlog
        keywords: [TPU, backlog, Anthropic, 积压订单]
      - name: 云利润率
        keywords: [利润率, margin, 折旧]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (config / "report_metadata_schema.yml").write_text("version: 1\n", encoding="utf-8")
        pdf_path = library / "pdfs" / "batch" / "ubs-google-260603.pdf"
        summary_path = library / "summaries" / "batch" / "ubs-google-260603.summary.md"
        pdf_path.write_bytes(b"%PDF-test")
        summary_path.write_text("# summary", encoding="utf-8")
        (reports / "google.md").write_text(
            f"""---
type: report
report_id: r_google
title: 瑞银-Alphabet-TPU backlog 与利润率-260603
pdf_path: {pdf_path.as_uri()}
summary_md_path: {summary_path.as_uri()}
feishu_doc_url: https://example.com/doc
downloaded_at: "2026-06-03T10:00:00+08:00"
---

# 瑞银-Alphabet-TPU backlog 与利润率-260603

## 摘要

# 瑞银 Alphabet TPU backlog 与利润率

## 核心结论
- Google TPU 和 Anthropic 订单会带来 backlog 转化收入，但折旧会压制 Google Cloud 利润率。

## 核心问题与回答
### Google TPU 对利润率有什么影响？
TPU 自研芯片有成本优势，但云资本开支和折旧节奏会带来 margin compression 风险。
""",
            encoding="utf-8",
        )
        return vault, library, config, db_path

    def test_metadata_fts_base_answer_and_health(self) -> None:
        vault, library, config, db_path = self.make_workspace()
        notes = kb.load_report_notes(vault)
        metadata = kb.extract_report_metadata(notes[0], kb.load_kb_configs(config), vault)

        self.assertEqual(metadata["broker"], "瑞银")
        self.assertEqual(metadata["report_date"], "2026-06-03")
        self.assertIn("Google", metadata["companies"])
        self.assertIn("云厂商资本开支", metadata["themes"])
        self.assertIn("TPU/backlog", metadata["subthemes"])
        self.assertEqual(metadata["report_type"], "company")
        self.assertEqual(metadata["company_scope"], "matched")

        kb.rebuild_search_index(db_path, vault, config)
        results = kb.search_reports(db_path, "Google TPU 利润率", company="Google", limit=5)
        self.assertEqual(len(results), 1)
        self.assertNotIn("file:///", results[0]["snippet"])
        self.assertNotIn("pdf_path", results[0]["snippet"])
        natural_results = kb.search_reports(db_path, "Google TPU 对利润率有什么影响", company="Google", limit=5)
        self.assertEqual(len(natural_results), 1)

        details = kb_answer.load_report_details(db_path, [results[0]["report_id"]])
        ranked = kb_answer.lexical_rerank("Google TPU 利润率", results, details)
        answer = kb_answer.render_answer("Google TPU 对利润率有什么影响", ranked, rerank_method="lexical_term_overlap")
        self.assertIn("综合结论", answer)
        self.assertIn("S1", answer)
        self.assertIn("Summary:", answer)
        self.assertIn("PDF:", answer)

        base = kb.render_reports_base()
        self.assertIn('file.inFolder("10_Reports")', base)
        self.assertIn("PCB/电子制造", base)
        self.assertIn("需要处理", base)
        self.assertIn("report_type", base)
        self.assertIn("company_scope", base)
        self.assertIn("需补实体", base)

        summary, buckets = kb_health_report.build_health(
            vault_root=vault,
            library_root=library,
            config_root=config,
            db_path=db_path,
        )
        self.assertEqual(summary["report_note_count"], 1)
        self.assertEqual(summary["pdf_count"], 1)
        self.assertEqual(summary["missing_raw"], 1)
        self.assertEqual(summary["company_matched"], 1)
        self.assertEqual(summary["company_scope_covered"], 1)
        text = kb_health_report.render_health_report(summary, buckets, "2026-06-15T00:00:00+08:00")
        self.assertIn("kb_maintain_wiki.py", text)
        self.assertIn("kb_answer.py", text)

    def test_report_date_does_not_parse_stock_code_as_future_date(self) -> None:
        title = "摩根士丹利-中天科技（600522）：确认AIDC推动的光纤需求增长-260608"
        parsed = kb.extract_report_date({}, title, Path("batch/摩根士丹利-中天科技（600522）：确认AIDC推动的光纤需求增长-260608.md"))
        self.assertEqual(parsed, "2026-06-08")

        no_date = kb.extract_report_date({"report_date": "2060-05-22"}, "中天科技（600522）", Path("batch/note.md"))
        self.assertEqual(no_date, "")

    def test_extracts_unconfigured_company_from_stock_code_title(self) -> None:
        note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": "高盛-贵州茅台（600519-SH）：股东大会要点-260612"},
            body="# 高盛-贵州茅台（600519-SH）：股东大会要点-260612\n",
            title="高盛-贵州茅台（600519-SH）：股东大会要点-260612",
            display_title="高盛-贵州茅台（600519-SH）：股东大会要点-260612",
            link_target="10_Reports/note",
        )
        entities = kb.extract_entities(note, {"entities": {"companies": []}}, None)
        self.assertIn("贵州茅台", entities["companies"])

    def test_extracts_company_from_title_signals_without_config_alias(self) -> None:
        titles = {
            "瑞银-Hugo-Boss-AG（BOSSn-DE）：2026年一季度营收超预期-260505": "Hugo-Boss-AG",
            "商汤科技：新一代AI模型与架构将发布，下半年业绩超预期": "商汤科技",
            "260507-GS-影石创新：短期内存成本负担；持续的产品迁移是长期收益；买入": "影石创新",
            "Naver一季度电商强劲，但GPU投入拖累利润率": "Naver",
            "260313-GS-台湾科技：Jentech（3653.TW）：管理层访问": "Jentech",
            "瑞银-澜起科技~H（6809.HK）全球AI基础设施投资与创新的关键受益者；首次覆盖给予买入评级": "澜起科技",
            "康诺亚-B：斯塔帕基巴特放量按计划推进，2026年销售目标指向75亿元": "康诺亚",
            "华勤技术：从消费电子ODM龙头走向AI服务器增量": "华勤技术",
            "壁仞科技：AI芯片放量加速，2028年前收入高增": "壁仞科技",
            "摩根士丹利-中天科技-600522-：结构性增长标志强劲盈利前景": "中天科技",
            "高盛-生益科技-600183-：AI需求增长支撑CCL价格上涨": "生益科技",
            "260512-GS-Life360公司：2026年第一季度回顾：订阅用户表现强劲": "Life360公司",
            "东方电缆：8%跌幅计入了多少地缘风险？": "东方电缆",
        }
        for title, company in titles.items():
            with self.subTest(title=title):
                note = kb.NoteRecord(
                    path=Path("note.md"),
                    frontmatter={"title": title},
                    body=f"# {title}\n",
                    title=title,
                    display_title=title,
                    link_target="10_Reports/note",
                )
                entities = kb.extract_entities(note, {"entities": {"companies": []}}, None)
                self.assertIn(company, entities["companies"])

    def test_does_not_extract_generic_industry_title_as_company(self) -> None:
        titles = [
            "中国航空公司：五一假期低于预期，燃油价格仍是核心变量",
            "中国白酒四季度去库存加速，一季度降幅收窄",
            "全球模拟半导体，这一轮会不一样吗",
            "260506-GS-宏观经济概览：最新观点和预测",
            "野村证券-亚洲洞察：中国：上调CPI和PPI通胀预测-260310",
            "260312-GS-亚太能源：提高对中国石油巨头的目标价",
            "人工智能采用率追踪：2026年3月采用率稳定在18.9%",
            "中国智能手机2月跟踪：出货承压，规格升级延续",
            "全球AI光模块：行业需求或将更加强劲",
            "野村-AI半导体与科技：GTC 2026：英伟达指引更强劲的2027年",
            "全球超大规模企业3月季度业绩中的AI趋势解读",
        ]
        for title in titles:
            with self.subTest(title=title):
                note = kb.NoteRecord(
                    path=Path("note.md"),
                    frontmatter={"title": title},
                    body=f"# {title}\n",
                    title=title,
                    display_title=title,
                    link_target="10_Reports/note",
                )
                entities = kb.extract_entities(note, {"entities": {"companies": []}}, None)
                self.assertEqual(entities["companies"], [])

    def test_macro_report_without_company_is_not_entity_gap(self) -> None:
        title = "260506-GS-宏观经济概览：CPI 通胀和美联储利率预测"
        note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": title},
            body=f"# {title}\n\nCPI 通胀、PMI 和美联储利率仍是市场主要变量。\n",
            title=title,
            display_title=title,
            link_target="10_Reports/note",
        )
        metadata = kb.extract_report_metadata(
            note,
            {"entities": {"companies": []}, "themes": {"themes": []}},
            None,
        )
        self.assertEqual(metadata["companies"], [])
        self.assertEqual(metadata["report_type"], "macro")
        self.assertEqual(metadata["company_scope"], "not_applicable")
        self.assertIn("company_not_applicable", metadata["quality_status"])
        self.assertNotIn("unmatched_company", metadata["quality_status"])

    def test_does_not_duplicate_config_alias_as_inferred_company(self) -> None:
        note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": "谷歌云Next 2026要点：代理式AI从试点走向规模化生产"},
            body="# 谷歌云Next 2026要点\nGoogle Cloud TPU 利润率\n",
            title="谷歌云Next 2026要点：代理式AI从试点走向规模化生产",
            display_title="谷歌云Next 2026要点：代理式AI从试点走向规模化生产",
            link_target="10_Reports/note",
        )
        configs = {
            "entities": {
                "companies": [
                    {
                        "name": "Google",
                        "aliases": ["Google", "谷歌", "谷歌云", "Google Cloud", "TPU"],
                    }
                ]
            }
        }
        entities = kb.extract_entities(note, configs, None)
        self.assertIn("Google", entities["companies"])
        self.assertNotIn("谷歌云", entities["companies"])

    def test_short_chinese_alias_avoids_known_non_company_context(self) -> None:
        configs = {
            "entities": {
                "companies": [
                    {
                        "name": "高通",
                        "aliases": ["高通", "Qualcomm"],
                    }
                ]
            }
        }
        macro_note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": "油价上行与美联储加息风险"},
            body="# 油价上行与美联储加息风险\n高通胀压力和高通膨风险可能影响美联储。\n",
            title="油价上行与美联储加息风险",
            display_title="油价上行与美联储加息风险",
            link_target="10_Reports/note",
        )
        macro_entities = kb.extract_entities(macro_note, configs, None)
        self.assertNotIn("高通", macro_entities["companies"])

        company_note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": "高通三月季度财报要点"},
            body="# 高通三月季度财报要点\n手机芯片需求回升。\n",
            title="高通三月季度财报要点",
            display_title="高通三月季度财报要点",
            link_target="10_Reports/note",
        )
        company_entities = kb.extract_entities(company_note, configs, None)
        self.assertIn("高通", company_entities["companies"])

    def test_summary_matching_uses_current_report_section(self) -> None:
        title = "野村证券-亚洲洞察：中国：上调CPI和PPI通胀预测-260310"
        body = f"""# {title}

## 摘要

## 报告1：野村证券-中国：1~2月进出口增速双双飙升-260310.pdf

这份报告讨论进出口，不应该进入当前报告。

## 报告2：野村证券-亚洲洞察：中国：上调CPI和PPI通胀预测-260310.pdf

这份报告受中东冲突导致全球能源价格飙升影响，上调CPI和PPI通胀预测。

## 报告3：瑞银-中国油气化工行业：中国石油和化工企业对油价变动的盈利敏感性分析-260311.pdf

这份报告分析中国石油和中海油盈利敏感性，不应该进入当前报告。
"""
        note = kb.NoteRecord(
            path=Path("note.md"),
            frontmatter={"title": title},
            body=body,
            title=title,
            display_title=title,
            link_target="10_Reports/note",
        )
        summary = kb.extract_note_summary_text(note)
        self.assertIn("上调CPI和PPI通胀预测", summary)
        self.assertNotIn("中国石油和中海油", summary)


if __name__ == "__main__":
    unittest.main()
