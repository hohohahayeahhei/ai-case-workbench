"""Synthetic evaluator contracts for engineering tests, not quality labels."""
from backend.app.scoring import DIMENSION_MAX,evidence_fingerprint,assessment_fingerprint


def assessment(case,level=3):
    quote=case['approach']['evidence']
    return {'recommendation_reason':'作者自述的流程值得借鉴，实际效果尚未独立复现。',
            'dimensions':{key:{'level':min(level,3) if key=='source_credibility' else level,
                              'reason':'测试用档位理由，不代表人工标签','evidence':[quote],
                              'missing':['尚缺更完整记录'] if level<4 else []} for key in DIMENSION_MAX}}


def accepted_review():
    return {'decision':'pass','reason':'测试复审通过','issues':[],
            'case_eligibility':{k:True for k in ('real_application','successful_core_task','same_case')},
            'dimension_checks':{k:{'supported':True,'reason':'测试逐维依据'} for k in DIMENSION_MAX},
            'editorial_checks':{k:True for k in ('factual','attribution','clarity','usefulness')}}


def approve(case,level=3):
    case.setdefault('verification',{'decision':'pass','reason':'test source verified'})
    case['quality_evaluation']={'status':'approved','input_fingerprint':evidence_fingerprint(case),
                                'assessment':assessment(case,level),'review':accepted_review(),'attempts':[{}]}
    case["quality_evaluation"]["reviewed_assessment_hash"] = assessment_fingerprint(case["quality_evaluation"]["assessment"])
    return case
