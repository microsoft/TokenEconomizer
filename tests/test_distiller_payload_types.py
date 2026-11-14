"""Malformed proposer payloads: wrongly-typed fields are dropped, never crash the distiller."""
import json

import pytest

import tokeneconomizer.microskills.distiller as dist
from tokeneconomizer.microskills import Distiller
from tokeneconomizer.microskills.distiller import parse_payload
from fixtures import write_claude_transcript


def _runner(payload):
    return lambda prompt, model="agent": (payload, 1.0)


def _payload(**body):
    return json.dumps(body)


MALFORMED = [
    '{"extractors": null, "facts": null}',
    _payload(extractors=[{"name": ["a"], "pattern": "x"}], facts=[]),
    _payload(extractors=[{"name": {"x": 1}, "pattern": "x"}], facts=[]),
    _payload(extractors=[], facts=[{"id": ["a"], "detector": {}}]),
    _payload(extractors=[], facts=[{"id": {"x": 1}, "detector": {}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": ["bad"]}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": {"a": 1}}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": "ran_tests", "fired": 5}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": "ran_tests", "confirm": ["x"]}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": "ran_tests", "contradict": {"x": 1}}}]),
    _payload(extractors=[], facts=[{"id": "f", "detector": {"scope": "ran_tests"}, "cite": 3}]),
]


@pytest.mark.parametrize("raw", MALFORMED)
def test_parse_payload_drops_wrongly_typed_fields(raw):
    exts, facts = parse_payload(raw)
    assert exts == []
    assert facts == []


def test_parse_payload_keeps_valid_entries_next_to_malformed():
    raw = _payload(
        extractors=[{"name": ["a"], "pattern": "x"}, {"name": "ok", "pattern": "x"}],
        facts=[{"id": {"x": 1}, "detector": {}},
               {"id": "good", "detector": {"scope": "ran_tests", "confirm": "passed"}}])
    exts, facts = parse_payload(raw)
    assert [e["name"] for e in exts] == ["ok"]
    assert [f["id"] for f in facts] == ["good"]


def test_ground_treats_non_list_cite_as_empty(tmp_path):
    d = Distiller("acme/widget")
    tr = str(tmp_path / "t.jsonl")
    write_claude_transcript(tr, [("bash", {"command": "pytest"}, "1 passed")])
    d.update(tr, iid="w-0", resolved=True)
    for cite in (3, "w-0", {}, {"w-0": 1}, [["w-0"], {"x": 1}, 7]):
        spec = {"id": "f", "detector": {"scope": "ran_tests", "confirm": "passed"}, "cite": cite}
        assert d._ground(spec) == 0


@pytest.mark.parametrize("raw", MALFORMED)
def test_update_survives_malformed_proposer_reply(tmp_path, raw):
    d = Distiller("acme/widget", runner=_runner(raw))
    d.last_propose = -1
    saved, dist.PROPOSE_EVERY = dist.PROPOSE_EVERY, 1
    try:
        for i in range(3):
            tr = str(tmp_path / f"t{i}.jsonl")
            write_claude_transcript(tr, [("bash", {"command": "pytest"}, "1 passed")])
            d.update(tr, iid=f"w-{i}", resolved=True)
    finally:
        dist.PROPOSE_EVERY = saved
    assert d.facts == {}
    assert d.extractors == {}


def test_valid_fact_admitted_alongside_malformed_ones(tmp_path):
    raw = _payload(
        extractors=[{"name": ["bad"], "pattern": "x"}],
        facts=[{"id": ["bad"], "detector": {}},
               {"id": "use-pytest", "claim": "pytest is canonical", "injection": "run pytest",
                "detector": {"scope": "ran_tests", "confirm": "passed", "fired": "ran_tests"},
                "cite": ["w-0", "w-1"]}])
    d = Distiller("acme/widget", runner=_runner(raw))
    d.last_propose = -1
    saved, dist.PROPOSE_EVERY = dist.PROPOSE_EVERY, 1
    try:
        for i in range(2):
            tr = str(tmp_path / f"t{i}.jsonl")
            write_claude_transcript(tr, [("bash", {"command": "pytest"}, "1 passed")])
            d.update(tr, iid=f"w-{i}", resolved=True)
    finally:
        dist.PROPOSE_EVERY = saved
    assert "use-pytest" in d.facts
    assert set(d.facts) == {"use-pytest"}
