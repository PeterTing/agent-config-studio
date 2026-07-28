"""Automatic fixes for findings that have one correct answer.

A finding is only auto-fixable when the remedy is mechanical: there is exactly
one sensible outcome and no judgement call about intent. Splitting an oversized
skill needs someone to decide which sections move; deleting one of two duplicate
directories needs someone to decide which one wins. Those stay manual, and the
dashboard says why rather than offering a button that guesses.

Every fix returns a :class:`~studio.patch.ChangeSet`, so it is previewable as a
diff, backed up before it is written, and reversible with ``studio rollback``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from . import canonical, patch, plugins as plugins_mod, usage
from .model import Finding, Inventory, Owner
from .rules import Config

#: Where files pulled out of a live config tree are parked. Deleting outright
#: would be the wrong default for something the user may still want.
QUARANTINE = os.path.join("var", "quarantine")


@dataclass
class Fix:
    rule: str
    label: str
    #: One-line description of what applying it will do.
    detail: str
    fn: Callable[[Finding, Inventory, Config, str], patch.ChangeSet | None]
    #: Whether "fix everything" may include this one.
    #:
    #: False means the fix is correct but the *decision* is not automatic. CB002
    #: is the case that forced this distinction: it reports every plugin with no
    #: recorded usage, but CB001's classifier deliberately keeps some of them -
    #: `figma` has no recorded skill call yet the instructions route design work
    #: to it. Sweeping all CB002 findings into a bulk fix would undo that
    #: judgement, so it stays a per-item button.
    bulk: bool = True


REGISTRY: dict[str, Fix] = {}


def fix(rule: str, label: str, detail: str, *, bulk: bool = True):
    def deco(fn):
        REGISTRY[rule] = Fix(rule=rule, label=label, detail=detail, fn=fn, bulk=bulk)
        return fn

    return deco


def _relocate(path: str, repo_root: str, reason: str) -> list[patch.Change]:
    """Copy a file into the repo's quarantine, then remove the original.

    The quarantine mirrors the source path rather than flattening to a basename.
    Stray files with the same name in different config roots - `settings.json.bak`
    under both ~/.claude and ~/.codex - otherwise map to one destination: the
    second copy overwrites the first, both originals are deleted, and one of them
    is simply gone. Quarantine is supposed to be the safe alternative to deleting.
    """
    rel = path.lstrip("/")
    dest = os.path.join(repo_root, QUARANTINE, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Change sets carry text. Forcing a binary file through them with
        # errors="replace" rewrites the bytes, and the original is deleted right
        # after - so the quarantined copy is corrupt and rollback cannot undo it.
        # No automatic fix is better than one that destroys the thing it saves.
        return []
    return [
        patch.Change(
            path=dest,
            new_text=text,
            action="create" if not os.path.exists(dest) else "modify",
            reason=f"quarantined copy: {reason}",
        ),
        patch.Change(path=path, new_text="", action="delete", reason=reason),
    ]


# --------------------------------------------------------------------------- #
# context / hygiene
# --------------------------------------------------------------------------- #


@fix(
    "CB001",
    "停用沒在用的 plugin",
    "停用「零使用紀錄且未被你的設定引用」的 plugin，把它們的 metadata 從每個 session 移除。",
)
def fix_cb001(finding, inv, cfg, repo_root):
    """Disable exactly the plugins the shared classifier calls avoidable.

    The decision lives in studio.plugins, shared with rule CB001, so the fix can
    never disable something the rule would not have flagged.
    """
    idx = usage.build(cache_path=os.path.join(repo_root, "var", "usage-cache.json"))
    summary = idx.summary()
    if summary.get("truncated") or (summary.get("file_coverage_pct") or 0) < 99.9:
        return None  # refuse to call anything unused on partial evidence

    counts = usage.plugin_usage(idx, inv)
    corpus = plugins_mod.reference_corpus((os.path.join(repo_root, "canonical", "*.md"),))
    rows = plugins_mod.classify(inv, counts, corpus)
    targets = {r["key"] for r in rows if r["verdict"] == "disable"}
    if not targets:
        return None

    settings = os.path.join(os.path.expanduser("~/.claude"), "settings.json")
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    enabled = data.get("enabledPlugins") or {}
    for key in targets:
        if key in enabled:
            enabled[key] = False
    data["enabledPlugins"] = enabled

    return patch.ChangeSet(
        name="disable-unused-plugins",
        description=f"Disable {len(targets)} plugin(s) with no recorded usage and no "
        "mention in your configuration.",
        changes=[
            patch.Change(
                path=settings,
                new_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                reason="context budget: unused plugin metadata",
            )
        ],
    )


@fix(
    "CB002",
    "停用這個 plugin",
    "把這一個 plugin 設為停用。隨時可以再打開。不納入批次修復：有些沒使用紀錄的 plugin 是你刻意留著的。",
    bulk=False,
)
def fix_cb002(finding, inv, cfg, repo_root):
    key = (finding.evidence or {}).get("plugin")
    if not key:
        return None
    settings = os.path.join(os.path.expanduser("~/.claude"), "settings.json")
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    enabled = data.get("enabledPlugins") or {}
    if key not in enabled or not enabled[key]:
        return None
    enabled[key] = False
    data["enabledPlugins"] = enabled
    return patch.ChangeSet(
        name="disable-plugin",
        description=f"Disable {key}.",
        changes=[
            patch.Change(
                path=settings,
                new_text=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                reason="no recorded usage",
            )
        ],
    )


@fix(
    "CB004",
    "移出設定目錄",
    "把殘留的備份檔搬到本 repo 的 var/quarantine/，設定樹裡就不再有含糊的檔案。",
)
def fix_cb004(finding, inv, cfg, repo_root):
    path = finding.path
    if not path or not os.path.isfile(path):
        return None  # directories need a decision about what to keep
    changes = _relocate(path, repo_root, "backup file inside a live config tree")
    if not changes:
        return None
    return patch.ChangeSet(
        name="quarantine-stray-file",
        description=f"Move {os.path.basename(path)} out of the live config tree.",
        changes=changes,
    )


@fix("CB005", "刪除空目錄", "移除這個空的目錄，它暗示了一個其實沒在用的機制。")
def fix_cb005(finding, inv, cfg, repo_root):
    path = finding.path
    if not os.path.isdir(path):
        return None
    try:
        if os.listdir(path):
            return None  # not empty any more; do not touch it
    except OSError:
        return None
    # Directory removal is not a file change set, so it is carried as a marker
    # the applier understands.
    cs = patch.ChangeSet(
        name="remove-empty-dir",
        description=f"Remove the empty directory {path}.",
        changes=[],
    )
    cs.remove_dirs = [path]  # type: ignore[attr-defined]
    return cs


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


@fix(
    "SK009",
    "加入目錄",
    "在這個 reference 檔開頭產生一份標題清單，讓部分讀取時仍看得到它涵蓋什麼。",
)
def fix_sk009(finding, inv, cfg, repo_root):
    path = finding.path
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.split("\n")

    # Refuse if one is already there. The rule that raises this finding checks
    # the same thing, but a fix has to be safe when invoked directly - otherwise
    # re-running it stacks a second contents list on top of the first.
    if re.search(r"^##\s+(Contents|Table of Contents|目錄)\s*$", text, re.M | re.I):
        return None

    fenced = False
    titles: list[str] = []
    for ln in lines:
        if ln.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(ln)
        if m:
            titles.append(m.group(2))
    if not titles:
        return None

    # Insert after the H1 if there is one, else at the very top.
    insert_at = 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            insert_at = i + 1
            break
    toc = ["", "## Contents", ""] + [f"- {t}" for t in titles] + [""]
    new = lines[:insert_at] + toc + lines[insert_at:]
    return patch.ChangeSet(
        name="add-contents",
        description=f"Add a contents list to {os.path.basename(path)}.",
        changes=[
            patch.Change(
                path=path,
                new_text="\n".join(new),
                reason="a long reference file needs a contents list",
            )
        ],
    )


# --------------------------------------------------------------------------- #
# mirrors / generated files
# --------------------------------------------------------------------------- #


@fix("MR001", "重新同步鏡像", "從宣告的來源把這組鏡像重新寫回一致。")
def fix_mr001(finding, inv, cfg, repo_root):
    label = (finding.evidence or {}).get("group")
    for name, src, others in canonical.mirror_groups(cfg):
        if name != label:
            continue
        if not os.path.isfile(src):
            return None
        with open(src, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        return patch.ChangeSet(
            name="resync-mirror",
            description=f"Re-sync mirror group {name} from {src}.",
            changes=[
                patch.Change(path=d, new_text=text, reason=f"mirror of {src}") for d in others
            ],
        )
    return None


@fix("MR003", "重新產生", "從 canonical 來源重新渲染這個檔案，把手動編輯覆蓋掉。")
def fix_mr003(finding, inv, cfg, repo_root):
    target = finding.path
    for spec in cfg.generated:
        if os.path.expanduser(spec.get("target", "")) != target:
            continue
        try:
            text = canonical.render_target(cfg, spec)
        except FileNotFoundError:
            return None
        return patch.ChangeSet(
            name="re-render",
            description=f"Re-render {os.path.basename(target)} from canonical sources.",
            changes=[patch.Change(path=target, new_text=text, reason="canonical render")],
        )
    return None


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #

#: Why the other rules are not auto-fixable. Shown in the UI so a missing button
#: reads as a deliberate decision rather than an omission.
MANUAL_ONLY = {
    "SK007": "要先決定哪些章節搬出去，這是編輯判斷，不是機械操作。",
    "SK012": "兩個目錄同名，要留哪一個是你的決定。",
    "SK013": "內容相同的兩份，要刪掉哪一份、還是宣告成鏡像，需要你決定。",
    "SK003": "改目錄名還是改 name 欄位，取決於哪一個是你要的。",
    "SK005": "description 要寫什麼只有你知道。",
    "SK008": "要改連結結構還是合併檔案，是設計決定。",
    "WF001": "孤兒 workflow 要接上路由還是刪掉，取決於你還要不要它。",
    "WF002": "壞掉的引用要修路徑、補檔案、還是刪步驟，意圖不明。",
    "WF003": "重複的內容要留哪一邊是編輯判斷。",
    "WF004": "某個 runtime 缺這個 workflow，要補過去還是兩邊都刪，取決於你還要不要它。",
    "WF005": "命名衝突要留 command 還是 skill 是你的決定。",
    "CB003": "搬動 861 個檔案影響範圍大，該由你決定去處。",
    "IN001": "縮短 instruction 是寫作，不是機械操作。",
    "IN002": "移除驗證步驟要判斷哪一句是步驟、哪一句是輸出約束。",
    "IN003": "同上。",
    "IN004": "重複的兩句要留哪一句是編輯判斷。",
    "IN005": "要把哪一段收斂成指標，取決於你想保留多少。",
    "IN006": "壞掉的引用意圖不明。",
    "IN007": "語氣調整是寫作。",
    "IN008": "矛盾要往哪一邊解，是你的決定。",
    "IN009": "要不要改用 canonical 產生，是架構決定。",
    "IN010": "要把哪一段收斂成指標，取決於你想保留多少。",
    "HK001": "hook 指令要怎麼改寫，取決於你要它檢查什麼。",
    "HK002": "注入文字要寫什麼是你的決定。",
    "HK003": "刪掉 hook 還是改寫它，是流程決定。",
    "HK004": "要加什麼 scope 取決於你的意圖。",
    "HK005": "同 HK001。",
    "SK001": "缺 frontmatter 的內容只有你知道。",
    "SK002": "改名會影響誰引用它。",
    "SK004": "description 內容只有你知道。",
    "SK006": "改寫描述是寫作。",
    "SK010": "路徑要改成什麼取決於它指向哪裡。",
    "SK011": "日期條件要怎麼重寫取決於現況。",
    "SK014": "frontmatter 怎麼修取決於原意。",
    "SK015": "哪一句才是對的，你才知道。",
    "MR002": "缺檔案要補還是要移除宣告，是你的決定。",
    "CB006": "重複安裝要留哪一個來源是你的決定。",
    "CB007": "「沒被呼叫過」不等於沒用 —— skill 是靠描述自動觸發的，可能只是還沒遇到對的情境。要刪哪些只有你知道。",
    "CB008": "設定裡提到它但從沒真的用過。要留（那個提及是刻意的）還是停用，是你的判斷。",
    "SK016": "兩邊差異可能是刻意的（各 runtime 工具名不同）。要改成 canonical 產生、還是宣告成鏡像，是架構決定。",
    "SK017": "描述重疊代表兩個 skill 在搶同一個觸發，但要合併還是把其中一個寫窄，是編輯判斷。",
    "WF006": "死路由要補上那個東西、改指向替代品、還是刪掉那行，意圖不明。",
    "MR004": "governance.json 的 JSON 語法要怎麼修，取決於你原本想宣告什麼。",
    "MR005": "檔案讀不到可能是權限、可能是壞檔，處理方式不同。",
}


def available(findings: list[Finding]) -> dict[str, dict]:
    """Map finding key -> what can be done about it."""
    out: dict[str, dict] = {}
    for f in findings:
        if f.waived:
            continue
        if f.owner is Owner.VENDOR:
            # Writing into vendor content is undone by its next upgrade, so
            # offering a button here would be offering to waste your time.
            out[f.key] = {
                "fixable": False,
                "why": "這是 plugin / 工具組帶進來的檔案，改了會被下次升級覆蓋。",
                "rule": f.rule,
                "path": f.path,
            }
            continue
        entry = REGISTRY.get(f.rule)
        if entry:
            out[f.key] = {
                "fixable": True,
                "bulk": entry.bulk,
                "label": entry.label,
                "detail": entry.detail,
                "rule": f.rule,
                "path": f.path,
                "subject": f.key.rsplit("|", 1)[-1] or os.path.basename(f.path),
            }
        elif f.rule in MANUAL_ONLY:
            out[f.key] = {"fixable": False, "why": MANUAL_ONLY[f.rule], "rule": f.rule, "path": f.path}
    return out


def bulk_keys(info: dict[str, dict]) -> list[str]:
    """Keys eligible for a single "fix everything" run."""
    return [k for k, v in info.items() if v.get("fixable") and v.get("bulk")]


def build_change_set(
    keys: list[str], findings: list[Finding], inv: Inventory, cfg: Config, repo_root: str
) -> tuple[patch.ChangeSet, list[str], list[str]]:
    """Combine the fixes for `keys` into one reviewable change set."""
    by_key = {f.key: f for f in findings}
    changes: list[patch.Change] = []
    remove_dirs: list[str] = []
    applied: list[str] = []
    skipped: list[str] = []

    for key in keys:
        f = by_key.get(key)
        entry = REGISTRY.get(f.rule) if f else None
        if not f or not entry:
            skipped.append(key)
            continue
        cs = entry.fn(f, inv, cfg, repo_root)
        if cs is None:
            skipped.append(key)
            continue
        changes.extend(cs.changes)
        remove_dirs.extend(getattr(cs, "remove_dirs", []))
        applied.append(key)

    combined = patch.ChangeSet(
        name="fix",
        description=f"Apply {len(applied)} automatic fix(es) from the dashboard.",
        changes=changes,
    )
    combined.remove_dirs = remove_dirs  # type: ignore[attr-defined]
    return combined, applied, skipped
