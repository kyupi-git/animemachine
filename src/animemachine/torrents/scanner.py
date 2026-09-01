#!/usr/bin/env python3
"""Incrementally index a torrent pool without touching qBittorrent or the NAS."""
from __future__ import annotations
import argparse, hashlib, json, os, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

def now(): return datetime.now(timezone.utc).isoformat()

from ..catalog.migrations import migrate
from ..config.loader import load_config
from ..storage import AVAILABLE, StorageUnavailableError, status_for_path
from . import metainfo

def indexer():
    return metainfo

def _iter_torrents_complete(root: Path):
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                listing = sorted(entries, key=lambda entry: entry.name.casefold(), reverse=True)
        except OSError as exc:
            raise StorageUnavailableError(exc.errno or 0, f"torrent pool unavailable: {root}") from exc
        directories = []
        files = []
        for entry in listing:
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".torrent"):
                    files.append(Path(entry.path))
            except OSError as exc:
                raise StorageUnavailableError(exc.errno or 0, f"torrent pool unavailable: {root}") from exc
        pending.extend(directories)
        yield from reversed(files)

def connect(value):
    if value != ":memory:": Path(value).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(value); db.row_factory=sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL"); db.execute("PRAGMA busy_timeout=60000"); return db

def publish_progress(path, phase, stats, *, complete=False):
    if not path: return
    payload={"schemaVersion":1,"phase":phase,"state":"complete" if complete else "running",
             "complete":bool(complete),"updatedAt":now(),"stats":dict(stats)}
    path.parent.mkdir(parents=True,exist_ok=True); temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    temporary.replace(path)

def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS torrent(info_hash TEXT PRIMARY KEY,torrent_path TEXT NOT NULL,torrent_bytes INTEGER,mtime_utc TEXT,manifest_sha256 TEXT NOT NULL,source_class TEXT,effective_group TEXT,language_hint TEXT,scan_state TEXT NOT NULL,scan_reason TEXT);
    CREATE TABLE IF NOT EXISTS torrent_source(source_path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,info_hash TEXT REFERENCES torrent(info_hash),presence_state TEXT NOT NULL,last_seen_at TEXT NOT NULL,parse_error TEXT);
    CREATE TABLE IF NOT EXISTS anime_work(work_id INTEGER PRIMARY KEY AUTOINCREMENT,target_unc TEXT UNIQUE NOT NULL,directory_name TEXT NOT NULL,series_unc TEXT,official_title TEXT NOT NULL,date_code TEXT NOT NULL,mal_id INTEGER,relation_state TEXT NOT NULL,scope_state TEXT NOT NULL DEFAULT 'active',library_state TEXT NOT NULL,placeholder_content TEXT,evidence_json TEXT NOT NULL,verified_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS anime_work_member(member_id INTEGER PRIMARY KEY AUTOINCREMENT,owner_work_id INTEGER NOT NULL REFERENCES anime_work(work_id) ON DELETE CASCADE,member_ordinal INTEGER NOT NULL,official_title TEXT NOT NULL,date_code TEXT NOT NULL,mal_id INTEGER,bangumi_subject_id INTEGER,relation_type TEXT NOT NULL DEFAULT 'collection_member',evidence_json TEXT NOT NULL DEFAULT '{}',UNIQUE(owner_work_id,member_ordinal));
    CREATE TABLE IF NOT EXISTS torrent_work(info_hash TEXT NOT NULL REFERENCES torrent(info_hash),work_id INTEGER NOT NULL REFERENCES anime_work(work_id),role TEXT NOT NULL,mapping_state TEXT NOT NULL,evidence_json TEXT NOT NULL,priority_rank INTEGER,PRIMARY KEY(info_hash,work_id));
    CREATE TABLE IF NOT EXISTS scope_exclusion(info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),scope_state TEXT NOT NULL,official_title TEXT NOT NULL,mal_id INTEGER,evidence_json TEXT NOT NULL,verified_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS title_review(info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),reason_codes_json TEXT NOT NULL,candidate_json TEXT NOT NULL,evidence_json TEXT NOT NULL,reviewed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS torrent_resolution(info_hash TEXT PRIMARY KEY REFERENCES torrent(info_hash),disposition TEXT NOT NULL,review_reason_text TEXT NOT NULL DEFAULT '',manual_action TEXT NOT NULL DEFAULT 'pending',user_note TEXT NOT NULL DEFAULT '',proposal_evidence_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS torrent_target_path(info_hash TEXT NOT NULL REFERENCES torrent(info_hash),target_ordinal INTEGER NOT NULL,target_unc TEXT NOT NULL,target_state TEXT NOT NULL,work_id INTEGER REFERENCES anime_work(work_id),confidence REAL NOT NULL,basis TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(info_hash,target_ordinal));
    CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS asset_provenance(asset_id INTEGER PRIMARY KEY AUTOINCREMENT,final_path TEXT NOT NULL UNIQUE,owner_path TEXT NOT NULL,bytes INTEGER,sha256 TEXT,media_created_at TEXT,source_info_hash TEXT,source_file_index INTEGER,source_torrent_path TEXT,replacement_state TEXT NOT NULL DEFAULT 'current',evidence_json TEXT NOT NULL DEFAULT '{}',verified_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS release_baseline(owner_path TEXT PRIMARY KEY,selected_strategy_json TEXT NOT NULL,comparison_fingerprint TEXT NOT NULL,policy_version TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS upgrade_candidate(upgrade_id INTEGER PRIMARY KEY AUTOINCREMENT,owner_path TEXT NOT NULL,old_info_hash TEXT,new_info_hash TEXT NOT NULL,comparison_fingerprint TEXT NOT NULL UNIQUE,manifest_delta_json TEXT NOT NULL,proof_json TEXT NOT NULL,staging_unc TEXT,state TEXT NOT NULL,last_source_fingerprint TEXT,detected_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_asset_owner ON asset_provenance(owner_path);
    CREATE INDEX IF NOT EXISTS ix_asset_source ON asset_provenance(source_info_hash,source_file_index);
    CREATE INDEX IF NOT EXISTS ix_upgrade_owner_state ON upgrade_candidate(owner_path,state);
    CREATE INDEX IF NOT EXISTS ix_torrent_work_work ON torrent_work(work_id,mapping_state);
    CREATE INDEX IF NOT EXISTS ix_anime_work_member_identity ON anime_work_member(mal_id,bangumi_subject_id);
    CREATE INDEX IF NOT EXISTS ix_torrent_source_hash ON torrent_source(info_hash);
    CREATE INDEX IF NOT EXISTS ix_torrent_target_path_target ON torrent_target_path(target_unc,target_state);
    CREATE INDEX IF NOT EXISTS ix_torrent_resolution_state ON torrent_resolution(disposition,manual_action);
    """)
    old={r[1] for r in db.execute("PRAGMA table_info(torrent)")}
    for name,decl in {"info_name":"TEXT","primary_work_name":"TEXT","title_state":"TEXT DEFAULT 'unmapped'","file_count":"INTEGER","total_bytes":"INTEGER","indexed_at":"TEXT","torrent_created_at":"TEXT","created_by":"TEXT","release_flags_json":"TEXT NOT NULL DEFAULT '[]'","collection_hint":"INTEGER","video_height":"INTEGER","video_scan":"TEXT","bit_depth":"INTEGER","release_unit":"TEXT NOT NULL DEFAULT 'unknown'","volume_sequence_json":"TEXT NOT NULL DEFAULT '[]'","episode_sequence_json":"TEXT NOT NULL DEFAULT '[]'"}.items():
        if name not in old: db.execute(f"ALTER TABLE torrent ADD COLUMN {name} {decl}")
    work_columns={r[1] for r in db.execute("PRAGMA table_info(anime_work)")}
    if "scope_state" not in work_columns: db.execute("ALTER TABLE anime_work ADD COLUMN scope_state TEXT NOT NULL DEFAULT 'active'")
    if "deprecation_content" not in work_columns: db.execute("ALTER TABLE anime_work ADD COLUMN deprecation_content TEXT")
    if "replacement_work_id" not in work_columns: db.execute("ALTER TABLE anime_work ADD COLUMN replacement_work_id INTEGER")
    resolution_columns={r[1] for r in db.execute("PRAGMA table_info(torrent_resolution)")}
    if "review_reason_text" not in resolution_columns: db.execute("ALTER TABLE torrent_resolution ADD COLUMN review_reason_text TEXT NOT NULL DEFAULT ''")
    db.execute("UPDATE torrent SET title_state='unmapped' WHERE title_state IS NULL"); db.commit()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("root",type=Path); ap.add_argument("--db",required=True)
    ap.add_argument("--config",type=Path,default=Path(__file__).resolve().parents[3]/"config.json")
    ap.add_argument("--queue-output",type=Path); ap.add_argument("--limit",type=int,default=0,help="test subset; unseen paths stay unchanged")
    ap.add_argument("--workers",type=int,default=0,help="bounded torrent parser threads; 0 selects a conservative default")
    ap.add_argument("--commit-every",type=int,default=100)
    ap.add_argument("--progress-file",type=Path)
    ap.add_argument("--name-query",action="append",default=[],help="targeted filename fragment; repeatable")
    a=ap.parse_args(); root=a.root.absolute()
    storage = status_for_path(root, timeout=4.0)
    if storage.state != AVAILABLE:
        publish_progress(a.progress_file, "pool_unavailable", {"state": storage.state}, complete=True)
        print(f"[storage] Torrent Pool: {storage.state}", file=sys.stderr)
        return 3
    state_dir = Path(os.getenv("ANM_STATE_DIR", str(Path(__file__).resolve().parents[3] / ".local" / "state")))
    config,_=load_config(a.config,state_dir/"config-cache.json")
    ix=indexer(); db=connect(a.db); schema(db);migrate(db);stamp=now();seen=set();policy=config["torrentPolicy"]
    # Reparse only when fields used by byte-level classification change.
    # Ranking/UI preferences are evaluated from indexed facts and must never
    # trigger a full reread of a hundred-thousand-file network pool.
    classifier_policy = {
        "contentClasses": policy.get("contentClasses", {}),
        "resolutions": policy.get("resolutions", {}),
        "allowUnlisted": {key: policy.get("allowUnlisted", {}).get(key)
                          for key in ("sourceClass", "resolution")},
        "resourceGroups": [{key: group.get(key) for key in ("id", "name", "aliases", "tier", "order")}
                           for group in policy.get("resourceGroups", [])],
    }
    policy_fingerprint=hashlib.sha256(json.dumps({"policy":classifier_policy,"classifierVersion":ix.CLASSIFIER_VERSION},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    prior_policy=db.execute("SELECT value FROM catalog_meta WHERE key='torrent_classification_policy'").fetchone()
    legacy_fingerprint=hashlib.sha256(json.dumps({"policy":policy,"classifierVersion":ix.CLASSIFIER_VERSION},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    legacy_migrated=bool(prior_policy and prior_policy["value"]==legacy_fingerprint)
    force_policy_reclassify=not prior_policy or (prior_policy["value"]!=policy_fingerprint and not legacy_migrated)
    name_queries=[value.casefold() for value in a.name_query if str(value).strip()]
    targeted=bool(name_queries)
    stats={"discovered":0,"unchanged":0,"parsed":0,"errors":0,"missing":0,"targeted":targeted}
    workers=max(1,min(8,a.workers or max(2,min(4,(os.cpu_count() or 2)//2))))
    commit_every=max(25,int(a.commit_every))
    last_publish=0.0
    scan_complete=True

    def candidates():
        nonlocal last_publish, scan_complete
        try:
            for path in _iter_torrents_complete(root):
                if name_queries and not any(value in path.stem.casefold() for value in name_queries):
                    continue
                if a.limit and stats["discovered"]>=a.limit: break
                stats["discovered"]+=1
                source=str(path.absolute())
                seen.add(source.casefold())
                st=path.stat()
                prior=db.execute("SELECT size,mtime_ns,presence_state FROM torrent_source WHERE source_path=?",(source,)).fetchone()
                if prior and not force_policy_reclassify and prior["size"]==st.st_size and prior["mtime_ns"]==st.st_mtime_ns and prior["presence_state"]=="present":
                    db.execute("UPDATE torrent_source SET last_seen_at=? WHERE source_path=?",(stamp,source)); stats["unchanged"]+=1
                else:
                    yield path,source,int(st.st_size),int(st.st_mtime_ns)
                if time.monotonic()-last_publish>=1:
                    publish_progress(a.progress_file,"pool_discovery",stats); last_publish=time.monotonic()
        except (OSError, StorageUnavailableError):
            scan_complete=False

    def parse(entry):
        path,source,size,mtime_ns=entry
        try:
            return entry,ix.inspect(path,True,policy),None,False
        except (OSError, StorageUnavailableError) as e:
            return entry,None,f"{type(e).__name__}: {e}",True
        except Exception as e:
            return entry,None,f"{type(e).__name__}: {e}",False

    def batches(iterable,size):
        batch=[]
        for item in iterable:
            batch.append(item)
            if len(batch)>=size:
                yield batch; batch=[]
        if batch: yield batch

    publish_progress(a.progress_file,"pool_discovery",stats)
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="anm-torrent") as executor:
      for batch in batches(candidates(),workers*4):
       for entry,r,parse_error,storage_error in executor.map(parse,batch):
        path,source,size,mtime_ns=entry
        if storage_error:
            scan_complete=False
            continue
        try:
            if parse_error: raise ValueError(parse_error)
            h=r["infoHash"].lower()
            manifest=hashlib.sha256(json.dumps(r["files"],ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
            reason=json.dumps(r.get("rejectReasons",[]),ensure_ascii=False,separators=(",",":"))
            db.execute("""INSERT INTO torrent(info_hash,torrent_path,torrent_bytes,mtime_utc,manifest_sha256,source_class,effective_group,language_hint,scan_state,scan_reason,info_name,title_state,file_count,total_bytes,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'unmapped',?,?,?) ON CONFLICT(info_hash) DO UPDATE SET torrent_path=excluded.torrent_path,torrent_bytes=excluded.torrent_bytes,mtime_utc=excluded.mtime_utc,manifest_sha256=excluded.manifest_sha256,source_class=excluded.source_class,effective_group=excluded.effective_group,language_hint=excluded.language_hint,scan_state=excluded.scan_state,scan_reason=excluded.scan_reason,info_name=excluded.info_name,file_count=excluded.file_count,total_bytes=excluded.total_bytes,indexed_at=excluded.indexed_at""",
              (h,source,r["torrentBytes"],r["mtimeUtc"],manifest,r["sourceClass"],r.get("resourceGroup"),r["subtitleLanguageHint"],r["eligibility"],reason,r["name"],r["fileCount"],r["totalBytes"],stamp))
            db.execute("UPDATE torrent SET torrent_created_at=?,created_by=?,release_flags_json=?,collection_hint=?,video_height=?,video_scan=?,bit_depth=?,release_unit=?,volume_sequence_json=?,episode_sequence_json=? WHERE info_hash=?",(r.get("creationDateUtc"),r.get("createdBy"),json.dumps(r.get("releaseFlags",[]),ensure_ascii=False),1 if r.get("collectionHint") else 0,r.get("videoHeight"),r.get("videoScan"),r.get("bitDepth"),r.get("releaseUnit","unknown"),json.dumps(r.get("volumeSequence",[])),json.dumps(r.get("episodeSequence",[])),h))
            db.execute("DELETE FROM torrent_manifest_file WHERE info_hash=?",(h,))
            db.executemany("INSERT INTO torrent_manifest_file(info_hash,file_index,source_path,length) VALUES(?,?,?,?)",((h,int(item["index"]),item["path"],int(item["length"])) for item in r["files"]))
            prior_resolution=db.execute("SELECT disposition,proposal_evidence_json FROM torrent_resolution WHERE info_hash=?",(h,)).fetchone()
            try: classifier_owned=bool(prior_resolution and json.loads(prior_resolution["proposal_evidence_json"] or "{}").get("owner")=="source_classifier")
            except (TypeError,ValueError): classifier_owned=False
            classifier_evidence=json.dumps({"owner":"source_classifier","policyFingerprint":policy_fingerprint},ensure_ascii=False)
            if r["eligibility"] == "reject":
                if not prior_resolution:
                    db.execute("INSERT INTO torrent_resolution(info_hash,disposition,review_reason_text,manual_action,user_note,proposal_evidence_json,updated_at) VALUES(?,'hard_reject',?,'pending','',?,?)",(h,", ".join(r.get("rejectReasons",[])),classifier_evidence,stamp))
                elif classifier_owned:
                    db.execute("UPDATE torrent_resolution SET disposition='hard_reject',review_reason_text=?,proposal_evidence_json=?,updated_at=? WHERE info_hash=?",(", ".join(r.get("rejectReasons",[])),classifier_evidence,stamp,h))
            elif classifier_owned and prior_resolution["disposition"]=="hard_reject":
                db.execute("UPDATE torrent_resolution SET disposition='review',review_reason_text='policy reclassification cleared classifier reject',proposal_evidence_json=?,updated_at=? WHERE info_hash=?",(classifier_evidence,stamp,h))
            db.execute("""INSERT INTO torrent_source(source_path,size,mtime_ns,info_hash,presence_state,last_seen_at,parse_error) VALUES(?,?,?,?,'present',?,NULL) ON CONFLICT(source_path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,info_hash=excluded.info_hash,presence_state='present',last_seen_at=excluded.last_seen_at,parse_error=NULL""",(source,size,mtime_ns,h,stamp)); stats["parsed"]+=1
        except Exception as e:
            message=parse_error or f"{type(e).__name__}: {e}"
            db.execute("""INSERT INTO torrent_source(source_path,size,mtime_ns,info_hash,presence_state,last_seen_at,parse_error) VALUES(?,?,?,NULL,'error',?,?) ON CONFLICT(source_path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,info_hash=NULL,presence_state='error',last_seen_at=excluded.last_seen_at,parse_error=excluded.parse_error""",(source,size,mtime_ns,stamp,message)); stats["errors"]+=1
        if (stats["parsed"]+stats["errors"])%commit_every==0: db.commit()
       publish_progress(a.progress_file,"pool_parse",stats); last_publish=time.monotonic()
    publish_progress(a.progress_file,"pool_reconcile",stats)
    if scan_complete and not a.limit and not targeted:
        for r in db.execute("SELECT source_path FROM torrent_source WHERE presence_state!='missing'"):
            if r["source_path"].casefold() not in seen:
                db.execute("UPDATE torrent_source SET presence_state='missing',last_seen_at=? WHERE source_path=?",(stamp,r["source_path"])); stats["missing"]+=1
        # A hash remains usable while any indexed copy is present.  Mark only
        # fully vanished torrent assets unavailable and touch indexed_at only
        # when the effective availability changed, so overlay sync can skip a
        # no-op pool walk even at very large scale.
        db.execute("""UPDATE torrent SET metadata_state='missing',indexed_at=?
            WHERE COALESCE(asset_kind,'torrent')='torrent' AND metadata_state!='missing'
              AND NOT EXISTS(SELECT 1 FROM torrent_source s WHERE s.info_hash=torrent.info_hash AND s.presence_state='present')""", (stamp,))
        db.execute("""UPDATE torrent SET metadata_state='available',indexed_at=?
            WHERE COALESCE(asset_kind,'torrent')='torrent' AND metadata_state!='available'
              AND EXISTS(SELECT 1 FROM torrent_source s WHERE s.info_hash=torrent.info_hash AND s.presence_state='present')""", (stamp,))
    db.execute(
        """INSERT OR IGNORE INTO torrent_resolution(info_hash,disposition,review_reason_text,manual_action,user_note,proposal_evidence_json,updated_at)
           SELECT info_hash,'hard_reject',COALESCE(scan_reason,''),'pending','',?,? FROM torrent WHERE scan_state='reject'""",
        (json.dumps({"owner": "source_classifier", "policyFingerprint": policy_fingerprint}, ensure_ascii=False), stamp),
    )
    # A limited warm-up must not bless a new policy over old, unvisited rows.
    # First-ever warm-up is safe because every remaining source is new; legacy
    # fingerprint migration is also safe because only non-classifier fields
    # were removed from the digest.
    if scan_complete and ((not a.limit and not targeted) or not prior_policy or legacy_migrated):
        db.execute("INSERT INTO catalog_meta(key,value,updated_at) VALUES('torrent_classification_policy',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(policy_fingerprint,stamp))
    db.commit()
    queue=[dict(r) for r in db.execute("""SELECT t.info_hash infoHash,t.info_name infoName,t.torrent_path torrentPath,t.source_class sourceClass,t.effective_group effectiveGroup,t.language_hint languageHint,t.file_count fileCount,t.total_bytes totalBytes,t.scan_state scanState,t.scan_reason scanReason FROM torrent t WHERE t.scan_state!='reject' AND COALESCE(t.title_state,'unmapped')='unmapped' AND NOT EXISTS(SELECT 1 FROM torrent_work tw WHERE tw.info_hash=t.info_hash AND tw.mapping_state='verified') ORDER BY COALESCE(t.info_name,t.torrent_path),t.info_hash""")]
    counts={r["scan_state"]:r["n"] for r in db.execute("SELECT scan_state,COUNT(*) n FROM torrent GROUP BY scan_state")}
    review_count=db.execute("SELECT COUNT(*) FROM title_review tr JOIN torrent t ON t.info_hash=tr.info_hash WHERE t.scan_state!='reject' AND t.title_state='review'").fetchone()[0]
    out={"schemaVersion":"2.0","generatedAt":now(),"root":str(root),"policyReclassified":force_policy_reclassify,"stats":{**stats,"catalogStates":counts,"unresolved":len(queue),"manualReview":review_count},"records":queue}
    if not scan_complete:
        out["stats"]["storageState"] = "unavailable"
    text=json.dumps(out,ensure_ascii=False,indent=2)+"\n"
    if a.queue_output: a.queue_output.parent.mkdir(parents=True,exist_ok=True); a.queue_output.write_text(text,encoding="utf-8")
    else: print(text,end="")
    publish_progress(a.progress_file,"pool_complete",out["stats"],complete=True)
    if not scan_complete:
        return 3
    return 2 if stats["errors"] else 0
if __name__=="__main__": raise SystemExit(main())
