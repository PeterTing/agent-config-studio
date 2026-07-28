# canonical/

The single source of truth for your agent instructions.

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`. Maintaining the same
rules by hand in both guarantees they drift apart. Instead, both are **rendered**
from shared sources here:

```
core.md          rules that apply to every runtime
claude.delta.md  Claude-specific tool names, gates, paths
codex.delta.md   Codex-specific tool names, gates, paths
       |
       |  studio sync
       v
~/.claude/CLAUDE.md      ~/.codex/AGENTS.md
```

Edit the sources, run `studio sync --apply`. Editing a rendered file directly is
drift, and rule `MR003` fails on it, naming the first differing line.

## Files

| File | Tracked in git? | What it is |
| --- | --- | --- |
| `core.md` | no | Your shared rules |
| `claude.delta.md` | no | Appended when rendering `CLAUDE.md` |
| `codex.delta.md` | no | Appended when rendering `AGENTS.md` |
| `governance.json` | no | Mirrors, generated targets, vendored paths, waivers |
| `examples/` | yes | Templates to copy from |

The first four are untracked on purpose: they contain your own instructions and
absolute paths on your machine. Publishing them would publish your setup.

## Getting started

```bash
cp canonical/examples/core.example.md          canonical/core.md
cp canonical/examples/claude.delta.example.md  canonical/claude.delta.md
cp canonical/examples/codex.delta.example.md   canonical/codex.delta.md
cp canonical/examples/governance.example.json  canonical/governance.json
# edit them, then:
python3 -m studio.cli sync            # preview the diff
python3 -m studio.cli sync --apply    # write it, with a backup
```

Nothing here is required. Skip `generated` in `governance.json` and the tool
still scans, grades, graphs and checks updates - you just do not get rendered
instruction files or drift detection.


## Rendering a skill, not just an instruction file

Any file can be a generated target, including a `SKILL.md`. Two skills here are
rendered into both runtimes from one source, because the only differences between
the copies were runtime-specific tool names - exactly the case `CLAUDE.md` and
`AGENTS.md` already solve.

Two things the renderer does for this to be safe:

* **The banner goes below the frontmatter.** YAML frontmatter is only frontmatter
  at the very top of a file. A banner above it costs the file its name and
  description, and a `SKILL.md` with no description silently stops being loadable
  at all - no error, it just never fires again.
* **A line that is only an empty variable disappears.** Runtimes do not merely
  word steps differently; one sometimes has a step the other genuinely does not.
  Without this, expressing that needs a second copy of the whole file, which is
  the thing being eliminated.

Content-comparison rules never see the banner (`canonical.strip_banner`),
otherwise identical provenance boilerplate in every generated file reads as
duplicated content.
