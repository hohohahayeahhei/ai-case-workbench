import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from backend.app.quality_agent import QualityAgent
from backend.app.scoring import score_case,assessment_errors,DIMENSION_MAX,assessment_fingerprint
from backend.app.score_maintenance import reconcile_scores
from backend.app.agent_skills import skill_text,skill_versions
from backend.app.db import Database
from backend.app.collection_export import live_records
from backend.app.query_agent import QueryAgent
from backend.app.publication_dates import today_local
from backend.tests.quality_fixtures import assessment,accepted_review,approve

ROOT=Path(__file__).resolve().parents[2]

class QualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.case={'case_id':'synthetic','title_original':'Workflow','source_url':'https://example.com/story',
                   'published_at':today_local().isoformat(), 'source_type':'web','discovery_mode':'web_search','verification':{'decision':'pass'},
                   'problem':{'claim':'Manual ticket reading','evidence':'Staff manually read tickets.'},
                   'approach':{'claim':'AI summarizes tickets','evidence':'AI summarized tickets for a human reviewer.'},
                   'outcome':{'claim':'Report delivered','evidence':'The reviewer delivered a report.'}}
        self.text=' '.join(self.case[k]['evidence'] for k in ('problem','approach','outcome'))

    def test_model_outage_does_not_create_default_score(self):
        model=Mock();model.structured.side_effect=RuntimeError('offline')
        self.case['quality_evaluation']=QualityAgent(model).evaluate(self.case,self.text)
        card=score_case(self.case)
        self.assertIsNone(card['quality_score']);self.assertFalse(card['selected'])
        self.assertEqual(model.structured.call_count,1)

    def test_every_nonzero_dimension_requires_original_quote(self):
        data=assessment(self.case)
        data['dimensions']['learning_value']['evidence']=['invented evidence']
        self.assertTrue(assessment_errors(data,self.text))
        data['dimensions']['learning_value']['evidence']=[]
        self.assertTrue(assessment_errors(data,self.text))

    def test_evidence_ids_resolve_to_exact_source_spans(self):
        proposal=assessment(self.case)
        for value in proposal['dimensions'].values():
            value.pop('evidence');value['evidence_ids']=['E001']
        model=Mock();model.structured.side_effect=[proposal,accepted_review()]
        evaluation=QualityAgent(model).evaluate(self.case,self.text)
        self.assertEqual(evaluation['status'],'approved')
        block=evaluation['assessment']['dimensions']['outcome_evidence']
        self.assertEqual(block['evidence'],[self.text])
        span=block['evidence_spans'][0]
        self.assertEqual(self.text[span['start']:span['end']],span['text'])

    def test_invented_evidence_id_cannot_authorize_scoring(self):
        proposal=assessment(self.case)
        proposal['dimensions']['outcome_evidence']['evidence_ids']=['E999']
        model=Mock();model.structured.return_value=proposal
        evaluation=QualityAgent(model).evaluate(self.case,self.text)
        self.assertEqual(evaluation['status'],'pending')
        self.assertEqual(model.structured.call_count,2)

    def test_review_repair_is_bounded_and_stores_feedback(self):
        proposal=assessment(self.case);bad=accepted_review();bad.update(decision='revise',issues=['outcome not supported'])
        model=Mock();model.structured.side_effect=[proposal,bad,proposal,accepted_review()]
        evaluated=QualityAgent(model).evaluate(self.case,self.text)
        self.assertEqual(evaluated['status'],'approved');self.assertEqual(len(evaluated['attempts']),2)
        self.assertIn('review_feedback',model.structured.call_args_list[2].args[2])
        self.assertEqual([c.args[0] for c in model.structured.call_args_list],['scoring','quality_review','scoring','quality_review'])
        self.case['quality_evaluation']=evaluated
        self.assertEqual(score_case(self.case)['quality_score'],75)

    def test_failed_real_use_cannot_be_published_as_success(self):
        model=Mock();r=accepted_review()
        r['decision']='reject';r['reason']='实际操作造成损失，没有完成用户任务'
        r['case_eligibility']['successful_core_task']=False
        model.structured.side_effect=[assessment(self.case),r]
        self.case['quality_evaluation']=QualityAgent(model).evaluate(self.case,self.text)
        self.assertEqual(score_case(self.case)['tier'],'rejected')
        self.assertIsNone(score_case(self.case)['quality_score'])
        self.assertEqual(model.structured.call_count,2)

    def test_factual_failure_routes_to_claim_repair_and_cannot_publish(self):
        model=Mock();bad=accepted_review();bad['editorial_checks']['factual']=False
        model.structured.side_effect=[assessment(self.case),bad]*2
        self.case['quality_evaluation']=QualityAgent(model).evaluate(self.case,self.text)
        self.assertEqual(model.structured.call_count,2)
        self.assertEqual(self.case['quality_evaluation']['repair_target'], 'extraction')
        self.assertIsNone(score_case(self.case)['quality_score'])

    def test_changed_claim_invalidates_old_approval(self):
        approve(self.case);self.assertTrue(score_case(self.case)['selected'])
        self.case['outcome']['claim']='A different result'
        self.assertIsNone(score_case(self.case)['quality_score'])

    def test_official_domain_and_long_keyword_padding_give_no_free_points(self):
        original=copy.deepcopy(self.case);approve(original)
        padded=copy.deepcopy(self.case);padded['source_url']='https://openai.com/story'
        padded['title_original']='MCP Agent Workflow API '*100
        # Holding evidence judgements fixed, metadata/length cannot change arithmetic.
        approve(padded)
        self.assertEqual(score_case(original)['quality_score'],score_case(padded)['quality_score'])

    def test_changed_assessment_invalidates_review(self):
        approve(self.case)
        self.case['quality_evaluation']['assessment']['dimensions']['outcome_evidence']['level']=4
        self.assertIsNone(score_case(self.case)['quality_score'])

    def test_numeric_hard_gate_overrides_approved_high_score(self):
        self.case['outcome']['claim']='Saved 999 hours'
        approve(self.case,4)
        card=score_case(self.case)
        self.assertIn('unsupported_metric',card['hard_flags']);self.assertFalse(card['selected']);self.assertIsNone(card['quality_score'])

    def test_high_total_cannot_offset_unobserved_outcome(self):
        approve(self.case,4)
        self.case['quality_evaluation']['assessment']['dimensions']['outcome_evidence']['level']=1
        self.case['quality_evaluation']['reviewed_assessment_hash'] = assessment_fingerprint(self.case['quality_evaluation']['assessment'])
        card = score_case(self.case)
        self.assertIsNotNone(card['quality_score'])
        self.assertFalse(card['selected'])

    def test_single_source_cannot_claim_independent_corroboration(self):
        a=assessment(self.case);a['dimensions']['source_credibility']['level']=4
        self.assertTrue(assessment_errors(a,self.text))

    def test_missing_review_dimension_cannot_pass(self):
        model=Mock();r=accepted_review();r['dimension_checks'].pop('learning_value')
        model.structured.side_effect=[assessment(self.case),r]*2
        self.assertEqual(QualityAgent(model).evaluate(self.case,self.text)['status'],'pending')

    def test_rubric_is_actually_loaded_and_hashed_for_both_agents(self):
        for role in ('score','review'):
            self.assertIn('source_credibility / 15',skill_text(role))
            self.assertIn(role,skill_versions())

    def test_rejected_record_cannot_keep_a_legacy_numeric_score(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db');db.init_schema()
            self.case['scorecard']={'score_version':'case-quality-v2','quality_score':72}
            db.save_source_item(self.case,'rejected')
            reconcile_scores(db)
            row=db.source_item(self.case['case_id']);item=json.loads(row['payload_json'])
            self.assertEqual(row['status'],'rejected')
            self.assertIsNone(item['scorecard']['quality_score'])
            self.assertEqual(item['score_history'][0]['scorecard']['quality_score'],72)
            db.close()

    def test_keyword_fallback_is_labeled_and_broad_query_stays_selected(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db');db.init_schema()
            approve(self.case,2);self.case['scorecard']=score_case(self.case)
            db.save_source_item(self.case,'candidate')
            q=QueryAgent(ROOT,Path(d)/'db')
            self.assertEqual(q.query(window='all',source_mode='live')['items'],[])
            result=q.query(query='Workflow',window='all',source_mode='live')
            self.assertTrue(result['meta']['fallback_to_candidates'])
            self.assertFalse(result['items'][0]['selected'])
            self.assertFalse(q.hot(source_mode='live')['meta']['heat_available'])
            q.catalog_db.close();db.close()

    def test_migration_is_idempotent_and_query_export_agree(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(Path(d)/'db');db.init_schema()
            self.case['scorecard']={'score_version':'case-quality-v3','quality_score':72}
            db.save_source_item(self.case,'selected')
            self.assertEqual(reconcile_scores(db),1);self.assertEqual(reconcile_scores(db),0)
            row=db.source_item('synthetic');self.assertEqual(row['status'],'needs_scoring')
            item=json.loads(row['payload_json']);self.assertEqual(item['score_history'][0]['scorecard']['quality_score'],72)
            self.assertFalse(live_records(db))
            approve(item);item['scorecard']=score_case(item);item['status']='selected';db.update_source_item('synthetic','selected',item)
            q=QueryAgent(ROOT,Path(d)/'db')
            self.assertEqual(q.query(window='all',source_mode='live')['items'][0]['quality_score'],75)
            self.assertEqual(live_records(db)[0]['scorecard']['quality_score'],75)
            q.catalog_db.close();db.close()

if __name__=='__main__':unittest.main()
