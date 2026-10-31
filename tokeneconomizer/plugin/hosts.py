"""Host adapters: one per coding agent whose hooks the live controller runs under.

Claude Code and Codex speak the same hook contract -- JSON on stdin with `session_id`,
`transcript_path`, `cwd`, `hook_event_name`, and `tool_name` / `tool_input` / `tool_response` per
tool call; JSON on stdout with `hookSpecificOutput.additionalContext`, `systemMessage`, and
`continue` / `stopReason`. The differences that matter here are few, and each is an attribute of
an adapter rather than a branch in a mechanism:

  * Turn identity. Claude Code stamps every event of a prompt-turn with `prompt_id`; Codex
    stamps `turn_id`; Cursor gives one `generation_id` per prompt.
  * Stopping. The plugin never stops a turn: the guard's last rung, on every agent and in every
    mode, is a notice telling the person how to (`stop_hint`). Claude Code's `{"continue":
    false}` from PostToolUse would halt its loop (verified on 2.1.286), but Codex's replaces the
    tool result and the model carries on, and Copilot's runs every call; the person decides.
  * Tool results. Claude Code's Bash `tool_response` is `{stdout, stderr, interrupted, ...}` with
    no exit code (verified); Codex's command items carry one, and it is used when present.
  * The session log. Claude Code's transcript is read as-is; Codex's rollout is rewritten into
    exec-stream lines first (`trace.codex_rollout_to_exec`).

The GitHub Copilot CLI runs the same Claude-format hook file (PascalCase events, Claude-shaped
snake_case payloads), with differences each verified on 1.0.83 against a local scripted model
endpoint (docs/PLUGIN.md §3):

  * Context goes in a TOP-LEVEL `additionalContext`; `hookSpecificOutput` is ignored.
  * There is no turn identity and no `transcript_path` before Stop; the session log is always
    `<COPILOT_HOME>/session-state/<session_id>/events.jsonl`, which the trace seam already reads.
  * Tool names are Claude's (`Bash`, `Read`); a shell result is `tool_result.
    text_result_for_llm` ending in `<shellId: N completed with exit code X>`, and a failed tool
    fires `PostToolUseFailure` with an `error` string.
  * `continue: false` does not stop a turn (three of three calls ran).
  * `systemMessage` is never shown. What the TUI does show is a stdout line
    `{"type": "progress", "message": ...}` printed BEFORE the hook's final JSON object, which
    still applies (verified on SessionStart, UserPromptSubmit and PostToolUse). So a Copilot
    notice is carried as `PROGRESS` lines that `hook.main` writes ahead of the output.

`detect` picks the adapter from the payload: Codex's `turn_id`, or a transcript under `.codex/`
named `rollout-*`; Copilot's session-state transcript, or no transcript at all inside a Copilot
hook process; Claude Code otherwise. Each plugin hook file except Claude Code's also passes
`--host` explicitly, so detection is the fallback rather than the rule.
"""
import json
import os
import re
import shlex
import time

from .. import trace

_RESPONSE_KEYS = ("stdout", "stderr", "output", "aggregated_output", "formatted_output",
                  "content", "result", "error", "message", "text_result_for_llm")
_EXIT_RE = re.compile(r"(?im)^\s*(?:exit code|process exited with code)[:\s]+(-?\d+)")
# Copilot appends this line to every shell result.
_COPILOT_EXIT_RE = re.compile(r"<shellId: [^>]*?completed with exit code (-?\d+)>")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
RESULT_CAP = 20000      # characters of a tool result the guard folds (errors live early)
# Notices a host shows as progress lines ahead of the hook's JSON (Copilot); `hook.main` writes
# them and removes the key, so it never reaches an agent as an output field.
PROGRESS = "_progress"


def response_text(resp, cap=RESULT_CAP):
    """Best-effort text of a hook's `tool_response`, whatever shape the tool returned."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp[:cap]
    if isinstance(resp, list):
        return " ".join(x.get("text", "") if isinstance(x, dict) else str(x)
                        for x in resp)[:cap]
    if isinstance(resp, dict):
        parts = []
        for k in _RESPONSE_KEYS:
            v = resp.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, list):
                parts.append(" ".join(x.get("text", "") for x in v if isinstance(x, dict)))
        if parts:
            return "\n".join(parts)[:cap]
        return json.dumps(resp, sort_keys=True)[:cap]
    return str(resp)[:cap]


def merge_outputs(*outputs):
    """Combine hook outputs: contexts and notices concatenate, the last stop decision wins."""
    out = {}
    for o in outputs:
        if not o:
            continue
        for k, v in o.items():
            if k == "systemMessage" and out.get(k):
                out[k] = out[k] + "\n" + v
            elif k == "additionalContext" and out.get(k):
                out[k] = out[k] + "\n\n" + v
            elif k == "additional_context" and out.get(k):
                out[k] = out[k] + "\n\n" + v
            elif k == PROGRESS:
                out[k] = list(out.get(k) or []) + list(v)
            elif k == "hookSpecificOutput" and out.get(k):
                a = out[k].get("additionalContext") or ""
                b = v.get("additionalContext") or ""
                out[k] = dict(out[k], **v)
                if a and b:
                    out[k]["additionalContext"] = a + "\n\n" + b
            else:
                out[k] = v
    return out


class Host:
    name = "?"
    # How a person stops a turn, for the loop guard's notice: in the agent's own UI, and in a
    # headless run (one prompt, so stopping the turn ends the run). Each agent's are verified
    # in its own terminal UI and headless mode (2026-10-21).
    interrupt = "Interrupt the agent to stop it."
    end_run = "Press Ctrl+C to end the run."
    # Whether context returned from UserPromptSubmit reaches the model (verified per host).
    prompt_context = False

    def session_id(self, p):
        return str(p.get("session_id") or "")

    def transcript(self, p):
        return p.get("transcript_path") or ""

    def cwd(self, p, env):
        return p.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    def env_session_id(self, env):
        """The session a `te` command runs inside, from its shell's environment ("" if none is
        named). `TE_SESSION_ID` is set by the Pi extension's `/te`."""
        return str(env.get("TE_SESSION_ID") or "")

    def turn_key(self, p):
        return ""

    def is_subagent(self, p):
        return bool(p.get("agent_id"))

    def context(self, event, text):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def notice(self, text):
        return {"systemMessage": text}

    def stop_hint(self, headless):
        return self.end_run if headless else self.interrupt

    # Whether the loop guard's still-stuck notice must also go through the model, because the
    # agent does not put hook messages where the person looks.
    relays_notices = False

    # Whether a resumed conversation still carries the context its first SessionStart added, so
    # the plugin need not add it again (Claude Code 2.1.307 and Codex 0.163.0 replay it).
    keeps_start_context = True

    def relay(self, text):
        """What the model is asked, so that it passes `text` on to the person, word for word.
        The notice is the person's to act on: it does not change the model's task."""
        return ("TokenEconomizer, a plugin the user runs, has a notice for them, and their main "
                "view does not show plugin messages. Put this line, word for word, in your next "
                f"message to the user, and again in your final reply:\n\n{text}\n\nIt is for "
                "the user to act on; it does not change your task.")

    def headless(self, env, transcript=None):
        return False

    def tool_call(self, p):
        """(tool_name, args, result_text, exit_code_or_None) for a PostToolUse payload, or for
        a PostToolUseFailure one: Claude Code reports a call that failed -- a non-zero exit, a
        refused command, a file that is not there -- only there, with an `error` string
        ("Exit code 2\nls: cannot access ...") and no `tool_response` (2.1.299)."""
        args = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        resp = _failure_response(p) or p.get("tool_response")
        return str(p.get("tool_name") or ""), args, response_text(resp), _exit_code(resp)

    # -- the finished session, in the shapes the mechanisms read --------------------------
    def mechanism_trace(self, transcript_path, workdir):
        """Path of a trace `trace.load_trajectory` reads, for the finished session."""
        return transcript_path if transcript_path and os.path.exists(transcript_path) else None

    def session_usage(self, transcript_path):
        raise NotImplementedError

    def prompt_count(self, transcript_path):
        """How many prompts the finished session ran; None where the log does not say."""
        return None

    def settle(self, transcript_path):
        """Wait, if this host needs to, until the finished session's log is complete."""

    def tool_usage(self, trace_path):
        return trace.tool_usage(trace_path)


def _failure_response(p):
    """A PostToolUseFailure payload's `error` as a response; None for any other payload. A
    shell failure names its exit code ("Exit code 2"); other failures carry none."""
    if p.get("tool_response") is not None or p.get("tool_result") is not None or \
            p.get("error") is None:
        return None
    text = str(p.get("error"))
    m = _EXIT_RE.search(text[:400])
    return dict({"error": text, "result_type": "failure"},
                **({"exit_code": int(m.group(1))} if m else {}))


def _exit_code(resp):
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "returncode", "status_code"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        meta = resp.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
            return meta["exit_code"]
    text = resp if isinstance(resp, str) else next(
        (resp.get(k) for k in ("output", "text_result_for_llm")
         if isinstance(resp, dict) and isinstance(resp.get(k), str)), None)
    if text:
        m = _EXIT_RE.search(text[:400])
        if m:
            return int(m.group(1))
        m = _COPILOT_EXIT_RE.search(text[-400:])
        if m:
            return int(m.group(1))
    return None


class ClaudeCode(Host):
    name = "claude"
    interrupt = "Press Esc to interrupt it."       # 2.1.306: the running command is killed too
    # 2.1.306 shows a hook's `systemMessage` only in the detailed transcript (Ctrl+O), and
    # `claude -p` prints it not at all; Claude's own reply is in the main view and in the output.
    relays_notices = True
    prompt_context = True

    def turn_key(self, p):
        return str(p.get("prompt_id") or "")

    def env_session_id(self, env):
        # Claude Code exports its session id to the Bash tool's shell (seen on 2.1.304).
        return str(env.get("TE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID") or "")

    def headless(self, env, transcript=None):
        # Observed in a `claude -p` session's hook environment (2.1.285):
        # CLAUDE_CODE_SESSION_ATTENDED=0 and CLAUDE_CODE_ENTRYPOINT=sdk-cli. The attended flag
        # says what matters directly -- is a person watching -- so it wins when present.
        attended = str(env.get("CLAUDE_CODE_SESSION_ATTENDED", "")).strip()
        if attended in ("0", "1"):
            return attended == "0"
        return str(env.get("CLAUDE_CODE_ENTRYPOINT", "")).startswith("sdk")

    def session_usage(self, transcript_path):
        return trace.claude_session_usage(transcript_path)

    def prompt_count(self, transcript_path):
        return trace.claude_prompt_count(transcript_path)

    def tool_usage(self, trace_path):
        return trace.claude_tool_usage(os.path.realpath(trace_path))


_CODEX_SHELL = {"bash", "shell", "exec_command", "local_shell", "unified_exec",
                "container.exec", "exec"}


class Codex(Host):
    name = "codex"
    # 0.163.0: Esc interrupts the turn, but a command it started keeps running as a "background
    # terminal" until `/stop`. (`codex exec` prints no hook messages, so its notice goes unseen.)
    interrupt = "Press Esc to interrupt it, then /stop to end a command it left running."

    def turn_key(self, p):
        return str(p.get("turn_id") or "")

    def tool_call(self, p):
        name, args, text, code = super().tool_call(p)
        low = name.lower()
        if low in _CODEX_SHELL:
            cmd = args.get("command", args.get("cmd", ""))
            if isinstance(cmd, list):
                cmd = shlex.join(str(c) for c in cmd)
            return "bash", {"command": str(cmd or "")}, text, code
        if low == "apply_patch":
            patch = next((v for v in (args.get("input"), args.get("patch"), args.get("command"))
                          if isinstance(v, str)), "")
            paths = _PATCH_FILE_RE.findall(patch)
            return "apply_patch", {"path": paths[0] if paths else "", "paths": paths}, text, code
        return name, args, text, code

    def headless(self, env, transcript=None):
        # The rollout's first record names its front end: `source: "exec"`, originator
        # `codex_exec` under `codex exec`; `codex-tui` in the terminal UI (0.163.0). The hook's
        # payload and environment are the same in both.
        try:
            with open(transcript or "", encoding="utf-8") as fh:
                meta = (json.loads(fh.readline()) or {}).get("payload") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return meta.get("source") == "exec" or str(meta.get("originator", "")) == "codex_exec"

    def mechanism_trace(self, transcript_path, workdir):
        out = os.path.join(workdir, "codex_exec_trace.jsonl")
        n = trace.codex_rollout_to_exec(transcript_path, out)
        return out if n is not None else None

    def session_usage(self, transcript_path):
        return trace.codex_session_usage(transcript_path)

    def prompt_count(self, transcript_path):
        return trace.codex_prompt_count(transcript_path)


class Pi(Host):
    """Pi (earendil-works/pi), through the extension in `plugin/pi/`.

    Pi has no hook protocol of its own -- extensions are TypeScript functions on its event
    bus -- so the extension speaks THIS one on Pi's behalf: it calls the same dispatcher with
    a Claude-shaped payload per event and applies the answer with Pi's own levers (a system
    prompt section, a steering message, a UI notice). What it sends:

      * `prompt_id`, which it numbers itself, for the turn identity Pi does not stamp;
      * `tool_response = {content, isError, exit_code}` -- Pi's bash result carries a real
        exit code in its structured content, which the ended-green proxy uses;
      * Pi's mode in `TE_PI_MODE` on the hook's environment: print / json / rpc are headless.
    """
    name = "pi"
    prompt_context = True              # the extension appends it to the session's section
    # 1.3.0: Esc aborts the turn and its command; Ctrl+C ends `pi -p`, but a command it started
    # runs on (detached) until it finishes.
    interrupt = "Press Esc to abort it."
    end_run = "Press Ctrl+C to end the run; a command it started runs on until it finishes."

    def turn_key(self, p):
        return str(p.get("prompt_id") or "")

    def headless(self, env, transcript=None):
        return str(env.get("TE_PI_MODE", "")).strip() in ("print", "json", "rpc")

    def mechanism_trace(self, transcript_path, workdir):
        out = os.path.join(workdir, "pi_trace.jsonl")
        n = trace.pi_session_to_claude(transcript_path, out)
        return out if n is not None else None

    def prompt_count(self, transcript_path):
        return trace.pi_prompt_count(transcript_path)

    def session_usage(self, transcript_path):
        return trace.pi_session_usage(transcript_path)


class OpenCode(Host):
    """opencode (anomalyco/opencode), through the plugin in `plugin/opencode/`.

    opencode has no hook protocol of its own -- plugins are TypeScript functions on its hook and
    event bus (1.x: hook functions; 2.x: `setup` and domain hooks) -- so the plugin speaks THIS
    one on opencode's behalf, as the Pi extension does: it
    calls the same dispatcher with a Claude-shaped payload per event and applies the answer with
    opencode's own levers (a system-prompt entry, a message into the session, a toast). What it
    sends:

      * `prompt_id`, which it numbers itself, for the turn identity opencode does not stamp;
      * `transcript_path`, the session's export as JSON, written at the end of every turn (from
        `opencode export` on 1.x; recorded by the plugin from its events on 2.x, which has no
        export), since opencode's own database is not a format to read;
      * `tool_response = {content, isError, exit_code}`, with `exit_code` from the bash tool's
        `metadata.exit` when it reports one;
      * opencode's mode in `TE_OPENCODE_MODE`: `run` (headless, `opencode run`) or `tui`.
    """
    name = "opencode"
    prompt_context = True              # the plugin appends it to the system prompt
    interrupt = "Press Esc to interrupt it."
    end_run = "Press Ctrl+C to end the run."

    def turn_key(self, p):
        return str(p.get("prompt_id") or "")

    def headless(self, env, transcript=None):
        return str(env.get("TE_OPENCODE_MODE", "")).strip() == "run"

    def mechanism_trace(self, transcript_path, workdir):
        out = os.path.join(workdir, "opencode_trace.jsonl")
        n = trace.opencode_session_to_claude(transcript_path, out)
        return out if n is not None else None

    def prompt_count(self, transcript_path):
        return trace.opencode_prompt_count(transcript_path)

    def session_usage(self, transcript_path):
        return trace.opencode_session_usage(transcript_path)


class Cursor(Host):
    """Cursor Agent hooks, translated to the shared live-controller contract.

    Cursor's JSONL transcript records prompts and tool requests, but not tool results or token
    usage. The adapter therefore keeps only the hook events TokenEconomizer needs in its private
    state directory and reconstructs a Claude-shaped trace from those events. Session economics
    remain unpriced until Cursor exposes per-session usage in a supported interface.
    """
    name = "cursor"
    prompt_context = False
    relays_notices = True
    interrupt = "Press Esc to stop the Cursor agent."
    end_run = "Press Ctrl+C to end the Cursor Agent CLI run."

    _EVENTS = {
        "sessionStart": "SessionStart",
        "beforeSubmitPrompt": "UserPromptSubmit",
        "postToolUse": "PostToolUse",
        "postToolUseFailure": "PostToolUseFailure",
        "stop": "Stop",
        "sessionEnd": "SessionEnd",
    }

    def session_id(self, p):
        return str(p.get("session_id") or p.get("conversation_id") or "")

    def cwd(self, p, env):
        roots = p.get("workspace_roots")
        root = roots[0] if isinstance(roots, list) and roots else ""
        return p.get("cwd") or env.get("CURSOR_PROJECT_DIR") or root or os.getcwd()

    def turn_key(self, p):
        return str(p.get("generation_id") or p.get("prompt_id") or "")

    def context(self, event, text):
        return {"additional_context": text}

    def notice(self, text):
        return {"additional_context": self.relay(text)}

    def transcript(self, p):
        if p.get("transcript_path"):
            return p["transcript_path"]
        sid = self.session_id(p)
        if not sid:
            return ""
        from .paths import state_root
        safe_id = os.path.basename(sid)
        return os.path.join(state_root(), "cursor-sessions", safe_id + ".jsonl")

    def prepare(self, payload, env):
        p = dict(payload or {})
        p["session_id"] = p.get("conversation_id") or p.get("session_id") or ""
        p["cwd"] = (p.get("cwd") or env.get("CURSOR_PROJECT_DIR") or
                    ((p.get("workspace_roots") or [""])[0]) or os.getcwd())
        p["hook_event_name"] = self._EVENTS.get(p.get("hook_event_name"), "")
        if p.get("session_id"):
            from .paths import state_root
            p["transcript_path"] = os.path.join(
                state_root(env), "cursor-sessions", os.path.basename(p["session_id"]) + ".jsonl")
        if p["hook_event_name"] == "SessionStart":
            p["source"] = "startup"
        if p["hook_event_name"] == "UserPromptSubmit":
            p["prompt_id"] = p.get("generation_id") or ""
        if p["hook_event_name"] in ("PostToolUse", "PostToolUseFailure"):
            p["tool_use_id"] = p.get("tool_use_id") or p.get("generation_id") or ""
            if p["hook_event_name"] == "PostToolUse":
                raw = p.get("tool_output")
                try:
                    output = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    output = raw
                if isinstance(output, dict):
                    p["tool_response"] = {
                        "content": "\n".join(str(output[k]) for k in ("stdout", "stderr", "output")
                                             if output.get(k) is not None),
                        "exit_code": _exit_code(output),
                        "isError": bool(output.get("isError") or output.get("is_error")),
                    }
                else:
                    p["tool_response"] = {"content": str(output or "")}
            else:
                p["error"] = p.get("error_message") or p.get("error") or "tool failed"
                if p.get("failure_type") == "permission_denied":
                    p["error"] = "Permission denied: " + str(p["error"])
        return p

    def record_event(self, payload):
        event = payload.get("hook_event_name")
        if event not in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
            return
        path = self.transcript(payload)
        if not path:
            return
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def mechanism_trace(self, transcript_path, workdir):
        out = os.path.join(workdir, "cursor_trace.jsonl")
        n = trace.cursor_events_to_claude(transcript_path, out)
        return out if n is not None else None

    def prompt_count(self, transcript_path):
        return trace.cursor_prompt_count(transcript_path)

    def session_usage(self, transcript_path):
        return {}

    def tool_call(self, p):
        name, args, text, code = super().tool_call(p)
        if name.lower() == "shell":
            return "Bash", args, text, code
        return name, args, text, code


def copilot_home(env=None):
    env = os.environ if env is None else env
    return env.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")


class Copilot(Host):
    """The GitHub Copilot CLI, through `plugin/.github/plugin/plugin.json` and its hook file."""
    name = "copilot"
    interrupt = "Press Esc twice to interrupt it."  # 1.0.101: "esc esc interrupt"
    prompt_context = True              # verified: top-level additionalContext
    # 1.0.102 rebuilds a resumed conversation's first message without it, dropping the facts
    # and missing the prompt cache; the same text given again rebuilds it as it was.
    keeps_start_context = False

    def transcript(self, p):
        # Only Stop carries `transcript_path`; the log's location is fixed by the session id.
        sid = self.session_id(p)
        return p.get("transcript_path") or (
            os.path.join(copilot_home(), "session-state", sid, "events.jsonl") if sid else "")

    def cwd(self, p, env):
        return p.get("cwd") or env.get("COPILOT_PROJECT_DIR") or os.getcwd()

    def context(self, event, text):
        return {"additionalContext": text}

    def notice(self, text):
        return {PROGRESS: [text]}

    def headless(self, env, transcript=None):
        # `copilot -p` hands its hooks the same payload, environment and session log as the
        # terminal UI (1.0.101); only its command line differs, and the hook is its child. Asked
        # only when a notice is written, so the `ps` calls cost nothing on an ordinary call.
        return any(_PROMPT_FLAG.search(a) for a in _ancestor_args()
                   if "copilot" in a.split(" ", 1)[0])

    def tool_call(self, p):
        args = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        resp = _failure_response(p) or p.get("tool_result")
        return str(p.get("tool_name") or ""), args, response_text(resp), _exit_code(resp)

    def session_usage(self, transcript_path):
        return trace.copilot_session_usage(transcript_path)

    def prompt_count(self, transcript_path):
        return trace.copilot_prompt_count(transcript_path)

    def settle(self, transcript_path, wait_s=15.0, fresh_s=60.0):
        # Copilot writes `session.shutdown` -- the event that carries usage -- just AFTER the
        # SessionEnd hook returns (observed order: hook.end, then session.shutdown), so the
        # detached fold can arrive first. Wait for it while the log is still fresh; an old
        # log without one belongs to a killed session and will never get one.
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                if time.time() - os.path.getmtime(transcript_path) > fresh_s:
                    return
                with open(transcript_path, "rb") as fh:
                    fh.seek(max(0, os.path.getsize(transcript_path) - 65536))
                    if b'"session.shutdown"' in fh.read():
                        return
            except OSError:
                return
            time.sleep(0.25)


_PROMPT_FLAG = re.compile(r"\s(?:-p|--prompt)(?:[\s=]|$)")


def _ancestor_args(limit=4):
    """The command lines of this process's nearest ancestors, nearest first."""
    import subprocess
    out, pid = [], os.getppid()
    if os.name == "nt":
        return out  # no `ps` there; Copilot's `-p` is then not told apart (a notice is kept)
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            line = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        ppid, _, args = line.partition(" ")
        if not args:
            break
        out.append(args.strip())
        pid = int(ppid) if ppid.strip().isdigit() else 0
    return out


HOSTS = {"claude": ClaudeCode, "codex": Codex, "copilot": Copilot, "opencode": OpenCode,
         "pi": Pi, "cursor": Cursor}


def default_host(env=None, recent=None):
    """The host a `te` command run without `--host` is about: `TE_HOST`, else the agent whose
    shell it runs in (Claude Code marks its shell `CLAUDECODE=1`, Copilot `COPILOT_CLI=1`),
    else -- a plain terminal -- `recent`, the agent that last worked in this repository (see
    `cli`), else Claude Code."""
    env = os.environ if env is None else env
    if env.get("TE_HOST") in HOSTS:
        return env["TE_HOST"]
    if env.get("CLAUDECODE") == "1":
        return "claude"
    if env.get("COPILOT_CLI") == "1":
        return "copilot"
    if env.get("CURSOR_AGENT") or env.get("CURSOR_PROJECT_DIR"):
        return "cursor"
    return recent if recent in HOSTS else "claude"


def detect(payload, env=None, hint=None):
    """The adapter for this hook call: an explicit `hint`, else inferred from the payload."""
    if hint in HOSTS:
        return HOSTS[hint]()
    if "turn_id" in (payload or {}):
        return Codex()
    tp = str((payload or {}).get("transcript_path") or "")
    sp = tp.replace("\\", "/")              # a Windows path, with either separator
    if sp.rsplit("/", 1)[-1].startswith("rollout-") or "/.codex/" in sp:
        return Codex()
    if "/session-state/" in sp and tp.endswith("events.jsonl"):
        return Copilot()
    # Claude Code always sends a transcript path; Copilot sends none before Stop, and marks its
    # hook processes. (Hooks inherit the parent's environment, so the marker alone would also
    # tag a Claude Code session started from inside Copilot -- hence the transcript test first.)
    if not tp and (env or {}).get("COPILOT_PLUGIN_ROOT"):
        return Copilot()
    return ClaudeCode()
