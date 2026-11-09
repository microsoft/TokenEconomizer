"""`media=audio`: transcripts of the audio files a prompt names, transcribed on the machine.

An agent cannot read audio: Claude Code's Read rejects an `.m4a` or `.aiff` as binary before any
hook sees the call, so a prompt such as "implement what the PM asks in notes/memo.m4a" sends the
agent hunting for a transcriber -- turns, tokens and usually a failed task. With `media=audio`
the prompt's hook finds the audio paths it names, transcribes each with Apple's Speech framework
(`plugin/scripts/te-transcribe.swift`, macOS 26+; no network, no account, no model call billed),
and adds the transcripts to the prompt's context, as the repository map is added.

The helper is a Swift program built once, with `swiftc`, into `<state root>/bin/`: by
`te media setup`, or in the background by the first prompt that names an audio file (that prompt
goes without a transcript). Without `swiftc` or on another OS the mechanism does nothing.
Transcripts are cached by the file's content hash, so naming the same memo again costs nothing.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

from . import paths

AUDIO_EXT = ("aiff", "aif", "wav", "m4a", "mp3", "caf", "aac", "flac")
# A path the prompt names: a word of path characters ending in an audio extension, which may be
# quoted or backticked; not part of a URL.
AUDIO_RE = re.compile(r"(?<![\w/.:~-])((?:~/|/|\./)?[\w.~/-]*[\w-]\.(?:%s))(?!\w|\.\w)"
                      % "|".join(AUDIO_EXT), re.I)
MAX_FILES = 3
MAX_BYTES = 50 * 1024 * 1024
TRANSCRIBE_TIMEOUT_S = 15
MAX_CHARS = 6000


def audio_paths(prompt, cwd):
    """The existing audio files `prompt` names, resolved against `cwd`, in order, at most
    MAX_FILES."""
    out = []
    for rel in AUDIO_RE.findall(prompt or ""):
        p = os.path.normpath(os.path.join(cwd, os.path.expanduser(rel)))
        if p not in out and os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES:
            out.append(p)
        if len(out) >= MAX_FILES:
            break
    return out


def source_path():
    """The helper's Swift source in the plugin directory (the checkout or the bundle)."""
    plugin = os.environ.get("TE_PLUGIN_ROOT")
    candidates = [os.path.join(plugin, "scripts", "te-transcribe.swift")] if plugin else []
    core = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates += [os.path.join(core, "plugin", "scripts", "te-transcribe.swift"),
                   os.path.join(os.path.dirname(core), "scripts", "te-transcribe.swift")]
    return next((c for c in candidates if os.path.isfile(c)), None)


def helper_path(env=None):
    return os.path.join(paths.state_root(env), "bin", "te-transcribe")


def helper_ready(env=None):
    h = helper_path(env)
    return os.path.isfile(h) and os.access(h, os.X_OK)


def supported():
    return sys.platform == "darwin" and shutil.which("swiftc") is not None


def build_helper(env=None, timeout=300):
    """Compile the helper; (ok, message)."""
    if not supported():
        return False, "needs macOS with swiftc (Xcode or the Command Line Tools)"
    src = source_path()
    if not src:
        return False, "te-transcribe.swift not found in the plugin"
    out = helper_path(env)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = "%s.%d.tmp" % (out, os.getpid())
    try:
        r = subprocess.run(["swiftc", "-O", "-parse-as-library", src, "-o", tmp],
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"swiftc failed to run: {type(e).__name__}"
    if r.returncode != 0 or not os.path.isfile(tmp):
        return False, "swiftc: " + (r.stderr or r.stdout or "").strip()[-300:]
    os.replace(tmp, out)
    return True, out


def transcript(path, env=None):
    """The transcript of `path` ("" when the helper is missing or fails), cached by content."""
    try:
        with open(path, "rb") as fh:
            key = hashlib.sha256(fh.read()).hexdigest()[:24]
    except OSError:
        return ""
    cache = os.path.join(paths.state_root(env), "media", key + ".txt")
    try:
        with open(cache, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        pass
    if not helper_ready(env):
        return ""
    try:
        r = subprocess.run([helper_path(env), path], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=TRANSCRIBE_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (r.stdout or "").strip()[:MAX_CHARS]
    if r.returncode != 0 or not text:
        return ""
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def for_prompt(prompt, cwd, env=None):
    """(block, info) for the prompt's context: the transcripts of the audio it names. `info`
    says what happened, for the plugin's log: files found, transcribed, and whether the helper
    still has to be built."""
    files = audio_paths(prompt, cwd)
    info = {"files": len(files), "transcribed": 0, "needs_helper": bool(files) and not
            helper_ready(env)}
    parts = []
    for p in files:
        text = transcript(p, env)
        if text:
            info["transcribed"] += 1
            rel = os.path.relpath(p, cwd) if p.startswith(os.path.abspath(cwd)) else p
            parts.append(f"[On-device transcript of {rel} (Apple Speech framework; it may "
                         f"contain recognition errors)]\n{text}")
    return "\n\n".join(parts), info
