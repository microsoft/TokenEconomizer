"""Side calls -- the proposer, the stuck judge, the nudge writer -- on the user's own agent.

The mechanisms call an optimizer as `runner(prompt, model) -> (text, effective_tokens)`
(`runner.AgentRunner.optimizer_call`). In the harness that call went to the agent's model through
the harness's own routing. Here it goes through the host CLI the user is already signed in to,
so no extra API key is needed and the call is billed where the session is:

  * Claude Code: `claude -p --output-format json --tools "" --max-turns 1`, with none of the
    person's MCP servers (`--strict-mcp-config` and an empty `--mcp-config`, where the CLI has
    them), on `paid_model` (default `haiku`). This is a deliberate departure from the
    experiments, which ran every optimizer call on the agent's own model so overhead could not
    look cheaper than the arm it was charged to; in a user's session the cheapest capable model
    is the right default, and the ledger records the real cost either way.
  * Codex: `codex exec --json --disable hooks --ephemeral -s read-only`. Each such call carries
    Codex's full base prompt (~25k tokens, mostly cached) on the user's plan quota, one reason
    micro-skills (whose proposer calls it) defaults off and the loop guard's judge is woken
    only by a sign of trouble.
  * Copilot CLI: `copilot -p` as a pure text completion, as `te_harness.copilot.optimizer_call`
    runs it: a neutral cwd, no custom instructions, no built-in MCP server, every tool denied
    and excluded. Its usage is read from the call's own session log (`result.sessionId` ->
    `session-state/<id>/events.jsonl`), the same `session.shutdown` the ledger prices sessions
    from. The model is `copilot_model`'s choice: under BYOK, Copilot has no router to fall back
    on, and `COPILOT_MODEL=auto` -- a common setting, the subscription's router -- made the call
    answer nothing (1.0.83, an Anthropic provider).
  * Pi: `pi --print --mode json` with nothing loaded but the model -- no tools, extensions,
    skills, prompt templates, context files, MCP servers or saved session. With no `paid_model`
    it runs on the person's own default provider and model, under whatever login Pi holds for
    it (a subscription signed in with `/login`, or an API key). A model whose provider an
    extension registers needs that extension loaded (`pi_extensions`); `pi_cli` names the `pi`
    to run, where the first on PATH is a wrapper the calls should not go through.

  * opencode: `opencode run --format json` in a throwaway directory, on the person's default
    model, with `bash`, `edit`, `webfetch` and `websearch` denied, no other plugin (`--pure` on
    1.x, `--standalone` on 2.x) and its session deleted afterwards.

Every side call runs with `TE_SIDECALL=1`, which the plugin's hook entry point treats as "do
nothing": without it the nested agent would run this plugin's hooks and recurse. A call that
returns no reply -- a CLI that is not signed in, say -- leaves one line in the plugin's
errors.log, which `te doctor` shows. One that answered it is not signed in also pauses that
host's side calls (`paused`): every later call would fail the same way, and Copilot's leave an
empty session behind each time. Nothing here runs unless micro-skills is on (the proposer)
or the loop guard is (the judge and the nudge writer).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from .. import trace
from . import accounting

TIMEOUT_S = 180


def _env():
    return dict(os.environ, TE_SIDECALL="1")


# What a CLI says when it is not signed in -- Copilot's "To authenticate, ... run the '/login'
# command" under BYOK, Claude Code's "Please run /login".
_SIGNED_OUT_RE = re.compile(r"To authenticate|/login\b|\b[Nn]ot (?:logged|signed) in")


def _no_reply(host, out):
    """Note a side call that came back empty, which nothing else would show -- as under
    Copilot's BYOK, which does not pass the provider's variables to the hooks that start it.
    A CLI that said it is not signed in pauses the host's side calls."""
    import time
    from .paths import state_root
    stderr = getattr(out, "stderr", "") or ""
    tail = " ".join(stderr.split())[-300:]
    try:
        root = state_root()
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} sidecall {host}: no reply "
                     f"(exit {getattr(out, 'returncode', '?')}) {tail}\n")
        if _SIGNED_OUT_RE.search(stderr + (getattr(out, "stdout", "") or "")):
            path = _pause_path(host, root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"at": time.time(), "reason": tail[-200:]}, fh)
    except OSError:
        pass


def _pause_path(host, root=None):
    from .paths import state_root
    return os.path.join(root or state_root(), "hosts", host, "sidecall.json")


def paused(host, root=None):
    """The pause on this host's side calls ({"at", "reason"}) since its CLI said it is not
    signed in, or None. `te config set` of `microskills` or `loopguard` lifts it (`resume`)."""
    try:
        with open(_pause_path(host, root), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("at") else None


def resume(host, root=None):
    """Lift the pause; True if there was one."""
    try:
        os.remove(_pause_path(host, root))
        return True
    except OSError:
        return False


def claude_cli():
    """The Claude Code executable to start: `claude` on PATH (with PATHEXT on Windows, so a
    `claude.cmd` shim too), else the native installer's `~/.local/bin/claude` -- which is often
    not on PATH, a shell reaching it through an alias or a profile function that a subprocess
    does not see. Falls back to the bare name, so the error stays the CLI-not-found one."""
    found = shutil.which("claude")
    if found:
        return found
    exe = os.path.expanduser(os.path.join("~", ".local", "bin",
                                          "claude.exe" if os.name == "nt" else "claude"))
    return exe if os.path.isfile(exe) else "claude"


_HELP = {}


def _claude_help(cli):
    """`claude --help`, read once per executable: which flags this Claude Code has."""
    if cli not in _HELP:
        try:
            _HELP[cli] = subprocess.run([cli, "--help"], capture_output=True,
                                        encoding="utf-8", errors="replace", timeout=30,
                                        stdin=subprocess.DEVNULL).stdout or ""
        except (OSError, subprocess.SubprocessError):
            _HELP[cli] = ""
    return _HELP[cli]


def claude_flags(cli=None):
    """`--no-session-persistence` when this Claude Code has it: a side call or a measurement is
    not a session of the person's, and should not appear in their history (`claude --resume`)."""
    cli = cli or claude_cli()
    return ["--no-session-persistence"] if "--no-session-persistence" in _claude_help(cli) else []


def claude_no_mcp(cli, cwd):
    """`--strict-mcp-config` with an empty `--mcp-config`, when this Claude Code has them: a side
    call needs none of the person's MCP servers. `--tools ""` removes only the built-in tools,
    ToolSearch with them, so every MCP server stayed attached and its tools were sent in full:
    with 503 of them (31 servers, Claude Code 2.1.301) a one-line Haiku call came to ~570k
    effective tokens, ~14k without. The empty config is a file in the call's own directory, so no
    `claude.cmd` shim re-quotes it on Windows. Not for the catalog probes, which need the tools."""
    if "--strict-mcp-config" not in _claude_help(cli):
        return []
    path = os.path.join(cwd, "mcp.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {}}, fh)
    return ["--strict-mcp-config", "--mcp-config", path]


def claude_call(prompt, model="haiku", cli=None, timeout=TIMEOUT_S):
    cli = cli or claude_cli()
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        # `--mcp-config` takes several values, so an option follows it, never a bare argument.
        cmd = ([cli, "-p", "--output-format", "json", "--max-turns", "1", "--tools", ""]
               + claude_no_mcp(cli, cwd) + ["--model", model or "haiku"] + claude_flags(cli))
        out = subprocess.run(cmd, input=prompt, capture_output=True,
                             encoding="utf-8", errors="replace", timeout=timeout, env=_env(),
                             cwd=cwd)
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        data = {}
    if not data.get("result"):
        _no_reply("claude", out)
    usage = dict(data.get("usage") or {})
    usage["model"] = next(iter(data.get("modelUsage") or {}), None) or (model or "claude")
    eff, _ = accounting.effective(usage)
    return data.get("result") or "", eff


def codex_call(prompt, model="", cli="codex", timeout=TIMEOUT_S):
    cmd = [cli, "exec", "--json", "--disable", "hooks", "--ephemeral", "-s", "read-only",
           "--skip-git-repo-check"]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=_env(), cwd=cwd)
    text, usage = "", None
    for line in (out.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = ev["item"].get("text") or text
        elif ev.get("type") == "turn.completed":
            usage = ev.get("usage") or usage
    if not text:
        _no_reply("codex", out)
    eff = None
    if usage:
        inp = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        cw = int(usage.get("cache_write_input_tokens") or 0)
        eff, _ = accounting.effective({
            "input_tokens": max(0, inp - cached - cw), "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": cw,
            "output_tokens": int(usage.get("output_tokens") or 0)
            + int(usage.get("reasoning_output_tokens") or 0),
            "model": model or "codex"})
    return text, eff


# Pi before 1.0 has no `--no-mcp` (and no built-in MCP to switch off); it rejects the flag.
PI_FLAGS = ("--print", "--mode", "json", "--no-session", "--no-tools", "--no-extensions",
            "--no-skills", "--no-prompt-templates", "--no-context-files", "--offline")


def pi_extension_args(extensions):
    """`--extension <path>` for each extension a side call loads despite `--no-extensions` (Pi
    loads explicit ones still): a provider registered by an extension -- a company gateway, a
    package's -- has no model in the call otherwise. Paths are made absolute, since the call runs
    in a throwaway directory; `builtin:<name>` is passed as it is. `--no-tools` keeps any tools
    such an extension adds out of the call."""
    return [a for p in extensions or () for a in (
        "--extension",
        p if p.startswith("builtin:") else os.path.abspath(os.path.expanduser(p)))]


def pi_call(prompt, model="", cli="pi", timeout=TIMEOUT_S, extensions=()):
    model_args = ["--model", model] if model else []
    ext_args = pi_extension_args(extensions)
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        for extra in (["--no-mcp"], []):
            cmd = [cli, *PI_FLAGS, *extra, *ext_args, *model_args, "--", prompt]
            out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                                 timeout=timeout, env=_env(), cwd=cwd,
                                 stdin=subprocess.DEVNULL)
            if "Unknown option: --no-mcp" not in (out.stderr or ""):
                break
    text, usage = trace.pi_json_reply(out.stdout)
    if not text:
        _no_reply("pi", out)
    eff = None
    if usage:
        usage.pop("cost_usd", None)
        usage["model"] = usage.get("model") or model or "pi"
        eff, _ = accounting.effective(usage)
    return text, eff


# A side call only answers, so what can change anything or reach out is denied. Its directory is
# an empty throwaway one, which is why the read-only tools are left alone: opencode 1.19.5 stalls
# at startup, with no output, when `read`, `task` or `*` is denied (it waits on a prompt nobody
# sees).
OPENCODE_CONFIG = json.dumps({"permission": {"bash": "deny", "edit": "deny", "webfetch": "deny",
                                             "websearch": "deny"},
                              "share": "disabled", "autoupdate": False})


_OC_FLAGS = {}


def opencode_flags(cli="opencode"):
    """The flags a side call adds, by what this opencode's `run --help` lists: `--pure` (1.x: no
    external plugins) and `--standalone` (2.x: a private server of its own, since plugins there
    run in a shared background service that does not inherit `TE_SIDECALL`, and the nested
    session would run this plugin's hooks)."""
    if cli not in _OC_FLAGS:
        try:
            help_text = subprocess.run([cli, "run", "--help"], capture_output=True,
                                       encoding="utf-8", errors="replace", timeout=30,
                                       stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        _OC_FLAGS[cli] = [f for f in ("--pure", "--standalone") if f in help_text]
    return list(_OC_FLAGS[cli])


def opencode_call(prompt, model="", cli="opencode", timeout=TIMEOUT_S):
    """A side call through `opencode run`: no external plugins where the CLI can say so, every
    tool that changes anything denied, in a throwaway directory, on the person's own default
    model unless `model` names one. The session it creates is deleted afterwards, so it is not
    in their history."""
    model_args = ["--model", model] if model else []
    env = dict(_env(), OPENCODE_CONFIG_CONTENT=OPENCODE_CONFIG)
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        out = subprocess.run([cli, "run", *opencode_flags(cli), "--format", "json", *model_args,
                              prompt],
                             capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
        text, usage, sid = trace.opencode_run_reply(out.stdout)
        if sid:
            try:
                subprocess.run([cli, "session", "delete", sid], capture_output=True, timeout=30,
                               env=env, cwd=cwd, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
    if not text:
        _no_reply("opencode", out)
    eff = None
    if usage:
        usage.pop("cost_usd", None)
        usage["model"] = model or "opencode"
        eff, _ = accounting.effective(usage)
    return text, eff


def cursor_call(prompt, model="", cli="cursor-agent", timeout=TIMEOUT_S):
    """Use Cursor Agent's read-only Ask mode for optimizer calls in an empty directory.

    Cursor's current CLI JSON result does not expose token usage, so the call is useful for the
    judge/proposer but its cost remains unpriced in TokenEconomizer's ledger.
    """
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        cmd = [cli, "--print", "--output-format", "json", "--mode", "ask", "--trust"]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=_env(), cwd=cwd, stdin=subprocess.DEVNULL)
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        data = {}
    text = data.get("result") if isinstance(data, dict) else ""
    if not text:
        _no_reply("cursor", out)
    return text or "", None


# Everything the Copilot CLI offers (1.0.83 names), so the completion is offered nothing; a name
# the installed CLI does not know is ignored.
COPILOT_TOOLS = ("bash", "read_bash", "write_bash", "stop_bash", "list_bash", "view", "grep",
                 "rg", "glob", "edit", "create", "str_replace", "apply_patch", "web_fetch",
                 "web_search", "ask_user", "session_store_sql", "sql", "task", "skill",
                 "list_agents", "read_agent", "write_agent", "fetch_copilot_cli_documentation")


# The cheapest capable model by BYOK provider type, in the ids that provider expects.
BYOK_DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5"}


def copilot_model(paid_model="", env=None):
    """The model a Copilot side call names: `paid_model`; else a concrete model the person's
    environment names; else, under BYOK, the provider's cheap default; else "" (Copilot's own
    default, which on the subscription is its router)."""
    env = os.environ if env is None else env
    if paid_model:
        return paid_model
    for k in ("COPILOT_PROVIDER_MODEL_ID", "COPILOT_MODEL"):
        v = (env.get(k) or "").strip()
        if v and v.lower() != "auto":
            return v
    base = env.get("COPILOT_PROVIDER_BASE_URL") or ""
    if "openrouter.ai" in base:            # OpenRouter's own ids, on either wire
        return "anthropic/claude-haiku-4.5"
    if base:
        return BYOK_DEFAULT_MODEL.get((env.get("COPILOT_PROVIDER_TYPE") or "openai").lower(), "")
    return ""


def copilot_call(prompt, model="", cli="copilot", timeout=TIMEOUT_S):
    from .. import trace
    from . import hosts
    model = copilot_model(model)
    cmd = [cli, "-p", prompt, "--output-format", "json", "--no-color", "--no-auto-update",
           "--no-custom-instructions", "--disable-builtin-mcps"]
    # The person's own MCP servers too: a completion needs none of them to connect.
    for server in sorted(trace.copilot_mcp_servers() - {"github-mcp-server"}):
        cmd += ["--disable-mcp-server", server]
    for t in COPILOT_TOOLS:
        cmd += ["--deny-tool", t, "--excluded-tools", t]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="te-side-") as cwd:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=_env(), cwd=cwd, stdin=subprocess.DEVNULL)
    text, sid = "", None
    for line in (out.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant.message":
            text = (ev.get("data") or {}).get("content") or text
        elif ev.get("type") == "result":
            sid = ev.get("sessionId") or sid
    if not text:
        _no_reply("copilot", out)
    eff = None
    if sid:
        log = os.path.join(hosts.copilot_home(), "session-state", sid, "events.jsonl")
        hosts.Copilot().settle(log, wait_s=5.0)
        usage = trace.copilot_session_usage(log)
        usage["model"] = usage.get("model") or model or "copilot"
        eff, _ = accounting.effective(usage)
    return text, eff


# The command each agent's side calls run, as the notices name it; the rest run `<agent> -p`.
_CALLS = {"codex": "codex exec", "opencode": "opencode run", "cursor": "cursor-agent -p"}


def call_name(host_name):
    """How a notice names this agent's side-call command: `claude -p`, `codex exec`, ..."""
    return _CALLS.get(host_name, f"{host_name} -p")


def runner_for(host_name, config):
    """The optimizer callable for this host. Each host's calls go through that host's own CLI,
    never another agent's. The caller decides whether a call is allowed: micro-skills' proposer
    when micro-skills is on, the loop guard's judge when the guard is."""
    model = config.paid_model
    if host_name == "pi":
        cli = os.path.expanduser(config.pi_cli) if config.pi_cli else "pi"
        extensions = list(config.pi_extensions)
        return lambda prompt, _m="agent": pi_call(prompt, model=model, cli=cli,
                                                  extensions=extensions)
    if host_name == "opencode":
        return lambda prompt, _m="agent": opencode_call(prompt, model=model)
    if host_name == "cursor":
        return lambda prompt, _m="agent": cursor_call(prompt, model=model)
    if host_name == "codex":
        return lambda prompt, _m="agent": codex_call(prompt, model=model)
    if host_name == "copilot":
        return lambda prompt, _m="agent": copilot_call(prompt, model=model)
    return lambda prompt, _m="agent": claude_call(prompt, model=model or "haiku")
