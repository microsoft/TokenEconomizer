"""scripts/dead_code.py, the after-merge dead-code sweep, on small git repositories built here.

Each fixture is a throwaway repository with the scanned layout (tokeneconomizer/, scripts/, ...)
and its files tracked, since the script reads only what `git ls-files` lists.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "dead_code.py")

_spec = importlib.util.spec_from_file_location("dead_code", SCRIPT)
dead_code = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dead_code)


def make_repo(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return str(tmp_path)


def found(root):
    return {(i["path"], i["name"]): i["status"] for i in dead_code.scan(root)}


MODULE = """\
    import json

    LIVE = 1
    DEAD_CONST = 2
    CALLED_CONST = json.dumps({})


    def live():
        return LIVE


    # A comment that belongs to the function below.
    def dead():
        return 1


    def tested():
        return 2


    @some_registry.register
    def hook():
        return 3


    def __getattr__(name):
        raise AttributeError(name)


    class Box(json.JSONEncoder):
        def default(self, o):
            return live()

        def unused_method(self):
            return 4

        def kept(self):
            return 5


    class Lonely:
        def only_dead(self):
            return 6


    print(Box().kept(), Lonely, CALLED_CONST)
    """


def test_unreferenced_and_test_only(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/mod.py": MODULE,
        "tests/test_mod.py": "from tokeneconomizer.mod import tested\n",
    })
    got = found(root)
    path = "tokeneconomizer/mod.py"
    assert got[(path, "dead")] == "unreferenced"
    assert got[(path, "DEAD_CONST")] == "unreferenced"
    assert got[(path, "Box.unused_method")] == "unreferenced"
    assert got[(path, "Lonely.only_dead")] == "unreferenced"
    assert got[(path, "tested")] == "test-only"
    # Used, decorated, a dunder, and an override of a standard-library base.
    for name in ("live", "LIVE", "hook", "__getattr__", "Box.default", "Box", "Box.kept"):
        assert (path, name) not in got


def test_a_name_in_any_text_file_counts_as_used(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/mod.py": "def from_docs():\n    pass\n",
        "docs/NOTES.md": "Call `from_docs` by hand.\n",
    })
    assert found(root) == {}


def test_allowlist_and_all(tmp_path):
    root = make_repo(tmp_path, {
        "plugin/host/__init__.py": "__all__ = ['exported']\n\n"
                                   "def exported():\n    pass\n\n"
                                   "def register(ctx):\n    pass\n\n"
                                   "def other():\n    pass\n",
        "scripts/dead_code_allowlist.txt": "# The host calls it by name.\n"
                                           "plugin/host/__init__.py::register\n",
    })
    assert found(root) == {("plugin/host/__init__.py", "other"): "unreferenced"}


def test_experiments_tests_are_not_scanned_but_count_as_tests(tmp_path):
    root = make_repo(tmp_path, {
        "experiments/te_bench/a.py": "def helper():\n    pass\n",
        "experiments/tests/test_a.py": "def unused_in_test():\n    pass\nhelper\n",
    })
    assert found(root) == {("experiments/te_bench/a.py", "helper"): "test-only"}


def test_fix_deletes_only_unreferenced_and_keeps_files_parseable(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/mod.py": MODULE,
        "tests/test_mod.py": "from tokeneconomizer.mod import tested\n",
    })
    items = dead_code.fix(root, dead_code.scan(root))
    text = (tmp_path / "tokeneconomizer" / "mod.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "def dead" not in text and "belongs to the function below" not in text
    assert "DEAD_CONST" not in text and "unused_method" not in text
    assert "def tested" in text and "def live" in text and "def hook" in text
    # A call in the value may matter for its effect; the last method keeps its class non-empty.
    assert "CALLED_CONST = json.dumps" in text
    assert "def only_dead" in text
    assert "\n\n\n\n" not in text
    removed = {i["name"] for i in items if i.get("removed")}
    assert removed == {"dead", "DEAD_CONST", "Box.unused_method"}
    # A second pass finds nothing more to delete.
    again = dead_code.fix(root, dead_code.scan(root))
    assert not any(i.get("removed") for i in again)


def test_cli_markdown_and_json(tmp_path):
    root = make_repo(tmp_path, {"scripts/tool.py": "def orphan():\n    pass\n"})
    md = subprocess.run([sys.executable, SCRIPT, "--root", root], check=True,
                        capture_output=True, text=True).stdout
    assert "`scripts/tool.py:1` function `orphan`" in md
    data = json.loads(subprocess.run([sys.executable, SCRIPT, "--root", root, "--json"],
                                     check=True, capture_output=True, text=True).stdout)
    assert data["counts"] == {"function/unreferenced": 1}
    assert (tmp_path / "scripts" / "tool.py").read_text() == "def orphan():\n    pass\n"


def test_runs_on_this_repository_without_changing_it():
    before = subprocess.run(["git", "-C", REPO, "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    out = subprocess.run([sys.executable, SCRIPT, "--json"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert all(i["status"] in ("unreferenced", "test-only") for i in data["items"])
    assert not any(i["name"] == "register" for i in data["items"])
    after = subprocess.run(["git", "-C", REPO, "status", "--porcelain"],
                           capture_output=True, text=True).stdout
    assert before == after


def test_prefix_dispatch_and_getattr_dispatch_count_as_overrides(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/serve.py": """\
            import cmd
            from http.server import BaseHTTPRequestHandler


            class Handler(BaseHTTPRequestHandler):
                def do_PATCH(self):
                    pass

                def stray(self):
                    pass


            class Shell(cmd.Cmd):
                def do_greet(self, arg):
                    pass

                def help_greet(self):
                    pass


            class Dispatch:
                def handle(self, kind):
                    return getattr(self, "on_" + kind)()

                def on_start(self):
                    pass


            print(Handler, Shell, Dispatch().handle)
            """,
    })
    assert found(root) == {("tokeneconomizer/serve.py", "Handler.stray"): "unreferenced"}


def test_dotted_imports_guarded_imports_and_scanned_bases_resolve(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/logs.py": """\
            import logging.handlers
            try:
                from socketserver import BaseRequestHandler
            except ImportError:
                BaseRequestHandler = object


            class Rotating(logging.handlers.RotatingFileHandler):
                def shouldRollover(self, record):
                    return False


            class Req(BaseRequestHandler):
                def setup(self):
                    pass


            print(Rotating, Req)
            """,
        "tokeneconomizer/enc.py": """\
            import json


            class Enc(json.JSONEncoder):
                pass


            print(Enc)
            """,
        "tokeneconomizer/enc2.py": """\
            from tokeneconomizer.enc import Enc


            class Enc2(Enc):
                def default(self, o):
                    return str(o)

                def orphaned(self):
                    pass


            class Enc3(Enc2):
                def default(self, o):
                    return 1


            print(Enc2, Enc3)
            """,
    })
    assert found(root) == {("tokeneconomizer/enc2.py", "Enc2.orphaned"): "unreferenced"}


def test_fix_leaves_no_gap_under_a_class_header(tmp_path):
    root = make_repo(tmp_path, {
        "tokeneconomizer/mod.py": """\
            class Box:

                def gone(self):
                    pass

                def kept(self):
                    pass


            Box().kept()
            """,
    })
    dead_code.fix(root, dead_code.scan(root))
    text = (tmp_path / "tokeneconomizer" / "mod.py").read_text(encoding="utf-8")
    assert text.startswith("class Box:\n    def kept(self):\n")


def test_cli_fix_repeats_until_nothing_more_goes(tmp_path):
    root = make_repo(tmp_path, {
        "scripts/tool.py": """\
            HELPER_LIMIT = 3


            def helper():
                return HELPER_LIMIT


            def orphan():
                return helper()


            def main():
                pass


            main()
            """,
    })
    out = subprocess.run([sys.executable, SCRIPT, "--root", root, "--fix", "--json"],
                         check=True, capture_output=True, text=True).stdout
    removed = {i["name"] for i in json.loads(out)["items"] if i["removed"]}
    assert removed == {"orphan", "helper", "HELPER_LIMIT"}
    text = (tmp_path / "scripts" / "tool.py").read_text(encoding="utf-8")
    assert text == "def main():\n    pass\n\n\nmain()\n"
