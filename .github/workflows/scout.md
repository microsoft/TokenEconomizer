---
name: Scout
description: Posts an implementation plan on every newly opened issue.
on:
  issues:
    types: [opened]
  roles: all
  reaction: eyes
permissions:
  contents: read
  issues: read
  pull-requests: read
  copilot-requests: write
engine: claude
model: copilot/claude-sonnet-5.5
user-rate-limit:
  max-runs-per-window: 3
  window: 60
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

# Scout: plan for a new issue

Issue #${{ github.event.issue.number }} in ${{ github.repository }} was just opened. Its sanitized
title and body:

<issue>
${{ steps.sanitized.outputs.text }}
</issue>

{{#runtime-import .github/workflows/shared/scout-plan.md}}
