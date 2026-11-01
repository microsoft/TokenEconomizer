"""The paid proposer: an LLM that authors repository facts, each with a host-run detector.

As a repository's sessions finish, one LLM call per `PROPOSE_EVERY` sessions reads a COMPACT
digest of the recent ones (never raw transcripts) and authors two kinds of durable artifacts:

  * facts       -- one-line declarative statements an agent would want stated up front (the
                   canonical test command, a required environment variable, a build step);
  * extractors  -- small host-executable predicates over the raw command and output stream that
                   surface evidence the fixed digest does not already expose.

Every fact carries a DETECTOR (a regex/predicate DSL), so the hot path stays LLM-free: the model
invents the facts, the host checks them, and each goes through the shared `lifecycle`. Only a
fact's contradiction condition may be revised after it is written; since that condition only
ever retires a fact, revising it cannot manufacture credibility, and the earned counters stay.

The proposer's effective-token cost accrues on `Distiller.opt_tokens`, which the caller charges
to the ledger as overhead.
"""
import json
import os
import re

from ..names import repo_filename
from ..trace import call_cwds
from ..trace.claude import CONVERSATION
from .lifecycle import Lifecycle, edited_files, edited_pyfiles

# ---- proposer cadence / caps / gates ---------------------------------------
PROPOSE_EVERY  = 8      # one LLM proposer call per this many tasks (PrefixStage rebinds
                        # this module global from config; default cadence otherwise)
WINDOW         = 8      # digests handed to the proposer per tick (rolling)
MAX_CANDS      = 4      # cap facts emitted per tick
MIN_CITE       = 2      # a candidate must cite >= this many confirming task-ids
MAX_EXTRACTORS = 12     # cap authored features (keeps the digest bounded)
RAW_SAMPLE     = 3      # raw excerpts shown to the proposer on an abstaining tick
ABSTAIN_GATE   = 0.5    # tick is "abstaining" if >= this frac of window cmds are unseen
STATE_SUBDIR   = "manual_state"
BLOB_CAP       = 20_000  # characters of a saved digest's raw commands (twice that of output)

# ---- regexes ---------------------------------------------------------------
PYTEST_RE = re.compile(r"-m\s+pytest|\bpytest\b|\bpy\.test\b")
REPRO_RE  = re.compile(r"python[0-9.]*\s+-c|/tmp/\S*\.py|reproduce|repro")
BADARG_RE = re.compile(r"unrecognized arguments|no such option|usage:|error: argument")


# ---- transcript parsing ----------------------------------------------------
def load_calls(fp):
    """Ordered ``(tool_lower, args, out_text)`` calls for any supported agent.

    Codex (``item.completed``) and Copilot (``tool.execution_*``) have no Claude-style
    ``message.content[]`` envelope, so BOTH are routed through the shared trajectory
    normalizer (trace.load_trajectory auto-detects every schema); otherwise a Copilot
    transcript would parse to [] and the proposer would run (billed) but ground nothing.
    """
    try:
        is_codex = is_copilot = False
        with open(fp, encoding="utf-8", errors="replace") as probe:
            for line in probe:
                if ('"item.completed"' in line
                        or '"type": "item.completed"' in line):
                    is_codex = True
                    break
                if ('"tool.execution_start"' in line
                        or '"tool.execution_complete"' in line):
                    is_copilot = True
                    break
        if is_codex or is_copilot:
            from ..trace import load_trajectory
            return [(name.lower(), args, out)
                    for name, args, out, _ok in load_trajectory(fp)]
    except OSError:
        return []
    uses, order, res = {}, [], {}
    for line in open(fp, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message")
        if not isinstance(m, dict) or d.get("type", "assistant") not in CONVERSATION:
            continue
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id") not in uses:
                uses[b.get("id")] = (b.get("name", ""), b.get("input") or {})
                order.append(b.get("id"))
            elif b.get("type") == "tool_result":
                cont = b.get("content")
                txt = cont if isinstance(cont, str) else json.dumps(cont)
                res[b.get("tool_use_id")] = txt
    return [(uses[t][0].lower(), uses[t][1], res.get(t, "")) for t in order]


def bash_cmds(calls):
    out = []
    for tool, args, res in calls:
        if tool in ("bash", "shell", "stop_bash"):
            out.append(((args.get("command") or args.get("cmd") or ""), res))
    return out


# ---- compact digest: one dict per finished task (the proposer's only view) --
def build_digest(repo, transcript_path, iid, resolved=None, generic=False):
    if generic:
        return build_generic_digest(repo, transcript_path, iid, resolved)
    try:
        calls = load_calls(transcript_path)
    except Exception:
        return None
    cmds = bash_cmds(calls)
    cmd_blob = "\n".join(c for c, _ in cmds)
    out_blob = "\n".join(o for _, o in cmds)
    test_cmds = [c for c, _ in cmds if PYTEST_RE.search(c)
                 or re.search(r"runtests\.py|unittest|nosetests|tox", c)]
    repro = any(REPRO_RE.search(c) and not PYTEST_RE.search(c) for c, _ in cmds)
    return {
        "iid": iid,
        "resolved": bool(resolved) if resolved is not None else None,
        "edited_files": sorted(edited_pyfiles(calls)),
        "n_cmds": len(cmds),
        "cmd_templates": _templates(c for c, _ in cmds),
        "test_invocations": _templates(test_cmds)[:6],
        "repro_written": repro,
        "importerror": bool(re.search(
            r"ImportError|ModuleNotFoundError|No module named", out_blob)),
        "badarg": bool(BADARG_RE.search(out_blob)),
        # RAW blobs kept for host-side detector eval, NOT serialized to the prompt.
        "_cmd_blob": cmd_blob,
        "_out_blob": out_blob,
    }


# ---- the generic digest: any language or toolchain (`Vocabulary.generic`, the plugin's) ---
# Build, test, lint and type-check commands across ecosystems, and test runs among them.
CHECK_RE = re.compile(
    r"\b(?:pytest|py\.test|tox|nox|unittest|runtests\.py|jest|vitest|mocha|rspec|phpunit|ctest|"
    r"tsc|eslint|prettier|ruff|flake8|pylint|mypy|pyright|golangci-lint|cmake|ninja|bazel)\b|"
    r"(?:^|[\s;&|(])make\b|"
    r"\b(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?(?:test|build|lint|check|typecheck)\b|"
    r"\b(?:cargo|go|mvn|gradle|gradlew|dotnet|mix|swift)\s+"
    r"(?:test|build|check|vet|clippy|compile|package|verify)\b")
TEST_RE = re.compile(
    r"\b(?:pytest|py\.test|tox|nox|unittest|runtests\.py|jest|vitest|mocha|rspec|phpunit|ctest)\b|"
    r"\b(?:make|npm|yarn|pnpm|bun|cargo|go|mvn|gradle|gradlew|dotnet|mix|swift|rake)\s+"
    r"(?:run\s+)?test\b")
MISSING_CMD_RE = re.compile(r"command not found|: not found\b|is not recognized as", re.I)
MISSING_DEP_RE = re.compile(
    r"ModuleNotFoundError|No module named|ImportError|Cannot find module|ERR_MODULE_NOT_FOUND|"
    r"cannot find package|could not find (?:package|crate|module|gem)|unresolved import|"
    r"cannot load such file|undefined reference to|fatal error: [^\n]*: No such file", re.I)
BAD_USAGE_RE = re.compile(r"(?m)^usage:|unrecognized arguments?|no such option|"
                          r"unknown (?:option|flag|command)|invalid option|unexpected argument|"
                          r"error: argument", re.I)
_EXIT_RE = re.compile(r"(?i)\bexit(?:ed with)?(?: code| status)?[:=\s]+(\d+)")
_FAILED_OUT_RE = re.compile(r"Traceback \(most recent call last\)|^\w*Error\b", re.M)
_ERROR_LINE_RE = re.compile(r"(?i)\b(?:error|failed|failure|fatal|exception|traceback|cannot|"
                            r"not found|denied|refused|panic)\b")


def _command_failed(out):
    m = _EXIT_RE.search(out or "")
    if m:
        return int(m.group(1)) != 0
    return bool(MISSING_CMD_RE.search(out or "") or MISSING_DEP_RE.search(out or "")
                or _FAILED_OUT_RE.search(out or ""))


def _relative(text, roots):
    """A command or error line with the repository's own paths made relative and the home
    directory written `~`: the same shape on every machine, and no one's paths in a prompt."""
    for r in roots:
        if r:
            text = text.replace(r + "/", "").replace(r, ".")
    text = re.sub(r"^cd \.\s*(?:&&|;)\s*", "", text)        # into the repository: no `cd`
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "/" else text


def error_signature(out, roots=()):
    """The first line of a failing output that names the failure, normalized so two sessions
    that fail the same way read the same: paths, numbers and addresses masked."""
    for ln in (out or "").splitlines():
        if _ERROR_LINE_RE.search(ln) or MISSING_CMD_RE.search(ln):
            s = _relative(ln.strip(), roots)
            s = re.sub(r"0x[0-9a-fA-F]+", "<hex>", s)
            s = re.sub(r"(?<![\w.~])/[^\s'\"`:,)]+", "<path>", s)
            s = re.sub(r"\b\d+\b", "N", s)
            return " ".join(s.split())[:120]
    return ""


def build_generic_digest(repo, transcript_path, iid, resolved=None):
    """The digest the plugin's proposer reads: every edited file whatever its language, the
    build/test/lint commands run, the commands that failed with a normalized error line, and
    flags that hold in any ecosystem. Raw blobs stay host-side for the detectors."""
    from .observe import repository_top
    try:
        calls = load_calls(transcript_path)
    except Exception:
        return None
    cmds = bash_cmds(calls)
    start = next((c for c in call_cwds(transcript_path) if c), None)
    roots = sorted({r for r in (start, repository_top(start)) if r}, key=len, reverse=True)
    out_blob = "\n".join(o for _, o in cmds)
    failed, seen = [], set()
    for c, o in cmds:
        if not _command_failed(o):
            continue
        row = (_templates([_relative(c, roots)], cap=1) or [""])[0], error_signature(o, roots)
        if row not in seen:
            seen.add(row)
            failed.append({"cmd": row[0], "error": row[1]})
    return {
        "iid": iid,
        "resolved": bool(resolved) if resolved is not None else None,
        "edited_files": sorted(edited_files(calls)),
        "n_cmds": len(cmds),
        "cmd_templates": _templates(_relative(c, roots) for c, _ in cmds),
        "check_commands": _templates(_relative(c, roots) for c, _ in cmds
                                     if CHECK_RE.search(c))[:6],
        "failed_commands": failed[:6],
        "missing_command": bool(MISSING_CMD_RE.search(out_blob)),
        "missing_dependency": bool(MISSING_DEP_RE.search(out_blob)),
        "bad_usage": bool(BAD_USAGE_RE.search(out_blob)),
        "_ran_tests": any(TEST_RE.search(c) for c, _ in cmds),
        "_cmd_blob": "\n".join(c for c, _ in cmds),
        "_out_blob": out_blob,
    }


def _templates(cmds, cap=12):
    """Dedup command shapes: drop long path/arg tails so the proposer sees FORMS."""
    seen, out = set(), []
    for c in cmds:
        c = c.strip()
        if not c:
            continue
        head = re.sub(r"\s+", " ", c)[:80]
        head = re.sub(r"/testbed/\S+", "<path>", head)
        if head not in seen:
            seen.add(head)
            out.append(head)
        if len(out) >= cap:
            break
    return out


def proposer_view(dg):
    """The token-cheap projection actually shown to the LLM (drops raw blobs)."""
    return {k: v for k, v in dg.items() if not k.startswith("_")}


def _rx(pat):
    if not pat:
        return None
    try:
        return re.compile(pat)
    except re.error:
        return None


# ---- detector DSL: host-executable, no LLM at eval time --------------------
# Each reads either digest: a fact the experiments' proposer named keeps a meaning under the
# generic digest, and the reverse.
PREDICATES = {
    "repro_written": lambda dg: bool(dg.get("repro_written")),
    "importerror":   lambda dg: bool(dg.get("importerror", dg.get("missing_dependency"))),
    "badarg":        lambda dg: bool(dg.get("badarg", dg.get("bad_usage"))),
    "ran_tests":     lambda dg: bool(dg.get("test_invocations") or dg.get("_ran_tests")),
    "ran_checks":    lambda dg: bool(dg.get("check_commands") or dg.get("test_invocations")),
    "command_failed": lambda dg: bool(dg.get("failed_commands")),
    "missing_command": lambda dg: bool(dg.get("missing_command")),
    "missing_dependency": lambda dg: bool(dg.get("missing_dependency", dg.get("importerror"))),
    "bad_usage":     lambda dg: bool(dg.get("bad_usage", dg.get("badarg"))),
    "edited":        lambda dg: bool(dg.get("edited_files")),
}
GENERIC_PREDICATES = ("ran_checks", "ran_tests", "command_failed", "missing_command",
                      "missing_dependency", "bad_usage", "edited")


class Detector:
    """Base detector: scope/fired name a built-in predicate or a regex over commands."""
    def __init__(self, spec):
        self.scope   = spec.get("scope", "")
        self.confirm = _rx(spec.get("confirm"))
        self.contra  = _rx(spec.get("contradict"))
        self.fired   = spec.get("fired") or spec.get("scope")
        self.raw = spec

    def _exercised(self, dg):
        p = PREDICATES.get(self.scope)
        if p is not None:
            return bool(p(dg))
        rx = _rx(self.scope)
        return bool(rx and rx.search(dg["_cmd_blob"]))

    def _fired(self, dg):
        p = PREDICATES.get(self.fired)
        if p is not None:
            return bool(p(dg))
        rx = _rx(self.fired)
        return bool(rx and rx.search(dg["_cmd_blob"]))

    def verdict(self, dg):
        exercised = self._exercised(dg)
        if not exercised:
            return None, False, False
        blob = dg["_cmd_blob"] + "\n" + dg["_out_blob"]
        if self.contra and self.contra.search(blob):
            return "contradict", True, True
        if self.confirm and self.confirm.search(blob):
            return "confirm", self._fired(dg), True
        return None, self._fired(dg), True


class DetectorV3Scope(Detector):
    """Detector whose scope/fired may reference an authored EXTRACTOR feature (resolved
    against the digest's per-task `features`), then a built-in predicate, then a regex."""
    def __init__(self, spec, features):
        self.features = features
        super().__init__(spec)

    def _lookup(self, name, dg):
        if name in self.features:
            return bool(self.features[name])
        if name in PREDICATES:
            return bool(PREDICATES[name](dg))
        rx = _rx(name)
        return bool(rx and rx.search(dg["_cmd_blob"]))

    def _exercised(self, dg):
        return self._lookup(self.scope, dg)

    def _fired(self, dg):
        return self._lookup(self.fired, dg)


# ---- authored extractor: raw-event predicate -> named feature --------------
class Extractor:
    """A proposer-authored feature over raw calls.
    kinds: cmd_regex (bool), out_regex (bool), cmd_count (int)."""
    def __init__(self, spec):
        self.name = spec["name"]
        self.kind = spec.get("kind", "cmd_regex")
        self.rx = _rx(spec.get("pattern"))
        self.spec = spec

    def compute(self, cmds):
        if self.rx is None:
            return False if self.kind != "cmd_count" else 0
        if self.kind == "out_regex":
            return any(self.rx.search(o) for _, o in cmds)
        if self.kind == "cmd_count":
            return sum(1 for c, _ in cmds if self.rx.search(c))
        return any(self.rx.search(c) for c, _ in cmds)

    def to_dict(self):
        return self.spec


# ---- generic fact: authored payload + validated lifecycle ------------------
class Fact:
    def __init__(self, spec):
        self.id        = spec["id"]
        self.claim     = spec.get("claim", spec["id"])
        self.injection = spec.get("injection", self.claim)
        self.high      = bool(spec.get("high_stakes", False))
        self.detector  = Detector(spec.get("detector", {}))
        self.spec      = spec
        self.life      = Lifecycle(self.id, self.high)   # the validated state machine

    @property
    def state(self):
        return self.life.state

    def injectable(self):
        return self.life.injectable()

    def firing_rate(self):
        return self.life.firing_rate()

    def observe(self, now, dg, files):
        """Fold one finished-task digest into this fact's lifecycle. The detector is
        rebuilt per task against that task's authored features."""
        det = DetectorV3Scope(self.spec.get("detector", {}), dg.get("features", {}))
        verdict, fired, exercised = det.verdict(dg)
        self.life.update(now, verdict, fired, exercised, files)
        return verdict, fired, exercised

    def to_dict(self):
        return {"spec": self.spec, "life": self.life.to_dict()}

    @classmethod
    def from_dict(cls, d):
        f = cls(d["spec"])
        f.life = Lifecycle.from_dict(d["life"])
        return f


# ---- proposer prompt (facts + extractors + revisable contradiction) --------
_PROPOSER_HEAD = (
    "You mine a DURABLE, REPO-SPECIFIC operating manual for a coding agent from a "
    "window of finished-task digests in ONE repository. Each digest lists edited "
    "files, command templates, test invocations, and boolean flags — NOT raw logs.\n\n"
    "Emit facts that a FUTURE task in this repo would want stated up front to avoid "
    "wasted commands: the canonical test invocation, whether a repro is redundant, a "
    "required env var, a build/reinstall step that's needed (or NOT needed), a lint/"
    "format hook that reformats, etc. Do NOT emit task-specific fixes or which lines "
    "to change — only durable interface facts true across tasks.\n\n"
)
PROPOSER_SYSTEM = (
    _PROPOSER_HEAD
    + "For EACH fact return an object with:\n"
    '  id          short kebab slug\n'
    '  claim       one sentence, what is true about this repo\n'
    '  injection   the exact line to inject into the agent prefix. State it as a '
    "DECLARATIVE fact about the repo (where things are, what the canonical command is). "
    "NEVER phrase it as a prohibition on exploration or tool use — do NOT tell the agent "
    "to avoid `find`/`grep`/searching/reading, or to skip discovering files. A prior "
    "that suppresses exploration silently breaks later tasks on a growing codebase where "
    "the agent must discover accreted files. State what is true; never forbid a tool.\n"
    '  high_stakes true if a wrong belief would break the run (test cmd, build step)\n'
    '  detector    {scope, confirm, contradict?, fired?} where scope is a python '
    "regex over the command string (or one of: repro_written, importerror, badarg, "
    "ran_tests) selecting tasks where the fact is RELEVANT; confirm is a regex whose "
    "match in commands+output means the fact HELD this task; contradict (optional) a "
    "regex meaning the fact was FALSE this task; fired (optional) a predicate/regex "
    "meaning the waste the fact prevents actually occurred.\n"
    '  cite        list of >=2 iids from the window where confirm matches (grounding)\n\n'
    "Return ONLY a JSON list of <=%d such objects. Be conservative: a fact you cannot "
    "ground with a working detector + 2 citations is worse than no fact." % MAX_CANDS
    + "\n\nADDITIONALLY: if a durable repo fact would need evidence NOT present in "
    "the digest flags, first author an EXTRACTOR that surfaces it, then reference the "
    "extractor's feature name as your detector scope/fired. Extractors are cheap "
    "host-run predicates over raw commands/outputs.\n"
    "Return a JSON OBJECT: {\"extractors\": [{name, kind, pattern}], \"facts\": [...]}\n"
    "  extractor.kind in {cmd_regex, out_regex, cmd_count}; pattern is a python regex.\n"
    "  A fact's detector.scope/fired may name any extractor you define here or a "
    "built-in (repro_written, importerror, badarg, ran_tests)."
    + "\n\nREVISION: the CONTRADICTION condition of an existing note may be corrected — "
    "and nothing else. Re-emit a note with the SAME id and the SAME "
    "claim/injection/high_stakes and the SAME detector.scope/detector.confirm/detector.fired, "
    "changing ONLY detector.contradict, to fix or tighten the regex that marks the fact "
    "FALSE this task. The note KEEPS its earned firing history and the new condition applies "
    "going forward. Re-emitting an id while changing any other field is rejected; extractors "
    "remain append-only."
)


# The plugin's: any language or toolchain, with tests one topic among several. Its rules
# (fields, detectors, extractors, revision) are the experiments' own.
_GENERIC_HEAD = (
    "You mine a DURABLE, REPO-SPECIFIC operating manual for a coding agent from a "
    "window of finished-session digests in ONE repository, whatever its language and "
    "toolchain. Each digest lists the files edited, command templates (paths relative to "
    "the repository), the build, test and lint commands run, the commands that failed "
    "with a normalized error line, and boolean flags — NOT raw logs or file contents. "
    "(Each session is called a task below.)\n\n"
    "Emit facts that a FUTURE session in this repository would want stated up front to "
    "avoid wasted commands: how to build, run or check it here and the exact invocation "
    "that works; the toolchain (interpreter, package manager, compiler, version manager) "
    "and how it is reached; a setup, install, code-generation or rebuild step that is "
    "needed (or NOT needed); an environment variable, service or working directory a "
    "command needs; where generated, configured or vendored files live; a command that "
    "fails here and the variant that works; a hook or formatter that rewrites files. "
    "Tests are one such topic, not the focus. Do NOT emit task-specific fixes or which "
    "lines to change — only durable facts true across sessions.\n\n"
)
PROPOSER_SYSTEM_GENERIC = (_GENERIC_HEAD + PROPOSER_SYSTEM[len(_PROPOSER_HEAD):]).replace(
    "repro_written, importerror, badarg, ran_tests", ", ".join(GENERIC_PREDICATES))


def build_proposer_prompt(repo, window, existing, existing_feats, raw_excerpts,
                          external=(), generic=False):
    """`existing` maps each fact id to its claim (ids alone are accepted). The plugin's
    (`generic`) prompt shows the claims, so a fact already held is not authored again under a
    new id; the experiments' prompt names the ids alone, as the paper ran it."""
    view = [proposer_view(d) for d in window]
    existing = existing if isinstance(existing, dict) else dict.fromkeys(existing, "")
    if generic:
        known = ("\n\nExisting facts, as `id: claim` (do NOT restate or paraphrase one under a "
                 "NEW id; re-emit an id with ONLY detector.contradict changed to revise its "
                 "contradiction condition, else use a NEW id for a fact not listed here):\n"
                 + ("\n".join(f"  - {i}: {c}" if c else f"  - {i}"
                              for i, c in sorted(existing.items())) or "(none)"))
    else:
        known = ("\n\nExisting fact ids (re-emit an id with ONLY detector.contradict changed to "
                 "revise its contradiction condition, else use a NEW id):\n"
                 + (", ".join(sorted(existing)) or "(none)"))
    p = ((PROPOSER_SYSTEM_GENERIC if generic else PROPOSER_SYSTEM)
         + f"\n\nRepo: {repo}"
         + known
         + "\n\nExisting feature names:\n" + (", ".join(sorted(existing_feats)) or "(none)")
         + "\n\nTask digests (chronological):\n" + json.dumps(view, indent=1))
    if raw_excerpts:
        p += ("\n\nRAW command excerpts from recent tasks the current extractors did "
              "NOT cover (author extractors to reach these if they hide a durable "
              "fact):\n" + "\n".join(f"  $ {c}" for c in raw_excerpts))
    if external:
        # Facts a separate, LLM-free layer already injects (the environment-fact profile).
        # Telling the proposer is what keeps its paid budget off facts it would otherwise
        # rediscover -- on the stored streams the interpreter fact was among the first it
        # authored, every time.
        p += ("\n\nFacts ALREADY injected before every task by a separate layer (do NOT "
              "restate or paraphrase them; author only facts beyond these):\n"
              + "\n".join(f"  - {c}" for c in external))
    p += "\n\nReturn the JSON object {extractors, facts}:"
    return p


def parse_payload(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return [], []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return [], []
    exts = [e for e in obj.get("extractors", [])
            if isinstance(e, dict) and e.get("name") and e.get("pattern")]
    facts = [f for f in obj.get("facts", [])
             if isinstance(f, dict) and f.get("id") and f.get("detector")]
    return exts, facts


def _reparse_cmds(dg):
    """Recover [(cmd,out)] from a digest's stashed blobs (for retro-featurize)."""
    cmds = dg.get("_cmd_blob", "").split("\n")
    outs = dg.get("_out_blob", "").split("\n")
    return list(zip(cmds, outs + [""] * (len(cmds) - len(outs))))


# ---- distiller -------------------------------------------------------------
class Distiller:
    """Learns and injects a per-repo operating manual over the task stream.

    Public surface (the injection Stage calls these):
        d = Distiller(repo, runner=..., model=...)       # fresh, or
        d = Distiller.load(repo, root, runner=..., model=...)  # resume state
        prefix = d.render()                              # manual for the NEXT task ("" if none)
        d.update(transcript_path, iid=..., resolved=...) # fold in the finished task
        d.save(root)
    """
    def __init__(self, repo, runner=None, model="agent", external_facts=None, vocab=None):
        self.repo = repo
        # The plugin's generic reading (`Vocabulary.generic`): the generic digest and prompt.
        self.generic = bool(getattr(vocab, "generic", False))
        # Zero-arg callable returning claims another layer already injects (see
        # build_proposer_prompt); None when the distiller runs alone.
        self.external_facts = external_facts
        self.now = 0                    # task index within the stream
        self.facts = {}                 # id -> Fact
        self.window = []                # rolling digests since last propose
        self.runner = runner
        self.model = model
        self.opt_tokens = 0.0           # cumulative effective proposer cost
        self.last_propose = -1
        self.extractors = {}            # name -> Extractor
        self.raw_window = []            # bounded recent [(cmd,out)] per task

    # ---- featurization -----------------------------------------------------
    def _observe_fact(self, fact, now, dg, files):
        return fact.observe(now, dg, files)

    def _featurize(self, dg, cmds):
        feats = {}
        for name, ex in self.extractors.items():
            try:
                feats[name] = ex.compute(cmds)
            except Exception:
                feats[name] = False
        dg["features"] = feats
        return dg

    def _sensed(self, cmd):
        """Does a built-in sense already read this command?"""
        if self.generic:
            return bool(CHECK_RE.search(cmd))
        return bool(PYTEST_RE.search(cmd) or REPRO_RE.search(cmd))

    def _coverage(self, cmds):
        """Fraction of commands matched by SOME extractor or built-in test/repro sense."""
        if not cmds:
            return 1.0
        seen = 0
        for c, _ in cmds:
            hit = (self._sensed(c)
                   or any(ex.rx and ex.rx.search(c) for ex in self.extractors.values()))
            if hit:
                seen += 1
        return seen / len(cmds)

    def _abstaining_excerpts(self):
        """Uncovered command templates from the raw window, for extractor authoring."""
        out = []
        for cmds in self.raw_window[-WINDOW:]:
            for c, _ in cmds:
                if self._sensed(c):
                    continue
                if any(ex.rx and ex.rx.search(c) for ex in self.extractors.values()):
                    continue
                t = re.sub(r"\s+", " ", c).strip()[:100]
                if t and t not in out:
                    out.append(t)
        return out[:RAW_SAMPLE * 4]

    # ---- proposer tick (the only paid step) --------------------------------
    def _maybe_propose(self):
        if self.runner is None or not self.window:
            return
        cov = [self._coverage(c) for c in self.raw_window[-WINDOW:]]
        abstaining = cov and (sum(cov) / len(cov)) < ABSTAIN_GATE
        excerpts = self._abstaining_excerpts()[:RAW_SAMPLE] if abstaining else []
        external = list(self.external_facts()) if self.external_facts else []
        prompt = build_proposer_prompt(self.repo, self.window,
                                       {i: f.claim for i, f in self.facts.items()},
                                       set(self.extractors), excerpts, external=external,
                                       generic=self.generic)
        raw, eff = self.runner(prompt, self.model)
        if eff is not None:
            self.opt_tokens += eff
        exts, facts = parse_payload(raw)
        for e in exts:
            self._admit_extractor(e)              # append-only
        for spec in facts[:MAX_CANDS]:
            self._admit(spec)
        self.last_propose = self.now

    def _admit_extractor(self, spec):
        if spec["name"] in self.extractors or len(self.extractors) >= MAX_EXTRACTORS:
            return False
        ex = Extractor(spec)
        if ex.rx is None:
            return False
        self.extractors[ex.name] = ex
        # retro-featurize the window so a same-tick fact can reference it
        for dg in self.window:
            cmds = _reparse_cmds(dg)
            dg.setdefault("features", {})[ex.name] = ex.compute(cmds)
        return True

    # ---- fact admission: new id = append; existing id = contradict-only revise --
    def _ground(self, spec):
        """#window cites whose confirm regex fires under the CURRENT features."""
        by_iid = {d["iid"]: d for d in self.window}
        hits = 0
        for iid in (spec.get("cite") or []):
            dg = by_iid.get(iid)
            if dg is None:
                continue
            det = DetectorV3Scope(spec.get("detector", {}), dg.get("features", {}))
            v, _, _ = det.verdict(dg)
            if v == "confirm":
                hits += 1
        return hits

    def _build_fact(self, spec):
        """Fresh Fact replayed over the window -> enters with real lifecycle state."""
        f = Fact(spec)
        for i, dg in enumerate(self.window):
            self._observe_fact(
                f, self.now - len(self.window) + i, dg,
                set(dg["edited_files"]))
        return f

    def _frozen_view(self, spec):
        """Everything about a note EXCEPT detector.contradict (the append-only surface)."""
        det = dict(spec.get("detector", {}))
        det.pop("contradict", None)
        return {
            "claim": spec.get("claim"),
            "injection": spec.get("injection"),
            "high_stakes": bool(spec.get("high_stakes", False)),
            "detector": json.dumps(det, sort_keys=True),
        }

    def _admit(self, spec):
        fid = spec.get("id")
        if fid not in self.facts:
            hits = self._ground(spec)
            if hits < MIN_CITE:
                return False, f"grounding-fail({hits}/{MIN_CITE})"
            spec.setdefault("rev", 1)
            self.facts[fid] = self._build_fact(spec)
            return True, "admitted"

        old = self.facts[fid]
        # ONLY the contradiction condition may be revised; everything else is frozen.
        if self._frozen_view(spec) != self._frozen_view(old.spec):
            return False, "revise-not-contradict-only"
        new_contra = spec.get("detector", {}).get("contradict")
        if new_contra and _rx(new_contra) is None:
            return False, "revise-bad-regex"
        old_contra = old.spec.get("detector", {}).get("contradict")
        if new_contra == old_contra:
            return False, "revise-noop"
        # confirm/scope unchanged => still grounded; contradict only ever RETIRES =>
        # mutate in place, KEEP the earned lifecycle, apply going forward.
        old.spec.setdefault("detector", {})["contradict"] = new_contra
        old.spec["rev"] = old.spec.get("rev", 1) + 1
        return True, "revised-contradict"

    # ---- the two operations the Stage calls --------------------------------
    def render(self, task=None):
        """Firing-gated manual for the NEXT task. '' if nothing qualifies yet."""
        lines = [f.injection for f in self.facts.values() if f.injectable()]
        if not lines:
            return ""
        head = (f"# repo operating manual ({self.repo.split('/')[-1]}) "
                + ("— learned from earlier sessions in this repository" if self.generic
                   else "— learned from prior tasks in this repo"))
        return head + "\n" + "\n".join(f"- {l}" for l in lines) + "\n"

    def update(self, transcript_path, iid="", resolved=None, edited=None):
        """Fold in the finished task, then advance the clock (may trigger a paid tick)."""
        try:
            calls = load_calls(transcript_path)
        except Exception:
            self.now += 1
            return
        cmds = bash_cmds(calls)
        dg = build_digest(self.repo, transcript_path, iid, resolved, generic=self.generic)
        if dg is None:
            self.now += 1
            return
        self._featurize(dg, cmds)
        self.raw_window.append(cmds)
        if len(self.raw_window) > WINDOW:
            self.raw_window = self.raw_window[-WINDOW:]
        files = set(dg["edited_files"])
        for f in self.facts.values():
            self._observe_fact(f, self.now, dg, files)
        self.window.append(dg)
        if len(self.window) > WINDOW:
            self.window = self.window[-WINDOW:]
        self.now += 1
        if self.now - self.last_propose >= PROPOSE_EVERY:
            self._maybe_propose()

    # ---- persistence -------------------------------------------------------
    @staticmethod
    def _path(root, repo):
        d = os.path.join(root, STATE_SUBDIR)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, repo_filename(repo) + ".json")

    def save(self, root):
        # The window goes with the facts: a proposer reloaded for each session (the plugin's
        # fold) must see the recent sessions, as one kept in memory over a stream (the
        # experiments') does -- a fact needs MIN_CITE of them to be grounded, so a window of
        # the current session alone could never admit one. Raw blobs are capped.
        window = [dict(dg, _cmd_blob=dg.get("_cmd_blob", "")[-BLOB_CAP:],
                       _out_blob=dg.get("_out_blob", "")[-2 * BLOB_CAP:])
                  for dg in self.window[-WINDOW:]]
        json.dump({"repo": self.repo, "now": self.now, "opt_tokens": self.opt_tokens,
                   "last_propose": self.last_propose,
                   "extractors": {k: e.to_dict() for k, e in self.extractors.items()},
                   "facts": {k: f.to_dict() for k, f in self.facts.items()},
                   "window": window,
                   "raw_cmds": [[c for c, _ in cmds] for cmds in self.raw_window[-WINDOW:]]},
                  open(self._path(root, self.repo), "w", encoding="utf-8"), indent=2)

    @classmethod
    def load(cls, repo, root, runner=None, model="agent", external_facts=None, vocab=None):
        p = cls._path(root, repo)
        self = cls(repo, runner, model, external_facts=external_facts, vocab=vocab)
        if not os.path.exists(p):
            return self
        d = json.load(open(p, encoding="utf-8"))
        self.now = d.get("now", 0)
        self.opt_tokens = d.get("opt_tokens", 0.0)
        self.last_propose = d.get("last_propose", -1)
        self.extractors = {k: Extractor(v) for k, v in d.get("extractors", {}).items()}
        self.facts = {k: Fact.from_dict(v) for k, v in d.get("facts", {}).items()}
        self.window = list(d.get("window") or [])[-WINDOW:]
        self.raw_window = [[(c, "") for c in cmds] for cmds in d.get("raw_cmds") or []]
        return self

    def debug_row(self):
        row = {k: {"state": f.state, "fire": f"{f.life.fire_num}/{f.life.fire_den}",
                   "rate": round(f.firing_rate(), 2), "inject": f.injectable(),
                   "claim": f.claim[:60]}
               for k, f in self.facts.items()}
        row["_extractors"] = sorted(self.extractors)
        for fid, f in self.facts.items():
            if isinstance(row.get(fid), dict):
                row[fid]["rev"] = f.spec.get("rev", 1)
        return row
