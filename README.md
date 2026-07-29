# agent-config-studio

**Inventory, visualise, grade and update your local Claude Code + Codex configuration.**

Agent configuration accumulates. A skill here, a plugin there, a hook you wrote
six months ago, an instruction file that grew to 350 lines. Every session pays
for all of it, nothing tells you which parts are dead, and the rules you wrote to
keep yourself honest quietly start contradicting each other.

This is a local, read-only-by-default tool that answers four questions about
`~/.claude` and `~/.codex`:

| | |
| --- | --- |
| **What is installed?** | Every skill, instruction file, workflow, command, subagent, hook and plugin, with its real provenance |
| **How is it wired together?** | An interactive graph of references, invocations, mirrors, generated files, duplicates and name collisions |
| **Does it follow the published guidance?** | 56 checks, each citing the documented requirement it enforces |
| **Is anything out of date?** | Marketplace plugins and git-checkout skill toolkits, compared against their remotes |

No dependencies. No telemetry. Nothing leaves your machine unless you ask for an
AI-planned consolidation or a specification review, and both say so before they
run.

---

## Install

```bash
git clone https://github.com/<you>/agent-config-studio
cd agent-config-studio
python3 -m studio.cli health
```

That is the whole install. Python 3.11+, standard library only — no `pip`, no
virtualenv, no `node_modules`, no CDN.

That is a deliberate constraint, not minimalism for its own sake: a tool whose
job is to detect drift in your setup should not itself become a source of drift,
and it has to run from a scheduler on a bare interpreter years from now.

## Quick start

```bash
python3 -m studio.cli serve      # dashboard at http://127.0.0.1:8787
python3 -m studio.cli health     # run every check, print the verdict, exit 1 on failure
python3 -m studio.cli updates    # check plugins and toolkits for available updates
python3 -m studio.cli usage      # what you actually invoke, from your own history
```

Everything above is read-only. Nothing writes to `~/.claude` or `~/.codex`
unless you explicitly run `sync --apply`, `apply`, or start the dashboard with
`--allow-actions`.

**New here?** [`docs/TUTORIAL.md`](docs/TUTORIAL.md) walks through a first run,
what each dashboard view answers, and the three routines the tool is built
around. ([繁體中文](docs/TUTORIAL.zh-TW.md))

## The dashboard

Six views at `127.0.0.1:8787`:

**Overview** — verdict, blocking count, preloaded metadata cost, available
updates, instruction-file sizes, a trend across every past run, and how much of
your history the usage index actually covered.

**Graph** — force-directed, filterable by kind and runtime. Node size tracks
weight (a skill's body length, a plugin's skill count). Red rings mark nodes with
blocking findings. Edge style encodes the relationship: reference, invocation,
declared mirror, generated-from, undeclared duplicate, name collision. Labels
that would overlap are hidden and reappear as you zoom, so a 700-node view stays
readable instead of turning into a wall of text.

**Findings** — every finding with severity, ownership, exact location, what is
wrong, how to fix it, and a link to the documentation the rule comes from.
Filterable by severity, ownership, category and free text.

**Plugins & updates** — installed version vs remote, per plugin and per toolkit.
Anything that cannot be compared is reported as *unknown*, never as up to date.

**Inventory** — the raw scan, searchable.

**Sync** — whether your rendered instruction files still match their canonical
sources, the pending diff, and every restore point.

### Triggering actions from the page

```bash
python3 -m studio.cli serve --allow-actions
```

Applying a sync and rolling back become buttons. Without the flag the dashboard
still shows every action and the exact command it corresponds to, so you can copy
it — you just cannot fire it from the page.

Binding to loopback is not on its own a defence: any web page you have open can
POST to `127.0.0.1`. Two gates gate the write endpoints instead. The `Origin`
header, which browsers always send on a cross-origin POST and page JavaScript
cannot forge, must match this server. And a per-process session token from
`GET /api/session` must be present — a cross-origin page cannot read that
response because CORS blocks it. Both are verified by tests, including that a
request carrying a valid token from another origin is still refused.

There is deliberately no Electron wrapper. The server already runs as a local
process with full filesystem access; packaging it in a browser shell would add a
hundred megabytes and a second update channel without granting a single
capability the CLI does not already have.

## What you can do

### One-click fixes

```bash
python3 -m studio.cli fix --list    # what is auto-fixable, and why the rest is not
python3 -m studio.cli fix           # preview the diff
python3 -m studio.cli fix --apply   # write it, with a backup
```

In the dashboard the findings table grows a button per finding, plus a single
**fix everything** for the ones that are safe in bulk.

A finding is only auto-fixable when the remedy is mechanical — exactly one
sensible outcome, no judgement about intent. Adding a contents list to a long
reference file qualifies. Splitting an oversized skill does not: someone has to
decide which sections move. Deleting one of two identical directories does not:
someone has to decide which one wins.

Findings without a fix show *why* instead of nothing, so a missing button reads
as a decision rather than an omission.

Two boundaries are deliberate:

- **Vendor-owned findings never get a button.** Writing into content a plugin
  will overwrite on its next upgrade wastes your time.
- **Per-item decisions stay per-item.** `CB002` reports every plugin with no
  recorded usage, but `CB001`'s classifier deliberately keeps some of them —
  `figma` has no recorded skill call, yet the instructions route design work to
  it. Sweeping all of `CB002` into a bulk fix would silently undo that, so it is
  a button per plugin and excluded from *fix everything*.

Files are copied into `var/quarantine/` before being removed from a config tree,
and every fix goes through the same backed-up change set as everything else.

### AI-planned consolidation

Some findings have no single correct answer. Which sections of an oversized skill
should move out, whether two identical files are a deliberate mirror or a
leftover — a checker cannot decide those, and for a long time this tool said so
and stopped.

```bash
python3 -m studio.cli consolidate            # propose plans, validate, show diffs
python3 -m studio.cli consolidate --apply    # write the plans that passed
python3 -m studio.cli consolidate --only SK007 --limit 3
```

The split of responsibility is strict, and it is the whole design:

- **The model proposes.** It sees section titles, sizes and content, and returns
  a structured plan. It never gets a file handle and never writes anything.
- **Code decides whether the plan is admissible.** Every plan is checked against
  the file it claims to act on: do the named sections exist, is any section
  claimed twice, does the result actually satisfy the rule that raised the
  finding, does the target path stay one level deep inside the skill, is any
  content lost. A plan failing any check is rejected whole — there is no partial
  application.
- **The existing machinery applies it.** Validated plans become the same change
  set as everything else: previewable, backed up, reversible.

A wrong proposal therefore costs a rejected plan or a rollback, never a damaged
file. The rejection paths are tested with the model stubbed out, because that
validation layer is the only thing between a bad proposal and your files.

Access goes through `claude -p` in headless mode — no API key, no HTTP client, no
SDK, and the zero-dependency promise holds. Every call is logged to `var/ai/`
with its prompt, response and cost. If the CLI is absent, consolidation reports
that no proposal is available and everything else works unchanged.

### Keeping the rules current as guidance evolves

A rule is a claim about what the published guidance says. Guidance gets revised,
and a rule written against last year's advice becomes confidently wrong with
nothing in the system noticing.

```bash
python3 -m studio.cli specs             # has any cited document changed?
python3 -m studio.cli specs --review    # ask what changed and whether rules still hold
python3 -m studio.cli specs --accept    # record current content as the new baseline
```

`canonical/spec-baseline.json` stores a normalised hash of every document the
rules cite, mapped to the rules that depend on it. Whitespace and HTML comments
are stripped before hashing, so reflowing a paragraph does not read as a change
of guidance.

Detection is deterministic and needs no model. The `--review` step is the only
part that does: it shows the model the changed document and the rules that cite
it, and asks which still match. It produces a review and nothing else — **rule
code is never edited automatically**. Accepting a new baseline is an explicit
statement that a person looked at the change.

### Updating drives each package's own updater

```bash
python3 -m studio.cli update           # what is out of date, and how each would be updated
python3 -m studio.cli update --apply   # run the updaters
python3 -m studio.cli update --only gstack --apply
```

The dashboard puts an **update** button next to each out-of-date item.

The distinction that matters: *never reimplement package management, but do
invoke the official one*. Those are different things, and an earlier version of
this tool conflated them and refused to update anything.

- **Plugins** go through `claude plugin update <plugin>`, Claude Code's own
  install logic. Nothing here edits `installed_plugins.json` by hand — there is a
  test asserting that.
- **Skill toolkits** are git checkouts, and their documented git-install upgrade
  is stash, fetch, reset to origin, then run the repo's own `setup`. That
  sequence runs, with the pre-upgrade commit captured *first* so a failed setup
  still has something to go back to.
- **Anything else** is reported as having no automatic path, with the command to
  run by hand. Guessing at a third mechanism is how you get an updater that
  drifts from the real one.

## Design decisions worth knowing

**Scanning and grading never write.** Every mutation goes through a change set
that saves a timestamped backup of the exact bytes it replaces. `studio backups`
lists restore points; `studio rollback <id>` puts them back.

**The dashboard is read-only until you opt in.** It binds to loopback and
refuses any other host. Write endpoints exist only with `--allow-actions`, and
even then require a matching `Origin` and a per-process session token, so a stray
browser tab cannot rewrite your agent configuration.

**Every rule cites its source.** A "compliant" verdict is re-derivable from the
published documentation rather than taken on trust — open the `spec` link on any
finding and the threshold the checker enforces is right there.

**No synthesised score.** A single 0–100 number invites tuning the number instead
of the configuration. The verdict is a count of unwaived findings *you own*,
which can only improve by fixing something or putting a waiver on the record.

**Ownership is part of the model.** A 2,500-line skill shipped by a plugin is
real, but editing it is undone by the next upgrade. Vendor- and toolkit-owned
findings are reported and never block; the actionable response is to upgrade,
remove, or waive.

**Silence beats guessing.** Checks that depend on "is this actually used?" stay
silent when the usage index is incomplete, and the remediation example refuses to
run at all below full coverage.

## What it checks

56 rules across six categories:

| Prefix | Covers |
| --- | --- |
| `SK` | Frontmatter validity, name rules, description quality, the 500-line body budget, progressive-disclosure depth, duplicate and colliding skills, cross-runtime copies that have drifted, skills whose descriptions overlap |
| `IN` | Instruction size, internal duplication, contradictions with a skill, over-verification, subagent self-review, forceful language, dead references |
| `HK` | Hook conditions that fire when there is nothing to report, imperative injected context, rules re-injected at context boundaries |
| `WF` | Orphan workflows, dead references, routing to things that do not exist, cross-runtime asymmetry, command/skill name collisions |
| `CB` | Avoidable preloaded metadata, unused plugins, skills you have never invoked, plugins your config names but never uses, duplicate installs, stray files |
| `AG` | Subagents that never load, missing or invalid `name`/`description`, duplicate names, descriptions that never say when to delegate |
| `MR` | Managed-mirror drift, generated-file drift, unreadable governance declarations, paths the scan could not read |

`python3 -m studio.cli health --json` emits every finding with machine-readable
evidence; the dashboard's rule list links each one to its source.

### One distinction the rules make deliberately

Current guidance says to remove instructions that **add a verification step** —
"include a final verification step", "use a subagent to verify" — because they
compound with behaviour the model already has.

It does **not** ask you to remove constraints on **output truthfulness** — "do
not claim verified without evidence", "report what was not exercised". Those are
not extra steps.

`IN002` and `IN003` are scoped to the step-adding forms only, and skip negated
statements, so a rule that *forbids* subagent self-review reads as compliance
rather than a violation. Conflating the two would have the checker demand
deleting a safeguard nothing objects to. There are tests for exactly this.

### Why cold skills are graded separately from plugins

A plugin is all-or-nothing: you cannot drop one of its skills, so its cost is
graded per plugin. Your own skills and a toolkit's are not - each one is a file
you can delete - so those are graded individually (`CB007`).

That distinction was not academic. Grading only plugins left the larger half of
preloaded cost unexamined; in the setup this was built against, 71 hand-authored
and toolkit skills had never once been invoked and were costing about 7,400
tokens per session with nothing reporting it.

A related gap sat in the plugin classifier. Naming a plugin in your instructions
counts as a reason to keep it, which is right - routing work to it is a deliberate
choice. But treating that as a *silent* keep meant the worst case went unreported:
a plugin mentioned in one line, never invoked by skill or by MCP tool, quietly
charging every session. `CB008` now surfaces those with the cost attached. Neither
rule can disable anything: a skill fires when its description matches the
conversation, so "never invoked" means "has not come up yet", not "useless", and
only a person can tell those apart.

### Three gaps that only showed up in a real configuration

Each of these was found by using the tool on a live setup and noticing something
it could not see.

**A pair that is one skill in two runtimes, drifting.** `SK013` only reports
copies that are still byte-identical; `MR001` only checks groups you declared. A
pair that is meant to be one skill, has already diverged, and was never declared
fell between them. The two most-invoked skills in the configuration this was
built against - 787 and 478 recorded uses - were exactly that. `SK016` closes it.

**Two different skills doing the same job.** Selection happens on descriptions,
so two descriptions covering the same ground make the choice ambiguous, and the
one that wins is not necessarily the one that works. Neither the name check nor
the byte check can see it, because the names differ and so do the bytes.
`SK017` reports the pair and leaves the editorial call to you.

**A routing line that resolves to nothing.** Instructions route work by naming
things. When a name stops existing nothing errors - the agent reads a confident
instruction, cannot act on it, and quietly does something else, which is worse
than no line at all because the line reads as coverage. `WF006` resolves every
backticked name against everything installed, including plugin-supplied commands.
It ignores CLI flags, paths, tool identifiers and CSS tokens, because a check
that cries wolf gets muted and then protects nothing.

### Never passing on evidence it does not have

Three separate paths let a run report PASS without having checked:

* A malformed `governance.json` was swallowed and read as "no declarations", so
  every mirror, generated-file, vendored and waiver check silently had nothing to
  check and all of them passed. A missing comma could turn the audit green.
* Unreadable paths were recorded in `scan_errors` and skipped - which keeps one
  bad file from taking the run down - but the verdict was computed only from
  findings, so a file that was never examined produced none and looked clean.
* An entirely unused plugin set produces an *empty* usage dictionary, and the
  guard tested that dictionary for truthiness. The usage rules therefore skipped
  themselves in exactly the case they exist for.

`MR004`, `MR005` and a separate index-availability flag close those. The
principle each one restores: absence of evidence must never be reported as
evidence of compliance.

## Evidence, not assumption

Several checks hinge on whether something is actually used. That is answered from
an index built over your complete local history:

- Claude Code transcripts — `Skill` calls, MCP tool calls, subagent spawns
- Codex rollout transcripts — tool calls, and `SKILL.md` reads, which is how
  Codex invokes a skill
- Typed slash commands from both runtimes

The index reports its own coverage. Counting only skill invocations would have
been actively wrong: in the setup this was built against, `playwright` (901 calls)
and `supabase` (186 calls) ship MCP tools rather than skills and would have looked
completely unused.

Results are cached per file, keyed on size and mtime. A cold pass over ~26 GB of
transcripts takes about a minute; a warm one takes about a second, which is what
makes a daily scheduled check practical.

## Scheduled health checks

```bash
scripts/install-launchd.sh install   # daily 09:20 — runs the rules,
                                     # checks remote updates, and re-fetches
                                     # every document the rules cite
scripts/install-launchd.sh status    # loaded? drifted from the repo? last verdict?
scripts/install-launchd.sh run-now   # trigger once and wait for the result
```

macOS only for now. Contributions for systemd timers welcome.

### Two macOS findings baked into this

Both cost real debugging time and are worth knowing if you write LaunchAgents.

**A LaunchAgent cannot read `~/Documents`, and `open()` there does not fail — it
hangs.** TCC protects that directory and a background agent has no way to show a
consent prompt. `ls` returns `Operation not permitted` immediately, but an
explicit `open()` blocks forever, so "check first, then read" does not save you.
Pointing `WorkingDirectory` inside it makes Python hang in `getcwd()` before
running a single line of your code. The installer therefore runs the agent from a
copy under `~/Library/Application Support`, which is not protected, and
`studio/safeio.py` puts a timeout around any read that might land outside the
agent-config directories. A path that times out is reported as *unverified*
rather than taking the run down.

**`ProcessType=Background` with `LowPriorityIO` throttles I/O hard.** The same
health check took over ten minutes under that QoS class and about one minute
without it. The plist uses `Nice` instead.

## Configuration

Everything is optional — the tool scans, grades, graphs and checks updates with
no configuration at all.

See [`canonical/README.md`](canonical/README.md) for the instruction-rendering
mechanism, which is what lets Claude Code and Codex share one source of truth
instead of two hand-maintained copies that drift. `canonical/governance.json`
also declares **mirrors** (paths that must stay byte-identical), **vendored**
paths (not yours to fix), and **waivers** (findings acknowledged with a reason,
on the record rather than muted).

## Layout

```
studio/
  fm.py         dependency-free frontmatter reader
  model.py      inventory and finding dataclasses
  scan.py       read-only filesystem scanners
  usage.py      invocation index with per-file caching
  toolkits.py   git-checkout skill toolkits and their versions
  plugins.py    the shared "is this plugin avoidable cost?" decision
  updates.py    remote comparison for plugins and toolkits
  graph.py      relationship graph builder
  rules/        one module per category, one function per check
  health.py     run, persist, trend
  canonical.py  render instruction files from shared sources
  patch.py      change sets, backups, rollback
  refactor.py   split an oversized SKILL.md into reference files
  safeio.py     reads that cannot hang
  ai.py         the one place a model is called; proposals only, never writes
  consolidate.py  AI-planned splits and duplicate resolution, validated in code
  specs.py      drift detection on the documents the rules cite
  server.py     loopback dashboard (stdlib http.server)
  cli.py        entry point
web/            dashboard; hand-rolled force-directed graph, no CDN
canonical/      your instruction sources + governance declarations
examples/       worked remediation scripts built on studio.patch
tests/          334 tests (283 Python + 51 dashboard), standard library unittest
```

## Writing your own checks

A rule is a generator that yields findings:

```python
@rule("XX001", "Short title", Severity.IMPORTANT, SPEC_URL, "category")
def xx001(inv: Inventory, cfg: Config):
    for s in inv.skills:
        if some_condition(s):
            yield make(
                REG["XX001"],
                "What is wrong, concretely, with the numbers that show it.",
                path=s.path,
                evidence={"machine": "readable"},
                remedy="What to do about it.",
            )
```

Drop it in a module under `studio/rules/`. Two conventions keep the output
trustworthy: cite a real specification URL, and say what is wrong rather than
which rule fired.

## Scope

**Does:** read your local agent configuration, grade it, show how it connects,
tell you what is stale, and apply reviewed changes with backups.

**Does not:** modify anything without an explicit apply, send anything anywhere,
manage plugin installation, or judge whether a skill is *good* — only whether it
is well-formed, reachable, non-duplicated and affordable.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

334 tests (283 Python + 51 dashboard). Four groups are worth singling out.

The hook tests run real shell commands against throwaway git repositories,
because the defect they exist to prevent is a condition that fires when there is
nothing to report — so "does not fire" is asserted as carefully as "does fire".

The action-gate tests cover the write endpoints, including the case that matters
most: a request carrying a valid session token but arriving from another origin
is still refused.

The fix tests check what each automatic fix must *preserve*, not only what it
changes — a fix that damages a file is the worst failure this tool could have.
They caught two real bugs on first run: re-running the contents-list fix stacked
a second list, and one rule had neither a fix nor an explanation for why not.

The consolidation tests stub the model out and feed it bad plans — hallucinated
section names, a section claimed twice, a plan that empties the skill, a target
path escaping the skill directory. What is under test is the rejection logic,
because that is the only thing standing between a bad proposal and your files.

### The dashboard is tested too

`web/render.js` holds every pure HTML builder the page uses - what a token
figure counts, whether a finding needs action, whether a skill can actually be
invoked. Keeping them free of DOM and state means they are testable without a
browser, and `tests/test_render.mjs` exercises them in plain Node: no test
framework, no npm, the same constraint as everything else here. `python3 -m
unittest` runs them alongside the Python suite and skips them if node is absent.

This existed because the UI was the one place carrying real judgement with no
automated coverage. Telling a reader to invoke a skill that cannot load, or
labelling a vendor-owned finding as their problem, is wrong in exactly the way
this tool exists to catch - and it was only ever verified by looking at it once.

## Contributing

Issues and pull requests welcome. Two things make a change easy to accept:

1. **A new rule cites the guidance it enforces.** Rules without a source are
   opinions, and opinions do not belong in a checker.
2. **A rule that can produce a false positive has a test for the negative case.**
   Several rules here exist in their current form only because the first version
   flagged something legitimate; those cases are pinned by tests.

## License

MIT — see [LICENSE](LICENSE).
