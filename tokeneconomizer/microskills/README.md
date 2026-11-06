# Micro-skills

How micro-skills (see the [main README](../../README.md)) are learned, kept and injected.

Each finished session goes through three steps:

1. **Observe** (`observe.py`). Every shell command is split into segments and classified by
   *intent* (run the tests, run the interpreter, build or check, use the package tool, fetch,
   probe the VCS) and *form* (how it was spelled), and its outcome is read off the output as an
   environmental error class or a sign that tests ran. The words that name intents come from a
   `Vocabulary`. By default the plugin reads every repository with `GENERIC` -- every ecosystem at
   once, every edited file, and the proposer's language-neutral digest and prompt; the experiments
   use `PYTHON_GIT` or `NODE_WEB`, the paper's narrower reading.
2. **Propose.** `envfacts.py` turns recurring shapes of evidence into candidate facts, with no
   model call: a failing form followed by a working one (*form repair*), every fetch to a host
   refused (*boundary*), VCS probes that always come back empty (*empty probe*), a test label
   that loads once rewritten (*arg repair*), a missing environment variable that a later run
   sets (*env value*). `distiller.py` adds an LLM proposer that authors facts with host-checked
   detectors; in the plugin, `microskills=on` runs both (micro-skills is off by default).
3. **Keep or retire** (`lifecycle.py`). A candidate becomes active after confirmations on
   three sessions (for a proposed fact about the code, sessions that edited different files),
   retires on contradiction (for a command-spelling fact, two contradicting sessions in a row),
   and is injected only while the failure it prevents keeps occurring, or keeps being
   prevented, in at least 40% of the sessions that exercise it (over at least four).
   A person can also retire a fact by hand (`stage.forget_facts`; the plugin's
   `te microskills forget`): it leaves the block, and its id goes into its layer's
   `forgotten` set, so the fact is never formed again under that id.

`stage.PrefixStage` ties these together for a caller: render the block before a session, fold
the session in after it. The block is capped (`MAX_FACTS`, `MAX_TOKENS`), and a lint refuses any
fact that reads as completion or verification advice.

| Module | |
|---|---|
| `observe.py` | command segmentation, intent and form, error classes, `observe_task` |
| `envfacts.py` | `EnvFactsDistiller`: the five LLM-free fact families, rendering, caps |
| `distiller.py` | `Distiller`: the model-backed proposer (part of `microskills=on`) and its detector DSL |
| `lifecycle.py` | `Lifecycle`: formation, retirement, the firing gate |
| `stage.py` | `PrefixStage`, `ComposedDistiller`, `forget_facts` |
