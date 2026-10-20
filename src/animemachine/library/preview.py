"""Read-only proposals for registered library directory names and attachments."""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import layout
from ..storage import AVAILABLE, status_for_path
from ..storage.sleep import require_awake
from ..storage.path_policy import authorize_existing, PathAuthorizationError


def inspect(db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    root = Path(config["deployment"]["libraryUncRoot"])
    require_awake(root, config)
    if status_for_path(root, timeout=4.0, config=config).state != AVAILABLE:
        raise OSError("local_library_unavailable")
    proposals = []
    examined = 0
    with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        targets = list(db.execute("SELECT * FROM runtime_work ORDER BY target_unc"))
        by_anime: dict[int, list[sqlite3.Row]] = {}
        for row in targets:
            by_anime.setdefault(int(row["anime_id"]), []).append(row)
        for row in targets:
            require_awake(root, config)
            current = Path(row["target_unc"])
            try:
                authorize_existing(current, [root], config=config)
            except (PathAuthorizationError, OSError):
                continue
            if not current.is_dir():
                continue
            examined += 1
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (ValueError, TypeError):
                evidence = {}
            work = db.execute("SELECT * FROM anime_work WHERE bgm_id=?",
                              (evidence.get("bangumiSubjectId"),)).fetchone() if evidence.get("bangumiSubjectId") else None
            if work is None:
                work = db.execute("SELECT * FROM anime_work WHERE id=?", (row["anime_id"],)).fetchone()
            if work is None:
                continue
            name = layout.format_work_directory(config["naming"]["workTemplate"],
                date=str(work["directory_date"]), title=str(work["directory_title"] or work["title_ja"]))
            expected = current.parent / name
            reason = "name"
            if work["physical_role"] == "supplement" and work["physical_owner_anime_id"]:
                owners = by_anime.get(int(work["physical_owner_anime_id"]), [])
                # An existing formal target is necessary; never invent a merge destination.
                owners = [owner for owner in owners if owner["target_unc"] != row["target_unc"]
                          and str(owner["official_title"]) != str(work["title_ja"])]
                if len(owners) != 1:
                    proposals.append({"bgmId": work["bgm_id"], "title": work["title_ja"], "current": str(current),
                                      "proposed": "", "reason": "owner_review", "conflict": True})
                    continue
                try:
                    owner_path = authorize_existing(Path(owners[0]["target_unc"]), [root], config=config)
                except (PathAuthorizationError, OSError):
                    proposals.append({"bgmId": work["bgm_id"], "title": work["title_ja"], "current": str(current),
                                      "proposed": "", "reason": "owner_review", "conflict": True})
                    continue
                expected = owner_path / "Series Extras" / name
                reason = "supplement"
            if str(current) == str(expected):
                continue
            # Detect an existing destination, including equivalent-width/case paths.
            # Resolve the nearest existing ancestor before touching a proposed
            # destination: an existing Series Extras link may leave the root.
            ancestor = expected
            try:
                while not ancestor.exists():
                    if ancestor.parent == ancestor:
                        raise PathAuthorizationError("destination has no existing ancestor")
                    ancestor = ancestor.parent
                authorize_existing(ancestor, [root], config=config)
            except (PathAuthorizationError, OSError):
                proposals.append({"bgmId": work["bgm_id"], "title": work["title_ja"], "current": str(current),
                                  "proposed": "", "reason": "owner_review", "conflict": True})
                continue
            conflict = expected.exists() and not current.samefile(expected)
            proposals.append({"bgmId": work["bgm_id"], "title": work["title_ja"], "current": str(current),
                              "proposed": str(expected), "reason": reason, "conflict": conflict})
    return {"examined": examined, "items": proposals, "count": len(proposals)}
