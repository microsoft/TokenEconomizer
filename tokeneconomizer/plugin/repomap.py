"""The repository map: aider's ranked map of a repository's key files and symbols, in context.

Opt-in (`repomap=on`). A map of the files and signatures that matter most gives the agent its
bearings up front, instead of reading files to learn where things are. It is the experiments'
repo-map baseline, delivered through the plugin's own channel instead of a project instructions
file, so nothing is written into the repository.

  * The map is aider's (`aider.repomap.RepoMap`): tree-sitter tags ranked by PageRank,
    trimmed to a token budget (`repomap_tokens`). aider is NOT a dependency of the plugin: it
    runs in whichever Python has it (`repomap_python`, else the interpreter of an installed
    `aider` command, else the plugin's own Python, else `python3`), as a separate process,
    so its dependency tree never loads into a hook.
  * aider caches the tags it parses. That cache lives in the plugin's state, next to the map
    -- not in the repository, where aider would put it.
  * A map is made for a commit and cached. At session start, a map for another commit (or
    none) is regenerated in the background; the session uses the cached one meanwhile.
  * `repomap=on`: on the first prompt of a session, a map ranked toward the identifiers in
    that prompt is generated within `repomap_timeout` seconds and added to the context; if
    that does not finish in time, the cached map goes in instead. Agents whose prompt hook
    cannot add context (Codex) get the cached map at session start.
  * `repomap=start`: the cached map at session start, on every agent. No per-prompt work.

The map is a fixed prefix on every request of the session: a cost on sessions that already
knew where to look, a saving on those that would have searched.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


# The generator ships with the core; the experiments' repo-map baseline runs the same one.
GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repomap_gen.py")

HEADER = ("# Repository map\n"
          "Ranked key files and symbols (signatures only) of this repository. Consult it to "
          "find where things are before searching.\n\n")
PROBE = "import aider.repomap"
REFRESH_TIMEOUT_S = 900


def _repo_dir(ctx):
    """Per repository, shared by every agent: a map does not depend on which one reads it."""
    return os.path.dirname(ctx.store.base)


def _state_path(ctx):
    return os.path.join(_repo_dir(ctx), "repomap.json")


def _cache_dir(ctx):
    return os.path.join(_repo_dir(ctx), "repomap-tags")


def read_state(ctx):
    try:
        with open(_state_path(ctx), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(ctx, d):
    path = _state_path(ctx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)
    os.replace(tmp, path)


def head_commit(root):
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             encoding="utf-8", errors="replace", timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _has_aider(python, env):
    # `-c` puts the working directory first on sys.path, and the background `te` runs in the
    # repository: an `aider/` the checkout carries would be the one imported, and run. An empty
    # directory instead; `-I` would also hide an aider installed in the user site.
    try:
        with tempfile.TemporaryDirectory(prefix="te-aider-probe-") as empty:
            return subprocess.run([python, "-c", PROBE], capture_output=True, timeout=60,
                                  env=env, cwd=empty).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _aider_command_python():
    """The interpreter behind an installed `aider` command (pipx and uv tool installs give it a
    venv of its own), read off the script's shebang."""
    exe = shutil.which("aider")
    if not exe:
        return None
    try:
        with open(exe, "rb") as fh:
            first = fh.readline().decode("utf-8", "replace").strip()
    except OSError:
        return None
    if first.startswith("#!"):
        parts = first[2:].split()
        if parts and os.path.basename(parts[0]) != "env" and os.path.exists(parts[0]):
            return parts[0]
    return None


def find_python(ctx):
    """A Python that can import aider, or None. The answer is remembered in the map's state.

    A Python the person named (`repomap_python`) is the only one tried: quietly using another
    would hide that the one they chose cannot make the map."""
    env = _gen_env(ctx)
    if ctx.config.repomap_python:
        return ctx.config.repomap_python if _has_aider(ctx.config.repomap_python, env) else None
    cands = [read_state(ctx).get("python"), _aider_command_python(), sys.executable,
             shutil.which("python3")]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if _has_aider(c, env):
            return c
    return None


def _gen_env(ctx):
    env = dict(ctx.env)
    env.pop("PYTHONPATH", None)
    env["TE_SIDECALL"] = "1"
    return env


def generate(ctx, python, idents=(), timeout=REFRESH_TIMEOUT_S):
    """(map text, error) for the repository as it is now."""
    cmd = [python, GEN, ctx.repo.root, str(ctx.config.repomap_tokens), ",".join(idents),
           "--git", "--cache", _cache_dir(ctx)]
    try:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=_gen_env(ctx), cwd=ctx.repo.root)
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s"
    except OSError as e:
        return "", str(e)
    if out.returncode != 0:
        return "", (out.stderr or "").strip()[-300:] or f"exit {out.returncode}"
    return out.stdout, ""


def refresh(ctx):
    """Make the cached map for the current commit. Returns the new state."""
    t0 = time.time()
    commit = head_commit(ctx.repo.root)
    python = find_python(ctx)
    if not python:
        named = ctx.config.repomap_python
        state = dict(read_state(ctx), commit=commit, updated=time.time(), error=(
            f"repomap_python {named} cannot import aider" if named else
            "no Python with aider found (install aider-chat, or set repomap_python)"))
        _write_state(ctx, state)
        return state
    text, err = generate(ctx, python)
    state = {"commit": commit, "python": python, "map": text, "chars": len(text),
             "tokens_budget": ctx.config.repomap_tokens, "secs": round(time.time() - t0, 1),
             "error": err or None, "updated": time.time()}
    if not text and read_state(ctx).get("map"):
        prior = read_state(ctx)
        state.update(map=prior["map"], chars=len(prior["map"]), commit=prior.get("commit"))
    _write_state(ctx, state)
    return state


def stale(ctx):
    s = read_state(ctx)
    return (not s.get("map") or s.get("commit") != head_commit(ctx.repo.root)
            or s.get("tokens_budget") != ctx.config.repomap_tokens)


def spawn_refresh(ctx):
    """Refresh in a detached process (inline when TE_FOLD_SYNC=1, for tests)."""
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return refresh(ctx)
    return ctx.spawn(["repomap", "refresh"], "repomap.log")


def cached_block(ctx):
    m = read_state(ctx).get("map") or ""
    return HEADER + m if m.strip() else ""


_IDENT_RES = (re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`"),
              re.compile(r"\b([a-z_][a-z0-9_]*_[a-z0-9_]+)\b"),
              re.compile(r"\b([A-Z][a-zA-Z0-9]+[A-Z][a-zA-Z0-9]*)\b"))


def prompt_idents(text):
    """Identifiers a prompt names (backticked names, snake_case, CamelCase): the map is ranked
    toward them, as the experiments ranked it toward the issue's."""
    ids = set()
    for rx in _IDENT_RES:
        ids |= set(rx.findall(text or ""))
    return sorted({i.split(".")[-1] for i in ids if len(i) > 2})[:60]


def for_prompt(ctx, prompt):
    """(block, how) for the first prompt of a session: a map ranked toward the prompt if it can
    be made in time, else the cached one."""
    idents = prompt_idents(prompt)
    python = read_state(ctx).get("python")
    if idents and python:
        text, err = generate(ctx, python, idents, timeout=ctx.config.repomap_timeout)
        if text.strip():
            return HEADER + text, "prompt"
    block = cached_block(ctx)
    return block, ("cached" if block else "none")
