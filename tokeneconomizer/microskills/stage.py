"""`PrefixStage`: the micro-skill lifecycle hook -- render before a session, learn after it.

It builds the distiller the profile names -- the LLM-free environment facts (`envfacts`), the
paid proposer (`distiller`), or both (`envfacts+distiller`, the free layer first and its claims
handed to the proposer as already stated) -- renders its block for the next session, and folds
each finished session back in, reporting the paid proposer's cost for that session.
"""
import os

from ..names import repo_filename
from ..stage import Stage
from . import distiller as _distiller
from .distiller import Distiller


# ---- composition: the free environment layer under the paid manual -------------------
class ComposedDistiller:
    """`envfacts` first, the paid manual on top, driven as one distiller.

    The environment layer renders first (its block is what the proposer is told not to
    restate); both fold every finished task; each persists in its own state dir. Overhead is
    the proposer's alone -- the environment layer is LLM-free by construction."""
    def __init__(self, env, dist):
        self.env, self.dist = env, dist

    @property
    def opt_tokens(self):
        return self.dist.opt_tokens

    @property
    def now(self):
        return self.dist.now

    def render(self, task=None):
        a = self.env.render(task)
        b = self.dist.render(task)
        return (a + ("\n" if a and b else "") + b) if (a or b) else ""

    def injected_ids(self):
        return self.env.injected_ids()

    def seed_injected(self, ids):
        self.env.seed_injected(ids)

    def update(self, transcript_path, iid="", resolved=None, edited=None):
        self.env.update(transcript_path, iid=iid, resolved=resolved, edited=edited)
        self.dist.update(transcript_path, iid=iid, resolved=resolved, edited=edited)

    def save(self, root):
        self.env.save(root)
        self.dist.save(root)

    def debug_row(self):
        row = dict(self.dist.debug_row())
        row.update(self.env.debug_row())
        row["_profile"] = "envfacts+distiller"
        return row


# ---- lifecycle stage -------------------------------------------------------
class PrefixStage(Stage):
    """Injection lifecycle hook: render the manual before a task, fold the finished
    task back into the distiller after it, and report the paid proposer's per-task
    token delta for the amortized ledger."""
    name = "prefix"

    def __init__(self, cfg, repo, results_root, fresh,
                 runner=None, optimizer_model="agent", adapter=None, agent=None):
        self.results_root = results_root
        self.adapter = adapter
        self.agent = agent
        # The Distiller is the single source of truth for the proposer cadence: rebind
        # the module global that update()'s gate reads (process-local, idempotent).
        pe = int(getattr(cfg, "propose_every", 0) or 0)
        if pe > 0:
            _distiller.PROPOSE_EVERY = pe
        profile = getattr(cfg, "profile", "distiller") or "distiller"
        # The environment-fact layer reads intents through a domain vocabulary; the adapter
        # supplies it when it has one (SWE-bench: Python/git; Web-Bench: Node/npm).
        vocab = adapter.env_vocabulary() if hasattr(adapter, "env_vocabulary") else None
        env = None
        if "envfacts" in profile:
            from .envfacts import EnvFactsDistiller
            caps = {"max_facts": getattr(cfg, "max_facts", 0) or None,
                    "max_tokens": getattr(cfg, "max_tokens", 0) or None}
            env = (EnvFactsDistiller(repo, vocab=vocab, **caps) if fresh
                   else EnvFactsDistiller.load(repo, results_root, vocab=vocab, **caps))
        if profile == "envfacts":
            # LLM-free environment facts alone. Same render/update/save interface, no
            # runner, opt_tokens stays 0 -- the ledger sees no overhead.
            self.reason = "envfacts"
            self.distiller = env
        else:
            # The paid proposer; with `envfacts+distiller` it is told which claims the free
            # layer already injects, so it authors only facts beyond them.
            external = env.injectable_claims if env is not None else None
            dist = (Distiller(repo, runner=runner, model=optimizer_model,
                              external_facts=external, vocab=vocab) if fresh
                    else Distiller.load(repo, results_root, runner=runner,
                                        model=optimizer_model, external_facts=external,
                                        vocab=vocab))
            if env is not None:
                self.reason = "envfacts+manual-distill"
                self.distiller = ComposedDistiller(env, dist)
            else:
                self.reason = "manual-distill"
                self.distiller = dist

    def render(self, task=None):
        """The firing-gated repo manual injected into the NEXT task's prefix."""
        return self.distiller.render(task=task) if self.distiller else ""

    def injected_ids(self):
        """The environment facts the last `render` chose, [] without that layer."""
        get = getattr(self.distiller, "injected_ids", None)
        return get() if get else []

    def after_task(self, task, row, run_dir_path, srow):
        """Fold the finished task into the fact lifecycle; return (opt_tokens, reason).
        The paid proposer accrues cumulatively on distiller.opt_tokens; capture THIS
        task's delta for the per-task ledger (same effective-token basis). A task that names
        `injected_ids` says which environment facts it was given (see `seed_injected`)."""
        transcript = os.path.join(run_dir_path, "transcript.jsonl")
        seed = getattr(self.distiller, "seed_injected", None)
        if seed and task.get("injected_ids") is not None:
            seed(task["injected_ids"])
        before = self.distiller.opt_tokens
        self.distiller.update(transcript, iid=task["instance_id"],
                              resolved=srow.get("resolved"))
        self.distiller.save(self.results_root)
        opt_tokens = self.distiller.opt_tokens - before
        srow["manual_facts"] = self.distiller.debug_row()
        return opt_tokens, self.reason


# ---- forgetting a fact by hand ---------------------------------------------
def _saved_layers(repo, root, vocab=None):
    """The distillers with state saved under `root`, loaded: the environment layer first."""
    from . import envfacts
    out = []
    for cls, sub in ((envfacts.EnvFactsDistiller, envfacts.STATE_SUBDIR),
                     (Distiller, _distiller.STATE_SUBDIR)):
        if os.path.exists(os.path.join(root, sub, repo_filename(repo) + ".json")):
            out.append(cls.load(repo, root, vocab=vocab))
    return out


def forget_facts(repo, root, ids, vocab=None, undo=False):
    """Forget the facts `ids` (or, with `undo`, bring them back) in whichever saved layer holds
    each, and save the layers that changed. A forgotten fact is no longer rendered, and is not
    learned again under its id. Returns [(id, changed, claim)] in the order given. When an id
    is unknown to every layer nothing is changed, and KeyError carries the unknown ids."""
    layers = _saved_layers(repo, root, vocab)
    owner = {}
    for layer in layers:
        for fid in layer.known_ids():
            owner.setdefault(fid, layer)
    unknown = [fid for fid in ids if fid not in owner]
    if unknown:
        raise KeyError(unknown)
    out, changed = [], []
    for fid in dict.fromkeys(ids):
        layer = owner[fid]
        did = layer.unforget(fid) if undo else layer.forget(fid)
        if did and layer not in changed:
            changed.append(layer)
        fact = layer.facts.get(fid)
        claim = (layer._claim_of(fact) if fact is not None
                 else layer.forgotten.get(fid, {}).get("claim", ""))
        out.append((fid, did, claim))
    for layer in changed:
        layer.save(root)
    return out


def forgotten_facts(repo, root, vocab=None):
    """{id: the claim it made} for every forgotten fact saved under `root`."""
    out = {}
    for layer in _saved_layers(repo, root, vocab):
        for fid, rec in layer.forgotten.items():
            out.setdefault(fid, rec.get("claim", ""))
    return out
