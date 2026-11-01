"""Where live state lives, and which repository a session belongs to.

State is keyed by REPOSITORY, not by directory: two clones or worktrees of one repository, or a
session started in a subdirectory, are the same stream of work and should share what was
learned about it. The key is derived from the `origin` remote when there is one (so a re-clone
keeps its history), from the real path of the main checkout otherwise, and from the directory
itself outside any checkout. Reading the remote parses `.git/config` directly -- a hook that
shelled out to `git` would pay a subprocess on every tool call. Which repository a running
session belongs to is decided once, at its first hook, and pinned (`repos`); state written under
an earlier key is carried over by `repos` too.

The state root is `$TOKENECONOMIZER_HOME`, else `~/.tokeneconomizer`. It is deliberately not a
host's plugin data directory: the CLI runs from the agent's Bash tool, where those variables are
not exported, and one root lets Claude Code and Codex sessions on the same repository sit side
by side (each host keeps its own subdirectory, because a turn envelope learned on one agent says
nothing about another's turn scale).
"""
import hashlib
import os
import re
from dataclasses import dataclass

HOME_ENV = "TOKENECONOMIZER_HOME"


def state_root(env=None):
    """Absolute path of the live-state root."""
    env = os.environ if env is None else env
    root = env.get(HOME_ENV) or os.path.join(os.path.expanduser("~"), ".tokeneconomizer")
    return os.path.abspath(root)


def secure_root(env=None):
    """Keep the state root private to its owner, as the agents keep their own session logs.

    What the plugin stores -- repository paths, the commands sessions ran, costs, error
    tracebacks -- is derived from those logs, and `bin/te` in it is what the slash commands run.
    New files are created private (`te_bootstrap` sets umask 077); a root that an earlier
    version, or another entry point, created with looser permissions is tightened here."""
    root = state_root(env)
    try:
        st = os.stat(root)
        if st.st_mode & 0o077 and st.st_uid == os.getuid():
            os.chmod(root, 0o700)
    except (OSError, AttributeError):
        pass


def launcher_path(env=None):
    return os.path.join(state_root(env), "bin", "te")


def cmd_launcher_path(env=None):
    return launcher_path(env) + ".cmd"


def _write_launcher(path, body, mode=0o700):
    """Write `path` when its text differs -- an installed copy's path changes across updates.
    True when it wrote."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            if fh.read() == body:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def ensure_launcher(env=None):
    """Keep `<state root>/bin/te` pointing at this plugin's `te`. Returns its path, or None.

    A slash command has to name the CLI's path. Claude Code substitutes `${CLAUDE_PLUGIN_ROOT}`
    into a command's text; the Copilot CLI substitutes nothing but `$ARGUMENTS` (1.0.83:
    `${CLAUDE_PLUGIN_ROOT}`, `${PLUGIN_ROOT}`, `${COPILOT_PLUGIN_ROOT}`, `{{plugin_root}}` all
    arrive literally), and its shell exports none of them. So every hook session start writes
    this one-line launcher, the commands fall back to it, and a person on any host has a stable
    `te` to run. The plugin's location comes from the bootstrap (`TE_PLUGIN_ROOT`); the file is
    rewritten only when it changes, as an installed copy's path does across updates.

    On Windows a `te.cmd` is written beside it: cmd and PowerShell run a `.cmd`, not the `sh`
    script, and that is the shell a person works in there. It starts the bootstrap directly and
    passes every argument through."""
    import shlex
    env = os.environ if env is None else env
    plugin = env.get("TE_PLUGIN_ROOT")
    target = os.path.join(plugin, "scripts", "te") if plugin else ""
    if not target or not os.access(target, os.X_OK):
        return None
    path = launcher_path(env)
    body = ("#!/bin/sh\n# Written by the TokenEconomizer plugin at session start: its `te` "
            "command line.\nexec %s \"$@\"\n" % shlex.quote(target))
    _write_launcher(path, body)
    if os.name == "nt":
        _write_launcher(cmd_launcher_path(env),
                        "@echo off\r\n"
                        "rem Written by the TokenEconomizer plugin at session start: its `te` "
                        "command line.\r\n"
                        '"%s" "%s" cli %%*\r\n'
                        % (env.get("TE_PYTHON") or "python",
                           os.path.join(plugin, "scripts", "te_bootstrap.py")))
    return path


def home_dir(env=None):
    """The real path of the person's home directory ("" if there is none)."""
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.realpath(home) if home and home != "~" else ""


def _gitdir_of_file(dot):
    """The directory a `.git` FILE points at (`gitdir: <path>`), or None."""
    try:
        with open(dot, encoding="utf-8") as fh:
            line = fh.read(4096).strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(os.path.dirname(dot), gitdir))
    return gitdir


def is_git_entry(dot):
    """Does the `.git` entry at `dot` make its directory a checkout, as git itself would take it?

    A directory counts when it holds `HEAD`; a file (a linked worktree, a submodule) when its
    `gitdir:` line names a directory that exists. An empty `.git` directory -- left behind by a
    tool, or by an undone `git init` -- is no repository to git, and so none here."""
    if os.path.isdir(dot):
        return os.path.isfile(os.path.join(dot, "HEAD"))
    gitdir = _gitdir_of_file(dot)
    return bool(gitdir) and os.path.isdir(gitdir)


def find_repo_root(start, env=None):
    """The nearest directory at or above `start` that is a git checkout (`is_git_entry`), else
    None.

    The walk honours `GIT_CEILING_DIRECTORIES` as git does -- it never climbs into a listed
    directory -- and treats the home directory as one more ceiling for anything below it: a
    versioned home directory is the repository of a session started in `~` itself, never of
    every unversioned project underneath it."""
    env = os.environ if env is None else env
    ceilings = {os.path.abspath(c) for c in
                (env.get("GIT_CEILING_DIRECTORIES") or "").split(os.pathsep) if c}
    home = home_dir(env)
    cur = os.path.abspath(start or os.getcwd())
    while True:
        if is_git_entry(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or parent in ceilings or (home and os.path.realpath(parent) == home):
            return None
        cur = parent


def _git_common_dir(root):
    """The git directory that holds `config` for a checkout, following worktree indirection."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot
    gitdir = _gitdir_of_file(dot)
    if not gitdir:
        return None
    # A worktree's gitdir is <main>/.git/worktrees/<name>; `commondir` points back at <main>/.git.
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            common = fh.read().strip()
        return os.path.normpath(os.path.join(gitdir, common))
    except OSError:
        return gitdir


_REMOTE_RE = re.compile(r'^\s*\[remote\s+"([^"]+)"\]\s*$')
_URL_RE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$")


def origin_url(root):
    """The `origin` remote URL (or the first remote's), read from the git config; None if none."""
    gitdir = _git_common_dir(root) if root else None
    if not gitdir:
        return None
    try:
        lines = open(os.path.join(gitdir, "config"), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    remotes, current = {}, None
    for line in lines.splitlines():
        m = _REMOTE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        if line.lstrip().startswith("["):
            current = None
            continue
        m = _URL_RE.match(line)
        if current and m and current not in remotes:
            remotes[current] = m.group(1)
    return remotes.get("origin") or (next(iter(remotes.values())) if remotes else None)


def normalize_remote(url):
    """`git@github.com:Owner/Repo.git` and `https://github.com/owner/repo` -> one spelling."""
    u = (url or "").strip()
    u = re.sub(r"^[a-z+]+://", "", u)            # scheme
    u = re.sub(r"^[^@/]+@", "", u)               # user@
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/\d]", u) else u   # scp-style host:path
    u = re.sub(r"\.git$", "", u.rstrip("/"))
    return u.lower()


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "repo").lower()).strip("-")[:40] or "repo"


def digest(canon):
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]


# How a repository was identified, as `RepoId.how` records it.
BY_REMOTE, BY_PATH, BY_DIR = "remote", "path", "dir"


@dataclass(frozen=True)
class RepoId:
    root: str        # the checkout this session runs in
    key: str         # stable directory name for its state
    label: str       # human name, used in rendered manuals ("owner/repo" or the basename)
    remote: str      # normalized remote, "" when the checkout has none
    how: str = ""    # BY_REMOTE, BY_PATH (a checkout with no remote) or BY_DIR (no checkout)


def _main_checkout(root):
    """The directory holding a checkout's shared `.git`: the checkout itself, or for a linked
    worktree the clone it was made from. None when that git directory is not a `.git` (a bare
    repository's worktree, `--separate-git-dir`)."""
    common = _git_common_dir(root)
    if common and os.path.basename(os.path.normpath(common)) == ".git":
        return os.path.dirname(os.path.normpath(common))
    return None


def repo_identity(cwd, env=None):
    """Identify the repository a session in `cwd` belongs to.

    One repository has one key however it is checked out: its normalized `origin` remote when
    it has one (every clone, worktree and re-clone), else the real path of its main checkout (a
    linked worktree shares its clone's), else -- outside any checkout -- the directory itself.
    The key's readable part is the repository's name (the remote's last component, or the main
    checkout's folder), not the folder one particular checkout sits in."""
    found = find_repo_root(cwd, env)
    root = found or os.path.abspath(cwd or os.getcwd())
    remote = normalize_remote(origin_url(root)) if found else ""
    if remote:
        how, canon, name = BY_REMOTE, remote, remote.rstrip("/").rsplit("/", 1)[-1]
        label = "/".join(remote.split("/")[-2:])
    else:
        main = (_main_checkout(root) or _git_common_dir(root) or root) if found else root
        how, canon = (BY_PATH if found else BY_DIR), os.path.realpath(main)
        name = label = os.path.basename(main.rstrip("/"))
    return RepoId(root=root, key=f"{_slug(name)}-{digest(canon)}", label=label or "repo",
                  remote=remote, how=how)


def repo_state_dir(repo, host, env=None):
    """`<root>/repos/<repo-key>/<host>` -- one host's state for one repository."""
    return os.path.join(state_root(env), "repos", repo.key, host)
