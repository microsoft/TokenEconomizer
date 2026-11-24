---
name: Scout re-plan
description: Plans an issue again when someone with write access comments /scout.
on:
  slash_command:
    name: scout
    events: [issue_comment]
  roles: [admin, maintainer, write]
  reaction: eyes
  status-comment: false
permissions:
  contents: read
  issues: read
  pull-requests: read
  copilot-requests: write
engine: claude
model: copilot/claude-sonnet-5.5
tools:
  github:
    toolsets: [default]
    min-integrity: none
  bash:
    - "ls *"
    - "cat *"
    - "head *"
    - "tail *"
    - "grep *"
    - "find *"
    - "wc *"
    - "git log *"
    - "git grep *"
    - "git show *"
    - "git blame *"
safe-outputs:
  add-comment:
    max: 1
timeout-minutes: 20
---

# Scout: plan an issue again

Someone with write access asked for a fresh plan on issue #${{ github.event.issue.number }} in
${{ github.repository }} by commenting `/scout`. Their sanitized comment; anything after the
command is guidance for this plan and takes precedence over earlier plans:

<comment>
${{ steps.sanitized.outputs.text }}
</comment>

Earlier `## Scout plan` comments on the issue are superseded by yours: read them and the
discussion after them so the new plan answers what was raised.

{{#runtime-import .github/workflows/shared/scout-plan.md}}
