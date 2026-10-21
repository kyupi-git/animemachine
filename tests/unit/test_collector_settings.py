from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from animemachine.config.collector import settings
from animemachine.config.policy import ConfigStore
from animemachine.torrents import collector, settings as shared
from animemachine.torrents.collector_filter import CatalogEvidence, FilterDecision


class CollectorSettingsTests(unittest.TestCase):
    def config(self):
        path = Path(__file__).parents[2] / 'src/animemachine/resources/config.example.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_packaged_and_source_examples_agree(self):
        root = Path(__file__).parents[2]
        example = json.loads((root / 'config/config.example.json').read_text(encoding='utf-8'))
        self.assertEqual(self.config(), example)
        schema = json.loads((root / 'config/config.schema.json').read_text(encoding='utf-8'))
        definition = schema['properties']['components']['properties']['torrentCollector']
        self.assertIn('search', definition['properties'])
        self.assertIn('pool', definition['properties'])

    def test_old_configs_receive_working_defaults_and_invalid_options_are_rejected(self):
        config = self.config()
        config['components'].pop('torrentCollector')
        original = copy.deepcopy(config)
        self.assertEqual(80, settings(config)['search']['maxResults'])
        self.assertEqual(original, config)
        ConfigStore.validate(config)
        invalid = [ {'search': {'sources': []}}, {'search': {'sources': ['unknown']}},
                    {'search': {'maxResults': True}}, {'search': {'maxResults': 1000}},
                    {'search': {'timeoutSeconds': '30'}}, {'pool': {'monitorEnabled': 'false'}} ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ConfigStore.validate({**config, 'components': {**config['components'], 'torrentCollector': value}})

    def test_managed_snapshot_contains_no_credentials_and_changes_only_when_needed(self):
        config = self.config()
        config['components']['aniRss']['apiKey'] = 'private-key'
        config['security']['password'] = 'private-password'
        with tempfile.TemporaryDirectory() as raw:
            catalog = Path(raw) / 'catalog.sqlite3'
            shared.publish(catalog, config)
            path = catalog.with_name('collector-settings.json')
            data = path.read_text(encoding='utf-8')
            self.assertNotIn('private-', data)
            stamp = path.stat().st_mtime_ns
            shared.publish(catalog, config)
            self.assertEqual(stamp, path.stat().st_mtime_ns)
            before = shared.read(catalog)
            path.write_text('{interrupted', encoding='utf-8')
            self.assertEqual(before, shared.read(catalog, before))
            self.assertIsNone(shared.read(catalog))

    def test_background_admission_reuses_preferences_without_deleting_existing_files(self):
        config = self.config()
        config['torrentPolicy']['contentClasses']['webrip'] = False
        decision = FilterDecision('accept', 'catalog_complete', 'high', 'serial', 'ANi', {})
        metadata = {'sourceClass':'webrip', 'resourceGroup':'ANi', 'videoHeight':1080}
        with mock.patch.object(collector, '_managed_config', config):
            deferred = collector._admission(decision, '[ANi] Work [Baha 1080p]', metadata)
            self.assertEqual('defer', deferred.decision)
            self.assertEqual('resource_preferences', deferred.reason)
            with mock.patch.object(collector.catalog_matcher, 'match', return_value=CatalogEvidence(generation='catalog-1')):
                before = collector._catalog_evidence('title', None).generation
                config['ui']['language'] = 'en'
                self.assertNotEqual(before, collector._catalog_evidence('title', None).generation)
            with mock.patch.object(collector, 'decide_final', return_value=decision), \
                 mock.patch.object(collector, '_catalog_evidence', return_value=CatalogEvidence()):
                self.assertEqual(asdict(decision), asdict(collector._audit_classifier('title', metadata)))
        with mock.patch.object(collector, '_managed_config', None):
            self.assertEqual(decision, collector._admission(decision, 'title', metadata))


if __name__ == '__main__':
    unittest.main()
