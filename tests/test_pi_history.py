"""Issue #166: omp (a Pi fork) session logs -- history import and host detection.

omp keeps Pi-style sessions under `~/.omp/agent/sessions/<folder>/<timestamp>_<id>.jsonl`,
with two differences from Pi's own logs: the first line is `{"type": "title", ...}`, and
extra non-conversation entry types (`custom`, `custom_message`, `developer`,
`credential_pin`, `model_change`, ...) appear between the messages. Live sessions fold under
the `pi` host (omp loads the Pi extension), and its shells set `OMPCODE=1` as well as
`CLAUDECODE=1`.
"""
import json
import os

from tokeneconomizer.plugin import hosts
from tokeneconomizer.plugin.hosts import HOSTS, Pi, default_host
from tokeneconomizer import trace


OMP_LOG = [
    # omp writes this first; Pi writes the session line first
    {"type": "title", "v": 1, "title": "Fix widget", "source": "auto"},
    {"type": "session", "version": 3, "id": "01AAA-1111-2222-3333",
     "timestamp": "2026-11-09T08:00:00.000Z", "cwd": "/home/u/widget", "title": "Fix widget"},
    {"type": "model_change", "model": "some/model"},                      # ignored
    {"type": "message", "id": "m1", "message": {"role": "user",
                                               "content": "list the files"}},
    {"type": "custom", "payload": {}},                                    # ignored
    {"type": "message", "id": "m2", "parentId": "m1",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
]


def _omp_log(tmp_path, name="2026-11-09T08-00-00-000Z_01AAA-1111-2222-3333.jsonl"):
    p = tmp_path / "sessions" / "-home-u-widget" / name
    p.parent.mkdir(parents=True)
    with open(p, "w", encoding="utf-8") as fh:
        for ev in OMP_LOG:
            fh.write(json.dumps(ev) + "\n")
    return p


def test_the_header_scan_skips_omps_title_line(tmp_path):
    p = _omp_log(tmp_path)
    head = trace.pi_session_header(str(p))
    assert head.get("id") == "01AAA-1111-2222-3333"
    assert head.get("cwd") == "/home/u/widget"


def test_the_prompt_count_ignores_omps_extra_entry_types(tmp_path):
    p = _omp_log(tmp_path)
    assert trace.pi_prompt_count(str(p)) == 1


def test_pi_history_lists_omp_sessions_by_their_id(tmp_path):
    p = _omp_log(tmp_path)
    pi = Pi()
    logs = pi.history(env={"HOME": "/nonexistent"}, extra_dirs=(str(tmp_path),))
    assert len(logs) == 1
    # the session id comes after the last underscore, not the whole file stem
    assert logs[0]["session_id"] == "01AAA-1111-2222-3333"
    assert logs[0]["path"] == str(p)


def test_the_log_head_reads_the_session_line(tmp_path):
    p = _omp_log(tmp_path)
    head = Pi().log_head(str(p))
    assert head["session_id"] == "01AAA-1111-2222-3333"
    assert head["cwd"] == "/home/u/widget"
    assert head["started"] is not None            # epoch seconds from the timestamp


def test_a_resumed_import_still_accepts_omps_file_name(tmp_path):
    from tokeneconomizer.plugin.history import _still_eligible

    class _Ctx:
        class store:                              # noqa: N801 - a stub the function reads
            @staticmethod
            def read(name):
                return {}

            @staticmethod
            def path(*_a):
                return str(tmp_path / "none.json")

    item = {"id": "01AAA-1111-2222-3333",
            "path": str(_omp_log(tmp_path))}
    assert _still_eligible(_Ctx(), item) is None  # not "unsafe_id"
    assert _still_eligible(_Ctx(), {"id": "../evil", "path": "x"}) == "unsafe_id"


def test_the_converted_log_carries_the_timestamp_and_cwd(tmp_path):
    p = _omp_log(tmp_path)
    out = tmp_path / "converted.jsonl"
    assert trace.pi_session_to_claude(str(p), str(out)) == 0   # no tool calls
    lines = [json.loads(l) for l in open(out, encoding="utf-8")]
    user = next(l for l in lines if l.get("type") == "user")
    assert user.get("timestamp") == "2026-11-09T08:00:00.000Z"
    assert user.get("cwd") == "/home/u/widget"
    assert trace.claude_log_head(str(out))["started"] is not None
    assert trace.claude_log_head(str(out))["cwd"] == "/home/u/widget"


def test_an_imported_omp_session_folds_with_its_prompts(tmp_path):
    from tokeneconomizer.plugin import history
    from tokeneconomizer.plugin.context import LiveContext
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "home"), "TE_FOLD_SYNC": "1",
           "TE_MICROSKILLS": "envfacts", "GIT_CEILING_DIRECTORIES": str(tmp_path),
           "PATH": os.environ.get("PATH", "")}
    ctx = LiveContext.build(Pi(), str(repo), env)
    p = _omp_log(tmp_path)
    item = {"id": "01AAA-1111-2222-3333", "path": str(p), "started": 1.0, "ended": 2.0}
    skipped = {}
    history._import_one(ctx, {}, item, skipped, None)
    assert skipped == {}
    folded = ctx.store.read("folded.json")
    assert folded["01AAA-1111-2222-3333"]["imported"]
    rec = [r for r in ctx.store.read_lines("ledger.jsonl") if r["kind"] == "session"]
    assert len(rec) == 1 and rec[0]["prompts"] == 1 and rec[0]["imported"]


def test_omps_shell_detects_as_the_pi_host():
    env = {"OMPCODE": "1", "CLAUDECODE": "1"}    # omp sets both; OMPCODE wins
    assert default_host(env) == "pi"
    assert default_host({"CLAUDECODE": "1"}) == "claude"
    assert default_host({}) in HOSTS            # the plain-terminal fallback
