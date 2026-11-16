"""Workloads: the recorded task set stays pinned to the evaluated per-group sizes.

`workloads` is the only place the *identity* of the evaluated tasks is written down, so
these tests guard it against drift: the per-group sizes below are the evaluated set, and an
edit that moves one of them is a change to the record, not a refactor.
"""
import importlib.util
import json
import os

from te_bench import workloads

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPORTER = os.path.join(_REPO, "scripts", "export_workloads.py")
_WORKLOAD_DIR = os.path.join(_REPO, "workloads")


def _exporter():
    spec = importlib.util.spec_from_file_location("_export_workloads", _EXPORTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_swebench_counts_are_the_evaluated_set():
    assert workloads.task_counts("swebench") == {
        "django/django": 40, "matplotlib/matplotlib": 34, "scikit-learn/scikit-learn": 32,
        "sphinx-doc/sphinx": 44, "sympy/sympy": 40, "pydata/xarray": 39,
    }


def test_webbench_2026_08_export_is_frozen():
    """A record of a run that already happened, pinned on its own rather than to the live
    sweep set -- which has since retired `flex` and `prisma`."""
    assert workloads.WEBBENCH_TASKS_PUBLISHED_2026_08 == {
        "flex": 21, "expressjs": 20, "react": 20, "prisma": 20, "calculator": 21,
    }


def test_webbench_sweep_set_is_five_distinct_streams():
    """The live sweep set. Its IDENTITY is the invariant, not just its size: Web-Bench reuses
    one task stream across whole project families, so a well-meant substitution can silently
    drop the suite to four distinct streams (see the note on WEBBENCH_TASKS)."""
    counts = workloads.task_counts("webbench")
    assert set(counts) == {"calculator", "expressjs", "react", "zustand", "table"}
    assert sum(counts.values()) == 102
    # 20 graded tasks each; `calculator` and `table` additionally open with `init`.
    assert {p for p, n in counts.items() if n == 21} == {"calculator", "table"}
    assert all(n in (20, 21) for n in counts.values())


def test_totals():
    assert workloads.total_tasks("swebench") == 229
    assert workloads.total_tasks("webbench") == 102
    assert workloads.total_tasks("creddata") == 100
    assert workloads.total_tasks("reposvul") == 100


def test_no_duplicate_ids_within_a_group():
    """Ids are unique per group. Web-Bench reuses `task-N` across projects by design."""
    for domain, groups in (("swebench", workloads.SWEBENCH_INSTANCES),
                           ("webbench", workloads.WEBBENCH_TASKS)):
        for group in groups:
            ids = workloads.instances(domain, group)
            assert len(ids) == len(set(ids)), f"{domain}/{group} has duplicate task ids"
    for domain in ("creddata", "reposvul"):
        ids = workloads.instances(domain)
        assert len(ids) == len(set(ids)), f"{domain} has duplicate task ids"


def test_full_split_only_is_a_subset_and_all_xarray():
    swebench = set(workloads.instances("swebench"))
    assert workloads.SWEBENCH_FULL_SPLIT_ONLY <= swebench
    assert len(workloads.SWEBENCH_FULL_SPLIT_ONLY) == 30
    assert all(i.startswith("pydata__xarray-") for i in workloads.SWEBENCH_FULL_SPLIT_ONLY)


def test_added_extends_sklearn_and_sympy_to_sixty_runnable():
    """The de-saturation extension: both streams run 60 tasks, the rest are untouched."""
    runnable = workloads.runnable_instances("swebench")
    assert len(runnable["scikit-learn/scikit-learn"]) == 60
    assert len(runnable["sympy/sympy"]) == 60
    for group in ("django/django", "matplotlib/matplotlib", "sphinx-doc/sphinx",
                  "pydata/xarray"):
        assert len(runnable[group]) == len(workloads.instances("swebench", group))


def test_added_ids_are_new_and_grouped_under_their_own_repo():
    canonical = set(workloads.instances("swebench"))
    for group, ids in workloads.SWEBENCH_ADDED.items():
        assert group in workloads.SWEBENCH_INSTANCES, f"{group} is not a known repo"
        assert not (set(ids) & canonical), f"{group}: added ids overlap the published set"
        assert len(ids) == len(set(ids)), f"{group}: duplicate added ids"


def test_exclusion_still_wins_over_the_extension():
    """14710 stays out of the 60-task sklearn stream: it does not terminate under eval."""
    sklearn = workloads.runnable_instances("swebench", "scikit-learn/scikit-learn")
    assert "scikit-learn__scikit-learn-14710" not in sklearn
    assert "scikit-learn__scikit-learn-14710" in workloads.instances(
        "swebench", "scikit-learn/scikit-learn")


def test_added_does_not_move_the_published_record():
    """The evaluated set is 229 tasks; additions must not touch it."""
    assert workloads.total_tasks("swebench") == 229
    assert workloads.task_counts("swebench")["scikit-learn/scikit-learn"] == 32
    assert workloads.task_counts("swebench")["sympy/sympy"] == 40


def test_added_ids_need_the_full_split():
    """Both repos' Verified rows were already spent, so every addition is full-split-only."""
    added = {i for ids in workloads.SWEBENCH_ADDED.values() for i in ids}
    assert not (added & workloads.SWEBENCH_FULL_SPLIT_ONLY)


def test_every_added_instance_yields_a_tag():
    for ids in workloads.SWEBENCH_ADDED.values():
        for iid in ids:
            assert workloads.swebench_image_tag(iid).endswith(":latest")


def test_copilot_extra_is_not_in_the_canonical_set():
    assert not (workloads.SWEBENCH_COPILOT_EXTRA & set(workloads.instances("swebench")))


def test_image_tag_uses_the_1776_separator():
    tag = workloads.swebench_image_tag("django__django-11532")
    assert tag == "swebench/sweb.eval.x86_64.django_1776_django-11532:latest"
    assert "__" not in tag.split("x86_64.", 1)[1]


def test_every_swebench_instance_yields_a_tag():
    for iid in workloads.instances("swebench"):
        assert workloads.swebench_image_tag(iid).endswith(":latest")


def test_instances_rejects_unknown_domain_and_group():
    for bad in (("nope", None), ("swebench", "pydata/xarray-typo")):
        try:
            workloads.instances(*bad)
        except KeyError:
            continue
        raise AssertionError(f"expected KeyError for {bad}")


def test_filter_tasks_keeps_order_and_drops_strangers():
    ids = workloads.instances("swebench", "pydata/xarray")
    tasks = [{"instance_id": "not-a-real-instance"}] + [{"instance_id": i} for i in ids]
    kept = workloads.filter_tasks(tasks, "swebench", "pydata/xarray")
    assert [t["instance_id"] for t in kept] == list(ids)


def test_filter_tasks_returns_empty_rather_than_falling_back():
    """The driver's --repo filter falls back to the full list on no match; this must not."""
    assert workloads.filter_tasks([{"instance_id": "x"}], "swebench", "sympy/sympy") == []


def test_exported_json_exists_for_every_domain():
    for domain in workloads.DOMAINS:
        path = os.path.join(_WORKLOAD_DIR, f"{domain}.json")
        assert os.path.isfile(path), f"missing {path}; run python scripts/export_workloads.py"


def test_exported_json_matches_the_module():
    """workloads/*.json is generated from workloads.py — regenerate it when the data moves."""
    build = _exporter().build
    for domain in workloads.DOMAINS:
        with open(os.path.join(_WORKLOAD_DIR, f"{domain}.json"), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert on_disk == build(domain), (
            f"workloads/{domain}.json is stale; run python scripts/export_workloads.py")


def test_exported_json_is_small_enough_to_commit():
    """These are ids only (no issue text), so they stay comfortably committable."""
    for domain in workloads.DOMAINS:
        size = os.path.getsize(os.path.join(_WORKLOAD_DIR, f"{domain}.json"))
        assert size < 200_000, f"workloads/{domain}.json grew to {size} bytes"


def test_exported_json_group_counts_agree_with_task_counts():
    for domain in workloads.DOMAINS:
        with open(os.path.join(_WORKLOAD_DIR, f"{domain}.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        assert doc["task_counts"] == {g: len(v) for g, v in doc["groups"].items()}
        assert doc["total_tasks"] == sum(doc["task_counts"].values())


def test_webbench_streams_are_contiguous():
    """Each stream is an optional `init` followed by task-1..task-N with no gaps."""
    for project, tasks in workloads.WEBBENCH_TASKS.items():
        numbered = list(tasks[1:]) if tasks[0] == "init" else list(tasks)
        assert numbered == [f"task-{n}" for n in range(1, len(numbered) + 1)], project


# ---- the two Web-Bench registries that live OUTSIDE the repo -----------------------------
# `workloads/*.json` is committed, so the test above can diff it. The driver task list (A)
# and the projects tree (B) are not: A is written to $TOKENECONOMIZER_WEBBENCH_TASKS and B is
# the Web-Bench checkout. Both are projections of WEBBENCH_TASKS and both fail LATE and
# confusingly when stale -- a desynced A surfaces mid-run as `session is running 'X' but the
# task belongs to 'Y'`. These skip when the env is not configured, so the suite stays
# offline-clean, and bite on any machine that can actually launch a run.

def _projects_root():
    return os.environ.get("TOKENECONOMIZER_WEBBENCH_PROJECTS")


def test_every_swept_webbench_project_has_a_tasks_jsonl():
    """(B) The harness and the adapter both read `tasks.jsonl` only; a yml-only project must
    have had one derived (scripts/webbench_tasks_from_yaml.py)."""
    root = _projects_root()
    if not root or not os.path.isdir(root):
        return                                  # checkout not present; nothing to verify
    for project, task_ids in workloads.WEBBENCH_TASKS.items():
        path = os.path.join(root, project, "tasks.jsonl")
        assert os.path.isfile(path), (
            f"{project}: no tasks.jsonl -- run "
            f"python scripts/webbench_tasks_from_yaml.py {project}")
        have = set()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    have.add(json.loads(line)["id"])
        missing = [t for t in task_ids if t not in have]
        assert not missing, f"{project}: tasks.jsonl lacks {missing} named by WEBBENCH_TASKS"


def test_driver_task_list_is_not_stale():
    """(A) $TOKENECONOMIZER_WEBBENCH_TASKS must match what WEBBENCH_TASKS projects to."""
    root, out = _projects_root(), os.environ.get("TOKENECONOMIZER_WEBBENCH_TASKS")
    if not (root and os.path.isdir(root) and out and os.path.isfile(out)):
        return
    spec = importlib.util.spec_from_file_location(
        "_export_webbench_tasks", os.path.join(_REPO, "scripts", "export_webbench_tasks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with open(out, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == mod.build(root), (
        f"{out} is stale; run python scripts/export_webbench_tasks.py")


# ---- Web-Bench's per-segment cap must stay under the evaluator's request abort ------------
# TE_TASK_TIMEOUT is shared with SWE-bench, where 3600 is correct; on Web-Bench anything past
# the evaluator's ~300s undici abort loses whole attempts and truncates the stream.

def test_webbench_task_timeout_is_clamped_under_the_evaluator_abort():
    from te_harness.webbench import (webbench_task_timeout, WEBBENCH_TASK_TIMEOUT_MAX,
                                     WEBBENCH_UNDICI_ABORT_S)
    assert WEBBENCH_TASK_TIMEOUT_MAX < WEBBENCH_UNDICI_ABORT_S
    for shared in ("3600", "1800", "301"):
        assert webbench_task_timeout({"TE_TASK_TIMEOUT": shared}) == WEBBENCH_TASK_TIMEOUT_MAX


def test_webbench_task_timeout_defaults_and_overrides():
    from te_harness.webbench import webbench_task_timeout, WEBBENCH_TASK_TIMEOUT_DEFAULT
    assert webbench_task_timeout({}) == WEBBENCH_TASK_TIMEOUT_DEFAULT
    # a domain-specific value wins over the shared one, and a low value is honoured as-is
    assert webbench_task_timeout({"TE_TASK_TIMEOUT": "3600",
                                  "TE_WEBBENCH_TASK_TIMEOUT": "200"}) == 200
    # junk must fall back, never crash a lane at task 0
    assert webbench_task_timeout({"TE_TASK_TIMEOUT": "abc"}) == WEBBENCH_TASK_TIMEOUT_DEFAULT


def test_swebench_task_timeout_is_untouched_by_the_webbench_clamp():
    """SWE-bench genuinely needs 3600 (sympy tasks run to 1800 s); the clamp is domain-local."""
    import te_harness.swebench as sb
    assert "WEBBENCH_TASK_TIMEOUT_MAX" not in dir(sb)


# ---- the abort ceiling is raisable, because the evaluator's bare `fetch` uses undici's
# GLOBAL dispatcher, which `--import te_harness/webbench_undici_preload.mjs` can reset.
# Opt-in only: unset, the abort ceiling stays at undici's stock 300 s and the cap at 260 s.

def test_webbench_undici_abort_defaults_to_the_stock_ceiling_and_adds_no_env():
    from te_harness.webbench import (webbench_undici_abort, webbench_undici_env,
                                     WEBBENCH_UNDICI_ABORT_DEFAULT_S)
    assert webbench_undici_abort({}) == WEBBENCH_UNDICI_ABORT_DEFAULT_S
    assert webbench_undici_env({}) == {}          # stock spawn must be byte-identical
    assert webbench_undici_env({"TE_WEBBENCH_UNDICI_ABORT": "300"}) == {}


def test_raising_the_abort_ceiling_raises_the_cap_and_preloads_the_dispatcher():
    import os
    from te_harness.webbench import (webbench_task_timeout, webbench_undici_abort,
                                     webbench_undici_env, UNDICI_PRELOAD,
                                     WEBBENCH_TASK_TIMEOUT_HEADROOM_S)
    env = {"TE_WEBBENCH_UNDICI_ABORT": "900", "TE_TASK_TIMEOUT": "3600"}
    assert webbench_undici_abort(env) == 900
    assert webbench_task_timeout(env) == 900 - WEBBENCH_TASK_TIMEOUT_HEADROOM_S
    extra = webbench_undici_env(env, repo="/tmp/wb")
    assert extra["TE_WEBBENCH_UNDICI_ABORT_MS"] == "900000"
    assert "--import" in extra["NODE_OPTIONS"] and "webbench_undici_preload" in extra["NODE_OPTIONS"]
    assert os.path.exists(UNDICI_PRELOAD)


def test_raising_the_ceiling_preserves_an_existing_node_options():
    from te_harness.webbench import webbench_undici_env
    extra = webbench_undici_env({"TE_WEBBENCH_UNDICI_ABORT": "600",
                                 "NODE_OPTIONS": "--max-old-space-size=4096"})
    assert extra["NODE_OPTIONS"].startswith("--max-old-space-size=4096 ")
    assert "--import" in extra["NODE_OPTIONS"]


def _export_swebench():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "export_swebench_tasks.py")
    spec = importlib.util.spec_from_file_location("export_swebench_tasks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_swebench_export_writes_the_stream_in_order_and_prefers_verified():
    """The task list follows the workload's stream order whatever order the splits come in,
    a Verified row wins over the full split's, and grading carries the grader's fields."""
    from te_bench import workloads
    ex = _export_swebench()
    order = [i for ids in workloads.runnable_instances("swebench").values() for i in ids]

    def row(iid, tag):
        r = {k: f"{tag}:{k}" for k in ex.GRADING_FIELDS}
        r.update(instance_id=iid, repo=f"{tag}:repo", problem_statement=f"{tag}:issue")
        return r
    verified = [row(i, "verified") for i in reversed(order[::2])]
    full = [row(i, "full") for i in order] + [row("not__evaluated-1", "full")]
    tasks, grading = ex.build([verified, full], set(order))
    assert [t["instance_id"] for t in tasks] == order == list(grading)
    assert tasks[0]["problem_statement"] == "verified:issue"
    assert tasks[1]["problem_statement"] == "full:issue"
    assert set(grading[order[0]]) == set(ex.GRADING_FIELDS)
