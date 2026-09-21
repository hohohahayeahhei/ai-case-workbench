"""Our evidence anchored rubric; model judgements, independent review and deterministic gates."""
from __future__ import annotations
import hashlib
import json
import re
from .agent_skills import skill_text

SCORING_VERSION = 'case-quality-v4'
THRESHOLDS = {'featured':85, 'selected':70, 'candidate':55}
DIMENSION_MAX = {'source_credibility':15, 'problem_reality':15, 'workflow_completeness':25,
                 'outcome_evidence':25, 'reproducibility':15, 'learning_value':5}
DIMENSION_LABELS = dict(zip(DIMENSION_MAX, ('来源可追溯性','实际问题','方法完整度','结果证据','可复用性','学习价值')))


def evidence_fingerprint(case: dict) -> str:
    data = {key:case.get(key) for key in ('source_url','source_type','title_original','problem','approach','outcome','limitations','verification','article_hash')}
    data['rubric'] = SCORING_VERSION
    data['instructions'] = skill_text('score', case.get('source_type')) + skill_text('review', case.get('source_type'))
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def assessment_fingerprint(assessment: dict) -> str:
    return hashlib.sha256(json.dumps(assessment, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def compact(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip().casefold()


def hard_flags(case: dict) -> list[str]:
    flags = []
    for key in ('problem','approach','outcome'):
        block = case.get(key) or {}
        if not block.get('claim') or not block.get('evidence'):
            flags.append('missing_evidence')
        numbers = lambda s:set(re.findall(r'\d+(?:\.\d+)?',str(s).replace(',','')))
        if numbers(block.get('claim','')) - numbers(block.get('evidence','')):
            flags.append('unsupported_metric')
    if case.get('status') == 'rejected' or case.get('quality_evaluation', {}).get('status') == 'rejected': flags.append('rejected_case')
    if case.get('verification',{}).get('decision') != 'pass':
        flags.append('verification_required')
    return list(dict.fromkeys(flags))


def assessment_errors(assessment, article_text: str) -> list[str]:
    if not isinstance(assessment,dict):
        return ['评分输出必须为对象']
    errors=[]
    judgments=assessment.get('dimensions')
    if not isinstance(judgments,dict) or set(judgments)!=set(DIMENSION_MAX):
        return ['dimensions 必须恰好包含六个评分维度']
    if not isinstance(assessment.get('recommendation_reason'),str) or not assessment['recommendation_reason'].strip():
        errors.append('缺少具体推荐理由')
    for key,value in judgments.items():
        if not isinstance(value,dict):
            errors.append(f'{key} 必须为对象'); continue
        level=value.get('level')
        if type(level) is not int or level not in range(5):
            errors.append(f'{key} level 必须为0—4整数')
        if key=='source_credibility' and level==4:
            errors.append('当前评审只有单篇来源，来源可追溯性最高3档')
        if not isinstance(value.get('reason'),str) or not value['reason'].strip():
            errors.append(f'{key} 缺少档位理由')
        if not isinstance(value.get('missing'),list) or not all(isinstance(x,str) for x in value['missing']):
            errors.append(f'{key} missing 必须为字符串数组')
        quotes=value.get('evidence')
        if not isinstance(quotes,list) or not all(isinstance(x,str) for x in quotes):
            errors.append(f'{key} evidence 必须为原文引文数组')
        elif (level and not quotes) or any(not compact(q) or compact(q) not in compact(article_text) for q in quotes):
            errors.append(f'{key} 引文必须存在于本轮原文，非零档至少一条')
    return errors


def review_errors(review) -> list[str]:
    if not isinstance(review,dict): return ['复审输出必须为对象']
    errors=[]
    if review.get('decision')!='pass': errors.append(str(review.get('reason') or '独立复审要求修正'))
    eligibility = review.get('case_eligibility', {})
    for key in ('real_application', 'successful_core_task', 'same_case'):
        if not isinstance(eligibility, dict) or eligibility.get(key) is not True:
            errors.append(f'成功案例门槛 {key} 未通过')
    checks=review.get('dimension_checks',{})
    for key in DIMENSION_MAX:
        check=checks.get(key) if isinstance(checks,dict) else None
        if not isinstance(check,dict) or check.get('supported') is not True or not check.get('reason'):
            errors.append(f'{key} 档位未获独立复审支持')
    layers=review.get('editorial_checks',{})
    for key in ('factual','attribution','clarity','usefulness'):
        if not isinstance(layers,dict) or layers.get(key) is not True: errors.append(f'编辑质检 {key} 未通过')
    if not isinstance(review.get('issues'),list) or review.get('issues'): errors.append('复审仍有未解决问题或缺少 issues 数组')
    return errors


def score_case(case: dict, model_scorecard: dict | None=None) -> dict:
    # Legacy free-text model_scorecard cannot authorize a numeric assessment.
    flags=hard_flags(case)
    evaluation=case.get('quality_evaluation') or {}
    valid=(evaluation.get('status')=='approved' and evaluation.get('input_fingerprint')==evidence_fingerprint(case)
           and evaluation.get('reviewed_assessment_hash') == assessment_fingerprint(evaluation.get('assessment', {}))
           and not review_errors(evaluation.get('review')))
    judgments=evaluation.get('assessment',{}).get('dimensions',{}) if valid else {}
    valid=valid and set(judgments)==set(DIMENSION_MAX) and all(isinstance(v,dict) and type(v.get('level')) is int and 0<=v['level']<=4 for v in judgments.values())
    points={k:round(judgments[k]['level']*maximum/4,2) if valid else None for k,maximum in DIMENSION_MAX.items()}
    total=int(sum(points.values())+0.5) if valid and not flags else None
    if flags or not valid:
        tier='needs_scoring'
        if case.get('verification',{}).get('decision')=='reject' or 'rejected_case' in flags: tier='rejected'
        elif 'missing_evidence' in flags: tier='needs_extraction'
    else:
        eligible=all(judgments[k]['level']>=2 for k in ('problem_reality','workflow_completeness','outcome_evidence'))
        tier=('featured' if total>=85 else 'selected' if total>=70 else 'candidate') if eligible else 'candidate'
    gaps=[{'dimension':k,'label':DIMENSION_LABELS[k],'missing':v['missing']} for k,v in judgments.items() if v.get('missing')] if valid else []
    reason=evaluation.get('assessment',{}).get('recommendation_reason') if valid else None
    if tier == 'rejected':
        reason=evaluation.get('review',{}).get('reason') or case.get('verification',{}).get('reason') or '原文或复审未通过成功案例门槛。'
    return {'score_version':SCORING_VERSION,'quality_score':total,'tier':tier,'selected':tier in {'featured','selected'},
            'dimensions':points,'dimension_max':DIMENSION_MAX,'dimension_labels':DIMENSION_LABELS,'judgments':judgments if valid else {},
            'hard_flags':flags,'reason':reason or '等待基于原文的评分和独立复审；不以默认分替代评审。',
            'source_note':'可追溯自述不等于独立复现','assessment_status':'approved' if valid and not flags else 'pending',
            'review':evaluation.get('review',{}),'attempts':len(evaluation.get('attempts',[])),'gaps':gaps,
            'pending_reasons':evaluation.get('errors',[])+flags,
            'method':'本项目六维档位评审；模型判断，程序加权及门槛校验；尚未人工校准'}
