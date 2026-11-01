"""Environment facts: micro-skills learned for free from recurring failures.

As a repository's sessions finish, this distiller looks for RECURRING SHAPES OF EVIDENCE that an
agent rediscovers the same environment fact by failing at it, session after session. When a
shape recurs across sessions that edited different files, it becomes a one-line declarative
fact, injected at the start of the next session. Every specific string in a rendered fact -- an
interpreter path, a runner form, a refused host -- is a PARAMETER learned from the sessions;
nothing here names a path, an environment manager, a repository or a benchmark.

The families:

  FormRepair   in one session, form A of an intent (run the interpreter, run the tests, use the
               package tool) fails with an infrastructure error and a later form B succeeds.
               Fact: "`A ...` fails here with <error>; `B ...` works." Retires when a bare form
               runs the test runner successfully.
  Boundary     every call of an operation class (fetching from a host) fails with one
               environmental error class and none succeeds. Fact: "Connections to <hosts> are
               refused here." Retires when a fetch to a listed host succeeds.
  EmptyProbe   discovery probes of one kind (VCS refs beyond the checkout) return nothing in
               every session where they run. Fact: "no remote, no refs beyond HEAD". Retires --
               hard -- the moment a probe returns something.
  ArgRepair    the test runner cannot load a label of one shape and, run the same way, loads it
               rewritten into another (a leading package prefix dropped, `::` written as `.`).
               A label naming something that does not exist matches neither rewrite, so a plain
               mistake never becomes a fact. Retires when a label of the failing shape loads.
  EnvValue     code fails asking for an environment variable by name, and a later run that sets
               it succeeds. Fact: "Code that imports the project needs `V` set here: `V=x`
               works." Retires when that value fails the same way.

Each fact goes through the shared `lifecycle`. No LLM is involved: `opt_tokens` is 0. The block
is capped at MAX_FACTS and MAX_TOKENS, and a lint refuses any fact that reads as completion or
verification advice.
"""
import json
import os
import re
from collections import Counter

from ..names import repo_filename
from .lifecycle import Lifecycle
from .observe import (DEFAULT_VOCAB, NETWORK_ERRORS, form_failed, form_worked, is_bare,
                      label_rule, observe_task)

STATE_SUBDIR = "envfacts_state"
MAX_FACTS = 4
MAX_TOKENS = 150          # ~4 chars per token; the whole block, header included
HEADER = "# environment facts learned from earlier tasks in this checkout"
# The plugin's (`Vocabulary.generic`): a person's work comes in sessions, not tasks.
GENERIC_HEADER = "# environment facts learned from earlier sessions in this repository"

# Words that would turn a fact into completion or verification advice.
_LINT_RE = re.compile(r"\b(skip|no need|don'?t|do not|stop|done|completed?|finish(?:ed)?|"
                      r"verify|verification|instead of (?:running|testing))\b", re.I)

_ERROR_PHRASE = {
    "module_missing": "ModuleNotFoundError",
    "command_missing": "command not found",
    "script_missing": "a file-not-found error",
    "usage_error": "a usage error",
    "permission_denied": "permission denied",
}


# --------------------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------------------
class EnvFact:
    def __init__(self, fid, family, high, params=None):
        self.id = fid
        self.family = family
        self.params = params or {}
        # An environment fact holds or not whatever files a session edits: in a small
        # repository where every session touches the same module, three sessions missing
        # `pytest` are three confirmations (seen live on all four agents).
        self.life = Lifecycle(fid, high, drift=False)

    # ---- lifecycle passthrough ----
    @property
    def state(self):
        return self.life.state

    def injectable(self):
        return self.life.injectable()

    def firing_rate(self):
        return self.life.firing_rate()

    # ---- rendering ----
    def claim(self):
        p = self.params
        if self.family == "form_repair":
            b = self.working_form()
            if b is None:
                return ""
            e = self._best("error_counts") or "module_missing"
            return (f"{_usage(p['a'])} fails here with {_ERROR_PHRASE.get(e, e)}; "
                    f"{_usage(b)} works.")
        if self.family == "boundary":
            hosts = [h for h, _ in Counter(p.get("hosts", {})).most_common(3)]
            return ("Outbound connections to " + _join(hosts) +
                    " are refused here; nothing can be fetched from them.")
        if self.family == "arg_repair":
            if p.get("kind") == "sep":
                return (f"{_usage(p['form'], '')} takes dotted test labels (`module.Class.test`, "
                        f"not `module::Class::test`).")
            a, b = min((k.split(" ", 1) for k in p.get("examples") or {"x x": 1}),
                       key=lambda ab: len(ab[0]))
            return (f"{_usage(p['form'], '')} takes test labels without the `{p['param']}` prefix "
                    f"(`{b}`, not `{a}`).")
        if self.family == "env_value":
            good = self._best("good") or ""
            bad = [v for v in sorted(p.get("bad") or {}) if v != good][:1]
            return (f"Code that imports the project needs `{p['var']}` set here: "
                    f"`{p['var']}={good}` works" + (f", `{bad[0]}` does not." if bad else "."))
        if self.family == "empty_probe":
            return ("This checkout has no git remote and no branches, tags or unreachable "
                    "objects beyond HEAD; its history ends at the checked-out commit.")
        return ""

    def _best(self, key):
        c = self.params.get(key) or {}
        return max(c.items(), key=lambda kv: kv[1])[0] if c else None

    def working_form(self):
        """The working form a FormRepair names: the one with the best record, sessions it worked
        in as typed less sessions it failed in -- among those that leave the shell where it is
        (`python tests/runtests.py`, not `cd tests && python runtests.py`) whenever one of them
        has a good record at all. None when no form has: then the fact makes no claim."""
        c = self.params.get("b_counts") or {}
        bad = self.params.get("b_fails") or {}
        net = {k: v - bad.get(k, 0) for k, v in c.items() if v > bad.get(k, 0)}
        pool = {k: n for k, n in net.items() if not _moves_shell(k)} or net
        return max(pool, key=lambda k: (pool[k], c[k])) if pool else None

    def to_dict(self):
        return {"id": self.id, "family": self.family, "params": self.params,
                "life": self.life.to_dict()}

    @classmethod
    def from_dict(cls, d):
        f = cls(d["id"], d["family"], d["life"].get("high", False), d.get("params") or {})
        f.life = Lifecycle.from_dict(d["life"])
        f.life.drift = False
        return f


CONTRADICT_STREAK = 2     # FormRepair: consecutive contradicted tasks before one counts


def fold(fact, now, verdict, fired, exercised, files):
    """Fold one task's verdict into a fact's lifecycle.

    Two adjustments to the shared state machine. A high-stakes CANDIDATE that is contradicted
    retires at once (`_Fact` hard-kills only ACTIVE facts): an image whose history continues
    past HEAD does not become clean by being probed again. And a FormRepair contradiction --
    the bare form ran the test runner -- only counts after CONTRADICT_STREAK consecutive
    exercised tasks said so: a single task whose tests happened to need none of the missing
    packages is not a fixed environment, and the asymmetric contradiction weight would
    otherwise retire a fact confirmed thirty times over."""
    if fact.family == "form_repair" and exercised:
        streak = fact.params.get("contra_streak", 0)
        streak = streak + 1 if verdict == "contradict" else 0
        fact.params["contra_streak"] = streak
        if verdict == "contradict" and streak < CONTRADICT_STREAK:
            verdict = None
    fact.life.update(now, verdict, fired, exercised, files)
    if verdict == "contradict" and fact.life.high and fact.life.state == "CANDIDATE":
        fact.life.state, fact.life.retired_at = "RETIRED", now


def typed_ok(o):
    """The form worked exactly as typed: no `VAR=value` before it, which its form leaves out,
    had to help. `PYTHONPATH=. ~/.local/bin/pytest` passing says nothing for a bare
    `~/.local/bin/pytest`, which here stopped at collection."""
    return form_worked(o) and not o.env


def _repairs(form, a_form, intent):
    """Can `form` stand in for the failing `a_form`? Another form -- and, outside test runs
    and builds, not a bare one: only a fuller spelling repairs a bare interpreter or package
    tool."""
    return form != a_form and (intent in ("run_tests", "run_build") or not is_bare(form))


def _moves_shell(form):
    return form.startswith("cd ") and " && " in form


def _usage(form, args=" …"):
    """A form as a claim shows it. One that starts with `cd DIR &&` is shown as a subshell: an
    agent whose shell keeps its directory between calls (Claude Code) that typed the bare
    `cd` would run every later command in DIR, where the next `cd DIR` fails."""
    return f"`({form}{args})`" if _moves_shell(form) else f"`{form}{args}`"


def _root(form):
    """The part of a working form that carries the environment: its activation prefix, or
    the directory of its explicit path."""
    if " && " in form:
        return form.split(" && ", 1)[0]
    if form.startswith(("conda run", "poetry run", "pipenv run", "uv run", "npx")):
        return " ".join(form.split()[:3])
    head = form.split()[0] if form else ""
    return head.rsplit("/", 1)[0] if "/" in head else head


def _join(items):
    items = list(items)
    if not items:
        return "the hosts it tried"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def lint_ok(text):
    return not _LINT_RE.search(text or "")


# --------------------------------------------------------------------------------------
# the distiller
# --------------------------------------------------------------------------------------
class EnvFactsDistiller:
    """Learns and injects environment facts over a repository's task stream.

    Interface parity with `injection.Distiller`, so `PrefixStage` drives both the same way:
        d = EnvFactsDistiller(repo, vocab=...)      # fresh, or .load(repo, root, vocab=...)
        prefix = d.render()                         # block for the NEXT task ("" if none)
        d.update(transcript_path, iid=..., resolved=...)
        d.save(root)
    `runner`/`model` are accepted and ignored: nothing here is paid. `vocab` is the domain's
    `Vocabulary` (default Python/git); `injectable_claims()` is what a paid proposer stacked
    on top is told not to restate. `max_facts` / `max_tokens` cap the rendered block
    (default MAX_FACTS / MAX_TOKENS).
    """
    def __init__(self, repo, runner=None, model="agent", vocab=None, max_facts=None,
                 max_tokens=None):
        self.max_facts = int(max_facts or MAX_FACTS)
        self.max_tokens = int(max_tokens or MAX_TOKENS)
        self.repo = repo
        self.vocab = vocab or DEFAULT_VOCAB
        self.now = 0
        self.facts = {}                 # id -> EnvFact
        self.opt_tokens = 0.0           # invariant: LLM-free
        self.last_injected = set()      # fact ids rendered for the task now running
        self.last_digest = None         # the most recent per-task verdicts (telemetry)

    # ---- render --------------------------------------------------------------------
    @property
    def header(self):
        return GENERIC_HEADER if self.vocab.generic else HEADER

    def render(self, task=None):
        chosen = self._select()
        self.last_injected = {f.id for f in chosen}
        if not chosen:
            return ""
        lines = [self.header] + [f"- {t}" for t in self._merged_lines(chosen)]
        return "\n".join(lines) + "\n"

    def injected_ids(self):
        """The ids of the facts the last `render` chose: what the next session is given."""
        return sorted(self.last_injected)

    def seed_injected(self, ids):
        """Say which facts the session about to be folded was actually given. A caller that
        renders in one process and folds in another (the plugin: a session-start hook, then a
        fold after the session) must, or an injected fact never counts as having fired and
        decays out of the block while it works."""
        self.last_injected = {i for i in ids or () if i in self.facts}

    def injectable_claims(self):
        """The claims that would be injected now, one string each (no header)."""
        return [f.claim() for f in self._select()]

    def _select(self):
        cands = [f for f in self.facts.values()
                 if f.injectable() and f.claim() and lint_ok(f.claim())]
        prio = {"form_repair": 0, "arg_repair": 1, "env_value": 2, "empty_probe": 3,
                "boundary": 4}
        cands.sort(key=lambda f: (prio.get(f.family, 9),
                                  0 if f.params.get("intent") == "run_interpreter" else 1,
                                  -f.firing_rate()))
        chosen = []
        for f in cands:
            trial = chosen + [f]
            if len(trial) > self.max_facts:
                break
            text = self.header + "\n" + "\n".join(f"- {t}" for t in self._merged_lines(trial))
            if len(text) > self.max_tokens * 4:
                continue
            chosen = trial
        return chosen

    def _merged_lines(self, facts):
        """FormRepair facts whose working forms share a root (the same activation prefix
        or the same directory) collapse into one line: they are one environment fact."""
        lines, groups = [], {}
        for f in facts:
            if f.family == "form_repair":
                groups.setdefault(_root(f.working_form() or ""), []).append(f)
            else:
                lines.append(f.claim())
        for _root_key, fs in groups.items():
            if len(fs) == 1:
                lines.insert(0, fs[0].claim())
                continue
            a_forms = _join([_usage(f.params["a"]) for f in fs])
            errs = sorted({_ERROR_PHRASE.get(f._best("error_counts") or "", "") for f in fs}
                          - {""})
            bs = sorted({_usage(f.working_form()) for f in fs})
            lines.insert(0, f"{a_forms} fail here ({', '.join(errs) or 'environment error'}); "
                            f"{_join(bs)} {'works' if len(bs) == 1 else 'work'}.")
        return lines

    # ---- update --------------------------------------------------------------------
    def update(self, transcript_path, iid="", resolved=None, edited=None):
        try:
            digest = observe_task(transcript_path, self.vocab)
        except Exception:
            self.now += 1
            self.last_injected = set()
            return
        obs = digest["obs"]
        files = set(digest["edited"]) if edited is None else set(edited)
        injected = set(self.last_injected)
        verdicts = {}

        # ---- FormRepair: discover candidates, then fold every fact ----
        by_intent = {}
        for o in obs:
            by_intent.setdefault(o.intent, []).append(o)
        for intent in ("run_interpreter", "run_tests", "run_build", "package_tool"):
            seq = by_intent.get(intent, [])
            # A test run that failed because its LABEL did not load says nothing about the way
            # the runner was called (Django reports a bad label as a ModuleNotFoundError): those
            # are ArgRepair's. Counting them here once taught "`python tests/runtests.py` fails;
            # `python runtests.py` works" from a stream of mistyped labels.
            fails = [o for o in seq if form_failed(o)]
            if intent not in ("run_tests", "run_build"):
                # An explicit path or activation that fails is a missing dependency, not a
                # resolution problem; only a bare form can be "repaired" by a fuller one.
                fails = [o for o in fails if is_bare(o.form)]
            for a in fails:
                if not any(typed_ok(o) and o.call > a.call and _repairs(o.form, a.form, intent)
                           for o in seq):
                    continue
                fid = f"form_repair:{intent}:{a.form}"
                f = self.facts.get(fid)
                if f is None:
                    f = EnvFact(fid, "form_repair", high=False,
                                params={"intent": intent, "a": a.form,
                                        "b_counts": {}, "error_counts": {}})
                    self.facts[fid] = f
                f.params["error_counts"][a.outcome] = f.params["error_counts"].get(a.outcome, 0) + 1
            # A session is one piece of evidence per form: every form that worked as typed adds
            # one, and a known working form that failed as typed (and never worked) takes one
            # back -- so one messy session cannot outvote several clean ones, and a form that
            # stops working here stops being named.
            ok = {o.form for o in seq if typed_ok(o)}
            failed = {o.form for o in seq if form_failed(o) and not o.env} - ok
            for f in self.facts.values():
                if f.family != "form_repair" or f.params["intent"] != intent:
                    continue
                for form in ok:
                    if _repairs(form, f.params["a"], intent):
                        f.params["b_counts"][form] = f.params["b_counts"].get(form, 0) + 1
                fails_ = f.params.setdefault("b_fails", {})
                for form in failed & set(f.params["b_counts"]):
                    fails_[form] = fails_.get(form, 0) + 1
        for f in [x for x in self.facts.values() if x.family == "form_repair"]:
            intent, a_form = f.params["intent"], f.params["a"]
            seq = by_intent.get(intent, [])
            exercised = bool(seq)
            a_failed = any(o.form == a_form and form_failed(o) for o in seq)
            b_forms = set(f.params["b_counts"])
            b_ran = any(o.form in b_forms and form_worked(o) for o in seq) or \
                any(form_worked(o) and not is_bare(o.form) for o in seq)
            # Contradiction: the bare form ran the TEST RUNNER without an environment error.
            # A bare interpreter one-liner succeeding is not evidence (pure-Python packages
            # import from the working tree under any interpreter).
            a_runner_ok = any(o.form == a_form and form_worked(o)
                              for o in by_intent.get("run_tests", []))
            fired = a_failed or (f.id in injected and b_ran and not a_failed)
            if a_failed or (b_ran and not a_runner_ok):
                verdict = "confirm"
            elif a_runner_ok:
                verdict = "contradict"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[f.id] = (verdict, fired, exercised)

        # ---- ArgRepair: labels the runner loads only in another shape ----
        runs = [o for o in by_intent.get("run_tests", []) if o.labels]
        for i, a_obs in enumerate(runs):
            if not a_obs.label_err:
                continue
            for a in a_obs.labels:
                for b_obs in runs[i + 1:]:
                    if b_obs.form != a_obs.form or not b_obs.loaded:
                        continue
                    rule = next((r for b in b_obs.labels for r in [label_rule(a, b)] if r), None)
                    if rule is None:
                        continue
                    b = next(b for b in b_obs.labels if label_rule(a, b) == rule)
                    fid = f"arg_repair:{a_obs.form}:{rule[0]}:{rule[1]}"
                    f = self.facts.get(fid)
                    if f is None:
                        f = EnvFact(fid, "arg_repair", high=False, params={
                            "form": a_obs.form, "kind": rule[0], "param": rule[1],
                            "examples": {}})
                        self.facts[fid] = f
                    key = f"{a} {b}"
                    f.params["examples"][key] = f.params["examples"].get(key, 0) + 1
                    break
        for f in [x for x in self.facts.values() if x.family == "arg_repair"]:
            mine = [o for o in runs if o.form == f.params["form"]]
            if f.params["kind"] == "sep":
                bad = lambda lab: "::" in lab
            else:
                bad = lambda lab, pre=f.params["param"]: lab.startswith(pre)
            bad_failed = any(o.label_err and any(bad(x) for x in o.labels) for o in mine)
            bad_loaded = any(o.loaded and any(bad(x) for x in o.labels) for o in mine)
            good_loaded = any(o.loaded and any(not bad(x) for x in o.labels) for o in mine)
            exercised = bool(mine)
            fired = bad_failed or (f.id in injected and good_loaded and not bad_failed)
            if bad_loaded:
                verdict = "contradict"
            elif bad_failed or good_loaded:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[f.id] = (verdict, fired, exercised)

        # ---- EnvValue: a variable the code asks for, and the value that satisfied it ----
        env = digest.get("env") or []
        for i, c in enumerate(env):
            for var in c["need"]:
                later = [d for d in env[i + 1:] if var in d["sets"]]
                good = [d["sets"][var] for d in later if d["ok"] and var not in d["need"]]
                if not good:
                    continue
                fid = f"env_value:{var}"
                f = self.facts.get(fid)
                if f is None:
                    f = EnvFact(fid, "env_value", high=False, params={
                        "var": var, "good": {}, "bad": {}})
                    self.facts[fid] = f
                f.params["good"][good[0]] = f.params["good"].get(good[0], 0) + 1
                for d in later:
                    if not d["ok"] and d["sets"][var] != good[0]:
                        val = d["sets"][var]
                        f.params["bad"][val] = f.params["bad"].get(val, 0) + 1
        for f in [x for x in self.facts.values() if x.family == "env_value"]:
            var, good = f.params["var"], f._best("good")
            asked = any(var in c["need"] for c in env)
            good_ok = any(c["sets"].get(var) == good and c["ok"] for c in env)
            good_failed = any(c["sets"].get(var) == good and var in c["need"] for c in env)
            exercised = asked or any(var in c["sets"] for c in env)
            fired = asked or (f.id in injected and good_ok)
            # Only the value working confirms the fact: code asking for the variable again
            # shows the waste recurs (it fires), not that the learned value is right.
            if good_failed:
                verdict = "contradict"
            elif good_ok:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[f.id] = (verdict, fired, exercised)

        # ---- Boundary: hosts refused ----
        fetches = by_intent.get("fetch_remote", [])
        failed_hosts = Counter(h for o in fetches if o.outcome in NETWORK_ERRORS for h in o.hosts)
        ok_hosts = {h for o in fetches if o.outcome == "ran" for h in o.hosts}
        fid = "boundary:fetch_remote"
        f = self.facts.get(fid)
        if f is None and failed_hosts:
            f = EnvFact(fid, "boundary", high=True, params={"hosts": {}})
            self.facts[fid] = f
        if f is not None:
            for h, n in failed_hosts.items():
                f.params["hosts"][h] = f.params["hosts"].get(h, 0) + n
            known = set(f.params["hosts"])
            exercised = any(o.hosts for o in fetches)
            fired = bool(failed_hosts)
            if ok_hosts & known:
                verdict = "contradict"
            elif failed_hosts:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[fid] = (verdict, fired, exercised)

        # ---- EmptyProbe: refs beyond the checkout ----
        probes = [o for o in by_intent.get("probe_vcs_refs", []) if o.outcome in ("hit", "empty")]
        fid = "empty_probe:vcs_refs"
        f = self.facts.get(fid)
        if f is None and probes:
            f = EnvFact(fid, "empty_probe", high=True, params={"kinds": {}})
            self.facts[fid] = f
        if f is not None:
            for o in probes:
                f.params["kinds"][o.probe] = f.params["kinds"].get(o.probe, 0) + 1
            exercised = bool(probes)
            fired = exercised
            if any(o.outcome == "hit" for o in probes):
                verdict = "contradict"
            elif probes:
                verdict = "confirm"
            else:
                verdict = None
            fold(f, self.now, verdict, fired, exercised, files)
            verdicts[fid] = (verdict, fired, exercised)

        self.last_digest = {"iid": iid, "resolved": resolved, "injected": sorted(injected),
                            "verdicts": verdicts, "n_calls": digest["n_calls"]}
        self.last_injected = set()
        self.now += 1

    # ---- persistence -------------------------------------------------------------
    @staticmethod
    def _path(root, repo):
        d = os.path.join(root, STATE_SUBDIR)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, repo_filename(repo) + ".json")

    def save(self, root):
        with open(self._path(root, self.repo), "w", encoding="utf-8") as fh:
            json.dump({"repo": self.repo, "now": self.now, "opt_tokens": 0.0,
                       "vocab": self.vocab.name,
                       "facts": {k: f.to_dict() for k, f in self.facts.items()}}, fh, indent=2)

    @classmethod
    def load(cls, repo, root, runner=None, model="agent", vocab=None, max_facts=None,
             max_tokens=None):
        self = cls(repo, vocab=vocab, max_facts=max_facts, max_tokens=max_tokens)
        p = self._path(root, repo)
        if not os.path.exists(p):
            return self
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        self.now = d.get("now", 0)
        self.facts = {k: EnvFact.from_dict(v) for k, v in d.get("facts", {}).items()}
        return self

    # ---- telemetry ---------------------------------------------------------------
    def debug_row(self):
        row = {}
        for k, f in self.facts.items():
            row[k] = {"state": f.state, "fire": f"{f.life.fire_num}/{f.life.fire_den}",
                      "rate": round(f.firing_rate(), 2), "inject": f.injectable(),
                      "claim": f.claim()[:120], "family": f.family}
        row["_profile"] = "envfacts"
        row["_vocab"] = self.vocab.name
        if self.last_digest:
            row["_task"] = self.last_digest
        return row
