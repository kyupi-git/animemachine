#!/usr/bin/env python3
"""Materialize deterministic anime relation edges and strict series components."""

from __future__ import annotations

import argparse
import hashlib
import json
import os.path
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from ..library import layout as library_layout


REVERSE_TO_CANONICAL = {
    "main_story": "side_story",
    "full_story": "summary",
}
SYMMETRIC = {
    "alternative_version", "alternative_setting", "same_setting",
    "character_appearance", "collaboration", "adaptation", "other",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS anime_relation_edge(
  source_anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  target_anime_id INTEGER NOT NULL REFERENCES anime_work(id) ON DELETE CASCADE,
  relation_code TEXT NOT NULL,
  grouping INTEGER NOT NULL CHECK(grouping IN (0,1)),
  provenance TEXT NOT NULL,
  PRIMARY KEY(source_anime_id,target_anime_id,relation_code)
);
CREATE TABLE IF NOT EXISTS anime_series_component(
  anime_id INTEGER PRIMARY KEY REFERENCES anime_work(id) ON DELETE CASCADE,
  component_id INTEGER NOT NULL,
  member_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_relation_edge_source ON anime_relation_edge(source_anime_id,grouping);
CREATE INDEX IF NOT EXISTS ix_relation_edge_target ON anime_relation_edge(target_anime_id,grouping);
CREATE INDEX IF NOT EXISTS ix_series_component ON anime_series_component(component_id,anime_id);
"""


def source_fingerprint(db: sqlite3.Connection) -> str:
    count, total = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(anime_id*31+related_bgm_id),0) FROM anime_relation"
    ).fetchone()
    archive = db.execute(
        "SELECT value FROM metadata WHERE key='archive_digest'"
    ).fetchone()
    return hashlib.sha256(
        f"relation-normalization-v3:{count}:{total}:{archive[0] if archive else ''}".encode()
    ).hexdigest()


def _components(nodes: Iterable[int], edges: Iterable[tuple[int, int]]) -> list[tuple[int, int, int]]:
    parent = {int(node): int(node) for node in nodes}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a == b:
            return
        if a > b:
            a, b = b, a
        parent[b] = a

    for left, right in edges:
        union(int(left), int(right))
    members: dict[int, list[int]] = defaultdict(list)
    for node in parent:
        members[find(node)].append(node)
    rows: list[tuple[int, int, int]] = []
    for values in members.values():
        component_id = min(values)
        rows.extend((node, component_id, len(values)) for node in values)
    return rows


def _month_index(value: str) -> int | None:
    try:
        year, month = str(value).split("-", 1)
        return int(year) * 12 + int(month)
    except (TypeError, ValueError):
        return None


def reanchor_series_entry_edges(
    work_rows: Iterable[sqlite3.Row], edge_rows: Iterable[sqlite3.Row]
) -> list[dict[str, Any]]:
    """Reanchor only exceptionally well-proven display edges to a subseries head.

    Bangumi sometimes links a parent work to the final chapter of a linear film
    trilogy, while the three chapters all point to one compilation.  The stored
    evidence remains untouched; only the graph payload moves that one bridge to
    the first chapter so the visual series entry is not orphaned.
    """
    nodes = {int(row["id"]): dict(row) for row in work_rows}
    edges = [dict(row) for row in edge_rows]
    sequel_out: dict[int, list[int]] = defaultdict(list)
    sequel_in: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        if edge["grouping"] and edge["relation_code"] == "sequel":
            source, target = int(edge["source_anime_id"]), int(edge["target_anime_id"])
            sequel_out[source].append(target)
            sequel_in[target].append(source)

    replacements: dict[tuple[int, int, str], dict[str, Any]] = {}
    for head in sorted(sequel_out):
        if sequel_in.get(head) or len(sequel_out[head]) != 1:
            continue
        chain = [head]
        seen = {head}
        while len(sequel_out.get(chain[-1], [])) == 1:
            target = sequel_out[chain[-1]][0]
            if target in seen or len(sequel_in.get(target, [])) != 1:
                break
            chain.append(target)
            seen.add(target)
        if len(chain) < 3:
            continue
        works = [nodes.get(anime_id) for anime_id in chain]
        if any(work is None for work in works):
            continue
        if len({work["media_code"] for work in works}) != 1:
            continue
        titles = [str(work.get("title_ja") or "") for work in works]
        if len(os.path.commonprefix(titles).strip()) < 6:
            continue
        months = [_month_index(work.get("start_month", "")) for work in works]
        if any(value is None for value in months):
            continue
        if any(not 1 <= months[index + 1] - months[index] <= 12 for index in range(len(months) - 1)):
            continue
        if months[-1] - months[0] > 24:
            continue

        # A common compilation/whole-work source for every member is the strong
        # evidence that the sequel chain is one multipart subseries.
        summary_sources = [
            {
                int(edge["source_anime_id"])
                for edge in edges
                if edge["relation_code"] == "summary"
                and int(edge["target_anime_id"]) == member
            }
            for member in chain
        ]
        common_summary = set.intersection(*summary_sources) if summary_sources else set()
        if not common_summary:
            continue

        tail = chain[-1]
        for edge in edges:
            if not edge["grouping"] or edge["relation_code"] not in {"prequel", "sequel"}:
                continue
            source, target = int(edge["source_anime_id"]), int(edge["target_anime_id"])
            if tail not in {source, target}:
                continue
            outside = target if source == tail else source
            if outside in seen or outside in common_summary:
                continue
            if any(
                other is not edge
                and outside in {int(other["source_anime_id"]), int(other["target_anime_id"])}
                and any(member in {int(other["source_anime_id"]), int(other["target_anime_id"])} for member in chain[:-1])
                and other["relation_code"] in {"prequel", "sequel"}
                for other in edges
            ):
                continue
            key = (source, target, str(edge["relation_code"]))
            inferred = dict(edge)
            if source == tail:
                inferred["source_anime_id"] = head
            else:
                inferred["target_anime_id"] = head
            inferred["provenance"] = f"anm-inferred-series-entry:{edge['provenance']}"
            inferred["inferred"] = 1
            replacements[key] = inferred

    output: list[dict[str, Any]] = []
    for edge in edges:
        key = (
            int(edge["source_anime_id"]),
            int(edge["target_anime_id"]),
            str(edge["relation_code"]),
        )
        output.append(replacements.get(key, edge))
    return output


def suppress_redundant_compilation_branches(
    edges: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide branch arrows repeated through a compilation in graph payloads.

    If P→C is a compilation edge and both P→B and C→B carry the same branch
    meaning, C→B adds no series-level information.  Keep every source row in
    SQLite, but omit only that provably transitive display edge.
    """
    rows = [dict(edge) for edge in edges]
    compilation_parents: dict[int, set[int]] = defaultdict(set)
    for edge in rows:
        if edge.get("grouping") and edge.get("relation_code") == "summary":
            compilation_parents[int(edge["target_anime_id"])].add(
                int(edge["source_anime_id"])
            )
    branch_edges = {
        (
            int(edge["source_anime_id"]),
            int(edge["target_anime_id"]),
            str(edge["relation_code"]),
        )
        for edge in rows
        if edge.get("grouping")
        and edge.get("relation_code") in {"spin_off", "side_story"}
    }
    return [
        edge
        for edge in rows
        if not (
            edge.get("grouping")
            and edge.get("relation_code") in {"spin_off", "side_story"}
            and any(
                (parent, int(edge["target_anime_id"]), str(edge["relation_code"]))
                in branch_edges
                for parent in compilation_parents.get(
                    int(edge["source_anime_id"]), set()
                )
            )
        )
    ]


def rebuild(db: sqlite3.Connection, *, force: bool = False) -> dict[str, int | str | bool]:
    db.executescript(SCHEMA)
    fingerprint = source_fingerprint(db)
    cached = db.execute(
        "SELECT value FROM metadata WHERE key='relation_graph_fingerprint'"
    ).fetchone()
    if not force and cached and cached[0] == fingerprint:
        largest = db.execute(
            "SELECT component_id,member_count FROM anime_series_component ORDER BY member_count DESC,component_id LIMIT 1"
        ).fetchone()
        return {
            "rebuilt": False,
            "edges": db.execute("SELECT COUNT(*) FROM anime_relation_edge").fetchone()[0],
            "components": db.execute(
                "SELECT COUNT(DISTINCT component_id) FROM anime_series_component"
            ).fetchone()[0],
            "largestComponent": int(largest[1]) if largest else 1,
            "largestComponentId": int(largest[0]) if largest else None,
            "fingerprint": fingerprint,
        }

    work_by_bgm = {
        int(bgm_id): int(anime_id)
        for anime_id, bgm_id in db.execute("SELECT id,bgm_id FROM anime_work")
    }
    work_dates = {
        int(anime_id): str(start_month or "")
        for anime_id, start_month in db.execute("SELECT id,start_month FROM anime_work")
    }
    source_rows: list[tuple[int, int, str, int, str]] = []
    for source, related_bgm, code, strict, source_name in db.execute(
        "SELECT anime_id,related_bgm_id,relation_code,strict_group,source FROM anime_relation"
    ):
        target = work_by_bgm.get(int(related_bgm or 0))
        if not target or int(source) == target:
            continue
        source_rows.append(
            (int(source), target, str(code), int(bool(strict)), str(source_name))
        )
    directed_codes: dict[tuple[int, int], set[str]] = defaultdict(set)
    for source, target, code, _, _ in source_rows:
        directed_codes[(source, target)].add(code)

    raw_edges: dict[tuple[int, int, str], tuple[int, str]] = {}
    for source, target, code, strict, source_name in source_rows:
        source_id, target_id, relation_code = int(source), target, str(code)
        if relation_code in {"prequel", "sequel"}:
            source_date, target_date = work_dates.get(source_id, ""), work_dates.get(target_id, "")
            if source_date and target_date and source_date != target_date:
                if source_date > target_date:
                    source_id, target_id = target_id, source_id
                    relation_code = "prequel" if relation_code == "sequel" else "sequel"
            elif relation_code == "prequel":
                # Without a reliable release order, retain the former canonical
                # direction so reciprocal prequel/sequel rows still collapse.
                source_id, target_id = target_id, source_id
                relation_code = "sequel"
        elif relation_code == "main_story":
            source_id, target_id = target_id, source_id
            reciprocal = directed_codes.get((source_id, target_id), set())
            relation_code = (
                "spin_off" if "spin_off" in reciprocal
                else "side_story" if "side_story" in reciprocal
                else "side_story"
            )
        elif relation_code in REVERSE_TO_CANONICAL:
            source_id, target_id = target_id, source_id
            relation_code = REVERSE_TO_CANONICAL[relation_code]
        elif relation_code in SYMMETRIC and source_id > target_id:
            source_id, target_id = target_id, source_id
        key = (source_id, target_id, relation_code)
        raw_edges[key] = (strict, source_name)

    grouping_pairs = {
        tuple(sorted((source, target)))
        for (source, target, _), (grouping, _) in raw_edges.items()
        if grouping
    }
    component_rows = _components(work_by_bgm.values(), grouping_pairs)
    with db:
        db.execute("DELETE FROM anime_relation_edge")
        db.execute("DELETE FROM anime_series_component")
        db.executemany(
            "INSERT INTO anime_relation_edge VALUES(?,?,?,?,?)",
            [
                (source, target, code, grouping, provenance)
                for (source, target, code), (grouping, provenance) in sorted(raw_edges.items())
            ],
        )
        db.executemany("INSERT INTO anime_series_component VALUES(?,?,?)", component_rows)
        db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('relation_graph_fingerprint',?)",
            (fingerprint,),
        )
    largest_component = max(component_rows, key=lambda row: (row[2], -row[1]), default=(0, 0, 1))
    return {
        "rebuilt": True,
        "edges": len(raw_edges),
        "components": len({row[1] for row in component_rows}),
        "largestComponent": largest_component[2],
        "largestComponentId": largest_component[1] or None,
        "fingerprint": fingerprint,
    }


def graph_rows(db: sqlite3.Connection, anime_id: int, *, include_context: bool = True,
               context_limit: int = 24) -> dict[str, Any] | None:
    seed = db.execute("SELECT id FROM anime_work WHERE id=?", (anime_id,)).fetchone()
    if not seed:
        return None
    component = db.execute(
        "SELECT component_id,member_count FROM anime_series_component WHERE anime_id=?",
        (anime_id,),
    ).fetchone()
    component_id = int(component[0]) if component else anime_id
    strict_nodes = {
        int(row[0])
        for row in db.execute(
            "SELECT anime_id FROM anime_series_component WHERE component_id=?", (component_id,)
        )
    } or {anime_id}
    nodes = set(strict_nodes)
    context_total = 0
    if include_context:
        marks = ",".join("?" for _ in strict_nodes)
        candidates: list[int] = []
        for row in db.execute(
            f"""SELECT source_anime_id,target_anime_id FROM anime_relation_edge
                 WHERE grouping=0 AND (source_anime_id IN ({marks}) OR target_anime_id IN ({marks}))""",
            [*strict_nodes, *strict_nodes],
        ):
            for value in (int(row[0]), int(row[1])):
                if value not in strict_nodes and value not in candidates:
                    candidates.append(value)
        context_total = len(candidates)
        nodes.update(candidates[:max(0, context_limit)])
    marks = ",".join("?" for _ in nodes)
    work_rows = db.execute(
        f"""SELECT id,bgm_id,title_ja,title_zh_hans,title_en,start_month,media_code,original_language
             FROM anime_work WHERE id IN ({marks}) ORDER BY start_month,id""",
        sorted(nodes),
    ).fetchall()
    edge_rows = db.execute(
        f"""SELECT source_anime_id,target_anime_id,relation_code,grouping,provenance
             FROM anime_relation_edge WHERE source_anime_id IN ({marks}) AND target_anime_id IN ({marks})
             ORDER BY grouping DESC,source_anime_id,target_anime_id,relation_code""",
        [*sorted(nodes), *sorted(nodes)],
    ).fetchall()
    display_edges = suppress_redundant_compilation_branches(
        reanchor_series_entry_edges(work_rows, edge_rows)
    )
    return {
        "rootAnimeId": anime_id,
        "seriesTitle": library_layout.franchise_title(dict(row) for row in work_rows if int(row["id"]) in strict_nodes),
        "componentId": component_id,
        "strictMemberCount": len(strict_nodes),
        "contextCount": min(context_total, context_limit),
        "contextTotal": context_total,
        "contextTruncated": context_total > context_limit,
        "nodes": [{**dict(row), "strict_member": int(row["id"]) in strict_nodes} for row in work_rows],
        "edges": display_edges,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--anime-id", type=int, help="also export one normalized graph as JSON")
    args = parser.parse_args()
    with sqlite3.connect(args.db) as db:
        db.row_factory = sqlite3.Row
        result: dict[str, Any] = dict(rebuild(db, force=args.force))
        if args.anime_id is not None:
            result["graph"] = graph_rows(db, args.anime_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
