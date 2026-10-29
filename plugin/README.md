# TokenEconomizer plugin

How to install and use TokenEconomizer in Claude Code, Codex, the GitHub Copilot CLI and Pi. What
the plugin does and why is in the [main README](../README.md); its design and the verified
behaviour of each agent are in [`docs/PLUGIN.md`](../docs/PLUGIN.md).

- [Before you start](#before-you-start)
- Install and use it on [Claude Code](#claude-code) · [Codex](#codex) ·
  [Copilot CLI](#copilot-cli) · [Pi](#pi)
- [What to expect](#what-to-expect) · [Commands](#commands) · [Settings](#settings)
- [Repository map](#repository-map) · [Using with rtk](#using-with-rtk) ·
  [Model calls and API keys](#model-calls-and-api-keys) · [Troubleshooting](#troubleshooting)

## Before you start

- [ ] Your agent installed and signed in, any way it supports (a subscription or an API key), at a
  supported version ([Supported versions](../README.md#supported-versions))
- [ ] Python 3.10 or newer on `PATH` (or set `TE_PYTHON=/path/to/python3.10+`)
- [ ] Copilot CLI only: the repository's folder trusted by Copilot (it asks the first time you
  start it there: choose **Yes, and remember this folder for future sessions**); Copilot ignores a
  folder's `.github/copilot` settings otherwise

No extra API key is needed. Plugins load when a session starts, so start a new session after
installing. Claude Code, Codex and the Copilot CLI install from a clone of this repository:

```bash
git clone https://github.com/microsoft/TokenEconomizer.git
```

Keep the clone where it is: the installed plugin runs from it.

> **Where commands go.** Lines starting with `/` or `$` (`/plugin …`, `/tokeneconomizer:…`,
> `$tokeneconomizer:…`, `/te …`) are typed into **the agent's prompt**, not your shell. Shell
> commands are in `bash` blocks.

## Claude Code

**1. Install.**

```bash
claude plugin marketplace add /path/to/TokenEconomizer
claude plugin install tokeneconomizer@tokeneconomizer
claude plugin list        # should show tokeneconomizer@tokeneconomizer ... enabled
```

(Inside Claude Code the same thing is `/plugin marketplace add …` and `/plugin install …`.)

**2. Check it.** Start Claude Code in any project (`cd my-project && claude`) and type
`/tokeneconomizer:status`. It should print a short status block, not an error. Claude Code may ask
permission to run the plugin's script; allow it always.

**3. Record the tool list, once.**

```text
/tokeneconomizer:toolprune refresh-catalog
```

This records which tools a Claude Code session loads, with no model call. Do it once on this
machine, and again after Claude Code updates. **Don't skip it:** without it the deny list sees
only the few tools Claude Code defers, not the ones every session loads.

**4. Work normally.** When the plugin leaves tools out ([What to expect](#what-to-expect) says
when), the next session start says so once. To see or undo it:

```text
/tokeneconomizer:toolprune            # the tools it left out, and the evidence
/tokeneconomizer:toolprune restore    # give them back, from your next session; they stay back
```

The deny rules go into this repository's `.claude/settings.local.json`, which the plugin keeps
out of git; deny rules you wrote are never touched. To approve each deny list yourself instead:
`/tokeneconomizer:config set toolprune=propose`, then `/tokeneconomizer:toolprune apply`.

**Where its messages appear.** Claude Code shows the plugin's one-line messages (facts added,
tools left out) only in its detailed transcript: press **Ctrl+O**, where they read
`<event> says: TokenEconomizer: …`. The exception is a turn that still looks stuck: the plugin
also asks Claude to repeat that line word for word, so it shows in Claude's reply (under
`claude -p`, in the final reply). Claude may word it differently or leave it out; the detailed
transcript always has it. Press **Esc** to stop the turn; under `claude -p`, Ctrl+C ends the
run. With `--output-format stream-json --verbose`, every message also arrives as a
`hook_response` event.

Each slash command costs one model turn; the same commands are free from a terminal, as
`~/.tokeneconomizer/bin/te --host claude <command>` run inside the repository
([Commands](#commands)). To try the plugin for one session without installing it:
`claude --plugin-dir /path/to/TokenEconomizer/plugin`.

**Update.** `git pull` in the clone, then start a new session (or `/reload-plugins`): Claude Code
runs the plugin straight from the clone. If `/tokeneconomizer:status` still shows the old
behaviour, run `claude plugin update tokeneconomizer@tokeneconomizer`.

**Uninstall.**

1. In each repository where the plugin left tools out: `/tokeneconomizer:toolprune restore`.
2. `claude plugin uninstall tokeneconomizer@tokeneconomizer`, then
   `claude plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer` to delete what it learned. (A line the plugin added to a
   repository's `.git/info/exclude` stays; it is harmless.)

## Codex

**1. Install.**

```bash
codex plugin marketplace add /path/to/TokenEconomizer
codex plugin add tokeneconomizer@tokeneconomizer
```

**2. Approve its hooks.** Start `codex` in a repository. Codex asks whether to trust the folder
(**Trust and continue**), then shows **Hooks need review**: choose **Trust all and continue**.
Hooks run from the next session on (the session in which you approve gets no session-start
injection), so quit with `/exit` and start `codex` again. To approve later: `/hooks`.

**3. Check it.** Type `$tokeneconomizer:status`. Codex has no plugin slash commands, so the
plugin's commands are Codex skills with the same names as on the other agents:
`$tokeneconomizer:status`, `:microskills`, `:toolprune`, `:config` and `:repos`, each followed by
its arguments (`$tokeneconomizer:config set loopguard=off`); type `$tokeneconomizer` to pick one.
Name the skill rather than asking in plain words: a question such as *"what has TokenEconomizer
learned here?"* may be answered from the plugin's files without running its command. Commands
that change something (`config set` or `unset`, `toolprune apply` or `restore`, `repos migrate`
or `merge`) write to `~/.tokeneconomizer`, which Codex's sandbox keeps read-only, so Codex asks
first: choose **Yes, proceed**. From a terminal, inside the repository:
`~/.tokeneconomizer/bin/te --host codex <command>` (the launcher appears after the first
session).

**4. Work normally.** See [What to expect](#what-to-expect). On Codex the deny list can turn off
the sub-agent (`collaboration`), `goals` and image (`image_media`) packages, each only after it
has been loaded and gone unused long enough; a package you used even once is kept. Web access,
asking you questions and the bundled skills are never turned off. The list is written to the
project's `.codex/config.toml`, which Codex reads **only in a trusted project** (otherwise the
session start that announces it says so), and never into a `config.toml` your repository
commits: that stays a proposal, and the session start says why.
`$tokeneconomizer:toolprune restore` gives the packages back, and `toolprune measure` shows the
difference with no model call.

**Messages.** Under `codex exec`, Codex prints no hook messages, so the plugin's notices are not
shown, though a loop guard redirect still reaches the model. In the terminal UI, a turn still
stuck after its redirect shows "↳ Hook · TokenEconomizer: … Press Esc to interrupt it, then /stop
to end a command it left running": Esc interrupts the turn, but a command it started keeps
running in the background until `/stop`. Under `codex exec`, Ctrl+C ends the run and its command.

**When a session is learned from.** Quitting interactive Codex (`/exit`) keeps the session open
for `codex resume` and sends no session end, so the plugin learns from it at a later session
start, once it has been quiet for ten minutes. `codex exec` sessions are learned from as they
end.

**Update.** `git pull` in the clone, then `codex plugin add tokeneconomizer@tokeneconomizer`
again to refresh Codex's installed copy (your hook approval is kept).

**Uninstall.**

1. In each repository where the plugin turned packages off:
   `~/.tokeneconomizer/bin/te --host codex toolprune restore` (it leaves an empty `.codex/`).
2. `codex plugin remove tokeneconomizer@tokeneconomizer`, then
   `codex plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer`.

**A self-contained install.** Codex copies an installed plugin, and the copy uses the package in
your clone. To install a copy that carries the package itself, so the clone can move or go:

```bash
python3 /path/to/TokenEconomizer/scripts/build_plugin.py --out ~/te-marketplace
codex plugin marketplace add ~/te-marketplace
codex plugin add tokeneconomizer@tokeneconomizer
```

To update it, rebuild the bundle and run `codex plugin add tokeneconomizer@tokeneconomizer` again.

## Copilot CLI

**1. Install.**

```bash
copilot plugin marketplace add /path/to/TokenEconomizer
copilot plugin install tokeneconomizer@tokeneconomizer
copilot plugin list       # tokeneconomizer@tokeneconomizer ... (enabled)
```

Copilot loads the plugin from the clone directly, without copying it.

**2. Check it.** In Copilot's interactive mode, type `/tokeneconomizer:status`. Each slash command
is a model turn, and Copilot asks before running the plugin's script; choose **Yes**. Slash
commands work only in interactive mode; the plugin's notices also appear under `copilot -p`.

**3. Record the tool list, once.**

```text
/tokeneconomizer:toolprune refresh-catalog
```

With no model call. Do it once on this machine, and again after Copilot updates.

**4. Work normally.** See [What to expect](#what-to-expect). Copilot cannot remove a single
built-in tool, only turn off a whole MCP server, so the deny list turns off a server only when
**all** of its tools have gone unused long enough: one of yours from
`~/.copilot/mcp-config.json`, or the built-in `github-mcp-server`, which loads only when you are
signed in to GitHub. It is written to `.github/copilot/settings.local.json`, and the next session
start says so. `/tokeneconomizer:toolprune restore` turns the server back on, and
`toolprune measure` shows the difference with no model call.

From a terminal, inside the repository: `~/.tokeneconomizer/bin/te --host copilot <command>` (the
launcher appears after the first session). A turn that is still stuck after the loop guard's
redirect shows "● TokenEconomizer: …" in the conversation (and on stdout under `copilot -p`); press
**Esc** twice to interrupt it, or Ctrl+C to end a `copilot -p` run.

**Update.** `git pull` in the clone; Copilot loads the plugin from it, so nothing else is needed.

**Uninstall.**

1. In each repository where the plugin turned a server off: `/tokeneconomizer:toolprune restore`.
2. `copilot plugin uninstall tokeneconomizer@tokeneconomizer`, then
   `copilot plugin marketplace remove tokeneconomizer`.
3. Optional: `rm -rf ~/.tokeneconomizer`.

**With your own model key (BYOK).** If Copilot answers *"Access denied by policy settings"*, your
organization's policy refuses its models, and Copilot can use your own provider instead. The
plugin works the same way there, except for its model calls (below). In your shell profile:

```bash
export COPILOT_PROVIDER_TYPE=anthropic                  # or openai, azure
export COPILOT_PROVIDER_BASE_URL=https://api.anthropic.com
export COPILOT_PROVIDER_API_KEY=...                     # your provider's key
```

Then start Copilot with a model id your provider knows, for example `copilot --model
claude-sonnet-5` (an Anthropic provider wants its own ids, such as `claude-haiku-4-5`, not the
subscription's `claude-haiku-4.5`). Through **OpenRouter**, keep Copilot on the Anthropic wire, so
prompt caching works, and give it both ids:

```bash
export COPILOT_PROVIDER_TYPE=anthropic
export COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api
export COPILOT_PROVIDER_BEARER_TOKEN=...                 # your OpenRouter key
export COPILOT_PROVIDER_MODEL_ID=claude-haiku-4.5        # the id Copilot configures itself for
export COPILOT_PROVIDER_WIRE_MODEL=anthropic/claude-haiku-4.5   # the id OpenRouter bills
```

Copilot does not pass these variables to the plugin's hooks, so under BYOK its model calls
(micro-skills' proposer, the loop guard's judge) cannot reach your
provider: `copilot -p` asks to sign in instead. The first such answer leaves a line in
`~/.tokeneconomizer/errors.log` and pauses those calls, and the next session start says so once;
the facts that need no model call are still learned (`microskills=envfacts` keeps only those).
Everything else, including `refresh-catalog` and `measure`, needs no provider.
`~/.tokeneconomizer/bin/te --host copilot doctor` shows the route it sees, and a pause.

To check the whole plugin on your own setup:

```bash
~/.tokeneconomizer/bin/te --host copilot probe      # 7 short real sessions, in a temp repo
```

It builds a throwaway repository that is set up for pytest, without pytest installed, and whose
tests pass under `python -m unittest`. It runs the same prompt in real Copilot sessions with the
plugin, under a state directory of its own, and reports for each session whether the fact the
plugin learned was in the context and which test command the model ran first. Then it applies the
deny list it proposed, shows the tool-definition tokens of one more session, and restores the
list. It makes real model calls on your key or quota, and leaves those sessions in Copilot's
history.

## Pi

**1. Install.** The repository is a Pi package, so Pi installs it like any other:

```bash
pi install git:github.com/microsoft/TokenEconomizer
```

(Over SSH: `pi install git:git@github.com:microsoft/TokenEconomizer`. From a clone:
`pi install /path/to/TokenEconomizer`.) On Pi 0.99.x, install nothing: clone the repository and
start each session with `pi --extension /path/to/TokenEconomizer/plugin/pi/tokeneconomizer.ts`,
using your clone's full path; `git pull` in the clone updates it.

**2. Check it.** Start a new Pi session and type `/te status`. The commands are `/te status`,
`/te microskills`, `/te config …` and `/te repos`; from a terminal, inside the repository,
`~/.tokeneconomizer/bin/te --host pi <command>`. Tool pruning does not apply: Pi loads four
tools. In print and JSON modes the plugin's notices go to stderr. A turn that is still stuck
after the loop guard's redirect gets a notice: press **Esc** to abort it, or Ctrl+C to end a
`pi -p` run (a command Pi started then runs on until it finishes).

**3. Work normally.** See [What to expect](#what-to-expect).

**Update.** `pi update --extensions` (plain `pi update` does not update extensions).

**Uninstall.** `pi remove git:github.com/microsoft/TokenEconomizer` (the source you installed
from); `pi config` turns it off without removing it. Optional: `rm -rf ~/.tokeneconomizer`.

## What to expect

The plugin works session by session: what it learns from a repository's earlier sessions is
applied to the ones that follow. Facts and the deny list take effect when a session starts; the
loop guard acts during a turn. Each agent learns on its own (what Claude Code learned in a
repository is not used by Codex there), while settings are shared by every agent.

- **Facts** (once you turn micro-skills on; it is off by default). Two sources propose them, in
  any language or toolchain. The first needs no model call: the same environment failure in a few
  sessions (a tool, interpreter, build or test command that fails as typed and works spelled
  another way). The second runs every `microskills_propose_every` sessions (3 by default): a model
  reads digests of the recent sessions (the commands run, including build, test and lint commands,
  the files edited, and each failing command with its error line, with paths relative to the
  repository) and writes facts about the repository, each with patterns that test it against later
  sessions. A fact becomes active once three sessions confirm it (for a proposed fact about the
  code, sessions that edited different files), and is injected at every session start while the
  failure it prevents keeps showing up, or keeps being avoided, in at least 40% of the sessions
  where it applies (over at least four). It is retired when a session contradicts it (for a
  command-spelling fact, two in a row). How much this helps depends on the repository's state and
  the tasks you run in it. A session start that adds facts says so once, in one line. A resumed
  session keeps the facts it started with, word for word, so its cached prompt still matches.
  `microskills` shows the facts injected and those still forming.
- **Deny list.** A tool, package or server that is loaded but never used here is left out without
  asking you, once it has gone unused over `toolprune_evidence` prompts (10 by default) across at
  least `toolprune_min_sessions` sessions (2), or over `toolprune_sessions` sessions of any length
  (5); Claude Code's `NotebookEdit` and Codex's `image_media` package need three times as long. A
  session that ran no prompt does not count. The list is written once the repository's sessions
  have been idle for `toolprune_quiet_minutes` (60 by default), so that no conversation you resume
  loses its prompt cache (a list that has waited a day is written anyway), and it applies from the
  next session, whose start says so once. `toolprune` shows a list still waiting.
  `toolprune restore` gives back what the plugin removed, and only that; what you give back is
  never removed again (`toolprune apply NAME` removes one again). With `toolprune=propose`, the
  list is only proposed, the session start says so, and nothing changes until you run
  `toolprune apply`. `toolprune=off` stops new lists but leaves an applied one in place;
  `toolprune restore` gives the tools back. A settings file the plugin creates is added to
  `.git/info/exclude`. Each checkout keeps its own settings file: a repository's clones share what
  was learned, and each gets the list when one of its own sessions ends.
- **Loop guard.** Quiet on normal turns, and idle until `loopguard.envelope_min_samples` turns in
  a repository (5 by default) have ended well: on their own, with no failing check after the last
  edit. After that, a turn that keeps hitting the same failures, or piles up errors, after
  `loopguard.min_turns` tool calls is put to a judge model. Only a "stuck" verdict with confidence
  `loopguard.judge_confidence` (0.7) or more attaches a redirect (written by that model) to a tool
  result (at the `aggressive` preset, an ambiguous verdict past the envelope also does), at most
  `loopguard_nudges` (1) per turn; if the turn is still stuck after that, a one-line notice tells
  you how to stop it. A long turn with no sign of looping is never judged. A turn past
  `loopguard.backstop_mult` (2.5) times the repository's usual length that also keeps churning
  edits through the same failures is redirected even if the judge disagrees. The plugin never
  stops a turn itself. Where the notice appears, and how to stop the turn, on each agent and in
  each mode: [When a turn gets stuck](../README.md#when-a-turn-gets-stuck).
- **Cost.** `status` shows each session's tokens, priced after it ends, and the plugin's own
  overhead: the loop guard's judge, and micro-skills' proposer when it is on.

## Commands

| Command | What it does |
|---|---|
| `status` | what it has learned in this repository, what it did, and what your sessions cost |
| `microskills` | the facts it injects at session start, and those still forming |
| `toolprune` | the deny list: what it left out, what it would leave out next, and the evidence |
| `toolprune apply [ITEM …]` | apply a waiting proposal (with `toolprune=propose`), or the named items; from the next session |
| `toolprune restore [ITEM …]` | give back the plugin's entries (all, or the named ones); they are not removed again |
| `toolprune refresh-catalog` | record the tools a session loads (Claude Code, Copilot) |
| `toolprune measure` | the request with and without the proposal |
| `config` | the settings: what is on, and every value in effect |
| `config set KEY=VALUE … [--global]` | change settings for this repository (or every one); see [Settings](#settings) |
| `config unset KEY … [--global]` | remove the setting at this level, so the global value (or the default) applies |
| `repomap` / `repomap refresh` | the cached repository map / make it now (see [Repository map](#repository-map)) |
| `repos` | every repository it keeps state for: how it was identified, its sessions per agent, the checkouts seen |
| `repos migrate` | file state an earlier version recorded under the repository it belongs to (runs by itself, once, after an upgrade) |
| `repos merge KEY [INTO]` | merge one repository's state into another's (default: this one), for state whose checkouts are gone |
| `doctor` | check Python, the state directory, the agents found, how this directory was identified, and the error log |

How to run them:

| Agent | Inside the agent | From a terminal, in the repository |
|---|---|---|
| Claude Code | `/tokeneconomizer:<command>` | `~/.tokeneconomizer/bin/te --host claude <command>` |
| Codex | `$tokeneconomizer:<command>` (`status`, `microskills`, `toolprune`, `config`, `repos`) | `~/.tokeneconomizer/bin/te --host codex <command>` |
| Copilot CLI | `/tokeneconomizer:<command>` (interactive mode) | `~/.tokeneconomizer/bin/te --host copilot <command>` |
| Pi | `/te <command>` | `~/.tokeneconomizer/bin/te --host pi <command>` |

`doctor` and `repomap` run from a terminal (or as `/te …` on Pi); there is no slash command or
Codex skill for them.

Inside an agent, each command is one model turn; from a terminal it costs nothing. Codex asks
before a command that changes something (`config set`, `toolprune restore`, ...), since it
writes outside Codex's sandbox: choose **Yes, proceed**. The Copilot CLI runs these commands in
its interactive mode only and asks before each: choose **Yes**.

The launcher in `~/.tokeneconomizer/bin` is written by the first session; before that, use
`/path/to/TokenEconomizer/plugin/scripts/te`. From a plain terminal, `--host` defaults to the
agent that last worked in the repository.

## Settings

Each of the three mechanisms can be turned on or off on its own, and each has numbers you can
set by hand. Settings apply to the current repository, or to every repository with `--global`,
and are shared by every agent working in it. The examples use Claude Code's form; the same
`config` command on each agent is:

| Agent | Set one value in this repository |
|---|---|
| Claude Code | `/tokeneconomizer:config set loopguard=off` |
| Codex | `$tokeneconomizer:config set loopguard=off` |
| Copilot CLI | `/tokeneconomizer:config set loopguard=off` (interactive mode) |
| Pi | `/te config set loopguard=off` |
| any agent, from a terminal | `~/.tokeneconomizer/bin/te config set loopguard=off` |

**Turn a mechanism on or off:**

```text
/tokeneconomizer:config set microskills=on               # micro-skills, in this repository
/tokeneconomizer:config set toolprune=off loopguard=off
/tokeneconomizer:config set microskills=on --global       # in every repository
/tokeneconomizer:config                                # what is on, and every value in effect
```

| Mechanism | Key | `on` means | Other values |
|---|---|---|---|
| Micro-skills (off by default) | `microskills` | `envfacts+distiller`: facts found without model calls, plus a model proposing facts every `microskills_propose_every` sessions | `off`; `envfacts`: only the facts found without model calls; `distiller`: only the proposer |
| Tool pruning | `toolprune` | `auto`: applied without asking once your sessions have been idle for `toolprune_quiet_minutes` (or a day after it formed), from the next session, which announces it | `off`; `propose`: only proposed, applied when you run `toolprune apply` |
| Loop guard | `loopguard` | `nudge`: a judge model decides whether a looping turn is stuck; if so, up to `loopguard_nudges` mid-turn redirects, then a notice to you if the turn is still stuck | `off` (no judge either); `stop` is read as `nudge`, with a warning |

**Tune a mechanism:**

```text
/tokeneconomizer:config set microskills_max_facts=2            # inject at most two facts
/tokeneconomizer:config set toolprune_evidence=20              # wait for twenty unused prompts
/tokeneconomizer:config set loopguard_nudges=2 loopguard.min_turns=30
/tokeneconomizer:config unset loopguard.min_turns              # back to the default
```

| Key | Default | What it sets |
|---|---|---|
| `microskills_max_facts` | `4` | the most facts from repeated failures injected at session start (the proposer's facts are not capped) |
| `microskills_max_tokens` | `150` | the size of those facts' block, in tokens (about 4 characters each) |
| `microskills_propose_every` | `3` | sessions between runs of the fact proposer (the first run comes one session early: after the second, by default) |
| `toolprune_evidence` | `10` | prompts a never-used tool must go unused, over at least `toolprune_min_sessions` sessions, before it is left out (three times as many for `NotebookEdit` and Codex's `image_media`; the web is never proposed) |
| `toolprune_min_sessions` | `2` | sessions those prompts must span; `1` lets a single session's prompts decide |
| `toolprune_sessions` | `5` | or this many sessions, however few prompts they ran |
| `toolprune_quiet_minutes` | `60` | how long the repository's sessions must have been idle before `auto` writes a deny list, so that no conversation resumed while its prompt cache is warm loses it (a list is written at the latest a day after it formed); `0` writes at once |
| `backfill_sessions` | `50` | on Claude Code, how many of the repository's earlier sessions (newest first, from `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`) the first session start learns from, in the background, so a new install does not begin empty; they are folded oldest first by the free layer only (no model call). `0` turns it off; `te backfill` reads again |
| `loopguard_aggr` | `default` | the guard's threshold preset: `conservative`, `default` or `aggressive` |
| `loopguard_nudges` | `1` | redirects per turn before the guard only tells you |
| `loopguard.<name>` | from the preset | one guard threshold, set by hand (below) |

The guard looks for a turn that keeps editing the same files and hitting the same failures. It
also learns how long this repository's turns usually run when they end well (on their own, with
no failing check after the last edit): the *envelope*. Until `envelope_min_samples` such turns
exist the guard does nothing; after that, some thresholds are learned from those turns, on top
of the preset. A value you set with `loopguard.<name>` wins over both. Defaults at each preset:

| `loopguard.<name>` | `conservative` | `default` | `aggressive` | What it sets |
|---|---|---|---|---|
| `min_turns` | 28 | 20 | 14 | tool calls in a turn before it can count as stuck |
| `edit_churn` | 5 | 4 | 3 | edits to one file that count as churn |
| `distinct_edit_files` | 3 | 3 | 3 | files edited that count as scattered |
| `error_outputs` | 8 | 6 | 4 | failing tool results that count as sustained errors |
| `stagnation` | 0.30 | 0.20 | 0.12 | share of failures that repeat an earlier one |
| `envelope_pct` | 90 | 90 | 90 | percentile of the repository's well-ended turns the envelope starts from |
| `envelope_margin` | 1.5 | 1.25 | 1.0 | multiplier on that percentile |
| `envelope_min_samples` | 7 | 5 | 3 | well-ended turns needed before the guard acts |
| `backstop_mult` | 3.5 | 2.5 | 1.8 | past this multiple of the envelope, a turn the rule calls stuck is redirected even if the judge disagrees, then flagged to you |
| `judge_confidence` | 0.85 | 0.7 | 0.55 | how sure the judge must be that a turn is stuck |
| `gate_min_turns` | 24 | 18 | 12 | tool calls before the judge is asked (never fewer than `min_turns`) |
| `gate_error_outputs` | 8 | 8 | 8 | failing results that ask the judge without a repeated failure |

**Repository map** (off by default; see [Repository map](#repository-map)):

| Key | Default | What it sets |
|---|---|---|
| `repomap` | `off` | `on`: a map ranked toward the session's first prompt; `start`: the cached map at session start |
| `repomap_tokens` | `2048` | the map's size, in tokens |
| `repomap_python` | auto | the Python that has aider installed |
| `repomap_timeout` | `8` | seconds the first prompt may wait for a ranked map before the cached one is used |

**Other settings:**

| Key | Default | Values |
|---|---|---|
| `paid_model` | `""` | model for the plugin's own calls; empty: `haiku` on Claude Code, the agent's own default model on Codex and Pi, on Copilot `COPILOT_PROVIDER_MODEL_ID` or `COPILOT_MODEL` (unless it is `auto`), else a BYOK provider's Haiku, else Copilot's own default |
| `shadow` | `false` | decide and log everything, act on nothing |
| `mode` | `auto` | `interactive` or `headless` to override what the plugin detects |

**Where settings live, and when they apply.** For one session, an environment variable wins:
`TE_` plus the setting's name in capitals (`TE_MICROSKILLS`, `TE_TOOLPRUNE_EVIDENCE`,
`TE_LOOPGUARD_NUDGES`, ...), and `TE_LOOPGUARD_THRESHOLDS='{"min_turns": 30}'` for the guard's
thresholds; `TE_DISABLE=1` turns the whole plugin off. Otherwise the repository's
settings win over the global ones, and those over the defaults; `unset` goes back one level. A
change applies from the agent's next hook call: the guard at the next tool call, facts and the
deny list at the next session start. A new fact-size cap shows once the next session has ended.

**Earlier names.** Before the mechanisms were renamed they were `inject`, `untool` and `spiral`,
with settings such as `inject_max_facts`, `untool_evidence`, `spiral_nudges`, `spiral.<name>` and
variables `TE_INJECT`, `TE_UNTOOL`, `TE_SPIRAL`, ...; all of them are still read (`spiral=guard`
as `loopguard=stop`, which works as `nudge`), and a new name set alongside its old one wins.
`config set` writes the new name. The earlier commands `te facts` and `te untool`, and
`/tokeneconomizer:facts` and `/tokeneconomizer:untool`, still work.

## Repository map

An opt-in fourth mechanism: aider's map of the repository's key files and symbols (signatures
only, ranked by how much of the code refers to them), added to the session's context so the
agent knows where things are before it starts reading files. Turn it on per repository:

```bash
pip install aider-chat                      # or: pipx install aider-chat / uv tool install aider-chat
~/.tokeneconomizer/bin/te config set repomap=on      # settings are shared by every agent
```

- **What goes in.** At most `repomap_tokens` tokens (default 2048), once per session. On Claude
  Code, the Copilot CLI and Pi it is ranked toward the identifiers in your first prompt; if that
  takes longer than `repomap_timeout` seconds (about 5 s on a repository of Django's size, well
  under a second on a small one), the cached map is used instead. Codex gets the cached map at
  session start. `repomap=start` does that on every agent and never delays a prompt.
- **What it reads.** The files `git ls-files` lists, in every language aider understands,
  without vendored code, minified files or files over 256 KB.
- **Where it runs.** In whichever Python has aider: `repomap_python` if you set it, else the one
  behind an installed `aider` command, else the plugin's own. `te doctor` says which it found.
  The plugin never imports aider itself.
- **What it writes.** Nothing in the repository. The map and aider's tag cache live in the
  plugin's state directory. The map is cached per commit and remade in the background when
  `HEAD` moves.
- **What it costs.** No model calls. The map is part of every request of the session, so it
  costs on sessions that already knew where to look and saves on those that would have searched.
  `te status` shows its size.

## Using with rtk

[rtk](https://github.com/rtk-ai/rtk) compresses shell output. Install it on its own (`rtk init
-g`); it does not need to be part of this plugin, and the two do not get in each other's way:
rtk rewrites shell commands before they run, and this plugin reads sessions, injects context and
watches tool results. The plugin reads `rtk`-prefixed commands as the commands they wrap, and
rtk adds no tools for the deny list to see. `te doctor` reports an installed rtk.

One interaction to know: with rtk 0.45.0, `rtk pytest` prints nothing when pytest is missing (it
exits with code 1). The agent sees no error and the plugin cannot learn the "`pytest` fails here;
`X` works" fact from it. Commands that name their interpreter (`.venv/bin/python -m pytest`) are
not rewritten, so the commands that fact recommends keep working.

## Model calls and API keys

**With the defaults, the plugin's only model calls are the loop guard's**: the judge, asked about
a turn that shows signs of looping, and, when the judge finds the turn stuck, the redirect it
writes. Tool pruning never calls a model. Turning micro-skills on adds its fact proposer, once
every `microskills_propose_every` sessions (`microskills=envfacts` keeps only the facts found
without a model call); `loopguard=off` turns the judge off along with the guard.

These calls run through **your own agent's CLI**: `claude -p`; `codex exec`; `copilot -p`, with
every tool removed; and `pi -p`, with nothing loaded but the model. So they use whatever your
sessions already use (a Claude subscription or Anthropic API key, your Codex login or OpenAI key,
Copilot's subscription, or the provider and login Pi is set up with), are billed there, and are
shown in `status`. Copilot with BYOK is the exception; see [Copilot CLI](#copilot-cli).

A call that gets no reply leaves a line in `~/.tokeneconomizer/errors.log`. If the CLI answers
that it is not signed in, that agent's calls are also paused (the loop guard then redirects only
at its backstop), the next session start says so once, and `config set microskills=on` (or
`loopguard=on`) tries again. `toolprune measure` on Claude Code makes its two calls the same way,
and only when you run it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| a command prints an import error, or the hooks seem to do nothing | run `~/.tokeneconomizer/bin/te doctor` (or `plugin/scripts/te doctor` in the clone) and read its errors.log lines |
| errors.log says the package "was not found for the plugin" | the clone the plugin was installed from moved or is gone: add it again, or use the [self-contained install](#codex) |
| `TokenEconomizer needs Python >= 3.10 (this is 3.x)` | install Python 3.10+ or set `TE_PYTHON` to one |
| the deny list leaves nothing out after many sessions | run `toolprune refresh-catalog` (Claude Code, Copilot) |
| a Codex deny list was applied but nothing changed | trust the project in Codex; `~/.tokeneconomizer/bin/te --host codex doctor` says whether it is trusted |
| errors.log has `sidecall copilot: no reply`, and a session start says the plugin's model calls are paused | they cannot use a Copilot BYOK provider ([Copilot CLI](#copilot-cli)): micro-skills keeps its facts that need no model call (`microskills=envfacts` stops trying the proposer), and the loop guard redirects only at its backstop. Once Copilot is signed in, `config set microskills=on` (or `loopguard=on`) tries again |
| `te` shows another agent's state | add `--host claude`, `codex`, `copilot` or `pi` |
