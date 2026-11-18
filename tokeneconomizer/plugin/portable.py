"""Portable, diff-friendly repository micro-skills (`<repo>/.tokeneconomizer/facts.json`).

Confirmed facts learned in a repository belong to the repository rather than to one machine or
agent. This module stores only sanitized, declarative facts under `<repo>/.tokeneconomizer/`,
refusing symlinks and dropping home-directory paths or secret tokens before writing or reading.
"""
import hashlib
import json
import os
import re

PORTABLE_DIR = ".tokeneconomizer"
FACTS_FILE = "facts.json"

_HOME_PATH_RE = re.compile(r"(?:/Users/[^/\s`'\"]+|/home/[^/\s`'\"]+|[A-Za-z]:[/\\]Users[/\\][^/\\\s`'\"]+)")
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|gh[po]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|xox[bp]-[A-Za-z0-9-]{12,}"
    r"|Bearer\s+[A-Za-z0-9._-]{12,}"
    r"|\b(?:api[_-]?key|secret|password|token)\s*=\s*\S+)",
    re.I,
)


def facts_path(repo_root):
    return os.path.join(repo_root, PORTABLE_DIR, FACTS_FILE)


def is_active(repo_root, cfg):
    mode = getattr(cfg, "portable_state", "auto")
    if mode == "off" or not repo_root:
        return False
    if mode == "on":
        return True
    return os.path.isfile(facts_path(repo_root))


def sanitize_claim(text, env=None):
    """Return a clean single-line claim safe to commit, or None if it leaks paths or secrets."""
    line = " ".join(str(text or "").split())
    if not line or len(line) > 300:
        return None
    env_map = os.environ if env is None else env
    homes = {os.path.expanduser("~"), env_map.get("HOME") or "", env_map.get("USERPROFILE") or ""}
    for h in homes:
        if len(h) > 3 and (h in line or h.replace("\\", "/") in line):
            return None
    if _HOME_PATH_RE.search(line) or _SECRET_RE.search(line):
        return None
    return line


def _guard_portable_target(repo_root, path):
    root = os.path.realpath(repo_root)
    pdir = os.path.dirname(path)
    if os.path.islink(pdir) or os.path.islink(path):
        raise RuntimeError(f"refusing to touch {path}: symlinked portable state path")
    os.makedirs(pdir, exist_ok=True)
    parent = os.path.realpath(pdir)
    if os.path.commonpath([root, parent]) != root:
        raise RuntimeError(f"refusing to write {path}: path resolves outside the repository")


def _open_tmp(tmp):
    if os.path.lexists(tmp):
        os.unlink(tmp)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o644)
    return os.fdopen(fd, "w", encoding="utf-8")


def _write_text_safe(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with _open_tmp(tmp) as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_facts(repo_root, env=None):
    """Read `{fact_id: {"claim": str}}` from `<repo>/.tokeneconomizer/facts.json`, or `{}`."""
    path = facts_path(repo_root)
    if not os.path.exists(path) or os.path.islink(path) or os.path.islink(os.path.dirname(path)):
        return {}
    root = os.path.realpath(repo_root)
    if os.path.commonpath([root, os.path.realpath(os.path.dirname(path))]) != root:
        return {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    raw = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for fid, entry in sorted(raw.items()):
        claim = entry.get("claim") if isinstance(entry, dict) else entry
        clean = sanitize_claim(claim, env=env)
        if clean:
            out[str(fid)] = {"claim": clean}
    return out


def extract_facts(inj, env=None):
    """Extract sanitized `{fact_id: {"claim": str}}` from a cached `injection.json` dict."""
    if not isinstance(inj, dict):
        return {}
    bullets = [ln[2:].strip() for ln in (inj.get("rendered") or "").splitlines()
               if ln.startswith("- ") and ln[2:].strip()]
    out = {}
    used_bullets = set()
    facts_meta = inj.get("facts") or {}
    if isinstance(facts_meta, dict):
        for fid, meta in sorted(facts_meta.items()):
            if not isinstance(meta, dict) or not meta.get("inject"):
                continue
            raw_claim = str(meta.get("claim") or "").strip()
            prefix = raw_claim[:-1] if raw_claim.endswith("\u2026") else raw_claim
            matched = next((b for b in bullets if prefix and b.startswith(prefix)), raw_claim)
            if matched in bullets:
                used_bullets.add(matched)
            clean = sanitize_claim(matched, env=env)
            if clean:
                out[str(fid)] = {"claim": clean}
    for b in bullets:
        if b in used_bullets:
            continue
        clean = sanitize_claim(b, env=env)
        if clean:
            fid = "fact:" + hashlib.sha1(clean.encode("utf-8")).hexdigest()[:10]
            out[fid] = {"claim": clean}
    return out


def write_facts(repo_root, facts_map, env=None):
    """Atomically write sanitized `{fact_id: {"claim": str}}` to `<repo>/.tokeneconomizer/facts.json`."""
    path = facts_path(repo_root)
    _guard_portable_target(repo_root, path)
    clean_map = {}
    for fid, rec in sorted((facts_map or {}).items()):
        claim = rec.get("claim") if isinstance(rec, dict) else rec
        clean = sanitize_claim(claim, env=env)
        if clean:
            clean_map[str(fid)] = {"claim": clean}
    body = json.dumps({"version": 1, "facts": clean_map}, indent=2, sort_keys=True) + "\n"
    _write_text_safe(path, body)
    return path, clean_map


def merge_rendered(repo_label, local_rendered, portable_facts, forgotten_ids=(),
                   max_facts=4, max_tokens=150):
    """Combine `local_rendered` with sanitized `portable_facts` not forgotten locally."""
    if not portable_facts:
        return local_rendered or ""
    lines = (local_rendered or "").splitlines()
    header = next((ln for ln in lines if ln.startswith("#")), "")
    bullets = [ln[2:].strip() for ln in lines if ln.startswith("- ") and ln[2:].strip()]
    seen = set(bullets)
    forgotten = set(forgotten_ids or ())
    max_chars = max(80, int(max_tokens or 150) * 4)
    limit = max(1, int(max_facts or 4))
    head = header or f"# environment facts learned from earlier sessions in {repo_label}"
    for fid, rec in sorted(portable_facts.items()):
        if fid in forgotten or len(bullets) >= limit:
            continue
        claim = str((rec or {}).get("claim") or "").strip()
        if not claim or claim in seen:
            continue
        trial = head + "\n" + "\n".join(f"- {b}" for b in bullets + [claim]) + "\n"
        if len(trial) > max_chars and bullets:
            continue
        bullets.append(claim)
        seen.add(claim)
    if not bullets:
        return ""
    return head + "\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
