"""`te --host copilot probe`: the plugin, end to end, in real Copilot sessions on your own model.

What `docs/PLUGIN.md` §3 verified, run by anyone on their own setup -- the Copilot subscription,
or a BYOK provider (`COPILOT_PROVIDER_*`) where an organization's policy refuses the
subscription's models. It builds a throwaway repository with one environment trap (bare `pytest`
fails, `python -m pytest` works), records the tool catalog, and runs the same short prompt in
real sessions with the plugin loaded, under a state root of its own -- your learned state is
never touched. The trap is the commonest one in Python repositories: the project is set up for
pytest (`[tool.pytest.ini_options]`), but pytest is not installed, so bare `pytest` and
`python -m pytest` both fail while the tests, written with `unittest`, pass under
`python -m unittest`. The repository is created inside a folder Copilot trusts when there is
one, because Copilot ignores an untrusted folder's settings, the deny list's file included.
Then it applies the deny list it proposed, runs one more session, restores it, and reports:

  * per session: whether the learned fact was in the context, the first test command the
    model ran, how many test commands failed, the request's tool-definition tokens;
  * whether the sessions WITH the fact avoided the trap that the sessions without it hit;
  * the tool-definition tokens before and after the deny list.

It makes real model calls (default 7 short sessions; ~25-60k tokens each, mostly cached) and
leaves those sessions in Copilot's own history. Nothing else outside a temporary directory is
written; `--keep` keeps that directory for inspection.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .. import trace
from . import denylist, hosts, sidecall, toolcheck
from .context import LiveContext

PROMPT = ("Run this repository's test suite and report how many tests pass. Do not install "
          "anything. Keep the answer to one line.")

# pytest must be missing for every interpreter the session might use, whatever is installed:
# the trap's `python`/`python3` put this stub first on the module path, so `python -m pytest`
# fails as it does where pytest is not installed. Nothing in the repository gives it away.
_PYTEST_STUB = 'raise ModuleNotFoundError("No module named \'pytest\'", name="pytest")\n'


# A test-runner invocation, and the forms that cannot work in the probe's repository. Counted
# by attempt, not exit code: models pipe test runs into `| tail`, which hides the failure (two
# of the first real Haiku sessions did exactly that).
_RUNNER_RE = re.compile(r"(?:^|[\s;&|(])(?:py\.?test|python[\d.]*\s+-m\s+(?:pytest|unittest))\b")
_TRAP_RE = re.compile(r"(?:^|[\s;&|(])(?:py\.?test|python[\d.]*\s+-m\s+pytest)\b")


def test_commands(cmds):
    """(runner commands in order, how many of them tried pytest) for a session's shell commands."""
    runs = [c for c in cmds if _RUNNER_RE.search(c)]
    return runs, sum(1 for c in runs if _TRAP_RE.search(c))


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def make_repo(root):
    """The throwaway repository and the trap directory put first on the sessions' PATH."""
    repo, trap = os.path.join(root, "repo"), os.path.join(root, "trap")
    _write(os.path.join(repo, "calc.py"), "def add(a, b):\n    return a + b\n")
    _write(os.path.join(repo, "tests", "__init__.py"), "")
    _write(os.path.join(repo, "tests", "test_calc.py"),
           "import unittest\n\nfrom calc import add\n\n\nclass TestAdd(unittest.TestCase):\n"
           "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n")
    _write(os.path.join(repo, "pyproject.toml"),
           '[project]\nname = "calc"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _write(os.path.join(repo, ".gitignore"), "__pycache__/\n")
    stub = os.path.join(trap, "pymods")
    _write(os.path.join(stub, "pytest", "__init__.py"), _PYTEST_STUB)
    _write(os.path.join(trap, "pytest"),
           '#!/bin/sh\necho "bash: pytest: command not found" >&2\nexit 127\n', 0o755)
    for name in ("python", "python3"):
        _write(os.path.join(trap, name), f'#!/bin/sh\nPYTHONPATH="{stub}${{PYTHONPATH:+:$PYTHONPATH}}" '
               f'exec "{sys.executable}" "$@"\n', 0o755)
    git = ["git", "-c", "user.name=te-probe", "-c", "user.email=te-probe@localhost",
           "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(git + ["-C", repo, "add", "-A"], check=True)
    subprocess.run(git + ["-C", repo, "commit", "-qm", "probe"], check=True)
    return repo, trap


def _plugin_root():
    root = os.environ.get("TE_PLUGIN_ROOT")
    if root and os.path.isfile(os.path.join(root, "hooks", "copilot-hooks.json")):
        return root
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "plugin"), os.path.dirname(here)):
        if os.path.isfile(os.path.join(cand, "hooks", "copilot-hooks.json")):
            return cand
    raise RuntimeError("cannot find the plugin directory (set TE_PLUGIN_ROOT)")


def _session_env(state, trap):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT",
                        "TE_SIDECALL", "TE_DISABLE", "TERM_PROGRAM")}
    # An installed copy of the plugin does not run beside the --plugin-dir one: Copilot loads
    # one plugin per name (verified: one SessionStart hook with both present).
    # The probe applies the deny list itself, for one measured session, and restores it: its
    # sessions must only propose one, as `toolprune=auto` would apply it for all the rest.
    # Micro-skills is off by default; the probe checks that a learned fact reaches the context,
    # and the trap's fact is one the free layer learns without a model call.
    env.update(TOKENECONOMIZER_HOME=state, PATH=trap + os.pathsep + env.get("PATH", ""),
               TE_TOOLPRUNE="propose", TE_MICROSKILLS="envfacts")
    return env


def run_session(repo, env, plugin, model, cli=None, timeout=600):
    from .sidecall import copilot_cli
    cli = copilot_cli(cli)
    cmd = [cli, "-p", PROMPT, "--plugin-dir", plugin, "--allow-all-tools", "--no-auto-update",
           "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    out = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, encoding="utf-8",
                         errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    sid, answer, error = None, "", (out.stderr or "").strip().splitlines()[-1:] or [""]
    for line in (out.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            sid = ev.get("sessionId")
        elif ev.get("type") == "assistant.message":
            answer = (ev.get("data") or {}).get("content") or answer
    return sid, answer.strip(), error[0]


def inspect_session(sid):
    """What one finished session did, from Copilot's own log."""
    log = os.path.join(hosts.copilot_home(), "session-state", sid, "events.jsonl")
    hosts.Copilot().settle(log)
    injected = False
    cmds = [(args or {}).get("command") or ""
            for (name, args, _, _) in [c for t in trace.copilot_turns(log) for c in t]
            if name == "bash"]
    tests, tried_pytest = test_commands(cmds)
    for line in open(log, encoding="utf-8", errors="replace"):
        if '"hook.end"' in line and '"sessionStart"' in line:
            out = (json.loads(line).get("data") or {}).get("output") or {}
            injected = injected or "fails here" in (out.get("additionalContext") or "")
    usage = trace.copilot_session_usage(log)
    first = tests[0] if tests else None
    if first:
        first = re.sub(r"^cd \S+ && ", "", first)[:60]
    return {"session": sid, "injected": injected, "tests": tests, "first_test": first,
            "tried_pytest": tried_pytest,
            "tool_definition_tokens": usage.get("tool_definitions_tokens"),
            "input_per_request": (round(sum(usage.get(k) or 0 for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                / usage["requests"]) if usage.get("requests") else None)}


def _wait_folded(ctx, sid, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sid in (ctx.store.read("folded.json") or {}):
            return True
        time.sleep(1)
    return False


def _line(r):
    return (f"  {r['label']:>10}: fact in context={'yes' if r['injected'] else 'no '}  "
            f"first test command: {r['first_test'] or '-'}  pytest attempts: "
            f"{r['tried_pytest']}  tool definitions: {r['tool_definition_tokens']} tokens")


def run(sessions=6, model=None, keep=False, cli="copilot", out=print):
    if not shutil.which(cli):
        raise RuntimeError(f"`{cli}` is not on PATH")
    model = sidecall.copilot_model(model or "")
    plugin = _plugin_root()
    base = next((f for f in (denylist.copilot_trusted_folders() or [])
                 if os.path.isdir(f) and os.access(f, os.W_OK)), None)
    root = tempfile.mkdtemp(prefix=".te-probe-copilot-", dir=base)
    state = os.path.join(root, "state")
    try:
        repo, trap = make_repo(root)
        env = _session_env(state, trap)
        route = ("BYOK " + (os.environ.get("COPILOT_PROVIDER_TYPE") or "openai")
                 if os.environ.get("COPILOT_PROVIDER_BASE_URL") else "Copilot subscription")
        out(f"TokenEconomizer probe -- Copilot ({route}), model {model or '(Copilot default)'}, "
            f"plugin {plugin}")
        if not base:
            out("  note: Copilot trusts no folder here, so the deny list cannot take effect "
                "in the probe's repository (Copilot ignores an untrusted folder's settings)")
        _, tools = toolcheck.refresh_catalog(state, host="copilot")
        out(f"  catalog: {len(tools)} tools recorded (local capture, no model call)")
        ctx = LiveContext.build(hosts.Copilot(), repo, env)
        results = []
        for i in range(1, sessions + 1):
            sid, answer, err = run_session(repo, env, plugin, model, cli=cli)
            if not sid:
                out(f"  session {i}: no session ran ({err or 'no output'})")
                return 1
            _wait_folded(ctx, sid)
            r = dict(inspect_session(sid), label=f"session {i}", answer=answer)
            results.append(r)
            out(_line(r) + f"  answer: {answer[:40]}")
        unt = ctx.store.read("untool.json") or {}
        proposal = unt.get("proposal") or []
        lean = None
        if proposal:
            denylist.apply(repo, proposal, ctx.store, host="copilot")
            try:
                sid, answer, err = run_session(repo, env, plugin, model, cli=cli)
                if sid:
                    _wait_folded(ctx, sid)
                    lean = dict(inspect_session(sid), label="deny list", answer=answer)
                    out(_line(lean) + f"  answer: {answer[:40]}")
            finally:
                denylist.restore(repo, ctx.store, host="copilot")
        # the verdict
        everything = results + ([lean] if lean else [])
        without = [r for r in everything if not r["injected"] and r["tests"]]
        with_ = [r for r in everything if r["injected"] and r["tests"]]
        out("")
        out(f"  without the fact: {sum(1 for r in without if r['tried_pytest'])}/{len(without)} "
            f"sessions tried pytest, which is not installed; with it: "
            f"{sum(1 for r in with_ if r['tried_pytest'])}/"
            f"{len(with_)}" + ("" if with_ else "  (the fact never cleared its firing gate; "
                                                 "run more --sessions)"))
        full = [r["tool_definition_tokens"] for r in results if r["tool_definition_tokens"]]
        if proposal and lean and full and lean["tool_definition_tokens"]:
            out(f"  deny list {', '.join(proposal)}: tool definitions {full[-1]} -> "
                f"{lean['tool_definition_tokens']} tokens per request")
        elif not proposal:
            out("  deny list: nothing proposed (a server is proposed once none of its tools "
                "was used in `toolprune_evidence` sessions)")
        return 0
    finally:
        if keep:
            out(f"  kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
