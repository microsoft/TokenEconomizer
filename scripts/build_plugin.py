#!/usr/bin/env python3
"""Assemble a self-contained TokenEconomizer plugin bundle and a marketplace around it.

    python scripts/build_plugin.py [--out dist/marketplace] [--zip]
    python scripts/build_plugin.py --droid [--out dist/tokeneconomizer-droid]

Why a build step at all. Loaded in place -- `claude --plugin-dir plugin/`, or this repository
added as a local-directory marketplace -- the plugin finds the core package one level up, in the
checkout itself. A marketplace INSTALL is different: Claude Code copies only the plugin directory
into its cache ("files outside the plugin directory aren't copied"), and Codex does the same, so
an installed copy of `plugin/` alone has no `tokeneconomizer` to import. The bundle fixes that by
copying the package (`tokeneconomizer/`, the mechanisms and the plugin runtime) into
`<plugin>/lib/`, which `scripts/te_bootstrap.py` looks in first.

The package is stdlib-only, so copying it is the whole dependency story: the bundle runs on any
Python >= 3.10 with nothing installed. `experiments/`, `scripts/` and the docs are not included.

Layout written to --out (a marketplace root you can `marketplace add`, publish as a branch or a
repository of its own, or zip for `claude --plugin-url`):

    <out>/.claude-plugin/marketplace.json
    <out>/tokeneconomizer/              the plugin, with lib/tokeneconomizer copied in
"""
import argparse
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")


def build(out):
    out = os.path.abspath(out)
    plugin_out = os.path.join(out, "tokeneconomizer")
    if os.path.exists(plugin_out):
        shutil.rmtree(plugin_out)
    # plugin/lib holds a symlink to the package for an install from a checkout (see
    # te_bootstrap); the bundle replaces it with a real copy.
    shutil.copytree(os.path.join(REPO, "plugin"), plugin_out,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "lib"))
    shutil.copytree(os.path.join(REPO, "tokeneconomizer"),
                    os.path.join(plugin_out, "lib", "tokeneconomizer"), ignore=IGNORE)
    with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as fh:
        market = json.load(fh)
    for entry in market.get("plugins", []):
        if entry.get("name") == "tokeneconomizer":
            entry["source"] = "./tokeneconomizer"
    os.makedirs(os.path.join(out, ".claude-plugin"), exist_ok=True)
    with open(os.path.join(out, ".claude-plugin", "marketplace.json"), "w",
              encoding="utf-8") as fh:
        json.dump(market, fh, indent=2)
        fh.write("\n")
    return plugin_out


def build_droid(out):
    """The same bundle laid out as a native Factory Droid plugin and marketplace.

    Droid loads `hooks/hooks.json` in its own shape (events at the top level) and needs a
    `.factory-plugin/plugin.json` to treat the directory as a native plugin; its slash commands
    live in `commands/`, not Claude Code's `slash-commands/`. The upstream files stay as they
    are: only the bundle is rewritten."""
    out = os.path.abspath(out)
    plugin_out = os.path.join(out, "tokeneconomizer")
    if os.path.exists(plugin_out):
        shutil.rmtree(plugin_out)
    # Only what Droid loads: the other agents' manifests and payloads would make the same
    # directory look like a Claude, Codex or Pi plugin too, and Droid reads those layouts.
    shutil.copytree(os.path.join(REPO, "plugin"), plugin_out,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".pytest_cache", "lib", ".DS_Store",
                        ".claude-plugin", ".codex-plugin", ".cursor-plugin", ".github",
                        "codex-skills", "hermes", "opencode", "pi", "slash-commands",
                        "package.json", "*.md.bak"))
    shutil.copytree(os.path.join(REPO, "tokeneconomizer"),
                    os.path.join(plugin_out, "lib", "tokeneconomizer"), ignore=IGNORE)
    # Droid's hook file: top-level events, `--host droid`, no PostToolUseFailure (Droid has no
    # such event).
    shutil.copyfile(os.path.join(REPO, "plugin", "hooks", "droid-hooks.json"),
                    os.path.join(plugin_out, "hooks", "hooks.json"))
    for name in ("droid-hooks.json", "codex-hooks.json", "copilot-hooks.json",
                 "cursor-hooks.json"):        # other agents' hook files: Droid reads one
        stale = os.path.join(plugin_out, "hooks", name)
        if os.path.exists(stale):
            os.remove(stale)
    # Slash commands become Droid commands. Droid has an Execute tool, not Claude's Bash, and
    # expands ${DROID_PLUGIN_ROOT}.
    commands_out = os.path.join(plugin_out, "commands")
    os.makedirs(commands_out, exist_ok=True)
    for name in sorted(os.listdir(os.path.join(REPO, "plugin", "slash-commands"))):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(REPO, "plugin", "slash-commands", name),
                    encoding="utf-8").read()
        text = text.replace("${CLAUDE_PLUGIN_ROOT}", "${DROID_PLUGIN_ROOT}")
        text = text.replace("with the Bash tool", "with the Execute tool")
        # Droid command names come straight from the filename and are not namespaced by plugin,
        # so `status.md` would become `/status` next to Droid's own commands.
        with open(os.path.join(commands_out, f"tokeneconomizer-{name}"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
    os.makedirs(os.path.join(out, ".factory-plugin"), exist_ok=True)
    with open(os.path.join(out, ".factory-plugin", "marketplace.json"), "w",
              encoding="utf-8") as fh:
        json.dump({
            "name": "tokeneconomizer-droid",
            "description": "TokenEconomizer for Factory Droid: learned repository facts, priced "
                           "sessions, and a failure-loop guard",
            "owner": {"name": "TokenEconomizer authors"},
            "plugins": [{"name": "tokeneconomizer", "source": "./tokeneconomizer",
                         "description": "Micro-skills injected at session start, session cost "
                                        "from Droid's own usage, and a guard for turns stuck in "
                                        "a failing loop.",
                         "category": "productivity"}],
        }, fh, indent=2)
        fh.write("\n")
    return plugin_out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(REPO, "dist", "marketplace"))
    ap.add_argument("--droid", action="store_true",
                    help="write a Factory Droid marketplace instead "
                         "(default <out>: dist/tokeneconomizer-droid)")
    ap.add_argument("--zip", action="store_true",
                    help="also write <out>/../tokeneconomizer-plugin.zip of the plugin directory")
    args = ap.parse_args(argv)
    if args.droid and args.out == os.path.join(REPO, "dist", "marketplace"):
        args.out = os.path.join(REPO, "dist", "tokeneconomizer-droid")
    plugin_out = build_droid(args.out) if args.droid else build(args.out)
    print(f"plugin bundle: {plugin_out}")
    print(f"marketplace:   {os.path.abspath(args.out)}")
    if args.zip:
        base = os.path.join(os.path.dirname(os.path.abspath(args.out)), "tokeneconomizer-plugin")
        path = shutil.make_archive(base, "zip", root_dir=os.path.dirname(plugin_out),
                                   base_dir=os.path.basename(plugin_out))
        print(f"zip:           {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
