"""Tests for single-pass judge+redirect calls and the direct HTTP side-call runner."""
import io
import json
import urllib.error
import urllib.request

import pytest

from tokeneconomizer.loopguard import judge as loop_judge
from tokeneconomizer.plugin import config as live_config
from tokeneconomizer.plugin import controller, sidecall
from tokeneconomizer.plugin import judge as plugin_judge


def _cfg(**kw):
    c = live_config.LiveConfig()
    for k, v in kw.items():
        assert c.set(k, v), c.warnings
    return c


class _Store:
    def __init__(self, req):
        self.files = {"judge/S.request.json": req}
        self.appended = []

    def read(self, k):
        return self.files.get(k)

    def write(self, k, v):
        self.files[k] = v

    def append(self, k, v):
        self.appended.append((k, v))


def test_single_pass_judge_captures_inline_nudge_only_when_stuck():
    prompts = []

    def runner(prompt, model="agent"):
        prompts.append(prompt)
        return json.dumps({
            "stuck": True, "confidence": 0.9, "reason": "same failure",
            "nudge": "Read the failing test file before editing again."
        }), 120.0

    v = loop_judge.llm_stuck_judge("turns=20 ...", runner, with_nudge=True)
    assert v["stuck"] is True
    assert v["_nudge"] == "Read the failing test file before editing again."
    assert v["_tokens"] == 120.0
    assert '"nudge"' in prompts[0]

    def spare_runner(prompt, model="agent"):
        return json.dumps({
            "stuck": False, "confidence": 0.8, "reason": "new failure",
            "nudge": "should be ignored"
        }), 80.0

    v2 = loop_judge.llm_stuck_judge("turns=20 ...", spare_runner, with_nudge=True)
    assert v2["stuck"] is False and "_nudge" not in v2


def test_run_job_skips_second_writer_call_when_single_pass_returns_nudge(tmp_path, monkeypatch):
    calls = []

    def fake_claude(prompt, model="haiku", **k):
        calls.append(prompt)
        return json.dumps({
            "stuck": True, "confidence": 0.88, "reason": "repeat edit",
            "nudge": "Stop re-editing parser.py and inspect the traceback first."
        }), 400.0

    monkeypatch.setattr(sidecall, "claude_call", fake_claude)
    store = _Store({"id": "J", "digest": "turns=20 ...", "threshold": 0.7})
    ctx = type("C", (), {"host": type("H", (), {"name": "claude"})(),
                         "root": str(tmp_path), "store": store, "config": _cfg()})()
    v = plugin_judge.run_job(ctx, "S", "J")
    assert len(calls) == 1
    assert v["stuck"] is True
    assert v["_nudge"] == "Stop re-editing parser.py and inspect the traceback first."
    assert store.appended[0][1]["overhead"] == {
        "spiral_judge_tokens": 400.0, "spiral_nudge_writer_tokens": 0.0}


def test_run_job_falls_back_to_nudge_writer_when_judge_omits_nudge(tmp_path, monkeypatch):
    calls = []

    def fake_claude(prompt, model="haiku", **k):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"stuck": true, "confidence": 0.85, "reason": "loop"}', 300.0
        return "Try inspecting the failing assertion.", 250.0

    monkeypatch.setattr(sidecall, "claude_call", fake_claude)
    store = _Store({"id": "J", "digest": "turns=20 ...", "threshold": 0.7})
    ctx = type("C", (), {"host": type("H", (), {"name": "claude"})(),
                         "root": str(tmp_path), "store": store, "config": _cfg()})()
    v = plugin_judge.run_job(ctx, "S", "J")
    assert len(calls) == 2
    assert v["_nudge"] == "Try inspecting the failing assertion."
    assert store.appended[0][1]["overhead"] == {
        "spiral_judge_tokens": 300.0, "spiral_nudge_writer_tokens": 250.0}


def test_sidecall_http_config_validation_and_env(tmp_path):
    c = live_config.LiveConfig()
    assert c.set("sidecall_url", "http://127.0.0.1:11434/v1")
    assert c.set("sidecall_model", "qwen2.5-coder")
    assert c.set("sidecall_key_env", "MY_API_KEY")
    assert not c.set("sidecall_url", "ftp://bad.host/v1")
    assert not c.set("sidecall_key_env", "sk-raw-secret-key")

    loaded = live_config.load_config(str(tmp_path), env={
        "TE_SIDECALL_URL": "https://api.example.com/v1",
        "TE_SIDECALL_MODEL": "haiku-fast",
        "TE_SIDECALL_KEY_ENV": "LLM_KEY",
    })
    assert loaded.sidecall_url == "https://api.example.com/v1"
    assert loaded.sidecall_model == "haiku-fast"
    assert loaded.sidecall_key_env == "LLM_KEY"


@pytest.mark.real_sidecalls
def test_http_sidecall_runner_and_antigravity_routing(tmp_path, monkeypatch):
    seen = []

    def fake_build_opener(*handlers):
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, req, timeout=None):
                seen.append({
                    "handlers": handlers,
                    "url": req.full_url,
                    "auth": req.get_header("Authorization"),
                    "req": json.loads(req.data.decode("utf-8")),
                    "timeout": timeout,
                })
                return _Resp(json.dumps({
                    "model": "claude-haiku-4-5",
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({
                        "stuck": True, "confidence": 0.91, "reason": "repeated pytest fail",
                        "nudge": "Inspect test_foo.py fixture setup instead of re-running pytest."
                    })}}],
                    "usage": {"prompt_tokens": 200, "completion_tokens": 40},
                }).encode("utf-8"))

        return _Opener()

    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    monkeypatch.setenv("TE_TEST_API_KEY", "secret-token")
    monkeypatch.setattr(sidecall.urllib.request, "build_opener", fake_build_opener)

    cfg = _cfg(sidecall_url="http://127.0.0.1:11434/v1", sidecall_model="claude-haiku-4-5",
               sidecall_key_env="TE_TEST_API_KEY")

    # Even Antigravity (NO_SIDE_CALLS) and paused hosts get AsyncJudge when HTTP is set.
    monkeypatch.setattr(sidecall, "paused", lambda *a, **k: {"at": 1.0})
    for host_name in ("antigravity", "claude"):
        ctx_hook = type("Ctx", (), {"config": cfg, "root": str(tmp_path)})()
        ctl = type("C", (), {"host": type("H", (), {"name": host_name})(),
                             "ctx": ctx_hook, "sid": "S"})()
        assert type(controller.LiveController._judge(ctl, {"judge_confidence": 0.7}, {})).__name__ == "AsyncJudge"

    store = _Store({"id": "J", "digest": "turns=20 ...", "threshold": 0.7})
    ctx = type("C", (), {"host": type("H", (), {"name": "antigravity"})(),
                         "root": str(tmp_path), "store": store, "config": cfg})()
    v = plugin_judge.run_job(ctx, "S", "J")
    assert len(seen) == 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
               for h in seen[0]["handlers"])
    assert seen[0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen[0]["auth"] == "Bearer secret-token"
    assert seen[0]["req"]["model"] == "claude-haiku-4-5"
    assert v["stuck"] is True
    assert v["_nudge"].startswith("Inspect test_foo.py")
    assert v["_tokens"] > 0 and store.appended[0][1]["overhead"]["spiral_judge_tokens"] > 0

    # Remote URLs keep default proxy handling (no empty ProxyHandler override).
    cfg_remote = _cfg(sidecall_url="https://api.example.com/v1/chat/completions",
                      sidecall_model="claude-haiku-4-5")
    sidecall.http_runner(cfg_remote)("ping")
    assert seen[1]["handlers"] == ()
    assert seen[1]["url"] == "https://api.example.com/v1/chat/completions"


@pytest.mark.real_sidecalls
def test_http_sidecall_falls_back_to_host_cli_on_error(tmp_path, monkeypatch):
    def failing_opener(*handlers):
        class _Opener:
            def open(self, req, timeout=None):
                raise urllib.error.URLError("connection refused")
        return _Opener()

    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    monkeypatch.setattr(sidecall.urllib.request, "build_opener", failing_opener)
    monkeypatch.setattr(sidecall, "claude_call", lambda p, model="": ("cli-ok", 10.0))
    cfg = _cfg(sidecall_url="http://127.0.0.1:9/v1")
    run = sidecall.runner_for("claude", cfg)
    assert run("hello") == ("cli-ok", 10.0)
    assert "sidecall http: failed" in (tmp_path / "errors.log").read_text(encoding="utf-8")
