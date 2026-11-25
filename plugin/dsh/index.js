/**
 * TokenEconomizer for DeepSeek Harness (deepseek-ai/dsh).
 *
 * dsh has no hook protocol -- plugins are Cordis modules on its event bus -- so this plugin
 * speaks the one the Claude Code, Codex and Pi integrations use, on dsh's behalf: every event
 * below becomes one call of the shared dispatcher (`../scripts/te-hook --host dsh`, JSON in,
 * JSON out), and the answer is applied with dsh's own levers. All of the mechanism logic lives
 * in the dispatcher; this file only translates.
 *
 *   agent/created          -> SessionStart       the learned manual, registered as the agent's
 *                                                `tokeneconomizer` system-prompt section
 *   agent/pre-step, the first step of a person's prompt
 *                          -> UserPromptSubmit   opens a prompt-turn; context it returns enters
 *                                                that step as a plugin-sourced user message
 *   tools/post-execute     -> PostToolUse        the guard; a redirect is appended to the tool
 *                                                result the model reads next
 *   agent/status idle      -> Stop               closes the turn; the session's events are
 *                                                written as JSON and the path sent along
 *   agent/disposed, a headless run's end, or the process exiting
 *                          -> SessionEnd         hands the session to a detached fold
 *
 * Subagents (`header.origin === 'subagent'`, or a delegation depth) are left alone: they are
 * part of the session that started them.
 *
 * Plain JavaScript that imports no dsh package, so it needs no build step and is not bound to
 * the exact dsh version its packages' peer ranges would pin. Like the hooks on the other agents
 * it fails open: a dispatcher that is missing, slow or broken answers "no change". It does
 * nothing under TE_SIDECALL=1 (the plugin's own side calls) or TE_DISABLE=1.
 */
import { spawn, spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'tokeneconomizer'
export const inject = []

const HERE = dirname(fileURLToPath(import.meta.url))
const SCRIPTS = join(HERE, '..', 'scripts')
const HOOK_TIMEOUT_MS = 60_000
const SECTION = 'tokeneconomizer'
// After dsh's own sections and before the persona suffix (order 10200).
const SECTION_ORDER = 10_100

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v ?? '').trim().toLowerCase())
const disabled = () => truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)

/** `dsh --profile headless` (or `dsh headless`) runs one task, then the process ends. */
export const mode = (env = process.env, argv = process.argv) =>
  env.TE_DSH_MODE || (argv.includes('headless') ? 'headless' : 'interactive')

/** The program and arguments that run a launcher. The launchers are POSIX `sh`, which Windows
 * cannot start; there the bootstrap they run is started with Python itself (`TE_PYTHON`, else
 * `python`). */
export function launch(script, args, platform = process.platform) {
  if (platform !== 'win32') return [join(SCRIPTS, script), args]
  const kind = script === 'te-hook' ? 'hook' : 'cli'
  return [process.env.TE_PYTHON || 'python', [join(SCRIPTS, 'te_bootstrap.py'), kind, ...args]]
}

const childEnv = () => ({ ...process.env, PYTHONUTF8: '1', TE_DSH_MODE: mode() })

/** One dispatcher call; `{}` (no change) on any failure. */
function hook(payload) {
  if (disabled()) return Promise.resolve({})
  return new Promise((done) => {
    let out = ''
    let child
    try {
      const [program, argv] = launch('te-hook', ['--host', 'dsh'])
      child = spawn(program, argv, {
        windowsHide: true,
        env: childEnv(),
        stdio: ['pipe', 'pipe', 'ignore'],
      })
    } catch {
      done({})
      return
    }
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      done({})
    }, HOOK_TIMEOUT_MS)
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (d) => (out += d))
    child.on('error', () => {
      clearTimeout(timer)
      done({})
    })
    child.on('close', () => {
      clearTimeout(timer)
      try {
        done(JSON.parse(out.trim().split('\n').pop() || '{}') ?? {})
      } catch {
        done({})
      }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

/** The same call, blocking: for a process that is exiting and cannot wait on an event loop. */
function hookSync(payload) {
  if (disabled()) return
  try {
    const [program, argv] = launch('te-hook', ['--host', 'dsh'])
    spawnSync(program, argv, {
      windowsHide: true,
      env: childEnv(),
      input: JSON.stringify(payload),
      timeout: HOOK_TIMEOUT_MS,
      stdio: ['pipe', 'ignore', 'ignore'],
    })
  } catch {
    /* fail open */
  }
}

const stateRoot = () =>
  resolve(process.env.TOKENECONOMIZER_HOME || join(homedir(), '.tokeneconomizer'))

/** A delegated child, which belongs to the session that started it. */
export function isSubagent(agent) {
  const header = agent?.session?.header
  return header?.origin === 'subagent' || (header?.delegationDepth ?? 0) > 0
}

const textOf = (content) =>
  Array.isArray(content)
    ? content.filter((b) => b?.type === 'text').map((b) => b.text ?? '').join('\n')
    : typeof content === 'string' ? content : ''

/** The session so far, as the JSON the fold reads (`trace/dsh.py`): dsh's own log is
 * Zstandard-compressed, so the plugin writes the same events uncompressed. Synchronous, so a
 * process that is exiting still writes it whole. */
export function exportSession(agent) {
  try {
    const header = agent.session.header ?? {}
    const id = String(header.id ?? agent.session.id ?? '')
    if (!/^[\w.-]+$/.test(id)) return ''
    const events = agent.session.snapshotEvents?.() ?? []
    const file = join(stateRoot(), 'dsh-sessions', `${id}.json`)
    const json = JSON.stringify({ header, events }, (_k, v) =>
      typeof v === 'bigint' ? Number(v) : v)
    mkdirSync(dirname(file), { recursive: true, mode: 0o700 })
    writeFileSync(file, json, { mode: 0o600 })
    return file
  } catch {
    return ''
  }
}

/** A user-role message the plugin adds; its `source.kind` keeps it apart from the person's. */
function pluginMessage(text) {
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: SECTION, form: 'snapshot', sections: [{ name: SECTION, text }] },
  })
}

export function apply(ctx) {
  // Both are set when the process starts (a side call's nested dsh), so nothing is subscribed.
  if (disabled()) return
  const sessions = new Map()     // session id -> {agent, manual, prompts, turn}

  const base = (agent, transcript = '') => ({
    session_id: String(agent.session.header?.id ?? agent.session.id ?? ''),
    transcript_path: transcript,
    cwd: agent.session.header?.cwd || process.cwd(),
  })

  const notify = (text) => {
    if (typeof text !== 'string' || !text) return
    try {
      if (mode() === 'headless') console.error(text)
      else ctx.logger?.info?.(text)
    } catch {
      /* fail open */
    }
  }

  const start = async (agent, source) => {
    const sid = base(agent).session_id
    if (!sid || sessions.has(sid)) return
    // A resumed conversation keeps counting from its saved prompts, so turn keys stay unique.
    const prompts = (agent.session.snapshotEvents?.() ?? []).filter((e) =>
      e?.type === 'user/message' && e.data?.source?.kind === 'user' && textOf(e.data.content).trim(),
    ).length
    const st = { agent, manual: '', prompts, turn: '' }
    sessions.set(sid, st)
    const out = await hook({ ...base(agent), hook_event_name: 'SessionStart', source })
    st.manual = out?.hookSpecificOutput?.additionalContext ?? ''
    notify(out?.systemMessage)
    if (!st.manual) return
    try {
      // Agent-scoped: the section unwinds with the agent. dsh reads it at every assembly and
      // sends nothing again while the text is unchanged.
      agent.ctx.inject(['systemPrompt'], (scope) => {
        scope.systemPrompt.section({
          name: SECTION,
          order: SECTION_ORDER,
          interpolate: false,
          text: () => st.manual,
        })
      })
    } catch {
      /* fail open */
    }
  }

  const end = async (sid, reason) => {
    const st = sessions.get(sid)
    if (!st || st.ending) return
    st.ending = true
    const file = exportSession(st.agent)
    await hook({ ...base(st.agent, file), hook_event_name: 'SessionEnd', reason })
    // Dropped only once handed over: a process that exits first sends it from `exit`.
    sessions.delete(sid)
  }

  // Where the process ends without `agent/disposed` (a killed TUI, a headless run torn down),
  // the sessions still open are handed over, once, synchronously.
  process.once('exit', () => {
    for (const st of sessions.values()) {
      hookSync({
        ...base(st.agent, exportSession(st.agent)),
        hook_event_name: 'SessionEnd',
        reason: 'exit',
      })
    }
    sessions.clear()
  })

  ctx.on('agent/created', async (payload) => {
    try {
      const agent = payload?.agent
      if (!agent?.session || isSubagent(agent)) return
      // A conversation that already holds messages is a resumption, whatever started it.
      const source = payload.source === 'startup' && (agent.session.snapshotEvents?.() ?? [])
        .some((e) => e?.type === 'user/message') ? 'resume' : payload.source || 'startup'
      await start(agent, source)
    } catch {
      /* fail open */
    }
  })

  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    try {
      if (decision?.kind === 'reject') return decision
      const agent = payload?.agent
      if (!agent?.session || payload.signal?.aborted || isSubagent(agent)) return decision
      const prompt = (payload.messages ?? [])
        .filter((m) => m?.source?.kind === 'user')
        .map((m) => textOf(m.content))
        .join('\n')
      if (payload.step !== 1 || !prompt.trim()) return decision
      const sid = base(agent).session_id
      if (!sessions.has(sid)) await start(agent, 'startup')
      const st = sessions.get(sid)
      if (!st) return decision
      st.prompts += 1
      st.turn = `${sid}:${st.prompts}`
      const out = await hook({
        ...base(agent),
        hook_event_name: 'UserPromptSubmit',
        prompt_id: st.turn,
        prompt,
      })
      notify(out?.systemMessage)
      const extra = out?.hookSpecificOutput?.additionalContext
      if (!extra) return decision
      return { ...decision, messages: [...(decision.messages ?? payload.messages ?? []),
        pluginMessage(extra)] }
    } catch {
      return decision
    }
  }, { prepend: true })

  ctx.on('tools/post-execute', async (exec, result, next) => {
    const decision = await next()
    try {
      const agent = exec?.agent
      if (decision?.kind !== 'accept' || decision.value !== undefined) return decision
      if (!agent?.session || isSubagent(agent)) return decision
      const st = sessions.get(base(agent).session_id)
      const content = decision.content ?? result?.content ?? []
      const value = result?.value && typeof result.value === 'object' ? result.value : {}
      const exit = typeof value.exitCode === 'number' ? value.exitCode : undefined
      const out = await hook({
        ...base(agent),
        hook_event_name: 'PostToolUse',
        prompt_id: st?.turn ?? '',
        tool_name: exec.name,
        tool_use_id: exec.callId,
        tool_input: exec.arguments ?? {},
        tool_response: { content: textOf(content), isError: !!result?.isError, exit_code: exit },
      })
      notify(out?.systemMessage)
      const nudge = out?.hookSpecificOutput?.additionalContext
      // The redirect rides on the result the model reads next.
      if (!nudge) return decision
      return { ...decision, content: [...content, { type: 'text', text: nudge }] }
    } catch {
      return decision
    }
  }, { prepend: true })

  ctx.on('agent/status', async (payload) => {
    try {
      if (payload?.status !== 'idle') return
      const agent = payload.agent
      if (!agent?.session || isSubagent(agent)) return
      const sid = base(agent).session_id
      const st = sessions.get(sid)
      if (!st?.turn) return
      // The session is written at the end of every turn and its path sent with `Stop`, so a
      // session whose process is killed is still folded later.
      const file = exportSession(agent)
      await hook({
        ...base(agent, file),
        hook_event_name: 'Stop',
        prompt_id: st.turn,
        stop_hook_active: false,
      })
      st.turn = ''
      // A headless run ends right after its one task, so the session is handed over now.
      if (mode() === 'headless') await end(sid, 'headless')
    } catch {
      /* fail open */
    }
  })

  ctx.on('agent/disposed', async (payload) => {
    try {
      const agent = payload?.agent
      if (!agent?.session) return
      await end(base(agent).session_id, 'disposed')
    } catch {
      /* fail open */
    }
  })
}
