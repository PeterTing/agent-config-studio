# examples/

Real remediation scripts, kept as worked examples of the change-set API rather
than as reusable commands.

Each one targets specific skills, hooks and plugins on the machine it was written
for, so running them verbatim will not do anything useful for you. What they do
show is the shape of a safe mutation:

```python
cs = patch.ChangeSet(name="...", description="...", changes=[...])
patch.save(cs, REPO_ROOT)          # write the diff for review
patch.apply(cs, REPO_ROOT)         # write the files, backing up first
```

Every script runs read-only by default and prints a diff; `--apply` is what
writes, and it always creates a restore point first.

| Script | What it demonstrates |
| --- | --- |
| `remediate_hooks.py` | Rewriting `settings.json` hooks; how a hook condition is made self-guarding so it cannot fire on nothing |
| `remediate_skills.py` | Rewriting frontmatter descriptions, and splitting an oversized `SKILL.md` into one-level-deep reference files |
| `remediate_plugins.py` | Refusing to act on incomplete evidence, then disabling only what two independent signals agree is unused |
| `remediate_hygiene.py` | Deleting, moving and mirroring files inside a single reviewable change set |

`remediate_plugins.py` is the most reusable of the four: its selection logic
lives in `studio/plugins.py`, shared with rule `CB001`, so the rule and the fix
can never disagree about what "avoidable" means.
