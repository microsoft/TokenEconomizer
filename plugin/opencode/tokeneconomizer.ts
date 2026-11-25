/**
 * TokenEconomizer for opencode (anomalyco/opencode).
 *
 * opencode has no hook protocol -- plugins are functions that return hooks -- so this plugin
 * speaks the one the Claude Code, Codex and Pi integrations use, on opencode's behalf: every
 * event below becomes one call of the shared dispatcher (`../scripts/te-hook --host opencode`,
 * JSON in, JSON out), and the answer is applied with opencode's own levers. All of the
 * mechanism logic lives in the dispatcher; this file only translates.
 *
 *   event session.created        -> SessionStart       the learned manual, kept for the session
 *   chat.message                 -> UserPromptSubmit   opens a prompt-turn
 *   experimental.chat.system.transform                 appends the manual to the system prompt,
 *                                                      on every request of the session
 *   tool.execute.after           -> PostToolUse        the guard; a redirect is appended to the
 *                                                      tool result the model reads next, a
 *                                                      still-stuck notice is shown as a toast
 *   event session.idle           -> Stop               closes the turn; the session is exported as
 *                                                      JSON and its path sent along
 *   event session.deleted, a headless run's end, a session quiet for 10 minutes, or the
 *   process exiting              -> SessionEnd         hands the session to a detached fold. On
 *                                                      2.x `opencode run` and the TUI are the
 *                                                      same background process, so the plugin
 *                                                      does not guess which one it is: any
 *                                                      session quiet as long as TE's own
 *                                                      recovery treats it as over (fold.STALE_S)
 *                                                      is handed over, whether or not a later
 *                                                      session start ever arrives.
 *
 * The module's one export is the default object below: opencode 1.x reads its `server`, 2.x its
 * `setup`, and 1.x would call a second export as another plugin. The two entries map the same
 * dispatcher events: on 2.x that is `ctx.session.hook("prompt")` for UserPromptSubmit,
 * `ctx.session.hook("context")` for the manual's injection, `ctx.tool.hook("execute.after")`
 * for PostToolUse, and the public event stream (`ctx.event.subscribe()`) for the rest --
 * `session.execution.{succeeded,failed,interrupted}` ends a turn where those events exist, and
 * the documented `session.idle` ends it where they do not.
 *
 * Like the hooks on the other agents it fails open: a dispatcher that is missing, slow or
 * broken answers "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side calls)
 * or TE_DISABLE=1.
 */
import type { Plugin } from "@opencode-ai/plugin";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = (() => {
  try {
    return dirname(fileURLToPath(import.meta.url));
  } catch {
    return __dirname;
  }
})();
/**
 * The directory holding the dispatcher (`te_bootstrap.py` / `te-hook`). Where it sits depends on
 * how the plugin was put where opencode loads it from: the checkout keeps it at
 * `<root>/plugin/scripts`, an installed plugin at `<dir>/scripts`. Resolve rather than assume, so
 * a move of the install is not a move of the dispatcher -- and the hook fails open rather than
 * pointing at a path that is not there.
 *
 * Only the shipped layouts and the launcher the plugin writes itself are anchors. Nothing walks
 * up from the plugin's directory: on Windows that walk ends at `C:\`, where a standard user can
 * create folders by default, so a planted `scripts/te_bootstrap.py` would run in every hook --
 * and any unrelated ancestor would win over the dispatcher the plugin actually ships with.
 */
const findScripts = (): string => {
  const wanted = (p: string) => existsSync(join(p, "te_bootstrap.py"));
  // Named explicitly: `TE_PLUGIN_ROOT` is the package (`<root>/scripts`), `TE_CORE_PATH` the
  // checkout the bootstrap already documents (`<root>/plugin/scripts`). A fresh install has no
  // launcher yet, so these are what make the first hook work.
  const explicit: string[] = [];
  const pluginRoot = process.env.TE_PLUGIN_ROOT;
  if (pluginRoot) explicit.push(join(pluginRoot, "scripts"));
  const core = process.env.TE_CORE_PATH;
  if (core) explicit.push(join(core, "plugin", "scripts"));
  for (const c of explicit) {
    if (wanted(c)) return c;
  }
  // The layout the plugin ships in: `<plugin>/opencode/<file>` next to `<plugin>/scripts`.
  const shipped = join(HERE, "..", "scripts");
  if (wanted(shipped)) return shipped;
  const state = process.env.TOKENECONOMIZER_HOME || join(homedir(), ".tokeneconomizer");
  const launcher = join(state, "bin", "te");
  try {
    if (existsSync(launcher)) {
      // The launcher is `exec <plugin>/scripts/te "$@"`, so its own directory is the dispatcher's.
      const body = readFileSync(launcher, "utf8");
      const m = /exec\s+(.+?)\s+"\$@"/.exec(body);
      if (m) {
        const target = m[1].replace(/^['"]|['"]$/g, "");
        const scripts = dirname(target);
        if (wanted(scripts)) return scripts;
      }
    }
  } catch {
    /* an unreadable launcher is no anchor */
  }
  return shipped;
};
const SCRIPTS = findScripts();
const HOOK_TIMEOUT_MS = 60_000;

const truthy = (v: string | undefined) =>
  ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());
const disabled = () => truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE);

/** `opencode run` is headless: one prompt, then the process ends. */
const mode = () => (process.env.TE_OPENCODE_MODE ?? (process.argv.includes("run") ? "run" : "tui"));

/** The program and arguments that run a launcher. The launchers are POSIX `sh`, which Windows
 * cannot start; there the bootstrap they run is started with Python itself (`TE_PYTHON`, else
 * `python`). */
function launch(
  script: string,
  args: string[],
  platform: string = process.platform,
): [string, string[]] {
  if (platform !== "win32") return [join(SCRIPTS, script), args];
  const kind = script === "te-hook" ? "hook" : "cli";
  return [process.env.TE_PYTHON || "python", [join(SCRIPTS, "te_bootstrap.py"), kind, ...args]];
}

const childEnv = () => ({ ...process.env, PYTHONUTF8: "1", TE_OPENCODE_MODE: mode() });

/** Every child starts with `windowsHide`. opencode 2.x runs plugins in its background service,
 * which on Windows has no console, so each `python` the hooks start (one per prompt, tool call
 * and turn) was given a console window of its own: the terminal flickered while the agent
 * worked. Elsewhere the option does nothing. */
const HIDDEN = { windowsHide: true } as const;

/** One dispatcher call; `{}` (no change) on any failure. */
function hook(payload: Record<string, unknown>): Promise<any> {
  if (disabled()) return Promise.resolve({});
  return new Promise((done) => {
    let out = "";
    let child;
    try {
      const [program, argv] = launch("te-hook", ["--host", "opencode"]);
      child = spawn(program, argv, {
        ...HIDDEN,
        env: childEnv(),
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      done({});
      return;
    }
    const finish = () => {
      clearTimeout(timer);
      try {
        done(JSON.parse(out.trim().split("\n").pop() || "{}") ?? {});
      } catch {
        done({});
      }
    };
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      done({});
    }, HOOK_TIMEOUT_MS);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (d: string) => (out += d));
    child.on("error", () => {
      clearTimeout(timer);
      done({});
    });
    child.on("close", finish);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

/** The same call, blocking: for a process that is exiting and cannot wait on an event loop. */
function hookSync(payload: Record<string, unknown>) {
  if (disabled()) return;
  try {
    const [program, argv] = launch("te-hook", ["--host", "opencode"]);
    spawnSync(program, argv, {
      ...HIDDEN,
      env: childEnv(),
      input: JSON.stringify(payload),
      timeout: HOOK_TIMEOUT_MS,
      stdio: ["pipe", "ignore", "ignore"],
    });
  } catch {
    /* fail open */
  }
}

const stateRoot = () =>
  resolve(process.env.TOKENECONOMIZER_HOME || join(homedir(), ".tokeneconomizer"));

/** The session so far, as the JSON the fold reads (`trace/opencode.py`), from `opencode export`.
 * A child process rather than the plugin client: `opencode run` exits as soon as its turn goes
 * idle, without waiting for a plugin to finish, so only a blocking call is sure to complete. The
 * running binary is opencode itself (`process.execPath`), or `opencode` on PATH under Node. */
function exportSession(sessionID: string, cwd: string): string {
  const file = join(stateRoot(), "opencode-sessions", `${sessionID}.json`);
  try {
    const exe = /(^|[\\/])(node|bun)(\.exe)?$/i.test(process.execPath)
      ? "opencode"
      : process.execPath;
    const res = spawnSync(exe, ["export", sessionID], {
      ...HIDDEN,
      cwd,
      encoding: "utf8",
      timeout: HOOK_TIMEOUT_MS,
      maxBuffer: 256 * 1024 * 1024,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const json = res.status === 0 ? res.stdout : "";
    if (!json.trimStart().startsWith("{")) return "";
    mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
    writeFileSync(file, json, { mode: 0o600 });
    return file;
  } catch {
    return "";
  }
}

const TokenEconomizer: Plugin = async ({ client, directory }) => {
  const manuals = new Map<string, string>();
  const prompts = new Map<string, number>();
  const turns = new Map<string, string>();
  const open = new Set<string>();
  let current = "";

  const base = (sessionID: string, transcript = "") => ({
    session_id: sessionID,
    transcript_path: transcript,
    cwd: directory,
  });

  const notify = async (text: unknown) => {
    if (typeof text !== "string" || !text) return;
    try {
      if (mode() === "run") console.error(text);
      else await client.tui.showToast({ body: { message: text, variant: "info" } });
    } catch {
      console.error(text);
    }
  };

  const cleanup = (sessionID: string) => {
    manuals.delete(sessionID);
    prompts.delete(sessionID);
    turns.delete(sessionID);
    if (current === sessionID) current = "";
  };

  // Where the process ends without a `session.deleted` (quitting the TUI, `opencode run`
  // finishing), the sessions still open are handed over, once, synchronously.
  const onExit = () => {
    for (const sessionID of open) {
      const file = exportSession(sessionID, directory);
      cleanup(sessionID);
      hookSync({
        ...base(sessionID, file),
        hook_event_name: "SessionEnd",
        reason: "exit",
      });
    }
    open.clear();
  };

  const start = async (sessionID: string) => {
    if (open.has(sessionID)) return;
    if (open.size === 0) process.once("exit", onExit);
    open.add(sessionID);
    prompts.set(sessionID, 0);
    const out = await hook({
      ...base(sessionID),
      hook_event_name: "SessionStart",
      source: "startup",
    });
    manuals.set(sessionID, out?.hookSpecificOutput?.additionalContext ?? "");
    await notify(out?.systemMessage);
  };

  const end = async (sessionID: string, reason: string) => {
    if (!open.delete(sessionID)) return;
    if (open.size === 0) process.off("exit", onExit);
    const file = exportSession(sessionID, directory);
    cleanup(sessionID);
    await hook({ ...base(sessionID, file), hook_event_name: "SessionEnd", reason });
  };

  return {
    event: async ({ event }: { event: any }) => {
      const props = event?.properties ?? {};
      if (event?.type === "session.created") {
        const id = props.info?.id ?? props.sessionID;
        if (id && !props.info?.parentID) await start(id);
      } else if (event?.type === "session.idle") {
        const id = props.sessionID;
        if (!id) return;
        if (!open.has(id)) await start(id);
        // The session is exported at the end of every turn and its path sent with `Stop`: the
        // TUI gives a plugin no chance to hand a session over when it is quit, and a session
        // whose export has gone quiet is folded by the next session's start.
        const file = exportSession(id, directory);
        await hook({
          ...base(id, file),
          hook_event_name: "Stop",
          prompt_id: turns.get(id) ?? "",
          stop_hook_active: false,
        });
        // A headless run ends right after its one turn, so the session is handed over now.
        if (mode() === "run") await end(id, "run");
      } else if (event?.type === "session.deleted") {
        const id = props.info?.id ?? props.sessionID;
        if (id) await end(id, "deleted");
      }
    },

    "chat.message": async (input: any, output: any) => {
      const id: string = input?.sessionID;
      if (!id) return;
      current = id;
      if (!open.has(id)) await start(id);
      const n = (prompts.get(id) ?? 0) + 1;
      prompts.set(id, n);
      const turn = `${id}:${n}`;
      turns.set(id, turn);
      const prompt = (output?.parts ?? [])
        .filter((p: any) => p?.type === "text")
        .map((p: any) => p.text)
        .join("\n");
      const out = await hook({
        ...base(id),
        hook_event_name: "UserPromptSubmit",
        prompt_id: turn,
        prompt,
      });
      // The repository map arrives on a session's first prompt; it joins the manual for the rest
      // of the session, since the manual is added to every request.
      const extra = out?.hookSpecificOutput?.additionalContext;
      if (extra) manuals.set(id, manuals.get(id) ? `${manuals.get(id)}\n\n${extra}` : extra);
      await notify(out?.systemMessage);
    },

    "experimental.chat.system.transform": async (input: any, output: any) => {
      const manual = manuals.get(input?.sessionID ?? current);
      if (manual) output.system.push(manual);
    },

    "tool.execute.after": async (input: any, output: any) => {
      const id: string = input?.sessionID;
      if (!id) return;
      const meta = (output?.metadata ?? {}) as Record<string, unknown>;
      const exit = typeof meta.exit === "number" ? meta.exit : undefined;
      const text = String(output?.output ?? "");
      const out = await hook({
        ...base(id),
        hook_event_name: "PostToolUse",
        prompt_id: turns.get(id) ?? "",
        tool_name: input.tool,
        tool_use_id: input.callID,
        tool_input: input.args ?? {},
        tool_response: {
          content: text,
          isError: exit !== undefined && exit !== 0,
          exit_code: exit,
        },
      });
      const nudge = out?.hookSpecificOutput?.additionalContext;
      // The redirect rides on the result the model reads next: opencode has no mid-turn message.
      if (nudge) output.output = `${text}\n\n${nudge}`;
      await notify(out?.systemMessage);
    },
  };
};

/**
 * opencode 2.x (`Plugin.define({ id, setup })`, `@opencode/plugin`); `setup` below, on the same
 * default export as v1's `server`, so one file serves both. v2 has no `opencode export`, so this
 * entry records the session itself from its events -- prompts, tool calls and results, each
 * step's tokens and cost -- and writes the same JSON shape `trace/opencode.py` reads. Where that
 * copy holds no prompt, the fold reads 2.x's `opencode session export` instead.
 *
 * A turn's end is signalled two ways across 2.x releases: the `session.execution.*` events and
 * the documented `session.idle`. Each settles the session's open turn, and the first one to
 * arrive closes it, so either style alone keeps the guard and the fold fed. The subscription is
 * aborted from the cleanup function `setup` returns.
 */
const textOf = (content: any): string =>
  typeof content === "string"
    ? content
    : Array.isArray(content)
      ? content
          .map((c: any) => (typeof c === "string" ? c : c?.text ?? ""))
          .filter(Boolean)
          .join("\n")
      : "";

const deliverNudge = (e: any, ok: boolean, result: any, nudge: string): boolean => {
  if (ok && result && typeof result === "object") {
    if (typeof result.content === "string") {
      result.content = `${result.content}\n\n${nudge}`;
      return true;
    }
    if (Array.isArray(result.content)) {
      result.content = [...result.content, { type: "text", text: nudge }];
      return true;
    }
    if (result.content === undefined && typeof result.output === "string") {
      result.output = `${result.output}\n\n${nudge}`;
      return true;
    }
    if (result.content === undefined) {
      result.content = nudge;
      return true;
    }
    return false;
  }
  if (!ok && e && typeof e === "object") {
    if (e.error && typeof e.error === "object" && typeof e.error.message === "string") {
      try {
        const updated = e.error.message ? `${e.error.message}\n\n${nudge}` : nudge;
        e.error.message = updated;
        if (e.error.message === updated) return true;
      } catch {
        /* read-only error.message */
      }
    }
    if (typeof e.error === "string") {
      try {
        const updated = e.error ? `${e.error}\n\n${nudge}` : nudge;
        e.error = updated;
        if (e.error === updated) return true;
      } catch {
        /* read-only event.error */
      }
    }
  }
  return false;
};

const setupV2 = async (ctx: any) => {
  const directory: string = ctx.location?.directory ?? ctx.location?.path ?? process.cwd();
  const open = new Set<string>();
  const unsettled = new Set<string>();          // sessions whose turn awaits its end signal
  const manuals = new Map<string, string>();
  const prompts = new Map<string, number>();
  const turns = new Map<string, string>();
  const records = new Map<string, any[]>();       // sessionID -> messages, in the export's shape
  const steps = new Map<string, { sessionID: string; msg: any }>();
  const names = new Map<string, { sessionID: string; name: string }>();
  const pendingNudges = new Map<string, string>();

  const base = (sessionID: string, transcript = "") => ({
    session_id: sessionID,
    transcript_path: transcript,
    cwd: directory,
  });
  const notify = (text: unknown) => {
    if (typeof text === "string" && text) console.error(text);
  };

  /** A resumed session keeps the turns an earlier hand-over already wrote, so the export a fold
   * reads is never replaced by only the new epoch. */
  const loadExport = (sessionID: string): any[] => {
    const file = join(stateRoot(), "opencode-sessions", `${sessionID}.json`);
    if (!existsSync(file)) return [];
    try {
      const doc = JSON.parse(readFileSync(file, "utf8"));
      return Array.isArray(doc?.messages) ? doc.messages : [];
    } catch {
      return [];
    }
  };

  const writeExport = (sessionID: string): string => {
    const file = join(stateRoot(), "opencode-sessions", `${sessionID}.json`);
    // A copy of this plugin that saw none of the session's messages has nothing to add, and must
    // not replace what another copy wrote: on 2.2.5 every session's file came out empty (#118).
    // An empty one is still written where there is none, so the session is folded once quiet,
    // and the fold then reads opencode's own export.
    if (!records.get(sessionID)?.length && existsSync(file)) return file;
    try {
      mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
      writeFileSync(
        file,
        JSON.stringify({ info: { id: sessionID }, messages: records.get(sessionID) ?? [] }),
        { mode: 0o600 },
      );
      return file;
    } catch {
      return "";
    }
  };

  const cleanup = (id: string) => {
    unsettled.delete(id);
    manuals.delete(id);
    prompts.delete(id);
    turns.delete(id);
    records.delete(id);
    pendingNudges.delete(id);
    for (const [mid, entry] of steps) {
      if (entry.sessionID === id) steps.delete(mid);
    }
    for (const [cid, entry] of names) {
      if (entry.sessionID === id) names.delete(cid);
    }
  };

  const start = async (id: string) => {
    if (open.has(id)) return;
    open.add(id);
    records.set(id, loadExport(id));
    prompts.set(id, 0);
    const out = await hook({ ...base(id), hook_event_name: "SessionStart", source: "startup" });
    manuals.set(id, out?.hookSpecificOutput?.additionalContext ?? "");
    notify(out?.systemMessage);
  };
  /**
   * A session that has been quiet this long is over, however it was started. opencode 2.x runs
   * plugins in its background service, where `opencode run` and the TUI are the same process, so
   * `mode()` cannot tell them apart -- and a release may move which one holds the plugin again.
   * TE's own recovery already calls a session quiet for `fold.STALE_S` (600 s) over; this
   * enforces the same rule here, so the hand-over does not wait for a later session start and
   * does not depend on guessing the mode. Any activity resets the timer, and a turn still open
   * (a long tool call, a permission prompt waiting on the person) is not quiet: the session is
   * handed over only once its turn has ended.
   */
  const QUIET_MS = (() => {
    const raw = Number(process.env.TE_OPENCODE_QUIET_MS);
    return Number.isFinite(raw) && raw > 0 ? Math.max(raw, 10_000) : 600_000;
  })();
  const quiet = new Map<string, ReturnType<typeof setTimeout>>();
  const settled = (id: string) => {
    const timer = quiet.get(id);
    if (timer) clearTimeout(timer);
    quiet.delete(id);
  };
  const bumpQuiet = (id: string) => {
    if (!open.has(id)) return;
    settled(id);
    const timer = setTimeout(() => {
      quiet.delete(id);
      if (unsettled.has(id)) bumpQuiet(id);
      else if (open.has(id)) void end(id, "idle");
    }, QUIET_MS);
    (timer as unknown as { unref?: () => void }).unref?.();
    quiet.set(id, timer);
  };
  const end = async (id: string, reason: string) => {
    settled(id);
    if (!open.delete(id)) return;
    const file = writeExport(id);
    cleanup(id);
    await hook({ ...base(id, file), hook_event_name: "SessionEnd", reason });
  };
  /** A turn's end, however 2.x signals it. `session.execution.*` and `session.idle` both arrive
   * per turn on releases that emit both; the first settles the turn and the second is a no-op.
   * A turn the plugin never saw open (a plugin reload mid-turn) is closed by the next prompt's
   * hook in the dispatcher instead. */
  const closeTurn = async (id: string) => {
    if (!unsettled.delete(id)) return;
    pendingNudges.delete(id);
    await hook({
      ...base(id, writeExport(id)),
      hook_event_name: "Stop",
      prompt_id: turns.get(id) ?? "",
      stop_hook_active: false,
    });
    // Where the process really is a one-shot run, hand the session over now; everywhere else the
    // quiet timer does it, so the fold no longer depends on how the plugin was launched.
    if (mode() === "run") await end(id, "run");
    else bumpQuiet(id);
  };
  const onExit = () => {
    for (const id of open) {
      const file = writeExport(id);
      cleanup(id);
      hookSync({ ...base(id, file), hook_event_name: "SessionEnd", reason: "exit" });
    }
    open.clear();
  };
  process.once("exit", onExit);

  const assistant = (sessionID: string, messageID: string) => {
    let entry = steps.get(messageID);
    if (!entry) {
      const msg = { info: { id: messageID, role: "assistant", path: { cwd: directory } }, parts: [] };
      entry = { sessionID, msg };
      steps.set(messageID, entry);
      records.get(sessionID)?.push(msg);
    }
    return entry.msg;
  };
  const toolPart = (m: any, callID: string) => m.parts.find((p: any) => p.callID === callID);

  // Each registration is its own attempt: a 2.x release that lacks one hook still loads the rest.
  try {
    await ctx.session.hook("prompt", async (e: any) => {
      const id: string = e.sessionID;
      await start(id);
      pendingNudges.delete(id);
      const n = (prompts.get(id) ?? 0) + 1;
      prompts.set(id, n);
      const turn = `${id}:${n}`;
      turns.set(id, turn);
      unsettled.add(id);
      bumpQuiet(id);
      const text: string = e.prompt?.text ?? "";
      records.get(id)?.push({
        info: { id: String(e.messageID ?? turn), role: "user" },
        parts: [{ type: "text", text }],
      });
      const out = await hook({
        ...base(id),
        hook_event_name: "UserPromptSubmit",
        prompt_id: turn,
        prompt: text,
      });
      const extra = out?.hookSpecificOutput?.additionalContext;
      if (extra) manuals.set(id, manuals.get(id) ? `${manuals.get(id)}\n\n${extra}` : extra);
      notify(out?.systemMessage);
    });
  } catch { /* no session.hook("prompt") on this release */ }

  try {
    await ctx.session.hook("context", (e: any) => {
      const manual = manuals.get(e.sessionID);
      if (manual && Array.isArray(e.system)) e.system.push({ type: "text", text: manual });
    });
  } catch { /* no session.hook("context") on this release */ }

  try {
    await ctx.tool.hook("execute.after", async (e: any) => {
      const id: string = e.sessionID;
      bumpQuiet(id);
      const turnAtCall = turns.get(id) ?? "";
      const ok = e.status === "completed";
      const result = ok ? e.result : undefined;
      const text = ok
        ? textOf(result?.content) || String(result?.output ?? "")
        : String(e.error?.message ?? e.error ?? "");
      const exit = typeof result?.metadata?.exit === "number" ? result.metadata.exit : undefined;
      const out = await hook({
        ...base(id),
        hook_event_name: "PostToolUse",
        prompt_id: turnAtCall,
        tool_name: e.tool,
        tool_use_id: e.id,
        tool_input: e.input ?? {},
        tool_response: {
          content: text,
          isError: !ok || (exit !== undefined && exit !== 0),
          exit_code: exit,
        },
      });
      const held = pendingNudges.get(id);
      const nudge = out?.hookSpecificOutput?.additionalContext;
      const combined = [held, nudge]
        .filter((v): v is string => typeof v === "string" && Boolean(v))
        .join("\n\n");
      if (combined) {
        if (deliverNudge(e, ok, result, combined)) pendingNudges.delete(id);
        else if ((turns.get(id) ?? "") === turnAtCall) pendingNudges.set(id, combined);
      }
      notify(out?.systemMessage);
    });
  } catch { /* no tool.hook("execute.after") on this release */ }

  const controller = new AbortController();
  (async () => {
    try {
      for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
        const t: string = ev?.type;
        const p = ev?.properties ?? ev?.data ?? ev;
        if (!t?.startsWith("session.")) continue;
        const id: string = p?.sessionID;
        if (!id) continue;
        if (t === "session.step.started") {
          const m = assistant(id, p.assistantMessageID);
          m.info.modelID = p.model?.id ?? p.model?.modelID ?? m.info.modelID;
        } else if (t === "session.step.ended") {
          const m = assistant(id, p.assistantMessageID);
          m.info.cost = p.cost ?? 0;
          m.info.tokens = p.tokens;
        } else if (t === "session.tool.input.started") {
          names.set(p.id, { sessionID: id, name: p.name });
        } else if (t === "session.tool.called") {
          const m = assistant(id, p.assistantMessageID);
          const name = names.get(p.id)?.name ?? "";
          names.delete(p.id);
          m.parts.push({
            type: "tool",
            callID: p.id,
            tool: name,
            state: { status: "running", input: p.input ?? {} },
          });
        } else if (t === "session.tool.success" || t === "session.tool.failed") {
          const part = toolPart(assistant(id, p.assistantMessageID), p.id);
          if (part) {
            part.state = t === "session.tool.success"
              ? { status: "completed", input: part.state.input, output: textOf(p.content),
                  metadata: p.metadata ?? {} }
              : { status: "error", input: part.state.input,
                  error: String(p.error?.message ?? textOf(p.content) ?? "") };
          }
        } else if (/^session\.execution\.(succeeded|failed|interrupted)$/.test(t)) {
          await closeTurn(id);
        } else if (t === "session.idle") {
          await closeTurn(id);
        } else if (t === "session.deleted") {
          await end(id, "deleted");
        }
      }
    } catch (e) {
    }
  })();

  return () => {
    controller.abort();
    process.off("exit", onExit);
  };
};

// One default export for both: v1 reads `server`, v2 reads `setup`.
export default { id: "tokeneconomizer", server: TokenEconomizer, setup: setupV2 };
