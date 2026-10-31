"""The opencode plugin under opencode's own runtime: CI's `opencode` job (`TE_OPENCODE_E2E=1`).

opencode 2.x runs plugins in its background service, which on Windows has no console of its own,
so every hook the plugin starts (a `python` per prompt, per tool call, per turn) got a new console
window: the terminal "flickered" while the agent worked. These load the plugin's 2.x entry in
opencode's binary run as Bun (`BUN_BE_BUN=1`), from a parent started without a console as the
service is, drive one prompt and one tool call through it, and read back from each hook process
whether it was given a console window. A copy of the plugin without `windowsHide` is the control.
The second test does the same through the real service (`opencode service start`, then
`opencode run`), with the plugin installed in opencode's config directory and a local stand-in
for the model.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENCODE = os.environ.get("TE_OPENCODE_BIN") or shutil.which("opencode")

pytestmark = pytest.mark.skipif(
    os.environ.get("TE_OPENCODE_E2E") != "1" or not OPENCODE,
    reason="drives opencode's own runtime: set TE_OPENCODE_E2E=1 and TE_OPENCODE_BIN to the "
           "opencode binary (CI's opencode job)")

# Imported by every Python the hooks start (through PYTHONPATH): whether it has a console window,
# and whether it is a hook the plugin started (`te_bootstrap.py hook ...`) or something the hook
# itself started in the background (a detached `te`, through `winspawn` on Windows).
PROBE = '''import json, os, sys
log = os.environ.get("TE_CONSOLE_PROBE")
if log:
    rec = {"pid": os.getpid(), "hook": sys.argv[1:2] == ["hook"]}
    if os.name == "nt":
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        rec["console_window"] = bool(hwnd)
        rec["visible"] = bool(hwnd and ctypes.windll.user32.IsWindowVisible(hwnd))
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\\n")
'''

# opencode 2.x's plugin context, as far as the plugin reads it: one prompt, one tool call, then
# the process exits (the plugin hands the open session over then, with a blocking call).
HARNESS = '''import plugin from %s;
const hooks: Record<string, any> = {};
await plugin.setup({
  location: { directory: process.env.TE_WORK },
  session: { hook: async (name: string, fn: any) => { hooks["session." + name] = fn; } },
  tool: { hook: async (name: string, fn: any) => { hooks["tool." + name] = fn; } },
  event: { subscribe: async function* () {} },
});
await hooks["session.prompt"]({ sessionID: "ses_t", messageID: "m1", prompt: { text: "say hi" } });
await hooks["tool.execute.after"]({ sessionID: "ses_t", status: "completed", tool: "bash",
  id: "c1", input: { command: "echo hi" }, result: { content: "hi", metadata: { exit: 0 } } });
console.log("harness done");
process.exit(0);
'''


def _run(tmp_path, plugin_file, name):
    """The harness against `plugin_file`, from a parent with no console; the probe's records."""
    work, probe_dir = tmp_path / f"work-{name}", tmp_path / "probe"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "sitecustomize.py").write_text(PROBE, encoding="utf-8")
    log = tmp_path / f"probe-{name}.jsonl"
    harness = tmp_path / f"harness-{name}.ts"
    harness.write_text(HARNESS % json.dumps(pathlib.Path(plugin_file).as_uri()), encoding="utf-8")
    env = dict(os.environ, BUN_BE_BUN="1", TE_WORK=str(work), TE_PYTHON=sys.executable,
               TOKENECONOMIZER_HOME=str(tmp_path / f"te-{name}"), TE_CORE_PATH=REPO,
               PYTHONPATH=str(probe_dir), TE_CONSOLE_PROBE=str(log))
    env.pop("TE_DISABLE", None)
    flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    out = subprocess.run([OPENCODE, "run", str(harness)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                         cwd=str(work), env=env, timeout=240, creationflags=flags)
    assert out.returncode == 0 and "harness done" in out.stdout, (out.stdout[-1500:],
                                                                   out.stderr[-1500:])
    recs = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    print(f"{name}: {recs}")
    # SessionStart, UserPromptSubmit, PostToolUse, and SessionEnd as the process exits
    assert len([r for r in recs if r["hook"]]) >= 4, (name, recs, out.stderr[-1500:])
    return recs


def test_the_plugins_hooks_open_no_console_window(tmp_path):
    recs = _run(tmp_path, os.path.join(REPO, "plugin", "opencode", "tokeneconomizer.ts"),
                "plugin")
    if os.name != "nt":
        return                                    # the round trip is what is checked here
    assert not [r for r in recs if r["visible"]], recs
    # The control: the same plugin without `windowsHide`, next to a copy of its launchers. Each
    # hook it starts gets a console window of its own, shown: the windows the fix removes. (What
    # a hook starts in the background has none either way.)
    copy = tmp_path / "plugin"
    shutil.copytree(os.path.join(REPO, "plugin", "scripts"), copy / "scripts")
    (copy / "opencode").mkdir()
    text = open(os.path.join(REPO, "plugin", "opencode", "tokeneconomizer.ts"),
                encoding="utf-8").read()
    assert "windowsHide: true" in text
    (copy / "opencode" / "tokeneconomizer.ts").write_text(
        text.replace("windowsHide: true", "windowsHide: false"), encoding="utf-8")
    control = _run(tmp_path, copy / "opencode" / "tokeneconomizer.ts", "control")
    assert all(r["visible"] for r in control if r["hook"]), control


class _Chat(BaseHTTPRequestHandler):
    """An OpenAI-compatible chat endpoint that answers every request with "done"."""

    def log_message(self, *args):
        pass

    def _send(self, kind, body):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send("application/json", json.dumps({"object": "list", "data": [
            {"id": "te-model", "object": "model"}]}).encode())

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        usage = {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101}
        if not req.get("stream"):
            self._send("application/json", json.dumps({
                "id": "c", "object": "chat.completion", "created": 0, "model": "te-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "done"}}],
                "usage": usage}).encode())
            return

        def chunk(choice, **extra):
            return "data: " + json.dumps(dict(id="c", object="chat.completion.chunk", created=0,
                                              model="te-model", choices=[choice], **extra)) + "\n\n"
        self._send("text/event-stream", (
            chunk({"index": 0, "delta": {"role": "assistant", "content": "done"},
                   "finish_reason": None})
            + chunk({"index": 0, "delta": {}, "finish_reason": "stop"}, usage=usage)
            + "data: [DONE]\n\n").encode())


@pytest.fixture
def chat():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _service_run(tmp_path, chat, name, hide=True):
    """One prompt through opencode's own background service, with the plugin installed in its
    config directory (beside a copy of its launchers) and the stand-in model; the probe's
    records of the Pythons the service's hooks started."""
    home, work = tmp_path / f"home-{name}", tmp_path / f"work-svc-{name}"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    probe_dir, log = tmp_path / "probe", tmp_path / f"probe-svc-{name}.jsonl"
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "sitecustomize.py").write_text(PROBE, encoding="utf-8")
    env = dict(os.environ, XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"),
               TE_PYTHON=sys.executable, TOKENECONOMIZER_HOME=str(tmp_path / f"te-svc-{name}"),
               TE_CORE_PATH=REPO, PYTHONPATH=str(probe_dir), TE_CONSOLE_PROBE=str(log))
    for var in ("TE_DISABLE", "BUN_BE_BUN"):
        env.pop(var, None)

    def oc(*args, timeout=120):
        return subprocess.run([OPENCODE, *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, cwd=str(work), env=env,
                              timeout=timeout)

    # Where this opencode keeps its configuration and logs, as it says itself.
    paths = dict(ln.split(None, 1) for ln in oc("debug", "paths").stdout.splitlines()
                 if len(ln.split(None, 1)) == 2)
    config = pathlib.Path(paths["config"].strip())
    shutil.copytree(os.path.join(REPO, "plugin", "scripts"), config / "scripts")
    text = open(os.path.join(REPO, "plugin", "opencode", "tokeneconomizer.ts"),
                encoding="utf-8").read()
    (config / "plugins").mkdir(parents=True, exist_ok=True)
    (config / "plugins" / "tokeneconomizer.ts").write_text(
        text if hide else text.replace("windowsHide: true", "windowsHide: false"),
        encoding="utf-8")
    (config / "opencode.json").write_text(json.dumps({
        "provider": {"te": {"npm": "@ai-sdk/openai-compatible", "name": "TE stand-in",
                            "options": {"baseURL": chat, "apiKey": "none"},
                            "models": {"te-model": {"name": "TE model"}}}},
        "model": "te/te-model", "small_model": "te/te-model",
        "autoupdate": False, "share": "disabled"}), encoding="utf-8")
    service_log = pathlib.Path(paths["log"].strip()) / "opencode.log"
    run = None
    try:
        oc("service", "stop")
        started = oc("service", "start")
        try:
            run = oc("run", "-m", "te/te-model", "Say hi.", timeout=240)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)                                   # the turn's last hook
    finally:
        oc("service", "stop")
    recs = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    print(f"service-{name}: {recs}")
    tail = (service_log.read_text(encoding="utf-8", errors="replace")[-3000:]
            if service_log.exists() else "no service log")
    assert run is not None and run.returncode == 0, (
        name, started.stdout[-500:], started.stderr[-500:],
        run and run.stdout[-800:], run and run.stderr[-800:], tail)
    # SessionStart and UserPromptSubmit when the prompt arrives; Stop when the turn ends
    assert len([r for r in recs if r["hook"]]) >= 2, (name, recs, tail)
    return recs


def test_hooks_from_opencodes_background_service_open_no_console_window(tmp_path, chat):
    """The real service, as the TUI and `opencode run` use it: on Windows it has no console of
    its own, so without `windowsHide` each hook it starts is shown a console window."""
    recs = _service_run(tmp_path, chat, "plugin")
    if os.name != "nt":
        return
    assert not [r for r in recs if r["visible"]], recs
    control = _service_run(tmp_path, chat, "control", hide=False)
    assert all(r["visible"] for r in control if r["hook"]), control
