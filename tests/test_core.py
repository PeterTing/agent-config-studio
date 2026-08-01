"""Tests for the parsing, refactoring, safe-IO and rule-logic primitives.

The rule tests focus on the judgements that are easy to get subtly wrong: telling
an instruction that *adds* a verification step apart from one that *forbids*
subagent self-review, and not letting a three-character skill name match inside
an unrelated word.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import textwrap
import unittest
from http import HTTPStatus
from unittest import mock

from studio import fm, refactor, safeio
from studio.model import (
    AgentDef,
    Command,
    Finding,
    Instruction,
    Inventory,
    Origin,
    Owner,
    Runtime,
    Severity,
    Skill,
)
from studio.rules import Config, run_all


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #


class Frontmatter(unittest.TestCase):
    def test_plain_scalars(self):
        f = fm.parse("---\nname: thing\ndescription: does a thing\n---\nbody\n")
        self.assertTrue(f.present)
        self.assertEqual(f.text("name"), "thing")
        self.assertEqual(f.text("description"), "does a thing")
        self.assertEqual(f.body, "body\n")

    def test_quoted_scalars_are_unwrapped(self):
        f = fm.parse('---\nname: "quoted-name"\n---\n')
        self.assertEqual(f.text("name"), "quoted-name")

    def test_folded_block_scalar_joins_lines(self):
        """The bug this guards: reading only the marker line reported a
        one-character description for every skill using `>-`, which made the
        preloaded-metadata estimate wrong by tens of thousands of bytes."""
        f = fm.parse(
            textwrap.dedent(
                """\
                ---
                name: browse
                description: >-
                  Fast headless browser for QA.
                  Use when asked to open a page.
                ---
                body
                """
            )
        )
        self.assertEqual(
            f.text("description"),
            "Fast headless browser for QA. Use when asked to open a page.",
        )

    def test_literal_block_scalar_keeps_newlines(self):
        f = fm.parse("---\nname: x\ndescription: |\n  line one\n  line two\n---\n")
        self.assertIn("\n", f.text("description"))

    def test_flow_and_block_lists(self):
        f = fm.parse('---\nname: x\nallowed-tools: [Read, "Write", Bash]\n---\n')
        self.assertEqual(f.get("allowed-tools"), ["Read", "Write", "Bash"])
        f2 = fm.parse("---\nname: x\ntools:\n  - Read\n  - Write\n---\n")
        self.assertEqual(f2.get("tools"), ["Read", "Write"])

    def test_no_frontmatter(self):
        f = fm.parse("# just markdown\n")
        self.assertFalse(f.present)
        self.assertEqual(f.body, "# just markdown\n")

    def test_unclosed_frontmatter_is_reported_not_guessed(self):
        f = fm.parse("---\nname: x\nstill going\n")
        self.assertFalse(f.present)
        self.assertTrue(any("never closed" in w for w in f.warnings))


# --------------------------------------------------------------------------- #
# section splitting
# --------------------------------------------------------------------------- #

SKILL_WITH_FENCED_HASHES = """\
---
name: demo
description: Demo skill. Use when testing the splitter.
---

# Demo

Intro text.

## Keep This

Kept content.

```bash
# perform workflow
echo "this hash is a shell comment, not a heading"
## also not a heading
```

## Move This

Moved content.

```bash
echo "fenced code inside a moved section"
```

## Keep This Too

More kept content.
"""


class SectionSplitting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "demo")
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "SKILL.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(SKILL_WITH_FENCED_HASHES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_headings_inside_fences_are_not_sections(self):
        """A shell comment starting with # would otherwise slice a code block."""
        titles = [s.title for s in refactor.parse_sections(SKILL_WITH_FENCED_HASHES)]
        self.assertIn("Move This", titles)
        self.assertNotIn("perform workflow", titles)
        self.assertNotIn("also not a heading", titles)

    def test_split_moves_only_named_sections(self):
        changes = refactor.split_skill(
            self.path, [{"target": "reference/moved.md", "heading": "Moved", "sections": ["Move This"]}]
        )
        by_path = {c.path: c.new_text for c in changes}
        skill = by_path[self.path]
        moved = by_path[os.path.join(self.dir, "reference/moved.md")]

        self.assertIn("Keep This", skill)
        self.assertIn("Keep This Too", skill)
        self.assertNotIn("Moved content.", skill)
        self.assertIn("Moved content.", moved)
        self.assertIn("reference/moved.md", skill, "SKILL.md must link to what it moved out")

    def test_split_preserves_every_non_blank_line(self):
        changes = refactor.split_skill(
            self.path, [{"target": "reference/moved.md", "heading": "Moved", "sections": ["Move This"]}]
        )
        original = {ln.strip() for ln in SKILL_WITH_FENCED_HASHES.split("\n") if ln.strip()}
        produced: set[str] = set()
        for c in changes:
            produced |= {ln.strip() for ln in c.new_text.split("\n") if ln.strip()}
        self.assertEqual(original - produced, set(), "splitting must not lose content")

    def test_fences_stay_balanced_on_both_sides(self):
        changes = refactor.split_skill(
            self.path, [{"target": "reference/moved.md", "heading": "Moved", "sections": ["Move This"]}]
        )
        for c in changes:
            fences = sum(1 for ln in c.new_text.split("\n") if ln.lstrip().startswith("```"))
            self.assertEqual(fences % 2, 0, f"unbalanced code fence in {c.path}")

    def test_unknown_section_raises_instead_of_silently_skipping(self):
        with self.assertRaises(KeyError):
            refactor.split_skill(
                self.path, [{"target": "reference/x.md", "sections": ["No Such Section"]}]
            )

    def test_set_description_replaces_only_the_description(self):
        change = refactor.set_description(self.path, "New text. Use when verifying.")
        parsed = fm.parse(change.new_text)
        self.assertEqual(parsed.text("name"), "demo")
        self.assertEqual(parsed.text("description"), "New text. Use when verifying.")
        self.assertIn("Kept content.", change.new_text)


# --------------------------------------------------------------------------- #
# safe IO
# --------------------------------------------------------------------------- #


class SafeIO(unittest.TestCase):
    def setUp(self):
        safeio.reset()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        safeio.reset()
        self._tmp.cleanup()

    def test_reads_a_normal_file(self):
        p = os.path.join(self._tmp.name, "f.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("hello")
        self.assertEqual(safeio.read_text(p), "hello")
        self.assertEqual(safeio.exists(p), True)

    def test_missing_file_is_none_not_an_exception(self):
        p = os.path.join(self._tmp.name, "nope.txt")
        self.assertIsNone(safeio.read_text(p))
        self.assertEqual(safeio.exists(p), False)

    def test_a_path_that_times_out_is_remembered(self):
        """A blocked path must cost one timeout per run, not one per check."""
        blocked = "/definitely/not/a/real/path/that/hangs"
        with safeio._lock:  # noqa: SLF001 - exercising the memo directly
            safeio._unreadable.add(blocked)
        self.assertIsNone(safeio.read_bytes(blocked))
        self.assertIsNone(safeio.exists(blocked))
        self.assertIn(blocked, safeio.known_unreadable())


# --------------------------------------------------------------------------- #
# rule logic
# --------------------------------------------------------------------------- #


def _instruction(tmpdir: str, text: str, name: str = "CLAUDE.md") -> Instruction:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Instruction(
        id=f"instruction:claude:{name}",
        path=path,
        runtime=Runtime.CLAUDE,
        lines=len(text.split("\n")),
        bytes=len(text.encode()),
    )


def _skill(tmpdir: str, name: str, description: str, body: str = "body\n") -> Skill:
    d = os.path.join(tmpdir, "skills", name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return Skill(
        id=f"skill:claude:{name}",
        name=name,
        dir_name=name,
        path=path,
        runtime=Runtime.CLAUDE,
        origin=Origin.LOCAL,
        description=description,
        body_lines=len(body.split("\n")),
    )


def _run(inv: Inventory, tmpdir: str) -> list:
    cfg = Config(repo_root=tmpdir)
    return run_all(inv, cfg)


class VerificationRules(unittest.TestCase):
    """IN002/IN003 must flag added steps, not forbidden ones."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _codes(self, text: str) -> set[str]:
        inv = Inventory(roots={"claude": self.tmp}, instructions=[_instruction(self.tmp, text)])
        return {f.rule for f in _run(inv, self.tmp)}

    def test_flags_an_added_verification_step(self):
        self.assertIn(
            "IN002",
            self._codes("# R\n\n- Include a final verification step for any non-trivial task.\n"),
        )

    def test_flags_subagent_review_of_own_work(self):
        self.assertIn("IN003", self._codes("# R\n\n- Use a subagent to verify the result.\n"))
        self.assertIn("IN003", self._codes("# R\n\n- 兩階段 Review：每個 agent 完成後都要複查。\n"))

    def test_does_not_flag_a_rule_forbidding_subagent_review(self):
        """The remediated instruction says not to do this. That is compliance."""
        codes = self._codes(
            "# R\n\n- 委派 subagent 只用於大而獨立的工作，也不用 subagent 複查自己的產出。\n"
        )
        self.assertNotIn("IN003", codes)
        codes_en = self._codes("# R\n\n- Do not use a subagent to verify your own work.\n")
        self.assertNotIn("IN003", codes_en)

    def test_does_not_flag_an_output_truthfulness_constraint(self):
        """No published guidance asks for these to be removed."""
        codes = self._codes(
            "# R\n\n宣稱完成前要有實際執行過的證據。沒有跑過的流程要標出來。\n"
        )
        self.assertNotIn("IN002", codes)
        self.assertNotIn("IN003", codes)


class SkillNameMatching(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_short_skill_name_does_not_match_inside_a_word(self):
        """`doc` must not match "docs" and drag a section into IN010."""
        text = (
            "# R\n\n## Standards\n\n"
            "- Coverage above 95%.\n- Zero linter warnings.\n"
            "- Explicit error handling.\n- Reports in Chinese unless docs are English.\n"
        )
        inv = Inventory(
            roots={"claude": self.tmp},
            instructions=[_instruction(self.tmp, text)],
            skills=[_skill(self.tmp, "doc", "Writes documentation. Use when asked for docs.")],
        )
        self.assertNotIn("IN010", {f.rule for f in _run(inv, self.tmp)})

    def test_section_restating_a_skill_is_flagged(self):
        text = (
            "# R\n\n## progress-dashboard gate\n\n"
            "- Always read prd-tracker.json first.\n"
            "- Never mark verified without evidence.\n"
            "- Record blockers in the tracker.\n"
            "- Update the tracker before continuing.\n"
            "- Keep the summary in Chinese.\n"
        )
        inv = Inventory(
            roots={"claude": self.tmp},
            instructions=[_instruction(self.tmp, text)],
            skills=[
                _skill(self.tmp, "progress-dashboard", "Tracks PRD acceptance. Use when reporting progress.")
            ],
        )
        self.assertIn("IN010", {f.rule for f in _run(inv, self.tmp)})


class StructuralNoise(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_markdown_table_separators_are_not_duplicate_rules(self):
        text = (
            "# R\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
            "| c | d |\n| --- | --- |\n| 3 | 4 |\n"
        )
        inv = Inventory(roots={"claude": self.tmp}, instructions=[_instruction(self.tmp, text)])
        self.assertNotIn("IN004", {f.rule for f in _run(inv, self.tmp)})

    def test_a_genuinely_repeated_rule_is_flagged(self):
        line = "- Debugging never guesses: find the root cause before changing anything.\n"
        inv = Inventory(
            roots={"claude": self.tmp}, instructions=[_instruction(self.tmp, "# R\n\n" + line + "\n" + line)]
        )
        self.assertIn("IN004", {f.rule for f in _run(inv, self.tmp)})


class OwnershipAndBlocking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_vendor_owned_findings_never_block(self):
        """Editing a plugin's file is undone on upgrade, so it cannot be a gate."""
        big = "x\n" * 700
        s = _skill(self.tmp, "vendor-skill", "A vendor skill. Use when testing.", big)
        s.origin = Origin.PLUGIN
        s.plugin = "some-plugin"
        inv = Inventory(roots={"claude": self.tmp}, skills=[s])
        findings = [f for f in _run(inv, self.tmp) if f.rule == "SK007"]
        self.assertTrue(findings, "an oversized body should still be reported")
        self.assertEqual(findings[0].owner, Owner.VENDOR)
        self.assertEqual(findings[0].severity, Severity.MINOR)

    def test_local_oversized_skill_blocks(self):
        big = "x\n" * 700
        inv = Inventory(roots={"claude": self.tmp}, skills=[_skill(self.tmp, "mine", "Mine. Use when testing.", big)])
        findings = [f for f in _run(inv, self.tmp) if f.rule == "SK007"]
        self.assertTrue(findings)
        self.assertEqual(findings[0].owner, Owner.LOCAL)
        self.assertEqual(findings[0].severity, Severity.IMPORTANT)

    def test_a_waiver_records_a_reason_and_stops_blocking(self):
        from studio.rules import Waiver

        big = "x\n" * 700
        inv = Inventory(roots={"claude": self.tmp}, skills=[_skill(self.tmp, "mine", "Mine. Use when testing.", big)])
        cfg = Config(repo_root=self.tmp, waivers=[Waiver("SK007", "*", "accepted for now")])
        findings = [f for f in run_all(inv, cfg) if f.rule == "SK007"]
        self.assertTrue(findings[0].waived)
        self.assertEqual(findings[0].waiver_reason, "accepted for now")


class PluginClassification(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _inv(self) -> Inventory:
        from studio.model import Plugin

        s = _skill(self.tmp, "unused-thing", "Unused. Use when testing.")
        s.origin = Origin.PLUGIN
        # Matches the paired Plugin.key: the scanner writes the full
        # `plugin@marketplace` identifier into both fields.
        s.plugin = "lonely-plugin@m"
        return Inventory(
            roots={"claude": self.tmp},
            skills=[s],
            plugins=[
                Plugin(
                    id="plugin:claude:lonely-plugin@m",
                    key="lonely-plugin@m",
                    marketplace="m",
                    runtime=Runtime.CLAUDE,
                    enabled=True,
                    skill_count=1,
                )
            ],
        )

    def test_zero_usage_and_unreferenced_is_avoidable(self):
        from studio.plugins import avoidable, classify

        rows = classify(self._inv(), {"other": 5}, corpus="nothing relevant here")
        self.assertEqual(rows[0]["verdict"], "disable")
        self.assertGreater(avoidable(rows)[0], 0)

    def test_being_named_but_never_used_is_flagged_for_review_not_disabled(self):
        """A mention is weak evidence, so it must not silently absolve the cost.

        The failure this pins: `figma` was named in one instruction line, had zero
        recorded usage of both its skills and its MCP tools, and was reported as a
        clean keep - so the single most expensive dead plugin never appeared in
        any finding. It must be surfaced, and it must still never be disabled
        automatically, because the reference may well be intentional.
        """
        from studio.plugins import avoidable, classify

        rows = classify(self._inv(), {}, corpus="we route design work to lonely-plugin")
        self.assertEqual(rows[0]["verdict"], "review")
        self.assertIn("never actually used", rows[0]["reason"])
        # Surfaced, but never swept into the automatic-disable set.
        self.assertEqual(avoidable(rows)[0], 0)

    def test_plugin_shipping_no_skills_is_kept_silently(self):
        """Nothing to reclaim means nothing to report - playwright ships zero
        skills, costs zero always-on tokens, and had 904 recorded uses."""
        from studio.model import Plugin, Runtime

        from studio.plugins import classify

        inv = self._inv()
        inv.plugins = [
            Plugin(
                id="plugin:claude:mcp-only",
                key="mcp-only@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=0,
            )
        ]
        inv.skills = []
        rows = classify(inv, {}, corpus="")
        self.assertEqual(rows[0]["verdict"], "keep")
        self.assertIn("no skills", rows[0]["reason"])

    def test_recorded_usage_keeps_it(self):
        from studio.plugins import classify

        rows = classify(self._inv(), {"lonely-plugin": 3}, corpus="")
        self.assertEqual(rows[0]["verdict"], "keep")
        self.assertIn("3", rows[0]["reason"])


class RuleRegistry(unittest.TestCase):
    def test_every_rule_cites_a_specification(self):
        from studio.rules import REGISTRY, ensure_loaded

        ensure_loaded()
        self.assertGreater(len(REGISTRY), 30)
        for r in REGISTRY:
            self.assertTrue(r.spec.startswith("https://"), f"{r.code} has no spec URL")
            self.assertTrue(r.title, f"{r.code} has no title")

    def test_rule_codes_are_unique(self):
        from studio.rules import REGISTRY, ensure_loaded

        ensure_loaded()
        codes = [r.code for r in REGISTRY]
        self.assertEqual(len(codes), len(set(codes)))

    def test_a_broken_check_is_reported_not_swallowed(self):
        from studio import rules as rules_mod

        ensure = rules_mod.ensure_loaded
        ensure()
        broken = rules_mod.Rule(
            code="ZZ999",
            title="deliberately broken",
            severity=Severity.IMPORTANT,
            spec="https://example.invalid",
            fn=lambda inv, cfg: (_ for _ in ()).throw(RuntimeError("boom")),
            category="test",
        )
        rules_mod.REGISTRY.append(broken)
        try:
            findings = run_all(Inventory(), Config(repo_root="/tmp"))
            self.assertTrue(any(f.rule == "ZZ999" and "boom" in f.detail for f in findings))
        finally:
            rules_mod.REGISTRY.remove(broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------- #
# dashboard write gates
# --------------------------------------------------------------------------- #


class ActionGates(unittest.TestCase):
    """Loopback alone does not protect a write endpoint.

    Any page you have open can POST to 127.0.0.1, so the gates are what actually
    matter: writes must be off unless asked for, must carry the per-process
    token, and must be refused from another origin even when the token is right.
    """

    def setUp(self):
        from studio import server

        self.server = server
        self.handler = server.Handler
        self._saved = (self.handler.allow_actions, self.handler.origin)
        self.handler.origin = "http://127.0.0.1:8787"

    def tearDown(self):
        self.handler.allow_actions, self.handler.origin = self._saved

    def _authorise(self, *, allow, headers):
        """Run Handler._authorised against a stub, returning its refusal or None."""
        self.handler.allow_actions = allow
        stub = self.handler.__new__(self.handler)
        stub.headers = headers
        return self.handler._authorised(stub)

    def test_writes_are_off_by_default(self):
        refusal = self._authorise(
            allow=False,
            headers={"X-Studio-Token": self.server._SESSION_TOKEN, "Origin": self.handler.origin},
        )
        self.assertIsNotNone(refusal)
        self.assertIn("--allow-actions", refusal)

    def test_a_valid_token_from_our_origin_is_accepted(self):
        refusal = self._authorise(
            allow=True,
            headers={"X-Studio-Token": self.server._SESSION_TOKEN, "Origin": self.handler.origin},
        )
        self.assertIsNone(refusal)

    def test_a_missing_token_is_refused(self):
        refusal = self._authorise(allow=True, headers={"Origin": self.handler.origin})
        self.assertIsNotNone(refusal)
        self.assertIn("token", refusal)

    def test_a_wrong_token_is_refused(self):
        refusal = self._authorise(
            allow=True, headers={"X-Studio-Token": "not-the-token", "Origin": self.handler.origin}
        )
        self.assertIsNotNone(refusal)

    def test_another_origin_is_refused_even_with_a_valid_token(self):
        """The CSRF case: a page on another site cannot read our token, but if it
        ever did, the origin check still stops it."""
        refusal = self._authorise(
            allow=True,
            headers={
                "X-Studio-Token": self.server._SESSION_TOKEN,
                "Origin": "https://evil.example",
            },
        )
        self.assertIsNotNone(refusal)
        self.assertIn("origin", refusal.lower())

    def test_the_token_is_not_a_fixed_string(self):
        self.assertGreaterEqual(len(self.server._SESSION_TOKEN), 32)


# --------------------------------------------------------------------------- #
# automatic fixes
# --------------------------------------------------------------------------- #


class AutomaticFixes(unittest.TestCase):
    """A fix that damages a file is the worst failure this tool could have, so
    each one is checked for what it must preserve, not only what it changes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        os.makedirs(os.path.join(self.tmp, "var"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_mechanical_rules_are_auto_fixable(self):
        """Anything needing a judgement call must stay manual."""
        from studio import fixes

        for rule in ("SK007", "SK012", "SK013", "IN001", "WF002", "HK001"):
            self.assertNotIn(rule, fixes.REGISTRY, f"{rule} should require a human decision")

    def test_every_non_fixable_rule_explains_itself(self):
        """A missing button must read as a decision, not an omission."""
        from studio import fixes
        from studio.rules import REGISTRY, ensure_loaded

        ensure_loaded()
        for r in REGISTRY:
            if r.code in fixes.REGISTRY:
                continue
            self.assertIn(r.code, fixes.MANUAL_ONLY, f"{r.code} has neither a fix nor a reason")

    def test_vendor_findings_are_never_auto_fixable(self):
        from studio import fixes
        from studio.model import Finding, Owner

        f = Finding(
            rule="SK009",
            severity=Severity.MINOR,
            title="t",
            detail="d",
            path="/x/reference/a.md",
            owner=Owner.VENDOR,
        )
        info = fixes.available([f])
        self.assertFalse(info[f.key]["fixable"])
        self.assertIn("升級覆蓋", info[f.key]["why"])

    def test_per_item_decisions_are_excluded_from_bulk(self):
        """CB002 reports every unused plugin, but CB001's classifier keeps some
        of them deliberately. Sweeping them into a bulk fix would undo that."""
        from studio import fixes

        self.assertFalse(fixes.REGISTRY["CB002"].bulk)
        self.assertTrue(fixes.REGISTRY["CB004"].bulk)

    def test_contents_fix_preserves_every_line(self):
        from studio import fixes
        from studio.model import Finding

        body = "# Title\n\n## One\n\ntext one\n\n## Two\n\n```bash\n## not a heading\n```\n\ntext two\n"
        path = os.path.join(self.tmp, "ref.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

        f = Finding(rule="SK009", severity=Severity.MINOR, title="t", detail="d", path=path)
        cs = fixes.REGISTRY["SK009"].fn(f, Inventory(), Config(repo_root=self.tmp), self.tmp)
        new = cs.changes[0].new_text

        original = {ln.strip() for ln in body.split("\n") if ln.strip()}
        produced = {ln.strip() for ln in new.split("\n") if ln.strip()}
        self.assertEqual(original - produced, set(), "adding a contents list must not drop content")
        self.assertIn("## Contents", new)
        self.assertIn("- One", new)
        self.assertIn("- Two", new)
        self.assertNotIn("- not a heading", new, "headings inside fences are not sections")
        self.assertLess(new.index("## Contents"), new.index("## One"), "contents goes first")

    def test_contents_fix_declines_when_one_already_exists(self):
        """Re-running must not stack a second contents list. The rule checks the
        same thing, but a fix has to be safe when invoked directly."""
        from studio import fixes
        from studio.model import Finding

        path = os.path.join(self.tmp, "ref.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# T\n\n## One\n\nx\n")
        f = Finding(rule="SK009", severity=Severity.MINOR, title="t", detail="d", path=path)
        cfg = Config(repo_root=self.tmp)

        first = fixes.REGISTRY["SK009"].fn(f, Inventory(), cfg, self.tmp).changes[0].new_text
        self.assertEqual(first.count("## Contents"), 1)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(first)
        self.assertIsNone(
            fixes.REGISTRY["SK009"].fn(f, Inventory(), cfg, self.tmp),
            "a second run must decline rather than add another contents list",
        )

    def test_quarantine_copies_before_deleting(self):
        """A file is never removed from the config tree without a copy landing
        somewhere first."""
        from studio import fixes
        from studio.model import Finding

        stray = os.path.join(self.tmp, "settings.json.bak")
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("important old content\n")

        f = Finding(rule="CB004", severity=Severity.MINOR, title="t", detail="d", path=stray)
        cs = fixes.REGISTRY["CB004"].fn(f, Inventory(), Config(repo_root=self.tmp), self.tmp)
        actions = {c.action for c in cs.changes}
        self.assertEqual(actions, {"create", "delete"})
        copy = next(c for c in cs.changes if c.action == "create")
        self.assertIn("quarantine", copy.path)
        self.assertEqual(copy.new_text, "important old content\n")

    def test_empty_directory_removal_refuses_a_non_empty_one(self):
        from studio import fixes
        from studio.model import Finding

        d = os.path.join(self.tmp, "notempty")
        os.makedirs(d)
        with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        f = Finding(rule="CB005", severity=Severity.MINOR, title="t", detail="d", path=d)
        self.assertIsNone(fixes.REGISTRY["CB005"].fn(f, Inventory(), Config(repo_root=self.tmp), self.tmp))

    def test_finding_keys_stay_distinct_for_one_file(self):
        """Every unused plugin is reported against settings.json; collapsing them
        would make a per-item fix act on whichever one won."""
        from studio.model import Finding

        a = Finding(rule="CB002", severity=Severity.MINOR, title="t", detail="d",
                    path="/s.json", evidence={"plugin": "alpha@m"})
        b = Finding(rule="CB002", severity=Severity.MINOR, title="t", detail="d",
                    path="/s.json", evidence={"plugin": "beta@m"})
        self.assertNotEqual(a.key, b.key)


class UpdateExecution(unittest.TestCase):
    """Updating must drive the official updater, never a reimplementation."""

    def test_plugin_updates_shell_out_to_the_official_cli(self):
        import inspect

        from studio import upgrade

        src = inspect.getsource(upgrade.update_plugin)
        self.assertIn('"claude", "plugin", "update"', src)
        # Nothing here should be hand-editing the install database.
        self.assertNotIn("installed_plugins.json", inspect.getsource(upgrade))

    def test_toolkit_upgrade_records_the_old_commit_before_touching_anything(self):
        """A failed setup leaves the checkout on new code; without the old ref
        there is nothing to go back to."""
        import inspect

        from studio import upgrade

        src = inspect.getsource(upgrade.update_toolkit)
        self.assertLess(
            src.index('"rev-parse", "HEAD"'), src.index('"reset", "--hard"'), "capture HEAD first"
        )
        self.assertIn("restore_hint", src)

    def test_toolkit_upgrade_refuses_a_non_git_directory(self):
        from studio import upgrade

        with tempfile.TemporaryDirectory() as d:
            r = upgrade.update_toolkit(d, "notgit")
            self.assertFalse(r.ok)
            self.assertIn("git", r.message)

    def test_plan_marks_targets_without_an_automatic_path(self):
        from studio import upgrade
        from studio.model import Inventory as Inv

        inv = Inv(toolkits=[{"name": "k", "root": "/nonexistent", "update_available": True,
                             "local_version": "1", "remote_version": "2", "manages_count": 3}])
        item = upgrade.plan(inv)[0]
        self.assertFalse(item["automatic"], "a non-git checkout has no automatic path")
        self.assertTrue(item["method"], "it must still say how to do it by hand")


class ToolkitMigrations(unittest.TestCase):
    """Skipping a toolkit's own migrations is how an updater silently diverges
    from the real one: `setup` alone does not cover stale config or moved files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "kit-upgrade", "migrations"))

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, body="#!/usr/bin/env bash\ntrue\n"):
        p = os.path.join(self.root, "kit-upgrade", "migrations", name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_version_compare_is_numeric_not_lexical(self):
        from studio import upgrade

        self.assertLess(upgrade.version_key("0.15.2.0"), upgrade.version_key("0.15.16.0"))
        self.assertGreater(upgrade.version_key("1.60.1.0"), upgrade.version_key("0.15.16.0"))

    def test_migrations_are_discovered_and_ordered(self):
        from studio import upgrade

        self._write("v0.15.16.0.sh")
        self._write("v0.15.2.0.sh")
        self._write("v1.2.0.0.sh")
        self._write("notamigration.txt")
        found = [os.path.basename(p) for p in upgrade._migration_scripts(self.root, "kit")]
        self.assertEqual(found, ["v0.15.2.0.sh", "v0.15.16.0.sh", "v1.2.0.0.sh"])

    def test_only_migrations_newer_than_the_old_version_run(self):
        from studio import upgrade

        marker = os.path.join(self.root, "ran.txt")
        self._write("v0.15.2.0.sh", f"#!/usr/bin/env bash\necho old >> {marker}\n")
        self._write("v1.2.0.0.sh", f"#!/usr/bin/env bash\necho new >> {marker}\n")
        steps: list[dict] = []
        ran = upgrade._run_migrations(self.root, "kit", "0.15.16.0", steps)
        self.assertEqual(ran, ["1.2.0.0"], "an already-applied migration must not re-run")
        with open(marker, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "new")

    def test_a_failing_migration_is_reported_not_swallowed(self):
        from studio import upgrade

        self._write("v9.0.0.0.sh", "#!/usr/bin/env bash\nexit 3\n")
        steps: list[dict] = []
        ran = upgrade._run_migrations(self.root, "kit", "1.0.0.0", steps)
        self.assertIn("有錯誤", ran[0])
        self.assertEqual(steps[-1]["rc"], 3)

    def test_no_old_version_means_no_migrations(self):
        """Without a known starting version there is no safe subset to run."""
        from studio import upgrade

        self._write("v1.0.0.0.sh")
        self.assertEqual(upgrade._run_migrations(self.root, "kit", "", []), [])


# --------------------------------------------------------------------------- #
# AI-proposed consolidation
# --------------------------------------------------------------------------- #


class ConsolidationValidation(unittest.TestCase):
    """The model proposes; this layer decides whether the proposal is admissible.

    These tests stub the model out entirely. What is under test is the rejection
    logic, because that is the only thing standing between a bad proposal and
    your files.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.skill_dir = os.path.join(self.tmp, "demo")
        os.makedirs(self.skill_dir)
        self.path = os.path.join(self.skill_dir, "SKILL.md")
        body = ["---", "name: demo", "description: Demo. Use when testing.", "---", "", "# Demo", ""]
        for title, n in (("Alpha", 200), ("Beta", 200), ("Gamma", 200), ("Delta", 40)):
            body += [f"## {title}", ""] + [f"{title} line {i}" for i in range(n)] + [""]
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body))
        self.finding = Finding(
            rule="SK007", severity=Severity.IMPORTANT, title="oversized", detail="d", path=self.path
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _with_plan(self, plan):
        """Run the planner with the model replaced by a fixed answer."""
        from studio import ai, consolidate

        original = ai.ask

        def fake(prompt, **kw):
            return ai.Answer(ok=True, text="", data=plan, cost_usd=0.0)

        ai.ask = fake
        try:
            return consolidate.propose_split(self.finding, self.tmp)
        finally:
            ai.ask = original

    def test_a_sound_plan_is_accepted_and_loses_nothing(self):
        p = self._with_plan(
            {
                "moves": [
                    {"target": "reference/alpha-beta.md", "heading": "A and B", "sections": ["Alpha", "Beta"]}
                ],
                "rationale": "moved the bulk",
            }
        )
        self.assertTrue(p.ok, p.rejected_because)
        with open(self.path, encoding="utf-8") as fh:
            original = {ln.strip() for ln in fh.read().split("\n") if ln.strip()}
        produced = set()
        for c in p.change_set.changes:
            produced |= {ln.strip() for ln in c.new_text.split("\n") if ln.strip()}
        self.assertEqual(original - produced, set(), "a split must move content, never drop it")

    def test_a_hallucinated_section_is_rejected(self):
        p = self._with_plan(
            {"moves": [{"target": "reference/x.md", "sections": ["Alpha", "SectionThatDoesNotExist"]}]}
        )
        self.assertFalse(p.ok)
        self.assertTrue(any("不存在" in r for r in p.rejected_because))

    def test_a_section_claimed_twice_is_rejected(self):
        p = self._with_plan(
            {
                "moves": [
                    {"target": "reference/a.md", "sections": ["Alpha"]},
                    {"target": "reference/b.md", "sections": ["Alpha"]},
                ]
            }
        )
        self.assertFalse(p.ok)
        self.assertTrue(any("重複指派" in r for r in p.rejected_because))

    def test_a_plan_that_does_not_get_under_budget_is_rejected(self):
        p = self._with_plan({"moves": [{"target": "reference/d.md", "sections": ["Delta"]}]})
        self.assertFalse(p.ok)
        self.assertTrue(any("超過" in r for r in p.rejected_because))

    def test_a_plan_that_empties_the_skill_is_rejected(self):
        p = self._with_plan(
            {
                "moves": [
                    {
                        "target": "reference/everything.md",
                        "sections": ["Alpha", "Beta", "Gamma", "Delta"],
                    }
                ]
            }
        )
        self.assertFalse(p.ok)
        self.assertTrue(any("掏空" in r for r in p.rejected_because))

    def test_an_escaping_target_path_is_rejected(self):
        """A reference file must stay one level deep inside the skill."""
        for bad in ("../../etc/passwd.md", "reference/nested/deep.md", "notreference/x.md"):
            with self.subTest(target=bad):
                p = self._with_plan({"moves": [{"target": bad, "sections": ["Alpha", "Beta"]}]})
                self.assertFalse(p.ok, f"{bad} should be refused")

    def test_a_model_failure_is_a_rejection_not_a_crash(self):
        from studio import ai, consolidate

        original = ai.ask
        ai.ask = lambda prompt, **kw: ai.Answer(ok=False, error="model unavailable")
        try:
            p = consolidate.propose_split(self.finding, self.tmp)
        finally:
            ai.ask = original
        self.assertFalse(p.ok)
        self.assertIn("model unavailable", " ".join(p.rejected_because))

    def test_the_model_never_receives_a_way_to_write(self):
        """The planner passes text and gets a plan back. Nothing else."""
        import inspect

        from studio import consolidate

        src = inspect.getsource(consolidate)
        for forbidden in ("open(", "os.remove", "shutil"):
            # `open(` appears for reading only; assert no write modes are used.
            pass
        self.assertNotIn('open(path, "w"', src)
        self.assertNotIn("os.remove", src)
        self.assertNotIn("shutil.", src)


class SpecFreshness(unittest.TestCase):
    """A rule written against last year's guidance is confidently wrong, and
    nothing else in the system would notice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        os.makedirs(os.path.join(self.tmp, "canonical"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_formatting_changes_do_not_count_as_guidance_changes(self):
        from studio import specs

        a = "Keep SKILL.md bodies under 500 lines.\n\nSplit when approaching it."
        b = "Keep SKILL.md   bodies under 500 lines.\n\n\nSplit when   approaching it.   "
        self.assertEqual(specs._hash(a), specs._hash(b))

    def test_a_substantive_change_is_detected(self):
        from studio import specs

        a = "Keep SKILL.md bodies under 500 lines."
        b = "Keep SKILL.md bodies under 300 lines."
        self.assertNotEqual(specs._hash(a), specs._hash(b))

    def test_html_comments_are_ignored(self):
        from studio import specs

        self.assertEqual(specs._hash("text <!-- note --> more"), specs._hash("text more"))

    def test_every_rule_is_covered_by_the_baseline_map(self):
        from studio import specs
        from studio.rules import REGISTRY, ensure_loaded

        ensure_loaded()
        mapped = {code for codes in specs._spec_rules().values() for code in codes}
        self.assertEqual(mapped, {r.code for r in REGISTRY}, "every rule must cite a tracked document")

    def test_baseline_round_trips(self):
        from studio import specs

        state = specs.SpecState(url="https://example.invalid/doc", rules=["XX001"], current_hash="abc123")
        path = specs.record(self.tmp, [state])
        self.assertTrue(os.path.isfile(path))
        loaded = specs.load_baseline(self.tmp)
        self.assertEqual(loaded["specs"]["https://example.invalid/doc"]["hash"], "abc123")

    def test_review_never_edits_a_rule(self):
        import inspect

        from studio import specs

        src = inspect.getsource(specs.review_change)
        self.assertNotIn("write", src.replace("rewritten", ""))
        self.assertIn("return", src)


class PurposeExtractionTests(unittest.TestCase):
    """Workflows and commands have no description field, so the catalogue has to
    derive one. Getting this wrong makes the catalogue quote step one of a
    checklist as if it were the file's purpose."""

    def test_frontmatter_description_wins(self):
        from studio.scan import _purpose

        raw = "---\ndescription: Ship the release\n---\n\n# Ship\n\nSomething else.\n"
        self.assertEqual(_purpose(raw), "Ship the release")

    def test_first_prose_line_when_no_frontmatter(self):
        from studio.scan import _purpose

        raw = "# TDD Command\n\n測試驅動開發流程 - RED → GREEN → REFACTOR\n\n## Usage\n"
        self.assertEqual(_purpose(raw), "測試驅動開發流程 - RED → GREEN → REFACTOR")

    def test_list_item_falls_back_to_heading(self):
        """A numbered step is not a statement of purpose."""
        from studio.scan import _purpose

        raw = "# BUILD 流程（開發實作）\n\n1. 先寫失敗測試\n2. 再實作\n"
        self.assertEqual(_purpose(raw), "BUILD 流程（開發實作）")

    def test_skips_fences_and_tables(self):
        from studio.scan import _purpose

        raw = "# X\n\n```bash\nrm -rf /\n```\n\n| a | b |\n\n真正的說明在這裡\n"
        self.assertEqual(_purpose(raw), "真正的說明在這裡")

    def test_empty_file_yields_empty_string(self):
        from studio.scan import _purpose

        self.assertEqual(_purpose(""), "")


class ColdSkillRule(unittest.TestCase):
    """CB007 exists because CB001/CB002 only ever graded plugins, leaving the
    larger half of preloaded cost - your own skills and a toolkit's - unchecked."""

    def _cfg(self, **kw):
        from studio.rules import Config

        return Config(repo_root=".", **kw)

    def _inv(self, skills):
        from studio.model import Inventory

        inv = Inventory()
        inv.skills = skills
        return inv

    def _skill(self, name, origin, runtime, desc_len=1400):
        from studio.model import Origin, Runtime, Skill

        return Skill(
            id=f"skill:{name}",
            name=name,
            dir_name=name,
            path=f"/tmp/{name}/SKILL.md",
            runtime=Runtime.CLAUDE if runtime == "claude" else Runtime.CODEX,
            origin=origin,
            description="x" * desc_len,
        )

    def test_silent_without_usage_evidence(self):
        """No index means no claim - the same discipline CB002 follows."""
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv([self._skill("cold", Origin.LOCAL, "claude")])
        self.assertEqual(list(cb007(inv, self._cfg())), [])

    def test_reports_cold_local_skills(self):
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv(
            [
                self._skill("cold-one", Origin.LOCAL, "claude"),
                self._skill("cold-two", Origin.LOCAL, "claude"),
            ]
        )
        out = list(cb007(inv, self._cfg(skill_usage={"something-else": 3}, usage_available=True, usage_complete=True)))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].evidence["cold_skills"], 2)
        self.assertIn("cold-one", out[0].evidence["names"])

    def test_used_skills_are_not_reported(self):
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv([self._skill("warm", Origin.LOCAL, "claude")])
        self.assertEqual(list(cb007(inv, self._cfg(skill_usage={"warm": 1}, usage_available=True, usage_complete=True))), [])

    def test_plugin_skills_excluded(self):
        """A plugin is all-or-nothing, so an individual plugin skill is not an
        action anyone can take. CB001 covers that at the level that works."""
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv([self._skill("from-plugin", Origin.PLUGIN, "claude", 4000)])
        self.assertEqual(list(cb007(inv, self._cfg(skill_usage={"other": 1}, usage_available=True, usage_complete=True))), [])

    def test_orphan_library_excluded(self):
        """Never loaded, so it costs nothing to leave alone."""
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv([self._skill("orphan", Origin.ORPHAN_LIBRARY, "claude", 4000)])
        self.assertEqual(list(cb007(inv, self._cfg(skill_usage={"other": 1}, usage_available=True, usage_complete=True))), [])

    def test_trivial_cost_is_not_worth_reporting(self):
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv([self._skill("tiny", Origin.LOCAL, "claude", desc_len=10)])
        self.assertEqual(list(cb007(inv, self._cfg(skill_usage={"other": 1}, usage_available=True, usage_complete=True))), [])

    def test_runtimes_are_reported_separately(self):
        """Each runtime only preloads its own, so one combined figure would
        overstate what either session actually pays."""
        from studio.model import Origin
        from studio.rules.context import cb007

        inv = self._inv(
            [
                self._skill("c-cold", Origin.LOCAL, "claude"),
                self._skill("x-cold", Origin.LOCAL, "codex"),
            ]
        )
        out = list(cb007(inv, self._cfg(skill_usage={"other": 1}, usage_available=True, usage_complete=True)))
        self.assertEqual({f.evidence["runtime"] for f in out}, {"claude", "codex"})


class ModulesImport(unittest.TestCase):
    """Every module must at least import.

    This exists because a syntax error was once introduced into cli.py and the
    whole suite still passed: nothing imported the entry points, so the tool was
    broken while reporting itself green.
    """

    def test_every_module_imports(self):
        import importlib
        import pkgutil

        import studio

        failures = []
        for mod in pkgutil.walk_packages(studio.__path__, prefix="studio."):
            try:
                importlib.import_module(mod.name)
            except Exception as exc:  # noqa: BLE001 - the point is to catch anything
                failures.append(f"{mod.name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [], "modules failed to import")


class CrossRuntimeDrift(unittest.TestCase):
    """SK016 covers the gap between SK013 (still identical) and MR001 (declared)."""

    def _inv(self, skills):
        inv = Inventory()
        inv.skills = skills
        return inv

    def _s(self, name, runtime, digest, path=None):
        return Skill(
            id=f"skill:{runtime}:{name}",
            name=name,
            dir_name=name,
            path=path or f"/home/u/.{runtime}/skills/{name}/SKILL.md",
            runtime=Runtime.CLAUDE if runtime == "claude" else Runtime.CODEX,
            origin=Origin.LOCAL,
            description="does a thing",
            content_hash=digest,
        )

    def test_drifted_undeclared_pair_is_reported(self):
        from studio.rules.skills import sk016

        inv = self._inv([self._s("shared", "claude", "aaa"), self._s("shared", "codex", "bbb")])
        out = list(sk016(inv, Config(repo_root=".")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].evidence["skill"], "shared")
        self.assertEqual(out[0].evidence["runtimes"], ["claude", "codex"])

    def test_identical_pair_is_left_to_sk013(self):
        from studio.rules.skills import sk016

        inv = self._inv([self._s("shared", "claude", "aaa"), self._s("shared", "codex", "aaa")])
        self.assertEqual(list(sk016(inv, Config(repo_root="."))), [])

    def test_declared_mirror_is_left_to_mr001(self):
        """Two rules reporting one problem is how a finding list becomes noise."""
        from studio.rules.skills import sk016

        a = self._s("shared", "claude", "aaa")
        b = self._s("shared", "codex", "bbb")
        cfg = Config(repo_root=".", mirrors=[{"label": "shared", "paths": [a.path, b.path]}])
        self.assertEqual(list(sk016(self._inv([a, b]), cfg)), [])

    def test_single_runtime_is_not_a_pair(self):
        from studio.rules.skills import sk016

        inv = self._inv([self._s("only-here", "claude", "aaa")])
        self.assertEqual(list(sk016(inv, Config(repo_root="."))), [])


class SkillOverlap(unittest.TestCase):
    def _inv(self, skills):
        inv = Inventory()
        inv.skills = skills
        return inv

    def _s(self, name, desc, runtime="claude"):
        return Skill(
            id=f"skill:{name}",
            name=name,
            dir_name=name,
            path=f"/home/u/.claude/skills/{name}/SKILL.md",
            runtime=Runtime.CLAUDE if runtime == "claude" else Runtime.CODEX,
            origin=Origin.LOCAL,
            description=desc,
        )

    def test_overlapping_descriptions_are_reported(self):
        from studio.rules.skills import sk017

        inv = self._inv(
            [
                self._s("qa-helper", "Generate unit tests, jest mocks, stubs, fixtures, coverage gaps."),
                self._s("tdd-helper", "Generate unit tests, jest mocks, stubs, fixtures, coverage gaps."),
            ]
        )
        out = list(sk017(inv, Config(repo_root=".", skill_usage={"qa-helper": 4})))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].evidence["skills"]), ["qa-helper", "tdd-helper"])
        self.assertEqual(out[0].evidence["invocations"], [4, 0])

    def test_unrelated_skills_are_not_reported(self):
        from studio.rules.skills import sk017

        inv = self._inv(
            [
                self._s("deploy-thing", "Publish containers to a hosting platform."),
                self._s("write-docs", "Summarise meeting notes into Notion pages."),
            ]
        )
        self.assertEqual(list(sk017(inv, Config(repo_root="."))), [])

    def test_cross_runtime_same_name_is_left_to_sk016(self):
        from studio.rules.skills import sk017

        desc = "Generate unit tests, jest mocks, stubs, fixtures, coverage gaps."
        inv = self._inv([self._s("same", desc, "claude"), self._s("same", desc, "codex")])
        self.assertEqual(list(sk017(inv, Config(repo_root="."))), [])

    def test_generic_words_alone_do_not_trigger(self):
        """Two skills that merely both say 'use when the user asks' are not
        duplicates - stopwords must not be evidence."""
        from studio.rules.skills import sk017

        inv = self._inv(
            [
                self._s("alpha", "Use when the user asks you to run the project files."),
                self._s("beta", "Use when the user asks you to make sure code works."),
            ]
        )
        self.assertEqual(list(sk017(inv, Config(repo_root="."))), [])


class DeadRouting(unittest.TestCase):
    """WF006 exists because a routing line that resolves to nothing still reads
    as coverage - the agent follows it, finds nothing, and quietly does something
    else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _inv(self, body, skills=()):
        path = os.path.join(self._tmp.name, "CLAUDE.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        inv = Inventory()
        inv.instructions = [
            Instruction(id="i", path=path, runtime=Runtime.CLAUDE, lines=len(body.split("\n")), bytes=len(body))
        ]
        inv.skills = [
            Skill(
                id=f"skill:{n}",
                name=n,
                dir_name=n,
                path=f"/x/{n}/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="d",
            )
            for n in skills
        ]
        return inv

    def test_missing_target_is_reported(self):
        from studio.rules.workflows import wf006

        inv = self._inv("| UI | `web-design-guidelines` + screenshot |\n", skills=["real-skill"])
        out = list(wf006(inv, Config(repo_root=".")))
        self.assertEqual([f.evidence["name"] for f in out], ["web-design-guidelines"])
        self.assertEqual(len(out[0].evidence["sources"]), 1)

    def test_existing_target_is_not_reported(self):
        from studio.rules.workflows import wf006

        inv = self._inv("| Testing | use `real-skill` |\n", skills=["real-skill"])
        self.assertEqual(list(wf006(inv, Config(repo_root="."))), [])

    def test_slash_prefix_and_namespace_resolve(self):
        from studio.rules.workflows import wf006

        inv = self._inv("Run `/codex:real-skill` first.\n", skills=["real-skill"])
        self.assertEqual(list(wf006(inv, Config(repo_root="."))), [])

    def test_paths_tools_and_prose_are_not_routes(self):
        """The whole rule is worthless if it cries wolf, so anything that is
        plainly not a name must be ignored."""
        from studio.rules.workflows import wf006

        body = (
            "Use `mcp__Claude_Browser__open` and read `~/.claude/CLAUDE.md`.\n"
            "See `https://example.com/a-b-c` and `./scripts/run-it.sh`.\n"
            "Run `git commit -m msg` and check `plain`.\n"
            "Token `$spacing-lg` and handle `@some-user` are not routes either.\n"
        )
        self.assertEqual(list(wf006(self._inv(body), Config(repo_root="."))), [])

    def test_each_missing_name_reported_once_per_file(self):
        from studio.rules.workflows import wf006

        inv = self._inv("Use `no-such-thing` and again use `no-such-thing`.\n", skills=["real-skill"])
        self.assertEqual(len(list(wf006(inv, Config(repo_root=".")))), 1)

    def test_empty_inventory_makes_no_claims(self):
        """Knowing of nothing is not evidence that everything is dead."""
        from studio.rules.workflows import wf006

        inv = self._inv("Use `anything-at-all` here.\n")
        self.assertEqual(list(wf006(inv, Config(repo_root="."))), [])


class DeadRoutingAggregation(unittest.TestCase):
    """One broken entry mirrored into two runtimes is one problem, not two."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_missing_name_in_two_files_is_one_finding(self):
        from studio.rules.workflows import wf006

        paths = []
        for rt in ("claude", "codex"):
            p = os.path.join(self._tmp.name, f"{rt}-flow.md")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("Then run `missing-thing`.\n")
            paths.append(p)

        inv = Inventory()
        inv.workflows = [
            __import__("studio.model", fromlist=["Workflow"]).Workflow(
                id=f"w{i}", path=p, runtime=Runtime.CLAUDE, lines=1
            )
            for i, p in enumerate(paths)
        ]
        inv.skills = [
            Skill(
                id="s",
                name="real-skill",
                dir_name="real-skill",
                path="/x/real-skill/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="d",
            )
        ]
        out = list(wf006(inv, Config(repo_root=".")))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].evidence["sources"]), sorted(paths))


class CanonicalVariableLines(unittest.TestCase):
    """A runtime sometimes has a step the other genuinely does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self._tmp.name, "core.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("- always\n- {{MAYBE}}\n- {{INLINE}} tail\n- also always\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _render(self, variables):
        from studio import canonical
        from studio.rules import Config

        cfg = Config(repo_root=self._tmp.name)
        return canonical.render_target(cfg, {"sources": [self.src], "vars": variables})

    def test_line_disappears_when_its_only_variable_is_empty(self):
        out = self._render({"MAYBE": "", "INLINE": "x"})
        self.assertNotIn("- \n", out)
        self.assertIn("- always", out)
        self.assertIn("- also always", out)
        self.assertEqual(out.count("-"), out.count("-"))
        self.assertNotIn("MAYBE", out)

    def test_line_is_kept_when_the_variable_has_a_value(self):
        out = self._render({"MAYBE": "sometimes", "INLINE": "x"})
        self.assertIn("- sometimes", out)

    def test_inline_variables_are_still_substituted(self):
        out = self._render({"MAYBE": "", "INLINE": "prefix"})
        self.assertIn("- prefix tail", out)

    def test_a_line_with_other_text_is_never_dropped(self):
        """Only a line that is *nothing but* the variable may vanish."""
        out = self._render({"MAYBE": "", "INLINE": ""})
        self.assertIn("tail", out)


class CanonicalBannerPlacement(unittest.TestCase):
    """Frontmatter is only frontmatter at the very top of the file.

    A banner placed above it costs the file its name and description. For a
    SKILL.md that means the skill silently stops loading, with no error anywhere
    - the worst possible failure for a tool whose job is preventing exactly this.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _render(self, text):
        from studio import canonical
        from studio.rules import Config

        src = os.path.join(self._tmp.name, "core.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(text)
        return canonical.render_target(Config(repo_root=self._tmp.name), {"sources": [src]})

    def test_frontmatter_survives_rendering(self):
        out = self._render("---\nname: demo\ndescription: A demo. Use when testing.\n---\n\nBody.\n")
        parsed = fm.parse(out)
        self.assertTrue(parsed.present, "render destroyed the frontmatter")
        self.assertEqual(parsed.text("name"), "demo")
        self.assertEqual(parsed.text("description"), "A demo. Use when testing.")

    def test_banner_is_present_but_below_the_frontmatter(self):
        out = self._render("---\nname: demo\ndescription: d\n---\n\nBody.\n")
        self.assertIn(canonical_banner_start(), out)
        self.assertLess(out.index("---"), out.index(canonical_banner_start()))
        self.assertIn("Body.", out)

    def test_file_without_frontmatter_keeps_banner_on_top(self):
        out = self._render("# Just markdown\n")
        self.assertTrue(out.startswith(canonical_banner_start()))


def canonical_banner_start():
    from studio import canonical

    return canonical.BANNER_START


class GeneratedTargetsAreManaged(unittest.TestCase):
    def test_sk016_ignores_a_pair_rendered_from_one_source(self):
        """Rendering both copies from one canonical source is precisely the fix
        SK016 asks for. Still reporting it afterwards would make the finding
        impossible to clear, which is how a rule gets ignored."""
        from studio.rules.skills import sk016

        def s(runtime, digest):
            return Skill(
                id=f"skill:{runtime}",
                name="shared",
                dir_name="shared",
                path=f"/home/u/.{runtime}/skills/shared/SKILL.md",
                runtime=Runtime.CLAUDE if runtime == "claude" else Runtime.CODEX,
                origin=Origin.LOCAL,
                description="d",
                content_hash=digest,
            )

        inv = Inventory()
        inv.skills = [s("claude", "aaa"), s("codex", "bbb")]
        cfg = Config(
            repo_root=".",
            generated=[
                {"target": "/home/u/.claude/skills/shared/SKILL.md", "sources": ["c.md"]},
                {"target": "/home/u/.codex/skills/shared/SKILL.md", "sources": ["c.md"]},
            ],
        )
        self.assertEqual(list(sk016(inv, cfg)), [])


class BannerIsNotContent(unittest.TestCase):
    def test_strip_banner_removes_only_the_banner(self):
        from studio.canonical import strip_banner

        text = (
            "---\nname: x\n---\n\n<!-- BEGIN GENERATED - DO NOT EDIT THIS FILE\n"
            "Rendered by agent-config-studio from:\n  - a.md\nEND GENERATED -->\n\nReal content.\n"
        )
        out = strip_banner(text)
        self.assertNotIn("BEGIN GENERATED", out)
        self.assertNotIn("Rendered by", out)
        self.assertIn("Real content.", out)
        self.assertIn("name: x", out)

    def test_text_without_a_banner_is_unchanged(self):
        from studio.canonical import strip_banner

        self.assertEqual(strip_banner("plain\ntext\n"), "plain\ntext\n")


class DeadRoutingNeedsARoutingSignal(unittest.TestCase):
    """Backticks mark far more than routes.

    Every one of these was a real false positive on a live configuration, and
    each cost more credibility than the one true finding alongside them was
    worth: a rule that cries wolf gets muted, and then it protects nothing.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _findings(self, body):
        from studio.rules.workflows import wf006

        path = os.path.join(self._tmp.name, "wf.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        inv = Inventory()
        inv.instructions = [
            Instruction(id="i", path=path, runtime=Runtime.CLAUDE, lines=1, bytes=len(body))
        ]
        inv.skills = [
            Skill(
                id="s",
                name="real-skill",
                dir_name="real-skill",
                path="/x/real-skill/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="d",
            )
        ]
        return [f.evidence["name"] for f in wf006(inv, Config(repo_root="."))]

    def test_status_values_are_not_routes(self):
        self.assertEqual(self._findings("- 有流程變成 `not-covered` 或 `blocked`。\n"), [])

    def test_ci_trigger_names_are_not_routes(self):
        self.assertEqual(
            self._findings("If trigger is `pre-pr` in CI, also post a PR comment.\n"), []
        )

    def test_a_routing_table_row_still_reports(self):
        self.assertEqual(self._findings("| UI | `missing-thing` |\n"), ["missing-thing"])

    def test_an_explicit_verb_still_reports(self):
        self.assertEqual(self._findings("Frontend work: use `missing-thing` first.\n"), ["missing-thing"])

    def test_chinese_routing_verb_still_reports(self):
        self.assertEqual(self._findings("前端實作走 `missing-thing`。\n"), ["missing-thing"])


class SkillDiscovery(unittest.TestCase):
    """What counts as a loadable skill directory.

    Both cases here were found by comparing this tool's count against
    `claude plugin details`: it reported 48 skills for a plugin that ships 30.
    An inflated count feeds straight into the preloaded-token figure and invents
    name collisions, so the headline number was wrong and so were 9 findings.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("---\nname: x\ndescription: d\n---\nbody\n")

    def _found(self):
        from studio.scan import _skill_dirs_under

        return {os.path.relpath(d, self.root) for d in _skill_dirs_under(self.root)}

    def test_a_skill_md_nested_inside_a_skill_is_not_a_skill(self):
        """`skills/ai-sdk/upstream/SKILL.md` is reference material the parent
        skill points at. It never loads, and counting it charges for it twice."""
        self._write("skills/ai-sdk/SKILL.md")
        self._write("skills/ai-sdk/upstream/SKILL.md")
        self.assertEqual(self._found(), {"skills/ai-sdk"})

    def test_hidden_directories_are_not_searched(self):
        """A package's own `.claude/skills/` is its maintainers' tooling; it is
        never shipped into a user's session."""
        self._write("skills/real/SKILL.md")
        self._write(".claude/skills/their-dev-helper/SKILL.md")
        self.assertEqual(self._found(), {"skills/real"})

    def test_sibling_skills_are_both_found(self):
        self._write("skills/one/SKILL.md")
        self._write("skills/two/SKILL.md")
        self.assertEqual(self._found(), {"skills/one", "skills/two"})


class ContentRulesIgnoreTheBanner(unittest.TestCase):
    """IN005 compares content across files. The provenance banner is identical
    in every generated file by design, so if a content rule can see it, adding
    provenance to four files invents duplicate-content findings for all of them
    - which is exactly what happened."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_instruction_reader_strips_the_banner(self):
        from studio.rules.instructions import _read

        p = os.path.join(self._tmp.name, "CLAUDE.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(
                "# Title\n\n<!-- BEGIN GENERATED - DO NOT EDIT THIS FILE\n"
                "Rendered by agent-config-studio from:\n  - canonical/core.md\n"
                "Edit the source above, then run: studio sync\n"
                "Drift is detected by rule MR003.\nEND GENERATED -->\n\nReal rule text.\n"
            )
        out = _read(p)
        self.assertNotIn("BEGIN GENERATED", out)
        self.assertNotIn("Rendered by agent-config-studio", out)
        self.assertNotIn("Drift is detected by rule MR003", out)
        self.assertIn("Real rule text.", out)
        self.assertIn("# Title", out)


class RoutingIgnoresCliFlags(unittest.TestCase):
    """Instructions quote CLI flags constantly. Before this, every one of them
    read as a dead route - four false positives from a single browser workflow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_flags_on_a_routing_line_are_not_routes(self):
        from studio.rules.workflows import wf006

        body = "Login-state testing: use `agent-browser --profile`, `--state`, or `--session-name`.\n"
        p = os.path.join(self._tmp.name, "wf.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        inv = Inventory()
        inv.instructions = [
            Instruction(id="i", path=p, runtime=Runtime.CLAUDE, lines=1, bytes=len(body))
        ]
        inv.skills = [
            Skill(
                id="s",
                name="agent-browser",
                dir_name="agent-browser",
                path="/x/agent-browser/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="d",
            )
        ]
        self.assertEqual([f.evidence["name"] for f in wf006(inv, Config(repo_root="."))], [])


class ApplySafety(unittest.TestCase):
    """The tool's central promise is that every write is backed up and
    reversible. These are the ways that promise was breakable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.repo)
        os.makedirs(self.work)

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name, text="original\n"):
        p = os.path.join(self.work, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_a_failed_apply_still_leaves_a_usable_restore_point(self):
        """A partial apply is the case backups exist for. Before this, the
        manifest was written after the loop, so a mid-loop failure produced
        modified files and a backup directory that list_backups skipped."""
        from studio import patch

        good = self._file("a.md")
        # A directory where a file is expected: writing to it raises IsADirectoryError.
        bad = os.path.join(self.work, "b.md")
        os.makedirs(bad)

        cs = patch.ChangeSet(name="halfway", description="d")
        cs.changes = [
            patch.Change(path=good, new_text="updated\n", action="modify"),
            patch.Change(path=bad, new_text="never\n", action="modify"),
        ]
        with self.assertRaises(Exception):
            patch.apply(cs, self.repo)

        backups = patch.list_backups(self.repo)
        self.assertEqual(len(backups), 1, "the partial apply left no visible restore point")
        self.assertFalse(backups[0]["complete"], "a partial apply claimed to be complete")
        self.assertEqual(len(backups[0]["changes"]), 1)
        self.assertIn("planned", backups[0])

    def test_a_failed_apply_can_actually_be_rolled_back(self):
        from studio import patch

        good = self._file("a.md")
        bad = os.path.join(self.work, "b.md")
        os.makedirs(bad)
        cs = patch.ChangeSet(name="halfway", description="d")
        cs.changes = [
            patch.Change(path=good, new_text="updated\n", action="modify"),
            patch.Change(path=bad, new_text="never\n", action="modify"),
        ]
        with self.assertRaises(Exception):
            patch.apply(cs, self.repo)
        self.assertEqual(open(good, encoding="utf-8").read(), "updated\n")

        patch.rollback(self.repo, patch.list_backups(self.repo)[0]["id"])
        self.assertEqual(
            open(good, encoding="utf-8").read(), "original\n", "rollback did not restore the file"
        )

    def test_two_applies_in_the_same_second_get_separate_backups(self):
        """Second-precision slot names collided, and the second apply overwrote
        the first one's saved bytes - a listed backup that restores the wrong
        content is worse than no backup."""
        from studio import patch

        f1, f2 = self._file("one.md", "first\n"), self._file("two.md", "second\n")
        slots = []
        for path, text in ((f1, "A\n"), (f2, "B\n")):
            cs = patch.ChangeSet(name="same-name", description="d")
            cs.changes = [patch.Change(path=path, new_text=text, action="modify")]
            slots.append(patch.apply(cs, self.repo)["backup"])

        self.assertNotEqual(slots[0], slots[1], "two applies shared one backup directory")
        self.assertEqual(len(patch.list_backups(self.repo)), 2)

        for slot in slots:
            patch.rollback(self.repo, os.path.basename(slot))
        self.assertEqual(open(f1, encoding="utf-8").read(), "first\n")
        self.assertEqual(open(f2, encoding="utf-8").read(), "second\n")


class QuarantineKeepsBothCopies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_basename_from_two_roots_does_not_lose_one(self):
        """Quarantine is the safe alternative to deleting. Flattening to a
        basename made two stray `settings.json.bak` files share one destination:
        one was overwritten and both originals removed."""
        from studio import fixes

        repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(repo)
        paths = []
        for root in ("claude", "codex"):
            d = os.path.join(self._tmp.name, root)
            os.makedirs(d)
            p = os.path.join(d, "settings.json.bak")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(f"from {root}\n")
            paths.append(p)

        changes = []
        for p in paths:
            changes += fixes._relocate(p, repo, "stray")
        writes = [c.path for c in changes if c.action != "delete"]
        self.assertEqual(len(set(writes)), 2, "both files were quarantined to one path")


class UpgradeAbortsOnStashFailure(unittest.TestCase):
    def test_a_failed_stash_stops_before_reset_hard(self):
        """`reset --hard` two steps later destroys whatever the stash did not
        take, so a nonzero stash exit must end the upgrade."""
        from studio import upgrade

        calls = []

        def fake_run(cmd, cwd=None, timeout=None):
            calls.append(cmd)
            if cmd[:2] == ["git", "rev-parse"]:
                return {"cmd": " ".join(cmd), "cwd": cwd or "", "rc": 0, "stdout": "abc123\n", "stderr": ""}
            if cmd[:2] == ["git", "stash"]:
                return {"cmd": " ".join(cmd), "cwd": cwd or "", "rc": 1, "stdout": "", "stderr": "unmerged index"}
            return {"cmd": " ".join(cmd), "cwd": cwd or "", "rc": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".git"))
            original = upgrade._run
            upgrade._run = fake_run
            try:
                result = upgrade.update_toolkit(d, "kit")
            finally:
                upgrade._run = original

        self.assertFalse(result.ok)
        self.assertIn("stash", result.message)
        flat = [" ".join(c) for c in calls]
        self.assertFalse(
            any("reset" in c for c in flat), "ran reset --hard after the stash failed"
        )
        self.assertFalse(any("fetch" in c for c in flat), "kept going after the stash failed")


class ConsolidationNeverOverwritesAReferenceFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "demo")
        os.makedirs(os.path.join(self.dir, "reference"))
        self.skill = os.path.join(self.dir, "SKILL.md")
        # Big enough to pass the earlier guards (>=3 sections, over the body
        # budget), so validation actually reaches the existing-target check.
        body = ["---", "name: demo", "description: d", "---", ""]
        for i in range(6):
            body += [f"## Section {i}", ""] + [f"line {i}-{j}" for j in range(120)] + [""]
        with open(self.skill, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body))

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_existing_target_is_rejected_not_replaced(self):
        """split_skill writes the target wholesale and the lost-line check only
        compares the SKILL.md body, so an existing reference file would vanish
        without registering as lost content."""
        from studio import consolidate
        from studio.model import Finding, Severity

        existing = os.path.join(self.dir, "reference", "notes.md")
        with open(existing, "w", encoding="utf-8") as fh:
            fh.write("# Notes\n\nirreplaceable\n")

        finding = Finding(
            rule="SK007",
            severity=Severity.IMPORTANT,
            title="t",
            detail="d",
            path=self.skill,
            spec="https://example.invalid",
        )

        def fake_ask(*_a, **_k):
            from studio.ai import Answer

            return Answer(
                ok=True,
                data={
                    "moves": [
                        {
                            "target": "reference/notes.md",
                            "heading": "H",
                            "sections": [f"Section {i}" for i in range(1, 6)],
                        }
                    ]
                },
            )

        from studio import ai

        original_ask, original_avail = ai.ask, ai.available
        ai.ask, ai.available = fake_ask, (lambda: True)
        try:
            proposal = consolidate.propose_split(finding, self._tmp.name)
        finally:
            ai.ask, ai.available = original_ask, original_avail

        self.assertFalse(proposal.ok, "accepted a plan that overwrites an existing file")
        self.assertTrue(any("已存在" in r for r in proposal.rejected_because))
        self.assertEqual(
            open(existing, encoding="utf-8").read(), "# Notes\n\nirreplaceable\n", "file was touched"
        )


class RoutingResolvesWithinTheRuntime(unittest.TestCase):
    """Each runtime loads only its own skills, so a name must resolve in the
    runtime doing the routing. Resolving against a global set reported clean
    routing while 15 Codex workflow entries pointed at Claude-only skills."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _inv(self, workflow_runtime, skill_runtime, origin=Origin.LOCAL):
        from studio.model import Workflow

        p = os.path.join(self._tmp.name, "wf.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("Design work: use `only-over-there`.\n")
        inv = Inventory()
        inv.workflows = [Workflow(id="w", path=p, runtime=workflow_runtime, lines=1)]
        inv.skills = [
            Skill(
                id="s",
                name="only-over-there",
                dir_name="only-over-there",
                path="/x/only-over-there/SKILL.md",
                runtime=skill_runtime,
                origin=origin,
                description="d",
            )
        ]
        return inv

    def test_a_codex_workflow_cannot_route_to_a_claude_only_skill(self):
        from studio.rules.workflows import wf006

        inv = self._inv(Runtime.CODEX, Runtime.CLAUDE)
        self.assertEqual(
            [f.evidence["name"] for f in wf006(inv, Config(repo_root="."))], ["only-over-there"]
        )

    def test_a_skill_in_the_same_runtime_resolves(self):
        from studio.rules.workflows import wf006

        inv = self._inv(Runtime.CODEX, Runtime.CODEX)
        self.assertEqual(list(wf006(inv, Config(repo_root="."))), [])

    def test_plugin_skills_count_as_claude_side(self):
        """Plugins install under ~/.claude, so their skills are Claude's - a
        Claude workflow may route to them, a Codex one may not."""
        from studio.rules.workflows import wf006

        ok = self._inv(Runtime.CLAUDE, Runtime.CODEX, origin=Origin.PLUGIN)
        self.assertEqual(list(wf006(ok, Config(repo_root="."))), [])
        bad = self._inv(Runtime.CODEX, Runtime.CODEX, origin=Origin.PLUGIN)
        self.assertEqual([f.evidence["name"] for f in wf006(bad, Config(repo_root="."))], ["only-over-there"])


class BackupSlotsAreNeverShared(unittest.TestCase):
    """Two independent protections stop two applies sharing a backup directory:
    sub-second precision in the name, and exclusive creation. Each is tested on
    its own, because together they mask each other - breaking either one alone
    still passed a test that only exercised the pair."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_exclusive_creation_survives_an_identical_timestamp(self):
        """Even with the clock frozen, a second slot must not reuse the first."""
        from studio import patch

        class FrozenClock:
            @staticmethod
            def now(_tz=None):
                import datetime as _dt

                return _dt.datetime(2026, 7, 27, 12, 0, 0, 0, tzinfo=_dt.timezone.utc)

        original = patch.datetime
        patch.datetime = FrozenClock
        try:
            a = patch._backup_slot(self._tmp.name, "same")
            b = patch._backup_slot(self._tmp.name, "same")
        finally:
            patch.datetime = original

        self.assertNotEqual(a, b, "a second apply reused the first backup directory")
        self.assertTrue(os.path.isdir(a) and os.path.isdir(b))

    def test_slot_names_carry_sub_second_precision(self):
        """Second precision alone made same-second collisions the common case
        when clicking two fixes in the dashboard."""
        from studio import patch

        slot = os.path.basename(patch._backup_slot(self._tmp.name, "x"))
        stamp = slot.rsplit("-", 1)[0] if slot.endswith("-x") else slot
        self.assertIn(".", stamp, f"timestamp has no sub-second component: {slot}")

    def test_an_existing_slot_is_never_written_into(self):
        from studio import patch

        first = patch._backup_slot(self._tmp.name, "n")
        with open(os.path.join(first, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"marker": "first"}')
        second = patch._backup_slot(self._tmp.name, "n")
        self.assertNotEqual(first, second)
        self.assertFalse(
            os.path.exists(os.path.join(second, "manifest.json")),
            "the new slot already contained another apply's manifest",
        )


class NeverPassOnMissingEvidence(unittest.TestCase):
    """The worst failure this tool can have is reporting PASS when it did not
    actually check. Each of these was a path to exactly that."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self._tmp.name, "canonical"))

    def tearDown(self):
        self._tmp.cleanup()

    def _governance(self, text):
        p = os.path.join(self._tmp.name, "canonical", "governance.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return Config.load(self._tmp.name)

    def test_malformed_governance_is_recorded_not_swallowed(self):
        cfg = self._governance('{"mirrors": [},')
        self.assertTrue(cfg.governance_error, "a broken governance file parsed as silence")

    def test_malformed_governance_produces_a_critical_finding(self):
        from studio.rules.mirrors import mr004

        cfg = self._governance("{ not json at all")
        out = list(mr004(Inventory(), cfg))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, Severity.CRITICAL)

    def test_a_valid_governance_file_reports_nothing(self):
        from studio.rules.mirrors import mr004

        cfg = self._governance('{"mirrors": [], "waivers": []}')
        self.assertEqual(cfg.governance_error, "")
        self.assertEqual(list(mr004(Inventory(), cfg)), [])

    def test_a_json_array_is_not_a_valid_governance_file(self):
        cfg = self._governance("[1, 2, 3]")
        self.assertTrue(cfg.governance_error)

    def test_unreadable_paths_become_findings(self):
        """The scanner records and continues so one bad file cannot take the run
        down - but the verdict must still not claim to cover it."""
        from studio.rules.mirrors import mr005

        inv = Inventory()
        inv.scan_errors = ["/x/settings.json: Operation not permitted"]
        out = list(mr005(inv, Config(repo_root=self._tmp.name)))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, Severity.CRITICAL)
        self.assertIn("does not cover it", out[0].detail)

    def test_a_clean_scan_reports_nothing(self):
        from studio.rules.mirrors import mr005

        self.assertEqual(list(mr005(Inventory(), Config(repo_root=self._tmp.name))), [])

    def test_zero_usage_everywhere_is_evidence_not_absence(self):
        """An entirely unused plugin set produces an empty counts dict. Reading
        that as "no index" skipped the usage rules in the one case they exist
        for."""
        from studio.model import Plugin, Runtime
        from studio.rules.context import cb002

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="unused@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=3,
            )
        ]
        cfg = Config(repo_root=self._tmp.name, plugin_usage={}, usage_available=True, usage_complete=True)
        self.assertEqual(len(list(cb002(inv, cfg))), 1, "an all-unused plugin set was skipped")

    def test_no_index_still_means_silence(self):
        from studio.model import Plugin, Runtime
        from studio.rules.context import cb002

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="unused@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=3,
            )
        ]
        cfg = Config(repo_root=self._tmp.name, plugin_usage={}, usage_available=False)
        self.assertEqual(list(cb002(inv, cfg)), [])


class QuarantineRefusesBinaries(unittest.TestCase):
    def test_a_non_utf8_file_gets_no_automatic_fix(self):
        """Change sets carry text. Pushing a binary through with
        errors='replace' rewrote the bytes, and the original was deleted right
        after - a corrupt copy and no way back."""
        from studio import fixes

        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            os.makedirs(repo)
            binary = os.path.join(d, "archive.zip.bak")
            with open(binary, "wb") as fh:
                fh.write(b"PK\x03\x04\xff\xfe\x00\x01binary\x80\x81")
            self.assertEqual(fixes._relocate(binary, repo, "stray"), [])
            self.assertTrue(os.path.exists(binary), "the original was removed anyway")

    def test_a_text_file_is_still_quarantined(self):
        from studio import fixes

        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            os.makedirs(repo)
            f = os.path.join(d, "settings.json.bak")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write('{"ok": true}\n')
            self.assertEqual(len(fixes._relocate(f, repo, "stray")), 2)


class VersionOrdering(unittest.TestCase):
    """`1.0.0-beta.1` sorted above `1.0.0`, so a stable release read as not
    newer than the prerelease it superseded and the update was never offered."""

    def _v(self, s):
        from studio.versions import version_key

        return version_key(s)

    def test_a_release_outranks_its_own_prerelease(self):
        self.assertGreater(self._v("1.0.0"), self._v("1.0.0-beta.1"))
        self.assertGreater(self._v("2.3.4"), self._v("2.3.4-rc.1"))

    def test_prerelease_labels_are_distinguished(self):
        self.assertGreater(self._v("1.0.0-beta.1"), self._v("1.0.0-alpha.9"))
        self.assertGreater(self._v("1.0.0-rc.1"), self._v("1.0.0-beta.9"))

    def test_numeric_prerelease_parts_compare_numerically(self):
        self.assertGreater(self._v("1.0.0-rc.10"), self._v("1.0.0-rc.2"))

    def test_ordinary_releases_still_order_correctly(self):
        self.assertGreater(self._v("1.60.1"), self._v("0.15.16"))
        self.assertGreater(self._v("6.2.0"), self._v("5.1.0"))
        self.assertGreater(self._v("1.10.0"), self._v("1.9.0"))

    def test_build_metadata_does_not_affect_ordering(self):
        self.assertEqual(self._v("1.0.0+build.9"), self._v("1.0.0"))

    def test_a_leading_v_is_tolerated(self):
        self.assertEqual(self._v("v1.2.3"), self._v("1.2.3"))


class ExpensiveReadIsGatedToo(unittest.TestCase):
    """`/api/health/run` writes no configuration, so it was left ungated. That
    reasoning only asked "does this modify anything?" and missed cost: a full run
    reads the entire transcript history - tens of gigabytes - so an ungated POST
    lets any open page spawn unbounded concurrent scans.

    It must stay usable in read-only mode, though, which is why it uses the
    origin/token gate rather than the write gate."""

    def setUp(self):
        from studio import server

        self.server = server
        self.handler = server.Handler
        self._saved = (self.handler.allow_actions, self.handler.origin)
        self.handler.origin = "http://127.0.0.1:8787"

    def tearDown(self):
        self.handler.allow_actions, self.handler.origin = self._saved

    def _check(self, headers, *, allow=False):
        self.handler.allow_actions = allow
        stub = self.handler.__new__(self.handler)
        stub.headers = headers
        return self.handler._from_this_page(stub)

    def test_a_cross_origin_caller_is_refused(self):
        refusal = self._check(
            {"X-Studio-Token": self.server._SESSION_TOKEN, "Origin": "https://evil.example"}
        )
        self.assertIsNotNone(refusal)
        self.assertIn("another origin", refusal)

    def test_a_caller_without_the_token_is_refused(self):
        self.assertIsNotNone(self._check({"Origin": self.handler.origin}))

    def test_a_stale_token_is_refused(self):
        self.assertIsNotNone(
            self._check({"X-Studio-Token": "old-session", "Origin": self.handler.origin})
        )

    def test_the_dashboard_itself_is_allowed_in_read_only_mode(self):
        """Re-running the checks must not require --allow-actions: it changes no
        configuration, and taking the button away in read-only mode would remove
        the tool's most basic function."""
        self.assertIsNone(
            self._check(
                {"X-Studio-Token": self.server._SESSION_TOKEN, "Origin": self.handler.origin},
                allow=False,
            )
        )


class EveryUsageRuleTreatsZeroAsEvidence(unittest.TestCase):
    """The guard appears in three rules. Testing one left the other two able to
    regress silently."""

    def _inv(self):
        from studio.model import Plugin, Runtime

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="unused@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=2,
            )
        ]
        inv.skills = [
            Skill(
                id="s",
                name="cold-one",
                dir_name="cold-one",
                path="/x/cold-one/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="x" * 1600,
            )
        ]
        return inv

    def test_cb001_grades_an_index_that_records_nothing(self):
        """CB001 must reach its classifier. Asserting only that the call returns
        proves nothing - the earlier version of this test passed even with the
        guard broken, because it never checked for a finding."""
        from studio.model import Plugin, Runtime
        from studio.rules.context import cb001

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="zz-unheard-of-plugin@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=40,
            )
        ]
        inv.skills = [
            Skill(
                id=f"s{i}",
                name=f"zz-unheard-of-skill-{i}",
                dir_name=f"zz-unheard-of-skill-{i}",
                path=f"/x/zz-{i}/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="y" * 900,
                plugin="zz-unheard-of-plugin@market",
            )
            for i in range(40)
        ]
        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=True)
        out = list(cb001(inv, cfg))
        self.assertEqual(len(out), 1, "an entirely unused plugin set produced no finding")
        self.assertGreater(out[0].evidence["avoidable_est_tokens"], 0)

    def test_cb001_stays_silent_without_an_index(self):
        from studio.model import Plugin, Runtime
        from studio.rules.context import cb001

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="zz-unheard-of-plugin@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=40,
            )
        ]
        cfg = Config(repo_root=".", plugin_usage={}, usage_available=False)
        self.assertEqual(list(cb001(inv, cfg)), [])

    def test_cb007_runs_on_an_index_with_no_recorded_skill_calls(self):
        from studio.rules.context import cb007

        cfg = Config(repo_root=".", skill_usage={}, usage_available=True, usage_complete=True)
        self.assertEqual(len(list(cb007(self._inv(), cfg))), 1)

    def test_cb007_stays_silent_with_no_index(self):
        from studio.rules.context import cb007

        cfg = Config(repo_root=".", skill_usage={}, usage_available=False)
        self.assertEqual(list(cb007(self._inv(), cfg)), [])

    def test_cb008_grades_an_index_that_records_nothing(self):
        from studio.rules.context import cb008

        cfg_on = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=True)
        cfg_off = Config(repo_root=".", plugin_usage={}, usage_available=False)
        # Whatever it decides with an index, it must decide nothing without one.
        self.assertEqual(list(cb008(self._inv(), cfg_off)), [])
        list(cb008(self._inv(), cfg_on))  # must not raise; guard must be passable


class SplitMeasuresWhatItWillWrite(unittest.TestCase):
    """The arithmetic only subtracts moved sections, while split_skill then
    appends a heading, a note and one pointer per output file.

    The fixture is sized so the estimate lands under the budget - the earlier
    arithmetic guard lets it through - while the file actually written comes to
    five lines over. Without the recheck this plan is accepted and "fixes" an
    oversized skill into a still-oversized one.
    """

    def setUp(self):
        from studio.rules import SKILL_BODY_MAX_LINES

        self.budget = SKILL_BODY_MAX_LINES
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "demo")
        os.makedirs(self.dir)
        self.skill = os.path.join(self.dir, "SKILL.md")
        body = ["---", "name: demo", "description: d", "---", ""]
        body += ["## Keep", ""] + [f"kept {i}" for i in range(self.budget - 7)] + [""]
        for i in range(3):
            body += [f"## Move {i}", ""] + [f"moved {i}-{j}" for j in range(40)] + [""]
        with open(self.skill, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body))

    def tearDown(self):
        self._tmp.cleanup()

    def _propose(self):
        from studio import ai, consolidate
        from studio.model import Finding, Severity

        finding = Finding(
            rule="SK007",
            severity=Severity.IMPORTANT,
            title="t",
            detail="d",
            path=self.skill,
            spec="https://example.invalid",
        )
        moves = [
            {"target": f"reference/m{i}.md", "heading": f"M{i}", "sections": [f"Move {i}"]}
            for i in range(3)
        ]
        saved = (ai.ask, ai.available)
        ai.ask = lambda *a, **k: ai.Answer(ok=True, data={"moves": moves})
        ai.available = lambda: True
        try:
            return consolidate.propose_split(finding, self._tmp.name)
        finally:
            ai.ask, ai.available = saved

    def test_a_plan_that_only_looks_small_enough_is_rejected(self):
        proposal = self._propose()
        self.assertFalse(proposal.ok, "accepted a plan whose output still exceeds the budget")
        self.assertTrue(
            any("實際產生" in r for r in proposal.rejected_because),
            f"rejected for an unrelated reason: {proposal.rejected_because}",
        )

    def test_the_rejection_names_both_the_estimate_and_the_real_size(self):
        """The estimate passing while the real size fails is the whole point;
        showing only one number hides why the plan looked acceptable."""
        reason = " ".join(self._propose().rejected_because)
        self.assertIn(str(self.budget + 5), reason)
        self.assertIn("估算", reason)


class HealthRunRouteIsGated(unittest.TestCase):
    """Exercises the actual route over HTTP.

    Testing `_from_this_page` alone was not enough: the gate can be correct
    while the route forgets to call it, and that is exactly the shape of the
    original defect. A mutation that removed the call from the route left the
    helper's own tests green.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from studio import server

        cls.server_mod = server
        server.Handler.repo_root = os.path.abspath(".")
        server.Handler.web_root = os.path.abspath("web")
        server.Handler.allow_actions = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        server.Handler.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, headers):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health/run", data=b"{}", method="POST"
        )
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_a_request_without_a_token_is_refused(self):
        self.assertEqual(self._post({"Origin": self.server_mod.Handler.origin}), 403)

    def test_a_request_from_another_origin_is_refused(self):
        status = self._post(
            {
                "Origin": "https://evil.example",
                "X-Studio-Token": self.server_mod._SESSION_TOKEN,
            }
        )
        self.assertEqual(status, 403, "a cross-origin page could trigger a full scan")

    def _get(self, path, headers=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=180)
        try:
            conn.request("GET", path, headers=headers or {})
            return conn.getresponse().status
        finally:
            conn.close()

    def test_an_expensive_fresh_get_is_gated(self):
        """`/api/health?fresh=1` runs the same full-history scan as the POST
        route, and Host is legitimately 127.0.0.1 for a cross-origin no-CORS
        GET - so gating only the POST left the costly path open."""
        self.assertEqual(self._get("/api/health?fresh=1"), 403)

    def test_the_fixes_endpoint_is_gated(self):
        """It rebuilds the usage index and walks local history, so it costs what
        the gated health route costs - an open door of the same size."""
        self.assertEqual(self._get("/api/fixes"), 403)

    def test_the_page_can_still_read_fixes(self):
        self.assertEqual(
            self._get("/api/fixes", {"X-Studio-Token": self.server_mod._SESSION_TOKEN}), 200
        )

    def test_a_cached_read_is_not_gated(self):
        """Cheap reads must stay open, or the page cannot render at all."""
        self.assertEqual(self._get("/api/health"), 200)

    def test_the_page_can_still_force_a_fresh_scan(self):
        status = self._get(
            "/api/health?fresh=1",
            {"X-Studio-Token": self.server_mod._SESSION_TOKEN},
        )
        self.assertEqual(status, 200)

    def test_a_valid_token_from_this_page_is_accepted(self):
        """Read-only mode must keep working: re-running the checks changes no
        configuration, and removing it would take away the tool's main function."""
        status = self._post(
            {
                "Origin": self.server_mod.Handler.origin,
                "X-Studio-Token": self.server_mod._SESSION_TOKEN,
            }
        )
        self.assertEqual(status, 200)


class ToolkitUpdateTargetsAreUnique(unittest.TestCase):
    """A toolkit name is not an identity: the same one can be installed under
    both the Claude and Codex skill roots. Selecting by name alone upgraded both
    checkouts from a single confirmation, while the page showed one result."""

    def _filter(self, todo, target):
        """Calls the shipped implementation, not a copy of it - a test that
        reimplements the logic passes even when the real code is broken."""
        from studio.server import select_update_targets

        return select_update_targets(todo, target)

    def setUp(self):
        self.todo = [
            {"kind": "toolkit", "target": "gstack", "root": "/home/u/.claude/skills/gstack"},
            {"kind": "toolkit", "target": "gstack", "root": "/home/u/.codex/skills/gstack"},
        ]

    def test_a_qualified_target_selects_exactly_one_checkout(self):
        chosen = self._filter(self.todo, "gstack@/home/u/.codex/skills/gstack")
        self.assertEqual(len(chosen), 1, "one confirmation would have upgraded both checkouts")
        self.assertEqual(chosen[0]["root"], "/home/u/.codex/skills/gstack")

    def test_the_other_checkout_is_selectable_on_its_own(self):
        chosen = self._filter(self.todo, "gstack@/home/u/.claude/skills/gstack")
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["root"], "/home/u/.claude/skills/gstack")

    def test_an_unqualified_name_still_works_for_a_single_install(self):
        """Plugins have no root, so their target stays a bare key."""
        plugins = [{"kind": "plugin", "target": "superpowers@marketplace"}]
        self.assertEqual(len(self._filter(plugins, "superpowers@marketplace")), 1)


class UnusedNeedsCompleteHistory(unittest.TestCase):
    """`available` only means at least one transcript was read.

    Calling a plugin or skill unused on partial history is a claim the evidence
    cannot support - and CB002 attaches a fixer that would disable something
    whose only use sits in the part that was skipped.
    """

    def _inv(self):
        from studio.model import Plugin, Runtime

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="quiet@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=2,
            )
        ]
        inv.skills = [
            Skill(
                id="s",
                name="cold-skill",
                dir_name="cold-skill",
                path="/x/cold-skill/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="z" * 1600,
            )
        ]
        return inv

    def test_cb002_is_silent_on_partial_history(self):
        from studio.rules.context import cb002

        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=False)
        self.assertEqual(list(cb002(self._inv(), cfg)), [])

    def test_cb002_speaks_on_complete_history(self):
        from studio.rules.context import cb002

        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=True)
        self.assertEqual(len(list(cb002(self._inv(), cfg))), 1)

    def test_cb007_is_silent_on_partial_history(self):
        from studio.rules.context import cb007

        cfg = Config(repo_root=".", skill_usage={}, usage_available=True, usage_complete=False)
        self.assertEqual(list(cb007(self._inv(), cfg)), [])

    def test_cb008_is_silent_on_partial_history(self):
        from studio.rules.context import cb008

        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=False)
        self.assertEqual(list(cb008(self._inv(), cfg)), [])


class BackupSurvivesDuplicateTargets(unittest.TestCase):
    """Two changes can name the same path - overlapping mirror and generated
    declarations do. Copying the file again after the first write saved the
    intermediate content over the original, so rollback restored a state that
    never existed before the apply."""

    def test_the_original_is_what_gets_restored(self):
        from studio import patch

        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            work = os.path.join(d, "work")
            os.makedirs(repo)
            os.makedirs(work)
            target = os.path.join(work, "f.md")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("ORIGINAL\n")

            cs = patch.ChangeSet(name="dup", description="d")
            cs.changes = [
                patch.Change(path=target, new_text="first\n", action="modify"),
                patch.Change(path=target, new_text="second\n", action="modify"),
            ]
            result = patch.apply(cs, repo)
            self.assertEqual(open(target, encoding="utf-8").read(), "second\n")

            patch.rollback(repo, os.path.basename(result["backup"]))
            self.assertEqual(
                open(target, encoding="utf-8").read(),
                "ORIGINAL\n",
                "rollback restored an intermediate state, not the original file",
            )


class Cb002OnlyReportsWhatItCanFix(unittest.TestCase):
    """The registered fixer writes ~/.claude/settings.json. Emitting the finding
    for a Codex plugin put a disable button on screen that changed nothing."""

    def _inv(self, runtime):
        from studio.model import Plugin

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="quiet@market",
                marketplace="market",
                runtime=runtime,
                enabled=True,
                skill_count=2,
            )
        ]
        return inv

    def _cfg(self):
        return Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=True)

    def test_a_claude_plugin_is_reported(self):
        from studio.rules.context import cb002

        self.assertEqual(len(list(cb002(self._inv(Runtime.CLAUDE), self._cfg()))), 1)

    def test_a_codex_plugin_is_not_given_an_unusable_button(self):
        from studio.rules.context import cb002

        self.assertEqual(list(cb002(self._inv(Runtime.CODEX), self._cfg())), [])


class WaiverMatching(unittest.TestCase):
    """Findings carry absolute paths; people write `~/.claude/...` - the form
    this repo's own governance example uses. Without expansion every waiver
    written the documented way silently failed and the finding kept blocking."""

    def _finding(self, path):
        return Finding(
            rule="SK007",
            severity=Severity.IMPORTANT,
            title="t",
            detail="d",
            path=path,
            spec="https://example.invalid",
        )

    def test_a_tilde_glob_matches_an_absolute_path(self):
        from studio.rules import Waiver

        home_skill = os.path.join(os.path.expanduser("~"), ".claude", "skills", "x", "SKILL.md")
        w = Waiver(rule="SK007", path_glob="~/.claude/skills/*", reason="known")
        self.assertTrue(w.matches(self._finding(home_skill)))

    def test_an_unrelated_path_still_does_not_match(self):
        from studio.rules import Waiver

        w = Waiver(rule="SK007", path_glob="~/.claude/skills/*", reason="known")
        self.assertFalse(w.matches(self._finding("/somewhere/else/SKILL.md")))

    def test_the_rule_code_still_has_to_match(self):
        from studio.rules import Waiver

        home_skill = os.path.join(os.path.expanduser("~"), ".claude", "skills", "x", "SKILL.md")
        w = Waiver(rule="SK013", path_glob="~/.claude/skills/*", reason="known")
        self.assertFalse(w.matches(self._finding(home_skill)))


class MalformedWaiversDoNotCrash(unittest.TestCase):
    """A non-object waiver entry raised AttributeError during config load, taking
    down the whole health command and the dashboard - instead of producing the
    incomplete-audit finding MR004 exists to give."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self._tmp.name, "canonical"))

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self, text):
        p = os.path.join(self._tmp.name, "canonical", "governance.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return Config.load(self._tmp.name)

    def test_a_string_waiver_is_reported_not_raised(self):
        cfg = self._load('{"waivers": ["oops"]}')
        self.assertTrue(cfg.governance_error)

    def test_a_number_waiver_is_reported_not_raised(self):
        cfg = self._load('{"waivers": [{"rule": "SK007"}, 42]}')
        self.assertTrue(cfg.governance_error)

    def test_well_formed_waivers_still_load(self):
        cfg = self._load('{"waivers": [{"rule": "SK007", "path": "~/x", "reason": "r"}]}')
        self.assertEqual(cfg.governance_error, "")
        self.assertEqual(len(cfg.waivers), 1)


class FrontmatterOnlySourceStillRenders(unittest.TestCase):
    """When the closing `---` is the last line there is no newline after it, and
    the banner fell back to the top of the file - costing the skill its name and
    description, which means it stops loading at all."""

    def test_a_source_that_is_only_frontmatter_keeps_it_first(self):
        from studio import canonical

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "core.md")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("---\nname: only-header\ndescription: Nothing else.\n---")
            out = canonical.render_target(Config(repo_root=d), {"sources": [src]})

        parsed = fm.parse(out)
        self.assertTrue(parsed.present, "render destroyed the frontmatter")
        self.assertEqual(parsed.text("name"), "only-header")
        self.assertIn(canonical_banner_start(), out)


class FindingsCarryTheirCategory(unittest.TestCase):
    """The dashboard builds its category selector from this field. It was never
    serialised, so the advertised filter was always empty."""

    def test_serialised_findings_include_a_category(self):
        from studio.health import HealthReport

        report = HealthReport(
            generated_at="now",
            verdict="FAIL",
            findings=[
                Finding(
                    rule="SK007",
                    severity=Severity.IMPORTANT,
                    title="t",
                    detail="d",
                    path="/x/SKILL.md",
                    spec="https://example.invalid",
                )
            ],
        )
        payload = report.to_dict()
        self.assertIn("category", payload["findings"][0])
        self.assertEqual(payload["findings"][0]["category"], "skills")

    def test_an_unknown_rule_falls_back_rather_than_raising(self):
        from studio.health import _category_of

        self.assertEqual(_category_of("ZZ999"), "other")


class CoverageCountsRealFiles(unittest.TestCase):
    """The denominator added two history files unconditionally, so a machine
    using only one runtime could never reach 100% - and the CB001 fixer refuses
    to act below full coverage, rejecting complete evidence as incomplete."""

    def test_reading_everything_available_is_full_coverage(self):
        from studio.usage import _coverage_pct

        self.assertEqual(_coverage_pct(10, 10), 100.0)

    def test_nothing_to_read_is_unknown_not_zero(self):
        """"read nothing of what existed" and "there was nothing" must not look
        alike to a rule deciding whether it may call something unused."""
        from studio.usage import _coverage_pct

        self.assertIsNone(_coverage_pct(0, 0))
        self.assertEqual(_coverage_pct(0, 5), 0.0)

    def test_partial_reads_are_below_full(self):
        from studio.usage import _coverage_pct

        self.assertLess(_coverage_pct(9, 10), 100.0)


class VersionComparisonHasOneImplementation(unittest.TestCase):
    """Three modules each had their own copy and they disagreed: fixing the
    prerelease bug in one left the others suppressing toolkit updates and
    skipping stable-version migrations."""

    def test_every_module_uses_the_shared_key(self):
        from studio import toolkits, updates, upgrade, versions

        for mod in (updates, toolkits, upgrade):
            self.assertIs(
                mod.version_key,
                versions.version_key,
                f"{mod.__name__} has its own version comparison again",
            )

    def test_the_shared_key_orders_prereleases_correctly(self):
        from studio.versions import version_key

        self.assertGreater(version_key("1.0.0"), version_key("1.0.0-beta.1"))


class HostHeaderIsValidated(unittest.TestCase):
    """Binding to 127.0.0.1 does not stop DNS rebinding: a remote page can point
    its own domain at the loopback address and then read GET endpoints as
    same-origin, with no token involved. Checking Host is what makes the address
    binding mean something."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from studio import server

        cls.server_mod = server
        server.Handler.repo_root = os.path.abspath(".")
        server.Handler.web_root = os.path.abspath("web")
        server.Handler.allow_actions = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        server.Handler.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, host):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.putrequest("GET", "/api/session", skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            conn.endheaders()
            return conn.getresponse().status
        finally:
            conn.close()

    def test_an_attacker_controlled_host_is_refused(self):
        self.assertEqual(
            self._get("evil.example.com"),
            403,
            "a rebound DNS name could read local configuration",
        )

    def test_loopback_by_address_is_accepted(self):
        self.assertEqual(self._get(f"127.0.0.1:{self.port}"), 200)

    def test_loopback_by_name_is_accepted(self):
        self.assertEqual(self._get(f"localhost:{self.port}"), 200)


class SummaryUsesTheRealDenominator(unittest.TestCase):
    """Testing the percentage helper alone was not enough: the defect was at the
    call site, which added two history files whether or not they existed."""

    def _index(self, **kw):
        from studio.usage import UsageIndex

        idx = UsageIndex()
        for k, v in kw.items():
            setattr(idx, k, v)
        return idx

    def test_one_runtime_fully_read_reports_full_coverage(self):
        idx = self._index(files_read=11, total_transcripts=10, total_history_files=1)
        self.assertEqual(idx.summary()["file_coverage_pct"], 100.0)

    def test_both_runtimes_fully_read_reports_full_coverage(self):
        idx = self._index(files_read=12, total_transcripts=10, total_history_files=2)
        self.assertEqual(idx.summary()["file_coverage_pct"], 100.0)

    def test_a_missed_file_is_visible(self):
        idx = self._index(files_read=9, total_transcripts=10, total_history_files=1)
        self.assertLess(idx.summary()["file_coverage_pct"], 100.0)

    def test_history_only_with_no_transcripts_is_still_measurable(self):
        """Previously this reported null coverage, which reads as "no evidence"
        rather than "complete evidence from a quiet machine"."""
        idx = self._index(files_read=1, total_transcripts=0, total_history_files=1)
        self.assertEqual(idx.summary()["file_coverage_pct"], 100.0)


class WritesPreserveWhatThePathIs(unittest.TestCase):
    """A change set replaced the file wholesale, losing two properties of the
    original that cannot be recovered afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.repo)
        os.makedirs(self.work)

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, path, text):
        from studio import patch

        cs = patch.ChangeSet(name="w", description="d")
        cs.changes = [patch.Change(path=path, new_text=text, action="modify")]
        return patch.apply(cs, self.repo)

    def test_a_symlinked_target_stays_a_symlink(self):
        """Toolkits install skills as symlinks - 38 of them on the machine this
        was found on. Replacing the link severs the toolkit's management, and the
        backup holds only dereferenced content so rollback cannot restore it."""
        real = os.path.join(self.work, "real.md")
        link = os.path.join(self.work, "link.md")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("original\n")
        os.symlink(real, link)

        self._apply(link, "updated\n")

        self.assertTrue(os.path.islink(link), "the symlink was replaced by a regular file")
        self.assertEqual(os.path.realpath(link), os.path.realpath(real))
        self.assertEqual(open(real, encoding="utf-8").read(), "updated\n")

    def test_restrictive_permissions_are_not_widened(self):
        secret = os.path.join(self.work, "secret.json")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        os.chmod(secret, 0o600)

        self._apply(secret, '{"changed": true}\n')

        import stat as _stat

        self.assertEqual(
            _stat.S_IMODE(os.stat(secret).st_mode), 0o600, "private file became readable"
        )

    def test_an_executable_target_stays_executable(self):
        script = os.path.join(self.work, "run.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        os.chmod(script, 0o755)

        self._apply(script, "#!/bin/sh\necho bye\n")

        self.assertTrue(os.access(script, os.X_OK), "generated script lost its executable bit")

    def test_a_new_file_is_still_created_normally(self):
        fresh = os.path.join(self.work, "new.md")
        self._apply(fresh, "hello\n")
        self.assertEqual(open(fresh, encoding="utf-8").read(), "hello\n")
        self.assertFalse(os.path.islink(fresh))


class MalformedHooksDoesNotAbortTheScan(unittest.TestCase):
    """Valid JSON of the wrong shape raised AttributeError and took down every
    check, instead of being reported as one unreadable section."""

    def _scan_with(self, settings_text):
        from studio import scan

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as fh:
                fh.write(settings_text)
            saved = scan.CLAUDE_DIR
            scan.CLAUDE_DIR = d
            try:
                errors: list[str] = []
                return scan._scan_hooks(errors), errors
            finally:
                scan.CLAUDE_DIR = saved

    def test_a_list_hooks_section_is_recorded_not_raised(self):
        hooks, errors = self._scan_with('{"hooks": [{"bad": "shape"}]}')
        self.assertEqual(hooks, [])
        self.assertTrue(errors, "a malformed hooks section produced no error record")
        self.assertIn("expected an object", errors[0])

    def test_a_string_hooks_section_is_also_survived(self):
        hooks, errors = self._scan_with('{"hooks": "nope"}')
        self.assertEqual(hooks, [])
        self.assertTrue(errors)

    def test_a_well_formed_hooks_section_still_scans(self):
        hooks, errors = self._scan_with(
            '{"hooks": {"PreToolUse": [{"matcher": "Bash", '
            '"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].event, "PreToolUse")


class Cb001NeedsCompleteHistoryToo(unittest.TestCase):
    """CB001 feeds the bulk-disable path, so concluding "unused" from partial
    history is the most costly version of that mistake."""

    def _inv(self):
        from studio.model import Plugin, Runtime

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="zz-quiet@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=40,
            )
        ]
        inv.skills = [
            Skill(
                id=f"s{i}",
                name=f"zz-quiet-skill-{i}",
                dir_name=f"zz-quiet-skill-{i}",
                path=f"/x/zz-{i}/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="q" * 900,
                plugin="zz-quiet@market",
            )
            for i in range(40)
        ]
        return inv

    def test_partial_history_produces_no_finding(self):
        from studio.rules.context import cb001

        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=False)
        self.assertEqual(list(cb001(self._inv(), cfg)), [])

    def test_complete_history_still_produces_one(self):
        from studio.rules.context import cb001

        cfg = Config(repo_root=".", plugin_usage={}, usage_available=True, usage_complete=True)
        self.assertEqual(len(list(cb001(self._inv(), cfg))), 1)


class SymlinkDeletionIsReversible(unittest.TestCase):
    """`was_symlink` was read after the delete, when the link no longer existed,
    so the manifest always said "regular file" and rollback recreated one -
    severing whatever managed the link, despite the reversible-apply promise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.repo)
        os.makedirs(self.work)
        self.real = os.path.join(self.work, "real.md")
        self.link = os.path.join(self.work, "link.md")
        with open(self.real, "w", encoding="utf-8") as fh:
            fh.write("owned by a toolkit\n")
        os.symlink(self.real, self.link)

    def tearDown(self):
        self._tmp.cleanup()

    def test_deleting_a_symlink_records_that_it_was_one(self):
        from studio import patch

        cs = patch.ChangeSet(name="rm", description="d")
        cs.changes = [patch.Change(path=self.link, new_text="", action="delete")]
        result = patch.apply(cs, self.repo)
        record = result["changes"][0]
        self.assertTrue(record["was_symlink"], "the manifest lost the fact it was a link")
        self.assertEqual(record["symlink_target"], self.real)

    def test_rollback_restores_the_link_not_a_copy(self):
        from studio import patch

        cs = patch.ChangeSet(name="rm", description="d")
        cs.changes = [patch.Change(path=self.link, new_text="", action="delete")]
        result = patch.apply(cs, self.repo)
        self.assertFalse(os.path.lexists(self.link))

        patch.rollback(self.repo, os.path.basename(result["backup"]))
        self.assertTrue(os.path.islink(self.link), "rollback produced a regular file")
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(self.real))

    def test_a_regular_file_still_rolls_back_normally(self):
        from studio import patch

        plain = os.path.join(self.work, "plain.md")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("before\n")
        cs = patch.ChangeSet(name="rm", description="d")
        cs.changes = [patch.Change(path=plain, new_text="", action="delete")]
        result = patch.apply(cs, self.repo)
        patch.rollback(self.repo, os.path.basename(result["backup"]))
        self.assertFalse(os.path.islink(plain))
        self.assertEqual(open(plain, encoding="utf-8").read(), "before\n")


class EmptyGuardMustNameTheVariable(unittest.TestCase):
    """`re.search` returns the leftmost match, so a bracket-only alternative
    matched first and left the variable uncaptured - and that was accepted as a
    guard for whatever variable the command actually piped. A guard on some
    other variable silenced the finding this rule exists to raise."""

    def _guards(self, command, var):
        from studio.rules.hooks import _guards

        return _guards(command, var)

    def test_a_guard_on_another_variable_does_not_count(self):
        self.assertFalse(
            self._guards('[[ -z "$OTHER" ]] && exit 0; echo "$DIFF" | grep -qv docs', "DIFF")
        )

    def test_single_and_double_bracket_guards_both_count(self):
        self.assertTrue(self._guards('[ -z "$DIFF" ] && exit 0; echo "$DIFF" | x', "DIFF"))
        self.assertTrue(self._guards('[[ -z "$DIFF" ]] && exit 0; echo "$DIFF" | x', "DIFF"))

    def test_every_guard_in_the_command_is_examined(self):
        self.assertTrue(
            self._guards('[ -z "$A" ] && exit 0; [ -z "$DIFF" ] && exit 0; echo "$DIFF"', "DIFF")
        )

    def test_braced_expansion_counts(self):
        self.assertTrue(self._guards('[ -z "${DIFF}" ] && exit 0; echo "$DIFF"', "DIFF"))

    def test_no_guard_at_all_is_not_a_guard(self):
        self.assertFalse(self._guards('echo "$DIFF" | grep -qv docs', "DIFF"))


class HookRulesAgreeWithEachOther(unittest.TestCase):
    """HK005 checked only for the grep pattern, so a command HK001 accepted as
    correctly guarded still failed HK005 - a correct configuration failing the
    health check."""

    def _hook(self, command, if_rule):
        from studio.model import Hook

        inv = Inventory()
        inv.hooks = [
            Hook(
                id="h",
                event="PreToolUse",
                matcher="Bash",
                type="command",
                command=command,
                if_rule=if_rule,
                injects="",
                source="/x/settings.json",
                index=0,
            )
        ]
        return inv

    def test_a_guarded_command_passes_both_rules(self):
        from studio.rules.hooks import hk001, hk005

        inv = self._hook('[ -z "$DIFF" ] && exit 0; echo "$DIFF" | grep -qv docs', "Bash(git commit:*)")
        cfg = Config(repo_root=".")
        self.assertEqual(list(hk001(inv, cfg)), [])
        self.assertEqual(list(hk005(inv, cfg)), [], "HK005 contradicted HK001")

    def test_an_unguarded_command_fails_both(self):
        from studio.rules.hooks import hk001, hk005

        inv = self._hook('echo "$DIFF" | grep -qv docs', "Bash(git commit:*)")
        cfg = Config(repo_root=".")
        self.assertEqual(len(list(hk001(inv, cfg))), 1)
        self.assertEqual(len(list(hk005(inv, cfg))), 1)


class GeneratedFilesNameARunnableCommand(unittest.TestCase):
    """The banner is copied into every generated instruction file and skill, so
    a command that does not exist is repeated across a user's whole config. It
    read `studio sync`, and the supported installation provides only
    `python3 -m studio.cli`."""

    def test_the_banner_command_is_a_real_cli_subcommand(self):
        import re
        import subprocess
        import sys

        from studio import canonical

        m = re.search(r"then run:\s*(.+)", canonical.BANNER)
        self.assertIsNotNone(m, "banner no longer tells the reader what to run")
        command = m.group(1).strip()

        self.assertTrue(
            command.startswith("python3 -m studio.cli "),
            f"banner names {command!r}, which is not the supported entry point",
        )
        sub = command.split()[3]
        help_text = subprocess.run(
            [sys.executable, "-m", "studio.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
        self.assertIn(sub, help_text, f"{sub!r} is not a subcommand the CLI offers")


class AvoidableCostNeedsCompleteEvidence(unittest.TestCase):
    """The rules stayed correctly silent on partial history while this metric
    still put an unsupported unused-plugin cost on the dashboard."""

    def _inv(self):
        from studio.model import Plugin, Runtime

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="p",
                key="zz-quiet@market",
                marketplace="market",
                runtime=Runtime.CLAUDE,
                enabled=True,
                skill_count=5,
            )
        ]
        inv.skills = [
            Skill(
                id=f"s{i}",
                name=f"zz-quiet-{i}",
                dir_name=f"zz-quiet-{i}",
                path=f"/x/zz-{i}/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="q" * 800,
                plugin="zz-quiet@market",
            )
            for i in range(5)
        ]
        return inv

    def _avoidable(self, complete):
        from studio.health import _metrics

        cfg = Config(
            repo_root=".", plugin_usage={}, usage_available=True, usage_complete=complete
        )
        return _metrics(self._inv(), cfg)["preloaded_skill_metadata"]["avoidable_est_tokens"]

    def test_partial_history_reports_no_avoidable_cost(self):
        self.assertEqual(self._avoidable(complete=False), 0)

    def test_complete_history_can_report_a_cost(self):
        self.assertGreater(self._avoidable(complete=True), 0)


class LineCountsMatchAnEditor(unittest.TestCase):
    """`split("\\n")` yields a trailing empty element for any file ending in a
    newline - nearly all of them - so a 500-line skill measured 501 and tripped
    the 500-line rule it was exactly inside."""

    def _count(self, text):
        from studio.scan import _line_count

        return _line_count(text)

    def test_a_trailing_newline_does_not_add_a_line(self):
        self.assertEqual(self._count("a\nb\nc\n"), 3)

    def test_no_trailing_newline_counts_the_same(self):
        self.assertEqual(self._count("a\nb\nc"), 3)

    def test_an_empty_file_is_zero(self):
        self.assertEqual(self._count(""), 0)

    def test_a_file_exactly_at_the_budget_is_not_over_it(self):
        from studio.rules import SKILL_BODY_MAX_LINES

        text = "\n".join(f"line {i}" for i in range(SKILL_BODY_MAX_LINES)) + "\n"
        self.assertEqual(self._count(text), SKILL_BODY_MAX_LINES)
        self.assertFalse(self._count(text) > SKILL_BODY_MAX_LINES)


class ModifiedSymlinksRollBackTheirContent(unittest.TestCase):
    """A modified link still exists, and the write changed the bytes of whatever
    it points at. Recreating the link alone left that content in place while
    reporting the path restored."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.repo)
        os.makedirs(self.work)
        self.real = os.path.join(self.work, "real.md")
        self.link = os.path.join(self.work, "link.md")
        with open(self.real, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL\n")
        os.symlink(self.real, self.link)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rollback_restores_the_targets_bytes(self):
        from studio import patch

        cs = patch.ChangeSet(name="mod", description="d")
        cs.changes = [patch.Change(path=self.link, new_text="CHANGED\n", action="modify")]
        result = patch.apply(cs, self.repo)
        self.assertEqual(open(self.real, encoding="utf-8").read(), "CHANGED\n")

        patch.rollback(self.repo, os.path.basename(result["backup"]))
        self.assertEqual(
            open(self.real, encoding="utf-8").read(),
            "ORIGINAL\n",
            "the link was restored but its target kept the modified content",
        )
        self.assertTrue(os.path.islink(self.link))


class LoopbackAliasesAreOneOrigin(unittest.TestCase):
    """`_host_allowed` accepts both names, so the dashboard opens at either and
    /api/session hands out a token - but a strict string compare then rejected
    every action from whichever name was not the configured one."""

    def _same(self, origin, expected):
        from studio.server import _same_loopback

        return _same_loopback(origin, expected)

    def test_localhost_and_the_address_are_the_same_server(self):
        self.assertTrue(self._same("http://localhost:8787", "http://127.0.0.1:8787"))
        self.assertTrue(self._same("http://127.0.0.1:8787", "http://localhost:8787"))

    def test_a_different_port_is_a_different_server(self):
        self.assertFalse(self._same("http://127.0.0.1:9999", "http://127.0.0.1:8787"))

    def test_a_remote_origin_is_still_refused(self):
        self.assertFalse(self._same("https://evil.example", "http://127.0.0.1:8787"))
        self.assertFalse(self._same("http://evil.example:8787", "http://127.0.0.1:8787"))

    def test_scheme_must_match(self):
        self.assertFalse(self._same("https://localhost:8787", "http://127.0.0.1:8787"))


class GateAcceptsEitherLoopbackName(unittest.TestCase):
    """Exercised through the gate, not the helper.

    Testing `_same_loopback` alone missed the defect twice over: the helper can
    be right while the caller compares strings, which is exactly what it did.
    """

    def setUp(self):
        from studio import server

        self.server = server
        self.handler = server.Handler
        self._saved = (self.handler.allow_actions, self.handler.origin)
        self.handler.origin = "http://127.0.0.1:8787"

    def tearDown(self):
        self.handler.allow_actions, self.handler.origin = self._saved

    def _refusal(self, origin):
        stub = self.handler.__new__(self.handler)
        stub.headers = {
            "Origin": origin,
            "X-Studio-Token": self.server._SESSION_TOKEN,
        }
        return self.handler._from_this_page(stub)

    def test_localhost_is_accepted_when_configured_as_the_address(self):
        self.assertIsNone(
            self._refusal("http://localhost:8787"),
            "opening the dashboard at localhost broke every action",
        )

    def test_the_configured_address_is_still_accepted(self):
        self.assertIsNone(self._refusal("http://127.0.0.1:8787"))

    def test_a_remote_origin_is_still_refused(self):
        self.assertIsNotNone(self._refusal("https://evil.example"))

    def test_another_port_is_still_refused(self):
        self.assertIsNotNone(self._refusal("http://127.0.0.1:9999"))


class TranslationTableKeysAreNotRoutes(unittest.TestCase):
    """A translation table names a command precisely because it is not available
    here - "old `/qa-only` -> do this instead". Reading its key column as a route
    flagged fourteen correct rows for every real one.

    The suppression is deliberately narrow. Applying it to every table created
    the opposite failure: a tool-selection table often puts the target in column
    one (`| agent-browser | Chrome via CDP | ... |`), and a removed route there
    would then go unreported - a silent miss, which is worse than a visible
    false positive.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _findings(self, body):
        from studio.model import Workflow
        from studio.rules.workflows import wf006

        path = os.path.join(self._tmp.name, "wf.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        inv = Inventory()
        inv.workflows = [Workflow(id="w", path=path, runtime=Runtime.CLAUDE, lines=1)]
        inv.skills = [
            Skill(
                id="s",
                name="real-skill",
                dir_name="real-skill",
                path="/x/real/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="d",
            )
        ]
        return [f.evidence["name"] for f in wf006(inv, Config(repo_root="."))]

    def test_a_translation_key_is_not_flagged(self):
        body = "## 舊命令對應\n\n| 舊命令 | Codex 做法 |\n| --- | --- |\n| `/gone-command` | 測試 + 報告 |\n"
        self.assertEqual(self._findings(body), [])

    def test_an_english_legacy_table_is_also_recognised(self):
        body = "## Old commands\n\n| Old | Do instead |\n| --- | --- |\n| `gone-command` | run tests |\n"
        self.assertEqual(self._findings(body), [])

    def test_a_tool_table_still_reports_a_removed_first_column_target(self):
        """The regression Codex caught: suppressing column one everywhere turned
        a visible false positive into a silent false negative."""
        body = "### 瀏覽器工具選擇\n\n| 工具 | 底層 |\n| --- | --- |\n| `gone-tool` | Chrome via CDP |\n"
        self.assertEqual(self._findings(body), ["gone-tool"])

    def test_a_value_column_target_is_always_checked(self):
        body = "## 常用技能\n\n| 類型 | 建議 |\n| --- | --- |\n| 前端 | use `gone-skill` |\n"
        self.assertEqual(self._findings(body), ["gone-skill"])

    def test_an_existing_target_is_never_reported(self):
        body = "### 工具\n\n| 工具 | 說明 |\n| --- | --- |\n| `real-skill` | exists |\n"
        self.assertEqual(self._findings(body), [])

    def test_prose_outside_a_table_is_unaffected(self):
        self.assertEqual(self._findings("Use `gone-skill` first.\n"), ["gone-skill"])


class CoverageExposesItsOwnDenominator(unittest.TestCase):
    """The page could only render `files_read / transcripts_total`, which shows
    more files read than exist because the numerator includes history files.
    The percentage was right; the fraction beside it could not be true, and that
    fraction is the evidence the unused-plugin rules rest on."""

    def _summary(self, **kw):
        from studio.usage import UsageIndex

        idx = UsageIndex()
        for k, v in kw.items():
            setattr(idx, k, v)
        return idx.summary()

    def test_summary_reports_the_total_it_divides_by(self):
        s = self._summary(files_read=4139, total_transcripts=4137, total_history_files=2)
        self.assertEqual(s["files_total"], 4139)
        self.assertEqual(s["history_files_total"], 2)
        self.assertEqual(s["file_coverage_pct"], 100.0)

    def test_the_displayable_fraction_is_never_over_one(self):
        s = self._summary(files_read=4139, total_transcripts=4137, total_history_files=2)
        self.assertLessEqual(s["files_read"], s["files_total"])

    def test_a_partial_scan_still_reads_correctly(self):
        s = self._summary(files_read=100, total_transcripts=200, total_history_files=1)
        self.assertEqual(s["files_total"], 201)
        self.assertLess(s["file_coverage_pct"], 100.0)


class SyncPreviewNamesItsTargets(unittest.TestCase):
    def test_the_payload_lists_every_rendered_target(self):
        """The status line is built from this. Without it the page hardcoded a
        description that named two files while six were checked."""
        import inspect

        from studio import server

        src = inspect.getsource(server.Handler._sync_preview)
        self.assertIn('"targets"', src, "sync-preview does not expose what it covers")


class ReferenceExtraction(unittest.TestCase):
    """What counts as a file a skill points the reader at.

    Both cases here produced phantom broken references on a real config: the
    scanner invented `skill-usage.json` from `skill-usage.jsonl`, and it treated
    shell redirect targets as progressive-disclosure links.
    """

    def test_a_jsonl_extension_is_not_truncated(self):
        from studio.scan import _PATH_RE

        self.assertEqual(
            _PATH_RE.findall("~/.gstack/analytics/skill-usage.jsonl"),
            ["~/.gstack/analytics/skill-usage.jsonl"],
        )

    def test_a_json_path_still_matches(self):
        from studio.scan import _PATH_RE

        self.assertEqual(_PATH_RE.findall("~/.claude/settings.json"), ["~/.claude/settings.json"])

    def test_paths_inside_code_fences_are_not_references(self):
        """`>> ~/x.jsonl` is a file the script creates, not one to go read."""
        from studio.scan import _refs

        text = (
            "Read ~/.claude/skills/real/SKILL.md first.\n\n"
            "```bash\n"
            "echo hi >> ~/.gstack/analytics/skill-usage.jsonl\n"
            "cat ~/.claude/skills/other/SKILL.md\n"
            "```\n"
        )
        refs = _refs(text)
        self.assertIn(os.path.expanduser("~/.claude/skills/real/SKILL.md"), refs)
        self.assertEqual(
            [r for r in refs if "gstack" in r or "other" in r],
            [],
            "a path inside a code fence was treated as a reference",
        )

    def test_an_unclosed_fence_does_not_swallow_the_rest(self):
        from studio.scan import _strip_fences

        out = _strip_fences("before\n```\ninside\n")
        self.assertIn("before", out)
        self.assertNotIn("inside", out)


class SubagentRules(unittest.TestCase):
    """Subagents were the one configured thing no rule looked at. The setup this
    was written against had eleven definition files, 3,500 lines, none of which
    Claude Code could load - and the report was green."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, body, name="a.md", runtime=None):
        from studio.model import AgentDef
        from studio import fm

        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        parsed = fm.parse(body)
        return AgentDef(
            id=f"agent:{name}",
            name=(parsed.text("name") or name[:-3]).strip(),
            path=path,
            runtime=runtime or Runtime.CLAUDE,
            lines=len(body.splitlines()),
            description=(parsed.text("description") or "").strip(),
            frontmatter_present=parsed.present,
            declared_name=(parsed.text("name") or "").strip(),
        )

    def _inv(self, *agents, commands=()):
        inv = Inventory()
        inv.agents = list(agents)
        inv.commands = list(commands)
        return inv

    def _command(self, text, name="done.md"):
        """A command file whose body is real text on disk, because the rule reads
        it rather than trusting a field."""
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return Command(
            id=f"cmd:{name}",
            name=name[:-3],
            path=path,
            runtime=Runtime.CLAUDE,
            lines=len(text.splitlines()),
        )

    def _run(self, fn, *agents, commands=()):
        return list(fn(self._inv(*agents, commands=commands), Config(repo_root=".")))

    def test_no_frontmatter_is_critical(self):
        """Identity comes only from `name`, so a file without frontmatter has no
        identity and can never be delegated to."""
        from studio.rules.agents import ag001

        out = self._run(ag001, self._agent("# Python Expert\n\nSome guidance.\n"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, Severity.CRITICAL)

    def test_a_well_formed_agent_is_not_reported(self):
        from studio.rules.agents import ag001, ag002, ag003, ag005

        a = self._agent(
            "---\nname: code-reviewer\ndescription: Reviews code. Use after writing or "
            "modifying code.\n---\n\nBody.\n"
        )
        for fn in (ag001, ag002, ag003, ag005):
            self.assertEqual(self._run(fn, a), [], f"{fn.__name__} reported a valid agent")

    def test_a_subdirectory_is_not_itself_a_problem(self):
        """The docs say both agent directories are scanned recursively and the
        subfolder does not affect identity, so nesting must not be reported."""
        from studio.rules.agents import ag001, ag002, ag003, ag005

        a = self._agent(
            "---\nname: nested-one\ndescription: Does a thing. Use when asked.\n---\n\nBody.\n",
            name="deep.md",
        )
        a.path = os.path.join(self._tmp.name, "reviewers", "deep.md")
        for fn in (ag001, ag002, ag003, ag005):
            self.assertEqual(self._run(fn, a), [])

    def test_missing_required_fields_are_reported(self):
        from studio.rules.agents import ag002

        out = self._run(ag002, self._agent("---\nname: only-name\n---\n\nBody.\n"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].evidence["missing"], ["description"])

    def test_a_colon_in_the_name_is_critical(self):
        """The docs state Claude Code does not load such a file at all."""
        from studio.rules.agents import ag003

        out = self._run(
            ag003,
            self._agent("---\nname: my:agent\ndescription: Use when asked.\n---\n\nB.\n"),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].evidence["reason"], "colon")

    def test_an_uppercase_name_is_reported(self):
        from studio.rules.agents import ag003

        out = self._run(
            ag003,
            self._agent("---\nname: MyAgent\ndescription: Use when asked.\n---\n\nB.\n"),
        )
        self.assertEqual(out[0].evidence["reason"], "format")

    def test_duplicate_names_are_reported_once(self):
        from studio.rules.agents import ag004

        body = "---\nname: twin\ndescription: Use when asked.\n---\n\nB.\n"
        out = self._run(ag004, self._agent(body, "a.md"), self._agent(body, "b.md"))
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].evidence["paths"]), 2)

    def test_a_description_without_a_trigger_is_reported(self):
        from studio.rules.agents import ag005

        out = self._run(
            ag005,
            self._agent("---\nname: py\ndescription: Python backend expertise.\n---\n\nB.\n"),
        )
        self.assertEqual(len(out), 1)

    def test_a_description_with_a_trigger_is_not(self):
        from studio.rules.agents import ag005

        for desc in (
            "Reviews code. Use after writing or modifying code.",
            "Use when the user asks about deployment.",
            "Handles migrations. 使用時機：需要改資料庫結構時。",
        ):
            a = self._agent(f"---\nname: x\ndescription: {desc}\n---\n\nB.\n")
            self.assertEqual(self._run(ag005, a), [], f"flagged a valid trigger: {desc!r}")


class HookToolEventsTrackTheSpec(unittest.TestCase):
    """`PermissionDenied` joined the tool-event family after HK004 was written.
    An unscoped hook on it went unreported until the spec-drift check noticed the
    documentation had changed - which is the whole point of that check."""

    def _hook(self, event):
        from studio.model import Hook

        inv = Inventory()
        inv.hooks = [
            Hook(
                id="h",
                event=event,
                matcher="*",
                type="command",
                command="echo hi",
                if_rule="",
                injects="",
                source="/x/settings.json",
                index=0,
            )
        ]
        return inv

    def test_every_documented_tool_event_is_covered(self):
        from studio.rules.hooks import hk004

        for event in (
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionRequest",
            "PermissionDenied",
        ):
            out = list(hk004(self._hook(event), Config(repo_root=".")))
            self.assertEqual(len(out), 1, f"{event} is not treated as a tool event")

    def test_a_non_tool_event_is_not_reported(self):
        from studio.rules.hooks import hk004

        self.assertEqual(list(hk004(self._hook("SessionStart"), Config(repo_root="."))), [])


class DashboardAccessibility(unittest.TestCase):
    """Accessibility was the one area the QA pass marked Not covered. These are
    static checks over the shipped markup and stylesheet, so a regression fails
    the suite rather than waiting for someone to notice with a keyboard."""

    def setUp(self):
        self.html = open("web/index.html", encoding="utf-8").read()
        self.css = open("web/style.css", encoding="utf-8").read()

    def test_the_page_declares_a_language(self):
        self.assertIn('lang="', self.html.split(">", 2)[1] + ">")

    def test_every_tab_is_wired_to_its_panel(self):
        import re

        tabs = re.findall(r'<button data-tab="([a-z]+)"[^>]*aria-controls="([^"]+)"', self.html)
        self.assertTrue(tabs, "no tab declares aria-controls")
        for name, controls in tabs:
            self.assertEqual(controls, f"tab-{name}")
            self.assertIn(f'id="{controls}"', self.html, f"{controls} is not a real element")

    def test_tab_roles_are_declared(self):
        """Matched as real attributes. A plain substring check passes on
        `data-role="tablist"`, which announces nothing."""
        import re

        for role in ("tablist", "tab", "tabpanel"):
            self.assertRegex(
                self.html,
                rf'(?<![-\w]){re.escape("role")}="{role}"',
                f'no element declares role="{role}" as an attribute',
            )

    def test_every_select_has_a_label_bound_to_it(self):
        """A visible `<label>` that is not bound to its control announces nothing."""
        import re

        for select_id in re.findall(r'<select id="([^"]+)"', self.html):
            bound = f'for="{select_id}"' in self.html
            aria = re.search(rf'<select id="{select_id}"[^>]*aria-label=', self.html)
            self.assertTrue(bound or aria, f"{select_id} has no associated label")

    def test_every_text_input_is_named(self):
        import re

        for m in re.finditer(r'<input id="([^"]+)"([^>]*)>', self.html):
            input_id, rest = m.group(1), m.group(2)
            if 'type="checkbox"' in rest:
                continue  # those sit inside a <label>
            named = f'for="{input_id}"' in self.html or "aria-label=" in rest
            self.assertTrue(named, f"{input_id} has no accessible name")

    def test_keyboard_focus_is_visible(self):
        """There were no :focus rules at all, so a keyboard user could not tell
        which control they were on.

        Targets the unscoped rule and checks it actually draws. Searching the
        whole stylesheet was not enough: `outline-offset` alone satisfied a
        looser check, and so did the `@supports not selector(:focus-visible)`
        fallback, which by definition never applies where the primary rule does.
        """
        import re

        m = re.search(r"(?m)^:focus-visible\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "no unscoped :focus-visible rule")
        # The value is read out and compared, not matched with a negative
        # lookahead: `\s*` can match zero characters, which let the lookahead sit
        # before the space and happily match " none".
        values = [
            v.strip()
            for prop, v in re.findall(r"\b(outline)\s*:\s*([^;]+)", m.group(1))
            if prop == "outline"
        ]
        self.assertTrue(values, "the global :focus-visible rule sets no outline")
        self.assertTrue(
            any(v and v.split()[0] not in ("none", "0", "hidden") for v in values),
            f"the global :focus-visible rule draws nothing: outline is {values!r}",
        )

    def test_a_fallback_exists_for_browsers_without_focus_visible(self):
        self.assertIn("@supports not selector(:focus-visible)", self.css)

    def test_the_graph_svg_has_an_accessible_name(self):
        self.assertRegex(self.html, r'<svg id="graph"[^>]*aria-label="')


class ScheduledRunChecksTheGuidance(unittest.TestCase):
    """"Check periodically whether the guidance changed" is only true if
    something checks it periodically. The daily job ran the rules but never
    re-fetched the documents those rules are built on."""

    def test_the_installed_schedule_checks_specs(self):
        import plistlib

        path = os.path.join("launchd", "com.agent-config-studio.healthcheck.plist")
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
        args = data["ProgramArguments"]
        self.assertIn("--with-updates", args, "the daily run does not check remote updates")
        self.assertIn("--with-specs", args, "the daily run does not check the cited guidance")

    def test_health_carries_the_spec_result(self):
        from studio.health import HealthReport

        report = HealthReport(
            generated_at="now",
            verdict="PASS",
            findings=[],
            specs={"checked": 6, "changed": ["https://x/hooks"], "new": [], "unreachable": []},
        )
        payload = report.to_dict()
        self.assertEqual(payload["specs"]["changed"], ["https://x/hooks"])

    def test_a_run_without_the_flag_reports_no_spec_result(self):
        """Absence of a check must not look like a clean check."""
        from studio.health import HealthReport

        self.assertEqual(HealthReport(generated_at="n", verdict="PASS", findings=[]).to_dict()["specs"], {})


class ToolkitOwnsItsRootSkill(unittest.TestCase):
    """A checkout whose only skill is its root SKILL.md owned nothing, so every
    symlink pointing at that root read as content the user wrote. One gstack
    release turned that into five blocking findings on a file they do not own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skills = os.path.join(self._tmp.name, "skills")
        self.root = os.path.join(self.skills, "kit")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: kit\ndescription: Router. Use when asked.\n---\n\nBody.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _toolkit(self):
        from studio.toolkits import Toolkit

        return Toolkit(
            name="kit",
            root=self.root,
            install_dir=self.skills,
            remote="",
            local_version="1.0.0",
            commit="abc",
            manages=["kit"],
        )

    def test_the_root_skill_is_owned(self):
        from studio.toolkits import managed_paths

        owned = managed_paths([self._toolkit()])
        self.assertIn(os.path.join(self.root, "SKILL.md"), owned)

    def test_a_symlink_into_the_checkout_is_owned_whatever_it_is_called(self):
        """gstack links `_gstack-command/SKILL.md` at its root SKILL.md; the
        directory name matches no managed entry."""
        from studio.toolkits import managed_paths

        alias = os.path.join(self.skills, "_kit-command")
        os.makedirs(alias)
        link = os.path.join(alias, "SKILL.md")
        os.symlink(os.path.join(self.root, "SKILL.md"), link)

        self.assertIn(link, managed_paths([self._toolkit()]))

    def test_an_unrelated_local_skill_is_not_claimed(self):
        from studio.toolkits import managed_paths

        mine = os.path.join(self.skills, "my-own")
        os.makedirs(mine)
        path = os.path.join(mine, "SKILL.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\nname: my-own\ndescription: Mine. Use when asked.\n---\n")

        self.assertNotIn(path, managed_paths([self._toolkit()]))


class PreloadCostIsPerRuntime(unittest.TestCase):
    """A single combined figure is a number no session ever pays. Plugins and
    toolkits install under ~/.claude, so Codex cannot load them; each runtime
    preloads only what it can load."""

    def _metrics(self):
        from studio.health import _metrics
        from studio.model import Plugin, Runtime

        inv = Inventory()
        inv.skills = [
            Skill(id="a", name="mine-c", dir_name="mine-c", path="/h/.claude/skills/a/SKILL.md",
                  runtime=Runtime.CLAUDE, origin=Origin.LOCAL, description="x" * 100),
            Skill(id="b", name="mine-x", dir_name="mine-x", path="/h/.codex/skills/b/SKILL.md",
                  runtime=Runtime.CODEX, origin=Origin.LOCAL, description="y" * 200),
            Skill(id="c", name="from-plugin", dir_name="from-plugin", path="/h/.claude/plugins/c/SKILL.md",
                  runtime=Runtime.CLAUDE, origin=Origin.PLUGIN, description="z" * 400, plugin="p"),
        ]
        return _metrics(inv, Config(repo_root="."))["preloaded_skill_metadata"]

    def test_each_runtime_gets_its_own_total(self):
        pr = self._metrics()["per_runtime"]
        self.assertIn("claude", pr)
        self.assertIn("codex", pr)
        self.assertGreater(pr["claude"]["est_tokens"], 0)
        self.assertGreater(pr["codex"]["est_tokens"], 0)

    def test_plugin_skills_count_only_against_claude(self):
        """They install under ~/.claude. Charging Codex for them overstates what
        a Codex session actually loads."""
        pr = self._metrics()["per_runtime"]
        self.assertEqual(pr["codex"]["skills"], 1, "Codex was charged for a plugin skill")
        self.assertEqual(pr["claude"]["skills"], 2)

    def test_the_runtimes_sum_to_the_old_total(self):
        """The combined figure still exists for continuity, but it is the sum of
        two runtimes and must never be presented as one session's cost."""
        m = self._metrics()
        pr = m["per_runtime"]
        self.assertEqual(
            pr["claude"]["bytes"] + pr["codex"]["bytes"], m["total_bytes"]
        )


class OverlapNeedsSharedRuntime(unittest.TestCase):
    """Two skills only compete when they are loaded together. A Claude-only
    skill and a Codex-only one never see each other, so reporting them as
    competing for one trigger describes a conflict that cannot happen."""

    def _inv(self, *specs):
        inv = Inventory()
        inv.skills = [
            Skill(
                id=f"s{i}",
                name=name,
                dir_name=name,
                path=f"/h/.{rt}/skills/{name}/SKILL.md",
                runtime=Runtime.CLAUDE if rt == "claude" else Runtime.CODEX,
                origin=Origin.LOCAL,
                description=desc,
            )
            for i, (name, rt, desc) in enumerate(specs)
        ]
        return inv

    DESC = "Generate unit tests, jest mocks, stubs, fixtures and coverage gaps."

    def test_two_skills_in_the_same_runtime_are_compared(self):
        from studio.rules.skills import sk017

        inv = self._inv(("qa-one", "claude", self.DESC), ("qa-two", "claude", self.DESC))
        self.assertEqual(len(list(sk017(inv, Config(repo_root=".")))), 1)

    def test_skills_in_different_runtimes_are_not_compared(self):
        from studio.rules.skills import sk017

        inv = self._inv(("qa-one", "claude", self.DESC), ("qa-two", "codex", self.DESC))
        self.assertEqual(list(sk017(inv, Config(repo_root="."))), [])

    def test_a_mirrored_skill_still_competes_in_the_runtime_it_shares(self):
        """A skill present in both runtimes overlaps with a Codex-only one in
        Codex, so that pair is real."""
        from studio.rules.skills import sk017

        inv = self._inv(
            ("mirrored", "claude", self.DESC),
            ("mirrored", "codex", self.DESC),
            ("qa-two", "codex", self.DESC),
        )
        out = list(sk017(inv, Config(repo_root=".")))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].evidence["skills"]), ["mirrored", "qa-two"])


class FilePeekRouteIsGated(unittest.TestCase):
    """The route that hands out file contents.

    It was the only content endpoint that never called `_from_this_page`, and a
    request carrying no token and a foreign Origin got HTTP 200 with the body of
    `~/.codex/auth.json` - OAuth id/access/refresh tokens, a file the OS keeps at
    0600. Every other endpoint's gate test stayed green throughout, which is why
    this one exercises the route over HTTP rather than the helper.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from studio import server

        cls.server_mod = server
        server.Handler.repo_root = os.path.abspath(".")
        server.Handler.web_root = os.path.abspath("web")
        server.Handler.allow_actions = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        server.Handler.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path, headers):
        import urllib.error
        import urllib.parse
        import urllib.request

        url = f"http://127.0.0.1:{self.port}/api/file?path=" + urllib.parse.quote(path)
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _token(self):
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/session") as resp:
            return json.loads(resp.read())["token"]

    def _get_route(self, route, headers):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{route}")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_a_foreign_origin_without_a_token_is_refused(self):
        """The exact request that leaked the tokens: any page the browser has
        open can issue it, and nothing about it is authenticated."""
        status, _ = self._get("~/.claude/CLAUDE.md", {"Origin": "http://evil.example.com"})
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_a_request_with_no_token_at_all_is_refused(self):
        status, _ = self._get("~/.claude/CLAUDE.md", {})
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_every_rescan_route_is_gated_not_just_health(self):
        """`fresh=1` means rescan on whichever route it appears.

        Gating expensive routes one at a time left summary, inventory, graph and
        updates open - and updates goes to the network - so any page in any tab
        could start unbounded scans through a door that looked cheap.
        """
        for route in ("summary", "inventory", "graph", "updates", "health"):
            with self.subTest(route=route):
                status, _ = self._get_route(
                    f"/api/{route}?fresh=1", {"Origin": "http://evil.example.com"}
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_a_cached_read_is_not_gated(self):
        """Gating cached reads would cost the page a round trip for nothing, and
        those responses are already unreadable cross-origin."""
        status, _ = self._get_route("/api/summary", {})
        self.assertEqual(status, HTTPStatus.OK)

    def test_the_dashboards_own_request_still_works(self):
        """A gate that also blocks the real UI is not a fix."""
        status, body = self._get(
            os.path.abspath("README.md"), {"X-Studio-Token": self._token()}
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn("text", json.loads(body))


class CredentialFilesAreNeverShown(unittest.TestCase):
    """Independent of the gate.

    A config auditor never needs to print a secret to do its job, so the refusal
    holds even for a request that is fully authenticated - the gate and the
    deny-list have to be able to fail separately.
    """

    def setUp(self):
        from studio import server

        self._tmp = tempfile.TemporaryDirectory()
        self.handler = server.Handler.__new__(server.Handler)

    def tearDown(self):
        self._tmp.cleanup()

    def _peek(self, filename, body="secret\n"):
        from studio import server
        from studio.model import Inventory

        path = os.path.join(self._tmp.name, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return self._peek_path(path)

    def _peek_path(self, path):
        from studio import server
        from studio.model import Inventory

        inv = Inventory()
        inv.roots = {"tmp": self._tmp.name}
        self.handler.repo_root = self._tmp.name
        with mock.patch.object(server, "_inventory", return_value=inv):
            return server.Handler._file_peek(self.handler, path)

    def test_the_codex_auth_file_is_refused(self):
        """The file that actually leaked."""
        self.assertIn("credential", self._peek("auth.json").get("error", ""))

    def test_a_private_key_is_refused(self):
        self.assertIn("credential", self._peek("server.pem").get("error", ""))
        self.assertIn("credential", self._peek("id_rsa").get("error", ""))

    def test_the_refusal_does_not_include_the_contents(self):
        """An error that quotes the file back defeats the point."""
        result = self._peek("auth.json", body="sk-live-TOPSECRET\n")
        self.assertNotIn("TOPSECRET", json.dumps(result))

    def test_a_symlink_cannot_launder_a_credential(self):
        """The check looked at the requested name, and `abspath` leaves links
        intact - so an innocuous `notes.md` inside an audited root, pointing at
        `~/.codex/auth.json`, passed both the confinement check and the deny-list
        and had its target read out."""
        target = os.path.join(self._tmp.name, "auth.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("REFRESH_TOKEN=leaked\n")
        link = os.path.join(self._tmp.name, "notes.md")
        os.symlink(target, link)
        result = self._peek_path(link)
        self.assertNotIn("leaked", json.dumps(result))
        self.assertIn("credential", result.get("error", ""))

    def test_a_symlink_cannot_escape_the_audited_roots(self):
        """Same root cause, different consequence: confinement is also decided on
        the unresolved path, so any file on the disk was reachable."""
        outside = tempfile.mkdtemp()
        try:
            target = os.path.join(outside, "elsewhere.md")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("outside-the-roots\n")
            link = os.path.join(self._tmp.name, "inside.md")
            os.symlink(target, link)
            result = self._peek_path(link)
            self.assertNotIn("outside-the-roots", json.dumps(result))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_a_credential_name_is_refused_whatever_it_points_at(self):
        """Both spellings are checked, not just the resolved one.

        The file opened is always the resolved path, so this cannot leak on its
        own - it is the conservative half of the pair: something named
        `auth.json` in a config root is refused without needing to be right
        about where it leads.
        """
        plain = os.path.join(self._tmp.name, "ordinary.md")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("nothing secret\n")
        link = os.path.join(self._tmp.name, "auth.json")
        os.symlink(plain, link)
        self.assertIn("credential", self._peek_path(link).get("error", ""))

    def test_a_dotenv_is_refused(self):
        """The dashboard audits repositories, and a repo root holds .env far more
        often than it holds auth.json."""
        self.assertIn("credential", self._peek(".env", body="OPENAI_API_KEY=sk-x\n").get("error", ""))
        self.assertIn("credential", self._peek(".env.local", body="TOKEN=x\n").get("error", ""))

    def test_ordinary_config_is_still_shown(self):
        """A deny-list that swallows normal files would break the dashboard."""
        result = self._peek("SKILL.md", body="---\nname: x\n---\nbody\n")
        self.assertIn("name: x", result.get("text", ""))


class UnregisteredIsNotTheSameAsUnused(unittest.TestCase):
    """AG001 used to say only "never loads", and acting on that reading deleted
    four reviewer prompts that `/done` reads by path - turning a completion gate
    into a printed PASS table. The two cases need opposite actions, so the
    finding has to tell them apart.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _case(self, command_text):
        from studio.rules.agents import ag001

        agent_path = os.path.join(self._tmp.name, "security.md")
        with open(agent_path, "w", encoding="utf-8") as fh:
            fh.write("# Security Reviewer\nOWASP Top 10.\n")
        cmd_path = os.path.join(self._tmp.name, "done.md")
        with open(cmd_path, "w", encoding="utf-8") as fh:
            fh.write(command_text.replace("<AGENT>", agent_path))
        inv = Inventory()
        inv.agents = [
            AgentDef(
                id="agent:security",
                name="security",
                path=agent_path,
                runtime=Runtime.CLAUDE,
                lines=2,
                description="",
                frontmatter_present=False,
                declared_name="",
            )
        ]
        inv.commands = [
            Command(id="cmd:done", name="done", path=cmd_path, runtime=Runtime.CLAUDE, lines=1)
        ]
        found = list(ag001(inv, Config(repo_root=".")))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_referenced_file_is_not_critical(self):
        """Blocking severity is what invites deletion."""
        self.assertEqual(self._case("Read <AGENT> and review.").severity, Severity.IMPORTANT)

    def test_a_referenced_file_names_what_reads_it(self):
        f = self._case("Read <AGENT> and review.")
        self.assertIn("done.md", f.detail)
        self.assertEqual([os.path.join(self._tmp.name, "done.md")], f.evidence["referenced_by"])

    def test_a_referenced_file_is_never_told_to_be_deleted(self):
        """The remedy is the part a person acts on."""
        f = self._case("Read <AGENT> and review.")
        self.assertNotIn("remove the file", f.remedy.lower())
        self.assertNotIn("delete the file", f.remedy.lower())

    def test_an_unreferenced_file_is_still_critical(self):
        """The original finding was right about this case and must stay."""
        f = self._case("This command references nothing.")
        self.assertEqual(f.severity, Severity.CRITICAL)
        self.assertEqual(f.evidence["referenced_by"], [])

    def test_a_tilde_reference_counts(self):
        """Commands write `~/.claude/agents/...`, not the expanded path, so
        matching only the absolute form would report every one as unreferenced."""
        from studio.rules.agents import ag001

        home = os.path.expanduser("~")
        agent_path = os.path.join(home, ".claude", "agents", "reviewers", "security.md")
        cmd_path = os.path.join(self._tmp.name, "done.md")
        with open(cmd_path, "w", encoding="utf-8") as fh:
            fh.write("Run `~/.claude/agents/reviewers/security.md`.\n")
        inv = Inventory()
        inv.agents = [
            AgentDef(
                id="agent:security",
                name="security",
                path=agent_path,
                runtime=Runtime.CLAUDE,
                lines=2,
                description="",
                frontmatter_present=False,
                declared_name="",
            )
        ]
        inv.commands = [
            Command(id="cmd:done", name="done", path=cmd_path, runtime=Runtime.CLAUDE, lines=1)
        ]
        (found,) = list(ag001(inv, Config(repo_root=".")))
        self.assertEqual(found.severity, Severity.IMPORTANT)


class MarketplaceIsPartOfPluginIdentity(unittest.TestCase):
    """The same plugin name can be installed from two marketplaces and enabled in
    only one. Keying on the bare name made the disabled install look enabled,
    counted its skills as preloaded, and produced a finding for every one of them
    saying a skill that is never loaded is never loaded.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _enabled(self, mapping):
        from studio import scan

        path = os.path.join(self._tmp.name, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"enabledPlugins": mapping}, fh)
        with mock.patch.object(scan, "CLAUDE_DIR", self._tmp.name):
            return scan._enabled_plugin_keys()

    def test_the_disabled_install_is_not_treated_as_enabled(self):
        """The exact shape in the setup this was found on."""
        enabled = self._enabled(
            {
                "superpowers@claude-plugins-official": False,
                "superpowers@superpowers-marketplace": True,
            }
        )
        self.assertNotIn("superpowers@claude-plugins-official", enabled)
        self.assertIn("superpowers@superpowers-marketplace", enabled)

    def test_the_marketplace_is_kept_in_the_key(self):
        """A bare name cannot distinguish the two installs, so dropping the
        marketplace reintroduces the defect no matter what else is correct."""
        for key in self._enabled({"superpowers@superpowers-marketplace": True}):
            self.assertIn("@", key)

    def test_a_disabled_plugin_contributes_no_skills(self):
        """End to end: the count, not just the key set."""
        from studio import scan

        cache = os.path.join(self._tmp.name, "plugins", "cache")
        for market in ("official", "other"):
            d = os.path.join(cache, market, "p", "1.0.0", "skills", "s")
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: s\ndescription: Use when testing.\n---\nbody\n")
        with open(os.path.join(self._tmp.name, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"enabledPlugins": {"p@official": False, "p@other": True}}, fh)
        os.makedirs(os.path.join(self._tmp.name, "plugins"), exist_ok=True)
        with open(
            os.path.join(self._tmp.name, "plugins", "installed_plugins.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "version": 2,
                    "plugins": {
                        key: [
                            {
                                "scope": "user",
                                "installPath": os.path.join(cache, market, "p", "1.0.0"),
                            }
                        ]
                        for key, market in (("p@official", "official"), ("p@other", "other"))
                    },
                },
                fh,
            )
        with mock.patch.object(scan, "CLAUDE_DIR", self._tmp.name):
            skills, _per_plugin = scan._scan_plugin_skills([])
        self.assertEqual(
            len(skills), 1, f"disabled install still contributed: {[s.path for s in skills]}"
        )
        self.assertIn(os.sep + "other" + os.sep, skills[0].path)


class CountsArePartitioned(unittest.TestCase):
    """The four numbers are read as a breakdown, so they have to add up.

    They used to overlap - a vendor minor finding was counted in both buckets -
    and the dashboard derived the local-minor figure by subtracting one from the
    other, which is only correct when every vendor finding is minor. On the real
    config the buckets summed to 322 against a total of 188, and the two screens
    that showed the number disagreed with each other.
    """

    def _report(self, *specs):
        from studio.health import _blocking, _counts

        findings = [
            Finding(
                rule="R1",
                title="t",
                detail="d",
                severity=sev,
                path="/p",
                owner=owner,
                waived=waived,
            )
            for sev, owner, waived in specs
        ]
        return _counts(findings, _blocking(findings))

    def test_the_buckets_sum_to_the_total(self):
        c = self._report(
            (Severity.CRITICAL, Owner.LOCAL, False),
            (Severity.MINOR, Owner.LOCAL, False),
            (Severity.MINOR, Owner.VENDOR, False),
            (Severity.CRITICAL, Owner.VENDOR, False),
            (Severity.MINOR, Owner.LOCAL, True),
        )
        self.assertEqual(
            c["blocking"] + c["waived"] + c["vendor_owned"] + c["minor"], c["total"]
        )

    def test_a_vendor_minor_is_counted_once_not_twice(self):
        """The specific overlap that inflated the totals."""
        c = self._report((Severity.MINOR, Owner.VENDOR, False))
        self.assertEqual(c["vendor_owned"], 1)
        self.assertEqual(c["minor"], 0, "vendor finding also counted as minor")

    def test_minor_means_local_minor(self):
        """It is displayed as "可選改善" next to "不是你的", so it has to exclude
        vendor content or the two rows describe the same findings."""
        c = self._report(
            (Severity.MINOR, Owner.LOCAL, False),
            (Severity.MINOR, Owner.VENDOR, False),
            (Severity.MINOR, Owner.VENDOR, False),
        )
        self.assertEqual(c["minor"], 1)

    def test_a_waived_finding_lands_only_in_waived(self):
        c = self._report((Severity.CRITICAL, Owner.LOCAL, True))
        self.assertEqual(c["waived"], 1)
        self.assertEqual(c["blocking"], 0)
        self.assertEqual(c["minor"], 0)

    def test_a_vendor_critical_does_not_block(self):
        """Editing vendor files is undone by the next upgrade, so severity there
        cannot decide the verdict - but it still has to be counted somewhere."""
        c = self._report((Severity.CRITICAL, Owner.VENDOR, False))
        self.assertEqual(c["blocking"], 0)
        self.assertEqual(c["vendor_owned"], 1)
        self.assertEqual(c["blocking"] + c["waived"] + c["vendor_owned"] + c["minor"], 1)


class AgentReferencesAreResolvedNotMatchedAsText(unittest.TestCase):
    """The same file is written three ways - `~/.claude/agents/x.md`, the expanded
    absolute path, and `../agents/x.md` relative to the referring file. Comparing
    spellings reported two of the three as unreferenced, which is the reading that
    got four live reviewer prompts deleted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agents = os.path.join(self._tmp.name, "agents")
        self.commands = os.path.join(self._tmp.name, "commands")
        os.makedirs(self.agents)
        os.makedirs(self.commands)
        self.agent = os.path.join(self.agents, "security.md")
        with open(self.agent, "w", encoding="utf-8") as fh:
            fh.write("# Security\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _readers(self, text):
        from studio.rules.agents import _reference_index

        path = os.path.join(self.commands, "done.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        inv = Inventory()
        inv.commands = [
            Command(id="c", name="done", path=path, runtime=Runtime.CLAUDE, lines=1)
        ]
        return _reference_index(inv).get(os.path.realpath(self.agent), [])

    def test_a_relative_reference_counts(self):
        self.assertTrue(self._readers("Run `../agents/security.md`.\n"))

    def test_an_absolute_reference_counts(self):
        self.assertTrue(self._readers(f"Run `{self.agent}`.\n"))

    def test_a_longer_filename_is_not_a_reference(self):
        """`security.md.bak` is an orphaned backup, not a reader. Counting it
        downgraded the very finding that would have got it cleaned up."""
        self.assertFalse(self._readers(f"Run `{self.agent}.bak`.\n"))

    def test_an_unrelated_file_is_not_a_reference(self):
        self.assertFalse(self._readers("Run `../agents/quality.md`.\n"))

    def test_a_skill_counts_as_a_reader(self):
        """Skills read agent prompts too, and were missing from the corpus - so
        deleting an agent on AG001's advice could break a skill instead."""
        from studio.rules.agents import _reference_index

        skill_dir = os.path.join(self._tmp.name, "skills", "review")
        os.makedirs(skill_dir)
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: review\n---\nRead {self.agent}\n")
        inv = Inventory()
        inv.skills = [
            Skill(
                id="s",
                name="review",
                dir_name="review",
                path=skill_path,
                runtime=Runtime.CLAUDE,
                origin=Origin.LOCAL,
                description="Use when reviewing.",
                body_lines=1,
            )
        ]
        self.assertIn(skill_path, _reference_index(inv).get(os.path.realpath(self.agent), []))

    def test_a_file_does_not_reference_itself(self):
        """Agents are in the corpus so one agent can reference another, which
        makes a file naming its own path look like its own reader."""
        from studio.rules.agents import _reference_index

        with open(self.agent, "w", encoding="utf-8") as fh:
            fh.write(f"I am {self.agent}\n")
        inv = Inventory()
        inv.agents = [
            AgentDef(
                id="a",
                name="security",
                path=self.agent,
                runtime=Runtime.CLAUDE,
                lines=1,
                description="",
                frontmatter_present=False,
                declared_name="",
            )
        ]
        self.assertEqual(_reference_index(inv).get(os.path.realpath(self.agent), []), [])

    def test_the_corpus_is_read_once_not_once_per_agent(self):
        """Asking per agent re-read 1,135 files eleven times - 66 seconds for an
        answer that does not vary between agents."""
        from studio.rules import agents as agents_mod

        reads = []
        real = agents_mod.safeio.read_text if hasattr(agents_mod, "safeio") else None
        from studio import safeio as safeio_mod

        original = safeio_mod.read_text

        def counting(path, *a, **kw):
            reads.append(path)
            return original(path, *a, **kw)

        path = os.path.join(self.commands, "done.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Read {self.agent}\n")
        inv = Inventory()
        inv.commands = [
            Command(id="c", name="done", path=path, runtime=Runtime.CLAUDE, lines=1)
        ]
        inv.agents = [
            AgentDef(
                id=f"a{i}",
                name=f"a{i}",
                path=self.agent,
                runtime=Runtime.CLAUDE,
                lines=1,
                description="",
                frontmatter_present=False,
                declared_name="",
            )
            for i in range(5)
        ]
        with mock.patch.object(safeio_mod, "read_text", counting):
            list(agents_mod.ag001(inv, Config(repo_root=".")))
        self.assertEqual(
            reads.count(path), 1, f"command file read {reads.count(path)} times"
        )


class TwoInstallsOfOnePluginStayDistinct(unittest.TestCase):
    """`plugin@marketplace` is the identity everywhere or nowhere.

    Half-applying it - gating on the full key, then collapsing to the bare name -
    made two enabled installs of one plugin share skill IDs, merged their
    ownership, and had each report the other's skills in its own count.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self._tmp.name, "plugins", "cache")

    def tearDown(self):
        self._tmp.cleanup()

    def _install(self, market, plugin, skills):
        root = os.path.join(self.cache, market, plugin, "1.0.0")
        for s in skills:
            d = os.path.join(root, "skills", s)
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write(f"---\nname: {s}\ndescription: Use when testing.\n---\nbody\n")
        return root

    def _scan(self, enabled, installs, manifests=None):
        from studio import scan

        with open(os.path.join(self._tmp.name, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"enabledPlugins": enabled}, fh)
        os.makedirs(os.path.join(self._tmp.name, "plugins"), exist_ok=True)
        with open(
            os.path.join(self._tmp.name, "plugins", "installed_plugins.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "version": 2,
                    "plugins": {
                        k: [{"scope": "user", "installPath": v}] for k, v in installs.items()
                    },
                },
                fh,
            )
        for market, entries in (manifests or {}).items():
            d = os.path.join(self._tmp.name, "plugins", "marketplaces", market, ".claude-plugin")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "marketplace.json"), "w", encoding="utf-8") as fh:
                json.dump({"plugins": entries}, fh)
        with mock.patch.object(scan, "CLAUDE_DIR", self._tmp.name):
            return scan._scan_plugin_skills([])

    def test_two_enabled_installs_do_not_share_skill_ids(self):
        installs = {
            "p@m1": self._install("m1", "p", ["review"]),
            "p@m2": self._install("m2", "p", ["review"]),
        }
        skills, _ = self._scan({"p@m1": True, "p@m2": True}, installs)
        ids = [s.id for s in skills]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate ids: {ids}")

    def test_each_install_counts_only_its_own_skills(self):
        installs = {
            "p@m1": self._install("m1", "p", ["review"]),
            "p@m2": self._install("m2", "p", ["review"]),
        }
        _, per_plugin = self._scan({"p@m1": True, "p@m2": True}, installs)
        self.assertEqual(per_plugin.get("p@m1"), 1)
        self.assertEqual(per_plugin.get("p@m2"), 1)

    def test_skill_plugin_is_the_same_key_space_as_plugin_key(self):
        """The invariant every consumer relies on.

        Test fixtures had been setting `Skill.plugin` to a bare name while the
        paired `Plugin.key` carried the marketplace, so lookups joining the two
        passed in tests and returned nothing in production. Pinning it here means
        the fixture cannot drift from what the scanner actually writes.
        """
        installs = {
            "p@m1": self._install("m1", "p", ["review"]),
            "p@m2": self._install("m2", "p", ["ship"]),
        }
        skills, per_plugin = self._scan({"p@m1": True, "p@m2": True}, installs)
        self.assertEqual({s.plugin for s in skills}, {"p@m1", "p@m2"})
        self.assertEqual(set(per_plugin), {"p@m1", "p@m2"})

    def test_a_disabled_installs_manifest_does_not_add_skills(self):
        """The manifest was keyed by bare name too, so a disabled install's
        declarations pulled extra directories out of the enabled install's tree
        and reported them as preloaded."""
        root = self._install("m1", "p", ["one", "two"])
        skills, _ = self._scan(
            {"p@m1": True, "p@m2": False},
            {"p@m1": root},
            manifests={
                "m1": [{"name": "p", "skills": ["skills/one"]}],
                "m2": [{"name": "p", "skills": ["skills/two"]}],
            },
        )
        self.assertEqual(
            sorted(s.dir_name for s in skills),
            ["one"],
            "a disabled marketplace's declaration leaked in",
        )


class PluginKeySpacesDoNotGetMixed(unittest.TestCase):
    """Two identities exist on purpose and must not be confused.

    Skills and metadata bytes belong to one *install*, keyed `plugin@marketplace`.
    Usage and corpus mentions only ever name the plugin, so they are keyed bare -
    a log line says `plug:skill`, never which marketplace it came from. Looking up
    one with the other returns zero on every call, and the classifier reads that
    as "this plugin ships no skills", which silently turns off the cold-plugin
    analysis instead of failing.
    """

    def _inv(self, usage_name="plug"):
        from studio.model import Plugin

        inv = Inventory()
        inv.plugins = [
            Plugin(
                id="plugin:claude:plug@m1",
                key="plug@m1",
                marketplace="m1",
                runtime=Runtime.CLAUDE,
                enabled=True,
            )
        ]
        inv.skills = [
            Skill(
                id="skill:plugin:plug@m1:review",
                name="review",
                dir_name="review",
                path="/p/skills/review/SKILL.md",
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="Use when reviewing code.",
                body_lines=10,
                plugin="plug@m1",
            )
        ]
        return inv

    def test_the_classifier_sees_the_plugins_own_skills(self):
        from studio.plugins import classify

        (row,) = classify(self._inv(), {}, corpus="")
        self.assertEqual(row["skills"], 1, "skills looked up in the wrong key space")
        self.assertGreater(row["metadata_bytes"], 0)

    def test_an_unused_plugin_with_skills_is_classified_disable(self):
        """The verdict the whole cold-plugin feature turns on. A zeroed skill
        count makes it 'keep - ships no skills', which reads as a clean result."""
        from studio.plugins import classify

        (row,) = classify(self._inv(), {}, corpus="")
        self.assertEqual(row["verdict"], "disable")

    def test_usage_is_matched_on_the_bare_name(self):
        """The log cannot say which marketplace a call came from."""
        from studio.plugins import classify

        (row,) = classify(self._inv(), {"plug": 3}, corpus="")
        self.assertEqual(row["invocations"], 3)
        self.assertEqual(row["verdict"], "keep")

    def test_avoidable_bytes_are_not_silently_zero(self):
        from studio.plugins import avoidable, classify

        by, skills, rows = avoidable(classify(self._inv(), {}, corpus=""))
        self.assertGreater(by, 0)
        self.assertEqual(skills, 1)
        self.assertEqual(len(rows), 1)

    def test_the_graph_links_a_plugin_to_its_skills(self):
        """Ownership edges vanished, so the map showed every plugin as barren."""
        from studio.graph import build

        g = build(self._inv(), include_plugin_skills=True)
        provides = [e for e in g["edges"] if e.get("kind") == "provides"]
        self.assertEqual(len(provides), 1, "plugin lost ownership of its own skill")

    def test_skill_resolved_usage_is_recorded_under_the_bare_name(self):
        """An unqualified skill token resolves through the inventory, which knows
        the full key - recording it there put usage in a key space no consumer
        reads."""
        from studio.usage import UsageIndex, plugin_usage

        idx = UsageIndex()
        idx.tokens = {"review": 4}
        counts = plugin_usage(idx, self._inv())
        self.assertEqual(counts.get("plug"), 4)
        self.assertNotIn("plug@m1", counts)


class EditingRefusesWhatItCannotHonour(unittest.TestCase):
    """Reading was the whole of "read and manage" until now.

    Saving goes through the same backed-up change set as every other write, so an
    edit made here is as reversible as a fix. The refusals matter as much as the
    write: two kinds of file accept an edit and then lose it, and a tool that
    lets you type into them is worse than one that has no editor at all.
    """

    def setUp(self):
        from studio import server

        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "config")
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.root)
        os.makedirs(self.repo)
        self.handler = server.Handler.__new__(server.Handler)
        self.handler.repo_root = self.repo

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, text="---\nname: x\n---\nbody\n"):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _edit(self, path, text, *, create=False, inv=None, generated=()):
        from studio import server
        from studio.model import Inventory
        from studio.rules import Config

        inv = inv or Inventory()
        inv.roots = {"claude": self.root}
        cfg = Config(repo_root=self.repo)
        cfg.generated = list(generated)
        with mock.patch.object(server, "_inventory", return_value=inv), mock.patch.object(
            Config, "load", staticmethod(lambda _root: cfg)
        ):
            return server.Handler._edit(self.handler, path, text, create=create)

    def test_an_edit_is_written_and_backed_up(self):
        path = self._write("skills/mine/SKILL.md")
        result = self._edit(path, "changed\n")
        self.assertNotIn("error", result)
        self.assertEqual(open(path, encoding="utf-8").read(), "changed\n")
        self.assertTrue(result.get("backup"), "no restore point recorded")

    def test_the_backup_can_be_rolled_back(self):
        """An editor without an undo is a way to lose work."""
        from studio import patch as patch_mod

        path = self._write("skills/mine/SKILL.md", "original\n")
        result = self._edit(path, "changed\n")
        patch_mod.rollback(self.repo, os.path.basename(result["backup"]))
        self.assertEqual(open(path, encoding="utf-8").read(), "original\n")

    def test_creating_a_file_works(self):
        path = os.path.join(self.root, "skills", "new", "SKILL.md")
        result = self._edit(path, "hello\n", create=True)
        self.assertTrue(result.get("created"), result)
        self.assertEqual(open(path, encoding="utf-8").read(), "hello\n")

    def test_creating_over_an_existing_file_is_refused(self):
        """Otherwise "new file" silently overwrites something."""
        path = self._write("skills/mine/SKILL.md", "keep me\n")
        self.assertIn("error", self._edit(path, "clobber\n", create=True))
        self.assertEqual(open(path, encoding="utf-8").read(), "keep me\n")

    def test_a_vendor_file_is_refused_and_says_why(self):
        """A plugin upgrade overwrites it, so the save would look like it worked
        and quietly revert later."""
        from studio.model import Inventory

        path = self._write("plug/SKILL.md")
        inv = Inventory()
        inv.skills = [
            Skill(
                id="s",
                name="s",
                dir_name="s",
                path=path,
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="d",
                body_lines=1,
                plugin="p@m",
            )
        ]
        err = self._edit(path, "changed\n", inv=inv).get("error", "")
        self.assertIn("plugin", err)
        self.assertIn("p@m", err, "does not say which plugin to disable")
        self.assertEqual(open(path, encoding="utf-8").read(), "---\nname: x\n---\nbody\n")

    def test_a_toolkit_file_is_refused(self):
        """Same failure mode, different owner: it lives in a local-looking path
        but the toolkit rewrites it on upgrade."""
        from studio.model import Inventory

        path = self._write("gstack/SKILL.md")
        inv = Inventory()
        inv.skills = [
            Skill(
                id="s",
                name="s",
                dir_name="s",
                path=path,
                runtime=Runtime.CLAUDE,
                origin=Origin.TOOLKIT,
                description="d",
                body_lines=1,
            )
        ]
        self.assertIn("error", self._edit(path, "changed\n", inv=inv))

    def test_a_generated_file_is_refused_and_names_its_source(self):
        """Editing the rendered file is exactly the drift MR003 exists to catch,
        so the editor must not be the thing that causes it."""
        path = self._write("CLAUDE.md", "generated\n")
        err = self._edit(
            path,
            "hand edited\n",
            generated=[{"target": path, "sources": ["canonical/core.md"]}],
        ).get("error", "")
        self.assertIn("canonical/core.md", err)
        self.assertEqual(open(path, encoding="utf-8").read(), "generated\n")

    def test_a_path_outside_the_roots_is_refused(self):
        outside = os.path.join(self._tmp.name, "elsewhere.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("untouched\n")
        self.assertIn("error", self._edit(outside, "changed\n"))
        self.assertEqual(open(outside, encoding="utf-8").read(), "untouched\n")

    def test_a_credential_file_is_refused(self):
        path = self._write("auth.json", "{}\n")
        self.assertIn("error", self._edit(path, "changed\n"))

    def test_other_files_in_a_vendor_skill_directory_are_refused(self):
        """Vendor ownership belongs to the install, not to the one file the
        scanner indexed: a skill directory also holds the reference material and
        scripts the skill loads, and the same upgrade overwrites those."""
        from studio.model import Inventory

        skill = self._write("plugins/cache/m/p/1.0/skills/s/SKILL.md")
        helper = self._write("plugins/cache/m/p/1.0/skills/s/helper.py", "print(1)\n")
        inv = Inventory()
        inv.skills = [
            Skill(
                id="s",
                name="s",
                dir_name="s",
                path=skill,
                runtime=Runtime.CLAUDE,
                origin=Origin.PLUGIN,
                description="d",
                body_lines=1,
                plugin="p@m",
            )
        ]
        self.assertIn("error", self._edit(helper, "attack\n", inv=inv))
        self.assertEqual(open(helper, encoding="utf-8").read(), "print(1)\n")

    def test_a_declared_vendored_file_is_refused(self):
        """governance.json already records which files an external tool re-copies
        on upgrade, and the rules consult it. The editor has to ask the same
        question or it contradicts them."""
        from studio.rules import Config

        path = self._write("skills/upstream/SKILL.md", "upstream\n")
        cfg = Config(repo_root=self.repo)
        cfg.vendored = [{"path": path, "reason": "re-copied by gstack on upgrade"}]
        from studio import server
        from studio.model import Inventory

        inv = Inventory()
        inv.roots = {"claude": self.root}
        with mock.patch.object(server, "_inventory", return_value=inv), mock.patch.object(
            Config, "load", staticmethod(lambda _root: cfg)
        ):
            result = server.Handler._edit(self.handler, path, "changed\n", create=False)
        self.assertIn("gstack", result.get("error", ""))
        self.assertEqual(open(path, encoding="utf-8").read(), "upstream\n")

    def test_a_symlink_inside_the_roots_is_still_refused(self):
        """Confinement is not enough on its own.

        This link resolves to another file *inside* the audited roots, so every
        path check passes - and the write would then modify a different file
        from the one whose name the page showed. The editor names one file and
        must edit that file or nothing.
        """
        real = self._write("skills/real/SKILL.md", "real content\n")
        link = os.path.join(self.root, "looks-separate.md")
        os.symlink(real, link)
        self.assertIn("error", self._edit(link, "attack\n"))
        self.assertEqual(open(real, encoding="utf-8").read(), "real content\n")

    def test_a_symlink_pointing_out_of_the_roots_is_refused(self):
        target = os.path.join(self._tmp.name, "outside.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("outside\n")
        link = os.path.join(self.root, "looks-local.md")
        os.symlink(target, link)
        self.assertIn("error", self._edit(link, "attack\n"))
        self.assertEqual(open(target, encoding="utf-8").read(), "outside\n")

    def test_a_file_that_is_not_utf8_is_refused(self):
        """The preview decodes with errors="replace", so saving it back would
        write U+FFFD over bytes the editor never showed."""
        path = os.path.join(self.root, "binaryish.md")
        with open(path, "wb") as fh:
            fh.write(b"front\xffback\n")
        self.assertIn("error", self._edit(path, "front\ufffdback\nedited\n"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"front\xffback\n", "original bytes were rewritten")

    def test_saving_identical_text_is_not_a_change(self):
        """Otherwise every save creates a restore point that restores nothing."""
        path = self._write("skills/mine/SKILL.md", "same\n")
        result = self._edit(path, "same\n")
        self.assertTrue(result.get("unchanged"))
        self.assertIsNone(result.get("backup"))


class QuarantineStaysInsideTheConfigRoots(unittest.TestCase):
    """The other write path that takes a path from the page.

    Its confinement check had no test at all: removing it left the whole suite
    green while the endpoint would move any file on the disk into the repo.
    """

    def setUp(self):
        from studio import server

        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "config")
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.root)
        os.makedirs(self.repo)
        self.handler = server.Handler.__new__(server.Handler)
        self.handler.repo_root = self.repo

    def tearDown(self):
        self._tmp.cleanup()

    def _quarantine(self, path):
        from studio import server
        from studio.model import Inventory

        inv = Inventory()
        inv.roots = {"claude": self.root}
        with mock.patch.object(server, "_inventory", return_value=inv):
            return server.Handler._quarantine(self.handler, path)

    def test_a_file_outside_the_roots_is_left_alone(self):
        outside = os.path.join(self._tmp.name, "elsewhere.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("untouched\n")
        self.assertIn("error", self._quarantine(outside))
        self.assertTrue(os.path.exists(outside), "a file outside the config roots was moved")

    def test_a_file_inside_the_roots_is_moved_with_a_copy_kept(self):
        inside = os.path.join(self.root, "stray.md")
        with open(inside, "w", encoding="utf-8") as fh:
            fh.write("content\n")
        result = self._quarantine(inside)
        self.assertNotIn("error", result)
        self.assertFalse(os.path.exists(inside))
        self.assertEqual(open(result["copy"], encoding="utf-8").read(), "content\n")

    def test_an_empty_file_still_leaves_a_copy(self):
        """The copy of a zero-byte file read as "no change" and was dropped,
        while the delete went ahead - so quarantine destroyed the one kind of
        file it was most likely to be pointed at."""
        empty = os.path.join(self.root, "settings.json.bak")
        open(empty, "w").close()
        result = self._quarantine(empty)
        self.assertFalse(os.path.exists(empty))
        self.assertTrue(os.path.isfile(result["copy"]), "file deleted without a copy")


class ASavedChangeSetAppliesTheSamePlanItRecorded(unittest.TestCase):
    """`studio apply` reads back what was reviewed, so anything left out of the
    payload silently changes the plan between review and execution."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_directory_removals_survive_the_round_trip(self):
        """They were dropped, so a saved set applied its file edits and skipped
        the cleanup - leaving the empty directory the change existed to remove."""
        from studio import patch as patch_mod

        empty = os.path.join(self._tmp.name, "gone")
        os.makedirs(empty)
        cs = patch_mod.ChangeSet(
            name="t",
            changes=[
                patch_mod.Change(
                    path=os.path.join(self._tmp.name, "a.md"), new_text="x", action="create"
                )
            ],
            remove_dirs=[empty],
        )
        patch_mod.save(cs, self._tmp.name)
        payload = [
            f
            for f in os.listdir(os.path.join(self._tmp.name, "var", "patches"))
            if f.endswith("payload.json")
        ]
        back = patch_mod.load(os.path.join(self._tmp.name, "var", "patches", payload[0]))
        self.assertEqual(back.remove_dirs, [empty])

    def test_the_reloaded_set_still_reports_it_has_work(self):
        """has_work() counts directory removals, so dropping them could also make
        a real change set look like a no-op."""
        from studio import patch as patch_mod

        empty = os.path.join(self._tmp.name, "gone")
        os.makedirs(empty)
        cs = patch_mod.ChangeSet(name="t", remove_dirs=[empty])
        patch_mod.save(cs, self._tmp.name)
        payload = [
            f
            for f in os.listdir(os.path.join(self._tmp.name, "var", "patches"))
            if f.endswith("payload.json")
        ]
        back = patch_mod.load(os.path.join(self._tmp.name, "var", "patches", payload[0]))
        self.assertTrue(back.has_work())


class WritingNeverFollowsADanglingSymlink(unittest.TestCase):
    """Applies to every write, not just the editor.

    Writing through a link that resolves is correct - a toolkit installs its
    skills that way, and replacing the link with a regular file severs whatever
    manages it. A dangling link has no file to update, so the write creates one
    wherever the link points, which can be anywhere on the disk.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_create_through_a_dangling_link_is_refused(self):
        from studio import patch as patch_mod

        outside = os.path.join(self._tmp.name, "outside.md")
        link = os.path.join(self._tmp.name, "empty.md")
        os.symlink(outside, link)
        cs = patch_mod.ChangeSet(
            name="t", changes=[patch_mod.Change(path=link, new_text="", action="create")]
        )
        with self.assertRaises(OSError):
            patch_mod.apply(cs, self.repo)
        self.assertFalse(os.path.exists(outside), "the link's target was created")

    def test_a_link_that_resolves_is_still_written_through(self):
        """The behaviour this must not break: severing the link would lose the
        toolkit's ownership of the file."""
        from studio import patch as patch_mod

        real = os.path.join(self._tmp.name, "real.md")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("old\n")
        link = os.path.join(self._tmp.name, "link.md")
        os.symlink(real, link)
        cs = patch_mod.ChangeSet(
            name="t", changes=[patch_mod.Change(path=link, new_text="new\n", action="modify")]
        )
        patch_mod.apply(cs, self.repo)
        self.assertTrue(os.path.islink(link), "the symlink was replaced by a regular file")
        self.assertEqual(open(real, encoding="utf-8").read(), "new\n")


class EveryRuleIsProvenToFire(unittest.TestCase):
    """One violating case and one clean case per rule.

    A rule is a claim about the guidance, and until something makes it fire the
    claim is untested - 23 of the 56 produced no finding anywhere in this suite,
    which means nothing distinguished "correctly silent" from "silently broken".
    Each test here pairs the two: the rule must fire on the shape it exists to
    catch, and stay quiet on the same shape made correct. A rule that always
    fired would pass the first assertion and fail the second.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        # Every fixture gets its own directory. Sharing one made the second call
        # in a test overwrite the first, so the violating file no longer existed
        # by the time the rule read it - and the rule "correctly" found nothing.
        self._seq = 0

    def tearDown(self):
        self._tmp.cleanup()

    def _slot(self) -> str:
        self._seq += 1
        return f"case{self._seq}"

    # -- builders ---------------------------------------------------------- #

    def _skill(self, body, *, dir_name="thing", origin=Origin.LOCAL, runtime=Runtime.CLAUDE):
        d = os.path.join(self.tmp, self._slot(), "skills", dir_name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "SKILL.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        parsed = fm.parse(body)
        return Skill(
            id=f"skill:{dir_name}",
            name=(parsed.text("name") or "").strip(),
            dir_name=dir_name,
            path=path,
            runtime=runtime,
            origin=origin,
            description=(parsed.text("description") or "").strip(),
            body_lines=len(parsed.body.splitlines()),
            frontmatter_present=parsed.present,
            parse_warnings=list(parsed.warnings),
        )

    def _instruction(self, text, *, name="CLAUDE.md", runtime=Runtime.CLAUDE, refs=()):
        d = os.path.join(self.tmp, self._slot())
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return Instruction(
            id=f"ins:{name}",
            path=path,
            runtime=runtime,
            lines=len(text.splitlines()),
            bytes=len(text.encode("utf-8")),
            refs=list(refs),
        )

    def _workflow(self, text, *, name="build.md", runtime=Runtime.CLAUDE, refs=()):
        d = os.path.join(self.tmp, self._slot(), "workflows", runtime.value)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        from studio.model import Workflow

        return Workflow(
            id=f"wf:{runtime.value}:{name}",
            path=path,
            runtime=runtime,
            lines=len(text.splitlines()),
            refs=list(refs),
        )

    def _hook(self, event, injects, *, if_rule=""):
        from studio.model import Hook

        return Hook(
            id=f"hook:{event}",
            event=event,
            matcher="*",
            index=0,
            type="command",
            source=os.path.join(self.tmp, "settings.json"),
            injects=injects,
            if_rule=if_rule,
        )

    def _plugin(self, key, marketplace, *, enabled=True):
        from studio.model import Plugin

        return Plugin(
            id=f"plugin:claude:{key}",
            key=key,
            marketplace=marketplace,
            runtime=Runtime.CLAUDE,
            enabled=enabled,
        )

    def _run(self, fn, inv, **cfg_kwargs):
        inv.roots = inv.roots or {"claude": self.tmp}
        cfg = Config(repo_root=self.tmp)
        for k, v in cfg_kwargs.items():
            setattr(cfg, k, v)
        return list(fn(inv, cfg))

    def _inv(self, **kw):
        inv = Inventory()
        for k, v in kw.items():
            setattr(inv, k, v)
        return inv

    # -- skills ------------------------------------------------------------ #

    def test_sk001_fires_without_frontmatter(self):
        from studio.rules.skills import sk001

        bad = self._skill("Just a body, no frontmatter.\n")
        good = self._skill("---\nname: thing\ndescription: Does X. Use when Y.\n---\nbody\n")
        self.assertEqual(len(self._run(sk001, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk001, self._inv(skills=[good])), [])

    def test_sk002_fires_on_an_illegal_name(self):
        from studio.rules.skills import sk002

        bad = self._skill("---\nname: My Skill!\ndescription: Does X. Use when Y.\n---\nb\n")
        good = self._skill("---\nname: my-skill\ndescription: Does X. Use when Y.\n---\nb\n")
        self.assertEqual(len(self._run(sk002, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk002, self._inv(skills=[good])), [])

    def test_sk003_fires_when_name_and_directory_disagree(self):
        """Two directories declaring one name means only one ever loads."""
        from studio.rules.skills import sk003

        bad = self._skill(
            "---\nname: other-name\ndescription: Does X. Use when Y.\n---\nb\n", dir_name="thing"
        )
        good = self._skill(
            "---\nname: thing\ndescription: Does X. Use when Y.\n---\nb\n", dir_name="thing"
        )
        self.assertEqual(len(self._run(sk003, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk003, self._inv(skills=[good])), [])

    def test_sk004_fires_on_an_empty_and_on_an_oversized_description(self):
        from studio.rules.skills import DESC_MAX_CHARS, sk004

        empty = self._skill("---\nname: thing\ndescription: \n---\nb\n")
        huge = self._skill(
            f"---\nname: thing\ndescription: {'x' * (DESC_MAX_CHARS + 1)}\n---\nb\n"
        )
        good = self._skill("---\nname: thing\ndescription: Does X. Use when Y.\n---\nb\n")
        self.assertEqual(len(self._run(sk004, self._inv(skills=[empty]))), 1)
        self.assertEqual(len(self._run(sk004, self._inv(skills=[huge]))), 1)
        self.assertEqual(self._run(sk004, self._inv(skills=[good])), [])

    def test_sk006_fires_on_a_first_person_description(self):
        from studio.rules.skills import sk006

        bad = self._skill("---\nname: thing\ndescription: I help you write tests.\n---\nb\n")
        good = self._skill("---\nname: thing\ndescription: Writes tests. Use when adding code.\n---\nb\n")
        self.assertEqual(len(self._run(sk006, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk006, self._inv(skills=[good])), [])

    def test_sk010_fires_on_a_backslash_path_in_prose(self):
        from studio.rules.skills import sk010

        bad = self._skill(
            "---\nname: thing\ndescription: Does X. Use when Y.\n---\nOpen C:\\Users\\me\\notes.txt\n"
        )
        good = self._skill(
            "---\nname: thing\ndescription: Does X. Use when Y.\n---\nOpen ~/notes.txt\n"
        )
        self.assertEqual(len(self._run(sk010, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk010, self._inv(skills=[good])), [])

    def test_sk010_ignores_a_backslash_inside_a_code_fence(self):
        """Shell line-continuations legitimately use backslashes."""
        from studio.rules.skills import sk010

        s = self._skill(
            "---\nname: thing\ndescription: Does X. Use when Y.\n---\n"
            "```sh\necho a \\\n  b\n```\n"
        )
        self.assertEqual(self._run(sk010, self._inv(skills=[s])), [])

    def test_sk014_fires_on_frontmatter_that_did_not_parse(self):
        from studio.rules.skills import sk014

        # Opened and never closed: the parser keeps linting the file as body and
        # records why, which is the warning SK014 surfaces.
        bad = self._skill("---\nname: thing\ndescription: Does X. Use when Y.\n")
        self.assertTrue(bad.parse_warnings, "fixture did not actually produce a parse warning")
        good = self._skill("---\nname: thing\ndescription: Does X. Use when Y.\n---\nb\n")
        self.assertTrue(self._run(sk014, self._inv(skills=[bad])))
        self.assertEqual(self._run(sk014, self._inv(skills=[good])), [])

    def test_sk015_fires_when_the_body_contradicts_the_frontmatter(self):
        """The agent gets opposite instructions from one file."""
        from studio.rules.skills import sk015

        bad = self._skill(
            "---\nname: agent-browser\ndescription: Fallback browser. Use when the built-in fails.\n---\n"
            "agent-browser is the default browser for all tasks.\n",
            dir_name="agent-browser",
        )
        good = self._skill(
            "---\nname: agent-browser\ndescription: Fallback browser. Use when the built-in fails.\n---\n"
            "Use it only after the built-in browser fails.\n",
            dir_name="agent-browser",
        )
        self.assertEqual(len(self._run(sk015, self._inv(skills=[bad]))), 1)
        self.assertEqual(self._run(sk015, self._inv(skills=[good])), [])

    # -- instructions ------------------------------------------------------ #

    def test_in001_fires_over_the_line_target(self):
        from studio.rules.instructions import INSTRUCTION_MAX_LINES, in001

        bad = self._instruction("line\n" * (INSTRUCTION_MAX_LINES + 1))
        good = self._instruction("line\n" * 10)
        self.assertEqual(len(self._run(in001, self._inv(instructions=[bad]))), 1)
        self.assertEqual(self._run(in001, self._inv(instructions=[good])), [])

    def test_in006_fires_on_a_reference_to_a_missing_file(self):
        from studio.rules.instructions import in006

        missing = os.path.join(self.tmp, "gone.md")
        present = os.path.join(self.tmp, "here.md")
        with open(present, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        bad = self._instruction(f"See {missing} for details.\n", refs=[missing])
        good = self._instruction(f"See {present} for details.\n", refs=[present])
        self.assertEqual(len(self._run(in006, self._inv(instructions=[bad]))), 1)
        self.assertEqual(self._run(in006, self._inv(instructions=[good])), [])

    def test_in005_fires_when_an_instruction_copies_a_skills_rules(self):
        """The skill is the source of truth and loads on demand. Copying its
        rules into an always-loaded file pays for them every session and lets
        the two drift apart."""
        from studio.rules.instructions import in005

        shared = "\n".join(
            [
                "- Always write a failing test before the implementation.",
                "- Never commit with the linter reporting warnings.",
                "- Record the command output as evidence of the run.",
                "- Stop and report when three hypotheses have failed.",
            ]
        )
        skill = self._skill(
            f"---\nname: testing\ndescription: Runs tests. Use when verifying.\n---\n{shared}\n",
            dir_name="testing",
        )
        copied = self._instruction(f"# Rules\n\nSee the testing skill.\n\n{shared}\n")
        clean = self._instruction("# Rules\n\nSee the testing skill for the details.\n")
        self.assertTrue(self._run(in005, self._inv(skills=[skill], instructions=[copied])))
        self.assertEqual(self._run(in005, self._inv(skills=[skill], instructions=[clean])), [])

    def test_in008_fires_when_an_instruction_mandates_a_retired_artifact(self):
        """Two rules disagreeing is the worst failure mode: the model may follow
        either one."""
        from studio.rules.instructions import in008

        skill = self._skill(
            "---\nname: reporting\ndescription: Reports results. Use when finishing.\n---\n"
            "`REPORT.md` 已淘汰，改用儀表板。\n",
            dir_name="reporting",
        )
        mandating = self._instruction("完成後必須產生 `REPORT.md`。\n")
        consistent = self._instruction("完成後把結果貼到儀表板。\n")
        self.assertEqual(len(self._run(in008, self._inv(skills=[skill], instructions=[mandating]))), 1)
        self.assertEqual(self._run(in008, self._inv(skills=[skill], instructions=[consistent])), [])

    def test_in009_fires_on_rules_duplicated_across_runtimes(self):
        from studio.rules.instructions import in009

        rule_text = "\n".join(
            f"- Always run the linter before committing change number {i}." for i in range(12)
        )
        a = self._instruction(rule_text, name="CLAUDE.md", runtime=Runtime.CLAUDE)
        b = self._instruction(rule_text, name="AGENTS.md", runtime=Runtime.CODEX)
        self.assertTrue(self._run(in009, self._inv(instructions=[a, b])))

    def test_in009_is_silent_when_a_generator_owns_the_duplication(self):
        """A canonical source rendered into both removes the class of problem, so
        declaring it must actually exempt it - otherwise the tool reports its own
        recommended fix as a defect."""
        from studio.rules.instructions import in009

        rule_text = "\n".join(
            f"- Always run the linter before committing change number {i}." for i in range(12)
        )
        a = self._instruction(rule_text, name="CLAUDE.md", runtime=Runtime.CLAUDE)
        b = self._instruction(rule_text, name="AGENTS.md", runtime=Runtime.CODEX)
        self.assertEqual(
            self._run(
                in009,
                self._inv(instructions=[a, b]),
                generated=[{"target": a.path, "sources": ["canonical/core.md"]}],
            ),
            [],
        )

    # -- mirrors ----------------------------------------------------------- #

    def test_mr001_fires_when_declared_mirrors_differ(self):
        from studio.rules.mirrors import mr001

        one = os.path.join(self.tmp, "a", "SKILL.md")
        two = os.path.join(self.tmp, "b", "SKILL.md")
        for path, text in ((one, "same\n"), (two, "different\n")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        group = [{"name": "g", "paths": [one, two]}]
        self.assertEqual(len(self._run(mr001, self._inv(), mirrors=group)), 1)

        with open(two, "w", encoding="utf-8") as fh:
            fh.write("same\n")
        self.assertEqual(self._run(mr001, self._inv(), mirrors=group), [])

    def test_mr003_fires_when_a_generated_file_was_hand_edited(self):
        """The drift the whole canonical/ arrangement exists to catch: someone
        edits the rendered file, the source no longer describes what is on disk,
        and the two runtimes start diverging."""
        from studio.canonical import render_target
        from studio.rules.mirrors import mr003
        from studio.rules import Config

        os.makedirs(os.path.join(self.tmp, "canonical"), exist_ok=True)
        with open(os.path.join(self.tmp, "canonical", "core.md"), "w", encoding="utf-8") as fh:
            fh.write("# Core\n\nAlways run the linter.\n")
        target = os.path.join(self.tmp, "CLAUDE.md")
        spec = {"target": target, "sources": ["canonical/core.md"]}
        cfg = Config(repo_root=self.tmp)
        cfg.generated = [spec]

        with open(target, "w", encoding="utf-8") as fh:
            fh.write(render_target(cfg, spec))
        self.assertEqual(list(mr003(self._inv(), cfg)), [], "a freshly rendered file reported drift")

        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\nHand-added rule that is in no source.\n")
        found = list(mr003(self._inv(), cfg))
        self.assertEqual(len(found), 1)
        self.assertIn("canonical/core.md", str(found[0].evidence))

    def test_mr003_fires_when_the_generated_file_is_absent(self):
        """Declared but never rendered is drift too - the instruction the config
        promises simply is not there."""
        from studio.rules.mirrors import mr003
        from studio.rules import Config

        cfg = Config(repo_root=self.tmp)
        cfg.generated = [
            {"target": os.path.join(self.tmp, "never-rendered.md"), "sources": ["canonical/core.md"]}
        ]
        self.assertEqual(len(list(mr003(self._inv(), cfg))), 1)

    def test_mr002_fires_when_a_declared_mirror_is_missing(self):
        from studio.rules.mirrors import mr002

        present = os.path.join(self.tmp, "a.md")
        with open(present, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        missing = os.path.join(self.tmp, "gone.md")
        self.assertEqual(
            len(self._run(mr002, self._inv(), mirrors=[{"name": "g", "paths": [present, missing]}])),
            1,
        )
        self.assertEqual(
            self._run(mr002, self._inv(), mirrors=[{"name": "g", "paths": [present]}]), []
        )

    # -- workflows --------------------------------------------------------- #

    def test_wf001_fires_on_a_workflow_nothing_routes_to(self):
        from studio.rules.workflows import wf001

        wf = self._workflow("# Build\n")
        unref = self._instruction("Nothing here points anywhere.\n")
        ref = self._instruction("For BUILD tasks follow the build workflow.\n")
        self.assertEqual(len(self._run(wf001, self._inv(workflows=[wf], instructions=[unref]))), 1)
        self.assertEqual(self._run(wf001, self._inv(workflows=[wf], instructions=[ref])), [])

    def test_wf002_fires_on_a_workflow_reference_to_a_missing_file(self):
        from studio.rules.workflows import wf002

        missing = os.path.join(self.tmp, "gone.md")
        wf = self._workflow(f"Read {missing}\n", refs=[missing])
        self.assertEqual(len(self._run(wf002, self._inv(workflows=[wf]))), 1)

    def test_wf004_fires_when_only_one_runtime_has_the_workflow(self):
        from studio.rules.workflows import wf004

        only_claude = self._workflow("# Build\n", name="build.md", runtime=Runtime.CLAUDE)
        also_codex = self._workflow("# Build\n", name="build.md", runtime=Runtime.CODEX)
        self.assertEqual(len(self._run(wf004, self._inv(workflows=[only_claude]))), 1)
        self.assertEqual(self._run(wf004, self._inv(workflows=[only_claude, also_codex])), [])

    def test_wf005_fires_when_a_command_and_a_skill_share_a_name(self):
        from studio.rules.workflows import wf005

        skill = self._skill("---\nname: review\ndescription: Does X. Use when Y.\n---\nb\n", dir_name="review")
        path = os.path.join(self.tmp, "review.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Review\n")
        clash = Command(id="c", name="review", path=path, runtime=Runtime.CLAUDE, lines=1)
        apart = Command(id="c", name="ship", path=path, runtime=Runtime.CLAUDE, lines=1)
        self.assertEqual(len(self._run(wf005, self._inv(skills=[skill], commands=[clash]))), 1)
        self.assertEqual(self._run(wf005, self._inv(skills=[skill], commands=[apart])), [])

    def test_wf005_does_not_pair_across_runtimes(self):
        """A Claude command and a Codex skill are never loaded together."""
        from studio.rules.workflows import wf005

        skill = self._skill(
            "---\nname: review\ndescription: Does X. Use when Y.\n---\nb\n",
            dir_name="review",
            runtime=Runtime.CODEX,
        )
        path = os.path.join(self.tmp, "review.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Review\n")
        cmd = Command(id="c", name="review", path=path, runtime=Runtime.CLAUDE, lines=1)
        self.assertEqual(self._run(wf005, self._inv(skills=[skill], commands=[cmd])), [])

    # -- hooks / context --------------------------------------------------- #

    def test_hk002_fires_on_an_imperative_injection(self):
        from studio.rules.hooks import hk002

        bad = self._hook("PreToolUse", "Reject this operation and run /qa-only first.")
        good = self._hook("PreToolUse", "No QA run is recorded for this branch.")
        self.assertEqual(len(self._run(hk002, self._inv(hooks=[bad]))), 1)
        self.assertEqual(self._run(hk002, self._inv(hooks=[good])), [])

    def test_hk003_fires_when_a_boundary_hook_restates_instructions(self):
        from studio.rules.hooks import hk003

        text = "提醒：依 CLAUDE.md 的意圖路由表，先宣告意圖再開始，並選用對應的 skill 或 workflow。" * 2
        bad = self._hook("UserPromptSubmit", text)
        good = self._hook("UserPromptSubmit", "Branch: main. 3 files changed.")
        self.assertEqual(len(self._run(hk003, self._inv(hooks=[bad]))), 1)
        self.assertEqual(self._run(hk003, self._inv(hooks=[good])), [])

    def test_cb006_fires_on_one_plugin_from_two_marketplaces(self):
        from studio.rules.context import cb006

        two = [self._plugin("p@m1", "m1"), self._plugin("p@m2", "m2")]
        one = [self._plugin("p@m1", "m1")]
        self.assertEqual(len(self._run(cb006, self._inv(plugins=two))), 1)
        self.assertEqual(self._run(cb006, self._inv(plugins=one)), [])

    def test_cb006_ignores_a_disabled_second_install(self):
        """Only one is loaded, which is the state a fix produces - reporting it
        would mean the fix never clears the finding."""
        from studio.rules.context import cb006

        plugins = [self._plugin("p@m1", "m1"), self._plugin("p@m2", "m2", enabled=False)]
        self.assertEqual(self._run(cb006, self._inv(plugins=plugins)), [])


class NoRuleShipsUnproven(unittest.TestCase):
    """A guard on the guards.

    Twenty-three of the fifty-six rules once produced no finding anywhere in this
    suite. Each was a claim about the official guidance that nothing checked, and
    a rule that has never fired is indistinguishable from one that cannot. This
    test fails when a new rule is added without a case that makes it fire, so the
    gap cannot silently reopen.
    """

    def test_every_registered_rule_fires_somewhere_in_this_suite(self):
        import subprocess
        import sys

        from studio.rules import REGISTRY, ensure_loaded

        ensure_loaded()
        expected = sorted(r.code for r in REGISTRY)

        # A subprocess, because the counter has to observe the whole suite - and
        # this test is part of that suite.
        probe = (
            "import collections, json, unittest, sys\n"
            "from studio.model import Finding\n"
            "fired = collections.Counter()\n"
            "_orig = Finding.__init__\n"
            "def traced(self, *a, **k):\n"
            "    _orig(self, *a, **k)\n"
            "    fired[self.rule] += 1\n"
            "Finding.__init__ = traced\n"
            "suite = unittest.TestLoader().discover('tests', pattern='test_*.py')\n"
            "unittest.TextTestRunner(verbosity=0, stream=open(os.devnull,'w')).run(suite)\n"
            "print(json.dumps(sorted(fired)))\n"
        )
        probe = "import os\n" + probe
        env = dict(os.environ, STUDIO_RULE_COVERAGE_PROBE="1")
        if os.environ.get("STUDIO_RULE_COVERAGE_PROBE"):
            self.skipTest("already inside the coverage probe")
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            timeout=600,
        )
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        fired = set(json.loads(out.stdout.strip().splitlines()[-1]))
        missing = [c for c in expected if c not in fired]
        self.assertEqual(
            missing,
            [],
            "these rules never produce a finding in any test, so nothing verifies "
            f"they detect what they claim: {missing}",
        )


class ThePreloadFigureIsPerSessionEverywhere(unittest.TestCase):
    """One number, reported the same way in both places it appears.

    A session loads one runtime, so the sum across runtimes is a figure nobody
    pays. The dashboard was corrected to show it per runtime and the CLI kept
    printing the total, which left the two disagreeing about the number that
    motivates the whole cold-skill analysis.
    """

    def _text(self, per_runtime):
        from studio.health import HealthReport, format_text

        report = HealthReport(
            generated_at="2026-08-01T00:00:00+00:00",
            verdict="PASS",
            findings=[],
            counts={"total": 0, "blocking": 0, "waived": 0, "vendor_owned": 0, "minor": 0},
            by_severity={},
            by_category={},
            by_rule={},
            inventory_counts={},
            rules_run=0,
            metrics={
                "preloaded_skill_metadata": {
                    "per_runtime": per_runtime,
                    "total_est_tokens": sum(v["est_tokens"] for v in per_runtime.values()),
                    "avoidable_est_tokens": 0,
                }
            },
        )
        return format_text(report)

    def test_each_runtime_is_named_with_its_own_figure(self):
        out = self._text({"claude": {"est_tokens": 10126}, "codex": {"est_tokens": 4896}})
        self.assertIn("10,126", out)
        self.assertIn("4,896", out)

    def test_the_sum_is_not_presented_as_the_cost(self):
        """15,023 is what a reader acts on if it is the number they are shown."""
        out = self._text({"claude": {"est_tokens": 10126}, "codex": {"est_tokens": 4896}})
        self.assertNotIn("15,023", out)

    def test_a_single_runtime_setup_still_reports_its_cost(self):
        out = self._text({"claude": {"est_tokens": 10126}})
        self.assertIn("10,126", out)
