# TokenEconomizer: Experience-Driven Black-Box Cost Control for Coding Agents

<p align="center">
  Aaron J. Li<sup>1</sup>,
  Weijia Xu<sup>2</sup>,
  Chandan Singh<sup>2</sup>,
  Zixiao Chen<sup>3</sup>,
  Jeevana Priya Inala<sup>2</sup>,
  Charlotte Siska<sup>3</sup>,
  <br>
  Acar Tamersoy<sup>3</sup>,
  Bin Yu<sup>1</sup>,
  Ion Stoica<sup>1</sup>,
  Michel Galley<sup>2</sup>,
  Jianfeng Gao<sup>2</sup>
</p>
<p align="center">
  <sup>1</sup>UC Berkeley &nbsp; <sup>2</sup>Microsoft Research &nbsp; <sup>3</sup>Microsoft Security
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2610.22732"><img src="https://img.shields.io/badge/arXiv-2610.22732-b31b1b.svg" alt="arXiv"></a>
  <a href="https://tokeneconomizer.github.io"><img src="https://img.shields.io/badge/project-page-2f6fb0.svg" alt="Project page"></a>
  <a href="https://x.com/aaron_jli/status/2114797313613053952"><img src="https://img.shields.io/badge/X-post-000000.svg?logo=x&logoColor=white" alt="X post"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache 2.0 license">
</p>

<p align="center">
  Our measurement on SWE-Bench tasks: <b>18.4–30.9% lower mean cost</b> across five agent–model
  combinations, cheaper in <b>all 30</b> repository × combination cells, and Pass@1 up in
  <b>4 of 5</b>. <a href="#results">Results</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-reduced-motion: reduce)" srcset="docs/assets/teaser.png">
    <img src="docs/assets/teaser.gif" width="100%" alt="An animation that opens on a title card: TokenEconomizer, experience-driven black-box cost control for coding agents, with its three mechanisms (micro-skills, tool pruning, failure-loop intervention), the agents it works with (Claude Code, Codex, GitHub Copilot CLI, Pi) and the UC Berkeley and Microsoft logos. Then a schematic of six sessions in one repository, each a bar of what it cost: the work itself plus three kinds of waste, namely re-learning the repo, tools never used, and getting stuck in loops. Each of the first three sessions teaches TokenEconomizer one thing, in order: 1 micro-skills remember how the repo works (run tests with python -m pytest), 2 tool pruning skips tools never used here (Read and Edit kept, Notebook and Browser left out), and 3 failure-loop intervention spots loops and steps in (the same error three times meets a nudge to try another way). In the session after each lesson that waste shrinks, so the bars step down below a dashed line marking the cost without TokenEconomizer. It ends on results measured on SWE-Bench tasks: 18.4–30.9% lower mean cost across five agent–model combinations, cheaper than default in 30 of 30 repository × combination cells, and Pass@1 up in 4 of 5; per combination, cost 23.6% lower and Pass@1 +1.3 for Claude Code with Sonnet 5, 30.9% and −1.0 with Opus 4.8, 24.7% and +0.9 for Codex with GPT-5.6 Terra, 26.1% and +1.6 with GPT-5.6 Sol, and 18.4% and +2.6 for Copilot CLI with Sonnet 5.">
  </picture>
</p>

TokenEconomizer makes coding agents cheaper to run in the repositories you work in often,
without changing the agent itself. It learns from the sessions an agent has already run in a
repository, and uses that experience to cut waste in the sessions that follow. It has three
mechanisms:

- **Micro-skills: remember how the repository works.** Sessions keep rediscovering the same
  facts: a build step that must run first, the spelling of a command that works here
  (`python -m pytest` where `pytest` is missing, `npx tsc` where `tsc` is), an environment
  variable a command needs, where generated files live. Micro-skills learns such facts, in any
  language, from two sources: failures that repeat across sessions, and a model that reviews the
  recent sessions every *k* sessions (*k* = 3) and proposes facts. A fact is stated at the start
  of each session once three sessions have confirmed it, and dropped when later sessions
  contradict it.
- **Tool pruning: stop loading tools that go unused.** Every loaded tool's definition is sent with
  every request. A tool that goes unused over *p* prompts spread across at least *m* sessions, or
  over *s* sessions of any length (*p* = 10, *m* = 2, *s* = 5), is left out of the next sessions
  through the agent's own settings. One command brings it back.
- **Failure-loop intervention (the loop guard): step in when a turn is stuck.** When a turn (the
  agent's work on one prompt) keeps hitting the same failures, a judge model checks whether it is
  stuck. If the judge is at least *c* sure (*c* = 0.7), the agent gets a redirect (a short note,
  added to its context, to try another approach), at most *r* times per turn (*r* = 1). If the
  turn is still stuck after that, TokenEconomizer tells you how to stop it; it never stops a turn
  itself.

TokenEconomizer is a plugin for **Claude Code**, **Codex**, the **GitHub Copilot CLI** and **Pi**.
Each italic letter above is a setting, shown with its default ([Settings](#settings)), and each
mechanism can be turned on or off. The plugin announces each action in one line, and if any part
of it fails, your session carries on as if it were not installed. Its model calls (the fact
proposer, and the judge with the redirect it writes) run through your own agent, on the same plan
or login, and their cost is included in its reports. What it learns stays on your machine, in
`~/.tokeneconomizer`.

## Results

The paper ran five agent–model combinations on the same 277 SWE-Bench tasks from six repositories.
Each task ran as its own session, and each repository's tasks ran in order, starting with nothing
learned. Costs include every model call TokenEconomizer made. Within each repository and
combination, cost fell by 5–40%. The savings held on Web-Bench (16–21% lower) and on two
security-auditing benchmarks where every task is in a new repository (6–20% lower). The
[project page](https://tokeneconomizer.github.io) has interactive charts, per-repository numbers
and nine annotated runs, and [`experiments/`](experiments/README.md) reruns the SWE-Bench and
Web-Bench experiments.

## How it works

<p align="center"><img src="docs/assets/framework_overview.png" width="100%" alt="TokenEconomizer: the plugin keeps what it knows about a repository and applies micro-skills, tool pruning and failure-loop intervention to your unchanged coding agent; each finished session's log updates all three"></p>

Coding agents are built to finish tasks, not to keep them cheap. Every turn pays again for the
agent's instructions, every loaded tool, and the conversation so far. Developers use the same
agent on the same repository again and again, so much of that cost repeats: the same facts
rediscovered, the same unused tools sent, the same kinds of failing loops. TokenEconomizer leaves
the agent's model, weights and code alone and learns across a repository's sessions instead of
treating each one on its own, so whatever the plugin spends is paid back over the sessions that
follow. Four principles guide the mechanisms:

- **Work through what the agent already reads.** Facts enter the context at session start, and
  tools are removed through the agent's own settings, so nothing depends on the agent learning a
  new tool.
- **Count cost as it is billed.** Most tokens in a session are cached re-reads, so cost is
  computed with cache pricing, and it includes the plugin's own model calls.
- **Protect task success.** Facts form only from repeated evidence, tools are removed only after
  going unused, and the loop guard leaves alone a turn that is still making progress.
- **Keep every action reversible.** A fact that later sessions contradict is dropped, removed
  tools can be given back, and the loop guard only redirects and tells you: stopping a turn is
  your call.

## Quick start

You need one of the four agents, signed in any way it supports, and Python 3.10 or newer on
`PATH`. The plugin needs no API key of its own and never handles credentials, so any plan or
login works.

### Install

Run these commands in the folder where you want to keep the clone. Claude Code, Codex and the
Copilot CLI run the plugin from the clone, so leave it in place. Then start the agent in one of
your repositories.

**Claude Code**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
claude plugin marketplace add "$PWD/TokenEconomizer"
claude plugin install tokeneconomizer@tokeneconomizer
```

Start `claude`, trust the folder (**Yes, I trust this folder**), and run
`/tokeneconomizer:status`, allowing its script if asked. Once per machine, and again after Claude
Code updates, run `/tokeneconomizer:toolprune refresh-catalog`.

**Codex**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
codex plugin marketplace add "$PWD/TokenEconomizer"
codex plugin add tokeneconomizer@tokeneconomizer
```

Start `codex`, choose **Trust and continue** for the folder, then at **Hooks need review**
choose **Trust all and continue**. The hooks take effect from your next Codex session, so quit
(`/exit`), start `codex` again, and check with `$tokeneconomizer:status`.

**Copilot CLI**

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
copilot plugin marketplace add "$PWD/TokenEconomizer"
copilot plugin install tokeneconomizer@tokeneconomizer
```

Start `copilot`, choose **Yes, and remember this folder for future sessions**, and run
`/tokeneconomizer:status`, choosing **Yes** to run its script. Once per machine, and again after
Copilot updates, run `/tokeneconomizer:toolprune refresh-catalog`.

**Pi**

```sh
pi install git:github.com/microsoft/TokenEconomizer
```

Start `pi` and run `/te status`.

If the install worked, the status command prints a short status block. `refresh-catalog`
records every tool a session loads (without a model call), so that tool pruning can consider all
of them. Keep the marketplace path absolute, as above: the Copilot CLI reads a relative path such
as `./TokenEconomizer` as the name of a GitHub repository.

To update:

| Agent | Update |
|---|---|
| Claude Code, Copilot CLI | `git pull` in the clone |
| Codex | `git pull` in the clone, then the `codex plugin add` line again |
| Pi | `pi update --extensions` |

**[`plugin/README.md`](plugin/README.md)** walks through each agent in more detail, including
uninstalling, using Copilot with your own model key, and troubleshooting.

### Supported versions

| Agent | Supported | Older versions |
|---|---|---|
| Claude Code | 2.1.300 to 2.1.310 | not supported |
| Codex | 0.153.1 to 0.164.0 | not supported |
| Copilot CLI | 1.0.83 to 1.0.102 | not supported |
| Pi | 1.2.0 to 1.3.0 | 0.99.x: load the extension by path (below) |

The commands above were tested on the oldest and newest version in each range; newer versions
have not been tested yet.

- **Codex before 0.141.0** does not run plugin hooks at all.
- **Pi 0.99.x**: instead of `pi install`, clone the repository and start each session with
  `pi --extension /path/to/TokenEconomizer/plugin/pi/tokeneconomizer.ts`, using your clone's
  full path; `git pull` in the clone updates it.

### Everyday commands

Work as usual; the plugin needs nothing from you. To see or change what it does, use its
commands. They have the same names on every agent; only the way you type them differs:

| Agent | Type a command as |
|---|---|
| Claude Code, Copilot CLI | `/tokeneconomizer:<command>` |
| Codex | `$tokeneconomizer:<command>` |
| Pi | `/te <command>` |
| a terminal, in the repository | `~/.tokeneconomizer/bin/te <command>` |

| Command | What it does |
|---|---|
| `status` | what it learned and did here, and what your sessions cost |
| `microskills` | the facts it injects, and those still forming |
| `toolprune` | the tools it left out, and the evidence (not on Pi) |
| `toolprune restore` | give them back; they will not be removed again |
| `config` | every setting in effect |
| `config set microskills=on` | turn a mechanism on (or `=off`) in this repository; add `--global` for every repository |

Inside an agent, a command costs one model turn; from a terminal it is free. The other commands
(`repos`, `doctor`, `config unset`, ...) and each agent's permission prompts are listed under
[Commands](plugin/README.md#commands).

## Settings

Tool pruning and the loop guard are on by default; with these defaults, the plugin calls a model
only when a turn shows signs of looping: the judge, and the redirect it writes if it finds the
turn stuck. Micro-skills is off by default, because its proposer adds model calls and how much it
saves depends on the repository and the work you do there. Each mechanism has its own switch
(`microskills`, `toolprune`, `loopguard`) that takes `on` or `off`, and the italic letters above
are these settings:

| | Setting | Default | What it sets |
|---|---|---|---|
| *k* | `microskills_propose_every` | 3 | sessions between runs of micro-skills' fact proposer |
| *p* | `toolprune_evidence` | 10 | prompts a tool must go unused before it is left out |
| *m* | `toolprune_min_sessions` | 2 | sessions those prompts must span |
| *s* | `toolprune_sessions` | 5 | or this many sessions, of any length |
| *q* | `toolprune_quiet_minutes` | 60 | minutes your sessions here must be idle before a deny list is written |
| | `backfill_sessions` | 50 | earlier Claude Code sessions in this repository learned from at install (0: none) |
| *e* | `loopguard.envelope_min_samples` | 5 | turns here that ended well (no failing check after the last edit) before the loop guard acts |
| *c* | `loopguard.judge_confidence` | 0.7 | how sure the judge must be that a turn is stuck |
| *r* | `loopguard_nudges` | 1 | redirects per turn before the guard only tells you |
| *b* | `loopguard.backstop_mult` | 2.5 | how many times its usual length a churning turn may run before it is redirected without the judge |

Change them with `config set`, shown here in Claude Code's form (other agents type it as in
[Everyday commands](#everyday-commands)):

```text
/tokeneconomizer:config set microskills=on                 # turn micro-skills on
/tokeneconomizer:config set microskills_propose_every=5    # k = 5: propose facts less often
/tokeneconomizer:config set toolprune_evidence=20          # p = 20: wait for twenty unused prompts
/tokeneconomizer:config set loopguard_nudges=2             # r = 2: redirect twice before telling you
```

Tool pruning counts prompts so that long sessions weigh more, and requires *m* sessions so that a
single session never decides alone. It writes the deny list only once your sessions in the
repository have been idle for *q* minutes (or, at the latest, a day after the list formed): a
conversation resumed after its tools change must write its whole history to the prompt cache again
instead of reading it, and after an idle hour that cache has expired anyway.

Besides `on` and `off`, two modes are useful: `toolprune=propose` only proposes the deny list, and
nothing changes until you run `toolprune apply`; `microskills=envfacts` learns facts only from
repeated failures, with no model calls.

To change a single session, set an environment variable when you start the agent:
`TE_DISABLE=1 claude` turns the whole plugin off, and `TE_LOOPGUARD=off codex` turns off one
mechanism (likewise `TE_MICROSKILLS` and `TE_TOOLPRUNE`). Every setting and its default are
listed under [Settings](plugin/README.md#settings); each one's environment variable is `TE_` plus
its name in capitals.

## What it does on each agent

Micro-skills and the loop guard work the same way on every agent. Each agent learns on its own:
facts, tool evidence and the loop guard's sense of a usual turn are kept per agent and
repository, while settings are shared. Tool pruning acts through each agent's own settings, so
what it can leave out differs:

| Agent | Tool pruning leaves out | Written to |
|---|---|---|
| Claude Code | single tools | `.claude/settings.local.json` |
| Codex | capability packages | `.codex/config.toml` |
| Copilot CLI | whole MCP servers | `.github/copilot/settings.local.json` |
| Pi | nothing (Pi loads only four tools) | — |

Tool pruning adds only its own entries and never touches deny rules you wrote; a settings file it
creates is kept out of git (`.git/info/exclude`). On Claude Code, the session-start line that says
what it left out shows only in the detailed transcript (**Ctrl+O**), so check with
`/tokeneconomizer:toolprune`. `toolprune restore` removes the plugin's entries and nothing else,
and tools you give back are not removed again. If the plugin cannot edit a file safely (a
`.codex/config.toml` your repository commits, a settings file that is not valid JSON, a symlink
that leads out of the repository), the list stays a proposal and the session start says why;
`toolprune apply` tries again. **[`docs/PLUGIN.md`](docs/PLUGIN.md)** records how each agent's
hooks and settings were verified to behave, and **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**
how the package fits together.

### When a turn gets stuck

The loop guard first learns how long a turn in this repository usually runs: it waits until *e*
turns here have ended well, on their own and with no failing check after the last edit. After
that, a turn that shows signs of looping (the same failures repeating, or errors piling up) goes
to the judge. One case is redirected without the judge: a turn that runs past *b* times the usual
length while churning the same edits through the same failures. If a turn is still stuck after its
redirects, you see one line that ends with how to stop it:

> TokenEconomizer: this turn still looks stuck after a redirect (judge-stuck@0.82, 46 tool
> calls). Press Esc to interrupt it.

Where that line appears, and how to stop the turn, in the terminal UI and in a headless run
(`claude -p`, `codex exec`, `copilot -p`, `pi -p`):

| Agent | Interactive: where it shows | How to stop | Headless: where it shows | How to stop |
|---|---|---|---|---|
| Claude Code | in Claude's reply | Esc | in the final reply | Ctrl+C |
| Codex | in the conversation | Esc, then `/stop` | not shown | Ctrl+C |
| Copilot CLI | in the conversation | Esc twice | on stdout | Ctrl+C |
| Pi | as a notification | Esc | on stderr | Ctrl+C |

- **Claude Code** shows a plugin's own lines only in its detailed transcript (Ctrl+O), so the
  plugin asks Claude to repeat this one in its reply; the model may word it differently or leave
  it out.
- **Codex**: Esc interrupts the turn, but a command it started keeps running until `/stop`.
  `codex exec` prints no hook messages.
- **Pi**: Ctrl+C ends a headless run, but a command it started runs on until it finishes.

In a headless run nobody may be watching, so a flagged turn runs until it ends or reaches the
agent's own limits; afterwards, `te status` counts the redirects and "still stuck" notices.

## Repository

| | |
|---|---|
| [`tokeneconomizer/microskills`](tokeneconomizer/microskills/) | Micro-skill synthesis and injection: facts learned from finished sessions |
| [`tokeneconomizer/toolprune`](tokeneconomizer/toolprune/) | Tool pruning: unused tools and Codex capability packages |
| [`tokeneconomizer/loopguard`](tokeneconomizer/loopguard/) | Failure-loop intervention: the per-call guard and its per-repository envelope |
| [`tokeneconomizer/trace`](tokeneconomizer/trace/) | Each agent's session log, read into one shape |
| [`tokeneconomizer/plugin`](tokeneconomizer/plugin/) | The runtime the hooks and the `te` command run |
| [`plugin`](plugin/) | The installable plugin: manifests, hook files, launchers, slash commands, the Pi extension |
| [`experiments`](experiments/) | The harness, launchers and task sets behind the paper's results |
| [`docs`](docs/) | `PLUGIN.md` (design, verified agent behavior) and `ARCHITECTURE.md` (how the package fits together) |
| [`tests`](tests/) | Offline tests for the package and the plugin |

The package is standard-library only. Run the tests with `python -m pytest tests` (add
`experiments/tests` for the benchmark side), and build a self-contained plugin bundle with
`python3 scripts/build_plugin.py --out dist/marketplace`.

## Reproducing the experiments

The code behind the paper's results -- a harness that runs Claude Code, Codex and the Copilot CLI
on SWE-Bench and Web-Bench, the launchers, and the evaluated task sets -- is in
**[`experiments/`](experiments/README.md)**, whose README gives the setup and one command per
result. It is not part of the installed plugin; the two share only `tokeneconomizer/`.

## Citation

```bibtex
@misc{li2026tokeneconomizer,
  title         = {{TokenEconomizer}: Experience-Driven Black-Box Cost Control for Coding Agents},
  author        = {Li, Aaron J. and Xu, Weijia and Singh, Chandan and Chen, Zixiao and
                   Inala, Jeevana Priya and Siska, Charlotte and Tamersoy, Acar and Yu, Bin and
                   Stoica, Ion and Galley, Michel and Gao, Jianfeng},
  year          = {2026},
  eprint        = {2610.22732},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CL},
  url           = {https://arxiv.org/abs/2610.22732}
}
```

## License

Apache 2.0; see [`LICENSE`](LICENSE).
