"""Canonical evaluated workloads — exactly which tasks the published results ran.

A results tree records which tasks ran only implicitly, in its run directories, so a fresh
user could not reproduce the workload without one. This module writes the task identities
down: it is the authority on what to provision (dataset rows and Docker images) and what to
run.

Stdlib-only and data-only, consistent with the rest of the core package: importing it
pulls in no dependencies and touches no filesystem, so it works on a bare clone with no
results tree, no datasets, and no Docker.

    from te_bench import workloads

    workloads.SWEBENCH_INSTANCES['pydata/xarray']      # the 39 xarray instances
    workloads.task_counts('swebench')                  # {'django/django': 40, ...}
    workloads.swebench_image_tag('django__django-11532')

**Provenance.** Extracted from the evaluated result trees (one row per task-run in
``<key>/index/all_rows.csv``).
"""

__all__ = [
    "SWEBENCH_INSTANCES", "SWEBENCH_SEEDS", "SWEBENCH_FULL_SPLIT_ONLY",
    "SWEBENCH_EXCLUDED", "SWEBENCH_ADDED", "runnable_instances",
    "SWEBENCH_COPILOT_EXTRA", "WEBBENCH_TASKS", "WEBBENCH_SEEDS",
    "WEBBENCH_TASKS_PUBLISHED_2026_08",
    "CREDDATA_INSTANCES", "CREDDATA_SEEDS", "REPOSVUL_INSTANCES", "REPOSVUL_SEEDS",
    "CODEX_SWEBENCH_RUNS", "DOMAINS", "instances", "task_counts", "total_tasks",
    "swebench_image_tag", "filter_tasks",
]

# ---------------------------------------------------------------------------
# SWE-bench  (Claude Code + Copilot CLI, trimech A/B; 6 repos x 3 seeds)
# ---------------------------------------------------------------------------
# 229 instances per seed. NOTE: this is NOT a subset of SWE-bench_Verified — 30 of the
# 229 exist only in the FULL SWE-bench test split (all of them xarray). Provisioning only
# Verified is therefore insufficient; see SWEBENCH_FULL_SPLIT_ONLY below.
SWEBENCH_INSTANCES: dict[str, tuple[str, ...]] = {
    "django/django": (
        "django__django-7530", "django__django-10097", "django__django-10554",
        "django__django-10880", "django__django-10914", "django__django-10973",
        "django__django-10999", "django__django-11066", "django__django-11087",
        "django__django-11095", "django__django-11099", "django__django-11119",
        "django__django-11133", "django__django-11138", "django__django-11149",
        "django__django-11163", "django__django-11179", "django__django-11206",
        "django__django-11211", "django__django-11239", "django__django-11265",
        "django__django-11276", "django__django-11292", "django__django-11299",
        "django__django-11333", "django__django-11400", "django__django-11433",
        "django__django-11451", "django__django-11477", "django__django-11490",
        "django__django-11532", "django__django-11551", "django__django-11555",
        "django__django-11603", "django__django-11728", "django__django-11734",
        "django__django-11740", "django__django-11749", "django__django-11790",
        "django__django-11820",
    ),
    "matplotlib/matplotlib": (
        "matplotlib__matplotlib-13989", "matplotlib__matplotlib-14623",
        "matplotlib__matplotlib-20488", "matplotlib__matplotlib-20676",
        "matplotlib__matplotlib-20826", "matplotlib__matplotlib-20859",
        "matplotlib__matplotlib-21568", "matplotlib__matplotlib-22719",
        "matplotlib__matplotlib-22865", "matplotlib__matplotlib-22871",
        "matplotlib__matplotlib-23299", "matplotlib__matplotlib-23314",
        "matplotlib__matplotlib-23412", "matplotlib__matplotlib-23476",
        "matplotlib__matplotlib-24026", "matplotlib__matplotlib-24149",
        "matplotlib__matplotlib-24177", "matplotlib__matplotlib-24570",
        "matplotlib__matplotlib-24627", "matplotlib__matplotlib-24637",
        "matplotlib__matplotlib-24870", "matplotlib__matplotlib-24970",
        "matplotlib__matplotlib-25122", "matplotlib__matplotlib-25287",
        "matplotlib__matplotlib-25311", "matplotlib__matplotlib-25332",
        "matplotlib__matplotlib-25479", "matplotlib__matplotlib-25775",
        "matplotlib__matplotlib-25960", "matplotlib__matplotlib-26113",
        "matplotlib__matplotlib-26208", "matplotlib__matplotlib-26291",
        "matplotlib__matplotlib-26342", "matplotlib__matplotlib-26466",
    ),
    "scikit-learn/scikit-learn": (
        "scikit-learn__scikit-learn-9288", "scikit-learn__scikit-learn-10297",
        "scikit-learn__scikit-learn-10844", "scikit-learn__scikit-learn-10908",
        "scikit-learn__scikit-learn-11310", "scikit-learn__scikit-learn-11578",
        "scikit-learn__scikit-learn-12585", "scikit-learn__scikit-learn-12682",
        "scikit-learn__scikit-learn-12973", "scikit-learn__scikit-learn-13124",
        "scikit-learn__scikit-learn-13135", "scikit-learn__scikit-learn-13142",
        "scikit-learn__scikit-learn-13328", "scikit-learn__scikit-learn-13439",
        "scikit-learn__scikit-learn-13496", "scikit-learn__scikit-learn-13779",
        "scikit-learn__scikit-learn-14053", "scikit-learn__scikit-learn-14087",
        "scikit-learn__scikit-learn-14141", "scikit-learn__scikit-learn-14496",
        "scikit-learn__scikit-learn-14629", "scikit-learn__scikit-learn-14710",
        "scikit-learn__scikit-learn-14894", "scikit-learn__scikit-learn-14983",
        "scikit-learn__scikit-learn-15100", "scikit-learn__scikit-learn-25102",
        "scikit-learn__scikit-learn-25232", "scikit-learn__scikit-learn-25747",
        "scikit-learn__scikit-learn-25931", "scikit-learn__scikit-learn-25973",
        "scikit-learn__scikit-learn-26194", "scikit-learn__scikit-learn-26323",
    ),
    "sphinx-doc/sphinx": (
        "sphinx-doc__sphinx-7440", "sphinx-doc__sphinx-7454", "sphinx-doc__sphinx-7462",
        "sphinx-doc__sphinx-7590", "sphinx-doc__sphinx-7748", "sphinx-doc__sphinx-7757",
        "sphinx-doc__sphinx-7889", "sphinx-doc__sphinx-7910", "sphinx-doc__sphinx-7985",
        "sphinx-doc__sphinx-8035", "sphinx-doc__sphinx-8056", "sphinx-doc__sphinx-8120",
        "sphinx-doc__sphinx-8265", "sphinx-doc__sphinx-8269", "sphinx-doc__sphinx-8459",
        "sphinx-doc__sphinx-8475", "sphinx-doc__sphinx-8548", "sphinx-doc__sphinx-8551",
        "sphinx-doc__sphinx-8593", "sphinx-doc__sphinx-8595", "sphinx-doc__sphinx-8621",
        "sphinx-doc__sphinx-8638", "sphinx-doc__sphinx-8721", "sphinx-doc__sphinx-9229",
        "sphinx-doc__sphinx-9230", "sphinx-doc__sphinx-9258", "sphinx-doc__sphinx-9281",
        "sphinx-doc__sphinx-9320", "sphinx-doc__sphinx-9367", "sphinx-doc__sphinx-9461",
        "sphinx-doc__sphinx-9591", "sphinx-doc__sphinx-9602", "sphinx-doc__sphinx-9658",
        "sphinx-doc__sphinx-9673", "sphinx-doc__sphinx-9698", "sphinx-doc__sphinx-9711",
        "sphinx-doc__sphinx-10323", "sphinx-doc__sphinx-10435", "sphinx-doc__sphinx-10449",
        "sphinx-doc__sphinx-10466", "sphinx-doc__sphinx-10614", "sphinx-doc__sphinx-10673",
        "sphinx-doc__sphinx-11445", "sphinx-doc__sphinx-11510",
    ),
    "sympy/sympy": (
        "sympy__sympy-11618", "sympy__sympy-12096", "sympy__sympy-12419", "sympy__sympy-12481",
        "sympy__sympy-12489", "sympy__sympy-13031", "sympy__sympy-13091", "sympy__sympy-13372",
        "sympy__sympy-13480", "sympy__sympy-13551", "sympy__sympy-13615", "sympy__sympy-13647",
        "sympy__sympy-13757", "sympy__sympy-13798", "sympy__sympy-13852", "sympy__sympy-13877",
        "sympy__sympy-13878", "sympy__sympy-13974", "sympy__sympy-14248", "sympy__sympy-14531",
        "sympy__sympy-14711", "sympy__sympy-14976", "sympy__sympy-15017", "sympy__sympy-15345",
        "sympy__sympy-15349", "sympy__sympy-15599", "sympy__sympy-15809", "sympy__sympy-15875",
        "sympy__sympy-15976", "sympy__sympy-16450", "sympy__sympy-16597", "sympy__sympy-16766",
        "sympy__sympy-16792", "sympy__sympy-16886", "sympy__sympy-17139", "sympy__sympy-17318",
        "sympy__sympy-17630", "sympy__sympy-17655", "sympy__sympy-18189", "sympy__sympy-18763",
    ),
    "pydata/xarray": (
        "pydata__xarray-4879", "pydata__xarray-6386", "pydata__xarray-6394",
        "pydata__xarray-6400", "pydata__xarray-6461", "pydata__xarray-6548",
        "pydata__xarray-6598", "pydata__xarray-6599", "pydata__xarray-6601",
        "pydata__xarray-6721", "pydata__xarray-6744", "pydata__xarray-6798",
        "pydata__xarray-6804", "pydata__xarray-6823", "pydata__xarray-6857",
        "pydata__xarray-6882", "pydata__xarray-6889", "pydata__xarray-6938",
        "pydata__xarray-6971", "pydata__xarray-6992", "pydata__xarray-6999",
        "pydata__xarray-7003", "pydata__xarray-7019", "pydata__xarray-7052",
        "pydata__xarray-7089", "pydata__xarray-7101", "pydata__xarray-7105",
        "pydata__xarray-7112", "pydata__xarray-7120", "pydata__xarray-7147",
        "pydata__xarray-7150", "pydata__xarray-7203", "pydata__xarray-7229",
        "pydata__xarray-7233", "pydata__xarray-7347", "pydata__xarray-7391",
        "pydata__xarray-7393", "pydata__xarray-7400", "pydata__xarray-7444",
    ),
}

# The three seeds each replay the SAME instance list; seeds vary only agent
# nondeterminism, never task selection (see experiments/scripts/run_experiment.py --seed).
SWEBENCH_SEEDS: tuple[int, ...] = (0, 1, 2)

# Instances NOT present in princeton-nlp/SWE-bench_Verified — fetch these from the full
# princeton-nlp/SWE-bench test split (they are the recent xarray tail).
SWEBENCH_FULL_SPLIT_ONLY: frozenset[str] = frozenset((
    "pydata__xarray-4879", "pydata__xarray-6386", "pydata__xarray-6394", "pydata__xarray-6400",
    "pydata__xarray-6548", "pydata__xarray-6598", "pydata__xarray-6601", "pydata__xarray-6798",
    "pydata__xarray-6804", "pydata__xarray-6823", "pydata__xarray-6857", "pydata__xarray-6882",
    "pydata__xarray-6889", "pydata__xarray-6971", "pydata__xarray-6999", "pydata__xarray-7003",
    "pydata__xarray-7019", "pydata__xarray-7052", "pydata__xarray-7089", "pydata__xarray-7101",
    "pydata__xarray-7105", "pydata__xarray-7112", "pydata__xarray-7120", "pydata__xarray-7147",
    "pydata__xarray-7150", "pydata__xarray-7203", "pydata__xarray-7347", "pydata__xarray-7391",
    "pydata__xarray-7400", "pydata__xarray-7444",
))

# The Copilot sweep additionally ran this instance; the Claude sweep and the published
# task_counts (xarray = 39) do not include it. Recorded for completeness only.
SWEBENCH_COPILOT_EXTRA: frozenset[str] = frozenset((
    "pydata__xarray-7179",
))

# Codex-native arms ran a subset of the same instances, one seed per repo.
CODEX_SWEBENCH_RUNS: tuple[tuple[str, str], ...] = (
    ("django/django", "s0"),
    ("sympy/sympy", "s0"),
    ("scikit-learn/scikit-learn", "s1"),
    ("sphinx-doc/sphinx", "s0"),
    ("pydata/xarray", "s1"),
)

# ---------------------------------------------------------------------------
# Web-Bench  (trimech-web; 5 projects, each a SEQUENTIALLY DEPENDENT stream)
# ---------------------------------------------------------------------------
# Order is load-bearing: task N assumes task N-1's edits, so run each tuple front to back.
# Every project runs `task-1` .. `task-20`; `calculator` and `table` additionally open with an
# `init` scaffold task (hence 21).
#
# The five are chosen on TWO MEASURED criteria:
#
#   grounding  -- the distiller admits a fact only if it can cite >= MIN_CITE=2 confirming
#     task-ids from the agent's own trajectory (microskills/distiller.py `_admit`), and the
#     digests it reads are COMMAND-shaped. So a project where the agent barely runs anything
#     starves it: every candidate dies as `grounding-fail`. `threejs` was retired for
#     exactly this.
#
#   wall-clock -- Web-Bench's evaluator aborts an agent request at undici's 300s default
#     (see webbench_task_timeout). A project whose tasks want more than ~150s of REQUEST time
#     cannot finish its stream. Measured: the suite norm is ~76s/task; `svg-chart` runs
#     174-200s/task, too slow to finish a 20-task stream, and is out for that.
#
# `calculator` is KEPT deliberately as the short static control: dropping it would collapse
# the 4x spread in stream length that made the starvation effect visible.
#
# `zustand` was ADOPTED 2026-09-08 after a baseline gate cell: 107.4s request/task (max
# 212.5s, nothing capped) is comfortable under the 240s cap. It does NOT ground (1.8 shell
# calls/task) and is not expected to -- grounding was dropped as a selection criterion.
#
# `sass` was rejected the same day on WALL-CLOCK RISK: 185.8s/task with 8 of 15 attempts
# hitting the cap, so its pass@1 was being set by truncation rather than by the model.
# `table` replaces it, chosen on WORST-CASE timing -- 7.0 estimated turns/task x 11.8 s/turn
# (the slowest rate ever measured here, on svg-chart) = 82s, roughly 3x headroom under the
# cap even if every estimate is wrong in the bad direction. Still ungated at time of writing.
#
# Rule of thumb the measurements support: app-logic projects run 4.7-5.9 s/turn (expressjs,
# zustand, react); visual/CSS ones run 7.3-11.8 (calculator, threejs, svg-chart, sass). Pick
# on turns x the WORST s/turn, not the expected one.
#
# Task ids are only unique WITHIN a project. Order is load-bearing: task N assumes N-1.
WEBBENCH_TASKS: dict[str, tuple[str, ...]] = {
    "calculator": (
        "init", "task-1", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7", "task-8",
        "task-9", "task-10", "task-11", "task-12", "task-13", "task-14", "task-15", "task-16",
        "task-17", "task-18", "task-19", "task-20",
    ),
    "expressjs": (
        "task-1", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7", "task-8",
        "task-9", "task-10", "task-11", "task-12", "task-13", "task-14", "task-15", "task-16",
        "task-17", "task-18", "task-19", "task-20",
    ),
    "react": (
        "task-1", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7", "task-8",
        "task-9", "task-10", "task-11", "task-12", "task-13", "task-14", "task-15", "task-16",
        "task-17", "task-18", "task-19", "task-20",
    ),
    "zustand": (
        "task-1", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7", "task-8",
        "task-9", "task-10", "task-11", "task-12", "task-13", "task-14", "task-15", "task-16",
        "task-17", "task-18", "task-19", "task-20",
    ),
    "table": (
        "init", "task-1", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7", "task-8",
        "task-9", "task-10", "task-11", "task-12", "task-13", "task-14", "task-15", "task-16",
        "task-17", "task-18", "task-19", "task-20",
    ),
}

# The project set the 2026-08 exports were actually run on. FROZEN: it describes a run that
# already happened and cannot be edited after the fact. Kept here so WEBBENCH_TASKS above is
# free to move on while the record stays pinned to what was measured.
WEBBENCH_TASKS_PUBLISHED_2026_08: dict[str, int] = {
    "flex": 21, "expressjs": 20, "react": 20, "prisma": 20, "calculator": 21,
}

# Web-Bench seeds are LETTERED in the published trees (not 0/1/2 like the other domains).
WEBBENCH_SEEDS: tuple[str, ...] = ("A", "B", "C")

# ---------------------------------------------------------------------------
# CredData  (security / secret detection, graded to line-level F1)
# ---------------------------------------------------------------------------
# 100 of CredData's 333 repo ids. Each id is a directory `data/<id>/` with labels in
# `meta/<id>.csv`; every repo gets the same read-only audit prompt.
CREDDATA_INSTANCES: tuple[str, ...] = (
    "00408ef6", "0401c075", "08295bf0", "0a0d22aa", "0ce78112", "110f8d35", "16fc0238",
    "262e8107", "28728ab4", "29d5ecae", "30201159", "31423103", "353ae7fa", "35c273e2",
    "371ea6d8", "39def7b4", "3d82970b", "443086e9", "46c52073", "489fc16c", "4db30bbe",
    "4f40bf46", "50b38ef0", "55031e17", "55aa4858", "57067d8d", "5cb346c3", "5cecf769",
    "5d8870b3", "60f9915d", "63800a10", "638eb0c6", "653a7746", "660708f3", "661abd43",
    "69f42949", "6a132a15", "6d59a88a", "6e2ed0e4", "7529016f", "75e7c64d", "7c7558cf",
    "7e01f352", "7f8b28a7", "81cd05d0", "826a020b", "848ad017", "86875835", "8bc560cd",
    "8c3c80e8", "8c9fcde7", "8d217d6c", "8f499d55", "9031b2b4", "91cfdb2b", "93ff401b",
    "98a9ba4e", "9c7e3662", "9ccb72ee", "9d90ba70", "9ef45116", "a0cd6261", "a15774b8",
    "a5c3685e", "a8bdca79", "ac9be8d9", "adf34139", "afdd01d8", "b6dbde6a", "b81d3227",
    "b8d8b8c3", "bbb4193f", "bcb18a48", "bd7bcb9d", "c2127ffb", "c9b945fa", "cc268724",
    "cd82f1a2", "cfdcc4f3", "d2d6eedc", "d518da13", "d93c01fb", "db218f09", "db832824",
    "df70e863", "e08ff555", "e097ae9a", "e3c63910", "e69fe1b9", "eb8448ad", "eb9ba732",
    "f129be24", "f43ddbf5", "f710ac3c", "f7ae636a", "f80c571b", "f85f83c9", "fc8343f4",
    "ff43ce4d", "ff527e6f",
)
CREDDATA_SEEDS: tuple[int, ...] = (0, 1, 2)

# ---------------------------------------------------------------------------
# ReposVul  (vulnerability detection, graded to line-level F1)
# ---------------------------------------------------------------------------
# 100 instances, ALL from the Python split (`py-*`) — the C/Java/PHP splits were not run.
REPOSVUL_INSTANCES: tuple[str, ...] = (
    "py-10081", "py-10115", "py-1045", "py-10585", "py-10746", "py-10878", "py-10883",
    "py-11295", "py-11519", "py-11621", "py-11682", "py-11868", "py-11990", "py-12077",
    "py-12275", "py-12277", "py-12279", "py-12292", "py-12343", "py-12375", "py-12440",
    "py-12497", "py-171", "py-1968", "py-1974", "py-211", "py-2210", "py-2496", "py-2656",
    "py-2660", "py-2754", "py-2771", "py-2847", "py-3163", "py-3380", "py-3482", "py-3596",
    "py-369", "py-3802", "py-4061", "py-4374", "py-4450", "py-4597", "py-4615", "py-4816",
    "py-49", "py-5016", "py-5203", "py-5259", "py-5557", "py-5566", "py-5567", "py-5569",
    "py-569", "py-5751", "py-5838", "py-5897", "py-5934", "py-5969", "py-5987", "py-6002",
    "py-6200", "py-623", "py-6237", "py-6510", "py-6540", "py-6565", "py-6634", "py-6646",
    "py-6678", "py-6764", "py-6773", "py-6817", "py-7185", "py-7458", "py-7563", "py-7740",
    "py-8152", "py-8320", "py-8450", "py-8464", "py-8504", "py-8523", "py-8851", "py-8958",
    "py-9020", "py-9030", "py-9070", "py-9081", "py-9217", "py-9275", "py-940", "py-941",
    "py-9652", "py-9659", "py-9824", "py-9859", "py-9876", "py-9902", "py-9968",
)
REPOSVUL_SEEDS: tuple[int, ...] = (0, 1, 2)

DOMAINS: tuple[str, ...] = ("swebench", "webbench", "creddata", "reposvul")

_BY_DOMAIN = {
    "swebench": SWEBENCH_INSTANCES,
    "webbench": WEBBENCH_TASKS,
    "creddata": {"creddata": CREDDATA_INSTANCES},
    "reposvul": {"reposvul": REPOSVUL_INSTANCES},
}


def instances(domain, group=None):
    """Task ids for `domain`, or just those of one `group` (repo / project).

    `group` is the SWE-bench repo slug (``pydata/xarray``) or Web-Bench project name.
    Raises KeyError on an unknown domain or group — never silently returns everything."""
    try:
        groups = _BY_DOMAIN[domain]
    except KeyError:
        raise KeyError(f"unknown domain {domain!r}; expected one of {DOMAINS}") from None
    if group is None:
        return tuple(i for g in groups.values() for i in g)
    if group not in groups:
        raise KeyError(f"{domain}: unknown group {group!r}; expected one of "
                       f"{tuple(groups)}")
    return tuple(groups[group])


# ---- forward-looking run exclusions -------------------------------------------------
# SWEBENCH_INSTANCES above is the PROVENANCE record: exactly what the published results
# ran, and it must not shrink (experiments/tests/test_workloads.py pins those sizes -- the
# published runs graded every id in it).
# This table is the separate, forward-looking question: which of those ids should a NEW run
# skip. `runnable_instances()` is what the exporter and launchers consume; task_counts()
# and total_tasks() deliberately do NOT subtract it, so the historical record stays intact.
SWEBENCH_EXCLUDED: dict[str, str] = {
    "scikit-learn__scikit-learn-14710":
        "eval.sh does not terminate under te_harness within TE_EVAL_TIMEOUT (1800 s, nor "
        "3600 s), so every run of it costs one full eval_timeout (te_harness swebench.py "
        "_grade catches the timeout); excluded to stop paying that. "
        "NOT ungradeable in principle -- it graded in the published runs, so results "
        "that include it and results that exclude it are not directly comparable on "
        "scikit-learn.",
}


# ---- forward-looking stream extensions ----------------------------------------------
# De-saturation. On the published workload scikit-learn and sympy resolve at or near the
# ceiling for every arm, so they no longer separate the arms; a stream that returns the same
# number for every arm cannot support any claim made on it.
#
# These ids EXTEND those two streams to 60 RUNNABLE tasks each (sklearn 31 + 29,
# sympy 40 + 20). They are additions for FUTURE runs only: `SWEBENCH_INSTANCES` above
# stays exactly as the published results ran it, and `task_counts()` / `total_tasks()`
# keep reporting 229, so the published record remains true.
# `runnable_instances()` is what the exporter and launchers consume, and it folds these in.
#
# Selection rule (reproducible from princeton-nlp/SWE-bench, full `test` split):
#   * not already in SWEBENCH_INSTANCES;
#   * gold patch 61-200 changed lines;
#   * `version` already present in that repo's existing stream, so the per-repo operating
#     manual `injection` learns stays valid across the extension;
#   * sorted by (gold lines, id), then sampled at even spacing so the band is covered
#     rather than clustered at one end.
#
# Why 61-200 and not "anything big": xarray is the precedent -- it is the one stream that
# was already extended with full-split tail ids, and its baseline rows (opus-4.8) show
# what each band actually scores:
#       full-only <=60 lines  -> 75% resolved (n=48)   no harder than Verified
#       full-only 61-120      -> 44% (n=9)
#       full-only 121-200     -> 17% (n=12)
#       full-only >200        ->  5% (n=21)   floor: costs the most, discriminates least
# So being outside Verified buys nothing on its own; gold-patch SIZE is the difficulty
# dial, and >200 overshoots into a floor. 61-200 blends to roughly 30% expected resolve,
# which brings both streams to ~65% pooled.
#
# GOLD-PATCH SCREENED, 2026-09-06: 49/49. Unlike SWE-bench_Verified these ids were never
# human-validated for solvability, so each one was screened before being committed here --
# start the official image, apply the GOLD patch into /testbed, run the official eval
# script, score it with the official get_eval_report. All 49 came back resolved=True with
# the gold patch applying cleanly, every FAIL_TO_PASS passing and zero PASS_TO_PASS
# regressions, so none is underspecified or ungradeable under this harness.
#
# Eval wall-clock ran 14-187 s (median 32 s): every added instance terminates roughly an
# order of magnitude inside the default TE_EVAL_TIMEOUT=1800, unlike scikit-learn-14710,
# whose eval does not terminate (see SWEBENCH_EXCLUDED).
#
# Screen script and per-instance records: $DATA/swebench/gold_screen.py and
# $DATA/swebench/gold_screen_added.jsonl. Re-run it if you extend this table again; a new
# id that fails belongs nowhere near a paid sweep. Note two ids carry an empty
# PASS_TO_PASS and so have no regression guard: scikit-learn-11585 and -11596.
SWEBENCH_ADDED: dict[str, tuple[str, ...]] = {
    "scikit-learn/scikit-learn": (
        "scikit-learn__scikit-learn-3840", "scikit-learn__scikit-learn-7760",
        "scikit-learn__scikit-learn-9274", "scikit-learn__scikit-learn-10459",
        "scikit-learn__scikit-learn-10577", "scikit-learn__scikit-learn-10803",
        "scikit-learn__scikit-learn-11496", "scikit-learn__scikit-learn-11585",
        "scikit-learn__scikit-learn-11596", "scikit-learn__scikit-learn-12421",
        "scikit-learn__scikit-learn-12758", "scikit-learn__scikit-learn-12827",
        "scikit-learn__scikit-learn-12989", "scikit-learn__scikit-learn-13010",
        "scikit-learn__scikit-learn-13046", "scikit-learn__scikit-learn-13363",
        "scikit-learn__scikit-learn-13467", "scikit-learn__scikit-learn-13618",
        "scikit-learn__scikit-learn-13628", "scikit-learn__scikit-learn-13864",
        "scikit-learn__scikit-learn-13910", "scikit-learn__scikit-learn-14237",
        "scikit-learn__scikit-learn-14464", "scikit-learn__scikit-learn-14544",
        "scikit-learn__scikit-learn-14704", "scikit-learn__scikit-learn-14806",
        "scikit-learn__scikit-learn-14999", "scikit-learn__scikit-learn-23099",
        "scikit-learn__scikit-learn-25744",
    ),
    "sympy/sympy": (
        "sympy__sympy-11862", "sympy__sympy-11989", "sympy__sympy-13259",
        "sympy__sympy-13265", "sympy__sympy-13301", "sympy__sympy-13988",
        "sympy__sympy-14308", "sympy__sympy-15225", "sympy__sympy-15304",
        "sympy__sympy-15555", "sympy__sympy-15586", "sympy__sympy-16781",
        "sympy__sympy-17194", "sympy__sympy-17313", "sympy__sympy-17340",
        "sympy__sympy-17394", "sympy__sympy-18199", "sympy__sympy-18256",
        "sympy__sympy-18477", "sympy__sympy-19201",
    ),
}


def runnable_instances(domain, group=None):
    """Canonical instances PLUS SWEBENCH_ADDED MINUS SWEBENCH_EXCLUDED — what a NEW
    run should execute.

    Differs from `instances()`/`task_counts()`, which report the recorded workload and
    stay fixed at the 229 the published results ran. Today that makes scikit-learn and
    sympy 60 runnable tasks each (the de-saturation extension) and leaves the other four
    repos untouched. Only 'swebench' has additions/exclusions; other domains pass through.
    """
    groups = _BY_DOMAIN[domain]
    skip = SWEBENCH_EXCLUDED if domain == "swebench" else {}
    add = SWEBENCH_ADDED if domain == "swebench" else {}
    out = {g: tuple(i for i in tuple(v) + tuple(add.get(g, ())) if i not in skip)
           for g, v in groups.items()}
    return out[group] if group is not None else out


def task_counts(domain):
    """{group: n} — the per-group workload size of the recorded workload."""
    return {g: len(v) for g, v in _BY_DOMAIN[domain].items()}


def total_tasks(domain):
    """Total task ids in one seed of `domain` (multiply by the seed count for task-runs)."""
    return len(instances(domain))


def swebench_image_tag(instance_id):
    """Official per-instance SWE-bench execution image for `instance_id`.

    The harness convention doubles the `__` separator into `_1776_`:
    ``django__django-11532`` -> ``swebench/sweb.eval.x86_64.django_1776_django-11532:latest``
    """
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def filter_tasks(tasks, domain, group=None):
    """Subset a driver task list to the canonical workload, preserving its order.

    `tasks` is the list of ``{instance_id, problem_statement, repo}`` dicts the driver
    loads from ``TOKENECONOMIZER_<DOMAIN>_TASKS``. Use this to pin a run to exactly the
    evaluated instances instead of whatever the task file happens to contain::

        tasks = json.load(open(os.environ['TOKENECONOMIZER_SWEBENCH_TASKS']))
        tasks = workloads.filter_tasks(tasks, 'swebench', 'pydata/xarray')

    Unlike the driver's ``--repo`` prefix match, an empty result is returned as empty
    rather than falling back to the unfiltered list."""
    wanted = set(instances(domain, group))
    return [t for t in tasks if t.get("instance_id") in wanted]
