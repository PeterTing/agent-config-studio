## Claude Code specifics

<!-- Appended after core.md when rendering ~/.claude/CLAUDE.md. Keep it to what
     genuinely differs from the other runtime: tool names, paths, gates. -->

| Purpose | Tool |
| --- | --- |
| Search and read | `Grep` `Glob` `Read` |
| Edit | `Edit` `Write` |
| Browser | `mcp__claude-in-chrome__*` |

### Before committing

State what you ran and what it produced. Docs-only commits can skip the test
gate, but the formatter and linter still have to pass.

### Configuration governance

This file is generated. Edit `canonical/core.md` or `canonical/claude.delta.md`,
then run `python3 -m studio.cli sync --apply`. Editing this file directly is
caught by rule MR003.
