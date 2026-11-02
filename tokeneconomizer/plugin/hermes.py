"""Native Hermes hooks for the TokenEconomizer live controller.

The adapter records only tool calls/results and token usage, in the same Claude-shaped JSONL
trace consumed by TokenEconomizer's existing mechanisms. User prompt text is not copied.
"""
import hashlib
import json
import os
import threading
import uuid

from . import hook, hosts
from .paths import secure_root, state_root


def _integer(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_usage(usage):
    """Map Hermes provider token summaries to TokenEconomizer's cache-aware usage fields."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = _integer(usage.get("cache_read_input_tokens", details.get("cached_tokens")))
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = max(0, _integer(usage.get("prompt_tokens")) - cached)
    return {
        "input_tokens": _integer(input_tokens),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": _integer(
            usage.get("cache_creation_input_tokens", details.get("cache_creation_tokens"))),
        "output_tokens": _integer(usage.get("output_tokens", usage.get("completion_tokens"))),
    }


def _is_error(result):
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error") or result.get("error")
                    or result.get("success") is False)
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return _is_error(value)
        return result.lstrip().lower().startswith(("error:", "tool error:"))
    return False


def _text(value, limit=hosts.RESULT_CAP):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)[:limit]
    return str(value or "")[:limit]


class HermesHooks:
    """Bridge Hermes' native lifecycle hooks to TokenEconomizer's existing host/controller."""

    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.lock = threading.RLock()
        self.sessions = {}
        self.tasks = {}

    def register(self, ctx):
        for name, callback in (
            ("pre_llm_call", self.pre_llm_call),
            ("pre_tool_call", self.pre_tool_call),
            ("post_tool_call", self.post_tool_call),
            ("pre_api_request", self.pre_api_request),
            ("post_api_request", self.post_api_request),
            ("on_session_end", self.on_session_end),
        ):
            ctx.register_hook(name, callback)

    def _disabled(self):
        return any(str(self.env.get(k, "")).lower() in ("1", "true", "yes", "on")
                   for k in ("TE_DISABLE", "TE_SIDECALL"))

    def _path(self, sid):
        key = hashlib.sha256(sid.encode("utf-8")).hexdigest()
        return os.path.join(state_root(self.env), "hosts", "hermes", "transcripts", key + ".jsonl")

    def _append(self, state, row):
        root = state_root(self.env)
        os.makedirs(root, mode=0o700, exist_ok=True)
        secure_root(self.env)
        path = state["transcript"]
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with self.lock, open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _dispatch(self, state, event, **fields):
        payload = {"hook_event_name": event, "session_id": state["sid"],
                   "transcript_path": state["transcript"], "cwd": state["cwd"], **fields}
        try:
            out = hook.run(payload, self.env, host_hint="hermes") or {}
            secure_root(self.env)
            return out
        except Exception:
            hook._record_error(self.env)
            return {}

    def pre_llm_call(self, session_id="", user_message="", platform="", **kwargs):
        if self._disabled() or not session_id:
            return None
        session_id = str(session_id)
        turn_id = str(kwargs.get("turn_id") or kwargs.get("task_id") or "")
        with self.lock:
            state = self.sessions.get(session_id)
            first = state is None
            if first:
                state = {"sid": session_id, "cwd": self.env.get("TE_HERMES_CWD") or os.getcwd(),
                         "transcript": self._path(session_id)}
                self.sessions[session_id] = state
            new_turn = bool(turn_id and state.get("turn_id") != turn_id)
            if new_turn:
                state["turn_id"] = turn_id
        if new_turn:
            # Keep prompt counts without retaining user-authored text.
            self._append(state, {"type": "user", "message": {"role": "user",
                                                                  "content": "[Hermes prompt]"}})
        start = self._dispatch(state, "SessionStart", source="startup", platform=platform) if first else {}
        prompt = self._dispatch(state, "UserPromptSubmit", prompt=str(user_message or ""),
                                platform=platform) if new_turn else {}
        pieces = [start.get("context"), prompt.get("context")]
        notices = [start.get("systemMessage"), prompt.get("systemMessage")]
        pieces = [text for text in pieces if isinstance(text, str) and text]
        notices = [text for text in notices if isinstance(text, str) and text]
        if notices:
            pieces.append("TokenEconomizer notice for the user: " + "\n".join(notices))
        return {"context": "\n\n".join(pieces)} if pieces else None

    def pre_api_request(self, task_id="", session_id="", turn_id="", **kwargs):
        if self._disabled() or not task_id:
            return None
        with self.lock:
            state = self.sessions.get(str(session_id))
            if state:
                state["turn_id"] = str(turn_id or "")
                self.tasks[str(task_id)] = state
        return None

    def post_api_request(self, task_id="", session_id="", api_request_id="", model="",
                         response_model=None, usage=None, **kwargs):
        if self._disabled() or not task_id:
            return None
        with self.lock:
            state = self.tasks.get(str(task_id))
            if not state or (session_id and str(session_id) not in self.sessions):
                return None
        normalized = normalize_usage(usage)
        self._append(state, {"type": "assistant", "message": {
            "id": str(api_request_id or uuid.uuid4().hex), "role": "assistant",
            "model": response_model or model or "", "usage": normalized, "content": []}})
        return None

    def pre_tool_call(self, tool_name="", args=None, task_id="", **kwargs):
        if self._disabled() or not task_id:
            return None
        args = args if isinstance(args, dict) else {}
        with self.lock:
            state = self.tasks.get(str(task_id))
            if not state:
                return None
            pending = state.pop("pending", None)
            if pending:
                return {"action": "block", "message": pending}
            call_id = uuid.uuid4().hex
            key = (str(tool_name), json.dumps(args, ensure_ascii=False, sort_keys=True))
            state.setdefault("calls", {}).setdefault(key, []).append(call_id)
        self._append(state, {"type": "assistant", "cwd": state["cwd"], "message": {
            "id": call_id, "role": "assistant", "model": "", "content": [{
                "type": "tool_use", "id": call_id, "name": str(tool_name), "input": args}]}})
        return None

    def post_tool_call(self, tool_name="", args=None, result="", task_id="", **kwargs):
        if self._disabled() or not task_id:
            return None
        args = args if isinstance(args, dict) else {}
        key = (str(tool_name), json.dumps(args, ensure_ascii=False, sort_keys=True))
        with self.lock:
            state = self.tasks.get(str(task_id))
            if not state:
                return None
            queue = state.get("calls", {}).get(key) or []
            if not queue:
                return None
            call_id = queue.pop(0)
        failed = _is_error(result)
        text = _text(result)
        self._append(state, {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": call_id, "content": text,
            "is_error": failed}]}})
        event = "PostToolUseFailure" if failed else "PostToolUse"
        out = self._dispatch(state, event, tool_name=str(tool_name), args=args,
                             result=result if isinstance(result, (str, dict, list)) else text,
                             **({"error": text} if failed else {}), prompt_id=state.get("turn_id", ""))
        prompt = out.get("context")
        notice = out.get("systemMessage")
        message = "\n\n".join(x for x in (prompt, notice) if isinstance(x, str) and x)
        if message:
            with self.lock:
                state["pending"] = message
        return None

    def on_session_end(self, session_id="", completed=False, interrupted=False, model="",
                       platform="", **kwargs):
        if self._disabled() or not session_id:
            return None
        with self.lock:
            state = self.sessions.pop(str(session_id), None)
        if not state:
            return None
        self._dispatch(state, "Stop", completed=bool(completed), platform=platform)
        self._dispatch(state, "SessionEnd", reason=("completed" if completed else "interrupted"),
                       platform=platform)
        with self.lock:
            for task_id, task_state in list(self.tasks.items()):
                if task_state is state:
                    self.tasks.pop(task_id, None)
        return None


def register(ctx):
    """Hermes plugin entry point."""
    HermesHooks().register(ctx)
