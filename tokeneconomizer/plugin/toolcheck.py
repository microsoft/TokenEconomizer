"""Asking the host CLIs what tools a session really loads.

  * `refresh_catalog` -- the tool list a fresh session loads (Claude Code, Copilot), recorded so
                         pruning sees the always-loaded tools, not only the ones a session used;
  * `verify_copilot`, `verify_codex` -- what an applied deny list actually removed;
  * `measure`         -- one request with and without a proposal. On Claude Code that is two
                         small `claude -p` requests on the cheapest model; Codex renders its
                         prompt input locally (`codex debug prompt-input`) and Copilot's request
                         is captured by a scripted endpoint on localhost, so neither calls a model.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import adapter as live_adapter
from .denylist import CODEX_TOGGLES, write_json as _write_json
from .sidecall import claude_cli, claude_flags


def verify_copilot(repo_root, servers, cli="copilot"):
    """{server: tools still in the request} for a session started in `repo_root`, by the local
    capture (no model call); None if no request could be captured."""
    body = copilot_capture(cwd=repo_root, cli=cli)
    if not body:
        return None
    names = _request_tools(body)
    return {s: sum(1 for n in names if n and n.startswith(s + "-")) for s in servers}


def verify_codex(repo_root, capabilities, cli="codex", timeout=30):
    """What `codex features list`, run in the repository, says about the applied toggles:
    {feature: enabled} for the features those capabilities switch off, or None when the CLI
    is unavailable. Free: it reads configuration and calls no model."""
    feats = [k for c in capabilities for t, k, v in CODEX_TOGGLES.get(c, ())
             if t == "features"]
    if not feats or not shutil.which(cli):
        return None
    try:
        out = subprocess.run([cli, "features", "list"], capture_output=True,
                             encoding="utf-8", errors="replace", cwd=repo_root, timeout=timeout,
                             env=dict(os.environ, TE_SIDECALL="1"))
    except (OSError, subprocess.SubprocessError):
        return None
    state = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in feats:
            state[parts[0]] = parts[-1] == "true"
    return state or None


def _init_tools(extra_args, cli=None, timeout=60):
    """Tool list from a `claude -p` session's init event; the process is killed right after it,
    before the model is asked anything."""
    cli = cli or claude_cli()
    env = dict(os.environ, TE_SIDECALL="1")
    cmd = [cli, "-p", "Reply with OK", "--output-format", "stream-json", "--verbose",
           "--model", "haiku", "--max-turns", "1"] + claude_flags(cli) + list(extra_args)
    with tempfile.TemporaryDirectory(prefix="te-probe-", ignore_cleanup_errors=True) as cwd:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace", env=env,
                             cwd=cwd, start_new_session=os.name != "nt")
        tools, version = None, None
        deadline = time.time() + timeout
        try:
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    tools, version = ev.get("tools"), ev.get("claude_code_version")
                    break
                if time.time() > deadline:
                    break
        finally:
            _kill_tree(p)
    return tools, version


def _kill_tree(p):
    """Kill the probe and what it started. The MCP servers a session launches outlive a plain
    `kill()`, and on Windows they keep the probe's temporary directory, their cwd, in use."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    p.kill()
    p.wait()


class _Capture(BaseHTTPRequestHandler):
    """A chat-completions endpoint that records the request and answers "OK"."""
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(json.dumps({"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        self.server.captured.append(body)
        msg = {"role": "assistant", "content": "OK"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": msg, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": usage}]
            self._send("".join(f"data: {json.dumps(dict(c, object='chat.completion.chunk'))}"
                               "\n\n" for c in chunks) + "data: [DONE]\n\n",
                       "text/event-stream")
        else:
            self._send(json.dumps({"object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": msg, "finish_reason": "stop"}]}))


def copilot_capture(extra_args=(), cwd=None, cli="copilot", timeout=90):
    """The first request a fresh `copilot -p` session sends, captured on localhost.

    Copilot's BYOK mode is pointed at a scripted endpoint in this process, so the request --
    system prompt, tool schemas -- is recorded exactly as Copilot builds it, and no model,
    credential or quota is touched. Returns the request body, or None."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    srv.captured = []
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    # The person's own model, when the environment names one: Copilot shapes its built-in tools
    # by model family (`edit`/`create` for Claude models, `apply_patch` for GPT ones).
    from .sidecall import copilot_model
    # A router's `provider/` prefix (OpenRouter: `anthropic/claude-haiku-4.5`) is not part of
    # the id Copilot recognises.
    model = (copilot_model() or "gpt-5.4").split("/", 1)[-1]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COPILOT_PROVIDER_") and k != "COPILOT_MODEL"}
    env.update(TE_SIDECALL="1", COPILOT_PROVIDER_TYPE="openai",
               COPILOT_PROVIDER_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}/v1",
               COPILOT_PROVIDER_API_KEY="te-local-probe", COPILOT_MODEL=model,
               COPILOT_PROVIDER_WIRE_API="completions", NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost")
    tmp = None
    if cwd is None:
        tmp = tempfile.mkdtemp(prefix="te-probe-")
        cwd = tmp
    try:
        subprocess.run([cli, "-p", "Reply with OK", "--no-auto-update", "--no-color"]
                       + list(extra_args), cwd=cwd, env=env, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return srv.captured[0] if srv.captured else None


def _request_tools(body):
    return [(t.get("function") or {}).get("name") or t.get("name") or t.get("type")
            for t in (body or {}).get("tools") or []]


def _copilot_version(cli="copilot"):
    try:
        out = subprocess.run([cli, "--version"], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return (out.stdout or "").strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def refresh_catalog(state_root, cli=None, host="claude"):
    """Record the tool list a fresh session loads on this machine."""
    if host == "copilot":
        body = copilot_capture(cli=cli or "copilot")
        tools = [t for t in _request_tools(body) if t]
        if not tools:
            raise RuntimeError("could not capture the tool list of a `copilot -p` request")
        path = live_adapter.catalog_path(state_root, "copilot")
        _write_json(path, {"tools": sorted(tools), "copilot_version": _copilot_version(),
                           "probed_at": time.time()})
        return path, sorted(tools)
    if host != "claude":
        raise RuntimeError(f"no catalog probe for {host}")
    tools, version = _init_tools([], cli=cli)
    if not tools:
        raise RuntimeError("could not read the tool list from a `claude -p` init event")
    path = live_adapter.catalog_path(state_root, "claude")
    _write_json(path, {"tools": sorted(tools), "claude_code_version": version,
                       "probed_at": time.time()})
    return path, sorted(tools)


def _prompt_tokens(extra_args, cli=None, timeout=180):
    cli = cli or claude_cli()
    env = dict(os.environ, TE_SIDECALL="1")
    cmd = [cli, "-p", "Reply with exactly: OK", "--output-format", "json", "--model", "haiku",
           "--max-turns", "1"] + claude_flags(cli) + list(extra_args)
    # ignore_cleanup_errors: on Windows an MCP server the session started can still hold the cwd
    # when `claude` exits (WinError 32), as in `_init_tools`.
    with tempfile.TemporaryDirectory(prefix="te-measure-", ignore_cleanup_errors=True) as cwd:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", env=env,
                             cwd=cwd, timeout=timeout)
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        data = {}
    u = data.get("usage") or {}
    total = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens"))
    if out.returncode or data.get("is_error") or not total:
        # A failed probe would otherwise count as 0 tokens, and the whole request as saved.
        raise RuntimeError("`claude -p` reported no usage: "
                           + ((out.stderr or out.stdout or "").strip()[:300] or "no output"))
    return total, data.get("total_cost_usd")


def _codex_prompt_chars(overrides, cli="codex", timeout=60):
    cmd = [cli, "debug", "prompt-input"]
    for table, key, value in overrides:
        cmd += ["-c", f"{table}.{key}={value}" if table else f"{key}={value}"]
    with tempfile.TemporaryDirectory(prefix="te-measure-") as cwd:
        out = subprocess.run(cmd + ["OK"], capture_output=True, encoding="utf-8", errors="replace",
                             cwd=cwd, timeout=timeout, env=dict(os.environ, TE_SIDECALL="1"))
    return len(out.stdout or "") if out.returncode == 0 else None


def measure(tools, cli=None, host="claude"):
    """Request size with and without `tools` removed.

    Claude Code: prompt tokens of two small requests. Codex and Copilot: characters of the
    rendered request, and a ~4-characters-per-token estimate, at no cost."""
    if not tools:
        return None
    if host == "codex":
        toggles = [t for c in tools for t in CODEX_TOGGLES.get(c, ())]
        full = _codex_prompt_chars([], cli=cli or "codex")
        lean = _codex_prompt_chars(toggles, cli=cli or "codex")
        if full is None or lean is None:
            raise RuntimeError("`codex debug prompt-input` failed")
        return {"capabilities": list(tools), "prompt_input_chars_full": full,
                "prompt_input_chars_lean": lean, "saved_chars_per_request": full - lean,
                "saved_tokens_per_request_est": (full - lean) // 4,
                "note": "prompt input only (developer/user items); tool schemas not included",
                "probe_cost_usd": 0.0}
    if host == "copilot":
        args = [a for s in tools for a in ("--disable-mcp-server", s)]
        full = copilot_capture(cli=cli or "copilot")
        lean = copilot_capture(args, cli=cli or "copilot")
        if not full or not lean:
            raise RuntimeError("could not capture a `copilot -p` request")
        size = lambda b: len(json.dumps(b.get("tools"))) + len(json.dumps(b.get("messages")))
        return {"mcp_servers": list(tools),
                "tools_full": len(_request_tools(full)), "tools_lean": len(_request_tools(lean)),
                "request_chars_full": size(full), "request_chars_lean": size(lean),
                "saved_chars_per_request": size(full) - size(lean),
                "saved_tokens_per_request_est": (size(full) - size(lean)) // 4,
                "probe_cost_usd": 0.0}
    cli = cli or claude_cli()
    full, c1 = _prompt_tokens([], cli=cli)
    # The deny list as a settings file, not `--disallowed-tools` on argv: hundreds of names
    # overflow cmd.exe's 8191-character line when `claude` is the npm `claude.cmd` shim. Bare-name
    # deny rules remove the same tools (see `denylist`), and are what `toolprune apply` writes.
    with tempfile.TemporaryDirectory(prefix="te-deny-", ignore_cleanup_errors=True) as d:
        settings = os.path.join(d, "settings.json")
        _write_json(settings, {"permissions": {"deny": list(tools)}})
        lean, c2 = _prompt_tokens(["--settings", settings], cli=cli)
    return {"tools": list(tools), "prompt_tokens_full": full, "prompt_tokens_lean": lean,
            "saved_per_request": full - lean,
            "probe_cost_usd": round((c1 or 0) + (c2 or 0), 4)}
