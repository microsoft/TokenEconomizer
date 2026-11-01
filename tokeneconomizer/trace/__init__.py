"""Reading agent session logs: the one place that knows each agent's file format.

Every mechanism reads a finished session through this package, so the format knowledge lives
here and nowhere else:

  * `calls`    -- `load_trajectory`: any agent's log as ordered `(name, args, result, ok)` calls;
                  `call_cwds`, `tool_usage`, `strip_rtk_prefix`;
  * `catalog`  -- the tool list a session loaded, from whichever source the agent publishes;
  * `claude`, `codex`, `copilot`, `opencode`, `pi` -- each agent's own session file: usage,
                  metadata, prompt-turns, and the conversions that let the readers above handle
                  it.

Stdlib only.
"""
from .calls import RTK_PREFIX_RE, call_cwds, load_trajectory, strip_rtk_prefix, tool_usage
from .catalog import (CODEX_DEFAULT_TOOL_CATALOG, catalog_sources, parse_catalog,
                      parse_catalog_with_source, parse_tool_catalog, parse_tool_catalog_claude,
                      parse_tool_catalog_flat, parse_tool_names_copilot)
from .claude import (claude_calls, claude_deferred_tools, claude_prompt_count,
                     claude_session_usage, claude_subagent_files, claude_tool_usage)
from .codex import (codex_capability_trace, codex_exec_calls, codex_prompt_count,
                    codex_rollout_to_exec, codex_session_meta, codex_session_usage,
                    rollout_item_to_exec)
from .copilot import (copilot_event_calls, copilot_mcp_servers, copilot_prompt_count,
                      copilot_session_meta, copilot_session_usage, copilot_turns,
                      mcp_server_of)
from .cursor import cursor_events_to_claude, cursor_prompt_count
from .opencode import (opencode_prompt_count, opencode_run_reply, opencode_session_info,
                       opencode_session_to_claude, opencode_session_usage)
from .pi import (pi_active_branch, pi_json_reply, pi_prompt_count, pi_session_header,
                 pi_session_to_claude, pi_session_usage)

__all__ = [
    "RTK_PREFIX_RE", "call_cwds", "load_trajectory", "strip_rtk_prefix", "tool_usage",
    "CODEX_DEFAULT_TOOL_CATALOG", "catalog_sources", "parse_catalog", "parse_catalog_with_source",
    "parse_tool_catalog", "parse_tool_catalog_claude", "parse_tool_catalog_flat",
    "parse_tool_names_copilot",
    "claude_calls", "claude_deferred_tools", "claude_prompt_count", "claude_session_usage",
    "claude_subagent_files", "claude_tool_usage",
    "codex_capability_trace", "codex_exec_calls", "codex_prompt_count", "codex_rollout_to_exec",
    "codex_session_meta", "codex_session_usage", "rollout_item_to_exec",
    "copilot_event_calls", "copilot_mcp_servers", "copilot_prompt_count", "copilot_session_meta",
    "copilot_session_usage", "copilot_turns", "mcp_server_of",
    "cursor_events_to_claude", "cursor_prompt_count",
    "opencode_prompt_count", "opencode_run_reply", "opencode_session_info",
    "opencode_session_to_claude", "opencode_session_usage",
    "pi_active_branch", "pi_json_reply", "pi_prompt_count", "pi_session_header",
    "pi_session_to_claude", "pi_session_usage",
]
