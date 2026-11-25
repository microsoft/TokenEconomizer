"""Micro-skill synthesis and injection: learn short repository facts from finished sessions,
and state them at the start of the next one.

  * `observe`    -- what a finished session shows about its environment (classified commands);
  * `envfacts`   -- `EnvFactsDistiller`, the LLM-free environment-fact families;
  * `distiller`  -- `Distiller`, the optional paid proposer and its detector DSL;
  * `lifecycle`  -- the formation, retirement and firing gate every fact goes through;
  * `stage`      -- `PrefixStage`, which renders the block and folds each session back in.

Stdlib only; never imports the plugin runtime.
"""
from .distiller import Distiller
from .envfacts import EnvFactsDistiller
from .lifecycle import Lifecycle
from .observe import GENERIC, NODE_WEB, PYTHON_GIT, Vocabulary, classify_command, observe_task
from .stage import ComposedDistiller, PrefixStage, forget_facts, forgotten_facts

__all__ = ["Distiller", "EnvFactsDistiller", "GENERIC", "Lifecycle", "NODE_WEB", "PYTHON_GIT",
           "Vocabulary", "classify_command", "observe_task", "ComposedDistiller",
           "PrefixStage", "forget_facts", "forgotten_facts"]
