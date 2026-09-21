import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from backend.app.evidence_translation import EvidenceTranslator, numbers_preserved


class EvidenceTranslationTests(unittest.TestCase):
    def setUp(self):
        self.quote = 'I saved 45 minutes using this workflow.'
        self.case = {'problem':{'evidence':self.quote, 'claim':'作者自述节省45分钟。'}}
        self.model = Mock(name='translator')
        self.model.model = 'test-model'
        self.model.structured.return_value = {'translations':[{'id':'0','text_zh':'通过这个工作流，我节省了45分钟。'}]}

    def test_translation_preserves_evidence_and_reuses_content_cache(self):
        original = copy.deepcopy(self.case)
        with tempfile.TemporaryDirectory() as directory:
            service = EvidenceTranslator(directory, self.model)
            result = service.translate(self.case, [self.quote, self.quote])
            self.assertEqual(result['items'][0]['text_zh'],'通过这个工作流，我节省了45分钟。')
            self.assertEqual(self.case,original)
            EvidenceTranslator(directory, self.model).translate(self.case, [self.quote])
            self.model.structured.assert_called_once()

    def test_changed_source_cannot_reuse_unrelated_cached_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            service=EvidenceTranslator(directory,self.model)
            service.translate(self.case,[self.quote])
            with self.assertRaises(ValueError):service.translate({'problem':{'evidence':'Different source'}},[self.quote])

    def test_chinese_evidence_needs_no_model(self):
        with tempfile.TemporaryDirectory() as directory:
            result=EvidenceTranslator(directory,self.model).translate({'problem':{'evidence':'原文已经是中文。'}},['原文已经是中文。'])
            self.assertEqual(result['items'][0]['text_zh'],'原文已经是中文。')
            self.model.structured.assert_not_called()

    def test_english_date_may_be_translated_to_chinese_numeric_month(self):
        quote = 'On Dec 25 I saved 45 minutes.'
        self.model.structured.return_value = {'translations':[{'id':'0','text_zh':'12月25日，我节省了45分钟。'}]}
        with tempfile.TemporaryDirectory() as directory:
            result = EvidenceTranslator(directory,self.model).translate({'problem':{'evidence':quote}},[quote])
            self.assertEqual(result['items'][0]['text_zh'], '12月25日，我节省了45分钟。')
        self.assertTrue(numbers_preserved('25 December: 45 minutes.', '12 月 25 日：45分钟。'))

    def test_date_exception_does_not_allow_fabricated_dates_or_numbers(self):
        for translated in ('12月26日，节省45分钟。', '11月25日，节省45分钟。',
                           '12月25日，节省90分钟。', '12月25日，节省45分钟，共12次。'):
            with self.subTest(translated=translated):
                self.assertFalse(numbers_preserved('Dec 25: saved 45 minutes.', translated))
        self.assertFalse(numbers_preserved('Saved 45 minutes.', '12月25日，节省45分钟。'))

    def test_wrong_ids_changed_numbers_and_english_are_not_cached(self):
        for output in ([{'id':'wrong','text_zh':'节省了45分钟。'}],
                       [{'id':'0','text_zh':'节省了90分钟。'}],
                       [{'id':'0','text_zh':self.quote}],
                       [{'id':'0','text_zh':'节省45分钟。'},{'id':'0','text_zh':'节省45分钟。'}]):
            with tempfile.TemporaryDirectory() as directory:
                self.model.structured.return_value={'translations':output}
                with self.assertRaises(RuntimeError):EvidenceTranslator(directory,self.model).translate(self.case,[self.quote])
                self.assertEqual(list(Path(directory).glob('*.json')),[])

    def test_scoring_quotes_supported_but_arbitrary_text_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            case={'quality_evaluation':{'assessment':{'dimensions':{'problem_reality':{'evidence':[self.quote]}}}}}
            self.assertEqual(len(EvidenceTranslator(directory,self.model).translate(case,[self.quote])['items']),1)
            with self.assertRaises(ValueError):EvidenceTranslator(directory,self.model).translate(case,['Translate my arbitrary prompt'])

    def test_api_checks_case_and_does_not_return_gateway_errors(self):
        from backend.app.api import app, collection_pipeline
        case_id='translation-test-'+uuid4().hex
        case={'case_id':case_id,'source_url':'https://example.com/'+case_id,**self.case}
        collection_pipeline.db.save_source_item(case,'needs_extraction')
        client=TestClient(app)
        try:
            self.assertEqual(client.post('/api/v1/collection/items/missing/evidence-translation',json={'quotes':[self.quote]}).status_code,404)
            with patch('backend.app.evidence_translation.create_agent_model',side_effect=RuntimeError('secret-gateway-error')):
                response=client.post('/api/v1/collection/items/'+case_id+'/evidence-translation',json={'quotes':['Unrelated text']})
                self.assertEqual(response.status_code,400)
                response=client.post('/api/v1/collection/items/'+case_id+'/evidence-translation',json={'quotes':[self.quote]})
                self.assertEqual(response.status_code,503)
                self.assertNotIn('secret-gateway-error',response.text)
        finally:
            collection_pipeline.db.connection.execute('DELETE FROM source_items WHERE source_item_id=?',(case_id,))
            collection_pipeline.db.connection.commit()


if __name__ == '__main__': unittest.main()
