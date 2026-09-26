"""The Ollama provider: request shape, parsing, retries, failure modes, preflight."""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stockflow.analyzer import FakeAnalyzer
from stockflow.errors import BackendUnavailable, MalformedResponseError
from stockflow.ollama import OllamaAnalyzer, OllamaUnavailable, _HttpError, describe
from stockflow.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, json_schema


def chat_reply(data=None, **extra):
    body = {
        "message": {"role": "assistant", "content": json.dumps(data or FakeAnalyzer.sample())},
        "done": True,
        "prompt_eval_count": 900,
        "eval_count": 250,
    }
    body.update(extra)
    return body


class ScriptedTransport:
    """Returns (or raises) one scripted item per call and records requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[tuple[str, dict | None]] = []

    def __call__(self, url, payload, timeout):
        self.requests.append((url, payload))
        item = self.script.pop(0) if self.script else chat_reply()
        if isinstance(item, Exception):
            raise item
        return item


def analyzer(transport, **kw):
    kw.setdefault("max_retries", 3)
    return OllamaAnalyzer("http://localhost:11434", "qwen2.5vl:3b", transport=transport, **kw)


class TestJsonSchema:
    def test_types_are_lower_case_json_schema(self):
        s = json_schema()
        assert s["type"] == "object"
        assert s["properties"]["keywords"] == {"type": "array", "items": {"type": "string"}}
        assert s["properties"]["commercial_score"]["type"] == "integer"
        assert s["properties"]["people_visible"]["type"] == "boolean"

    def test_keeps_enums_and_required_drops_gemini_only_keys(self):
        s = json_schema()
        assert s["properties"]["category"]["enum"] == RESPONSE_SCHEMA["properties"]["category"]["enum"]
        assert s["required"] == RESPONSE_SCHEMA["required"]
        assert "property_ordering" not in s

    def test_does_not_mutate_the_gemini_schema(self):
        json_schema()
        assert RESPONSE_SCHEMA["type"] == "OBJECT"
        assert "property_ordering" in RESPONSE_SCHEMA


class TestRequest:
    def test_one_chat_call_with_image_schema_and_prompts(self):
        t = ScriptedTransport(chat_reply())
        analyzer(t).analyze(b"jpeg-bytes", "measured: sharp")

        assert len(t.requests) == 1
        url, payload = t.requests[0]
        assert url == "http://localhost:11434/api/chat"
        assert payload["model"] == "qwen2.5vl:3b"
        assert payload["stream"] is False
        assert payload["format"] == json_schema()
        system, user = payload["messages"]
        assert system == {"role": "system", "content": SYSTEM_PROMPT}
        assert base64.b64decode(user["images"][0]) == b"jpeg-bytes"
        assert "measured: sharp" in user["content"]

    def test_bounds_runaway_generation_without_array_length_constraints(self):
        t = ScriptedTransport(chat_reply())
        analyzer(t).analyze(b"x")
        payload = t.requests[0][1]
        assert payload["options"]["num_predict"] == 1500
        assert payload["options"]["repeat_penalty"] > 1.0
        # maxItems/minItems crash Ollama 0.34.4's grammar engine.
        keywords = payload["format"]["properties"]["keywords"]
        assert "maxItems" not in keywords and "minItems" not in keywords

    def test_parses_into_the_same_analysis_as_gemini(self):
        data = FakeAnalyzer.sample(title="Macro of a green leaf beetle on a fern")
        result = analyzer(ScriptedTransport(chat_reply(data))).analyze(b"x")
        assert result.title == "Macro of a green leaf beetle on a fern"
        assert result.category == data["category"]
        assert len(result.keywords) == len(data["keywords"])

    def test_records_token_counts(self):
        a = analyzer(ScriptedTransport(chat_reply()))
        a.analyze(b"x")
        assert a.stats["prompt_tokens"] == 900
        assert a.stats["output_tokens"] == 250
        assert a.stats["calls"] == 1


class TestRetries:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)

    def test_malformed_output_is_retried(self):
        bad = {"message": {"content": "this is not json"}}
        a = analyzer(ScriptedTransport(bad, chat_reply()))
        assert a.analyze(b"x").title
        assert a.stats["retries"] == 1

    def test_empty_content_is_retried(self):
        a = analyzer(ScriptedTransport({"message": {"content": ""}}, chat_reply()))
        assert a.analyze(b"x").title

    def test_gives_up_after_max_retries(self):
        bad = {"message": {"content": "{}"}}
        t = ScriptedTransport(bad, bad, bad)
        with pytest.raises(MalformedResponseError):
            analyzer(t).analyze(b"x")
        assert len(t.requests) == 3

    def test_server_error_is_retried(self):
        a = analyzer(ScriptedTransport(_HttpError(500, "runner crashed"), chat_reply()))
        assert a.analyze(b"x").title


class TestUnavailable:
    def test_missing_model_is_not_retried(self):
        t = ScriptedTransport(_HttpError(404, "model 'qwen2.5vl:3b' not found"))
        with pytest.raises(OllamaUnavailable, match="ollama pull qwen2.5vl:3b"):
            analyzer(t).analyze(b"x")
        assert len(t.requests) == 1

    def test_unreachable_server_is_not_retried(self):
        t = ScriptedTransport(urllib.error.URLError("connection refused"))
        with pytest.raises(OllamaUnavailable, match="Cannot reach Ollama"):
            analyzer(t).analyze(b"x")
        assert len(t.requests) == 1

    def test_is_a_backend_outage_not_a_photo_failure(self):
        assert issubclass(OllamaUnavailable, BackendUnavailable)


class TestPreflight:
    def test_ok_when_model_present_with_latest_tag(self):
        t = ScriptedTransport({"models": [{"name": "qwen2.5vl:3b:latest"}]})
        assert analyzer(t).check() is None
        assert t.requests[0] == ("http://localhost:11434/api/tags", None)

    def test_ok_when_model_present_exactly(self):
        t = ScriptedTransport({"models": [{"name": "qwen2.5vl:3b"}]})
        assert analyzer(t).check() is None

    def test_missing_model_says_how_to_pull_it(self):
        t = ScriptedTransport({"models": [{"name": "llava:7b"}]})
        assert "ollama pull qwen2.5vl:3b" in analyzer(t).check()

    def test_unreachable_server_says_how_to_start_it(self):
        t = ScriptedTransport(urllib.error.URLError("connection refused"))
        assert "ollama serve" in analyzer(t).check()


class TestDescribe:
    @pytest.mark.parametrize("host", ["http://localhost:11434", "http://127.0.0.1:11434"])
    def test_local(self, host):
        assert "this machine" in describe(host)

    def test_remote(self):
        assert "remote server" in describe("http://192.168.1.20:11434")


class TestRealHttp:
    """The stdlib transport against a real (local, stub) HTTP server."""

    @pytest.fixture
    def server(self):
        class Handler(BaseHTTPRequestHandler):
            received: list = []

            def log_message(self, *args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._send(200, {"models": [{"name": "qwen2.5vl:3b"}]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                Handler.received.append(payload)
                if payload["model"] != "qwen2.5vl:3b":
                    self._send(404, {"error": f"model '{payload['model']}' not found"})
                else:
                    self._send(200, chat_reply())

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{srv.server_address[1]}", Handler.received
        srv.shutdown()
        srv.server_close()

    def test_round_trip(self, server):
        host, received = server
        a = OllamaAnalyzer(host, "qwen2.5vl:3b")
        assert a.check() is None
        result = a.analyze(b"jpeg-bytes")
        assert result.title == FakeAnalyzer.sample()["title"]
        assert received[0]["format"]["type"] == "object"

    def test_http_404_body_is_reported(self, server):
        host, _ = server
        with pytest.raises(OllamaUnavailable, match="not found"):
            OllamaAnalyzer(host, "missing-model").analyze(b"x")

    def test_connection_refused(self):
        # Port 9 (discard) is not listening on a test machine.
        with pytest.raises(OllamaUnavailable):
            OllamaAnalyzer("http://127.0.0.1:9", "qwen2.5vl:3b", timeout=5).analyze(b"x")
