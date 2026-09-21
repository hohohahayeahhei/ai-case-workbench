"""Role schemas constrain syntax; existing evidence gates still decide truth."""
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, Field
from typing import Literal

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Claim(Contract):
    claim: StrictStr
    evidence_ids: list[StrictStr]

class Extraction(Contract):
    case_decision: Literal['case', 'not_case', 'incomplete']
    reason: StrictStr
    evidence_ids: list[StrictStr]
    problem: Claim
    approach: Claim
    outcome: Claim
    limitations: list[StrictStr]

class Verification(Contract):
    decision: Literal['pass', 'reject']
    reason: StrictStr
    limitations: list[StrictStr]

class Dimension(Contract):
    level: StrictInt = Field(ge=0, le=4)
    reason: StrictStr
    evidence_ids: list[StrictStr]
    missing: list[StrictStr]

class Dimensions(Contract):
    source_credibility: Dimension
    problem_reality: Dimension
    workflow_completeness: Dimension
    outcome_evidence: Dimension
    reproducibility: Dimension
    learning_value: Dimension

class Assessment(Contract):
    dimensions: Dimensions
    recommendation_reason: StrictStr

class Check(Contract):
    supported: StrictBool
    reason: StrictStr

class DimensionChecks(Contract):
    source_credibility: Check
    problem_reality: Check
    workflow_completeness: Check
    outcome_evidence: Check
    reproducibility: Check
    learning_value: Check

class Eligibility(Contract):
    real_application: StrictBool
    successful_core_task: StrictBool
    same_case: StrictBool
    reason: StrictStr

class Editorial(Contract):
    factual: StrictBool
    attribution: StrictBool
    clarity: StrictBool
    usefulness: StrictBool

class Review(Contract):
    decision: Literal['pass', 'revise', 'reject']
    reason: StrictStr
    case_eligibility: Eligibility
    dimension_checks: DimensionChecks
    editorial_checks: Editorial
    issues: list[StrictStr]

class TranslatedQuote(Contract):
    id: StrictStr
    text_zh: StrictStr

class EvidenceTranslation(Contract):
    translations: list[TranslatedQuote]

CONTRACTS = {'extraction': Extraction, 'verification': Verification,
             'scoring': Assessment, 'quality_review': Review,
             'evidence_translation': EvidenceTranslation}

def role_contract(role, payload):
    # The legacy demo verifier has a different contract. Live cases and all
    # extraction/review calls use the strict schema; demos keep their own gates.
    if role == 'verification' and 'article_text' not in payload:
        return None
    return CONTRACTS.get(role)
