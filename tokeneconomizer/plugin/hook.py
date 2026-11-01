"""The entry point every plugin hook executes: hook JSON on stdin, hook JSON on stdout.

    python -m tokeneconomizer.plugin.hook [--host claude|codex|copilot|opencode|pi]

It never fails the agent. Whatever goes wrong -- unreadable input, a full disk, a bug -- the hook
prints `{}` (which changes nothing) and records the traceback in `<state root>/errors.log`. It
also does nothing at all in two cases: `TE_DISABLE=1`, and `TE_SIDECALL=1`, which marks the
nested agent process of the plugin's own paid side calls (`sidecall`) so it cannot recurse
into these hooks.
"""
import contextlib
import json
import os
import sys
import time
import traceback


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def run(payload, env, host_hint=None):
    """Handle one hook payload; returns the output dict (no I/O, so tests call it directly)."""
    from . import hosts
    from .context import LiveContext
    from .controller import LiveController
    host = hosts.detect(payload, env, hint=host_hint)
    if hasattr(host, "prepare"):
        payload = host.prepare(payload, env)
    ctx = LiveContext.build(host, host.cwd(payload, env), env,
                            session_id=host.session_id(payload) or None, settle=False)
    if not ctx.config.enabled:
        return {}
    if hasattr(host, "record_event"):
        host.record_event(payload)
    return LiveController(ctx, payload).handle()


def _record_error(env):
    try:
        from .paths import state_root
        root = state_root(env)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()}\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


def main(argv=None, stdin=None, stdout=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    hint = None
    if "--host" in argv:
        i = argv.index("--host")
        hint = argv[i + 1] if i + 1 < len(argv) else None
    out = {}
    try:
        # The agents send UTF-8 whatever the locale; a text stdin would decode it with the
        # locale's code page on Windows and garble a non-ASCII path.
        buf = getattr(stdin, "buffer", None)
        raw = buf.read().decode("utf-8") if buf is not None else stdin.read()
        if not (_truthy(env.get("TE_SIDECALL")) or _truthy(env.get("TE_DISABLE"))):
            payload = json.loads(raw) if raw.strip() else {}
            if isinstance(payload, dict):
                # stdout carries exactly one JSON object back to the agent. The mechanisms
                # print progress and warnings (ToolingStage's catalog warning, for one), and a
                # single stray line there would make the agent discard the whole hook output.
                with contextlib.redirect_stdout(sys.stderr):
                    out = run(payload, env, host_hint=hint) or {}
                from .paths import secure_root
                secure_root(env)
    except Exception:
        _record_error(env)
        out = {}
    from .hosts import PROGRESS
    for line in out.pop(PROGRESS, None) or ():
        stdout.write(json.dumps({"type": "progress", "message": line}))
        stdout.write("\n")
    stdout.write(json.dumps(out))
    stdout.write("\n")
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
