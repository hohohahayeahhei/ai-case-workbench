"""Video-specific policy revisions must not invalidate ordinary article approvals."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.agent_skills import ROOT as SKILL_ROOT, skill_text
from backend.app.scoring import SCORING_VERSION, score_case, evidence_fingerprint
from backend.tests.quality_fixtures import approve


class DouyinSkillScopeTests(unittest.TestCase):
    def case(self):
        return {'case_id': 'scope-test', 'source_url': 'https://example.com/story', 'source_type': 'web',
                'title_original': '实际客户报告', 'status': 'selected',
                'problem': {'claim': '需要手工逐条阅读客户问题。', 'evidence': '需要手工逐条阅读客户问题。'},
                'approach': {'claim': '使用人工智能总结工单并由人工核对。', 'evidence': '使用人工智能总结工单并由人工核对。'},
                'outcome': {'claim': '团队完成了包含问题清单的报告。', 'evidence': '团队完成了包含问题清单的报告。'}}

    def test_original_article_fingerprint_and_approval_survive_video_feature(self):
        case = approve(self.case())
        # Reconstruct the persisted pre-video contract from the original article
        # skills and rubric. Extra source policy must not silently change it.
        original = {}
        rubric = (SKILL_ROOT / 'case-score/references/rubric.md').read_text(encoding='utf-8')
        for role in ('score', 'review'):
            body = (SKILL_ROOT / f'case-{role}/SKILL.md').read_text(encoding='utf-8').split('---', 2)[2].strip()
            original[role] = body + '\n\n# 运行时加载的评分规则\n' + rubric
        data = {key: case.get(key) for key in ('source_url', 'source_type', 'title_original', 'problem', 'approach', 'outcome', 'limitations', 'verification', 'article_hash')}
        data.update(rubric=SCORING_VERSION, instructions=original['score'] + original['review'])
        legacy = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        case['quality_evaluation']['input_fingerprint'] = legacy
        self.assertEqual(evidence_fingerprint(case), legacy)
        self.assertTrue(score_case(case)['selected'])

    def test_changing_video_skill_invalidates_video_approval_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('case-score', 'case-review', 'case-douyin'):
                shutil.copytree(SKILL_ROOT / name, root / name)
            with patch('backend.app.agent_skills.ROOT', root):
                article = approve(self.case())
                video = copy.deepcopy(self.case())
                video['source_type'] = 'douyin'
                approve(video)
                policy = root / 'case-douyin/SKILL.md'
                policy.write_text(policy.read_text(encoding='utf-8') + '\n新增受控视频核验条件。\n', encoding='utf-8')
                self.assertTrue(score_case(article)['selected'])
                self.assertFalse(score_case(video)['selected'])
                self.assertEqual(skill_text('score'), skill_text('score', 'web'))
                self.assertNotEqual(skill_text('score'), skill_text('score', 'douyin'))


if __name__ == '__main__':
    unittest.main()
