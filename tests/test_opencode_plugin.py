"""The opencode plugin under opencode's own runtime: CI's `opencode` job (`TE_OPENCODE_E2E=1`).

opencode 2.x runs plugins in its background service, which on Windows has no console of its own,
so every hook the plugin starts (a `python` per prompt, per tool call, per turn) got a new console
window: the terminal "flickered" while the agent worked. These load the plugin's 2.x entry in
opencode's binary run as Bun (`BUN_BE_BUN=1`), from a parent started without a console as the
service is, drive one prompt and one tool call through it, and read back from each hook process
whether it was given a console window. A copy of the plugin without `windowsHide` is the control.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENCODE = os.environ.get("TE_OPENCODE_BIN") or shutil.which("opencode")

pytestmark = pytest.mark.skipif(
    os.environ.get("TE_OPENCODE_E2E") != "1" or not OPENCODE,
    reason="drives opencode's own runtime: set TE_OPENCODE_E2E=1 and TE_OPENCODE_BIN to the "
           "opencode binary (CI's opencode job)")

# Imported by every hook's Python (through PYTHONPATH): whether it has a console window.
PROBE = '''import json, os
log = os.environ.get("TE_CONSOLE_PROBE")
if log:
    rec = {"pid": os.getpid()}
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
    assert len(recs) >= 4, (name, recs, out.stderr[-1500:])
    return recs


def test_the_plugins_hooks_open_no_console_window(tmp_path):
    recs = _run(tmp_path, os.path.join(REPO, "plugin", "opencode", "tokeneconomizer.ts"),
                "plugin")
    if os.name != "nt":
        return                                    # the round trip is what is checked here
    assert not [r for r in recs if r["visible"]], recs
    # The control: the same plugin without `windowsHide`, next to a copy of its launchers. Each
    # hook gets a console of its own, so this test would see the windows the fix removes.
    copy = tmp_path / "plugin"
    shutil.copytree(os.path.join(REPO, "plugin", "scripts"), copy / "scripts")
    (copy / "opencode").mkdir()
    text = open(os.path.join(REPO, "plugin", "opencode", "tokeneconomizer.ts"),
                encoding="utf-8").read()
    assert "windowsHide: true" in text
    (copy / "opencode" / "tokeneconomizer.ts").write_text(
        text.replace("windowsHide: true", "windowsHide: false"), encoding="utf-8")
    control = _run(tmp_path, copy / "opencode" / "tokeneconomizer.ts", "control")
    assert all(r["console_window"] for r in control), control
