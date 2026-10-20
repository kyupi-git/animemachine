"""Validated settings shared by manual search and the optional Collector service."""
from __future__ import annotations

import copy
from typing import Any

SEARCH_SOURCES = ('mikan', 'nyaa', 'acgrip')
DEFAULTS = {
    'search': {'sources': list(SEARCH_SOURCES), 'maxResults': 80, 'timeoutSeconds': 120},
    'pool': {'monitorEnabled': True, 'collectionEnabled': True, 'historyEnabled': True},
}


def settings(config: dict[str, Any]) -> dict[str, Any]:
    supplied = config.get('components', {}).get('torrentCollector', {})
    if not isinstance(supplied, dict):
        raise ValueError('components.torrentCollector must be an object')
    result = copy.deepcopy(DEFAULTS)
    for section in result:
        value = supplied.get(section, {})
        if not isinstance(value, dict):
            raise ValueError(f'components.torrentCollector.{section} must be an object')
        result[section].update(value)
    sources = result['search']['sources']
    if not isinstance(sources, list) or not sources or any(source not in SEARCH_SOURCES for source in sources):
        raise ValueError('collector_search_sources_required')
    result['search']['sources'] = list(dict.fromkeys(sources))
    for key, low, high in (('maxResults', 10, 200), ('timeoutSeconds', 30, 180)):
        value = result['search'][key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'components.torrentCollector.search.{key} must be {low}..{high}')
    for key in DEFAULTS['pool']:
        if not isinstance(result['pool'][key], bool):
            raise ValueError(f'components.torrentCollector.pool.{key} must be boolean')
    return result
