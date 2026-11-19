"""Locate the `tokeneconomizer` package and run the hook dispatcher (`hook`) or the command line
(`cli`), both in `tokeneconomizer.plugin`.

The package is found, in order:

  1. `$TE_CORE_PATH`                  -- a checkout you point at explicitly;
  2. `<plugin root>/lib`              -- in the repository, a symlink to the package; Claude
                                         Code's install copies the plugin directory into its
                                         cache (even from a local marketplace) and follows it,
                                         so the installed copy is self-contained. In the bundle
                                         `scripts/build_plugin.py` writes, a real copy;
  3. `<plugin root>/..`               -- the repository this plugin directory lives in, for a
                                         checkout whose symlinks were not created (`--plugin-dir`);
  4. the marketplace it came from    -- Codex's install copies the plugin directory but drops
                                         the `lib` symlink; for a copy under
                                         `<CODEX_HOME>/plugins/cache/<marketplace>/...`, the
                                         checkout `config.toml` names as that marketplace's
                                         `source` (the clone a local marketplace was added
                                         from), or Codex's own checkout of a git marketplace,
                                         `<CODEX_HOME>/.tmp/marketplaces/<marketplace>`;
  5. a Copilot or VS Code copy       -- for a copy under `<COPILOT_HOME>/installed-plugins/...`
                                         or VS Code's `agentPlugins/...`, the marketplace
                                         checkouts under the Copilot home and the sources its
                                         `config.json`, `marketplaces.json` or VS Code's
                                         `installed.json` name;
  6. Claude Code's marketplace       -- the source `~/.claude/plugins/known_marketplaces.json`
                                         records for this marketplace, which another agent's
                                         copy can share;
  7. `<TOKENECONOMIZER_HOME>/src`     -- a clone kept in the state directory;
  8. an installed package            -- `pip install` of the repository.

When none has it, a hook answers `{}` as always, and says why once in the plugin's errors.log.

This file must stay importable by any Python 3 so that an old interpreter can still answer a
hook with `{}` instead of a syntax error: no f-strings, no annotations, nothing newer than 3.0.
"""
import os
import re
import sys

MIN_VERSION = (3, 10)

# `source = ...` in Codex's config.toml: a TOML basic string ("...", with escapes) or a literal
# one ('...', as written), which Codex writes for a path with backslashes (0.163.0, Windows).
_SOURCE = re.compile(r'''source\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')''')


def _toml_string(basic, literal):
    r"""A `source` value: a literal string as written; a basic string with its `\\` and `\"`
    undone, any other backslash kept, as a path pasted between double quotes has them."""
    if literal is not None:
        return literal
    return re.sub(r'\\([\\"])', r'\1', basic)


def _without_long_prefix(path):
    r"""`path` without the extended-length prefix Codex puts on a local source on Windows:
    `\\?\C:\src` is `C:\src`, and `\\?\UNC\server\share` is `\\server\share`."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _marketplace_sources(plugin_root):
    """For a Codex install copy (`<home>/plugins/cache/<marketplace>/<plugin>/<version>`), the
    `source` its `config.toml` records for that marketplace (a local one), and the checkout Codex
    keeps of a git one."""
    parts = plugin_root.rstrip(os.sep).split(os.sep)
    if len(parts) < 6 or parts[-4] != "cache" or parts[-5] != "plugins":
        return []
    home, market = os.sep.join(parts[:-5]), parts[-3]
    out = [os.path.join(home, ".tmp", "marketplaces", market)]
    # Claude Code keeps the same cache layout under `~/.claude/plugins/cache/`; on a checkout
    # without symlinks (Windows) the copy has no `lib`, so the package is found in the clone
    # its `known_marketplaces.json` records for this marketplace.
    try:
        import json
        known = json.load(open(os.path.join(home, "plugins", "known_marketplaces.json"),
                               encoding="utf-8"))
        entry = known.get(market) or {}
        source = entry.get("source") or {}
        for cand in (source.get("path"), entry.get("installLocation")):
            if cand:
                out.append(cand)
    except (IOError, OSError, ValueError, AttributeError):
        pass
    try:
        text = open(os.path.join(home, "config.toml"), encoding="utf-8", errors="replace").read()
    except (IOError, OSError):
        return out
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]").strip().replace('"', "").replace("'", "")
            continue
        m = _SOURCE.match(line)
        if m and section == "marketplaces." + market:
            source = _without_long_prefix(_toml_string(m.group(1), m.group(2)))
            if source:
                out.append(source)
    return out


def _state_home():
    return os.environ.get("TOKENECONOMIZER_HOME") or os.path.join(
        os.path.expanduser("~"), ".tokeneconomizer")


def _strip_jsonc(text):
    """Strip `//`, `/* ... */` comments and trailing commas outside JSON strings."""
    out = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                end = text.find("\n", i + 2)
                i = n if end == -1 else end
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                if end == -1:
                    break
                for c in text[i:end + 2]:
                    if c == "\n":
                        out.append("\n")
                i = end + 2
                continue
        if ch == ",":
            j = i + 1
            while j < n:
                if text[j] in " \t\r\n":
                    j += 1
                elif text[j:j + 2] == "//":
                    end = text.find("\n", j + 2)
                    j = n if end == -1 else end + 1
                elif text[j:j + 2] == "/*":
                    end = text.find("*/", j + 2)
                    j = n if end == -1 else end + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _json_load_relaxed(path):
    """Read a JSON/JSONC file (comments and trailing commas allowed outside strings)."""
    import json
    try:
        fh = open(path, encoding="utf-8", errors="replace")
        try:
            text = fh.read()
        finally:
            fh.close()
    except (IOError, OSError):
        return None
    try:
        return json.loads(_strip_jsonc(text), strict=False)
    except ValueError:
        return None


def _paths_from_entry(entry):
    """Extract string directory candidates from a marketplace registry entry."""
    out = []
    if isinstance(entry, str):
        out.append(entry)
    elif isinstance(entry, dict):
        src = entry.get("source")
        if isinstance(src, str):
            out.append(src)
        elif isinstance(src, dict):
            for key in ("path", "location", "root"):
                if isinstance(src.get(key), str):
                    out.append(src[key])
        for key in ("path", "installLocation", "location", "sourcePath", "uri"):
            val = entry.get(key)
            if isinstance(val, str):
                out.append(val)
    cleaned = []
    for cand in out:
        if cand.startswith("file:///"):
            from urllib.parse import unquote
            # `file:///C:/foo` -> `C:/foo`; `file:///home/foo` -> `/home/foo`
            tail = unquote(cand[8:])
            if len(tail) >= 2 and tail[1] == ":":
                cand = tail
            else:
                cand = "/" + tail
        cleaned.append(cand)
    return cleaned


def _known_marketplace_sources(plugins_dir, market):
    data = _json_load_relaxed(os.path.join(plugins_dir, "known_marketplaces.json"))
    if not isinstance(data, dict):
        return []
    out = []
    keys = [market] if market else []
    for k in data:
        if k not in keys and "tokeneconomizer" in str(k).lower():
            keys.append(k)
    for k in keys:
        out.extend(_paths_from_entry(data.get(k)))
    return out


def _copilot_sources(copilot_home, market):
    """Marketplace checkouts and registered local sources under a Copilot home directory."""
    out = []
    names = [market] if market and market != "_direct" else []
    if "tokeneconomizer" not in names:
        names.append("tokeneconomizer")
    for sub in ("marketplaces", ".marketplaces", os.path.join("plugins", "marketplaces"),
                "marketplace-cache"):
        base = os.path.join(copilot_home, sub)
        for name in names:
            out.append(os.path.join(base, name))
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for entry in entries:
            if "tokeneconomizer" in entry.lower():
                out.append(os.path.join(base, entry))
    for fname in ("config.json", "marketplaces.json", "known_marketplaces.json"):
        data = _json_load_relaxed(os.path.join(copilot_home, fname))
        if not isinstance(data, dict):
            continue
        for section in (data, data.get("marketplaces"), data.get("pluginMarketplaces")):
            if isinstance(section, dict):
                for name in names:
                    out.extend(_paths_from_entry(section.get(name)))
                for k, v in section.items():
                    if "tokeneconomizer" in str(k).lower():
                        out.extend(_paths_from_entry(v))
    return out


def _extra_sources(plugin_root):
    """Package locations outside Claude/Codex's cache layout: Copilot `installed-plugins`,
    VS Code's `agentPlugins` mirrors, cross-cache `~/.claude/plugins/known_marketplaces.json`
    (`omp`/Copilot on Windows), and `<TOKENECONOMIZER_HOME>/src`."""
    out = []
    norm = os.path.normpath(plugin_root).rstrip(os.sep)
    parts = norm.split(os.sep)
    market = None
    user_home = os.path.expanduser("~")

    if "installed-plugins" in parts:
        idx = parts.index("installed-plugins")
        copilot_home = os.sep.join(parts[:idx]) or os.sep
        if idx + 1 < len(parts):
            market = parts[idx + 1]
            out.extend(_copilot_sources(copilot_home, market))
            if market == "_direct" and idx + 2 < len(parts):
                out.extend(_copilot_sources(copilot_home, parts[idx + 2]))

    if "agentPlugins" in parts:
        ap_idx = parts.index("agentPlugins")
        ap_dir = os.sep.join(parts[:ap_idx + 1]) or os.sep
        for cfg_dir in (ap_dir, os.path.dirname(ap_dir)):
            data = _json_load_relaxed(os.path.join(cfg_dir, "installed.json"))
            if isinstance(data, dict):
                for item in data.values():
                    out.extend(_paths_from_entry(item))
            elif isinstance(data, list):
                for item in data:
                    out.extend(_paths_from_entry(item))
        user_copilot = os.path.join(user_home, ".copilot")
        out.extend(_copilot_sources(user_copilot, "tokeneconomizer"))
        inst_root = os.path.join(user_copilot, "installed-plugins")
        try:
            for sub in sorted(os.listdir(inst_root)):
                if os.path.isdir(os.path.join(inst_root, sub)):
                    out.extend(_copilot_sources(user_copilot, sub))
        except OSError:
            pass

    if not market and len(parts) >= 6 and parts[-4] == "cache" and parts[-5] == "plugins":
        market = parts[-3]
    out.extend(_known_marketplace_sources(os.path.join(user_home, ".claude", "plugins"), market))
    out.append(os.path.join(_state_home(), "src"))
    return out


def _symlink_stub(plugin_root):
    """Where `lib/tokeneconomizer` points when git checked the symlink out as a plain file
    holding its target (Windows without symlink support): the directory holding the package."""
    stub = os.path.join(plugin_root, "lib", "tokeneconomizer")
    try:
        if not os.path.isfile(stub) or os.path.getsize(stub) > 1024:
            return None
        target = open(stub, encoding="utf-8").read().strip()
    except (IOError, OSError):
        return None
    return os.path.dirname(os.path.normpath(os.path.join(plugin_root, "lib", target)))


def core_path():
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in ([os.environ.get("TE_CORE_PATH"), os.path.join(plugin_root, "lib"),
                  _symlink_stub(plugin_root), os.path.dirname(plugin_root)]
                 + _marketplace_sources(plugin_root)
                 + _extra_sources(plugin_root)):
        if cand and os.path.isfile(os.path.join(cand, "tokeneconomizer", "plugin", "hook.py")):
            return cand
    return None


def _say_missing(plugin_root):
    """One line in the plugin's errors.log (not repeated) when the core cannot be found."""
    home = _state_home()
    line = ("bootstrap: the tokeneconomizer package was not found for the plugin at %s; "
            "install from the bundle (scripts/build_plugin.py), clone into "
            "~/.tokeneconomizer/src, or set TE_CORE_PATH\n" % plugin_root)
    path = os.path.join(home, "errors.log")
    try:
        if os.path.exists(path) and line in open(path, encoding="utf-8", errors="replace").read():
            return
        if not os.path.isdir(home):
            os.makedirs(home)
        open(path, "a", encoding="utf-8").write(line)
    except (IOError, OSError):
        pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    rest = sys.argv[2:]
    if sys.version_info < MIN_VERSION:
        if mode == "hook":
            getattr(sys.stdin, "buffer", sys.stdin).read()     # bytes: no locale to decode with
            sys.stdout.write("{}\n")
            return 0
        sys.stderr.write("TokenEconomizer needs Python >= %d.%d (this is %s); set TE_PYTHON\n"
                         % (MIN_VERSION[0], MIN_VERSION[1], sys.version.split()[0]))
        return 1
    # Everything the plugin writes under its state root is derived from the agents' session
    # logs, which the agents keep private; so are these files.
    os.umask(0o077)
    # Where this plugin is, for the stable launcher the hooks keep (`paths.ensure_launcher`).
    os.environ["TE_PLUGIN_ROOT"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    core = core_path()
    if core:
        sys.path.insert(0, core)
    try:
        if mode == "hook":
            from tokeneconomizer.plugin import hook
            return hook.main(rest)
        from tokeneconomizer.plugin import cli
        return cli.main(rest)
    except ImportError:
        if core is None:
            _say_missing(os.environ["TE_PLUGIN_ROOT"])
            if mode != "hook":
                sys.stderr.write("TokenEconomizer: its core package was not found next to the "
                                 "plugin at %s (see ~/.tokeneconomizer/errors.log)\n"
                                 % os.environ["TE_PLUGIN_ROOT"])
                return 1
        if mode == "hook":
            sys.stdout.write("{}\n")
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
