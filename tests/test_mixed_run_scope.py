"""Mixed auto-build parts keep their own narration ownership order.

A ready full-film plan can still fail while revalidating a style part when every
part shares one evidence object and the last part's scope overwrites the rest.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from adapt_project import read_json
from adaptation import validate_adapted_spec
from auto_templates import choose, library
from build_auto_project import _same_saved_runs
from content_evidence import from_text, validate_coverage
from pack_catalog import resolve

COUNTS = [3, 2, 3, 4, 2, 3, 2, 2, 2, 4, 2, 4, 2, 2, 2, 2, 1, 3, 2]
STYLES = ['01-D'] * 7 + ['01-E'] + ['01-D'] * 6 + ['01-E'] + ['01-D'] * 4


class MixedRunScopeTests(unittest.TestCase):
    def test_saved_aliased_scope_is_not_order_evidence_and_derived_scope_matches(self):
        self.assertEqual(sum(COUNTS), 47)
        self.assertEqual(len(STYLES), 19)
        pieces = [f'句{i:02d}。\n' for i in range(47)]
        text = ''.join(pieces)
        units = []
        cursor = 0
        for piece in pieces:
            units.append(dict(start=cursor, end=cursor + len(piece)))
            cursor += len(piece)
        evidence = from_text(text, units)
        ordered = [unit['id'] for unit in evidence['units']]
        groups = []
        cursor = 0
        for count in COUNTS:
            groups.append(ordered[cursor:cursor + count])
            cursor += count
        segments = []
        for index, refs in enumerate(groups):
            segments.append(dict(id=f's{index:02d}', ownershipRefs=list(refs), supportingRefs={}))
        # The full film owns every unit once, in narration order.
        full = validate_coverage(evidence, segments)
        self.assertEqual(full['status'], 'declared-source-verified')
        self.assertEqual(full['ownedUnits'], 47)
        self.assertEqual([ref for segment in segments for ref in segment['ownershipRefs']], ordered)
        runs = []
        start = 0
        for style, refs in zip(STYLES, groups):
            if not runs or runs[-1]['style'] != style:
                runs.append(dict(style=style, spec=dict(contentEvidence=evidence, adaptation=dict(segments=[]))))
            runs[-1]['spec']['adaptation']['segments'].append(dict(id=style, ownershipRefs=list(refs)))
            start += 1
        # Simulate the old shared-object assignment. JSON then stores the last part's scope in every part.
        for run in runs:
            run['spec']['contentEvidence']['scopeRefs'] = [ref for segment in run['spec']['adaptation']['segments']
                                                           for ref in segment['ownershipRefs']]
        self.assertEqual(len({id(run['spec']['contentEvidence']) for run in runs}), 1)
        saved = json.loads(json.dumps(runs))
        self.assertTrue(all(run['spec']['contentEvidence']['scopeRefs'] == saved[-1]['spec']['contentEvidence']['scopeRefs'] for run in saved))
        self.assertEqual(len(saved[0]['spec']['contentEvidence']['scopeRefs']), 8)
        self.assertEqual(len(saved[0]['spec']['adaptation']['segments'][0]['ownershipRefs']), 3)
        with self.assertRaises(Exception):
            validate_coverage(saved[0]['spec']['contentEvidence'], saved[0]['spec']['adaptation']['segments'])
        derived = []
        for run in saved:
            evidence_copy = json.loads(json.dumps(run['spec']['contentEvidence']))
            segments_copy = run['spec']['adaptation']['segments']
            evidence_copy['scopeRefs'] = [ref for segment in segments_copy for ref in segment['ownershipRefs']]
            derived.append(validate_coverage(evidence_copy, segments_copy)['ownedUnits'])
        self.assertEqual(derived, [19, 2, 16, 2, 8])
        self.assertEqual(sum(derived), 47)
        recomputed = json.loads(json.dumps(saved))
        for run in recomputed:
            run['spec']['contentEvidence']['scopeRefs'] = [ref for segment in run['spec']['adaptation']['segments']
                                                           for ref in segment['ownershipRefs']]
        self.assertTrue(_same_saved_runs(recomputed, saved))
        saved[0]['spec']['adaptation']['segments'][0]['ownershipRefs'] = list(reversed(saved[0]['spec']['adaptation']['segments'][0]['ownershipRefs']))
        self.assertFalse(_same_saved_runs(recomputed, saved))

    def test_choose_then_revalidate_keeps_each_part_scope(self):
        entries = library(layout='portrait')

        def entry(style):
            return next(item for item in entries if item['styleId'] == style)

        def segment(item, source, index, ref):
            contract = next(scene for scene in item['profile']['scenes'] if scene['sceneId'] == source['id'])
            slots = {}
            for slot in source['slots']:
                if slot['type'] == 'talk':
                    slots[slot['id']] = '@talk'
                elif slot['type'] in ('text', 'dynamicText'):
                    text = '测' * min(4, slot.get('maxChars', 4))
                    if str(slot.get('sourceText', '')).strip() == text:
                        text = '甲' + text[1:]
                    slots[slot['id']] = text
                else:
                    raise AssertionError(slot['type'])
            frames = round((source['source']['end'] - source['source']['start']) * 60)
            return dict(id=f's-{index}', text=f'合成段{index}', intent=contract['intents'][0],
                        phases=list(contract['requiredPhases']),
                        counts={key: limits['min'] for key, limits in contract.get('cardinality', {}).items()},
                        anchors={role: dict(frame=round((next(cue['at'] for cue in source['cues'] if cue['id'] == cue_id) - source['source']['start']) * 60))
                                 for cue_id, role in contract['cueRoles'].items()},
                        durationFrames=frames, ownershipRefs=[ref],
                        candidates={item['styleId'] + ':' + source['id']: dict(slots=slots)})

        editorial, kinetic = entry('01-D'), entry('01-E')
        keyword = next(scene for scene in editorial['manifest']['scenes'] if scene['id'] == 'basic-keyword')
        items = next(scene for scene in kinetic['manifest']['scenes'] if scene['id'] == 'three-items-consolidate')
        pieces = ['甲段正文。\n', '乙段正文。\n', '丙段正文。\n']
        text = ''.join(pieces)
        units, cursor = [], 0
        for piece in pieces:
            units.append(dict(start=cursor, end=cursor + len(piece)))
            cursor += len(piece)
        evidence = from_text(text, units)
        segments = [segment(editorial, keyword, 0, evidence['units'][0]['id']),
                    segment(kinetic, items, 1, evidence['units'][1]['id']),
                    segment(editorial, keyword, 2, evidence['units'][2]['id'])]
        frames = sum(item['durationFrames'] for item in segments)
        brief = dict(schema='adu-auto-brief/1', layout='portrait', fps=60, brand='synthetic-scope',
                     music={'mode': 'none'}, narrationDuration=frames / 60,
                     faceTracking=dict(mode='fixed', cx=.5, cy=.42, h=.28),
                     allowedStyles=['01-D', '01-E'], contentEvidence=evidence, segments=segments)
        with tempfile.TemporaryDirectory() as temp:
            result = choose(brief, Path(temp), entries)
        self.assertTrue(result['report']['ready'], result['report'].get('missing'))
        self.assertEqual([run['style'] for run in result['runs']], ['01-D', '01-E', '01-D'])
        scopes = []
        for run in result['runs']:
            owned = [ref for segment in run['spec']['adaptation']['segments'] for ref in segment['ownershipRefs']]
            self.assertEqual(run['spec']['contentEvidence']['scopeRefs'], owned)
            self.assertEqual(validate_coverage(run['spec']['contentEvidence'], run['spec']['adaptation']['segments'])['ownedUnits'], len(owned))
            manifest = read_json(resolve(run['selection'], ROOT / 'packs') / 'manifest.json')
            validate_adapted_spec(manifest, run['spec'], Path(temp), ROOT / 'adaptation-profiles', allow_pending_talk=True,
                                  composition_context=dict(startFrame=run['startFrame'], durationFrames=frames))
            scopes.append(owned[0])
        self.assertEqual(len(set(scopes)), 3)
        self.assertEqual(len({id(run['spec']['contentEvidence']) for run in result['runs']}), 3)


if __name__ == '__main__':
    unittest.main()
