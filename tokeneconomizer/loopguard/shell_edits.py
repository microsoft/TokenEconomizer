"""Edits made through the shell, which an edit-tool counter does not see.

`SpiralState` counts an edit only when an edit TOOL makes it (Edit/Write/str_replace). An agent
that edits through Bash -- `sed -i`, `perl -pi`, a redirect or `tee` into a source file, an
inline Python script that writes the file back, `patch`/`git apply`, Codex's `apply_patch` fed a
patch on stdin -- looked like one that had edited nothing, and a judge reading the digest was told
no file had been edited when four had.
`shell_edit_targets` reads those writes off the command, and `fold_shell_edits` counts them
after the regular update, so the call still counts once.
"""
import re
import shlex

from .state import basename

# `<<<` is a here-string: its word is the input itself, so no body follows on the next lines.
_HEREDOC_START = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_OPS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"}
_OUT = {">", ">>", ">|", "&>", "&>>"}                  # redirects that write their operand
_SKIP_OPERAND = {"<", "<<", "<<<", "<&", ">&", "<>"}   # input, heredoc tags, fd duplication
# Redirect and `tee` targets count only when they look like source, so `> out.log` or
# `| tee /tmp/run.txt` -- capturing output -- is not mistaken for an edit.
_SOURCE_FILE = re.compile(
    r"\.(?:py|pyi|pyx|pxd|js|jsx|mjs|cjs|ts|tsx|rs|go|java|kt|kts|scala|c|cc|cpp|cxx|h|hh|hpp|"
    r"rb|php|cs|swift|m|mm|sh|bash|zsh|lua|jl|ex|exs|erl|hs|ml|clj|sql|toml|ya?ml|json|cfg|ini|"
    r"conf|md|rst|html?|css|scss|vue|svelte|xml|gradle|properties|in)$", re.I)
_PYTHON = re.compile(r"(?:^|[\s/;&|(])python\d?(?:\.\d+)?\b")
_PY_OPEN_LIT = re.compile(r"open\(\s*(['\"])([^'\"\n]+)\1\s*,\s*(['\"])([^'\"]*)\3")
_PY_OPEN_VAR = re.compile(r"open\(\s*([A-Za-z_]\w*)\s*,\s*(['\"])([^'\"]*)\2")
_PY_WRITE_PATH_LIT = re.compile(
    r"(?:pathlib\.)?Path\(\s*(['\"])([^'\"\n]+)\1\s*\)\.write_(?:text|bytes)\(")
_PY_WRITE_PATH_VAR = re.compile(r"\b([A-Za-z_]\w*)\.write_(?:text|bytes)\(")
# Codex's patch format, as `apply_patch <<'PATCH'` takes it on stdin (seen live on 0.163.0, the
# edit and the test run in one command); `trace.codex` reads the same lines from a session log.
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)


def _not_scratch(path):
    return bool(path) and not path.startswith(("/dev/", "/proc/", "/tmp/"))


def _unquote(t):
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t


def _split_heredocs(cmd):
    """(the command's shell text without heredoc bodies, the bodies)."""
    shell, bodies, tag, body = [], [], None, []
    for line in cmd.split("\n"):
        if tag is not None:
            if line.strip() == tag:
                bodies.append("\n".join(body))
                tag, body = None, []
            else:
                body.append(line)
            continue
        shell.append(line)
        m = _HEREDOC_START.search(line)
        if m:
            tag = m.group(2)
    if tag is not None:
        bodies.append("\n".join(body))
    return "\n".join(shell), bodies


def _tokens(text):
    """Shell words and operators. Quotes stay on quoted words, so a quoted `'>'` or `'|'` is
    never read as an operator; ValueError on unbalanced quotes."""
    lex = shlex.shlex(text, posix=False, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def _commands(shell):
    """(simple commands as unquoted word lists, the files their redirects write)."""
    try:
        streams = [_tokens(shell)]
    except ValueError:                          # one bad line must not hide the others
        streams = []
        for line in shell.split("\n"):
            try:
                streams.append(_tokens(line) + ["\n"])
            except ValueError:
                continue
    cmds, writes = [], []
    for toks in streams:
        cur, i = [], 0
        while i < len(toks):
            t = toks[i]
            if t in _OPS:
                if cur:
                    cmds.append(cur)
                cur = []
            elif t in _OUT or t in _SKIP_OPERAND:
                if cur and cur[-1].isdigit():   # `2>`: the fd number is not an operand
                    cur.pop()
                if t in _OUT and i + 1 < len(toks) and toks[i + 1] not in _OPS:
                    writes.append(_unquote(toks[i + 1]))
                i += 1
            else:
                cur.append(_unquote(t))
            i += 1
        if cur:
            cmds.append(cur)
    return cmds, writes


def _inplace_files(tokens, inplace_flag, script_flag):
    """File operands of an in-place editor (`sed -i`, `perl -pi`), or [] if not in place.
    `script_flag(t)` says where an option puts the script: "next" for the following word
    (`-e`, `--file`), "inline" for the option itself (`-e's/a/b/'`, `--expression=...`)."""
    if not any(inplace_flag(t) for t in tokens[1:]):
        return []
    files, have_script, skip = [], False, False
    for t in tokens[1:]:
        if skip:
            skip, have_script = False, True
            continue
        if not t or t.startswith("-"):
            where = script_flag(t) if t else None
            if where == "next":
                skip = True                     # the next token is the script
            elif where == "inline":
                have_script = True
            continue
        if not have_script:
            have_script = True                  # the first operand is the script
            continue
        files.append(t)
    return files


def _short_script_flag(t, script_letters, stop_letters):
    """Where a short-option cluster such as `-ne`, `-pe's/a/b/'` or `-i.bak` puts the script.
    A letter in `stop_letters` takes the rest of the word as its own value (`-i.bak`)."""
    if t.startswith("--") or len(t) < 2:
        return None
    for k, c in enumerate(t[1:], start=2):
        if c in stop_letters:
            return None
        if c in script_letters:
            return "inline" if t[k:] else "next"
    return None


def _sed_script_flag(t):
    if t in ("--expression", "--file"):
        return "next"
    if t.startswith(("--expression=", "--file=")):
        return "inline"
    return _short_script_flag(t, "ef", "i")


def _perl_script_flag(t):
    return _short_script_flag(t, "eE", "iIMmxdDC")   # `-i.bak`, `-Mmod`, `-Idir` ...


def _py_writes(text):
    """Paths an inline Python script writes; "(script)" for a write whose path this cannot
    resolve."""
    out = []
    for m in _PY_OPEN_LIT.finditer(text):
        if set(m.group(4)) & set("wax+"):
            out.append(m.group(2))
    variables = [m.group(1) for m in _PY_OPEN_VAR.finditer(text) if set(m.group(3)) & set("wax+")]
    out += [m.group(2) for m in _PY_WRITE_PATH_LIT.finditer(text)]
    variables += [m.group(1) for m in _PY_WRITE_PATH_VAR.finditer(text)]
    for var in dict.fromkeys(variables):
        found = re.findall(rf"\b{re.escape(var)}\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?(['\"])"
                           rf"([^'\"\n]+)\1", text)
        loop = re.search(rf"\bfor\s+{re.escape(var)}\s+in\s+[\[(]([^\])]*)[\])]", text)
        paths = [p for _, p in found]
        if loop:
            paths += re.findall(r"['\"]([^'\"\n]+)['\"]", loop.group(1))
        out += paths or ["(script)"]
    return out


# git's own options that take the next word as their value (`git -C repo apply x.diff`).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                   "--attr-source"}


def _git_args(tokens):
    """The words of a git command from its subcommand on, past git's own options such as
    `-C <dir>`, `-c <name>=<value>` or `--no-pager`; [] for any other command."""
    if tokens[0].rsplit("/", 1)[-1] != "git":
        return []
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _GIT_VALUE_OPTS else 1
    return tokens[i:]


def _restored(tokens):
    """The paths a `git checkout -- <path>` or `git restore <path>` puts back; [] for any other
    command."""
    args = _git_args(tokens)
    if len(args) < 2 or args[0] not in ("checkout", "restore"):
        return []
    rest = (args[args.index("--") + 1:] if "--" in args
            else args[1:] if args[0] == "restore" else [])
    return [t for t in rest if not t.startswith("-")]


def _bare(cmds):
    """Each command with the `sudo`, `env`, `VAR=1` ... before it dropped; empty ones left out."""
    out = []
    for tokens in cmds:
        while tokens and (tokens[0] in ("sudo", "command", "env", "time", "nohup")
                          or re.match(r"^\w+=", tokens[0])):
            tokens = tokens[1:]
        if tokens:
            out.append(tokens)
    return out


def shell_revert_targets(cmd):
    """Basenames of the files a shell command puts back with `git checkout --` or `git
    restore`: `shell_edit_targets` counts them as written, which they are; the loop guard counts
    them as edits only for a file the turn edited."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    shell, _ = _split_heredocs(cmd)
    cmds, _ = _commands(shell)
    names = [basename(p) for tokens in _bare(cmds) for p in _restored(tokens) if _not_scratch(p)]
    return list(dict.fromkeys(n for n in names if n))


def shell_edit_targets(cmd):
    """Basenames of the files a shell command writes, in order, without repeats."""
    if not isinstance(cmd, str) or not cmd.strip():
        return []
    shell, bodies = _split_heredocs(cmd)
    cmds, writes = _commands(shell)
    found = [w for w in writes if _SOURCE_FILE.search(w)]
    for tokens in _bare(cmds):
        head = tokens[0].rsplit("/", 1)[-1]
        if head == "sed":
            found += _inplace_files(
                tokens, lambda t: bool(re.match(r"^(?:-[a-zA-Z]*i|--in-place)", t)),
                _sed_script_flag)
        elif head == "perl":
            found += _inplace_files(tokens, lambda t: bool(re.match(r"^-[a-zA-Z]*i", t)),
                                    _perl_script_flag)
        elif head == "tee":
            found += [t for t in tokens[1:] if not t.startswith("-") and _SOURCE_FILE.search(t)]
        elif head == "apply_patch":
            found += _PATCH_FILE.findall("\n".join(bodies + [" ".join(tokens[1:])])) or ["(patch)"]
        elif head == "patch" or _git_args(tokens)[:1] == ["apply"]:
            found.append("(patch)")
        else:
            found += _restored(tokens)
    if _PYTHON.search(shell):
        found += _py_writes("\n".join([shell] + bodies))
    names = [p if p.startswith("(") else basename(p)
             for p in found if p.startswith("(") or _not_scratch(p)]
    return list(dict.fromkeys(n for n in names if n))


def fold_shell_edits(st, names):
    """Count `names` as edits in a SpiralState, and say so in its action log."""
    for b in names:
        st.edit_order.append(b)
        st.edit_counts[b] = st.edit_counts.get(b, 0) + 1
    if names and st.recent:
        shown = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        st.recent[-1] = f"{st.recent[-1]}  [edits {shown}]"
