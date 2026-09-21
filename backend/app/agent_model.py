"""Model boundary for the three specialist agents.

The default provider is deterministic so the demo and tests remain offline.
An OpenAI-compatible provider can be enabled with environment variables later;
credentials are read only from the process environment and never persisted.
"""

from __future__ import annotations

import json
import os
import re
import time
from http.client import IncompleteRead, RemoteDisconnected
from urllib.parse import urlparse
import urllib.error
import urllib.request
from dataclasses import dataclass
from copy import deepcopy
from pydantic import ValidationError
from .output_contracts import role_contract
from .network_policy import request_timeout, retry_delay, TRANSIENT_HTTP
from typing import Any, Dict, List, Protocol

from .config import load_local_env
from .agent_skills import skill_text


load_local_env()


class AgentModel(Protocol):
    name: str

    def structured(self, role: str, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...


class InvalidStructuredOutput(RuntimeError):
    """A completed response whose content does not satisfy the JSON contract."""


@dataclass
class MockAgentModel:
    """Offline model substitute that still exercises role-separated calls."""

    name: str = "mock-llm"

    def structured(self, role: str, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if role == "research":
            return {"selected_case_ids": [case["case_id"] for case in payload.get("candidates", [])]}
        if role == "verification":
            case = payload["case"]
            if case.get("expected_label") == "reject_unsupported_metric":
                return {"decision": "reject", "reason": "数字缺少可验证出处", "confidence": 0.98}
            if case.get("expected_label") == "reject_duplicate":
                return {"decision": "reject", "reason": "疑似重复案例，交由程序去重", "confidence": 0.95}
            return {"decision": "pass", "reason": "原文支持问题、方法和结果", "confidence": 0.93}
        if role == "editor":
            return {"items": [
                {
                    "case_id": case["case_id"],
                    "title": case["title_original"],
                    "source_url": case["source_url"],
                    "problem": case["problem"]["claim"],
                    "approach": case["approach"]["claim"],
                    "outcome": case["outcome"]["claim"],
                }
                for case in payload.get("verified_cases", [])
            ]}
        if role == "scoring":
            case = payload.get("case", {})
            return {
                "recommendation_reason": (
                    "案例包含明确的问题、AI 使用过程和结果证据，"
                    "但仍应结合来源类型和限制条件判断是否精选。"
                    if case.get("problem", {}).get("evidence")
                    else "证据不足，暂不建议精选。"
                )
            }
        raise ValueError(f"unknown agent role: {role}")


class OpenAICompatibleModel:
    """Minimal JSON-mode adapter for OpenAI-compatible chat endpoints."""

    name = "openai-compatible"

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.base_url = os.environ.get("AI_CASE_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("AI_CASE_LLM_MODEL", "gpt-5.5")
        if not self.api_key:
            raise RuntimeError("AI_CASE_LLM_MODE=openai_compatible requires OPENAI_API_KEY")

    def structured(self, role: str, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        contract = role_contract(role, payload)
        schema = contract.model_json_schema() if contract else None
        user_payload = "Return only one valid JSON object, with no explanation.\n"
        if schema:
            user_payload += ("Follow the provided JSON Schema. All fields are required. "
                             "Use empty strings/arrays when evidence is absent, never invent evidence.\n"
                             + json.dumps(schema, ensure_ascii=False) + "\n")
        user_payload += json.dumps({"role": role, "payload": payload}, ensure_ascii=False)
        fmt = {"type": "json_schema", "name": role, "strict": True, "schema": schema} if schema else {"type": "json_object"}
        chat_format = {"type": "json_schema", "json_schema": {k:v for k,v in fmt.items() if k != "type"}} if schema else fmt
        bodies = {
            "chat": {"model": self.model, "response_format": chat_format,
                     "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_payload}]},
            "responses": {"model": self.model, "instructions": system,
                          "input": [{"role": "user", "content": user_payload}],
                          "store": False, "stream": False, "text": {"format": fmt}},
        }
        preferred = getattr(self, '_working_wire', None) or os.environ.get("AI_CASE_LLM_WIRE_API", "chat").lower()
        failures = []
        for wire in (["responses", "chat"] if preferred == "responses" else ["chat", "responses"]):
            body = deepcopy(bodies[wire])
            if schema and wire in getattr(self, '_json_only_wires', set()):
                self._json_mode(body, wire)
            for format_attempt in range(2):
                try:
                    try:
                        data = self._post(wire, body)
                    except urllib.error.HTTPError as exc:
                        detail = self._http_error_detail(exc)
                        # Only downgrade an explicitly unsupported schema, never
                        # retry an arbitrary invalid request or a content refusal.
                        if schema and exc.code in {400, 422} and self._schema_unsupported(detail):
                            self._json_only_wires = getattr(self, '_json_only_wires', set()) | {wire}
                            self._json_mode(body, wire)
                            data = self._post(wire, body)
                        else:
                            raise
                    result = self._parse_json_result(wire, data)
                    if contract:
                        try:
                            contract.model_validate(result)
                        except ValidationError as exc:
                            locations = [".".join(map(str,e['loc'])) + ": " + e['type'] for e in exc.errors(include_input=False)]
                            raise InvalidStructuredOutput("Schema mismatch: " + "; ".join(locations)[:500]) from None
                    self._working_wire = wire
                    return result
                except InvalidStructuredOutput as exc:
                    if format_attempt:
                        raise
                    # A changed prompt is essential: repeating an identical bad
                    # request does not tell the model what needs repairing.
                    correction = "Your previous output was not accepted: " + str(exc) + ". Regenerate the complete JSON object using the original evidence; do not invent facts."
                    key = 'messages' if wire == 'chat' else 'input'
                    body[key].append({'role':'user', 'content':correction})
                except urllib.error.HTTPError as exc:
                    detail = self._http_error_detail(exc)
                    failures.append(wire + ": " + detail)
                    if exc.code not in {404, 405} and not (exc.code in {400, 422} and self._wire_unsupported(detail)):
                        raise RuntimeError("LLM request failed: " + "; ".join(failures)) from None
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
                    raise RuntimeError(f"LLM request failed: {type(exc).__name__}: {self._redact(str(exc))}") from None
        raise RuntimeError("LLM request failed: " + "; ".join(failures))

    @staticmethod
    def _json_mode(body, wire):
        if wire == 'chat': body['response_format'] = {'type':'json_object'}
        else: body['text']['format'] = {'type':'json_object'}

    @staticmethod
    def _schema_unsupported(detail):
        text = detail.lower()
        return any(x in text for x in ('not supported', 'unsupported', 'unknown parameter')) and any(x in text for x in ('json_schema', 'schema', 'response_format', 'text.format', 'response format'))

    @staticmethod
    def _wire_unsupported(detail):
        text = detail.lower()
        return any(x in text for x in ('not supported', 'unsupported', 'unknown endpoint')) and any(x in text for x in ('responses', 'chat/completions', 'response format'))

    @staticmethod
    def _redact(text):
        for name in ('OPENAI_API_KEY', 'TENCENT_DOCS_TOKEN'):
            secret = os.environ.get(name)
            if secret: text = text.replace(secret, '[redacted]')
        text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", text)
        return text

    @classmethod
    def _http_error_detail(cls, exc):
        if hasattr(exc, '_safe_detail'): return exc._safe_detail
        try:
            raw = exc.read(16000).decode('utf-8', 'replace')
            data = json.loads(raw)
            error = data.get('error', data)
            body = ' '.join(str(error.get(k, '')) for k in ('type','code','param','message')) if isinstance(error, dict) else str(error)
        except Exception:
            body = str(exc.reason)  # Never dump an HTML login page or headers.
        detail = f"HTTP {exc.code}: " + cls._redact(body)[:500]
        exc._safe_detail = detail
        return detail

    def _post(self, wire_api: str, body: Dict[str, Any]) -> Dict[str, Any]:
        # Retry a transient transport failure once; never retry a content rejection.
        self._request_deadline = time.monotonic() + request_timeout(bool(body.get("tools"))) * 2 + 8
        for attempt in range(2):
            failure = None
            try:
                return self._post_once(wire_api, body)
            except urllib.error.HTTPError as exc:
                if attempt or exc.code not in TRANSIENT_HTTP:
                    raise
                failure = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected) as exc:
                if attempt:
                    raise
                failure = exc
            time.sleep(retry_delay(failure, attempt))
        raise RuntimeError('LLM request retry exhausted')

    def _post_once(self, wire_api: str, body: Dict[str, Any]) -> Dict[str, Any]:
        route = "chat/completions" if wire_api == "chat" else "responses"
        paths = [f"{self.base_url}/{route}"]
        if not self.base_url.endswith("/v1"):
            paths.append(f"{self.base_url}/v1/{route}")
        last_error: urllib.error.HTTPError | None = None
        for path in paths:
            request = urllib.request.Request(
                path,
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            remaining = getattr(self, '_request_deadline', time.monotonic() + request_timeout(bool(body.get('tools')))) - time.monotonic()
            if remaining <= 0: raise TimeoutError('HTTP request budget exhausted')
            try:
                with urllib.request.urlopen(request, timeout=min(remaining, request_timeout(bool(body.get("tools"))))) as response:
                    raw = response.read().decode("utf-8")
                    # Some gateways serve their web UI (HTTP 200) on unknown routes.
                    if raw.lstrip().startswith("<"):
                        continue
                    if raw.lstrip().startswith(("event:", "data:")):
                        return self._parse_sse_result(raw)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("LLM endpoint returned non-JSON content") from exc
                    if not isinstance(data, dict):
                        raise RuntimeError("LLM endpoint returned a non-object response")
                    return data
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {404, 405}:
                    raise
        if last_error:
            raise last_error
        raise RuntimeError("LLM endpoint returned HTML; check base URL and AI_CASE_LLM_WIRE_API")

    @staticmethod
    def _parse_sse_result(raw: str) -> Dict[str, Any]:
        """Some gateways stream despite stream=false. Require the final response."""
        completed = None
        chat_parts, chat_finish, done = [], None, False
        for block in re.split(r"\r?\n\r?\n", raw):
            payload = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if payload == "[DONE]":
                done = True
                continue
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError("LLM returned a malformed SSE event") from exc
            if event.get('error'):
                raise RuntimeError("LLM stream failed")
            for choice in event.get('choices', []):
                if choice.get('index', 0) != 0: continue
                delta = choice.get('delta', {})
                if delta.get('refusal'): raise RuntimeError("LLM refused this input")
                if isinstance(delta.get('content'), str): chat_parts.append(delta['content'])
                if choice.get('finish_reason'): chat_finish = choice['finish_reason']
            if event.get("type") in {"response.failed", "response.incomplete", "error"}:
                raise RuntimeError("LLM stream failed or was incomplete")
            if event.get("type") == "response.completed":
                completed = event.get("response")
        if completed is None and done and chat_finish == 'stop' and chat_parts:
            return {'choices':[{'finish_reason':'stop', 'message':{'content':''.join(chat_parts)}}]}
        if not isinstance(completed, dict) or completed.get("status") != "completed":
            raise RuntimeError("LLM stream ended without a completed response")
        return completed

    @staticmethod
    def _parse_json_result(wire_api: str, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if wire_api == "chat":
                if data["choices"][0].get("finish_reason") not in {None, "stop"}:
                    raise RuntimeError("LLM output was not completed")
                if data["choices"][0]["message"].get("refusal"):
                    raise RuntimeError("LLM refused this input")
                content = data["choices"][0]["message"]["content"]
            else:
                if data.get("status") not in {None, "completed"} or data.get("error"):
                    raise RuntimeError("LLM response failed or was incomplete")
                if any(p.get("type") == "refusal" for item in data.get("output", []) for p in item.get("content", [])):
                    raise RuntimeError("LLM refused this input")
                content = data.get("output_text")
                if not content:
                    content = "".join(
                        part["text"]
                        for item in data["output"]
                        for part in item.get("content", [])
                        if part.get("type") in {"output_text", "text"} and part.get("text")
                    )
            # Some compatible gateways wrap an otherwise valid object in Markdown.
            # Only accept a whole fenced object, never salvage prose/partial output.
            if isinstance(content, str):
                fenced = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*', content, re.S)
                if fenced:
                    content = fenced.group(1)
            result = json.loads(content)
        except (KeyError, IndexError, StopIteration, TypeError, json.JSONDecodeError) as exc:
            raise InvalidStructuredOutput("LLM returned invalid structured JSON") from exc
        if not isinstance(result, dict):
            raise InvalidStructuredOutput("LLM structured output must be a JSON object")
        return result

    def search_web(self, query: str, max_results: int = 8) -> list[Dict[str, Any]]:
        """Only return URLs backed by an actual Responses web-search tool call."""
        from .publication_dates import date_policy
        body = {
                "model": self.model, "store": False, "stream": False,
                "instructions": skill_text("scout") + "\n只发现候选，最多两次搜索；不要阅读全文或评分。简短列出来源。",
                "reasoning": {"effort": "low"},
                "max_tool_calls": 2,
                "input": [{"role": "user", "content": query + f"\nReturn at most {max_results} cited candidates." + "\nPublication window (inclusive): " + json.dumps(date_policy(), ensure_ascii=False)}],
                "tools": [{"type": "web_search"}],
                "include": ["web_search_call.action.sources"],
            }
        try:
            # Compatible gateways may reject newer latency controls. Remove only
            # the exact optional parameter named by an unsupported-parameter error.
            for _ in range(3):
                try:
                    data = self._post("responses", body)
                    break
                except urllib.error.HTTPError as exc:
                    detail = self._http_error_detail(exc).lower()
                    removable = next((key for key in ('max_tool_calls', 'reasoning')
                                      if key in body and re.search(r'\b' + key + r'\b', detail)), None)
                    if exc.code in {400, 422} and removable and any(
                        marker in detail for marker in ('unsupported parameter', 'unknown parameter', 'not supported')
                    ):
                        body.pop(removable)
                        continue
                    raise
            else:
                raise RuntimeError('Web search gateway rejected optional parameters')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Web search service unavailable: {self._http_error_detail(exc)}") from None
        except (OSError, ValueError) as exc:
            raise RuntimeError("Web search service unavailable") from exc
        output = data.get("output", [])
        if data.get("status") != "completed" or not any(item.get("type") == "web_search_call" and item.get("status") == "completed" for item in output):
            raise RuntimeError("Model gateway did not execute web_search; no search results accepted")
        sources = []
        for item in output:
            for part in item.get("content", []):
                sources.extend(a for a in part.get("annotations", []) if a.get("type") == "url_citation")
        for item in output:
            if item.get("type") == "web_search_call":
                sources.extend(item.get("action", {}).get("sources", []))
        seen = set()
        results = []
        excluded = re.findall(r"-site:([\w.-]+)", query.lower())
        for source in sources:
            url = source.get("url", "")
            host = urlparse(url).hostname or ""
            if any(host == domain or host.endswith("." + domain) for domain in excluded):
                continue
            if url.startswith(("https://", "http://")) and url not in seen:
                seen.add(url)
                results.append({"url": url, "title": source.get("title", url), "provider": "responses.web_search",
                                "published_at": source.get("published_at") or source.get("date") or source.get("publishedDate")})
        if not results:
            raise RuntimeError("Web search returned no citable sources")
        return results[:max_results]


def create_agent_model(mode: str | None = None) -> AgentModel:
    selected = mode or os.environ.get("AI_CASE_LLM_MODE", "mock")
    if selected == "mock":
        return MockAgentModel()
    if selected == "openai_compatible":
        return OpenAICompatibleModel()
    raise ValueError(f"Unsupported AI_CASE_LLM_MODE: {selected}")
