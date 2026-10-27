"""Read-only projection of existing background task state."""
from __future__ import annotations

import re
from typing import Any


def snapshot(maintenance: dict[str, Any], archive: dict[str, Any], preload: dict[str, Any],
             season: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    rows = []
    def append(key: str, kind: str, value: dict[str, Any]) -> None:
        state = str(value.get('state') or 'idle').lower()
        if state in {'idle', 'stopped', 'disabled'}:
            return
        state = {'checking':'running','building':'running','merging':'running','warming':'running',
                 'starting':'running','warm':'complete','ready':'complete','error':'failed'}.get(state,state)
        raw_error = str(value.get('error') or value.get('error_text') or '')
        error = raw_error.split(':',1)[0] if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?::.*)?',raw_error,re.S) else ('background_task_failed' if raw_error else '')
        done = value.get('done',value.get('processed',value.get('overallDone',0))) or 0
        total = value.get('total',value.get('overallTotal',0)) or 0
        rows.append({'id':key,'kind':kind,'state':state,'done':done,'total':total,'failed':value.get('failed',0),
                     'error':error,'phase':str(value.get('phase') or value.get('stage') or ''),
                     'retryable':state in {'failed','interrupted'} and kind != 'layout',
                     'animeId':value.get('animeId')})
    for key,value in maintenance.items():
        if key in {'torrentSearch','aniRssSearch'}:
            for anime_id,job in value.items():
                append(f'{key}:{anime_id}',key,job)
        else:
            append(key,key,value)
    append('archive','archive',archive)
    append('preload','preload',preload)
    append('season','season',season)
    append('layout','layout',layout)
    rows.sort(key=lambda row: ({'failed':0,'interrupted':0,'running':1,'paused':2}.get(row['state'],3),row['id']))
    return {'items':rows,'running':sum(row['state']=='running' for row in rows)}
