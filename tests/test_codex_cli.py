"""The plugin installed and run by a real Codex CLI: CI's `codex` job (`TE_CODEX_E2E=1`).

Codex installs a COPY of plugin/ without the package (its `lib` link is dropped, or on a Windows
checkout is a plain file), so the copy finds the package through the marketplace `source` Codex
writes in its config.toml: a TOML literal string with the extended-length prefix on Windows
(0.163.0). These install the plugin as the README says, then run a `codex exec` session against
a local stand-in for the model that gives one canned answer (no key, no model call), and check
that the hooks ran and the session was learned from.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.conftest import git_sh

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin")
CODEX = shutil.which("codex")
# Windows runs no shebang: the launchers go through Git Bash's `sh`.
SH = [git_sh() or "sh"] if os.name == "nt" else []

pytestmark = pytest.mark.skipif(
    os.environ.get("TE_CODEX_E2E") != "1" or not CODEX,
    reason="drives a real Codex CLI: set TE_CODEX_E2E=1 with `codex` on PATH (CI's codex job)")


def _env(tmp_path):
    """Codex and the plugin each in their own home under the test, and no other way for the
    install copy to find the package."""
    return dict(os.environ, CODEX_HOME=str(tmp_path / "codex"),
                TOKENECONOMIZER_HOME=str(tmp_path / "te"), TE_CORE_PATH="", PYTHONPATH="")


def _codex(env, *args, cwd=None, timeout=120):
    out = subprocess.run([CODEX, *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", stdin=subprocess.DEVNULL, cwd=cwd, env=env,
                         timeout=timeout)
    assert out.returncode == 0, (args, out.stdout[-2000:], out.stderr[-2000:])
    return out


def _install(env):
    """README's two commands; returns Codex's install copy and the `source` line it wrote."""
    os.makedirs(env["CODEX_HOME"], exist_ok=True)
    _codex(env, "plugin", "marketplace", "add", REPO)
    _codex(env, "plugin", "add", "tokeneconomizer@tokeneconomizer")
    with open(os.path.join(PLUGIN, ".codex-plugin", "plugin.json"), encoding="utf-8") as fh:
        version = json.load(fh)["version"]
    copy = os.path.join(env["CODEX_HOME"], "plugins", "cache", "tokeneconomizer",
                        "tokeneconomizer", version)
    with open(os.path.join(env["CODEX_HOME"], "config.toml"), encoding="utf-8") as fh:
        source = [ln for ln in fh.read().splitlines() if ln.strip().startswith("source =")]
    print(f"{_codex(env, '--version').stdout.strip()} wrote: {' | '.join(source)}")
    return copy, source


def test_codex_installs_the_plugin_and_its_copy_finds_the_package(tmp_path):
    env = _env(tmp_path)
    copy, source = _install(env)
    assert os.path.isfile(os.path.join(copy, "scripts", "te-hook"))
    assert not os.path.isdir(os.path.join(copy, "lib", "tokeneconomizer")), \
        "the copy carries the package: the marketplace lookup below is not what finds it"
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    found = [p for p in te_bootstrap._marketplace_sources(copy)
             if os.path.isdir(p) and os.path.samefile(p, REPO)]
    assert found, (source, te_bootstrap._marketplace_sources(copy))
    out = subprocess.run(SH + [os.path.join(copy, "scripts", "te"), "doctor"],
                         capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env)
    assert out.returncode == 0 and "python" in out.stdout, (source, out.stderr[-1000:])


class _Model(BaseHTTPRequestHandler):
    """The Responses API, as far as Codex reads it for a turn with no tool call: every request
    is answered with one assistant message."""
    requests = []
    bodies = []

    def log_message(self, *args):
        pass

    def do_GET(self):                   # model list and the like: none here
        self.requests.append(("GET", self.path))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.requests.append(("POST", self.path))
        rid = f"resp_{len(self.requests)}"

        def event(kind, **fields):
            return f"event: {kind}\ndata: {json.dumps(dict(type=kind, **fields))}\n\n"
        body = (event("response.created", response={"id": rid})
                + event("response.output_item.done", item={
                    "type": "message", "role": "assistant", "id": f"msg_{rid}",
                    "content": [{"type": "output_text", "text": "hi"}]})
                + event("response.completed", response={"id": rid, "usage": {
                    "input_tokens": 10, "input_tokens_details": None, "output_tokens": 1,
                    "output_tokens_details": None, "total_tokens": 11}})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def model():
    _Model.requests, _Model.bodies = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _use_model(env, model):
    """Point Codex at the stand-in model."""
    config = os.path.join(env["CODEX_HOME"], "config.toml")
    tables = ""
    if os.path.exists(config):
        with open(config, encoding="utf-8") as fh:
            tables = fh.read()
    with open(config, "w", encoding="utf-8") as fh:     # top-level keys go before any table
        fh.write('model = "te-test-model"\nmodel_provider = "te_test"\n\n'
                 f'[model_providers.te_test]\nname = "te-test"\nbase_url = "{model}"\n'
                 'wire_api = "responses"\nsupports_websockets = false\n'
                 "request_max_retries = 0\nstream_max_retries = 0\n\n" + tables)


def test_a_codex_session_runs_the_hooks_and_is_learned_from(tmp_path, model):
    env = _env(tmp_path)
    _, source = _install(env)
    _use_model(env, model)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("x\n")
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    # Persisted hook trust is the person's "Trust all and continue"; CI runs this checkout's own
    # hooks, which is what the flag is for.
    out = _codex(env, "exec", "--dangerously-bypass-hook-trust", "--skip-git-repo-check",
                 "Say hi.", cwd=str(proj), timeout=180)
    # What Codex said about the hooks, for CI to show when this fails.
    for line in (out.stdout + out.stderr).splitlines():
        if "hook" in line.lower():
            print(f"codex: {line}")
    assert ("POST", "/v1/responses") in _Model.requests, (out.stdout[-1000:], out.stderr[-2000:])

    root = env["TOKENECONOMIZER_HOME"]
    errors = os.path.join(root, "errors.log")
    logged = open(errors, encoding="utf-8").read() if os.path.exists(errors) else ""
    sessions = glob.glob(os.path.join(root, "repos", "*", "codex", "sessions", "*.json"))
    assert sessions, ("no hook recorded the session", source, logged[-2000:],
                      out.stderr[-2000:])
    assert not logged, logged[-2000:]
    # SessionEnd hands the session to a detached `te fold` (on Windows, `winspawn`).
    sid = os.path.basename(sessions[0])[:-len(".json")]
    store = os.path.dirname(os.path.dirname(sessions[0]))
    deadline = time.time() + 120
    folded = {}
    while time.time() < deadline and sid not in folded:
        try:
            with open(os.path.join(store, "folded.json"), encoding="utf-8") as fh:
                folded = json.load(fh)
        except (OSError, ValueError):
            pass
        time.sleep(1)
    fold_log = os.path.join(store, "fold.log")
    assert sid in folded, (open(fold_log, encoding="utf-8").read()[-2000:]
                           if os.path.exists(fold_log) else "no fold.log")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


@pytest.mark.real_sidecalls
def test_a_side_call_hands_codex_its_prompt_as_written(tmp_path, model, monkeypatch):
    """On Windows `codex` is npm's `codex.cmd`, and through cmd.exe a newline would end the
    prompt, `%PATH%` expand and an open quote let `&` start a command. The side call starts what
    the shim runs, so the prompt reaches the model as written."""
    from tokeneconomizer.plugin import sidecall
    env = _env(tmp_path)
    os.makedirs(env["CODEX_HOME"], exist_ok=True)
    _use_model(env, model)
    for k in ("CODEX_HOME", "TOKENECONOMIZER_HOME"):
        monkeypatch.setenv(k, env[k])
    print(f"codex: started as {sidecall.program('codex')}")
    prompt = 'Say "hi & echo te-was-here\nthen 100% of %PATH%.'
    text, _ = sidecall.codex_call(prompt)
    sent = [s for b in _Model.bodies for s in _strings(json.loads(b))]
    assert any(prompt in s for s in sent), (text, [s for s in sent if "hi" in s][:5])
    assert text == "hi"
