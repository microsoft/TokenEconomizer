"""One session's hooks, one at a time.

An agent that calls tools in parallel runs their hooks in parallel too: Claude Code starts a
message's `PostToolUse` hooks together. Each hook reads the session document, folds its call in and
writes the document back, so without this the last writer wins and the loop guard's turn misses
calls.

`run` takes the session's lock before a hook's handler runs. A hook must not hold up the agent, so
it waits only a few seconds (`WAITS`). One that gets no lock by then saves its payload to the
session's queue and answers `{}`, and whoever holds the lock applies the queue: the holder after
its own event, every later hook before its own. An event leaves the queue before it is applied, so
it is applied at most once.

A queued hook has already answered, so what matters is what its answer would have been:

- `SessionStart` carries the injected facts, and `UserPromptSubmit` the repository map under
  `repomap=on`. These are not queued: they wait, and then run without the lock.
- A loop-guard decision reached on a replayed `PostToolUse` is held on the turn, and the turn's next
  `PostToolUse` acts on it (`LiveController.post_tool`). Nothing is counted until then, and a turn
  that ends first drops it: there is no one left to redirect.
- Every other answer is `{}`.

Locks and queues live under `<root>/locks/<host>/`, outside any repository's state: `sessions/` is
read as session records, and a session's repository can change under it (`repos`).
"""
import itertools
import json
import os
import re
import time

from .store import Store

# Seconds a hook waits for its session's lock. Each stays short of the shortest timeout any agent
# gives that hook (`plugin/hooks/*.json`), which a test pins: Codex allows SessionEnd 3 s.
WAITS = {"SessionStart": 10.0, "UserPromptSubmit": 5.0, "PostToolUse": 3.0,
         "PostToolUseFailure": 3.0, "Stop": 5.0, "SessionEnd": 1.0}
QUEUED = frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"})
DRAIN_MAX_EVENTS = 200             # per drain, so one hook never replays without end
DRAIN_BUDGET_S = 8.0
IDLE_S = 2 * 86400                 # a lock file or empty queue left this long is removed

_SEQ = itertools.count()


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(text or "unknown"))[:200] or "unknown"


def _queues(ctrl, event):
    """Is this event's answer always `{}`, so that the hook can leave it for another to apply?"""
    if event == "UserPromptSubmit" and ctrl.cfg.repomap == "on":
        return False
    return event in QUEUED


def enqueue(queue, payload):
    """Save one hook's payload to a session's queue; whether it was saved. Names sort by time; the
    pid and a sequence keep two saves in one clock tick apart (Windows' clock can repeat)."""
    try:
        os.makedirs(queue, exist_ok=True)
        name = f"{time.time_ns():020d}-{os.getpid():08d}-{next(_SEQ):06d}"
        tmp = os.path.join(queue, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, os.path.join(queue, name + ".json"))
        return True
    except (OSError, TypeError, ValueError):
        return False


def drain(ctrl, queue):
    """Apply a session's queued events, oldest first, with the caller holding the session's lock.
    Each is removed before it is applied, so a crash loses it rather than applies it twice."""
    done, deadline = 0, time.time() + DRAIN_BUDGET_S
    while done < DRAIN_MAX_EVENTS and time.time() < deadline:
        try:
            names = sorted(n for n in os.listdir(queue) if n.endswith(".json"))
        except OSError:
            return
        if not names:
            return
        for name in names:
            path = os.path.join(queue, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, ValueError):
                payload = None
            try:
                os.remove(path)
            except OSError:
                continue
            if isinstance(payload, dict):
                _replay(ctrl, payload)
                done += 1
            if done >= DRAIN_MAX_EVENTS or time.time() >= deadline:
                return


def _replay(ctrl, payload):
    """Run a queued event's handler; its answer goes nowhere, since the hook that queued it has
    answered. An error here is the queued hook's, so it must not fail the hook replaying it."""
    try:
        other = type(ctrl)(ctrl.ctx, payload)
        handler = other.handler(payload.get("hook_event_name") or "")
        if handler and other.sid == ctrl.sid:
            other.replaying = True
            handler()
    except Exception:
        ctrl._record_error()


def _prune(base):
    """Remove the lock files and empty queues of long-idle sessions, at most once a day. A queue
    still holding events stays: the session's next hook applies them."""
    now, marker = time.time(), os.path.join(base, ".pruned")
    try:
        if now - os.path.getmtime(marker) < 86400:
            return
    except OSError:
        pass
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            try:
                if now - os.path.getmtime(path) <= IDLE_S:
                    continue
                if name.endswith(".lock"):
                    os.remove(path)
                elif name.endswith(".queue") and not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                pass
        open(marker, "w", encoding="utf-8").close()
    except OSError:
        pass


def run(ctrl, event, handler):
    """`handler`'s answer, with the session's other hooks kept out; `{}` when it was queued."""
    base = os.path.join(ctrl.ctx.root, "locks", _safe(ctrl.host.name))
    name = _safe(ctrl.sid)
    queue = os.path.join(base, name + ".queue")
    with Store(base).locked(name, timeout=WAITS.get(event, 3.0)) as got:
        _prune(base)
        if got:
            drain(ctrl, queue)
            try:
                return handler() or {}
            finally:
                drain(ctrl, queue)
    if _queues(ctrl, event) and enqueue(queue, ctrl.p):
        return {}
    return handler() or {}
