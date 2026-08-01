"""Build the relationship graph the dashboard renders.

Local, hand-authored config is shown node-by-node because that is what you edit.
Plugin-provided skills are collapsed into their plugin node by default: 297 of
them would bury the 68 that are actually yours.
"""

from __future__ import annotations

import os

from .model import Inventory, Origin


EDGE_KINDS = {
    "references": "points at a file",
    "invokes": "tells the agent to run this",
    "mirror": "declared byte-identical copy",
    "generated_from": "rendered from this source",
    "duplicate": "undeclared identical content",
    "provides": "plugin ships this",
    "collision": "two things claim one name",
}


def _node(nid: str, label: str, kind: str, **extra) -> dict:
    n = {"id": nid, "label": label, "kind": kind}
    n.update(extra)
    return n


def build(inv: Inventory, cfg=None, include_plugin_skills: bool = False) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(node: dict) -> None:
        nodes.setdefault(node["id"], node)

    def link(src: str, dst: str, kind: str, **extra) -> None:
        if src == dst or src not in nodes or dst not in nodes:
            return
        edge = {"source": src, "target": dst, "kind": kind}
        edge.update(extra)
        if edge not in edges:
            edges.append(edge)

    # ---- nodes ----------------------------------------------------------- #
    path_index: dict[str, str] = {}

    for ins in inv.instructions:
        add(
            _node(
                ins.id,
                os.path.basename(ins.path),
                "instruction",
                runtime=ins.runtime.value,
                path=ins.path,
                lines=ins.lines,
                bytes=ins.bytes,
            )
        )
        path_index[ins.path] = ins.id

    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY:
            continue
        if s.origin in (Origin.PLUGIN, Origin.TOOLKIT) and not include_plugin_skills:
            continue
        add(
            _node(
                s.id,
                s.name or s.dir_name,
                "skill",
                runtime=s.runtime.value,
                origin=s.origin.value,
                path=s.path,
                body_lines=s.body_lines,
                desc_len=len(s.description),
                plugin=s.plugin,
            )
        )
        path_index[s.path] = s.id

    for wf in inv.workflows:
        add(
            _node(
                wf.id,
                os.path.basename(wf.path),
                "workflow",
                runtime=wf.runtime.value,
                path=wf.path,
                lines=wf.lines,
            )
        )
        path_index[wf.path] = wf.id

    for c in inv.commands:
        add(_node(c.id, "/" + c.name, "command", runtime=c.runtime.value, path=c.path, lines=c.lines))
        path_index[c.path] = c.id

    for a in inv.agents:
        add(_node(a.id, a.name, "agent", runtime=a.runtime.value, path=a.path, lines=a.lines))
        path_index[a.path] = a.id

    for h in inv.hooks:
        add(
            _node(
                h.id,
                f"{h.event}" + (f"[{h.matcher}]" if h.matcher else ""),
                "hook",
                runtime="claude",
                path=h.source,
                event=h.event,
                if_rule=h.if_rule,
            )
        )

    for p in inv.plugins:
        add(
            _node(
                p.id,
                p.key,
                "plugin",
                runtime=p.runtime.value,
                enabled=p.enabled,
                skill_count=p.skill_count,
                marketplace=p.marketplace,
                update_available=p.update_available,
            )
        )

    # ---- name lookup for invocation edges -------------------------------- #
    by_name: dict[str, str] = {}
    for s in inv.skills:
        if s.origin is Origin.ORPHAN_LIBRARY or s.id not in nodes:
            continue
        for key in filter(None, {s.name, s.dir_name}):
            by_name.setdefault(key, s.id)
    for c in inv.commands:
        by_name.setdefault(c.name, c.id)

    # ---- reference edges -------------------------------------------------- #
    for holder in (*inv.instructions, *inv.workflows, *inv.skills):
        if holder.id not in nodes:
            continue
        for ref in getattr(holder, "refs", []):
            tid = path_index.get(ref)
            if tid:
                link(holder.id, tid, "references")

    # ---- invocation edges ------------------------------------------------- #
    for holder in (*inv.instructions, *inv.workflows, *inv.commands, *inv.skills):
        if holder.id not in nodes:
            continue
        for token in getattr(holder, "invokes", []):
            target = by_name.get(token)
            if not target and ":" in token:
                target = by_name.get(token.split(":", 1)[1])
            if target:
                link(holder.id, target, "invokes")

    # ---- plugin -> skill edges ------------------------------------------- #
    if include_plugin_skills:
        # Both sides are the full `plugin@marketplace` key. Comparing a bare name
        # against it matched nothing, so every plugin lost its skills in the graph
        # and appeared to ship none.
        by_key = {p.key: p for p in inv.plugins}
        for s in inv.skills:
            if s.origin is Origin.PLUGIN and s.plugin and s.id in nodes:
                owner = by_key.get(s.plugin)
                if owner is not None:
                    link(owner.id, s.id, "provides")

    # ---- mirror / generated edges ---------------------------------------- #
    if cfg is not None:
        for group in cfg.mirrors:
            paths = [os.path.expanduser(p) for p in group.get("paths", [])]
            ids = [path_index[p] for p in paths if p in path_index]
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    link(ids[i], ids[j], "mirror", group=group.get("name", ""))
        for spec in cfg.generated:
            target = os.path.expanduser(spec.get("target", ""))
            tid = path_index.get(target)
            if not tid:
                continue
            for src in spec.get("sources", []):
                full = src if os.path.isabs(src) else os.path.join(cfg.repo_root, src)
                sid = f"canonical:{os.path.basename(full)}"
                add(_node(sid, os.path.basename(full), "canonical", path=full, runtime="shared"))
                link(tid, sid, "generated_from")

    # ---- duplicate + collision edges ------------------------------------- #
    by_hash: dict[str, list[str]] = {}
    for s in inv.skills:
        if s.id in nodes and s.content_hash:
            by_hash.setdefault(s.content_hash, []).append(s.id)
    for ids in by_hash.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if not any(
                    e["kind"] == "mirror" and {e["source"], e["target"]} == {ids[i], ids[j]}
                    for e in edges
                ):
                    link(ids[i], ids[j], "duplicate")

    skill_names = {s.name: s.id for s in inv.skills if s.origin is Origin.LOCAL and s.name}
    for c in inv.commands:
        if c.name in skill_names:
            link(c.id, skill_names[c.name], "collision")

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "edge_kinds": EDGE_KINDS,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "plugin_skills_collapsed": not include_plugin_skills,
        },
    }
