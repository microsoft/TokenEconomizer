#!/usr/bin/env python3
"""Enumerate and cost a sweep BEFORE running it.

A sweep is a product: arms x repos x seeds, per (agent, model, domain) tree. At the sizes
the main experiments ask for that product is five figures of task-runs, and the only cheap
moment to notice is before the first container starts. So this module is the one place the
matrix is written down, and it answers three questions offline (no Docker, no datasets, no
API key):

  --format cells      one `arm<TAB>repo<TAB>seed` line per cell, for a launcher to loop over
  --format estimate   task-runs, wall-clock and dollars for the requested slice

Task counts come from the committed `workloads/<domain>.json` manifest -- the same file
the launcher passes as `--workload` and the driver filters the task stream to -- so the
plan cannot drift from what the run actually executes. That matters beyond tidiness:
`main_sweep.sh` decides a cell is finished by comparing its stored row count against this
number, so a plan that counted excluded instances would re-run completed cells forever.

Rate assumptions are declared, not hidden: `--sec-per-task` and `--usd-per-task` default to
what the stored Claude-direct SWE-bench tree actually measured, and both are printed with
the estimate so a reader can rescale it. They are means over a heavy-tailed distribution;
treat the output as an order of magnitude, not a quote.

Usage:
    python scripts/sweep_plan.py --domain swebench --arms baseline trimech \\
        --repos django/django --seeds 0 1 2 --format cells
"""
import argparse
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_EXPERIMENTS, os.path.dirname(_EXPERIMENTS)]  # te_harness; the core at the root
from te_bench import workloads                                   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_experiment import ARMS, SCAFFOLD_ARMS                          # noqa: E402


# ---- the main-experiment matrix ---------------------------------------------------------
# The arms of the main sweep, per domain. Web-Bench swaps in the two spiral arms tuned for
# its 20-21-task streams (`-web`: LLM nudge + frozen envelope), because its streams are too
# short for the within-stream envelope to warm up.
SWEEP_ARMS = {
    "swebench": ("baseline", "baseline-medium", "baseline-low",
                 "inject-only", "untool-only", "spiral-only", "trimech",
                 # external tool baselines + the two well-defined combinations
                 "rtk", "serena", "repomap", "trimech-rtk", "trimech-repomap"),
    # Web-Bench carries the GENERALIZATION claim, not the decomposition -- see
    # SWEEP_ARMS_WEBBENCH_OUT_OF_SCOPE for what that buys and what it gives up.
    "webbench": ("baseline", "baseline-medium", "baseline-low", "trimech-web",
                 # the one external tool baseline that needs no container and no Python
                 "rtk"),
}
# Runnable on Web-Bench, deliberately NOT in its sweep. Distinct from
# SWEEP_ARMS_WEBBENCH_UNAVAILABLE below, which is about what the harness CANNOT host: pass
# any of these to `--arms` and it runs.
#
# The decomposition (`inject-only` / `untool-only` / `spiral-only-web`) and the combination
# arm (`trimech-rtk-web`) are answered on SWE-bench, at 12 arms x 6 repos x 5 seeds. Three
# reasons not to pay for them twice:
#
#   1. Short streams distort exactly these arms. Injection needs a 5-task proposer cadence
#      and untooling needs 3 consecutive unused tasks; a Web-Bench stream is 20-21 tasks
#      against 39-60 on SWE-bench, so a single-mechanism arm there measures the mechanism
#      in WARM-UP, not in steady state. `trimech-web` at least has all three compounding.
#   2. `spiral-only-web` is the weakest buy in the matrix. Spiral fired 0 times across 498
#      opus-5/sonnet-5 rows and 1 nudge / 3 aborts across 224 opus-4.8 rows; Web-Bench
#      streams are a third the length AND run a frozen envelope (they are too short for the
#      within-stream one to warm up), so the arm would cost 100 cells to report zero.
#   3. 5 projects is ~5 project-level observations per arm, and they are wildly
#      heterogeneous (one project is a single index.html, another grows to ~140 files).
#      Separating inject-only from untool-only at that resolution was never going to reach
#      significance, so cutting them mostly removes numbers nobody could lean on.
#
# What this gives up, said plainly: without `untool-only` here, "is trimech-web just
# untooling?" cannot be answered ON Web-Bench -- only on SWE-bench. That is the accepted
# cost of framing Web-Bench as generalization rather than decomposition: there the three
# are measured as a combined stack.
SWEEP_ARMS_WEBBENCH_OUT_OF_SCOPE = ("inject-only", "untool-only", "spiral-only-web",
                                    "trimech-rtk-web")
# Which scaffold arms Web-Bench CANNOT host, and why -- said here rather than leaving the
# reader to notice the two lists differ.
#
# `rtk` runs there (since 2026-09-05): it is a static binary plus a PreToolUse hook that
# wraps `ls`/`find`/`grep` and compresses their output before it reaches context, so it
# needs neither a container nor a language, and the Web-Bench agent does issue those
# commands. te_harness.webbench.WEBBENCH_SCAFFOLDS is the runner-side twin of this set.
#
# The other two do not:
#   repomap -- its generator walks `*.py` (tokeneconomizer/plugin/repomap_gen.py) and a Web-Bench
#              project is TypeScript/JavaScript, so the map would come back empty. Needs a
#              language change, not a wiring change.
#   serena  -- an MCP navigation server for a codebase. Web-Bench hands the agent a file
#              set with no installed project, and the projects run 1-140 files, so there is
#              essentially nothing to navigate.
# Generic arms only: this planner enumerates the Claude / Copilot sweep. The Codex arms
# are launched by codex_ab.sh, and its Web-Bench list already leaves out the two repomap-bearing arms for the same reason as here.
SWEEP_ARMS_WEBBENCH_UNAVAILABLE = ("serena", "repomap", "trimech-repomap")
SWEEP_SEEDS = (0, 1, 2)   # the paper's three seeds

# Measured on the stored claude_swebench tree: 984 task-runs, mean $0.34/task on the honest
# basis (effective + overhead at the $5/1M-output tier), and ~112-133 s/task from the two
# most recent 40-task lanes' start-to-finish wall clock.
DEFAULT_SEC_PER_TASK = 120.0
DEFAULT_USD_PER_TASK = 0.34


def group_sizes(domain, manifest=None):
    """{group -> n_tasks} a NEW run will actually execute, per group.

    NOT `workloads.task_counts()`, which is the provenance record and deliberately does
    not subtract `SWEBENCH_EXCLUDED` -- tests/test_workloads.py pins those sizes, and
    the published trees really did grade every id in them.

    The number here has to match what the driver runs, because `main_sweep.sh` compares
    the row.json files a cell already holds against it to decide whether the cell is
    finished. Off by one and SKIP_DONE never fires for that group: every resume re-runs
    completed cells, paid for, silently. So this reads the same committed manifest the
    launcher passes as `--workload` and the driver filters to (`load_tasks` keeps exactly
    the ids the manifest names), and falls back to `runnable_instances` when a manifest is
    not on disk. tests/test_sweep_arms.py asserts the two agree. `manifest` names another one.
    """
    manifest = manifest or os.path.join(_EXPERIMENTS, "workloads", f"{domain}.json")
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8") as fh:
            groups = json.load(fh).get("groups") or {}
        if groups:
            return {g: len(ids) for g, ids in groups.items()}
    return {g: len(ids) for g, ids in workloads.runnable_instances(domain).items()}


def cells(domain, arms, repos, seeds):
    """The (arm, repo, seed) triples of one lane, in run order: seed-major, then repo.

    Seed-major matches how the stored runs were launched -- a seed is one complete pass
    over the repo set, so an interrupted sweep leaves whole seeds finished rather than
    every seed half-done, and the first seed alone is already a publishable slice.
    """
    out = []
    for seed in seeds:
        for repo in repos:
            for arm in arms:
                out.append((arm, repo, seed))
    return out


def estimate(domain, arms, repos, seeds, sec_per_task, usd_per_task, limit=0, manifest=None):
    """Task-runs, wall-clock and dollars for the requested slice.

    `limit` mirrors the launcher's LIMIT (cap tasks per cell, 0 = all). It has to be
    honoured here or the estimate describes a run nobody asked for: a LIMIT=1 pilot would
    be costed at the full stream for a cell that runs ONE task, which is the wrong
    direction for a number whose whole job is to be checked before spending money.
    """
    sizes = group_sizes(domain, manifest)
    unknown = [r for r in repos if r not in sizes]
    per_repo = {r: sizes.get(r, 0) for r in repos}
    if limit and limit > 0:
        per_repo = {r: min(n, limit) for r, n in per_repo.items()}
    tasks = sum(per_repo.values()) * len(arms) * len(seeds)
    return {
        "domain": domain, "n_arms": len(arms), "n_repos": len(repos), "n_seeds": len(seeds),
        "n_cells": len(arms) * len(repos) * len(seeds), "n_task_runs": tasks,
        "hours_serial": tasks * sec_per_task / 3600.0,
        "usd": tasks * usd_per_task, "unknown_repos": unknown,
        "limit": limit or 0,
    }


def _fmt_estimate(e, show_cost=False):
    """Counts by default; the derived cost/wall-clock only when asked for.

    `cells` and `task-runs` are exact arithmetic. `hours_serial` and `usd` are one measured
    per-task mean multiplied by that count, and the per-task distribution is heavy-tailed
    and dominated by the repo's eval script rather than by the arm -- so the aggregate is a
    number with no error bar that reads like a quote. It stays available behind
    `--show-cost` for a per-arm sanity check; it is not printed by default -- budget from
    per-arm RUNTIME instead of any sweep-wide total.
    """
    cap = f"  limit={e['limit']}/cell" if e.get("limit") else ""
    line = (f"{e['domain']:9s} arms={e['n_arms']:2d} repos={e['n_repos']:2d} "
            f"seeds={e['n_seeds']:2d}  cells={e['n_cells']:5d}  "
            f"task-runs={e['n_task_runs']:7,d}")
    if show_cost:
        line += f"  serial={e['hours_serial']:8,.0f}h  ~${e['usd']:9,.0f}"
    return line + cap


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", default="swebench", choices=sorted(SWEEP_ARMS))
    ap.add_argument("--arms", nargs="*", default=None,
                    help="default: the domain's SWEEP_ARMS")
    ap.add_argument("--repos", nargs="*", default=None,
                    help="default: every group in the domain's workload record")
    ap.add_argument("--seeds", nargs="*", type=int, default=None,
                    help="default: %s" % " ".join(map(str, SWEEP_SEEDS)))
    ap.add_argument("--limit", type=int, default=0,
                    help="cap tasks per cell (0 = all), mirroring the launcher's LIMIT so "
                         "a pilot is costed as a pilot")
    ap.add_argument("--show-cost", action="store_true",
                    help="also print the DERIVED wall-clock and $ estimate (off by "
                         "default: it is one repo-dominated per-task mean times the "
                         "task count, and reads like a quote it cannot support)")
    ap.add_argument("--sec-per-task", type=float, default=DEFAULT_SEC_PER_TASK)
    ap.add_argument("--usd-per-task", type=float, default=DEFAULT_USD_PER_TASK)
    ap.add_argument("--workload", default=None,
                    help="workload manifest to count tasks from (default: workloads/<domain>.json)")
    ap.add_argument("--format", default="estimate",
                    choices=["cells", "estimate", "arms", "counts"])
    args = ap.parse_args()

    arms = args.arms or list(SWEEP_ARMS[args.domain])
    repos = args.repos or sorted(group_sizes(args.domain, args.workload))
    seeds = args.seeds if args.seeds is not None else list(SWEEP_SEEDS)

    bad = [a for a in arms if a not in ARMS]
    if bad:
        raise SystemExit(f"unknown arm(s): {' '.join(bad)}")

    if args.format == "arms":
        for a in arms:
            print(a)
        return
    if args.format == "counts":
        for group, n in sorted(group_sizes(args.domain, args.workload).items()):
            print(f"{group}\t{n}")
        return
    if args.format == "cells":
        unavailable = [a for a in arms
                       if args.domain == "webbench" and a in SWEEP_ARMS_WEBBENCH_UNAVAILABLE]
        if unavailable:
            print(f"# SKIPPING scaffold arms not wired for webbench: "
                  f"{' '.join(unavailable)}", file=sys.stderr)
        for arm, repo, seed in cells(args.domain,
                                     [a for a in arms if a not in unavailable],
                                     repos, seeds):
            print(f"{arm}\t{repo}\t{seed}")
        return

    if args.format == "estimate":
        usable = [a for a in arms
                  if not (args.domain == "webbench"
                          and a in SWEEP_ARMS_WEBBENCH_UNAVAILABLE)]
        e = estimate(args.domain, usable, repos, seeds,
                     args.sec_per_task, args.usd_per_task, limit=args.limit,
                     manifest=args.workload)
        if e["unknown_repos"]:
            print(f"!! not in the {args.domain} workload record: "
                  f"{' '.join(e['unknown_repos'])} (counted as 0 tasks)", file=sys.stderr)
        print(_fmt_estimate(e, args.show_cost))
        if args.show_cost:
            print(f"  at {args.sec_per_task:.0f}s and ${args.usd_per_task:.2f} per task-run "
                  f"(measured means, heavy-tailed -- an order of magnitude, not a quote)")
        return


if __name__ == "__main__":
    main()
