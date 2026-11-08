"""Cross-style asset names stay pack-scoped during full-library planning.

The portrait catalog currently has two different `wall` contracts. Planning must
keep both, still apply each one to its own scenes, and refuse an unscoped lookup
that would have to pick a side. Synthetic counts below are fixture thresholds,
not a change to either pack's choreography.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from adapt_project import AdaptError, slot_bindings
from adaptation import BRIEF_SCHEMA, plan_adaptation
from auto_templates import capabilities, library, universe


def write_wall(folder, count, unique=27):
    folder.mkdir(parents=True)
    for index in range(count):
        (folder / f'{index:03d}.jpg').write_bytes(b'x')
    (folder / 'wall-meta.json').write_text(json.dumps({'uniqueWorks': unique, 'displayTiles': count}))
    (folder / 'index.json').write_text(json.dumps([{'slug': f'work-{index % unique}'} for index in range(count)]))
    return folder


def wall_scene(scene_id):
    return {'id': scene_id, 'slots': [{'id': 'wall', 'type': 'wall-sprites', 'required': True}]}


def bind_wall(scene_id, folder, definitions):
    return slot_bindings(wall_scene(scene_id), {'id': 'episode', 'slots': {'wall': {'path': str(folder), 'glob': '*.jpg'}}},
                         folder.parent, None, definitions)


class LibraryAssetDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.portrait = library(layout='portrait')
        cls.landscape = library(layout='landscape')

    def entry(self, entries, style):
        return next(item for item in entries if item['styleId'] == style)

    def test_portrait_library_retains_both_wall_contracts(self):
        caps = capabilities(self.portrait, 'portrait')
        self.assertEqual(caps['styles'], 11)
        self.assertEqual(caps['availableStyles'], 11)
        self.assertEqual(len(caps['groups']), 54)
        self.assertEqual(caps['availableGroups'], 54)
        manifest, profile, index = universe(self.portrait, {'layout': 'portrait'})
        self.assertEqual(len(profile['scenes']), 54)
        self.assertEqual(len(index), 54)
        editorial = self.entry(self.portrait, '01-D')['manifest']['assetDefinitions']['wall']
        showcase = self.entry(self.portrait, '03-A')['manifest']['assetDefinitions']['wall']
        self.assertNotEqual(editorial, showcase)
        self.assertEqual(manifest['assetDefinitions']['01-D:wall'], editorial)
        self.assertEqual(manifest['assetDefinitions']['03-A:wall'], showcase)
        self.assertNotIn('wall', manifest['assetDefinitions'])
        for entry in self.portrait:
            for key, value in entry['manifest'].get('assetDefinitions', {}).items():
                self.assertEqual(manifest['assetDefinitions'][entry['styleId'] + ':' + key], value)
        # A name owned by one pack remains addressable without a style prefix.
        presenter = self.entry(self.portrait, '03-A')['manifest']['assetDefinitions']['presenter']
        self.assertEqual(manifest['assetDefinitions']['presenter'], presenter)
        self.assertEqual(manifest['assetDefinitions']['03-A:presenter'], presenter)

    def test_landscape_library_has_the_same_cross_style_wall_split(self):
        manifest, _, _ = universe(self.landscape, {'layout': 'landscape'})
        editorial = self.entry(self.landscape, '01-D')['manifest']['assetDefinitions']['wall']
        showcase = self.entry(self.landscape, '03-A')['manifest']['assetDefinitions']['wall']
        self.assertNotEqual(editorial, showcase)
        self.assertEqual(self.entry(self.landscape, '01-D')['selection'], 'editorial-performance@1.2.0-candidate')
        self.assertEqual(self.entry(self.landscape, '03-A')['selection'], 'anim4-showcase-macro@1.3.1-candidate')
        self.assertEqual(manifest['assetDefinitions']['01-D:wall'], editorial)
        self.assertEqual(manifest['assetDefinitions']['03-A:wall'], showcase)
        self.assertNotIn('wall', manifest['assetDefinitions'])

    def test_real_pack_and_scoped_library_both_require_the_authored_wall_count(self):
        manifest, _, _ = universe(self.portrait, {'layout': 'portrait'})
        pack = self.entry(self.portrait, '03-A')['manifest']
        source = next(scene for scene in manifest['scenes'] if scene['id'].startswith('03-A:') and any(
            slot.get('id') == 'wall' and slot.get('type') == 'wall-sprites' for slot in scene.get('slots', [])))
        slot = next(item for item in source['slots'] if item['id'] == 'wall')
        self.assertNotIn('minFiles', slot)
        local = next(scene for scene in pack['scenes'] if scene['id'] == source['id'].split(':', 1)[1])
        with tempfile.TemporaryDirectory() as temp:
            folder = write_wall(Path(temp) / 'wall', 27)
            with self.assertRaisesRegex(AdaptError, 'needs 389 prepared sprite sheets'):
                bind_wall(source['id'], folder, manifest['assetDefinitions'])
            with self.assertRaisesRegex(AdaptError, 'needs 389 prepared sprite sheets'):
                bind_wall(local['id'], folder, pack['assetDefinitions'])
            sequence = next(scene for scene in manifest['scenes'] if any(
                slot.get('id') == 'grid_opus55p' and slot.get('type') == 'sequence' for slot in scene.get('slots', [])))
            sequence_slot = next(slot for slot in sequence['slots'] if slot['id'] == 'grid_opus55p')
            self.assertNotIn('minFiles', sequence_slot)
            frames = Path(temp) / 'frames'
            frames.mkdir()
            (frames / 'f_0001.jpg').write_bytes(b'x')
            with self.assertRaisesRegex(AdaptError, 'needs 780 images, found 1'):
                slot_bindings({'id': sequence['id'], 'slots': [{'id': 'grid_opus55p', 'type': 'sequence', 'required': True}]},
                              {'slots': {'grid_opus55p': str(frames)}}, frames.parent, None, manifest['assetDefinitions'])

    def test_scoped_counts_apply_and_unscoped_or_same_style_disagreement_fails(self):
        selected = deepcopy([self.entry(self.portrait, style) for style in ('01-D', '03-A')])
        by_style = {item['styleId']: item for item in selected}
        by_style['01-D']['manifest']['assetDefinitions']['wall'] = {'originalItems': 30}
        by_style['03-A']['manifest']['assetDefinitions']['wall'] = {'originalItems': 40}
        manifest, _, _ = universe([by_style['01-D'], by_style['03-A']], {'layout': 'portrait'})
        self.assertNotIn('wall', manifest['assetDefinitions'])
        self.assertEqual(manifest['assetDefinitions']['01-D:wall']['originalItems'], 30)
        self.assertEqual(manifest['assetDefinitions']['03-A:wall']['originalItems'], 40)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            small = write_wall(root / 'small', 30)
            large = write_wall(root / 'large', 40)
            with patch('adapt_project.probe_media', return_value={'width': 512, 'height': 288}):
                small_bound, _ = bind_wall('01-D:synthetic', small, manifest['assetDefinitions'])
                large_bound, _ = bind_wall('03-A:synthetic', large, manifest['assetDefinitions'])
                self.assertEqual(small_bound['wall']['count'], 30)
                self.assertEqual(small_bound['wall']['uniqueWorks'], 27)
                self.assertEqual(large_bound['wall']['count'], 40)
                with self.assertRaisesRegex(AdaptError, 'needs 40 prepared sprite sheets'):
                    bind_wall('03-A:synthetic', small, manifest['assetDefinitions'])
                with self.assertRaisesRegex(AdaptError, 'needs 30 prepared sprite sheets'):
                    bind_wall('01-D:synthetic', large, manifest['assetDefinitions'])
            with self.assertRaisesRegex(AdaptError, 'Ambiguous library asset definition: wall') as raised:
                bind_wall('unscoped-scene', small, manifest['assetDefinitions'])
            self.assertNotIn('prepared sprite', str(raised.exception))
        conflicted = deepcopy(by_style['03-A'])
        conflicted['styleId'] = '01-D'
        conflicted['manifest']['assetDefinitions']['wall'] = {'originalItems': 41}
        with self.assertRaisesRegex(AdaptError, 'Conflicting library asset definitions: 01-D:wall'):
            universe([by_style['01-D'], conflicted], {'layout': 'portrait'})

    def test_identical_definitions_keep_the_bare_pack_name(self):
        selected = deepcopy([self.entry(self.portrait, style) for style in ('01-D', '03-A')])
        shared = deepcopy(selected[0]['manifest']['assetDefinitions']['wall'])
        selected[1]['manifest']['assetDefinitions']['wall'] = deepcopy(shared)
        manifest, _, _ = universe(selected, {'layout': 'portrait'})
        self.assertEqual(manifest['assetDefinitions']['wall'], shared)
        self.assertEqual(manifest['assetDefinitions']['01-D:wall'], shared)
        self.assertEqual(manifest['assetDefinitions']['03-A:wall'], shared)
        with tempfile.TemporaryDirectory() as temp:
            folder = write_wall(Path(temp) / 'wall', 27)
            with self.assertRaisesRegex(AdaptError, 'needs 389 prepared sprite sheets'):
                bind_wall('pack-scene', folder, manifest['assetDefinitions'])

    def test_single_style_universe_still_resolves_its_bare_wall(self):
        entry = self.entry(self.portrait, '01-D')
        manifest, _, _ = universe([entry], {'layout': 'portrait'})
        self.assertEqual(manifest['assetDefinitions']['wall'], entry['manifest']['assetDefinitions']['wall'])
        self.assertEqual(manifest['assetDefinitions']['01-D:wall'], entry['manifest']['assetDefinitions']['wall'])
        with tempfile.TemporaryDirectory() as temp:
            folder = write_wall(Path(temp) / 'wall', 27)
            with self.assertRaisesRegex(AdaptError, 'needs 389 prepared sprite sheets'):
                bind_wall('editorial-scene', folder, manifest['assetDefinitions'])
            with self.assertRaisesRegex(AdaptError, 'needs 389 prepared sprite sheets'):
                bind_wall('01-D:editorial-scene', folder, manifest['assetDefinitions'])

    def test_full_portrait_plan_uses_scoped_wall_and_does_not_abort(self):
        manifest, profile, _ = universe(self.portrait, {'layout': 'portrait'})
        entry = self.entry(self.portrait, '03-A')
        source = next(scene for scene in entry['manifest']['scenes'] if any(
            slot.get('id') == 'wall' and slot.get('type') == 'wall-sprites' for slot in scene.get('slots', [])))
        contract = next(scene for scene in entry['profile']['scenes'] if scene['sceneId'] == source['id'])
        frames = round((source['source']['end'] - source['source']['start']) * 60)
        with tempfile.TemporaryDirectory() as temp:
            folder = write_wall(Path(temp) / 'wall', 27)
            segment = dict(id='s0', text='合成测试：作品墙数量校验，不是本期口播。', intent=contract['intents'][0],
                           phases=list(contract['requiredPhases']),
                           counts={key: limits['min'] for key, limits in contract.get('cardinality', {}).items()},
                           anchors={role: dict(frame=round((next(cue['at'] for cue in source['cues'] if cue['id'] == cue_id) - source['source']['start']) * 60))
                                    for cue_id, role in contract['cueRoles'].items()},
                           durationFrames=frames,
                           candidates={'03-A:' + source['id']: dict(slots={'wall': {'path': str(folder), 'glob': '*.jpg'}})})
            brief = dict(schema=BRIEF_SCHEMA, layout='portrait', fps=60, brand='synthetic-library-repro',
                         music={'mode': 'none'}, narrationDuration=frames / 60, segments=[segment])
            subset = {**profile, 'scenes': [scene for scene in profile['scenes'] if scene['styleId'] == '03-A']}
            result = plan_adaptation(manifest, subset, brief, Path(temp), allow_pending_talk=True)
            blob = json.dumps(result['report'], ensure_ascii=False)
            self.assertIn('needs 389 prepared sprite sheets', blob)
            self.assertNotIn('Conflicting library asset definitions', blob)
            self.assertNotIn('Ambiguous library asset definition', blob)
            self.assertFalse(result['report']['ready'])

    def test_pipeline_auto_plan_no_longer_stops_on_cross_library_wall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            brief = dict(schema='adu-auto-brief/1', layout='portrait', fps=60, brand='synthetic-library-repro',
                         music={'mode': 'none'}, narrationDuration=1,
                         segments=[dict(id='s0', text='合成测试段落，不是本期口播。', intent='not-a-catalog-intent',
                                        phases=[], durationFrames=60, counts={}, anchors={}, candidates={})])
            path = root / 'brief.json'
            path.write_text(json.dumps(brief, ensure_ascii=False, indent=2) + '\n')
            output = root / 'plan'
            completed = subprocess.run([sys.executable, str(ROOT / 'scripts/pipeline.py'), 'auto-plan', str(path), str(output)],
                                       capture_output=True, text=True)
            self.assertNotIn('Conflicting library asset definitions', completed.stderr)
            self.assertEqual(completed.returncode, 2, completed.stderr + completed.stdout)
            report = json.loads((output / 'report.json').read_text())
            self.assertFalse(report['ready'])
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['stylesCompared'], 11)
            self.assertEqual(report['groupsCompared'], 54)
            self.assertEqual(report['availableStyles'], 11)
            self.assertEqual(report['availableGroups'], 54)
            self.assertEqual(report['selectedScenes'], [])
