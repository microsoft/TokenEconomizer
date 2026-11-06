"""The lifecycle every learned fact goes through, whoever proposed it.

A fact is a CANDIDATE until it is confirmed on K_PROMOTE tasks -- tasks that edited different
files, for a fact about the code (stable across the repository's history, not one task's
coincidence); any K_PROMOTE tasks, for a fact about the environment, which does not change with
the files a task edits (`drift=False`). It is then ACTIVE until a
recency-weighted score, with contradictions weighted LAMBDA times a confirmation, falls below the
floor -- or at once, for a high-stakes fact, on one clean contradiction. An ACTIVE fact is
injected only while it clears the FIRING GATE: the waste it prevents occurred (or, once injected,
kept being prevented) on at least FIRE_GATE of the tasks that exercised it.
"""
import json
import os
import time

# ---- lifecycle knobs (the validated replay constants) ----------------------
K_PROMOTE   = 3      # confirmations (drifted, where drift applies) to promote -> ACTIVE
GAMMA       = 0.8    # recency decay per task-ago
LAMBDA      = 5.0    # contradiction weight (asymmetry: fail toward retirement)
SCORE_FLOOR = 0.0    # retire ACTIVE when recency score < floor
HARD_KILL   = True   # high-stakes fact: single clean contradiction retires now
FIRE_GATE   = 0.40   # inject only if firing rate on this repo >= 40%
FIRE_MIN_N  = 4      # ...measured over at least this many exercised tasks


def edited_pyfiles(calls):
    fs = set()
    for tool, args, _ in calls:
        if tool in ("edit", "multiedit", "write", "create", "str_replace_editor"):
            p = args.get("file_path") or args.get("path") or ""
            if p.endswith(".py"):
                fs.add(os.path.basename(p))
        elif tool == "apply_patch":
            for p in args.get("paths") or ():
                if p.endswith(".py"):
                    fs.add(os.path.basename(p))
    return fs


def edited_files(calls):
    """Every file an edit tool wrote, by basename, whatever its language: the plugin's reading
    (`Vocabulary.generic`). The experiments count Python files only (`edited_pyfiles`)."""
    fs = set()
    for tool, args, _ in calls:
        if tool in ("edit", "multiedit", "write", "create", "str_replace_editor"):
            p = args.get("file_path") or args.get("path") or ""
            if p:
                fs.add(os.path.basename(p))
        elif tool == "apply_patch":
            for p in args.get("paths") or ():
                fs.add(os.path.basename(p))
    return fs


class Lifecycle:
    def __init__(self, key, high, drift=True):
        self.key = key
        self.high = high
        self.drift = drift                 # promotion needs confirmations on different files
        self.state = "CANDIDATE"           # CANDIDATE | ACTIVE | RETIRED
        self.confirms = []                 # [(task_idx, [edited_files])]
        self.contradicts = []              # [task_idx]
        self.fire_num = 0                  # tasks where the waste occurred (fired)
        self.fire_den = 0                  # tasks where the fact was exercised
        self.promoted_at = None
        self.retired_at = None

    def to_dict(self):
        return {"key": self.key, "high": self.high, "drift": self.drift, "state": self.state,
                "confirms": self.confirms, "contradicts": self.contradicts,
                "fire_num": self.fire_num, "fire_den": self.fire_den,
                "promoted_at": self.promoted_at, "retired_at": self.retired_at}

    @classmethod
    def from_dict(cls, d):
        f = cls(d["key"], d["high"])
        f.__dict__.update(d)
        return f

    def _drifted_confirms(self):
        if not self.drift:
            return len(self.confirms)
        kept = []
        for idx, fs in self.confirms:
            fs = set(fs)
            if not fs or all(len(fs & set(k)) == 0 for _, k in kept):
                kept.append((idx, fs))
        return len(kept)

    def score(self, now):
        s = sum(GAMMA ** (now - i) for i, _ in self.confirms)
        s -= LAMBDA * sum(GAMMA ** (now - i) for i in self.contradicts)
        return s

    def firing_rate(self):
        return (self.fire_num / self.fire_den) if self.fire_den else 0.0

    def injectable(self):
        """ACTIVE and clears the firing gate -> goes into the rendered manual."""
        return (self.state == "ACTIVE"
                and self.fire_den >= FIRE_MIN_N
                and self.firing_rate() >= FIRE_GATE)

    def update(self, now, verdict, fired, exercised, files):
        if exercised:
            self.fire_den += 1
            if fired:
                self.fire_num += 1
        if verdict == "confirm":
            self.confirms.append((now, sorted(files)))
        elif verdict == "contradict":
            self.contradicts.append(now)
        if self.state == "CANDIDATE":
            if self._drifted_confirms() >= K_PROMOTE:
                self.state, self.promoted_at = "ACTIVE", now
        elif self.state == "ACTIVE":
            hard = self.high and verdict == "contradict" and HARD_KILL
            if hard or self.score(now) < SCORE_FLOOR:
                self.state, self.retired_at = "RETIRED", now


# ---- forgetting: a person retires a fact by hand -----------------------------
# Seconds slept between tries of a Windows `os.replace`, which fails while another process has the
# target open; POSIX renames over an open file at once.
_REPLACE_BACKOFF = (0.01, 0.02, 0.05, 0.1, 0.2)


def write_json_atomic(path, obj):
    """Write `obj` to `path` through a temporary file and `os.replace`: a reader, or a write cut
    short, never leaves half a document behind."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        for delay in _REPLACE_BACKOFF if os.name == "nt" else ():
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(delay)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class Forgetting:
    """The forgotten facts of a distiller (`self.facts`: id -> fact). A forgotten fact leaves
    `facts`, so it is neither rendered nor folded; its id is kept in `forgotten`, with the claim
    it made and its record, so that it is never learned again under that id and `unforget` can
    put it back as it was. `FACT_CLS` reads a record back."""
    FACT_CLS = None

    @staticmethod
    def _claim_of(fact):
        claim = fact.claim
        return claim() if callable(claim) else claim

    def known_ids(self):
        return set(self.facts) | set(self.forgotten)

    def forget(self, fid):
        """True when `fid` is forgotten now, False when it already was; KeyError when no fact
        has that id."""
        if fid in self.forgotten:
            return False
        fact = self.facts.pop(fid)
        self.forgotten[fid] = {"claim": self._claim_of(fact), "record": fact.to_dict()}
        return True

    def unforget(self, fid):
        """True when `fid` is back among the facts, False when it was not forgotten; KeyError
        when no fact has that id."""
        if fid not in self.forgotten:
            if fid in self.facts:
                return False
            raise KeyError(fid)
        rec = self.forgotten.pop(fid).get("record")
        if rec:
            self.facts[fid] = self.FACT_CLS.from_dict(rec)
        return True

    def drop_forgotten(self):
        """Take out any forgotten id the last fold learned again."""
        for fid in self.forgotten:
            self.facts.pop(fid, None)
