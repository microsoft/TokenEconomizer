"""The installable plugin: manifests, hook files, launchers, and the self-contained bundle.

These pin the packaging facts a host relies on and a refactor could silently break: every hook
file runs the one launcher that exists and is executable, the Codex file names its host and keeps
SessionEnd under Codex's three-second cap, the marketplace points at the plugin directory, the
launcher fails open, and the bundle `scripts/build_plugin.py` writes runs with no checkout beside
it -- which is what a marketplace install, copying only the plugin directory, actually gets.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin")
# Windows runs no shebang: the launchers go through Git Bash's `sh`.
SH = ["sh"] if os.name == "nt" else []
EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def _commands(hooks_doc):
    for event, groups in hooks_doc["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                yield event, group, h


def test_manifests_agree_on_identity():
    claude = _json(PLUGIN, ".claude-plugin", "plugin.json")
    codex = _json(PLUGIN, ".codex-plugin", "plugin.json")
    # Copilot reads `.github/plugin/plugin.json` in preference to `.claude-plugin/` (verified on
    # 1.0.83: only the hooks it names ran), which is how it gets a hook file of its own.
    copilot = _json(PLUGIN, ".github", "plugin", "plugin.json")
    assert claude["name"] == codex["name"] == copilot["name"] == "tokeneconomizer"
    assert claude["version"] == codex["version"] == copilot["version"]
    assert codex["hooks"] == "./hooks/codex-hooks.json"
    assert copilot["hooks"] == "hooks/copilot-hooks.json"
    for m in (codex, copilot):
        assert os.path.exists(os.path.join(PLUGIN, m["hooks"]))
    assert "hooks" not in claude              # Claude Code reads hooks/hooks.json by convention


@pytest.mark.parametrize("name,host", [("hooks.json", None), ("codex-hooks.json", "codex"),
                                       ("copilot-hooks.json", "copilot")])
def test_hook_files_run_the_launcher_on_every_event(name, host):
    doc = _json(PLUGIN, "hooks", name)
    # no PreToolUse: the plugin never blocks a call. Claude Code and Copilot report a failed
    # tool in an event of its own, which the guard must see too.
    want = EVENTS | ({"PostToolUseFailure"} if host in (None, "copilot") else set())
    assert set(doc["hooks"]) == want
    launcher = os.path.join(PLUGIN, "scripts", "te-hook")
    assert os.access(launcher, os.X_OK)
    for event, group, h in _commands(doc):
        assert h["type"] == "command" and "/scripts/te-hook" in h["command"]
        assert "matcher" not in group                     # PostToolUse watches every tool
        if host:
            assert f"--host {host}" in h["command"]
        if host == "copilot":
            assert h["command"].startswith('"${PLUGIN_ROOT}/scripts/te-hook"')
        if host == "codex":
            assert h["commandWindows"] == CODEX_WINDOWS
            if event == "SessionEnd":
                assert h["timeout"] <= 3                  # Codex's cap for SessionEnd
        else:
            assert "commandWindows" not in h


# Codex on Windows runs a hook's `commandWindows` in a native shell, cmd.exe or PowerShell, where
# `command`'s sh syntax means nothing (0.164.0). This one reads the same in both: Python finds the
# plugin in the environment Codex sets, so no path or variable is on the line; the code has no
# space, double quote, `%` or `$`; and `--%`, PowerShell's stop-parsing token, keeps its `;` and
# parentheses from PowerShell, while cmd.exe passes it on to Python as `-X --%`, which Python
# ignores (PowerShell drops it, so there `-X` takes the `-u` instead).
CODEX_WINDOWS = (
    "python -X --% -u -X utf8 -c e=__import__('os').environ;r=e.get('PLUGIN_ROOT')"
    "or(e['CLAUDE_PLUGIN_ROOT']);s=__import__('sys');"
    "s.argv[:]=[r+'/scripts/te_bootstrap.py','hook','--host','codex'];"
    "__import__('runpy').run_path(s.argv[0],run_name='__main__')")


@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_codex_windows_command_runs_the_hook_as_either_shell_passes_it(tmp_path, shell):
    """Each shell's reading of `commandWindows`, as the argv Python receives: cmd.exe passes the
    line on as it is, PowerShell without its `--%`. Neither has a quote to strip."""
    argv = CODEX_WINDOWS.split()
    if shell == "powershell":
        argv.remove("--%")
    assert argv[0] == "python"
    work = tmp_path / "work"
    work.mkdir()
    payload = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(work),
               "source": "startup", "turn_id": "t"}
    p = subprocess.run([sys.executable] + argv[1:], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60,
                       env=_hook_env(tmp_path, PLUGIN_ROOT=PLUGIN))
    assert p.returncode == 0 and json.loads(p.stdout) == {}, p.stderr[-1000:]
    assert list((tmp_path / "home" / "repos").glob("*/codex/sessions/s.json"))   # it ran
    assert not os.path.exists(tmp_path / "home" / "errors.log")


def test_marketplace_lists_the_plugin_directory():
    m = _json(REPO, ".claude-plugin", "marketplace.json")
    entry = {p["name"]: p for p in m["plugins"]}["tokeneconomizer"]
    assert entry["source"] == "./plugin"
    assert os.path.isdir(os.path.join(REPO, entry["source"], ".claude-plugin"))


def test_no_bin_directory():
    """Files in a plugin's bin/ go on the Bash tool's PATH, and claude.ai will not install a
    plugin that has one; the launchers live in scripts/."""
    assert not os.path.exists(os.path.join(PLUGIN, "bin"))


def _run_hook(launcher, payload, env):
    p = subprocess.run(SH + [launcher], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


# What every Windows process inherits, whatever runs it: the launchers know Windows by `OS`, and
# Python and Git need the rest to start.
WINDOWS_ENV = ("OS", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def _hook_env(tmp_path, **extra):
    env = {k: os.environ[k] for k in WINDOWS_ENV if os.name == "nt" and k in os.environ}
    env.update({"PATH": os.environ.get("PATH", ""), "TOKENECONOMIZER_HOME": str(tmp_path / "home"),
                "GIT_CEILING_DIRECTORIES": str(tmp_path), "TE_FOLD_SYNC": "1"})
    env.update(extra)
    return env


def test_the_launcher_runs_the_dispatcher_from_the_checkout(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart", "session_id": "s",
                              "cwd": str(work), "source": "startup"}, _hook_env(tmp_path))
    assert code == 0 and json.loads(out) == {}
    assert os.path.isdir(tmp_path / "home" / "repos")      # it ran, and recorded the session


def test_the_launcher_fails_open_without_python(tmp_path):
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart"},
                             {"PATH": "", "TE_PYTHON": "/nonexistent/python"})
    assert code == 0 and json.loads(out) == {}


def test_the_bootstrap_answers_no_change_on_an_old_python(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(te_bootstrap, "MIN_VERSION", (99, 0))
    monkeypatch.setattr(sys, "argv", ["te_bootstrap.py", "hook"])
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert te_bootstrap.main() == 0 and out.getvalue().strip() == "{}"


def test_the_bundle_runs_without_a_checkout(tmp_path):
    out = tmp_path / "market"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_plugin.py"),
                    "--out", str(out)], check=True, capture_output=True)
    bundle = out / "tokeneconomizer"
    assert (bundle / "lib" / "tokeneconomizer" / "plugin" / "hook.py").exists()
    assert not (bundle / "lib" / "te_live").exists()
    assert (bundle / ".github" / "plugin" / "plugin.json").exists()
    assert (bundle / "hooks" / "copilot-hooks.json").exists()
    assert (bundle / "lib" / "tokeneconomizer" / "__init__.py").exists()
    assert not (bundle / "lib" / "te_harness").exists()
    assert not (bundle / "lib" / "te_bench").exists()
    assert not list(bundle.rglob("__pycache__"))
    m = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert m["plugins"][0]["source"] == "./tokeneconomizer"
    work = tmp_path / "work"
    work.mkdir()
    # Run the bundled launcher with the core reachable ONLY through the bundle's lib/.
    code, stdout, stderr = _run_hook(str(bundle / "scripts" / "te-hook"),
                                     {"hook_event_name": "SessionStart", "session_id": "s",
                                      "cwd": str(work), "source": "startup"},
                                     _hook_env(tmp_path, PYTHONNOUSERSITE="1"))
    assert code == 0 and json.loads(stdout) == {}, stderr
    sys.path.insert(0, str(bundle / "scripts"))
    try:
        import importlib
        import te_bootstrap
        importlib.reload(te_bootstrap)
        assert te_bootstrap.core_path() == str(bundle / "lib")
    finally:
        sys.path.pop(0)


# ---- the layering the README promises ------------------------------------------------------
MECHANISMS = ("microskills", "toolprune", "loopguard", "trace")
CORE_MODULES = ("config.py", "domain.py", "ledger.py", "pricing.py", "stage.py")


def _imports(path):
    """(module, level) of every import statement in a file."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level
            if node.level and not node.module:          # `from .. import plugin`
                for a in node.names:
                    yield a.name, node.level


def _core_files():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for name in CORE_MODULES + ("__init__.py",):
        yield os.path.join(pkg, name), 0
    for sub in MECHANISMS:
        for name in sorted(os.listdir(os.path.join(pkg, sub))):
            if name.endswith(".py"):
                yield os.path.join(pkg, sub, name), 1


def test_the_mechanisms_never_import_the_plugin_runtime():
    """The plugin runs the mechanisms; the mechanisms never know it exists. Checked on the source
    (no import names `plugin`, absolutely or relatively) and on a live import."""
    for path, depth in _core_files():
        for mod, level in _imports(path):
            if level == 0:
                assert not mod.startswith("tokeneconomizer.plugin"), f"{path} imports {mod}"
            else:
                # `from ..plugin import x` inside a subpackage, `from .plugin` at the top level
                parts = mod.split(".")
                reaches_top = level == depth + 1
                assert not (reaches_top and parts[0] == "plugin"), f"{path} imports .{mod}"
    probe = ("import sys, tokeneconomizer, tokeneconomizer.domain, tokeneconomizer.stage\n"
             + "".join(f"import tokeneconomizer.{m}\n" for m in MECHANISMS)
             + "print(sorted(m for m in sys.modules if m.startswith('tokeneconomizer.plugin') "
               "or m.split('.')[0] in ('te_bench', 'te_harness', 'te_live')))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_plugin_ships_and_loads_no_benchmark_code():
    """The benchmark adapters, the facade, the workloads, the baselines and the benchmark-only
    controllers live with the experiments (`experiments/te_bench/`). The plugin's library is the
    package alone, and running every part of the plugin loads none of them."""
    shipped = os.path.join(REPO, "tokeneconomizer")       # what lib/ links (below)
    assert sorted(n for n in os.listdir(shipped)
                  if os.path.isdir(os.path.join(shipped, n)) and n != "__pycache__") == sorted(
        MECHANISMS + ("plugin",))
    for gone in ("adapters", "mechanisms", "vendor", "facade.py", "workloads.py", "runner.py",
                 "arms.py", "baselines"):
        assert not os.path.exists(os.path.join(shipped, gone)), gone
    for gone in ("codex_spiral.py", "spiral_stage.py", "nudge_guard.py"):
        assert not any(gone in files for _, _, files in os.walk(shipped)), gone
    mods = ("hook", "controller", "fold", "cli", "denylist", "toolcheck", "repomap",
            "sidecall", "judge", "probe")
    probe = ("import sys\n" + "".join(f"import tokeneconomizer.plugin.{m}\n" for m in mods)
             + "print(sorted({m.split('.')[0] for m in sys.modules} "
               "& {'te_bench', 'te_harness', 'te_live'}))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_package_is_stdlib_only_and_never_touches_the_benchmark_harness():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for top, _, files in os.walk(pkg):
        for name in sorted(files):
            # the repository-map generator runs as a subprocess under a Python that has aider;
            # nothing imports it
            if not name.endswith(".py") or name == "repomap_gen.py":
                continue
            path = os.path.join(top, name)
            for mod, level in _imports(path):
                if level:
                    continue
                root = mod.split(".")[0]
                assert root not in ("te_harness", "te_bench", "te_live", "docker"), \
                    f"{path} imports {mod}"
                # tomllib: standard library from 3.11, imported behind an ImportError guard
                assert root in sys.stdlib_module_names or root in (
                    "tokeneconomizer", "tomllib"), f"{path} imports non-stdlib {mod}"


def test_a_resumed_pi_conversation_keeps_the_section_it_carries():
    """Pi rebuilds a conversation's system message from its system entries, so a resumed one is
    given back the facts section it carries, byte for byte: unwrapped from the `<name>` tags Pi
    stores it in (Pi wraps it again), gone once a patch removed it, and as a compaction restated
    it. Run through Node, which loads the extension's TypeScript directly."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    ext = os.path.join(PLUGIN, "pi", "tokeneconomizer.ts")
    script = """
const { pathToFileURL } = await import("node:url");      // Windows: a path is no import URL
const { carriedSection } = await import(pathToFileURL(process.argv[1]).href);
const sys = (sections) => ({ type: "message", message: { role: "system", sections } });
const compact = (sections) => ({ type: "compaction", systemMessage: { sections } });
const user = { type: "message", message: { role: "user" } };
const facts = sys({ tokeneconomizer: "<tokeneconomizer>\\nFacts:\\n- one\\n</tokeneconomizer>" });
console.log(JSON.stringify([
  carriedSection([]),
  carriedSection([facts, user]),
  carriedSection([facts, sys({ cwd: "<cwd>\\n/x\\n</cwd>" })]),
  carriedSection([facts, sys({ tokeneconomizer: null })]),
  carriedSection([facts, compact({ cwd: "c" })]),
  carriedSection([compact(facts.message.sections)]),
]));
"""
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings",
                          "--input-type=module", "-e", script, ext],
                         capture_output=True, text=True, timeout=60)
    if run.returncode and "strip-types" in run.stderr:
        pytest.skip("this node cannot load TypeScript")
    assert run.returncode == 0, run.stderr
    facts = "Facts:\n- one"
    assert json.loads(run.stdout) == ["", facts, facts, "", "", facts]


def test_the_plugin_directory_is_also_a_pi_package():
    """One directory, three agents: Pi loads the extension its `pi` manifest names, and that
    extension drives the same launchers the Claude Code and Codex hooks run."""
    pkg = _json(PLUGIN, "package.json")
    assert "pi-package" in pkg["keywords"]
    (ext,) = pkg["pi"]["extensions"]
    src = open(os.path.join(PLUGIN, ext), encoding="utf-8").read()
    assert 'join(HERE, "..", "scripts")' in src and '"te-hook", ["--host", "pi"]' in src
    # Exactly the dispatcher's five events. Not `tool_call`, the one Pi event that can block a
    # call before it runs: the plugin guards nothing, on Pi as on the hook hosts.
    assert set(re.findall(r'pi\.on\("(\w+)"', src)) == {
        "session_start", "before_agent_start", "tool_result", "agent_end", "session_shutdown"}
    # The repository root is the same package for `pi install git:github.com/...`, which clones
    # the repository and reads the manifest at its root: same package, same extension file.
    root = _json(os.path.dirname(PLUGIN), "package.json")
    (root_ext,) = root["pi"]["extensions"]
    assert os.path.realpath(os.path.join(os.path.dirname(PLUGIN), root_ext)) == \
        os.path.realpath(os.path.join(PLUGIN, ext))
    assert {k: v for k, v in root.items() if k != "pi"} == {k: v for k, v in pkg.items()
                                                           if k != "pi"}
    # no lockfile: Claude Code installs a plugin's npm dependencies only when one exists,
    # and this package has none to install (the Pi API is a peer dependency)
    assert not any(os.path.exists(os.path.join(PLUGIN, f)) for f in
                   ("package-lock.json", "npm-shrinkwrap.json", "bun.lock", "bun.lockb"))


@pytest.mark.skipif(not os.path.islink(os.path.join(PLUGIN, "lib", "tokeneconomizer")),
                    reason="checkout without symlinks (Windows): see the known_marketplaces test")
def test_the_plugin_carries_the_core_as_symlinks_an_install_copy_follows(tmp_path):
    """Claude Code installs a COPY of plugin/ into its cache, even from a local checkout, so the
    core must be reachable from inside plugin/."""
    import shutil
    assert os.listdir(os.path.join(PLUGIN, "lib")) == ["tokeneconomizer"]
    link = os.path.join(PLUGIN, "lib", "tokeneconomizer")
    assert os.path.islink(link)
    # Windows reports the target with its own separator
    assert os.readlink(link).replace(os.sep, "/") == "../../tokeneconomizer"
    assert os.path.isfile(os.path.join(link, "plugin", "hook.py"))
    # What an install does: copy the directory, following links. The copy runs on its own.
    copy = str(tmp_path / "cache" / "tokeneconomizer" / "0.1.0")
    shutil.copytree(PLUGIN, copy, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = subprocess.run(SH + [os.path.join(copy, "scripts", "te"), "doctor"], capture_output=True,
                         text=True, env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h")),
                         timeout=60)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]


def test_a_codex_install_copy_finds_the_core_in_the_marketplace_it_came_from(tmp_path):
    """Codex installs a COPY of plugin/ that drops the `lib` symlink (0.153.1), so the copy
    finds the core through the marketplace's `source` in Codex's config.toml."""
    import shutil
    home = tmp_path / "codex"
    copy = home / "plugins" / "cache" / "tokeneconomizer" / "tokeneconomizer" / "0.1.0"
    shutil.copytree(PLUGIN, str(copy), symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "lib"))
    (copy / "lib").mkdir()

    def te(source, literal=False):
        # As Codex writes it: a TOML basic string (a JSON string is one), or on Windows, for a
        # path with backslashes, a literal one (0.163.0).
        value = f"'{source}'" if literal else json.dumps(source, ensure_ascii=False)
        (home / "config.toml").write_text(
            'check_for_update_on_startup = false\n\n[marketplaces.tokeneconomizer]\n'
            f'source_type = "local"\nsource = {value}\n')
        return subprocess.run(SH + [str(copy / "scripts" / "te"), "doctor"], capture_output=True,
                              text=True, timeout=60, cwd=str(tmp_path),
                              env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h"),
                                       TE_CORE_PATH="", PYTHONPATH=""))
    out = te(REPO)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
    out = te(REPO, literal=True)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
    out = te(str(tmp_path / "nowhere"))
    assert out.returncode == 1 and "core package was not found" in out.stderr
    assert "was not found for the plugin at" in (tmp_path / "h" / "errors.log").read_text()
    # A git marketplace (`codex plugin marketplace add owner/repo`): config.toml names the URL,
    # and Codex keeps its checkout under .tmp/marketplaces (seen on 0.153.1).
    (home / ".tmp" / "marketplaces").mkdir(parents=True)
    try:
        os.symlink(REPO, str(home / ".tmp" / "marketplaces" / "tokeneconomizer"))
    except OSError:
        pytest.skip("symlinks need privileges on Windows")
    out = te("https://github.com/microsoft/TokenEconomizer.git")
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]


def test_the_codex_marketplace_source_is_read_in_either_toml_string_form(tmp_path):
    r"""Codex on Windows writes a local marketplace's `source` as a TOML literal string, with the
    extended-length prefix (0.163.0): `source = '\\?\C:\src\TokenEconomizer'`. A basic string
    has its escapes undone, and another marketplace's `source` is not this one's."""
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    home = tmp_path / "codex"
    home.mkdir()
    copy = home / "plugins" / "cache" / "tokeneconomizer" / "tokeneconomizer" / "0.1.0"

    def source(line):
        (home / "config.toml").write_text(
            "[marketplaces.other]\nsource = '/elsewhere'\n\n"
            f'[marketplaces.tokeneconomizer]\nsource_type = "local"\n{line}\n', encoding="utf-8")
        return te_bootstrap._marketplace_sources(str(copy))[1:]   # after Codex's git checkout

    assert source(r"source = '\\?\C:\src\TokenEconomizer'") == [r"C:\src\TokenEconomizer"]
    assert source(r"source = '\\?\UNC\server\share\TokenEconomizer'") == [
        r"\\server\share\TokenEconomizer"]
    assert source(r'source = "C:\\src\\Token \"Economizer\""') == [r'C:\src\Token "Economizer"']
    assert source('source = "/home/me/TokenEconomizer"') == ["/home/me/TokenEconomizer"]


def test_a_claude_install_copy_without_symlinks_finds_the_core_in_its_marketplace(tmp_path):
    """On a checkout without symlinks (Windows), `lib/tokeneconomizer` is a file holding the
    link's target, and Claude Code's install copy (`~/.claude/plugins/cache/...`) carries that
    file, not the package. The copy finds the core through the marketplace Claude Code recorded
    in `plugins/known_marketplaces.json`."""
    home = tmp_path / "claude-home"
    copy = home / "plugins" / "cache" / "tokeneconomizer" / "tokeneconomizer" / "0.1.0"
    shutil.copytree(PLUGIN, str(copy), symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "lib"))
    (copy / "lib").mkdir()
    (copy / "lib" / "tokeneconomizer").write_text("../../tokeneconomizer")

    def te(path):
        (home / "plugins" / "known_marketplaces.json").write_text(json.dumps(
            {"tokeneconomizer": {"source": {"source": "directory", "path": path},
                                 "installLocation": path}}))
        return subprocess.run(SH + [str(copy / "scripts" / "te"), "doctor"], capture_output=True,
                              text=True, timeout=60, cwd=str(tmp_path),
                              env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h"),
                                       TE_CORE_PATH="", PYTHONPATH=""))
    out = te(REPO)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
    out = te(str(tmp_path / "nowhere"))
    assert out.returncode == 1 and "core package was not found" in out.stderr


def test_codex_gets_its_commands_as_plugin_skills_and_no_one_else_does():
    """Codex has no plugin slash commands; its plugins carry skills. Ours live outside `skills/`,
    which Claude Code and Copilot would load beside their slash commands. Codex lists a plugin's
    skills under the plugin's name (0.163.0), so `status` is `$tokeneconomizer:status`, as the
    slash command is `/tokeneconomizer:status`; each description names TokenEconomizer, which is
    what a request in plain words, or a Codex that lists the bare name, matches on."""
    from tokeneconomizer.plugin import cli
    manifest = _json(PLUGIN, ".codex-plugin", "plugin.json")
    assert manifest["skills"] == "./codex-skills"
    assert not os.path.exists(os.path.join(PLUGIN, "skills"))
    root = os.path.join(PLUGIN, "codex-skills")
    subcommands = set(cli.build_parser()._subparsers._group_actions[0].choices)
    names = sorted(os.listdir(root))
    assert names == ["config", "microskills", "repos", "savings", "status", "toolprune"]
    for name in names:
        text = open(os.path.join(root, name, "SKILL.md"), encoding="utf-8").read()
        assert text.startswith(f'---\nname: "{name}"\ndescription: >\n')
        assert "TokenEconomizer" in text.split("\n")[3]
        cmd = re.search(r'bin/te" --host codex --cwd "\$\(pwd\)" (\w+)', text)
        assert cmd and cmd.group(1) in subcommands and cmd.group(1) == name


def test_the_commands_keep_their_earlier_names(tmp_path, monkeypatch, capsys):
    """`te facts` / `te untool` and `/tokeneconomizer:facts` / `:untool` were the names before
    the mechanisms were renamed; they run exactly what `microskills` / `toolprune` run."""
    from tokeneconomizer.plugin import cli
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    for new, old in (("microskills", "facts"), ("toolprune", "untool")):
        outs = []
        for name in (new, old):
            assert cli.main(["--cwd", str(work), "--host", "claude", name]) == 0
            outs.append(capsys.readouterr().out)
        assert outs[0] == outs[1] and outs[0]
        body = {}
        for name in (new, old):
            text = open(os.path.join(PLUGIN, "slash-commands", f"{name}.md"), encoding="utf-8").read()
            body[name] = re.search(r'"\$te" --cwd "\$\(pwd\)" (\w+)', text).group(1)
        assert body[new] == body[old] == new


def test_toolprune_note_after_refresh_catalog(tmp_path, monkeypatch, capsys):
    """Right after `refresh-catalog`, the repository has not folded a session with the catalog
    yet; the note says so instead of asking for the command just run."""
    from tokeneconomizer.plugin import cli
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    assert cli.main(["--cwd", str(work), "--host", "claude", "toolprune"]) == 0
    assert "without `te --host claude toolprune refresh-catalog`" in capsys.readouterr().out
    (home / "hosts" / "claude").mkdir(parents=True)
    (home / "hosts" / "claude" / "catalog.json").write_text("{}", encoding="utf-8")
    assert cli.main(["--cwd", str(work), "--host", "claude", "toolprune"]) == 0
    out = capsys.readouterr().out
    assert "joins this repository's evidence from its next session" in out
    assert "without `te --host" not in out


def test_codex_sees_no_claude_format_commands_folder():
    """Codex turns a plugin's `commands/` folder into skills of its own, which would list every
    command twice beside `codex-skills/`; Claude Code and Copilot read `slash-commands/` instead."""
    assert not os.path.exists(os.path.join(PLUGIN, "commands"))
    for manifest in (".claude-plugin/plugin.json", ".github/plugin/plugin.json"):
        with open(os.path.join(PLUGIN, manifest), encoding="utf-8") as fh:
            assert json.load(fh)["commands"] == "./slash-commands/"


def _shipped_python():
    for top in ("tokeneconomizer", os.path.join("plugin", "scripts"), "scripts"):
        for root, _dirs, files in os.walk(os.path.join(REPO, top)):
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def test_text_io_names_its_encoding():
    """The agents' payloads, session logs and output are UTF-8 whatever the locale, and Python
    decodes a file or a pipe opened without an encoding with the locale's code page -- cp1251 on
    a Russian Windows, cp1252 on a US one -- which garbles a non-ASCII path and stops a fold at
    the first undecodable byte. So every text-mode `open()`, `read_text`/`write_text` and text
    subprocess call in the shipped code names its encoding."""
    import ast
    missing = []
    for path in _shipped_python():
        for call in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if not isinstance(call, ast.Call):
                continue
            f, kw = call.func, {k.arg: k.value for k in call.keywords}
            if isinstance(f, ast.Name) and f.id == "open":
                mode = call.args[1] if len(call.args) > 1 else kw.get("mode")
                text = not (isinstance(mode, ast.Constant) and "b" in mode.value)
            elif isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
                text = True
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and f.value.id == "subprocess"):
                text = "text" in kw or "universal_newlines" in kw
            else:
                continue
            if text and "encoding" not in kw:
                missing.append(f"{os.path.relpath(path, REPO)}:{call.lineno}")
    assert not missing, missing
