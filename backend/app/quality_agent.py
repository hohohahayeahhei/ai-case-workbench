"""A bounded score → independent review → repair loop over fetched evidence."""
from copy import deepcopy
from .agent_model import MockAgentModel
from .agent_skills import skill_text, skill_versions
from .scoring import assessment_errors, review_errors, evidence_fingerprint, hard_flags, assessment_fingerprint
from .db import utc_now
from .evidence_catalog import build_evidence_catalog
from .case_context import evidence_case_context

class QualityAgent:
    def __init__(self, model, event=None):
        self.model=model
        self.event=event or (lambda *a,**k:None)

    def evaluate(self, case, article_text):
        result={'status':'pending','input_fingerprint':evidence_fingerprint(case),
                'model':getattr(self.model,'name',type(self.model).__name__),'skill_versions':skill_versions(case.get('source_type')),
                'attempts':[],'errors':[],'at':utc_now()}
        if isinstance(self.model,MockAgentModel) or hard_flags(case):
            result['errors']=hard_flags(case) or ['未配置真实评审模型，不能给出质量分']
            return result
        payload={'case':evidence_case_context(case),'article_text':article_text[:24000]}
        catalog = build_evidence_catalog(payload['article_text'])
        payload['evidence_catalog'] = catalog
        for attempt in range(2):
            checkpoint={'attempt':attempt+1,'started_at':utc_now()}; result['attempts'].append(checkpoint)
            try:
                self.event('scorer','model.scoring' if not attempt else 'model.scoring.repair','running',case_id=case['case_id'])
                assessment=self.model.structured('scoring',skill_text('score', case.get('source_type')),payload)
                assessment = deepcopy(assessment)
                reference_errors = []
                if isinstance(assessment.get('dimensions'), dict):
                    for key, value in assessment['dimensions'].items():
                        if isinstance(value, dict) and 'evidence_ids' in value:
                            ids = value['evidence_ids']
                            if not isinstance(ids, list) or any(not isinstance(ref, str) or ref not in catalog for ref in ids):
                                reference_errors.append(f'{key} 引用编号必须存在于 evidence_catalog')
                                value['evidence'] = []
                            else:
                                value['evidence'] = [catalog[ref]['text'] for ref in ids]
                                value['evidence_spans'] = [{**catalog[ref], 'id': ref} for ref in ids]
                errors=reference_errors + assessment_errors(assessment,payload['article_text'])
                checkpoint.update(assessment=assessment,validation_errors=errors)
                if not errors:
                    self.event('reviewer','model.quality_review','running',case_id=case['case_id'])
                    review=self.model.structured('quality_review',skill_text('review', case.get('source_type')),{'case':payload['case'],'article_text':payload['article_text'],'evidence_catalog':catalog,'assessment':assessment,
                        'evidence_format': '引文与偏移由程序精确取回，按完整原文核对当前档位。低分但评分准确的候选也可通过复审，由程序另行判断70分入选门槛。'})
                    checkpoint['review']=review
                    if review.get('decision') == 'reject':
                        result.update(status='rejected', review=review, errors=[str(review.get('reason') or '不符合成功案例门槛')])
                        self.event('reviewer', 'quality.case_rejected', 'rejected', case_id=case['case_id'])
                        return result
                    errors=review_errors(review)
                    if not errors:
                        result.update(status='approved',assessment=assessment,review=review,errors=[],reviewed_assessment_hash=assessment_fingerprint(assessment))
                        self.event('reviewer','model.quality_review','approved',case_id=case['case_id'])
                        return result
                result['errors']=errors; checkpoint['errors']=errors
                layers = checkpoint.get('review', {}).get('editorial_checks', {})
                if isinstance(layers, dict) and any(layers.get(key) is False for key in ('factual', 'attribution')):
                    result['repair_target'] = 'extraction'
                    self.event('reviewer','quality.claim_repair_required','pending',case_id=case['case_id'],issues=errors)
                    break  # Changing a score cannot repair the underlying claims.
                payload={**payload,'previous_assessment':assessment,'review_feedback':checkpoint.get('review'),
                         'validation_errors':errors,'repair_instruction':'重读对应原文，按缺口修正档位和推荐理由。无新证据不能补造事实或加分。'}
                self.event('reviewer','quality.repair_required','pending',case_id=case['case_id'],issues=errors)
            except (RuntimeError,ValueError,OSError,TypeError) as exc:
                result['errors']=[f'评审服务未完成：{type(exc).__name__}: {str(exc)[:180]}']
                checkpoint['errors']=result['errors']; break
        self.event('reviewer','quality.pending','needs_scoring',case_id=case['case_id'])
        return result
