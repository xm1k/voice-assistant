import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
import yaml
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
from backend.common.schemas import Decision
from backend.llm import client


class CompletionRequestTests(unittest.TestCase):
    def test_provider_request_contract(self):
        profiles = yaml.safe_load(
            (ROOT / "config/providers.yaml").read_text()
        )["llm"]["profiles"]
        for name in ("deepseek", "deepseek_v4_pro", "openrouter"):
            with self.subTest(provider=name):
                config = profiles[name]
                if name == "deepseek_v4_pro":
                    self.assertEqual(config["model"], "deepseek-v4-pro")
                    self.assertEqual(config["api_key_env"], "DEEPSEEK_API_KEY")
                messages = [{"role": "system", "content": "Return JSON."},
                            {"role": "user", "content": "создай дейлик по будням"}]
                original = [dict(message) for message in messages]
                captured = []
                result = {"type": "ready", "answer": "Ответ", "commands": [],
                          "question": None, "context": None, "query": None, "record_refs": []}

                def respond(request):
                    captured.append(json.loads(request.content))
                    return httpx.Response(200, json={"choices": [{
                        "finish_reason": "stop", "message": {"content": json.dumps(result)}
                    }]})

                async def active_config():
                    return config

                factory = httpx.AsyncClient
                with patch.object(client, "_active_llm_config", active_config), \
                     patch.dict("os.environ", {config["api_key_env"]: "test-key"}), \
                     patch.object(client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                                  factory(transport=httpx.MockTransport(respond), **kwargs)):
                    self.assertEqual(asyncio.run(client.complete(messages)), result)

                self.assertEqual(messages, original)
                payload = captured[0]
                self.assertEqual(payload["model"], config["model"])
                if name.startswith("deepseek"):
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(payload.get("thinking"), {"type": "disabled"})
                    instruction = payload["messages"][0]["content"]
                    schema_text = instruction.split("JSON Schema Decision:\n", 1)[1].split("\nПример JSON:\n", 1)
                    schema = json.loads(schema_text[0])
                    self.assertEqual(schema, client.strict_schema(TypeAdapter(Decision).json_schema()))
                    TypeAdapter(Decision).validate_json(schema_text[1])
                    self.assertEqual(payload["messages"][1:], original)
                else:
                    self.assertEqual(payload["response_format"]["type"], "json_schema")
                    self.assertEqual(payload["messages"], original)
                    self.assertNotIn("thinking", payload)
