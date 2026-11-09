"""`media=audio`: transcripts of the audio files a prompt names, from an on-device helper."""
import os
import stat
import subprocess

import pytest

from tokeneconomizer.plugin import config as live_config
from tokeneconomizer.plugin import media


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "memo.aiff").write_bytes(b"FORM....AIFF")
    (tmp_path / "talk.m4a").write_bytes(b"....ftypM4A ")
    return tmp_path


@pytest.mark.parametrize("prompt, want", [
    ("Implement what notes/memo.aiff asks.", ["notes/memo.aiff"]),     # a sentence's period
    ("see `talk.m4a`, then notes/memo.aiff", ["talk.m4a", "notes/memo.aiff"]),
    ("notes/memo.aiff and notes/memo.aiff again", ["notes/memo.aiff"]),
    ("https://example.com/talk.m4a", []),                              # a URL is not a file
    ("notes/missing.aiff", []),                                         # nor a missing one
    ("notes/memo.aiff.bak", []),
])
def test_audio_paths(repo, prompt, want):
    assert media.audio_paths(prompt, str(repo)) == [str(repo / w) for w in want]


def _fake_helper(tmp_path, monkeypatch, body):
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home))
    h = home / "bin" / "te-transcribe"
    h.parent.mkdir(parents=True)
    h.write_text("#!/bin/sh\n" + body)
    h.chmod(h.stat().st_mode | stat.S_IEXEC)
    return home


def test_for_prompt_transcribes_and_caches(repo, tmp_path, monkeypatch):
    home = _fake_helper(tmp_path, monkeypatch, 'echo "round half up"; echo x >> "$0.calls"\n')
    block, info = media.for_prompt("do what notes/memo.aiff says", str(repo))
    assert "On-device transcript of notes/memo.aiff" in block and "round half up" in block
    assert info == {"files": 1, "transcribed": 1, "needs_helper": False}
    media.for_prompt("again: notes/memo.aiff", str(repo))
    assert (home / "bin" / "te-transcribe.calls").read_text().count("x") == 1   # cached


def test_for_prompt_without_the_helper_says_it_needs_one(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "empty"))
    block, info = media.for_prompt("notes/memo.aiff", str(repo))
    assert block == "" and info["needs_helper"] is True


def test_a_failing_helper_adds_nothing(repo, tmp_path, monkeypatch):
    _fake_helper(tmp_path, monkeypatch, "echo boom >&2; exit 1\n")
    assert media.for_prompt("notes/memo.aiff", str(repo))[0] == ""


def test_build_helper_needs_swiftc(monkeypatch):
    monkeypatch.setattr(media, "supported", lambda: False)
    ok, msg = media.build_helper()
    assert not ok and "swiftc" in msg


def test_build_helper_compiles_the_bundled_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    monkeypatch.setattr(media, "supported", lambda: True)
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        open(cmd[cmd.index("-o") + 1], "w").close()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    ok, out = media.build_helper()
    assert ok and out == media.helper_path() and os.path.isfile(out)
    assert seen["cmd"][0] == "swiftc" and seen["cmd"][3].endswith("te-transcribe.swift")


def test_media_setting():
    c = live_config.LiveConfig()
    assert c.media == "off" and c.set("media", "audio") and not c.set("media", "video")


# ---- through the hooks ---------------------------------------------------------------------
from tests.test_live_hooks import call, env, repo as hook_repo  # noqa: E402,F401


def _ctx_text(out):
    return (out.get("hookSpecificOutput") or {}).get("additionalContext") or out.get(
        "additionalContext") or ""


def _prompt(e, cwd, sid, prompt):
    base = {"session_id": sid, "transcript_path": "", "cwd": cwd, "prompt_id": "p"}
    call(e, dict(base, hook_event_name="SessionStart", source="startup"))
    return call(e, dict(base, hook_event_name="UserPromptSubmit", prompt=prompt))


@pytest.fixture
def memo(hook_repo):
    os.makedirs(os.path.join(hook_repo, "notes"), exist_ok=True)
    with open(os.path.join(hook_repo, "notes", "memo.aiff"), "wb") as fh:
        fh.write(b"FORM....AIFF")
    return hook_repo


def _helper(e):
    if os.name == "nt":
        pytest.skip("the stand-in helper is an sh script")
    h = os.path.join(e["TOKENECONOMIZER_HOME"], "bin", "te-transcribe")
    os.makedirs(os.path.dirname(h), exist_ok=True)
    with open(h, "w") as fh:
        fh.write("#!/bin/sh\necho 'please round half up'\n")
    os.chmod(h, 0o755)


def test_a_prompt_naming_audio_gets_its_transcript_in_context(env, memo):
    e = dict(env, TE_MEDIA="audio")
    _helper(e)
    out = _prompt(e, memo, "m1", "Implement what notes/memo.aiff asks.")
    text = _ctx_text(out)
    assert "On-device transcript of notes/memo.aiff" in text and "round half up" in text


def test_media_off_or_shadow_adds_nothing(env, memo):
    _helper(env)
    assert _ctx_text(_prompt(env, memo, "m2", "Implement notes/memo.aiff")) == ""
    e = dict(env, TE_MEDIA="audio", TE_SHADOW="1")
    assert _ctx_text(_prompt(e, memo, "m3", "Implement notes/memo.aiff")) == ""


def test_without_the_helper_the_first_prompt_starts_a_build_and_says_so(env, memo, monkeypatch):
    from tokeneconomizer.plugin import media as m
    monkeypatch.setattr(m, "supported", lambda: True)
    spawned = []
    from tokeneconomizer.plugin import context
    monkeypatch.setattr(context.LiveContext, "spawn", lambda self, argv, log: spawned.append(argv))
    out = _prompt(dict(env, TE_MEDIA="audio"), memo, "m4", "Implement notes/memo.aiff")
    assert spawned == [["media", "setup"]] and "audio transcriber" in out.get("systemMessage", "")
