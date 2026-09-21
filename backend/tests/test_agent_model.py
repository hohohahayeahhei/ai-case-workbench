import json
import io
import unittest
import urllib.error
from unittest.mock import patch

from backend.app.agent_model import OpenAICompatibleModel


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": '{"decision":"pass"}'}}]}).encode()


class _NotFound:
    def __enter__(self):
        raise urllib.error.HTTPError("http://model.test:14000/chat/completions", 404, "missing", {}, None)

    def __exit__(self, *_):
        return False


class _ResponsesResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"output_text": '{"decision":"pass"}'}).encode()


class _BadRequest:
    def __enter__(self):
        raise urllib.error.HTTPError(
            "http://model.test:14000/responses",
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":{"message":"unsupported response format"}}'),
        )

    def __exit__(self, *_):
        return False


class AgentModelTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'AI_CASE_LLM_WIRE_API': 'chat'})
        env.start()
        self.addCleanup(env.stop)

    def test_openai_compatible_adapter_posts_structured_json(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-secret",
                "AI_CASE_LLM_BASE_URL": "http://model.test:14000",
                "AI_CASE_LLM_MODEL": "gpt-6-astra",
            },
            clear=False,
        ):
            model = OpenAICompatibleModel()
            with patch("backend.app.agent_model.urllib.request.urlopen", return_value=_Response()) as opener:
                result = model.structured("verification", "return JSON", {"case_id": "case-1"})
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.full_url, "http://model.test:14000/chat/completions")
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")

    def test_openai_compatible_adapter_falls_back_to_v1_path(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-secret",
                "AI_CASE_LLM_BASE_URL": "http://model.test:14000",
                "AI_CASE_LLM_MODEL": "gpt-6-astra",
            },
            clear=False,
        ):
            model = OpenAICompatibleModel()
            with patch("backend.app.agent_model.urllib.request.urlopen", side_effect=[_NotFound(), _Response()]) as opener:
                model.structured("verification", "return JSON", {"case_id": "case-1"})
        self.assertEqual(opener.call_args_list[1].args[0].full_url, "http://model.test:14000/v1/chat/completions")

    def test_openai_compatible_adapter_supports_responses_wire_api(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-secret",
                "AI_CASE_LLM_BASE_URL": "http://model.test:14000",
                "AI_CASE_LLM_MODEL": "gpt-6-astra",
                "AI_CASE_LLM_WIRE_API": "responses",
            },
            clear=False,
        ):
            model = OpenAICompatibleModel()
            with patch("backend.app.agent_model.urllib.request.urlopen", return_value=_ResponsesResponse()) as opener:
                result = model.structured("verification", "return JSON", {"case_id": "case-1"})
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.full_url, "http://model.test:14000/responses")
        self.assertEqual(body["instructions"], "return JSON")
        self.assertIn("JSON", body["input"][0]["content"])
        self.assertEqual(result["decision"], "pass")

    def test_structured_falls_back_to_chat_after_responses_400(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "test-secret",
                "AI_CASE_LLM_BASE_URL": "http://model.test:14000",
                "AI_CASE_LLM_MODEL": "gpt-6-astra",
                "AI_CASE_LLM_WIRE_API": "responses",
            },
            clear=False,
        ):
            model = OpenAICompatibleModel()
            with patch(
                "backend.app.agent_model.urllib.request.urlopen",
                side_effect=[_BadRequest(), _Response()],
            ) as opener:
                result = model.structured("verification", "return JSON", {"case_id": "case-1"})
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(opener.call_args_list[0].args[0].full_url, "http://model.test:14000/responses")
        self.assertEqual(opener.call_args_list[1].args[0].full_url, "http://model.test:14000/chat/completions")


if __name__ == "__main__":
    unittest.main()
