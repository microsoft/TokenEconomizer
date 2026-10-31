"""The plugin's settings: which mechanisms act in real sessions, and how far each may go.

The defaults:

  * microskills = "off"      -- nothing is learned or injected. `on` is "envfacts+distiller":
                                the LLM-free facts plus the proposer, which reads digests of
                                finished sessions every `microskills_propose_every` sessions and
                                calls a model through the agent's own CLI (charged to the
                                ledger) to write facts beyond them, as in the experiments.
                                "envfacts" alone makes no model call;
  * toolprune   = "auto"     -- a deny list is applied once the evidence forms it and the
                                repository's sessions have been idle (toolprune_quiet_minutes),
                                from the next session, and announced once at that session's start;
                                `toolprune restore` gives it back. "propose" only proposes it,
                                and writes nothing to the agent's settings until the person
                                runs `toolprune apply`;
  * loopguard   = "nudge"    -- a turn that shows signs of looping is put to a judge, a model
                                called through the agent's own CLI, and redirected mid-turn only
                                when the judge finds it stuck (the redirect written by that
                                model), at most once per prompt-turn; a turn still stuck after it
                                gets a notice to the person, saying how to stop it. The plugin
                                never stops a turn itself. "stop", which used to stop a headless
                                turn on Claude Code and Pi, is still read, and acts as "nudge"
                                with a warning. Every model call the plugin makes is charged to
                                the session's ledger;
  * shadow      = False      -- with True, every decision is computed and logged but none
                                reaches the agent: the way to measure a mechanism before
                                trusting it.

Each mechanism also takes a plain `on` / `off` (`on` = the value above), and has settings a
person can tune by hand:

  * microskills_max_facts, microskills_max_tokens -- caps on the injected block;
  * microskills_propose_every   -- sessions between proposer runs;
  * toolprune_evidence          -- prompts a never-used tool must have stayed unused through,
                                   over at least toolprune_min_sessions sessions, before it is
                                   proposed -- or over toolprune_sessions sessions, however short;
  * toolprune_protect           -- tools never proposed, by name or pattern (`mcp__headroom__*`),
                                   comma-separated; a deny rule the plugin added for one is
                                   removed at the next fold;
  * toolprune_quiet_minutes     -- how long every session of the repository must have been idle
                                   before `auto` writes a deny list (0: at once), so that no
                                   conversation resumed with a warm prompt cache loses it;
  * loopguard_aggr              -- the guard's threshold preset;
  * loopguard_nudges            -- redirects per prompt-turn before the guard only notifies;
  * loopguard.<name>            -- one guard threshold, pinned over the preset and over what the
                                   repository's history learned (LOOPGUARD_KEYS);
  * repomap, repomap_tokens, repomap_python, repomap_timeout -- the opt-in repository map;
  * pi_extensions, pi_cli       -- on Pi, the extensions the plugin's own model calls load (a
                                   custom provider's, whose model they could not reach otherwise;
                                   comma-separated paths), and the `pi` they run.

The names before the mechanisms were renamed are read too, so an existing config file keeps
working: `inject` (microskills), `untool` (toolprune), `spiral` (loopguard), with their
settings (`inject_max_facts`, `untool_evidence`, `spiral.<name>`, ...) and environment variables
(`TE_INJECT`, ...), and the value `guard` for `stop`. Writing a setting stores its new name.

Sources, lowest precedence first: built-in defaults, `<root>/config.json`, the repository's
`config.json`, then environment variables (`TE_MICROSKILLS`, `TE_TOOLPRUNE`, `TE_LOOPGUARD`,
`TE_MODE`, `TE_SHADOW`, `TE_DISABLE`, and one per setting; `TE_LOOPGUARD_THRESHOLDS` takes
JSON). Within one source a new name wins over its old one. An invalid value is ignored with
a warning recorded in the result rather than raised: a typo in a config file must not break the
agent's hooks.
"""
import json
import math
import os
from dataclasses import asdict, dataclass, field, fields

MICROSKILLS = ("off", "envfacts", "envfacts+distiller", "distiller")
TOOLPRUNE = ("off", "propose", "auto")
LOOPGUARD = ("off", "nudge", "stop")
REPOMAP = ("off", "on", "start")
MODES = ("auto", "interactive", "headless")
AGGR = ("conservative", "default", "aggressive")

# What `on` turns each mechanism to, and the words read as on / off.
ON_VALUE = {"microskills": "envfacts+distiller", "toolprune": "auto", "loopguard": "nudge",
            "repomap": "on"}
_ON = ("on", "true", "yes", "1", "enable", "enabled")
_OFF = ("off", "false", "no", "0", "disable", "disabled")
# Values renamed with their mechanism.
_VALUE_ALIASES = {"loopguard": {"guard": "stop"}}

# Setting names from before the mechanisms were renamed: old -> new.
ALIASES = {
    "inject": "microskills",
    "inject_max_facts": "microskills_max_facts",
    "inject_max_tokens": "microskills_max_tokens",
    "propose_every": "microskills_propose_every",
    "untool": "toolprune",
    "untool_evidence": "toolprune_evidence",
    "spiral": "loopguard",
    "spiral_aggr": "loopguard_aggr",
    "spiral_nudges": "loopguard_nudges",
    "spiral_thresholds": "loopguard_thresholds",
}
_THRESHOLD_PREFIXES = ("loopguard.", "spiral.")

# Settings that no longer exist, and where what they did went. A config file that still has one
# is read without complaint; `config set` says why it is refused.
OBSOLETE = {
    "paid": "the loop guard always asks its judge (`loopguard=off` turns it off), and "
            "micro-skills' proposer runs whenever micro-skills is on (`microskills=envfacts` "
            "keeps only the facts that need no model call)",
}

# The loop guard's thresholds a person may pin (`loopguard.<name>=<value>`), with their type
# and range. They are the keys of `loopguard.DEFAULT_THRESHOLDS`; a turn's "turns" are its tool
# calls here.
LOOPGUARD_KEYS = {
    "min_turns": (int, 1, None, "tool calls in a turn before it can count as stuck"),
    "edit_churn": (int, 1, None, "edits to one file that count as churn"),
    "distinct_edit_files": (int, 1, None, "files edited that count as scattered"),
    "error_outputs": (int, 1, None, "failing tool results that count as sustained errors"),
    "stagnation": (float, 0.0, 1.0, "share of failures that repeat an earlier one"),
    "envelope_pct": (float, 1.0, 100.0, "percentile of this repository's ended-green turns"),
    "envelope_margin": (float, 0.1, None, "multiplier on that percentile: the envelope"),
    "envelope_min_samples": (int, 1, None, "ended-green turns needed before the envelope is used"),
    "backstop_mult": (float, 0.1, None, "stuck past this multiple of the envelope: redirect "
                                        "without the judge"),
    "judge_confidence": (float, 0.0, 1.0, "the judge's confidence needed to redirect"),
    "gate_min_turns": (int, 1, None, "tool calls before the judge is consulted"),
    "gate_error_outputs": (int, 1, None, "failing results that wake the judge without a repeat"),
}


def canonical(key):
    """A setting's current name: `inject` -> `microskills`, `spiral.x` -> `loopguard.x`."""
    for prefix in _THRESHOLD_PREFIXES:
        if key.startswith(prefix):
            return "loopguard." + key[len(prefix):]
    return ALIASES.get(key, key)


def _threshold_name(key):
    """`min_turns` for `loopguard.min_turns` (or the old `spiral.min_turns`), else None."""
    for prefix in _THRESHOLD_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


@dataclass
class LiveConfig:
    enabled: bool = True
    mode: str = "auto"
    microskills: str = "off"
    microskills_max_facts: int = 4
    microskills_max_tokens: int = 150
    microskills_propose_every: int = 3
    toolprune: str = "auto"
    toolprune_evidence: int = 10
    toolprune_min_sessions: int = 2
    toolprune_sessions: int = 5
    toolprune_quiet_minutes: int = 60
    toolprune_protect: list = field(default_factory=list)
    loopguard: str = "nudge"
    loopguard_aggr: str = "default"
    loopguard_nudges: int = 1
    loopguard_thresholds: dict = field(default_factory=dict)
    repomap: str = "off"
    repomap_tokens: int = 2048
    repomap_python: str = ""
    repomap_timeout: int = 8
    paid_model: str = ""
    pi_extensions: list = field(default_factory=list)
    pi_cli: str = ""
    shadow: bool = False
    vocabulary: str = "auto"
    warnings: list = field(default_factory=list)

    _CHOICES = {"mode": MODES, "microskills": MICROSKILLS, "toolprune": TOOLPRUNE,
                "loopguard": LOOPGUARD, "repomap": REPOMAP, "loopguard_aggr": AGGR,
                "vocabulary": ("auto", "generic", "python-git", "node-web")}
    # The smallest value a number setting takes, where it is not 1.
    _MIN = {"toolprune_quiet_minutes": 0}

    def as_dict(self):
        d = asdict(self)
        d.pop("warnings", None)
        return d

    def set(self, key, value, source="?"):
        """Apply one setting with type coercion and validation; False if rejected."""
        name = _threshold_name(key)
        if name is not None:
            return self._set_threshold(name, value, source)
        key = canonical(key)
        if key in OBSOLETE:
            return True
        names = {f.name: f for f in fields(self) if f.name != "warnings"}
        if key not in names:
            self.warnings.append(f"{source}: unknown key {key!r}")
            return False
        if key == "loopguard_thresholds":
            if not isinstance(value, dict):
                self.warnings.append(f"{source}: loopguard_thresholds must be an object")
                return False
            return all([self._set_threshold(k, v, source) for k, v in value.items()])
        if key in ON_VALUE and isinstance(value, (str, bool, int)):
            word = str(value).strip().lower()
            if word in _ON:
                value = ON_VALUE[key]
            elif word in _OFF:
                value = "off"
            else:
                value = _VALUE_ALIASES.get(key, {}).get(word, value)
        cur = getattr(self, key)
        try:
            if isinstance(cur, list):
                items = value if isinstance(value, list) else str(value).split(",")
                v = [str(x).strip() for x in items if str(x).strip()]
            elif isinstance(cur, bool):
                v = value if isinstance(value, bool) else str(value).strip().lower() in (
                    "1", "true", "yes", "on")
            elif isinstance(cur, int):
                v = int(value)
                lo = self._MIN.get(key, 1)
                if v < lo:
                    raise ValueError(f"must be >= {lo}")
            else:
                v = str(value).strip()
        except (TypeError, ValueError, OverflowError) as e:
            self.warnings.append(f"{source}: {key}={value!r} rejected ({e})")
            return False
        choices = self._CHOICES.get(key)
        if choices and v not in choices:
            self.warnings.append(f"{source}: {key}={v!r} not one of {choices}")
            return False
        setattr(self, key, v)
        return True

    def _set_threshold(self, name, value, source):
        spec = LOOPGUARD_KEYS.get(name)
        if spec is None:
            self.warnings.append(f"{source}: unknown loopguard threshold {name!r} "
                                 f"(one of {', '.join(LOOPGUARD_KEYS)})")
            return False
        kind, lo, hi, _ = spec
        try:
            if isinstance(value, bool):
                raise ValueError("not a number")
            if not math.isfinite(float(value)):
                raise ValueError("must be finite")
            v = kind(float(value)) if kind is int and float(value).is_integer() else kind(value)
        except (TypeError, ValueError, OverflowError) as e:
            self.warnings.append(f"{source}: loopguard.{name}={value!r} rejected ({e})")
            return False
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
            self.warnings.append(f"{source}: loopguard.{name}={v!r} rejected (must be {rng})")
            return False
        self.loopguard_thresholds[name] = v
        return True


# Environment variables, old names first so that a new name set alongside its old one wins.
_ENV = {"TE_MODE": "mode", "TE_SHADOW": "shadow",
        "TE_PAID_MODEL": "paid_model", "TE_VOCABULARY": "vocabulary",
        "TE_INJECT": "microskills", "TE_INJECT_MAX_FACTS": "microskills_max_facts",
        "TE_INJECT_MAX_TOKENS": "microskills_max_tokens",
        "TE_PROPOSE_EVERY": "microskills_propose_every",
        "TE_UNTOOL": "toolprune", "TE_UNTOOL_EVIDENCE": "toolprune_evidence",
        "TE_SPIRAL": "loopguard", "TE_SPIRAL_AGGR": "loopguard_aggr",
        "TE_SPIRAL_NUDGES": "loopguard_nudges",
        "TE_MICROSKILLS": "microskills", "TE_MICROSKILLS_MAX_FACTS": "microskills_max_facts",
        "TE_MICROSKILLS_MAX_TOKENS": "microskills_max_tokens",
        "TE_MICROSKILLS_PROPOSE_EVERY": "microskills_propose_every",
        "TE_TOOLPRUNE": "toolprune", "TE_TOOLPRUNE_EVIDENCE": "toolprune_evidence",
        "TE_TOOLPRUNE_QUIET_MINUTES": "toolprune_quiet_minutes",
        "TE_TOOLPRUNE_MIN_SESSIONS": "toolprune_min_sessions",
        "TE_TOOLPRUNE_SESSIONS": "toolprune_sessions",
        "TE_TOOLPRUNE_PROTECT": "toolprune_protect",
        "TE_LOOPGUARD": "loopguard", "TE_LOOPGUARD_AGGR": "loopguard_aggr",
        "TE_LOOPGUARD_NUDGES": "loopguard_nudges",
        "TE_REPOMAP": "repomap", "TE_REPOMAP_TOKENS": "repomap_tokens",
        "TE_REPOMAP_PYTHON": "repomap_python", "TE_REPOMAP_TIMEOUT": "repomap_timeout",
        "TE_PI_EXTENSIONS": "pi_extensions", "TE_PI_CLI": "pi_cli"}
_ENV_THRESHOLDS = ("TE_SPIRAL_THRESHOLDS", "TE_LOOPGUARD_THRESHOLDS")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _old_names_first(items):
    """A file's settings with every old name before every new one, so the new one wins."""
    return sorted(items, key=lambda kv: canonical(kv[0]) == kv[0])


def load_config(root, repo_dir=None, env=None):
    """Merge defaults, the global file, the repository file and the environment."""
    env = os.environ if env is None else env
    cfg = LiveConfig()
    for label, path in (("global", os.path.join(root, "config.json")),
                        ("repo", os.path.join(repo_dir, "config.json") if repo_dir else None)):
        if path:
            for k, v in _old_names_first(_read(path).items()):
                cfg.set(k, v, source=f"{label} {path}")
    for var, key in _ENV.items():
        if env.get(var) not in (None, ""):
            cfg.set(key, env[var], source=var)
    for var in _ENV_THRESHOLDS:
        if env.get(var):
            try:
                cfg.set("loopguard_thresholds", json.loads(env[var]), source=var)
            except ValueError:
                cfg.warnings.append(f"{var}: not valid JSON")
    if str(env.get("TE_DISABLE", "")).strip().lower() in ("1", "true", "yes", "on"):
        cfg.enabled = False
    if cfg.loopguard == "stop":
        cfg.warnings.append("loopguard=stop acts as nudge: the plugin no longer stops a turn; a "
                            "turn still stuck after the redirect gets a notice saying how to stop "
                            "it (set loopguard=nudge to silence this)")
    return cfg


def _drop_old_names(current, key):
    """Remove every old spelling of `key` from a config file's contents."""
    for old, new in ALIASES.items():
        if new == key:
            current.pop(old, None)


def write_config(path, updates):
    """Merge `updates` into the JSON config at `path` (validating each key first), under the
    settings' current names. A `loopguard.<name>` key lands in `loopguard_thresholds`."""
    gone = [k for k in updates if canonical(k) in OBSOLETE]
    if gone:
        raise ValueError("; ".join(f"`{k}` no longer exists: {OBSOLETE[canonical(k)]}"
                                   for k in gone))
    probe = LiveConfig()
    bad = [k for k, v in updates.items() if not probe.set(k, v, source="te config")]
    if bad:
        raise ValueError("; ".join(probe.warnings))
    current = _read(path)
    # an old file's thresholds move to the new name, so nothing pinned is lost
    if "spiral_thresholds" in current:
        old = current.pop("spiral_thresholds")
        if isinstance(old, dict):
            current["loopguard_thresholds"] = dict(old, **current.get("loopguard_thresholds")
                                                   or {})
    for k in updates:
        name = _threshold_name(k)
        key = canonical(k)
        if name is not None:
            current.setdefault("loopguard_thresholds", {})[name] = \
                probe.loopguard_thresholds[name]
        elif key == "loopguard_thresholds":
            current.setdefault("loopguard_thresholds", {}).update(probe.loopguard_thresholds)
        else:
            _drop_old_names(current, key)
            current[key] = getattr(probe, key)
    _write(path, current)
    return current


def unset_config(path, keys):
    """Remove keys (by either name) from the JSON config at `path`, so the next source down
    applies again. `loopguard.<name>` removes one threshold; `loopguard_thresholds` all."""
    known = {f.name for f in fields(LiveConfig) if f.name != "warnings"}
    bad = [k for k in keys if canonical(k) not in known and _threshold_name(k) not in
           LOOPGUARD_KEYS]
    if bad:
        raise ValueError(f"unknown key(s): {', '.join(bad)}")
    current = _read(path)
    for k in keys:
        name = _threshold_name(k)
        if name is not None:
            for table in ("loopguard_thresholds", "spiral_thresholds"):
                (current.get(table) or {}).pop(name, None)
                if not current.get(table):
                    current.pop(table, None)
        else:
            key = canonical(k)
            current.pop(key, None)
            _drop_old_names(current, key)
    _write(path, current)
    return current


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
