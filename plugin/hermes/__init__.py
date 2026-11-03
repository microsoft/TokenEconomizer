"""TokenEconomizer for Hermes Agent (NousResearch/hermes-agent).

Hermes's plugins are Python functions on its hooks, in the gateway process that runs every
session, so this plugin speaks the hook protocol the other agents use on Hermes's behalf: each
event below becomes one call of the shared dispatcher (`../scripts/te_bootstrap.py hook --host
hermes`, JSON in, JSON out), in a process of its own, and the answer is applied with Hermes's
levers. All of the mechanism logic lives in the dispatcher; this file only translates.

    prompt section callback   -> SessionStart       the learned facts, in the system prompt; Hermes
                                                    freezes them for the session, and builds this
                                                    section BEFORE it fires `on_session_start`
    pre_llm_call              -> UserPromptSubmit   opens a prompt-turn; on a resumed session, whose
                                                    prompt is restored and which fires neither of
                                                    the above, first a SessionStart
    transform_tool_result     -> PostToolUse        the guard; the only tool hook whose answer the
                                                    model reads. It runs before `post_tool_call`
    on_session_end            -> Stop               fires after every TURN despite its name; a
                                                    turn cut short sends no Stop, so it cannot feed
                                                    the envelope
    on_session_end (one-shot),
    on_session_finalize       -> SessionEnd         a detached fold. `hermes -z` sends no finalize,
                                                    nor does a killed session: the next session's
                                                    start folds it once its hooks have gone quiet.
                                                    The fold reads the session from `state.db`

One gateway serves every session and every repository, and its own directory and environment
belong to neither, so everything about a session is read from its row in `state.db`, per session.

The plugin follows only sessions a person started in a directory on this machine: `created_source`
`cli` or `oneshot`, with a `cwd`. That is also what keeps it out of its own side calls, which run
in this same gateway as `--source tool` and which `TE_SIDECALL` cannot reach; a session whose row
cannot be read is not followed. Subagents (`subagent`), messaging platforms, cron and the other
surfaces are not followed either.

Hermes runs hooks under `plugins.hook_callback_timeout` (30s), and one overrun silences that
callback for 60s for every session, so a dispatcher call has a shorter limit of its own. Every
failure, here or there, means no change. It does nothing under TE_SIDECALL=1 or TE_DISABLE=1.
"""
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.realpath(__file__))
BOOTSTRAP = os.path.join(HERE, "..", "scripts", "te_bootstrap.py")
USER_SOURCES = ("cli", "oneshot")
HOOK_TIMEOUT_S = 20
LOCK_TIMEOUT_S = 5
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
SECTION_ID = "tokeneconomizer.facts"
# A gateway runs for weeks. An id forgotten is only looked up again, and `hermes -z` never says it
# has finished with a session, so these are capped rather than cleared.
MAX_IGNORED = 4096
MAX_ENDED = 4096

# A redirect goes first in the tool's JSON: the result stays the object Hermes and the model expect
# (appended text made Hermes read a failed call as `ok`), and a result too long for the context is
# cut from its end.
REDIRECT_FIELD = "tokeneconomizer_redirect"
# Hermes wraps these tools' results as untrusted data, which the model is told not to obey
# (agent/tool_dispatch_helpers.py); `tool_call` may be any of them. A redirect waits for a result
# outside the wrapper.
UNTRUSTED_TOOLS = ("web_extract", "web_search", "tool_call")
UNTRUSTED_PREFIXES = ("browser_", "mcp_")


def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def disabled():
    return _truthy(os.environ.get("TE_SIDECALL")) or _truthy(os.environ.get("TE_DISABLE"))


def hermes_home():
    """This profile's home. One gateway can serve several profiles, and Hermes binds the running
    hook's, so it is asked on each call."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def state_root():
    return os.path.abspath(os.environ.get("TOKENECONOMIZER_HOME")
                           or os.path.join(os.path.expanduser("~"), ".tokeneconomizer"))


def dispatch(payload, cwd, mode, home):
    """One dispatcher call; {} (no change) on any failure. Python is the one running this file,
    which Hermes needs to be new enough, unless TE_PYTHON names another. It runs in the session's
    directory: the gateway's own may no longer exist."""
    if disabled():
        return {}
    env = dict(os.environ, PYTHONUTF8="1", TE_HERMES_MODE=mode, HERMES_HOME=home)
    try:
        out = subprocess.run([os.environ.get("TE_PYTHON") or sys.executable, BOOTSTRAP, "hook",
                              "--host", "hermes"],
                             input=json.dumps(payload), capture_output=True, encoding="utf-8",
                             errors="replace", timeout=HOOK_TIMEOUT_S, cwd=cwd, env=env, **HIDDEN)
        answer = json.loads((out.stdout or "").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {}
    return answer if isinstance(answer, dict) else {}


def _connect(home):
    uri = pathlib.Path(home, "state.db").as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def _row(con, session_id):
    row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def lineage(con, session_id):
    """The session's rows, oldest first. Compression can end a session and go on in a child, with
    a new id, in the middle of a turn: to the plugin those are one session, named by the first."""
    rows, seen = [], set()
    row = _row(con, session_id)
    while row and row["id"] not in seen:
        seen.add(row["id"])
        rows.insert(0, row)
        parent = _row(con, row["parent_session_id"]) if row.get("parent_session_id") else None
        row = parent if parent and parent.get("end_reason") == "compression" else None
    return rows


def with_redirect(result, text):
    """`result` carrying `text`, or None when it cannot without losing its shape. A JSON object
    keeps every byte it had, after a first key of ours; plain text gets the line after it."""
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return f"{result}\n\n{text}"
    if not isinstance(parsed, dict):
        return None                               # a JSON list or scalar has no place for it
    rest = result.lstrip()[1:].lstrip()
    field = f"{{{json.dumps(REDIRECT_FIELD)}: {json.dumps(text)}"
    return field + "}" if rest.startswith("}") else f"{field}, {rest}"


def untrusted(tool_name):
    return tool_name in UNTRUSTED_TOOLS or str(tool_name).startswith(UNTRUSTED_PREFIXES)


class Bridge:
    """What the plugin keeps about sessions in the gateway's memory, keyed by the lineage's first
    id. A gateway restart loses it, and the next hook finds each session again."""

    def __init__(self, call=dispatch, home=hermes_home):
        self.call = call
        self.home = home
        self.sessions = {}                     # any id of a followed session -> its record
        self.ignored = set()
        self.guard = threading.Lock()

    def _forget_ended(self):
        """Drop the oldest records of sessions that have ended, once they pass the cap. A record
        kept is what gives a `chat --oneshot` its notices at finalize, which follows its end."""
        ended = [k for k, v in self.sessions.items() if not v["started"]]
        for key in ended[:max(0, len(ended) - MAX_ENDED)]:
            del self.sessions[key]

    def _record(self, row, root):
        return {"id": root, "cwd": row["cwd"], "lock": threading.Lock(), "started": False,
                "mode": "oneshot" if (row.get("created_source") or row.get("source")) == "oneshot"
                        else "cli", "held": [], "notices": []}

    def session(self, session_id):
        """The record of a session the plugin follows, or None. A row that cannot be read is
        retried at the next hook: following it now could be following a side call."""
        if not session_id or disabled():
            return None
        with self.guard:
            found = self.sessions.get(session_id)
            if found is not None or session_id in self.ignored:
                return found
        try:
            con = _connect(self.home())
            try:
                chain = lineage(con, session_id)
            finally:
                con.close()
        except (OSError, ValueError, sqlite3.Error):
            return None
        if not chain:
            return None
        leaf, root = chain[-1], chain[0]["id"]
        origin = chain[0].get("created_source") or chain[0].get("source")
        cwd = leaf.get("cwd") or chain[0].get("cwd")
        with self.guard:
            if origin not in USER_SOURCES or not cwd or not os.path.isdir(cwd):
                if len(self.ignored) >= MAX_IGNORED:
                    self.ignored.clear()
                self.ignored.add(session_id)
                return None
            found = self.sessions.get(root)
            if found is None:
                found = self._record(dict(chain[0], cwd=cwd, created_source=origin), root)
                self._forget_ended()
                self.sessions[root] = found
            self.sessions[session_id] = found
        return found

    def _payload(self, rec, event, **fields):
        return dict(fields, session_id=rec["id"], cwd=rec["cwd"], hook_event_name=event,
                    transcript_path=os.path.join(state_root(), "hermes-sessions",
                                                 f"{rec['id']}.json"))

    def _send(self, rec, event, **fields):
        out = self.call(self._payload(rec, event, **fields), rec["cwd"], rec["mode"], self.home())
        notice = out.get("systemMessage")
        if isinstance(notice, str) and notice:
            logger.info("%s", notice)
            rec["notices"].append(notice)
        return out

    def _start(self, rec, source):
        rec["started"] = True
        out = self._send(rec, "SessionStart", source=source)
        return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""

    # -- Hermes's hooks ----------------------------------------------------------------------
    def section(self, info):
        rec = self.session((info or {}).get("session_id"))
        if rec is None:
            return ""
        with rec["lock"]:
            return self._start(rec, "startup") if not rec["started"] else ""

    def pre_llm_call(self, session_id="", user_message="", turn_id="", **_):
        rec = self.session(session_id)
        if rec is None:
            return None
        if not rec["lock"].acquire(timeout=LOCK_TIMEOUT_S):
            return None
        try:
            rec["held"] = []                      # a redirect belongs to the turn it was written in
            if not rec["started"]:
                self._start(rec, "resume")        # Hermes restored the facts with the prompt
            prompt = user_message if isinstance(user_message, str) else "\n".join(
                p.get("text", "") for p in user_message or [] if isinstance(p, dict))
            out = self._send(rec, "UserPromptSubmit", prompt_id=str(turn_id), prompt=prompt)
        finally:
            rec["lock"].release()
        extra = (out.get("hookSpecificOutput") or {}).get("additionalContext")
        return {"context": extra} if extra else None

    def transform_tool_result(self, tool_name="", args=None, result=None, session_id="",
                              turn_id="", tool_call_id="", status=None, error_message=None, **_):
        rec = self.session(session_id)
        if rec is None or not isinstance(result, str):
            return None
        # A model's parallel calls arrive together, and a session's guard state is read, changed
        # and written back by each.
        if not rec["lock"].acquire(timeout=LOCK_TIMEOUT_S):
            return None
        try:
            out = self._send(rec, "PostToolUse", prompt_id=str(turn_id), tool_name=tool_name,
                             tool_use_id=tool_call_id, tool_input=args or {}, tool_response={
                                 "content": result, "status": status, "error": error_message})
            text = (out.get("hookSpecificOutput") or {}).get("additionalContext")
            if text:
                rec["held"].append(text)
            if not rec["held"] or untrusted(tool_name):
                return None
            answer = with_redirect(result, "\n\n".join(rec["held"]))
            if answer is not None:
                rec["held"] = []
            return answer
        finally:
            rec["lock"].release()

    def on_session_end(self, session_id="", turn_id="", completed=False, interrupted=False,
                       failed=False, **_):
        rec = self.session(session_id)
        if rec is None or not rec["lock"].acquire(timeout=LOCK_TIMEOUT_S):
            return
        try:
            if completed and not interrupted and not failed:
                self._send(rec, "Stop", prompt_id=str(turn_id), stop_hook_active=False)
            if rec["mode"] == "oneshot":
                # `hermes -z` sends no `on_session_finalize`.
                self._end(rec, "oneshot")
        finally:
            rec["lock"].release()

    def _end(self, rec, reason):
        if rec["started"]:
            rec["started"] = False
            self._send(rec, "SessionEnd", reason=reason)

    def on_session_finalize(self, session_id=None, reason="", **_):
        rec = self.session(session_id)
        if rec is None:
            return None
        with rec["lock"]:
            self._end(rec, str(reason or "finalize"))
            notices, rec["notices"] = rec["notices"], []
        with self.guard:
            for key in [k for k, v in self.sessions.items() if v is rec]:
                del self.sessions[key]            # a resumed session is found again
        return "\n".join(notices) or None


def register(ctx):
    bridge = Bridge()
    ctx.register_system_prompt_section(SECTION_ID, bridge.section, position="after_memory",
                                       max_chars=4000)
    ctx.register_hook("pre_llm_call", bridge.pre_llm_call)
    ctx.register_hook("transform_tool_result", bridge.transform_tool_result)
    ctx.register_hook("on_session_end", bridge.on_session_end)
    ctx.register_hook("on_session_finalize", bridge.on_session_finalize)
