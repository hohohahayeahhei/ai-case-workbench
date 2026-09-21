"""Evidence-first extraction for discovered articles.

Without a configured model this agent only extracts safe signals and keeps the
candidate pending.  It never turns a headline into a claimed business result.
"""

from __future__ import annotations

import re
from typing import Any

from .agent_model import AgentModel, MockAgentModel
from .agent_skills import skill_text
from .evidence_catalog import build_evidence_catalog


class ExtractionAgent:
    name = "case-extraction-agent"

    TOOL_PATTERNS = {
        "ChatGPT": r"chatgpt|gpt[- ]?\d",
        "Claude": r"claude",
        "Gemini": r"gemini",
        "Copilot": r"copilot",
        "Cursor": r"cursor",
        "Perplexity": r"perplexity",
        "AI Agent": r"agent|智能体|子代理",
    }

    def extract_signals(self, item: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
        text = " ".join(str(value) for value in (item.get("title_original", ""), item.get("summary", ""), (document or {}).get("text", ""))).lower()
        tools = [name for name, pattern in self.TOOL_PATTERNS.items() if re.search(pattern, text, flags=re.I)]
        metrics = re.findall(r"\b\d+(?:\.\d+)?\s*(?:%|倍|小时|分钟|x)\b", text, flags=re.I)
        action_signals = [token for token in ("自动", "流程", "workflow", "agent", "检索", "生成", "总结", "分析") if token.lower() in text]
        return {
            "extraction_status": "needs_structured_claims",
            "tools_detected": tools,
            "metric_mentions": metrics,
            "action_signals": action_signals,
            "evidence_url": item.get("source_url", ""),
            "reason": "已提取候选信号；问题、方法和结果仍需模型或人工根据原文确认。",
        }

    def extract_case(self, item: dict[str, Any], document: dict[str, Any], model: AgentModel, review_feedback: dict | None = None) -> dict[str, Any]:
        """Ask a configured model for claims, then accept only source-backed output."""
        source_text = str(document.get("text", ""))
        if isinstance(model, MockAgentModel):
            return self.extract_signals(item, document)
        payload = {
                "item": {"title": item.get("title_original", ""), "url": item.get("source_url", ""), "source_type": item.get('source_type')},
                "article_text": source_text[:20000],
        }
        catalog = build_evidence_catalog(payload['article_text'])
        payload['evidence_catalog'] = catalog
        if review_feedback:
            payload.update(previous_output={key: item.get(key) for key in ('problem', 'approach', 'outcome', 'limitations')},
                           review_feedback=review_feedback,
                           repair_instruction='根据复审指出的事实或归因问题重新抽取。删去不受支持或矛盾的数字，区分作者自述与独立验证；引文必须保持原文，不得修改来源。')
        errors = []
        for attempt in range(2):
            result = model.structured("extraction", skill_text("extract", item.get('source_type')), payload)
            if result.get('case_decision') == 'not_case':
                refs = result.get('evidence_ids')
                if isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in catalog for ref in refs) and isinstance(result.get('reason'), str) and result['reason'].strip():
                    return {'extraction_status': 'not_case', 'reason': result['reason'],
                            'evidence_spans': [{'id': ref, **catalog[ref]} for ref in refs], 'attempts': attempt + 1}
            for field in ('problem', 'approach', 'outcome'):
                block = result.get(field)
                if isinstance(block, dict) and 'evidence_ids' in block:
                    refs = block['evidence_ids']
                    if isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in catalog for ref in refs):
                        spans = sorted((catalog[ref] for ref in set(refs)), key=lambda span: span['start'])
                        # A claim's quote must remain one contiguous original passage.
                        if all(a['end'] == b['start'] for a, b in zip(spans, spans[1:])):
                            block['evidence'] = source_text[spans[0]['start']:spans[-1]['end']]
                            block['evidence_spans'] = spans
                        else:
                            block['evidence'] = ''
                    else:
                        block['evidence'] = ''
            errors = self.validation_errors(result, source_text)
            if not errors:
                break
            payload = {**payload, "previous_output": result, "validation_errors": errors,
                       "repair_instruction": "Only retain one observed scenario. Make each claim narrowly supported by its own exact quote. Empty fields are acceptable when evidence is absent."}
        if errors:
            return {
                "extraction_status": "model_rejected",
                "signals": self.extract_signals(item, document),
                "reason": "一次修正后证据仍未通过：" + "；".join(errors),
                "attempts": attempt + 1,
            }
        return {**{field: result[field] for field in ("problem", "approach", "outcome")},
                "limitations": [str(x) for x in result.get("limitations", [])] if isinstance(result.get("limitations"), list) else [],
                "extraction_status": "structured_claims", "attempts": attempt + 1}

    @staticmethod
    def _valid_case_evidence(result: dict[str, Any], source_text: str) -> bool:
        return not ExtractionAgent.validation_errors(result, source_text)

    @staticmethod
    def validation_errors(result: dict[str, Any], source_text: str) -> list[str]:
        if not all(isinstance(result.get(field), dict) for field in ("problem", "approach", "outcome")):
            return ["problem、approach、outcome 必须各为包含 claim、evidence 的对象"]
        errors = []
        compact_source = re.sub(r"\s+", " ", source_text).strip().lower()
        for field in ("problem", "approach", "outcome"):
            block = result[field]
            if not isinstance(block.get("claim"), str) or not isinstance(block.get("evidence"), str):
                errors.append(f"{field}: claim 和 evidence 必须是字符串")
                continue
            claim = str(block.get("claim", "")).strip()
            evidence = re.sub(r"\s+", " ", str(block.get("evidence", ""))).strip().lower()
            if not claim or len(evidence) < 8 or evidence not in compact_source:
                errors.append(f"{field}: 摘要为空、引文过短或引文不是原文的连续文本，请直接复制原句")
            numbers = lambda text: set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))
            if numbers(claim) - numbers(evidence):
                errors.append(f"{field}: 摘要数字 {sorted(numbers(claim) - numbers(evidence))} 未出现在该段引文，请删去不受支持的数字或选择包含它们的原句")
        return errors
