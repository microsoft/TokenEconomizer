"""Which repository's state a session belongs to, over time: session pins, the checkout index,
carrying state over from an earlier key, and the listing behind `te repos`.

Three things can put one repository's sessions under several keys, or several repositories'
sessions under one; each is handled here.

  * A session that changes directory. Every hook names the directory the agent is in NOW --
    Claude Code's `cwd` follows the agent's `cd` -- so a session that moves into another
    repository would be recorded under both. The first hook of a session pins it
    (`<root>/sessions/<host>/<id>.json`), and every later hook, and the fold it hands off, uses
    the pinned repository: the one the session started in, which is also where the agent files
    its own session log.
  * A key that changed. Up to 0.1.0 a checkout's folder name was part of the key and any `.git`
    entry made a checkout, and a repository's `origin` can move. The first time a checkout is
    seen (`index.json` remembers the ones already seen) the state its earlier keys hold is sent
    where it belongs (`adopt`); `te repos migrate` does the same for every state directory, and
    runs by itself, once, in the background after an upgrade.
  * Sessions filed under a repository that is not theirs. Each recorded session names the
    directory it ran in; one whose directory now resolves to another repository moves there
    and is folded again, so that repository learns from it (`migrate_dir`).

Nothing is deleted on the way. A state directory merged whole is set aside under
`<root>/merged/`; learned state that gives way to another's is kept beside it as `premerge-*`;
a session that moves keeps its ledger record when its log is gone and it cannot be folded again.
"""
import contextlib
import json
import os
import re
import shutil
import time
import traceback

from ..names import repo_filename
from . import hosts as hosts_mod
from .paths import (BY_PATH, BY_REMOTE, RepoId, _slug, digest, home_dir, is_git_entry,
                    normalize_remote, origin_url, repo_identity, state_root)
from .store import Store

INDEX = "index.json"
LAYOUT = 2                  # the state layout this version writes (1: everything up to 0.1.0)
PIN_MAX_AGE_S = 30 * 86400  # a session pinned this long ago is over (a resume pins anew)
LOCK_WAIT_S = 2.0           # how long a hook waits to carry state over before trying next time
MIGRATE_RESPAWN_S = 600     # a background migration started this recently is still running
_FIELDS = ("root", "key", "label", "remote", "how")
# A fold derives these from session logs, so a merge keeps one side's and folds the other
# side's sessions again; the guard's envelope comes from Stop hooks instead and is concatenated.
LEARNED = ("injection.json", "untool.json", "mechanisms")


def repos_dir(root):
    return os.path.join(root, "repos")


def _repo_dir(root, key):
    return os.path.join(repos_dir(root), key)


def _record_error(root):
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} (repos)\n")
            fh.write(traceback.format_exc())
    except Exception:
        pass


# ---- session pins ---------------------------------------------------------------------------
def _pin_store(root, host):
    return Store(os.path.join(root, "sessions", host))


def _pin_name(session_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:200] + ".json"


def _repo_from(d):
    if not isinstance(d, dict) or not d.get("key") or not d.get("root"):
        return None
    return RepoId(**{k: str(d.get(k) or "") for k in _FIELDS})


def pinned(root, host, session_id):
    """The repository a running session was pinned to, following any merge since; or None.

    Only a merge made AFTER the pin moves it: a key merged away can come back -- a checkout
    whose only state was a stray fragment of another session -- and a session pinned to it
    since then belongs to it, even before it has written anything there."""
    doc = _pin_store(root, host).read(_pin_name(session_id))
    repo = _repo_from(doc)
    if repo is None or os.path.isdir(_repo_dir(root, repo.key)):
        return repo
    redirects = (Store(root).read(INDEX) or {}).get("redirects") or {}
    key, hops, since = repo.key, 0, float(doc.get("at") or 0)
    while key in redirects and hops < 8:
        r = redirects[key]
        if float(r.get("at") or 0) < since:
            break
        key, hops = r.get("to"), hops + 1
    if not key or key == repo.key:
        return repo
    return RepoId(root=repo.root, key=key, label=repo.label, remote=repo.remote, how=repo.how)


def pin(root, host, session_id, repo, cwd):
    _pin_store(root, host).write(_pin_name(session_id), dict(
        {k: getattr(repo, k) for k in _FIELDS}, cwd=cwd, at=time.time()))


def prune_pins(root, host, now=None, max_age_s=PIN_MAX_AGE_S):
    """Remove pins older than `max_age_s`; at most once a day, so a hook pays nothing."""
    st = _pin_store(root, host)
    now = now or time.time()
    marker = st.path(".pruned")
    try:
        if now - os.path.getmtime(marker) < 86400:
            return 0
    except OSError:
        pass
    n = 0
    try:
        for name in os.listdir(st.base):
            p = st.path(name)
            if name.endswith(".json") and now - os.path.getmtime(p) > max_age_s:
                os.remove(p)
                n += 1
        open(marker, "w", encoding="utf-8").close()
    except OSError:
        pass
    return n


# ---- the identity the plugin used up to 0.1.0 (frozen: migration has to find that state) ----
def _legacy_root(start, env):
    env = os.environ if env is None else env
    ceilings = {os.path.abspath(c) for c in
                (env.get("GIT_CEILING_DIRECTORIES") or "").split(os.pathsep) if c}
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or parent in ceilings:
            return None
        cur = parent


def legacy_key(cwd, env=None):
    """The key 0.1.0 gave `cwd`: any `.git` entry made a checkout, the walk did not stop at the
    home directory, and the key's readable part was the checkout's own folder name."""
    root = _legacy_root(cwd, env) or os.path.abspath(cwd)
    remote = normalize_remote(origin_url(root)) if os.path.exists(
        os.path.join(root, ".git")) else ""
    return f"{_slug(os.path.basename(root.rstrip('/')))}-{digest(remote or os.path.realpath(root))}"


# ---- reading one state directory ----------------------------------------------------------------
def _host_dirs(repo_dir):
    try:
        return sorted(h for h in os.listdir(repo_dir)
                      if h in hosts_mod.HOSTS and os.path.isdir(os.path.join(repo_dir, h)))
    except OSError:
        return []


def _sessions(st):
    """{session_id: record} for one agent's state, counting a session known only to the fold."""
    out = {}
    d = st.path("sessions")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for name in names:
        if name.endswith(".json"):
            rec = st.read(os.path.join("sessions", name))
            out[name[:-5]] = rec if isinstance(rec, dict) else {}
    for sid in st.read("folded.json") or {}:
        out.setdefault(sid, {})
    return out


_CWD_RE = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')


def _transcript_cwd(path):
    """The first directory a session log names (every agent's log records where it started)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            m = _CWD_RE.search(fh.read(262144))
        return json.loads(m.group(1)) if m else None
    except (OSError, ValueError, TypeError):
        return None


def session_cwd(rec, folded=None):
    """Where a recorded session started: the first directory its log names, else its record's
    `cwd`. The log comes first because every fragment of a session (0.1.0 filed one wherever
    the agent had moved) shares it, and because 0.1.0 rewrote a record's `cwd` at every
    SessionStart, a compaction's included."""
    return _transcript_cwd(_transcript_of(rec, folded)) or rec.get("cwd") or None


def _transcript_of(rec, folded_entry):
    return rec.get("transcript") or (folded_entry or {}).get("transcript") or ""


def _has_learned(st):
    for name in LEARNED:
        p = st.path(name)
        if os.path.isfile(p) or (os.path.isdir(p) and any(os.scandir(p))):
            return True
    return False


def dir_label(repo_dir):
    """The repository name a state directory was learned under: its `repo.json`, else the name its
    micro-skill state is filed under, else its ledger's."""
    doc = Store(repo_dir).read("repo.json") or {}
    if doc.get("label"):
        return doc["label"]
    for h in _host_dirs(repo_dir):
        st = Store(os.path.join(repo_dir, h))
        for sub in ("envfacts_state", "manual_state"):
            try:
                names = [n for n in os.listdir(st.path("mechanisms", sub)) if n.endswith(".json")]
            except OSError:
                continue
            if names:
                return names[0][:-5].replace("__", "/")
        for rec in reversed(st.read_lines("ledger.jsonl")):
            if rec.get("repo"):
                return rec["repo"]
    return ""


def note_repo(root, repo):
    """Record a repository's identity beside its state, for `te repos` (once it has state)."""
    st = Store(_repo_dir(root, repo.key))
    if not os.path.isdir(st.base):
        return
    doc = st.read("repo.json") or {}
    want = {k: getattr(repo, k) for k in ("key", "label", "remote", "how")}
    if any(doc.get(k) != v for k, v in want.items()):
        st.write("repo.json", dict(doc, **want, updated=time.time()))


# ---- moving sessions between state directories -----------------------------------------------
def _write_lines(st, name, records):
    target = st.path(name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, target)


def _rename_label(mech_dir, label):
    """Mechanism state is filed by repository name; file what moved under the destination's."""
    if not label:
        return
    want = repo_filename(label)
    for sub in ("envfacts_state", "manual_state"):
        d = os.path.join(mech_dir, sub)
        try:
            names = [n for n in os.listdir(d) if n.endswith(".json")]
        except OSError:
            continue
        if len(names) == 1 and names[0] != want + ".json" and \
                not os.path.exists(os.path.join(d, want + ".json")):
            p = os.path.join(d, want + ".json")
            os.replace(os.path.join(d, names[0]), p)
            try:
                with open(p, encoding="utf-8") as fh:
                    doc = json.load(fh)
                if isinstance(doc, dict) and "repo" in doc:
                    doc["repo"] = label
                    with open(p, "w", encoding="utf-8") as fh:
                        json.dump(doc, fh, indent=2)
            except (OSError, ValueError):
                pass
    d = os.path.join(mech_dir, "interface_state")
    try:
        names = [n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))]
    except OSError:
        names = []
    if len(names) == 1 and names[0] != want and not os.path.exists(os.path.join(d, want)):
        os.replace(os.path.join(d, names[0]), os.path.join(d, want))


def _take_learned(src, dst, label):
    """Move `src`'s learned state into `dst`, setting aside whatever `dst` had."""
    aside = None
    for name in LEARNED:
        p = dst.path(name)
        if os.path.isdir(p) and not any(os.scandir(p)):
            os.rmdir(p)
        elif os.path.lexists(p):
            aside = aside or dst.ensure(f"premerge-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(p, os.path.join(aside, name))
    for name in LEARNED:
        if os.path.exists(src.path(name)):
            dst.ensure()
            shutil.move(src.path(name), dst.path(name))
    _rename_label(dst.path("mechanisms"), label)
    return aside


def _merge_applied(src, dst):
    from .denylist import APPLIED, applied_by_path, kept_by_path, write_applied
    d_doc, s_doc = dst.read(APPLIED) or {}, src.read(APPLIED) or {}
    by, kept = applied_by_path(d_doc), kept_by_path(d_doc)
    for path, tools in applied_by_path(s_doc).items():
        by[path] = sorted(set(by.get(path) or []) | set(tools))
    for path, tools in kept_by_path(s_doc).items():
        kept[path] = sorted(set(kept.get(path) or []) | set(tools))
    if by or kept:
        write_applied(dst, by, kept)


def _merge_envelope(src, dst):
    from ..loopguard.envelope import ENVELOPE_HISTORY
    a, b = src.read("envelope.json"), dst.read("envelope.json") or {}
    if not a:
        return
    merged = dict(b or a)
    for k in ("resolved_turns", "resolved_snaps"):
        merged[k] = (list(a.get(k) or []) + list(b.get(k) or []))[-ENVELOPE_HISTORY:]
    dst.write("envelope.json", merged)


@contextlib.contextmanager
def _locks(stores, timeout):
    """Every lock a fold or the guard's envelope takes, on each store; yields whether all were."""
    with contextlib.ExitStack() as stack:
        ok = True
        for st in stores:
            for name in ("fold", "envelope"):
                if not stack.enter_context(st.locked(name, timeout=timeout)):
                    ok = False
                    break
            if not ok:
                break
        yield ok


def _merge_host(src, dst, sids, whole, label="", timeout=LOCK_WAIT_S):
    """Move sessions `sids` of one agent's state `src` into `dst` (both Stores).

    Learned state is not additive, so one side keeps its own: with `whole`, `src`'s if it
    learned from more sessions (`dst`'s is set aside), else always `dst`'s. The sessions the
    surviving learned state never saw are unmarked as folded, so the destination's next session
    start folds them again from their logs; one whose log is gone keeps its ledger record and
    stays folded. Returns {"moved", "refold"}, or None when a lock could not be taken."""
    with _locks((src, dst), timeout) as ok:
        if not ok:
            return None
        s_folded, d_folded = src.read("folded.json") or {}, dst.read("folded.json") or {}
        s_sess, d_sess = _sessions(src), _sessions(dst)
        sids = [s for s in sids if s in s_sess]
        take = whole and _has_learned(src) and (
            not _has_learned(dst) or len(s_folded) > len(d_folded))
        # The sessions the surviving learned state has not seen: never one it folded itself.
        if take:
            unlearned = set(d_folded) - set(s_folded)
            _take_learned(src, dst, label)
        else:
            unlearned = (set(sids) & set(s_folded)) - set(d_folded)
        refold = set()
        for sid in unlearned:
            rec = s_sess.get(sid) if sid in sids else d_sess.get(sid)
            entry = (s_folded if sid in sids else d_folded).get(sid)
            tp = _transcript_of(rec or {}, entry)
            if tp and os.path.exists(tp):
                refold.add(sid)

        incoming = set(sids)
        s_ledger, d_ledger = src.read_lines("ledger.jsonl"), dst.read_lines("ledger.jsonl")
        # One session record per session: 0.1.0 could fold a session it had split over two keys
        # under both, and its cost counts once -- the later fold's, which read the longer log.
        # A refolded session's record is written again by its fold.
        sessions, other = {}, {}
        for r in d_ledger + [r for r in s_ledger if r.get("session_id") in incoming]:
            sid = r.get("session_id")
            if r.get("kind") == "session":
                cur = sessions.get(sid)
                if sid not in refold and (cur is None or float(r.get("folded_at") or 0)
                                          > float(cur.get("folded_at") or 0)):
                    sessions[sid] = r
            else:
                other.setdefault((r.get("kind"), sid, r.get("at")), r)
        ledger = sorted(list(sessions.values()) + list(other.values()),
                        key=lambda r: float(r.get("folded_at") or r.get("at") or 0))
        _write_lines(dst, "ledger.jsonl", ledger)
        folded = {s: v for s, v in d_folded.items() if s not in refold}
        folded.update({s: s_folded[s] for s in incoming if s in s_folded and s not in refold})
        dst.write("folded.json", folded)
        for sid in incoming:
            rec = dict(s_sess.get(sid) or {})
            mine = d_sess.get(sid)
            if mine and float(mine.get("updated") or 0) > float(rec.get("updated") or 0):
                rec = dict(mine)
            if rec:
                if sid in refold:
                    rec["folded"] = False
                dst.write(os.path.join("sessions", f"{sid}.json"), rec)
        for sid in refold - incoming:
            rec = dict(d_sess.get(sid) or {})
            if rec:
                rec["folded"] = False
                dst.write(os.path.join("sessions", f"{sid}.json"), rec)
        for r in src.read_lines("events.jsonl"):
            if r.get("session_id") in incoming:
                dst.append("events.jsonl", r)
        if whole:
            _merge_envelope(src, dst)
            _merge_applied(src, dst)
            if src.read("notices.json") and not dst.read("notices.json"):
                dst.write("notices.json", src.read("notices.json"))
        else:
            _write_lines(src, "ledger.jsonl", [r for r in s_ledger
                                               if r.get("session_id") not in incoming])
            src.write("folded.json", {s: v for s, v in s_folded.items() if s not in incoming})
            for sid in incoming:
                with contextlib.suppress(OSError):
                    os.remove(src.path("sessions", f"{sid}.json"))
        return {"moved": len(incoming), "refold": len(refold)}


def merge_whole(root, src_key, dst_key, label="", timeout=LOCK_WAIT_S):
    """Merge every agent's state under `src_key` into `dst_key` and set `src_key` aside under
    `<root>/merged/`. The caller holds the index lock. Returns {host: counts}, or None."""
    src_dir, dst_dir = _repo_dir(root, src_key), _repo_dir(root, dst_key)
    label = label or dir_label(dst_dir) or dir_label(src_dir)
    out = {}
    for h in _host_dirs(src_dir):
        src, dst = Store(os.path.join(src_dir, h)), Store(os.path.join(dst_dir, h))
        r = _merge_host(src, dst, list(_sessions(src)), whole=True, label=label, timeout=timeout)
        if r is None:
            return None
        out[h] = r
    for name in ("config.json",):           # a repository's own settings, if it had none
        if os.path.exists(os.path.join(src_dir, name)) and \
                not os.path.exists(os.path.join(dst_dir, name)):
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(os.path.join(src_dir, name), os.path.join(dst_dir, name))
    aside = os.path.join(root, "merged", f"{src_key}.{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(aside), exist_ok=True)
    os.replace(src_dir, aside)
    # A session pinned to the old key follows it (`pinned`).
    idx = Store(root).read(INDEX) or {}
    redirects = {k: v for k, v in (idx.get("redirects") or {}).items()
                 if k != dst_key and isinstance(v, dict)}
    redirects[src_key] = {"to": dst_key, "at": time.time()}
    idx["redirects"] = redirects
    Store(root).write(INDEX, idx)
    return out


def _resolve(cwd, env, cache):
    """The repository `cwd` belongs to now; None when the directory is gone (nothing says)."""
    if not cwd or not os.path.isdir(cwd):
        return None
    if cwd not in cache:
        cache[cwd] = repo_identity(cwd, env)
    return cache[cwd]


def plan_dir(root, key, env=None, cache=None):
    """({destination key: {"repo": RepoId, "hosts": {host: [session ids]}}}, here) for the
    sessions recorded under `key`, grouped by the repository each one started in, as it
    resolves now; a session whose directory is gone stays where it is and is not listed.
    `here` says whether any directory a session record names -- where a hook fired -- still
    resolves to `key`: a checkout that only ever saw sessions started elsewhere is still one."""
    cache = {} if cache is None else cache
    groups, here = {}, False
    repo_dir = _repo_dir(root, key)
    for h in _host_dirs(repo_dir):
        st = Store(os.path.join(repo_dir, h))
        folded = st.read("folded.json") or {}
        for sid, rec in _sessions(st).items():
            repo = _resolve(session_cwd(rec, folded.get(sid)), env, cache)
            if repo is not None:
                g = groups.setdefault(repo.key, {"repo": repo, "hosts": {}})
                g["hosts"].setdefault(h, []).append(sid)
            if not here and rec.get("cwd"):
                seen = _resolve(rec["cwd"], env, cache)
                here = seen is not None and seen.key == key
    return groups, here


def _live(root, key, env, cache, idx):
    """Does a checkout that exists now resolve to `key`? (Seen in the index, checked afresh.)"""
    for path, v in (idx.get("checkouts") or {}).items():
        if v.get("key") == key:
            repo = _resolve(path, env, cache)
            if repo is not None and repo.key == key:
                return True
    return False


def migrate_dir(root, key, env=None, timeout=LOCK_WAIT_S, cache=None, live=False):
    """Send each session recorded under `key` to the repository it started in, as that resolves
    now (`plan_dir`).

    Sessions that started in a directory resolving to `key` stay, with those whose directory is
    gone; the rest move out (`_merge_host`). When `key` is no live key any more -- no directory
    its sessions name and no known checkout resolves to it (`live` says one does): a key from
    before the derivation changed, or from a remote that moved -- the whole state directory
    goes to the repository most of its sessions belong to, learned state included
    (`merge_whole`). The caller holds the index lock. Returns {"key", "moves": [...], "into"},
    or None when a lock was busy."""
    out = {"key": key, "moves": [], "into": None}
    if not os.path.isdir(_repo_dir(root, key)):
        return out
    cache = {} if cache is None else cache
    groups, here = plan_dir(root, key, env, cache)
    if key in groups:
        note_repo(root, groups[key]["repo"])
    away = {k: g for k, g in groups.items() if k != key}
    if not away:
        return out
    live = live or here or key in groups or _live(root, key, env, cache,
                                                  Store(root).read(INDEX) or {})
    whole_into = None if live else max(
        away, key=lambda k: sum(len(v) for v in away[k]["hosts"].values()))
    for dst_key, g in sorted(away.items()):
        if dst_key == whole_into:
            continue
        for h, sids in g["hosts"].items():
            r = _merge_host(Store(os.path.join(_repo_dir(root, key), h)),
                            Store(os.path.join(_repo_dir(root, dst_key), h)), sids,
                            whole=False, label=g["repo"].label, timeout=timeout)
            if r is None:
                return None
            out["moves"].append(dict(r, host=h, into=dst_key, cwd=g["repo"].root))
        note_repo(root, g["repo"])
    if whole_into:
        g = away[whole_into]
        r = merge_whole(root, key, whole_into, label=g["repo"].label, timeout=timeout)
        if r is None:
            return None
        out["into"] = whole_into
        note_repo(root, g["repo"])
        out["moves"].extend(dict(c, host=h, into=whole_into, cwd=g["repo"].root)
                            for h, c in r.items())
    return out


# ---- the checkout index ---------------------------------------------------------------------
def _record_moves(idx, done):
    """Count what a migration moved, for the one-time notice (`migration_notice`)."""
    moves = [m for r in done for m in r.get("moves") or ()]
    if moves:
        m = idx.get("migration") or {}
        idx["migration"] = {"at": time.time(), "noticed": False,
                            "sessions": int(m.get("sessions") or 0) * (not m.get("noticed"))
                            + sum(x["moved"] for x in moves),
                            "merged": int(m.get("merged") or 0) * (not m.get("noticed"))
                            + sum(1 for r in done if r.get("into"))}
    return idx


def _candidates(root, repo, env, idx):
    """State directories that may hold this checkout's sessions under another key."""
    keys = []
    if repo.key not in (idx.get("checked") or {}):
        keys.append(repo.key)                     # sessions of other repositories filed here
    keys.append(legacy_key(repo.root, env))
    prev = ((idx.get("checkouts") or {}).get(os.path.realpath(repo.root)) or {}).get("key")
    if prev:
        keys.append(prev)
    if repo.how == BY_REMOTE:                    # other clones' and worktrees' folder names
        tail = "-" + repo.key.rsplit("-", 1)[-1]
        try:
            keys.extend(sorted(n for n in os.listdir(repos_dir(root)) if n.endswith(tail)))
        except OSError:
            pass
    seen, out = set(), []
    for k in keys:
        if k not in seen and os.path.isdir(_repo_dir(root, k)):
            seen.add(k)
            out.append(k)
    return out


def _prune_index(idx, now):
    co = idx.get("checkouts") or {}
    idx["checkouts"] = {p: v for p, v in co.items()
                        if os.path.isdir(p) or now - float(v.get("seen") or 0) < 30 * 86400}
    return idx


def adopt(root, repo, env=None, timeout=LOCK_WAIT_S):
    """The first time a checkout is seen under its key, send the state its earlier keys hold
    where it belongs. Cheap after that: one read of `index.json`. Returns the migrations run."""
    real = os.path.realpath(repo.root)
    ist = Store(root)
    idx = ist.read(INDEX) or {}
    if ((idx.get("checkouts") or {}).get(real) or {}).get("key") == repo.key:
        return []
    done = []
    with ist.locked("index", timeout=timeout) as got:
        if not got:
            return []
        idx = ist.read(INDEX) or {}
        if ((idx.get("checkouts") or {}).get(real) or {}).get("key") == repo.key:
            return []
        ok, cache = True, {}
        for key in _candidates(root, repo, env, idx):
            r = migrate_dir(root, key, env, timeout=timeout, cache=cache, live=key == repo.key)
            if r is None:
                ok = False
                continue
            done.append(r)
            idx = ist.read(INDEX) or {}
            if os.path.isdir(_repo_dir(root, key)):
                idx.setdefault("checked", {})[key] = time.time()
            ist.write(INDEX, idx)
        note_repo(root, repo)
        if ok:
            idx = _record_moves(ist.read(INDEX) or {}, done)
            idx.setdefault("checkouts", {})[real] = {"key": repo.key, "seen": time.time()}
            idx.setdefault("checked", {})[repo.key] = time.time()
            ist.write(INDEX, _prune_index(idx, time.time()))
    return done


def migrate_all(root, env=None, timeout=60.0):
    """`te repos migrate`: run `migrate_dir` over every state directory not yet checked, and mark
    the state root as on this layout. Returns the migrations run, or None if the index was busy."""
    ist = Store(root)
    done = []
    with ist.locked("index", timeout=timeout) as got:
        if not got:
            return None
        cache = {}
        try:
            keys = sorted(os.listdir(repos_dir(root)))
        except OSError:
            keys = []
        complete = True
        for key in keys:
            idx = ist.read(INDEX) or {}
            if key in (idx.get("checked") or {}) or not os.path.isdir(_repo_dir(root, key)):
                continue
            r = migrate_dir(root, key, env, timeout=timeout, cache=cache)
            if r is None:
                complete = False
                continue
            done.append(r)
            idx = ist.read(INDEX) or {}
            if os.path.isdir(_repo_dir(root, key)):
                idx.setdefault("checked", {})[key] = time.time()
            ist.write(INDEX, idx)
        idx = _record_moves(ist.read(INDEX) or {}, done)
        if complete:
            idx["layout"] = LAYOUT
        ist.write(INDEX, idx)
    return done


def merge(root, src_key, dst_repo, timeout=60.0):
    """`te repos merge`: put one state directory into another, whatever its sessions' directories
    say -- for state whose checkouts are gone. `dst_repo` is a RepoId, or a key."""
    ist = Store(root)
    with ist.locked("index", timeout=timeout) as got:
        if not got:
            return None
        dst_key = dst_repo if isinstance(dst_repo, str) else dst_repo.key
        if not isinstance(dst_repo, str):
            note_repo(root, dst_repo)
        label = "" if isinstance(dst_repo, str) else dst_repo.label
        return merge_whole(root, src_key, dst_key, label=label, timeout=timeout)


def needs_migration(root):
    """Whether this state root still holds state laid out by an earlier version."""
    if (Store(root).read(INDEX) or {}).get("layout", 0) >= LAYOUT:
        return False
    try:
        return any(os.path.isdir(_repo_dir(root, k)) for k in os.listdir(repos_dir(root)))
    except OSError:
        return False


def start_migration(ctx):
    """Once after an upgrade: migrate every state directory, in the background (inline under
    `TE_FOLD_SYNC=1`, for tests). A marker keeps concurrent session starts from piling up."""
    root = ctx.root
    if not needs_migration(root):
        return False
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        run_migration(root, ctx.env)
        return True
    marker = os.path.join(root, ".migrating")
    try:
        if time.time() - os.path.getmtime(marker) < MIGRATE_RESPAWN_S:
            return False
    except OSError:
        pass
    open(marker, "w", encoding="utf-8").close()
    return ctx.spawn(["repos", "migrate", "--quiet"], "migrate.log")


def run_migration(root, env=None):
    """`migrate_all`, then fold the sessions it moved, each where it now belongs."""
    done = migrate_all(root, env)
    if done is not None:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(root, ".migrating"))
    if done:
        refold_moved(done, env)
    return done


def migration_notice(root, command="te repos"):
    """Once, after a migration moved anything: a sentence saying so (else None)."""
    ist = Store(root)
    m = (ist.read(INDEX) or {}).get("migration") or {}
    if not m or m.get("noticed"):
        return None
    with ist.locked("index", timeout=LOCK_WAIT_S) as got:
        if not got:
            return None
        idx = ist.read(INDEX) or {}
        m = idx.get("migration") or {}
        if m.get("noticed"):
            return None
        m["noticed"] = True
        ist.write(INDEX, idx)
    return (f"TokenEconomizer: state recorded by an earlier version is now filed by repository "
            f"({m.get('sessions', 0)} session(s) moved, {m.get('merged', 0)} directory(ies) "
            f"merged; nothing deleted) -- see `{command}`.")


def refold_moved(done, env=None):
    """Fold, where they landed, the sessions a migration unmarked as folded."""
    from .context import LiveContext
    from . import fold
    targets = {(m["host"], m["cwd"]) for r in done or () for m in r.get("moves") or ()
               if m.get("refold") and m.get("cwd")}
    for host, cwd in sorted(targets):
        try:
            ctx = LiveContext.build(hosts_mod.HOSTS[host](), cwd, env)
            fold.spawn_fold(ctx, fold.pending_sessions(ctx))
        except Exception:
            _record_error(state_root(env))


def settle(root, host, repo, cwd, env=None, session_id=None):
    """Make `repo` the home of a hook or command in `cwd`: carry over what its earlier keys hold
    (`adopt`), then pin the session, if there is one, to it. Carrying over fails open -- an
    error there is logged, never raised. Returns whether any state moved."""
    moved = []
    try:
        moved = adopt(root, repo, env)
    except Exception:
        _record_error(root)
    if session_id:
        pin(root, host, session_id, repo, cwd)
    return any(r.get("moves") for r in moved)


# ---- what `te repos` shows -------------------------------------------------------------------
def how_words(how, remote=""):
    if how == BY_REMOTE:
        return f"by its origin remote ({remote})"
    if how == BY_PATH:
        return "by its path (a git repository with no remote)"
    return "by its directory (not a git repository)"


def describe(repo):
    """How a repository was identified, in words."""
    return "identified " + how_words(repo.how, repo.remote)


def ignored_git_entries(cwd, env=None):
    """`.git` entries between `cwd` and its checkout (or the filesystem root) that git would not
    take for a repository, and so neither does `find_repo_root`."""
    out = []
    cur = os.path.abspath(cwd)
    while True:
        dot = os.path.join(cur, ".git")
        if is_git_entry(dot):
            return out
        if os.path.lexists(dot):
            out.append(dot)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def listing(root, env=None, current=None):
    """One row per state directory: identity, agents and sessions, checkouts, notes."""
    idx = Store(root).read(INDEX) or {}
    checkouts = {}
    for path, v in (idx.get("checkouts") or {}).items():
        checkouts.setdefault(v.get("key"), []).append(path)
    home = home_dir(env)
    rows = []
    try:
        keys = sorted(os.listdir(repos_dir(root)))
    except OSError:
        keys = []
    for key in keys:
        d = _repo_dir(root, key)
        if not os.path.isdir(d):
            continue
        doc = Store(d).read("repo.json") or {}
        agents, cwds, last = {}, set(), 0.0
        for h in _host_dirs(d):
            st = Store(os.path.join(d, h))
            sess = _sessions(st)
            agents[h] = len(sess)
            for name in ("ledger.jsonl", "sessions"):
                try:
                    last = max(last, os.path.getmtime(st.path(name)))
                except OSError:
                    pass
            for rec in sess.values():
                if rec.get("cwd"):
                    cwds.add(rec["cwd"])
        notes = []
        if key not in (idx.get("checked") or {}) and idx.get("layout", 0) < LAYOUT:
            notes.append("recorded by an earlier version; `te repos migrate` sorts it out "
                         "(it runs by itself at the next session start)")
        if home and any(os.path.realpath(p) == home for p in checkouts.get(key, [])):
            notes.append("your home directory: holds only sessions started in ~ itself")
        rows.append({"key": key, "label": doc.get("label") or dir_label(d) or key,
                     "how": doc.get("how", ""), "remote": doc.get("remote", ""),
                     "agents": agents, "last": last, "state": d,
                     "checkouts": sorted(checkouts.get(key, [])), "cwds": sorted(cwds),
                     "current": bool(current and current.key == key), "notes": notes})
    try:
        merged = len(os.listdir(os.path.join(root, "merged")))
    except OSError:
        merged = 0
    rows.sort(key=lambda r: (not r["current"], -r["last"]))
    return rows, merged


def home_git_note(env=None):
    """A sentence about a `.git` in the home directory, or None when there is none."""
    home = home_dir(env)
    if not home or not os.path.lexists(os.path.join(home, ".git")):
        return None
    if is_git_entry(os.path.join(home, ".git")):
        return (f"{home} is a git repository; it is the repository of sessions started there, "
                f"not of the unversioned directories below it")
    return (f"{home}/.git exists but is not a git repository (git ignores it, and so does "
            f"TokenEconomizer)")
