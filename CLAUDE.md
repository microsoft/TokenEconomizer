# TokenEconomizer

A plugin for Claude Code, Codex, the Copilot CLI and Pi (experimentally opencode, Cursor, Grok Build,
Hermes Agent, the Antigravity CLI and Factory Droid) that reads finished sessions and changes what
later sessions in the same repository pay for: micro-skills, tool pruning, and a loop guard.
See `README.md` for the overview and the method, `docs/PLUGIN.md` for the design and the verified
agent behavior, and `docs/ARCHITECTURE.md` for how the package fits together.

## Layout
- `tokeneconomizer/` — the one Python package, standard library only
  - `microskills/` — learn repository facts from finished sessions; render the injected block
  - `toolprune/` — unused-tool evidence and the deny-list proposal; Codex capability packages
  - `loopguard/` — the per-call failure-loop guard and its per-repository envelope
  - `trace/` — each agent's session-log format (Claude Code, Codex, Copilot CLI, Pi, opencode,
    Cursor, Grok Build, Hermes Agent, Antigravity CLI, Factory Droid)
  - `plugin/` — the runtime: hook dispatch, per-repository state, settings, the `te` CLI
  - `config.py`, `ledger.py`, `pricing.py`, `domain.py`, `stage.py` — shared by all three
- `plugin/` — the installable plugin directory: manifests per agent, hook files, launchers
  (`scripts/te`, `scripts/te-hook`), slash commands (`slash-commands/`, named in the Claude Code
  and Copilot manifests, since Codex would turn a `commands/` folder into duplicate skills), Codex
  skills (`codex-skills/`, Codex having no plugin slash commands), the Pi extension, the opencode
  plugin (`opencode/`, one file whose default export serves 1.x via `server` and 2.x via
  `setup`), the Hermes plugin, and the Droid manifest (`plugin/.factory-plugin/`, with
  `plugin/hooks/droid-hooks.json`); `lib/` links the package
- `experiments/` — benchmark-only: `te_bench/` (adapters, arms, facade, benchmark-only
  controllers), `te_harness/` (Docker runners), `scripts/`, `workloads/`, its own `tests/`;
  its `README.md` is the run book
- `package.json` — the repository as a Pi package (`pi install git:github.com/…`); it names
  the same extension as `plugin/package.json`, which a test pins
- `scripts/build_plugin.py` — a self-contained plugin bundle for marketplace installs
- `tests/` — offline tests for the package and the plugin

## Conventions
- The mechanisms (`microskills`, `toolprune`, `loopguard`, `trace`) never import
  `tokeneconomizer.plugin`, and nothing under `tokeneconomizer/` imports `experiments/`; both
  are pinned by `tests/test_plugin_package.py`.
- Standard library only, Python >= 3.10. A hook must start fast and fail open: any exception in
  a hook becomes `{}` and a line in `~/.tokeneconomizer/errors.log`.
- On-disk state names (`injection.json`, `untool.json`, `envelope.json`, event kinds) are a
  compatibility surface; renaming them strands what users have learned.
- Settings keep their earlier names as aliases (`inject`, `untool`, `spiral`, ...; see
  `plugin/config.py`); write new code against the current names.
- No run outputs in the repository: transcripts, rows, streams, figures rendered from runs and
  tables derived from them stay out of git (`.gitignore`). The one exception is the paper's
  headline results in `README.md` (the line under the badges, the Results section, and the
  teaser's closing results, which read `docs/assets/results_overview.py`'s `ROWS`; that script
  also draws `results_overview.*`, which the README no longer shows): quoted from the paper's
  Table 1 and attributed to it. Change them only with the paper, and redraw both figures with
  `python docs/assets/results_overview.py` and `python docs/assets/teaser.py --plex FONTS`
  (its docstring says where to get IBM Plex). The teaser's middle act is a schematic with
  made-up sessions and no numbers.
- The plugin's own text (`plugin/`, its manifests, slash commands and skills, and what the
  hooks print) makes no claims of effect.
- Tests: `python -m pytest tests experiments/tests`. A change to what the plugin learns or
  decides should be checked against recorded sessions, not only the unit tests.
- Every model-backed test goes through the agent's own CLI on a provider you control
  (OpenRouter in development); never hard-code a key.
