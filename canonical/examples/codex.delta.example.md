## Codex specifics

<!-- Appended after core.md when rendering ~/.codex/AGENTS.md. -->

| Purpose | Tool |
| --- | --- |
| Search and read | `rg` `sed` `ls` `find` `git diff` |
| Edit | `apply_patch` |
| Browser | the Browser or Chrome plugin |

Skills live at `~/.codex/skills/<name>/SKILL.md`; read one when the situation
calls for it.

### Configuration governance

This file is generated. Edit `canonical/core.md` or `canonical/codex.delta.md`,
then run `python3 -m studio.cli sync --apply`. Editing this file directly is
caught by rule MR003.
