#!/usr/bin/env python3
"""List definitions nothing else in the repository names, and optionally delete them.

    python scripts/dead_code.py [--json] [--fix] [--root DIR]

Scans the Python under tokeneconomizer/, experiments/ (not experiments/tests), plugin/ and
scripts/ for top-level functions, classes, methods of top-level classes and module-level
constants (UPPER_CASE names), and sorts each into:

  unreferenced  its name appears as a whole word nowhere in the tracked text files but its own
                definition
  test-only     it appears elsewhere, but only in test files

The check is a word search, not name resolution, and that is deliberate: a name that also turns
up in a doc, a JS file, a manifest or a string counts as used, so what is reported is close to
certainly dead, at the cost of missing names that happen to be common words. It skips dunders,
names in a module's __all__, definitions with a decorator that may register them by name, methods
that override a standard-library base (directly or through scanned classes, `do_*` on an HTTP
handler and the like included), methods of a class that calls getattr(self, <a name built at
run time>), and every `path::name` in scripts/dead_code_allowlist.txt.

--fix deletes the unreferenced definitions only, with their decorators and the comment block
directly above, and leaves test-only ones in the report for a person to decide. It repeats the
scan until nothing more goes, so what only served a deleted definition goes in the same run (a
removed item's line is where it stood in the pass that removed it). It keeps a file
unchanged when the result would not parse, never empties a class, and does not delete a
constant whose value calls something, since that call may matter for its effect. The report is
written to stdout; the exit code is 0 unless the arguments are wrong.
"""
import argparse
import ast
import cmd
import http.server
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter

ROOTS = ("tokeneconomizer/", "experiments/", "plugin/", "scripts/")
EXCLUDED = ("experiments/tests/",)
ALLOWLIST = "scripts/dead_code_allowlist.txt"
UNREFERENCED = "unreferenced"
TEST_ONLY = "test-only"
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CONSTANT = re.compile(r"_?[A-Z][A-Z0-9_]*")
# Decorators that only change how a definition is called, never register it somewhere under
# its name; any other decorator might (a hook table, a CLI, pytest), so the definition is skipped.
PLAIN_DECORATORS = {
    "staticmethod", "classmethod", "property", "cached_property", "lru_cache", "cache",
    "wraps", "dataclass", "total_ordering", "abstractmethod", "contextmanager",
}
# ast.NodeVisitor dispatches on `visit_<NodeName>` built at run time, so no such name is ever
# written out anywhere.
VISITOR_BASES = {"NodeVisitor", "NodeTransformer"}
# Standard-library bases that dispatch the same way, on a prefix and a name only known at run
# time (`do_` + the HTTP verb, `do_` + the typed command), so the base has no such attribute.
DISPATCH_PREFIXES = (
    (ast.NodeVisitor, ("visit_",)),
    (http.server.BaseHTTPRequestHandler, ("do_",)),
    (cmd.Cmd, ("do_", "help_", "complete_")),
)


def is_test_path(path):
    base = os.path.basename(path)
    return (path.startswith(("tests/", "experiments/tests/")) or "/tests/" in path
            or base.startswith("test_") or base == "conftest.py")


def tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True,
                         capture_output=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def read_text(root, path):
    full = os.path.join(root, path)
    # A symlink (plugin/lib links the package) would count every word of its target twice.
    if os.path.islink(full) or not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def load_allowlist(root):
    allowed = set()
    text = read_text(root, ALLOWLIST)
    for line in (text or "").splitlines():
        entry = line.split("#", 1)[0].strip()
        if "::" in entry:
            allowed.add(entry)
    return allowed


def _decorator_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _registers(node):
    return any(_decorator_name(d) not in PLAIN_DECORATORS for d in node.decorator_list)


def _stdlib_imports(tree):
    """Map each name the module imports to the dotted path it came from."""
    names = {}
    # Every import, not only the top-level ones: a base imported under `try:` or
    # `if TYPE_CHECKING:` is still a base. ast.walk visits the top level first, and the first
    # binding of a name is kept.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names.setdefault(a.asname, a.name)
                else:
                    names.setdefault(a.name.split(".")[0], a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                names.setdefault(a.asname or a.name, node.module + ":" + a.name)
    return names


def _resolve_base(expr, imports):
    """The standard-library class a base expression names, or None."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id not in imports:
        return None
    module, _, attr = imports[expr.id].partition(":")
    if module.split(".")[0] not in sys.stdlib_module_names:
        return None
    dotted = module.split(".") + ([attr] if attr else []) + parts[::-1]
    # Import the longest prefix that is a module, then walk the rest as attributes: a submodule
    # (`logging.handlers.X` after `import logging.handlers`, or `from x import submodule`) is
    # an attribute of its package only once something has imported it.
    for cut in range(len(dotted), len(module.split(".")) - 1, -1):
        try:
            obj = importlib.import_module(".".join(dotted[:cut]))
            for name in dotted[cut:]:
                obj = getattr(obj, name)
        except (ImportError, AttributeError):
            continue
        return obj if isinstance(obj, type) else None
    return None


def _base_name(expr):
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return ""


def _ancestors(cls, imports, classes, seen=None):
    """Yield (class node, its standard-library bases) for cls and the scanned classes above it.

    A base that is not a standard-library class is followed into every scanned class of that
    name, in any module, so `class B(A)` with `class A(json.JSONEncoder)` elsewhere still counts
    as a JSONEncoder. A name shared by unrelated classes only protects more, never less.
    """
    seen = set() if seen is None else seen
    if id(cls) in seen:
        return
    seen.add(id(cls))
    stdlib = []
    for base in cls.bases:
        resolved = _resolve_base(base, imports)
        if resolved is not None:
            stdlib.append(resolved)
            continue
        for other, other_imports in classes.get(_base_name(base), ()):
            yield from _ancestors(other, other_imports, classes, seen)
    yield cls, stdlib


def _dispatches_by_name(cls):
    """Whether the class looks a method up by a name built at run time: getattr(self, ...)."""
    for node in ast.walk(cls):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id in ("self", "cls")
                and not isinstance(node.args[1], ast.Constant)):
            return True
    return False


def _overrides(cls, name, imports, classes):
    """Whether something may call the method by a name nobody writes out."""
    for node, stdlib in _ancestors(cls, imports, classes):
        if _dispatches_by_name(node):
            return True
        for base in stdlib:
            if hasattr(base, name):
                return True
            if any(issubclass(base, b) and name.startswith(prefixes)
                   for b, prefixes in DISPATCH_PREFIXES):
                return True
        if (any(_base_name(b) in VISITOR_BASES for b in node.bases)
                and name.startswith("visit_")):
            return True
    return False


def class_index(trees):
    """Map each class name to the (node, imports) of every scanned class of that name."""
    classes = {}
    for tree in trees:
        imports = _stdlib_imports(tree)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((node, imports))
    return classes


def _all_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                for elt in ast.walk(node.value) if node.value else ():
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        names.add(elt.value)
    return names


def _calls(node):
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def definitions(path, source, classes=None):
    """Yield (kind, qualname, name, node, class_node) for each candidate in one module.

    source is the text or its parsed tree. classes is class_index() over every scanned module,
    for following a base defined in another one; without it only this module's are followed.
    """
    tree = source if isinstance(source, ast.Module) else ast.parse(source, filename=path)
    if classes is None:
        classes = class_index([tree])
    exported = _all_names(tree)
    imports = _stdlib_imports(tree)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name not in exported and not _registers(node):
                yield "function", node.name, node.name, node, None
        elif isinstance(node, ast.ClassDef):
            if node.name not in exported and not _registers(node):
                yield "class", node.name, node.name, node, None
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not _registers(item) and not _overrides(node, item.name, imports, classes)):
                    yield "method", node.name + "." + item.name, item.name, item, node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            # `A = B = 1` and tuple targets are left alone: deleting one name would take others.
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                name = targets[0].id
                if CONSTANT.fullmatch(name) and name not in exported:
                    yield "constant", name, name, node, None


def scan(root):
    files = tracked_files(root)
    allowed = load_allowlist(root)
    used, used_in_tests = Counter(), Counter()
    sources = {}
    for path in files:
        if path == ALLOWLIST:
            continue
        text = read_text(root, path)
        if text is None:
            continue
        words = Counter(WORD.findall(text))
        (used_in_tests if is_test_path(path) else used).update(words)
        if (path.endswith(".py") and path.startswith(ROOTS) and not path.startswith(EXCLUDED)
                and not is_test_path(path)):
            sources[path] = text
    trees = {}
    for path, source in sorted(sources.items()):
        try:
            trees[path] = ast.parse(source, filename=path)
        except SyntaxError:
            continue
    classes = class_index(trees.values())
    items = []
    for path, tree in trees.items():
        found = list(definitions(path, tree, classes))
        for kind, qualname, name, node, cls in found:
            if name.startswith("__") and name.endswith("__"):
                continue
            if path + "::" + qualname in allowed or path + "::" + name in allowed:
                continue
            # The definition itself is one occurrence of the name.
            elsewhere = used[name] - 1
            if elsewhere > 0:
                continue
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            items.append({
                "path": path, "name": qualname, "kind": kind, "line": start,
                "status": UNREFERENCED if used_in_tests[name] == 0 else TEST_ONLY,
                "removable": not (kind == "constant" and _calls(node.value)),
                "_span": (start, node.end_lineno),
                "_class": (cls.lineno, cls.end_lineno) if cls is not None else None,
            })
    return items


def _comment_start(lines, start):
    """Extend a 1-based start line up over the comment block directly above it."""
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    while start > 1:
        above = lines[start - 2]
        if above.strip().startswith("#") and len(above) - len(above.lstrip()) == indent:
            start -= 1
        else:
            break
    return start


def _class_would_empty(tree, cls_span, spans):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and (node.lineno, node.end_lineno) == cls_span:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            return all(any(s <= stmt.lineno and stmt.end_lineno <= e for s, e in spans)
                       for stmt in body)
    return False


def _cut(lines, start, end):
    """Remove lines start..end (1-based) and the blank lines that would double up around them."""
    before = 0
    while start - 2 - before >= 0 and not lines[start - 2 - before].strip():
        before += 1
    after = 0
    while end + after < len(lines) and not lines[end + after].strip():
        after += 1
    keep_after = max(before, after) - before if end + after < len(lines) else 0
    if start - 2 - before < 0 or lines[start - 2 - before].rstrip().endswith(":"):
        # The first statement of the file or of a block: the next one moves up to the top or
        # under the header, with no gap.
        start -= before
        keep_after = 0
    elif end + after >= len(lines):
        # At the end of the file: drop the blanks before it too, so the file ends on code.
        start -= before
    return lines[:start - 1] + lines[end + after - keep_after:]


def fix(root, items):
    """Delete the removable unreferenced items in place; mark each with whether it went."""
    by_path = {}
    for item in items:
        item["removed"] = False
        if item["status"] == UNREFERENCED and item["removable"]:
            by_path.setdefault(item["path"], []).append(item)
    for path, todo in by_path.items():
        full = os.path.join(root, path)
        with open(full, encoding="utf-8", newline="") as fh:
            source = fh.read()
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        # A method inside a class that goes too needs no cut of its own.
        classes = {i["_span"] for i in todo if i["kind"] == "class"}
        todo = [i for i in todo if i["_class"] not in classes]
        spans = [i["_span"] for i in todo]
        chosen = []
        for item in todo:
            if item["_class"] is not None and _class_would_empty(tree, item["_class"], spans):
                item["note"] = "kept: deleting it would empty its class"
                continue
            chosen.append(item)
        if not chosen:
            continue
        new = lines
        # Bottom-up, so the line numbers above each cut still hold.
        for item in sorted(chosen, key=lambda i: i["_span"][0], reverse=True):
            start = _comment_start(new, item["_span"][0])
            new = _cut(new, start, item["_span"][1])
        text = "".join(new)
        try:
            ast.parse(text, filename=path)
        except SyntaxError:
            for item in chosen:
                item["note"] = "kept: the file would not parse without it"
            continue
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        for item in chosen:
            item["removed"] = True
    return items


def sweep(root, max_passes=20):
    """Scan and fix until a pass removes nothing, so code that only served what went goes too.

    Returns the items every pass removed, followed by what the last scan still reports.
    """
    removed = []
    for _ in range(max_passes):
        items = fix(root, scan(root))
        gone = [i for i in items if i.get("removed")]
        if not gone:
            break
        removed += gone
    return removed + [i for i in items if not i.get("removed")]


def counts(items):
    table = Counter((i["kind"], i["status"]) for i in items)
    return {f"{k}/{s}": n for (k, s), n in sorted(table.items())}


def markdown(items, fixed):
    out = ["# Dead-code sweep", ""]
    if not items:
        out.append("Nothing unreferenced or test-only was found.")
        return "\n".join(out) + "\n"
    out += ["A name counts as used when it appears as a whole word in any tracked text file "
            "other than at its own definition. Allowlist false positives in "
            f"`{ALLOWLIST}`.", "",
            "| kind | unreferenced | test-only |", "|---|---|---|"]
    for kind in ("function", "class", "method", "constant"):
        u = sum(1 for i in items if i["kind"] == kind and i["status"] == UNREFERENCED)
        t = sum(1 for i in items if i["kind"] == kind and i["status"] == TEST_ONLY)
        out.append(f"| {kind} | {u} | {t} |")
    sections = [(UNREFERENCED, "Unreferenced"), (TEST_ONLY, "Test-only (left for a person)")]
    if fixed:
        removed = [i for i in items if i.get("removed")]
        out += ["", f"## Removed ({len(removed)})", ""]
        out += [f"- `{i['path']}:{i['line']}` {i['kind']} `{i['name']}`" for i in removed]
        items = [i for i in items if not i.get("removed")]
        sections[0] = (UNREFERENCED, "Unreferenced, not removed")
    for status, title in sections:
        group = [i for i in items if i["status"] == status]
        if not group:
            continue
        out += ["", f"## {title} ({len(group)})", ""]
        for i in group:
            why = []
            if status == UNREFERENCED and not i["removable"]:
                why.append("its value calls something")
            if i.get("note"):
                why.append(i["note"])
            tail = f" ({'; '.join(why)})" if why else ""
            out.append(f"- `{i['path']}:{i['line']}` {i['kind']} `{i['name']}`{tail}")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="the repository to scan (default: this one)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    ap.add_argument("--fix", action="store_true",
                    help="delete the unreferenced definitions (never the test-only ones)")
    args = ap.parse_args(argv)
    items = sweep(args.root) if args.fix else scan(args.root)
    public = [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]
    if args.json:
        json.dump({"counts": counts(public), "items": public}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(markdown(public, args.fix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
