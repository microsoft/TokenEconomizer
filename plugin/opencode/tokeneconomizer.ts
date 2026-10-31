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
 *   event session.deleted, a headless run's end, or the process exiting
 *                                -> SessionEnd         hands the session to a detached fold; a
 *                                                      TUI quit sends nothing, so the plugin's
 *                                                      session-recovery folds it at the next
 *                                                      start once its export has been quiet for
 *                                                      10 minutes
 *
 * The module's one export is the default object below: opencode 1.x reads its `server`, 2.x its
 * `setup`, and 1.x would call a second export as another plugin.
 *
 * Like the hooks on the other agents it fails open: a dispatcher that is missing, slow or
 * broken answers "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side calls)
 * or TE_DISABLE=1.
 */
import type { Plugin } from "@opencode-ai/plugin";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
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
const SCRIPTS = join(HERE, "..", "scripts");
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

  const start = async (sessionID: string) => {
    if (open.has(sessionID)) return;
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
    const file = exportSession(sessionID, directory);
    await hook({ ...base(sessionID, file), hook_event_name: "SessionEnd", reason });
  };

  // Where the process ends without a `session.deleted` (quitting the TUI, `opencode run`
  // finishing), the sessions still open are handed over, once, synchronously.
  process.once("exit", () => {
    for (const sessionID of open) {
      hookSync({
        ...base(sessionID, exportSession(sessionID, directory)),
        hook_event_name: "SessionEnd",
        reason: "exit",
      });
    }
    open.clear();
  });

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
 * step's tokens and cost -- and writes the same JSON shape `trace/opencode.py` reads.
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

const setupV2 = async (ctx: any) => {
  const directory: string = ctx.location?.directory ?? ctx.location?.path ?? process.cwd();
  const open = new Set<string>();
  const manuals = new Map<string, string>();
  const prompts = new Map<string, number>();
  const turns = new Map<string, string>();
  const records = new Map<string, any[]>();       // sessionID -> messages, in the export's shape
  const steps = new Map<string, any>();           // assistantMessageID -> assistant message
  const names = new Map<string, string>();        // tool call id -> tool name

  const base = (sessionID: string, transcript = "") => ({
    session_id: sessionID,
    transcript_path: transcript,
    cwd: directory,
  });
  const notify = (text: unknown) => {
    if (typeof text === "string" && text) console.error(text);
  };

  const writeExport = (sessionID: string): string => {
    const file = join(stateRoot(), "opencode-sessions", `${sessionID}.json`);
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

  const start = async (id: string) => {
    if (open.has(id)) return;
    open.add(id);
    records.set(id, []);
    prompts.set(id, 0);
    const out = await hook({ ...base(id), hook_event_name: "SessionStart", source: "startup" });
    manuals.set(id, out?.hookSpecificOutput?.additionalContext ?? "");
    notify(out?.systemMessage);
  };
  const end = async (id: string, reason: string) => {
    if (!open.delete(id)) return;
    await hook({ ...base(id, writeExport(id)), hook_event_name: "SessionEnd", reason });
  };
  process.once("exit", () => {
    for (const id of open)
      hookSync({ ...base(id, writeExport(id)), hook_event_name: "SessionEnd", reason: "exit" });
    open.clear();
  });

  const assistant = (sessionID: string, messageID: string) => {
    let m = steps.get(messageID);
    if (!m) {
      m = { info: { id: messageID, role: "assistant", path: { cwd: directory } }, parts: [] };
      steps.set(messageID, m);
      records.get(sessionID)?.push(m);
    }
    return m;
  };
  const toolPart = (m: any, callID: string) => m.parts.find((p: any) => p.callID === callID);

  await ctx.session.hook("prompt", async (e: any) => {
    const id: string = e.sessionID;
    await start(id);
    const n = (prompts.get(id) ?? 0) + 1;
    prompts.set(id, n);
    const turn = `${id}:${n}`;
    turns.set(id, turn);
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

  await ctx.session.hook("context", (e: any) => {
    const manual = manuals.get(e.sessionID);
    if (manual && Array.isArray(e.system)) e.system.push({ type: "text", text: manual });
  });

  await ctx.tool.hook("execute.after", async (e: any) => {
    const id: string = e.sessionID;
    const ok = e.status === "completed";
    const result = ok ? e.result : undefined;
    const text = ok
      ? textOf(result?.content) || String(result?.output ?? "")
      : String(e.error?.message ?? e.error ?? "");
    const exit = typeof result?.metadata?.exit === "number" ? result.metadata.exit : undefined;
    const out = await hook({
      ...base(id),
      hook_event_name: "PostToolUse",
      prompt_id: turns.get(id) ?? "",
      tool_name: e.tool,
      tool_use_id: e.id,
      tool_input: e.input ?? {},
      tool_response: {
        content: text,
        isError: !ok || (exit !== undefined && exit !== 0),
        exit_code: exit,
      },
    });
    const nudge = out?.hookSpecificOutput?.additionalContext;
    if (nudge && ok && result) {
      if (typeof result.content === "string") result.content = `${result.content}\n\n${nudge}`;
      else if (Array.isArray(result.content))
        result.content = [...result.content, { type: "text", text: nudge }];
      else result.content = nudge;
    }
    notify(out?.systemMessage);
  });

  (async () => {
    try {
      for await (const ev of ctx.event.subscribe()) {
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
          names.set(p.id, p.name);
        } else if (t === "session.tool.called") {
          const m = assistant(id, p.assistantMessageID);
          m.parts.push({
            type: "tool",
            callID: p.id,
            tool: names.get(p.id) ?? "",
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
          // A turn is over: export, and send the path with `Stop`.
          await hook({
            ...base(id, writeExport(id)),
            hook_event_name: "Stop",
            prompt_id: turns.get(id) ?? "",
            stop_hook_active: false,
          });
          if (mode() === "run") await end(id, "run");
        } else if (t === "session.deleted") {
          await end(id, "deleted");
        }
      }
    } catch (e) {
    }
  })();
};

// One default export for both: v1 reads `server`, v2 reads `setup`.
export default { id: "tokeneconomizer", server: TokenEconomizer, setup: setupV2 };
