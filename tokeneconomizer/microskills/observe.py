"""What a finished session shows about its environment: classified commands and outcomes.

Each shell command is split into segments, and each segment is classified by INTENT (run the
interpreter, run the tests, use the package tool, fetch from a host, probe the VCS) and FORM
(how it was spelled: `python -m pytest`, `/opt/env/bin/python -m pytest`, `cd tests && python
runtests.py`). Its outcome is read off the output as an environmental error class
(`module_missing`, `command_missing`, `connection_refused`, ...) or a sign that tests ran.
Nothing here names a path, a repository or a harness: the words that name intents are a
`Vocabulary` the domain supplies, so the same reader handles a Python/git and a Node/npm
repository. `observe_task` returns everything the environment-fact families need from one
session.
"""
import os
import posixpath
import re
from dataclasses import dataclass

from ..trace import call_cwds, load_trajectory, strip_rtk_prefix
from .lifecycle import edited_files, edited_pyfiles


# --------------------------------------------------------------------------------------
# vocabulary -- the domain-supplied words that name intents (never harness constants)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Vocabulary:
    """What the families recognise as an interpreter, a test runner, a package tool, a
    fetcher, an environment probe. A domain adapter supplies one (`env_vocabulary()`); the
    families themselves are the same for every vocabulary."""
    name: str
    interpreters: frozenset       # executables that run the language
    canonical_interpreter: str    # how a shebang-run script is spelled as a form
    runner_heads: frozenset       # bare test-runner executables
    runner_scripts: tuple         # scripts that ARE the runner when an interpreter runs them
    runner_modules: frozenset     # `<interpreter> -m <module>` runners
    package_tools: frozenset
    fetch_heads: frozenset = frozenset({"curl", "wget"})
    env_probe_heads: frozenset = frozenset({"which", "command", "type", "whereis"})
    # Tools whose `<tool> test` runs the tests.
    task_heads: frozenset = frozenset({"make", "npm", "yarn", "pnpm", "cargo", "go"})
    # Executables that build, compile, lint or type-check (the `run_build` intent).
    build_heads: frozenset = frozenset()
    # The language-neutral reading the plugin uses: builds and checks are an intent of their
    # own, every edited file counts, and the proposer reads the generic digest
    # (`distiller.build_generic_digest`). The experiments' vocabularies keep their narrower one.
    generic: bool = False


PYTHON_GIT = Vocabulary(
    name="python-git",
    interpreters=frozenset({"python", "python2", "python3", "pypy", "pypy3"}),
    canonical_interpreter="python",
    runner_heads=frozenset({"pytest", "py.test", "tox", "nox", "nosetests", "trial"}),
    runner_scripts=("runtests.py", "bin/test", "bin/doctest", "manage.py"),
    runner_modules=frozenset({"pytest", "unittest", "nose", "django", "tox", "nox"}),
    package_tools=frozenset({"pip", "pip3", "conda", "mamba", "poetry", "pipenv", "uv",
                             "apt", "apt-get"}),
)

NODE_WEB = Vocabulary(
    name="node-web",
    interpreters=frozenset({"node", "nodejs", "deno", "bun", "tsx", "ts-node"}),
    canonical_interpreter="node",
    runner_heads=frozenset({"jest", "vitest", "mocha", "playwright", "cypress", "ava",
                            "karma"}),
    runner_scripts=(),
    runner_modules=frozenset(),
    package_tools=frozenset({"npm", "yarn", "pnpm", "corepack", "apt", "apt-get"}),
)

# The plugin's: every ecosystem at once, since a repository a person works in may mix them.
GENERIC = Vocabulary(
    name="generic",
    interpreters=PYTHON_GIT.interpreters | NODE_WEB.interpreters
    | frozenset({"ruby", "perl", "php", "Rscript", "julia", "lua", "elixir"}),
    canonical_interpreter="python",
    runner_heads=PYTHON_GIT.runner_heads | NODE_WEB.runner_heads
    | frozenset({"rspec", "phpunit", "ctest", "bats"}),
    runner_scripts=PYTHON_GIT.runner_scripts,
    runner_modules=PYTHON_GIT.runner_modules,
    package_tools=PYTHON_GIT.package_tools | NODE_WEB.package_tools
    | frozenset({"gem", "bundle", "composer", "cargo", "go", "mvn", "gradle", "dotnet", "brew",
                 "rustup", "pyenv", "nvm", "asdf"}),
    task_heads=frozenset({"make", "npm", "yarn", "pnpm", "cargo", "go", "mvn", "gradle",
                          "gradlew", "dotnet", "mix", "swift", "rake"}),
    build_heads=frozenset({"make", "cmake", "ninja", "bazel", "tsc", "eslint", "prettier",
                           "ruff", "flake8", "pylint", "mypy", "pyright", "black", "isort",
                           "golangci-lint", "gcc", "g++", "clang", "javac", "webpack", "vite"}),
    generic=True,
)
# `<tool> build`, `npm run lint`, ...: what a generic reading counts as building or checking.
BUILD_SUBS = frozenset({"build", "check", "compile", "lint", "vet", "typecheck", "type-check",
                        "clippy", "package", "verify", "fmt", "format"})
_RUN_SCRIPT_HEADS = frozenset({"npm", "yarn", "pnpm", "bun"})

DEFAULT_VOCAB = PYTHON_GIT
# Module-level aliases of the default vocabulary, kept for the offline analyses.
INTERPRETERS = PYTHON_GIT.interpreters
RUNNER_HEADS = PYTHON_GIT.runner_heads
RUNNER_SCRIPTS = PYTHON_GIT.runner_scripts
RUNNER_MODULES = PYTHON_GIT.runner_modules
PACKAGE_TOOLS = PYTHON_GIT.package_tools
FETCH_HEADS = PYTHON_GIT.fetch_heads
GIT_FETCH_SUBS = frozenset({"fetch", "clone", "pull", "ls-remote"})
GIT_PROBE_SUBS = frozenset({"remote", "branch", "tag", "fsck", "stash", "show-ref",
                            "for-each-ref"})
ENV_PROBE_HEADS = PYTHON_GIT.env_probe_heads
PREFIX_HEADS = ("sudo", "env", "time", "exec", "nice", "nohup", "timeout")
_PASSTHROUGH = frozenset({"cd", "echo", "head", "tail", "cat", "sort", "uniq", "tee", "true",
                          "printf", "less", "more"})

_ACTIVATE_RE = re.compile(r"^(?:source|\.)\s+\S*activate\s+([\w.-]+)\s*$")
_CD_RE = re.compile(r"^cd\s+(['\"]?)([^\s;&|'\"]+)\1\s*$")
_CD_FAILED_RE = re.compile(r"\bcd: [^\n]*: (?:No such file or directory|Not a directory)")
_CONDA_ACTIVATE_RE = re.compile(r"^(?:conda|mamba)\s+activate\s+([\w.-]+)\s*$")
_CONDA_RUN_RE = re.compile(r"^(?:conda|mamba)\s+run\s+(?:--no-capture-output\s+|--live-stream\s+)*"
                           r"(?:-n|--name)\s+([\w.-]+)\s+")
_TOOL_RUN_RE = re.compile(r"^(poetry|pipenv|uv|npx|bundle|yarn|pnpm)\s+(?:run|exec)\s+")
_NPX_RE = re.compile(r"^npx\s+(?:(?:--yes|-y|--no-install|--no)\s+)*")
_INTERP_VERSIONED_RE = re.compile(r"^(?:python|node)\d(?:\.\d+)?$")
_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+")
_ASSIGN_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAP_RE = re.compile(r"^/bin/(?:ba)?sh\s+-l?c\s+(.*)$", re.S)
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
_URL_HOST_RE = re.compile(r"https?://([^/\s'\"`]+)")

# Error classes an ENVIRONMENT produces (as opposed to the code under test). Order matters:
# the first match names the class.
ERROR_CLASSES = (
    ("module_missing", re.compile(r"ModuleNotFoundError|No module named|Cannot find module|"
                                  r"ERR_MODULE_NOT_FOUND", re.I)),
    ("command_missing", re.compile(r"command not found|: not found\b|is not recognized", re.I)),
    # The interpreter started, but the script it was told to run is not where it looked:
    # `python runtests.py` outside the directory that has it.
    ("script_missing", re.compile(r"can't open file .*No such file or directory", re.I)),
    ("permission_denied", re.compile(r"Permission denied|EACCES", re.I)),
    ("usage_error", re.compile(r"(?m)^usage:|unrecognized arguments|no such option|"
                               r"error: argument|unknown option", re.I)),
    ("connection_refused", re.compile(r"Connection refused|Failed to connect to|"
                                      r"couldn't connect to|Could not connect|ECONNREFUSED", re.I)),
    ("name_resolution", re.compile(r"Could not resolve host|Temporary failure in name "
                                   r"resolution|Name or service not known|getaddrinfo|"
                                   r"nodename nor servname|ENOTFOUND", re.I)),
    ("unreachable", re.compile(r"Network is unreachable|No route to host|Connection timed out|"
                               r"Operation timed out|timed out after|ETIMEDOUT", re.I)),
)
NETWORK_ERRORS = frozenset({"connection_refused", "name_resolution", "unreachable"})
FORM_ERRORS = frozenset({"module_missing", "command_missing", "script_missing",
                         "usage_error", "permission_denied"})

# What "the probe returned something" looks like, per VCS probe kind. Deliberately narrow
# patterns so the output of an unrelated segment in a compound command cannot masquerade
# as a hit.
_PROBE_HIT = {
    "remote": re.compile(r"(?m)^\S+\s+\S+\s+\((?:fetch|push)\)\s*$"),
    "branch": re.compile(r"(?m)^\s*remotes/\S+"),
    "tag": re.compile(r"(?m)^v?\d+(?:\.\d+)+(?:[a-z]+\d*|\.post\d+|-\S+)?\s*$"),
    "fsck": re.compile(r"(?m)^(?:unreachable|dangling)\s+(?:commit|tree|blob|tag)\s+[0-9a-f]{7,}"),
    "stash": re.compile(r"(?m)^stash@\{\d+\}"),
    "refs": re.compile(r"(?m)^[0-9a-f]{7,}\s+refs/"),
}


# --------------------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------------------
def unwrap(cmd):
    """Strip an agent's `/bin/bash -lc "<cmd>"` wrapper, if present."""
    cmd = cmd or ""
    m = _WRAP_RE.match(cmd.strip())
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        q = inner[0]
        inner = inner[1:-1]
        if q == '"':
            inner = inner.replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\")
    return inner


def segments(cmd):
    """Split a shell command into simple segments on && || ; | and newlines, outside quotes.
    Heredoc bodies are elided first so their contents are never mistaken for commands."""
    cmd = _HEREDOC_RE.sub(lambda m: m.group(0).split("\n", 1)[0] + " <heredoc>", cmd or "")
    out, buf, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            buf.append(c)
            if c == q:
                q = None
            elif c == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 1
        elif c in "\"'":
            q = c; buf.append(c)
        elif c == "\n" or c == ";" or c == "|" or (c == "&" and cmd[i:i + 2] == "&&"):
            out.append("".join(buf)); buf = []
            if cmd[i:i + 2] in ("&&", "||"):
                i += 1
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return [s.strip().strip("()").strip() for s in out if s.strip().strip("()").strip()]


def _tokens(seg):
    return seg.split()


class Observation:
    """One classified command segment."""
    __slots__ = ("call", "intent", "form", "head", "prefix", "hosts", "probe", "outcome",
                 "segment", "labels", "label_err", "loaded", "tests_ran", "env")

    def __init__(self, call, intent, form, head, prefix, segment, hosts=(), probe=None):
        self.call, self.intent, self.form, self.head, self.prefix = call, intent, form, head, prefix
        self.segment, self.hosts, self.probe, self.outcome = segment, tuple(hosts), probe, None
        self.labels, self.label_err, self.loaded = (), False, False
        self.tests_ran = False
        self.env = False          # typed with `VAR=value` before it, which its form leaves out

    def __repr__(self):
        return f"Obs({self.intent},{self.form!r},{self.outcome})"


def repository_top(start):
    """The repository a directory belongs to: the nearest directory at or above `start` that
    holds a `.git` (a directory, or a worktree's file); None when there is none on disk, or
    `start` itself is gone (a removed checkout must not borrow an enclosing repository)."""
    if not start or not os.path.isabs(start) or not os.path.isdir(start):
        return None
    d = os.path.normpath(start)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _cd_target(cur, target, root=None, top=None):
    """Where `cd target` from `cur` lands, relative to the directory the session started in
    ("" for that directory itself); None when it leaves it or cannot be told (`~`, `$VAR`,
    `-`, an absolute path with no known start).

    A session started in a subdirectory of its repository (`top`) that changes up to the
    repository's top runs what a session started there runs: `cd <top> && pytest` is the plain
    `pytest`, not a form of its own spelled with one machine's path. Any other directory above
    the start is taken as typed: relative to `top` it would hold only for sessions that start
    there."""
    if not target or target[0] in "~$-":
        return None
    # The command is shell text (Git Bash on Windows too), so its paths are compared in one
    # POSIX spelling: `C:\r`, `C:/r` and `/c/r` are the same directory.
    target, cur = _shell_path(target), _shell_path(cur or ".")
    if _shell_isabs(target):
        if not root:
            return None
        rel = posixpath.relpath(posixpath.normpath(target), posixpath.normpath(_shell_path(root)))
    else:
        rel = posixpath.normpath(posixpath.join(cur, target))
    if rel == ".":
        return ""
    if rel == ".." or rel.startswith("../"):
        if not (top and root):
            return None
        at = posixpath.normpath(posixpath.join(_shell_path(root), rel))
        return "" if at == posixpath.normpath(_shell_path(top)) else None
    return rel


_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)|^([A-Za-z]):(?=/|$)")


def _shell_path(p):
    """`p` with forward slashes and a Windows drive spelled `c:` (from a backslashed `C:`, `C:/`
    or `/c/`); a POSIX path is returned as it is."""
    p = p.replace("\\", "/") if os.sep == "\\" else p
    m = _DRIVE_RE.match(p)
    if m and os.sep == "\\":
        p = (m.group(1) or m.group(2)).lower() + ":" + p[m.end():]
    return p


def _shell_isabs(p):
    return p.startswith("/") or (os.sep == "\\" and bool(re.match(r"^[a-z]:(/|$)", p)))


def classify_command(cmd, call_index=0, vocab=None, cwd="", root=None, top=None):
    """Observations for one shell command (possibly compound). Activation prefixes carry
    across the segments of one command, which is how `source activate X && python ...`
    becomes a single form. `vocab` names the domain's intents (default Python/git).

    `cwd` is the directory the command started in, relative to the one the session started
    in (`root`, an absolute path when known): a shell that keeps its directory between calls
    runs a bare `python runtests.py` in `tests/` after an earlier `cd tests`, and that is the
    same form as `cd tests && python runtests.py` typed at the start. `top` is the repository
    `root` lies in (`repository_top`), for a `cd` up to it from a subdirectory (`_cd_target`)."""
    vocab = vocab or DEFAULT_VOCAB
    obs = []
    carried = ""
    here = cwd or ""
    cd = f"cd {here} &&" if here else ""
    for seg in segments(unwrap(cmd)):
        # Transparent wrappers first: `K=V rtk pytest`, `rtk K=V pytest` and `rtk proxy pytest`
        # are all a pytest run. rtk's Codex mode puts that prefix on every command the agent
        # types (trace.strip_rtk_prefix), and a first-token vocabulary that did not remove it
        # would form no fact at all on the very arm that stacks injection on rtk.
        typed = seg.strip()
        seg = _ENV_ASSIGN_RE.sub("", typed).strip()
        assigned = seg != typed
        unwrapped = strip_rtk_prefix(seg).strip()
        seg = _ENV_ASSIGN_RE.sub("", unwrapped).strip()
        assigned = assigned or seg != unwrapped
        if not seg:
            continue
        # A directory change is part of how the rest of the command runs: `cd tests && python
        # runtests.py` works where a bare `python runtests.py` does not, so it is carried into
        # the forms that follow, as an activation is.
        m = _CD_RE.match(seg)
        if m:
            there = _cd_target(here, m.group(2), root, top) if here is not None else None
            if there is None:
                here, cd = None, f"cd {m.group(2)} &&"
            else:
                here, cd = there, (f"cd {there} &&" if there else "")
            continue
        m = _ACTIVATE_RE.match(seg) or _CONDA_ACTIVATE_RE.match(seg)
        if m:
            carried = f"{seg.split()[0] if seg.startswith(('conda', 'mamba')) else 'source'} activate {m.group(1)} &&"
            if carried.startswith("conda") or carried.startswith("mamba"):
                carried = f"conda activate {m.group(1)} &&"
            continue
        prefix = " ".join(x for x in (cd, carried) if x)
        body = seg
        m = _CONDA_RUN_RE.match(body)
        if m:
            prefix = (prefix + " " if prefix else "") + f"conda run -n {m.group(1)}"
            body = body[m.end():]
        m = _TOOL_RUN_RE.match(body)
        if m:
            prefix = (prefix + " " if prefix else "") + f"{m.group(1)} run"
            body = body[m.end():]
        else:
            m = _NPX_RE.match(body)
            if m and m.end() > 0:
                prefix = (prefix + " " if prefix else "") + "npx"
                body = body[m.end():]
        toks = _tokens(body)
        while toks and (toks[0] in PREFIX_HEADS or _ASSIGN_TOKEN_RE.match(toks[0])):
            if _ASSIGN_TOKEN_RE.match(toks[0]):          # `env VAR=value cmd`
                assigned, toks = True, toks[1:]
            else:
                toks = toks[1:] if toks[0] != "timeout" else toks[2:]
        if not toks:
            continue
        head = toks[0]
        base = head.rsplit("/", 1)[-1]
        rest = toks[1:]
        o = _classify(head, base, rest, body, prefix, seg, call_index, vocab)
        if o is not None:
            o.env = assigned
            obs.append(o)
    return obs


def _runner_script(rest, vocab):
    for t in rest:
        if t.startswith("-"):
            continue
        for s in vocab.runner_scripts:
            if t == s or t.endswith("/" + s) or t.endswith(s):
                return t.lstrip("./") if t.startswith("./") else t
        return None
    return None


def _classify(head, base, rest, body, prefix, seg, call, vocab):
    def form(expr):
        return (prefix + " " + expr).strip() if prefix else expr

    if base in vocab.interpreters or _INTERP_VERSIONED_RE.match(base):
        if rest and all(t in ("--version", "-V", "-VV", "-v") for t in rest):
            return Observation(call, "probe_env", form(head), head, prefix, seg)
        if "-m" in rest:
            mod = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
            if mod in vocab.runner_modules:
                return Observation(call, "run_tests", form(f"{head} -m {mod}"), head, prefix, seg)
        script = _runner_script(rest, vocab)
        if script and (script != "manage.py" or "test" in rest):
            return Observation(call, "run_tests", form(f"{head} {script}"), head, prefix, seg)
        return Observation(call, "run_interpreter", form(head), head, prefix, seg)
    if base in vocab.runner_heads:
        return Observation(call, "run_tests", form(head), head, prefix, seg)
    for s in vocab.runner_scripts:
        if head == s or head.endswith("/" + s) or head == "./" + s:
            if s == "manage.py" and "test" not in rest:
                break
            # A script run through its shebang is the interpreter running it; spell the
            # form that way so `./tests/runtests.py` and `python tests/runtests.py` pool.
            return Observation(call, "run_tests",
                               form(f"{vocab.canonical_interpreter} " + head.lstrip("./")),
                               head, prefix, seg)
    if base in vocab.task_heads and rest and rest[0] == "test":
        return Observation(call, "run_tests", form(f"{base} test"), head, prefix, seg)
    if vocab.generic:
        if base in _RUN_SCRIPT_HEADS and len(rest) > 1 and rest[0] == "run":
            script = rest[1]
            kind = script.split(":", 1)[0]
            if kind == "test":
                return Observation(call, "run_tests", form(f"{base} run {script}"), head, prefix,
                                   seg)
            if kind in BUILD_SUBS:
                return Observation(call, "run_build", form(f"{base} run {script}"), head, prefix,
                                   seg)
        if base in vocab.task_heads and rest and rest[0] in BUILD_SUBS:
            return Observation(call, "run_build", form(f"{base} {rest[0]}"), head, prefix, seg)
        if base in vocab.build_heads:
            return Observation(call, "run_build", form(base), head, prefix, seg)
    if base in vocab.env_probe_heads:
        return Observation(call, "probe_env", form(base), head, prefix, seg)
    if base in vocab.fetch_heads:
        return Observation(call, "fetch_remote", form(base), head, prefix, seg,
                           hosts=_URL_HOST_RE.findall(body))
    if base == "git" and rest:
        sub = rest[0]
        if sub in GIT_FETCH_SUBS:
            return Observation(call, "fetch_remote", form(f"git {sub}"), head, prefix, seg,
                               hosts=_URL_HOST_RE.findall(body))
        if sub in GIT_PROBE_SUBS:
            kind = _probe_kind(sub, rest[1:])
            if kind:
                return Observation(call, "probe_vcs_refs", form(f"git {sub}"), head, prefix,
                                   seg, probe=kind)
        return None
    if base in vocab.package_tools:
        if base.startswith(("pip", "npm", "yarn", "pnpm")) and rest and rest[0] in (
                "install", "download", "add", "ci"):
            hosts = _URL_HOST_RE.findall(body)
            return Observation(call, "fetch_remote", form(f"{base} {rest[0]}"), head, prefix,
                               seg, hosts=hosts)
        return Observation(call, "package_tool", form(base), head, prefix, seg)
    return None


def _probe_kind(sub, args):
    a = set(args)
    if sub == "remote":
        return "remote"
    if sub == "branch" and a & {"-a", "-r", "--all", "--remotes", "-ar", "-ra"}:
        return "branch"
    if sub == "tag" and "--contains" in a:      # plain `git tag` lists PAST tags too
        return "tag"
    if sub == "fsck":
        return "fsck"
    if sub == "stash" and args[:1] == ["list"]:
        return "stash"
    if sub in ("show-ref", "for-each-ref"):
        return "refs"
    return None


def error_class(output):
    for name, rx in ERROR_CLASSES:
        if rx.search(output or ""):
            return name
    return None


def _exit_code_hint(output):
    m = re.search(r"(?i)\bexit code[:=\s]+(\d+)", output or "")
    return int(m.group(1)) if m else None


# The runner could not load a test label: the module, file or name it names does not resolve.
# Read off the output rather than the exit code -- agents pipe test runs through `| tail`.
_LABEL_ERR_RE = re.compile(r"Failed to import test module|unittest\.loader\._FailedTest|"
                           r"ERROR: (?:file or directory )?not found|no tests ran|"
                           r"module '[\w.]+' has no attribute", re.I)
_NO_MODULE_RE = re.compile(r"No module named ['\"]?([\w.]+)")


def _label_failed(labels, out):
    """Did the runner fail to load one of these labels? Either it says so, or the module it
    cannot import is the label's own (`tests` for `tests.app`) -- a missing `pytest` is not."""
    if _LABEL_ERR_RE.search(out or ""):
        return True
    heads = {re.split(r"[./:]", lab, maxsplit=1)[0] for lab in labels}
    return any(m.group(1).split(".")[0] in heads for m in _NO_MODULE_RE.finditer(out or ""))


# ... and positive evidence that it loaded: tests actually ran. Output an agent filtered away
# (`| grep FAILED`, a silent run) proves nothing either way.
_LOADED_RE = re.compile(r"\bRan [1-9]\d* tests?\b|\b[1-9]\d* (?:passed|failed|passing|failing)\b|"
                        r"\bcollected [1-9]\d* items?\b")
# Other runners' summaries, for the generic reading: Go's `ok  pkg 0.01s` and `--- PASS`, Rust's
# `test result:`, RSpec's `3 examples, 0 failures`, .NET's `Passed!`, Maven's `Tests run: 3`.
_LOADED_GENERIC_RE = re.compile(r"^ok\s+\S+\s+[\d.]+s\b|^--- (?:PASS|FAIL)\b|"
                                r"\btest result: (?:ok|FAILED)\b|"
                                r"\b[1-9]\d* examples?, \d+ failures?\b|"
                                r"\b(?:Passed|Failed)!\s+-|\bTests run: [1-9]\d*", re.M)
# Code asking for an environment variable by name, and the ways a command sets one.
_ENV_NEED_RE = re.compile(r"environment variable\s+['\"`]?([A-Z][A-Z0-9_]{2,})")
_ENV_PREFIX_RE = re.compile(r"(?:^|[\s;&(])(?:export\s+)?([A-Z][A-Z0-9_]{2,})=(['\"]?)([\w.\-/:]+)\2")
_ENV_INCODE_RE = re.compile(r"os\.environ(?:\.setdefault\(\s*|\[\s*)['\"]([A-Z][A-Z0-9_]{2,})['\"]"
                            r"\s*(?:,|\]\s*=)\s*['\"]([\w.\-/:]+)['\"]")
_FAILED_RE = re.compile(r"Traceback \(most recent call last\)|^\w*Error\b|"
                        r"\b[Ee]xit [Cc]ode[:=\s]+[1-9]", re.M)


_LABEL_TOKEN_RE = re.compile(r"^[A-Za-z_][\w.\-/]*(?:::[\w.\-\[\]]+)*$")


def _test_labels(o):
    """The test labels a runner observation was given: positional arguments after the runner's
    own token that look like a module, a path or a test id. A plain name right after an option
    is taken to be that option's value (`-k name`), so it is not a label."""
    toks = _tokens(o.segment)
    last = (o.form.split() or [""])[-1]
    start = next((i + 1 for i, t in enumerate(toks)
                  if t == last or t.lstrip("./") == last or t.endswith("/" + last)), len(toks))
    out, skip, prev = [], False, ""
    for t in toks[start:]:
        if skip:
            skip, prev = False, t
            continue
        if t in (">", ">>", "2>", "<", "|") or t.startswith(("2>", ">", "<", "|")):
            skip = t in (">", ">>", "2>", "<")
            prev = t
            continue
        structured = any(c in t for c in (".", "::", "/"))
        after_option = prev.startswith("-") and "=" not in prev
        if (not t.startswith("-") and _LABEL_TOKEN_RE.match(t)
                and (structured or not after_option)):
            out.append(t)
        prev = t
    return tuple(out)


def label_rule(a, b):
    """("sep", "::") or ("strip", prefix) when label `b` is label `a` rewritten into the shape the
    runner loads, else None."""
    if "::" in a and "::" not in b:
        da = a.replace("::", ".")
        if b == da or da.startswith(b + ".") or b.startswith(da + "."):
            return ("sep", "::")
    for sep in (".", "/"):
        if sep not in a:
            continue
        p = a.split(sep, 1)[0] + sep
        rest = a[len(p):]
        if rest and not b.startswith(p) and (
                b == rest or rest.startswith(b + sep) or b.startswith(rest + sep)):
            return ("strip", p)
    return None


def _env_calls(cmd, out, success):
    """What one call says about environment variables: those its output asks for, and those
    its command sets (inline, exported or in the code it runs)."""
    sets = {m.group(1): m.group(3) for m in _ENV_PREFIX_RE.finditer(cmd)}
    sets.update({m.group(1): m.group(2) for m in _ENV_INCODE_RE.finditer(cmd)})
    return {"need": sorted(set(_ENV_NEED_RE.findall(out or ""))), "sets": sets,
            "ok": bool(success) and not _FAILED_RE.search(out or "")}


def form_failed(o):
    """Did this run show that the way the command was CALLED fails here? Not when the runner
    started and rejected what it was given: a label it could not load (Django reports one as a
    ModuleNotFoundError) or an option it does not take (`-k` to Django's runner) is a mistake
    in the arguments, and once taught "`python tests/runtests.py` fails" from mistyped labels."""
    if o.outcome not in FORM_ERRORS or o.label_err:
        return False
    return not (o.intent == "run_tests" and o.outcome == "usage_error")


def form_worked(o):
    """Did this run show the form works? A test run must show tests running: one whose
    output was filtered away (`| grep -E "^(OK|FAILED)"` around a "can't open file") or that
    stopped before any test proves nothing."""
    return o.outcome == "ran" and (o.intent != "run_tests" or o.tests_ran)


def observe_task(transcript_path, vocab=None):
    """Per-task digest: observations with outcomes, the edited files, the call count.

    Outcome attribution inside a compound command is heuristic and conservative: an
    infrastructure error is attributed to the segment whose executable the error names, else
    to the last relevant segment; a call that succeeded marks every relevant segment as
    having run; a failed call with no infrastructure error marks only the first relevant
    segment as having run and leaves the others unknown (an `&&` chain may not have reached
    them)."""
    vocab = vocab or DEFAULT_VOCAB
    calls = load_trajectory(transcript_path)
    cwds = call_cwds(transcript_path)
    root = next((c for c in cwds if c), None)
    top = repository_top(root)
    obs_all = []
    triples = []
    env_calls = []
    for idx, (name, args, out, ok) in enumerate(calls):
        tool = (name or "").lower()
        triples.append((tool, args if isinstance(args, dict) else {}, out or ""))
        if tool not in ("shell", "bash", "stop_bash"):
            continue
        cmd = (args.get("command") or args.get("cmd") or "") if isinstance(args, dict) else ""
        out = out or ""
        if _CD_FAILED_RE.search(out):
            continue                     # stopped at its `cd`: nothing after it ran
        here = ""
        if root and idx < len(cwds) and cwds[idx]:
            here = _cd_target("", cwds[idx], root, top)
        if here is None:
            # Started outside the session's directory: its own `cd`s are taken as typed.
            obs = classify_command(cmd, idx, vocab)
        else:
            obs = classify_command(cmd, idx, vocab, cwd=here, root=root, top=top)
        if vocab.generic or "python" in cmd or "=" in cmd:
            ec = _env_calls(cmd, out, ok if _exit_code_hint(out) is None else
                            _exit_code_hint(out) == 0)
            if ec["need"] or ec["sets"]:
                env_calls.append(dict(ec, call=idx))
        if not obs:
            continue
        for o in obs:
            if o.intent == "run_tests":
                o.labels = _test_labels(o)
                o.label_err = bool(o.labels) and _label_failed(o.labels, out)
                o.loaded = bool(o.labels) and not o.label_err and bool(_LOADED_RE.search(out))
                o.tests_ran = bool(_LOADED_RE.search(out) or (
                    vocab.generic and _LOADED_GENERIC_RE.search(out)))
        # Pass-through segments (a leading `cd`, a trailing `| head`, an `echo ---`
        # separator) neither add evidence nor pollute the output, so they do not break a
        # call's homogeneity; filters that rewrite content (grep, sed, awk, wc) do.
        n_seg = sum(1 for sg in segments(unwrap(cmd))
                    if (sg.split() or [""])[0].rsplit("/", 1)[-1] not in _PASSTHROUGH)
        homogeneous = n_seg == len(obs) and len({o.intent for o in obs}) == 1
        hint = _exit_code_hint(out)
        success = bool(ok) if hint is None else (hint == 0)
        err = error_class(out)
        if err in FORM_ERRORS or err in NETWORK_ERRORS:
            targets = [o for o in obs if o.intent in ("run_interpreter", "run_tests",
                                                       "run_build", "package_tool",
                                                       "fetch_remote")]
            named = [o for o in targets
                     if (o.head and o.head in out) or (o.head and o.head.rsplit("/", 1)[-1] in
                                                        _error_line(out, err))]
            if err in NETWORK_ERRORS:
                named = [o for o in targets if o.intent == "fetch_remote"
                         and (not o.hosts or any(h in out for h in o.hosts))] or \
                        [o for o in targets if o.intent == "fetch_remote"]
            chosen = named or (targets[-1:] if not success else [])
            for o in chosen:
                o.outcome = err
            if success:
                for o in obs:
                    if o.outcome is None:
                        o.outcome = "ran"
        elif success:
            for o in obs:
                o.outcome = "ran"
        else:
            first = True
            for o in obs:
                if o.intent in ("run_interpreter", "run_tests", "run_build", "package_tool",
                                "fetch_remote"):
                    o.outcome = "ran" if first else None
                    first = False
                else:
                    o.outcome = "ran" if o.intent != "probe_vcs_refs" else None
        for o in obs:
            if o.intent == "probe_vcs_refs":
                # Judged only when every segment of the call is a probe: another segment's
                # output (a `git describe`, a `sed`) would otherwise be read as the probe's.
                if not homogeneous:
                    o.outcome = None
                else:
                    o.outcome = "hit" if _PROBE_HIT[o.probe].search(out) else (
                        "empty" if success else None)
            if o.intent == "fetch_remote" and o.outcome == "ran":
                # Reachability needs a homogeneous call that exited 0 with real output and no
                # network error; a compound command's exit code and output belong to its
                # last segment, not to the fetch.
                if not homogeneous or not success or not out.strip() or err in NETWORK_ERRORS:
                    o.outcome = None
        obs_all.extend(obs)
    edited = edited_files(triples) if vocab.generic else edited_pyfiles(triples)
    return {"obs": obs_all, "edited": sorted(edited), "n_calls": len(calls),
            "env": env_calls}


def _error_line(out, err):
    rx = dict(ERROR_CLASSES)[err]
    m = rx.search(out or "")
    if not m:
        return ""
    start = (out or "").rfind("\n", 0, m.start()) + 1
    end = (out or "").find("\n", m.end())
    return (out or "")[start:end if end >= 0 else None]


def is_bare(form):
    """A form with no activation prefix and no explicit path is 'bare'."""
    head = form.split()[0] if form else ""
    return "/" not in head and " activate " not in form and " run " not in form and \
        not form.startswith(("conda run", "poetry run", "pipenv run", "uv run", "npx "))
