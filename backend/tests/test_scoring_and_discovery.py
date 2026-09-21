import unittest
from pathlib import Path

from backend.app.discovery_agent import SourceDiscoveryAgent
from backend.app.scoring import SCORING_VERSION, score_case
from backend.app.extraction_agent import ExtractionAgent
from backend.tests.quality_fixtures import approve


ROOT = Path(__file__).resolve().parents[2]


class ScoringAndDiscoveryTests(unittest.TestCase):
    def test_score_is_program_computed_and_versioned(self):
        case = SourceDiscoveryAgent(ROOT).fixture_candidates("online_snapshot")[0]
        # Use synthetic, internally matching claims for arithmetic, not labels for a real article.
        for key in ("problem", "approach", "outcome"):
            case[key]["claim"] = case[key]["evidence"]
        approve(case)
        result = score_case(case)
        self.assertEqual(result["score_version"], SCORING_VERSION)
        self.assertGreaterEqual(result["quality_score"], 0)
        self.assertLessEqual(result["quality_score"], 100)
        self.assertIn(result["tier"], {"featured", "selected", "candidate", "rejected"})

    def test_unsupported_metric_is_not_selected(self):
        case = {
            "source_type": "marketing",
            "problem": {"claim": "团队希望提高效率", "evidence": "页面提及问题"},
            "approach": {"claim": "接入 AI 自动化流程", "evidence": "页面提及功能"},
            "outcome": {"claim": "效率提升 300%", "evidence": "宣传语"},
        }
        result = score_case(case)
        self.assertIn("unsupported_metric", result["hard_flags"])
        self.assertFalse(result["selected"])

    def test_source_channel_does_not_collapse_scores(self):
        base = {
            "source_type": "web",
            "source_url": "https://example.com/case",
            "problem": {"claim": "团队每天需要处理大量客户问题", "evidence": "之前客户问题都由人工逐条处理，导致项目排期变慢。"},
            "approach": {"claim": "团队先用 AI 提取问题，再通过 API 自动生成回复", "evidence": "文章记录了输入数据、调用工具和输出结果。"},
            "outcome": {"claim": "处理时间减少", "evidence": "作者表示实际处理时间减少，并保留了前后记录。"},
            "limitations": [],
        }
        official = {**base, "source_url": "https://academy.openai.com/case"}
        personal = {**base, "source_url": "https://example.com/personal"}
        approve(official); approve(personal)
        self.assertEqual(score_case(official)["quality_score"], score_case(personal)["quality_score"])
        self.assertEqual(score_case(official)["judgments"]["source_credibility"]["level"], 3)

    def test_evidence_richness_changes_score(self):
        sparse = {
            "source_type": "web", "source_url": "https://example.com",
            "problem": {"claim": "有问题", "evidence": "文章提到问题。"},
            "approach": {"claim": "使用 AI", "evidence": "文章提到使用 AI。"},
            "outcome": {"claim": "有改善", "evidence": "文章提到有改善。"},
        }
        rich = {**sparse,
                "problem": {"claim": "团队每天处理客户问题导致交付变慢", "evidence": "作者描述了原有人工流程、发生频率和对项目排期的影响。"},
                "approach": {"claim": "先把客户记录输入 AI，再调用 API 生成结构化回复并由人复核", "evidence": "原文给出了输入、提示词、API 调用、输出和人工复核步骤。"},
                "outcome": {"claim": "处理时间减少", "evidence": "作者对比了改造前后的实际记录，并说明了适用边界。"}}
        # Text length alone cannot replace an evidence assessment.
        self.assertIsNone(score_case(rich)["quality_score"])
        self.assertIsNone(score_case(sparse)["quality_score"])

    def test_fixture_discovery_is_deterministic(self):
        agent = SourceDiscoveryAgent(ROOT)
        items = agent.fixture_candidates("online_snapshot")
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item["discovery_mode"] == "fixture" for item in items))

    def test_community_article_is_not_labeled_vendor_official(self):
        agent = SourceDiscoveryAgent(ROOT)
        for url in ("https://cloud.tencent.com/developer/article/123", "https://developer.cloud.tencent.com/article/123"):
            self.assertEqual(agent.source_label(url), "腾讯云开发者社区（作者投稿）")

    def test_source_metadata_distinguishes_primary_and_discovery(self):
        agent = SourceDiscoveryAgent(ROOT)
        official = agent.source_metadata("https://openai.com/news/example")
        social = agent.source_metadata("https://x.com/OpenAI/status/123")
        media = agent.source_metadata("https://www.theverge.com/ai-artificial-intelligence/example")
        self.assertEqual(official["source_role"], "primary")
        self.assertEqual(official["evidence_policy"], "eligible")
        self.assertEqual(social["source_name"], "OpenAI 官方 X")
        self.assertEqual(social["evidence_policy"], "requires_original_link")
        self.assertEqual(media["source_role"], "discovery")

    def test_search_branches_are_tier_aware(self):
        branches = SourceDiscoveryAgent(ROOT).search_branches("AI 工作流")
        by_id = {branch["id"]: branch for branch in branches}
        self.assertIn("official", by_id)
        self.assertIn("practitioner", by_id)
        self.assertIn("context", by_id)
        self.assertIn("site:openai.com", by_id["official"]["query"])
        self.assertIn("site:simonwillison.net", by_id["practitioner"]["query"])

    def test_atom_parser_keeps_untrusted_feed_as_candidate(self):
        payload = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>AI customer workflow</title><link href='https://example.com/a'/><updated>2026-09-12</updated><summary>Example</summary></entry></feed>"""
        items = SourceDiscoveryAgent._parse_feed(payload, "https://example.com/feed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "needs_extraction")

    def test_extractor_only_returns_signals_without_claims(self):
        result = ExtractionAgent().extract_signals({"title_original": "用 Claude 自动总结客服记录", "source_url": "https://example.com"}, {"text": "团队用 Claude 自动总结客服记录，节省 30 分钟。"})
        self.assertEqual(result["extraction_status"], "needs_structured_claims")
        self.assertIn("Claude", result["tools_detected"])
        self.assertEqual(result["metric_mentions"], ["30 分钟"])

    def test_model_extraction_accepts_only_evidence_found_in_article(self):
        class SafeModel:
            def structured(self, role, system, payload):
                self.role = role
                return {
                    "problem": {"claim": "客户问题需要人工处理", "evidence": "客户问题需要人工处理"},
                    "approach": {"claim": "团队使用 Claude 自动总结", "evidence": "团队使用 Claude 自动总结"},
                    "outcome": {"claim": "处理时间减少 30 分钟", "evidence": "处理时间减少 30 分钟"},
                    "limitations": [],
                }

        text = "客户问题需要人工处理。团队使用 Claude 自动总结。处理时间减少 30 分钟。"
        result = ExtractionAgent().extract_case(
            {"title_original": "客服自动化", "source_url": "https://example.com"},
            {"text": text},
            SafeModel(),
        )
        self.assertEqual(result["extraction_status"], "structured_claims")

    def test_model_extraction_rejects_fabricated_evidence(self):
        class UnsafeModel:
            def structured(self, role, system, payload):
                return {
                    "problem": {"claim": "真实问题", "evidence": "文章没有这句话"},
                    "approach": {"claim": "真实方法", "evidence": "团队使用 Claude"},
                    "outcome": {"claim": "真实结果", "evidence": "处理时间减少"},
                    "limitations": [],
                }

        result = ExtractionAgent().extract_case(
            {"title_original": "客服自动化", "source_url": "https://example.com"},
            {"text": "团队使用 Claude。处理时间减少。"},
            UnsafeModel(),
        )
        self.assertEqual(result["extraction_status"], "model_rejected")


if __name__ == "__main__":
    unittest.main()
