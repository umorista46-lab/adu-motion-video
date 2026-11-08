"""Narration PCM stays independent of sparse presenter windows.

Synthetic media only. The old single audio+video talk path is still required
to reject a WAV, and an edit map is still not a substitute for this binding.
"""
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from adapt_project import AdaptError
from auto_templates import library, save
from build_auto_project import build_auto
from color_management import fingerprint_file
from narration_slice import preflight
from presenter_binding import ABSENCE_RGB, GEOMETRY, install


def tone(samples):
    data = array('h')
    for index in range(samples):
        value = int(9000 * math.sin(index / 47))
        data.append(value)
        data.append(value)
    return data.tobytes()


def write_wav(path, samples):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(tone(samples))


def write_source(path, frames, rate=60):
    folder = path.parent / (path.stem + '-frames')
    folder.mkdir()
    for index in range(frames):
        Image.new('RGB', (320, 180), ((index * 9) % 256, 30 + (index % 50), 180)).save(folder / f'f_{index:04d}.png')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', str(rate), '-i', str(folder / 'f_%04d.png'),
                    '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=3',
                    '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
                   check=True, capture_output=True)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pixel(path):
    with Image.open(path) as image:
        return image.convert('RGB').getpixel((image.width // 2, image.height // 2))


def color_count(path, color, tolerance):
    with Image.open(path) as image:
        pixels = image.convert('RGB').getdata()
    return sum(1 for pixel in pixels if sum(abs(a - b) for a, b in zip(pixel, color)) <= tolerance)


def mean_difference(left_path, right_path):
    with Image.open(left_path) as left, Image.open(right_path) as right:
        left, right = list(left.convert('RGB').getdata()), list(right.convert('RGB').getdata())
    if len(left) != len(right):
        raise AssertionError('frame sizes differ')
    return sum(abs(a - b) for one, two in zip(left, right) for a, b in zip(one, two)) / (len(left) * 3)


def oracle(source, start_frame, dest):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-ss', f'{start_frame / 60:.6f}',
                    '-map', '0:v:0', '-an', '-vf', GEOMETRY, '-frames:v', '1', '-q:v', '3', str(dest)],
                   check=True, capture_output=True)


class PresenterBindingTests(unittest.TestCase):
    def test_master_audio_and_sparse_frames_stay_traceable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames = 240
            narration = root / 'master.wav'
            write_wav(narration, frames * 800)
            source = root / 'person.mp4'
            write_source(source, 180)
            binding = root / 'binding.json'
            binding.write_text(json.dumps(dict(
                schema='adu-presenter-binding/1',
                narration=dict(sha256=sha256(narration)),
                sources=[dict(id='person-a', path='person.mp4', sha256=sha256(source))],
                windows=[dict(id='window-a', sourceId='person-a', masterStartFrame=30, masterEndFrame=90, sourceStartFrame=15)]
            ), indent=2))
            stage = root / 'stage'
            stage.mkdir()
            receipt = install(binding, narration, stage, frames)
            with wave.open(str(stage / 'voice.wav'), 'rb') as handle:
                self.assertEqual((handle.getframerate(), handle.getnchannels(), handle.getsampwidth()), (48000, 2, 2))
                self.assertEqual(handle.readframes(handle.getnframes()), tone(frames * 800))
            info = receipt['presenterBinding']
            self.assertFalse(info['narration']['takenFromPresenterSources'])
            self.assertEqual(info['narration']['edgeQuantizationSamples'], 0)
            self.assertEqual(info['absence']['frames'], frames - 60)
            gap = pixel(stage / 'talk/clip_000/f_00001.jpg')
            self.assertLess(sum(abs(a - b) for a, b in zip(gap, ABSENCE_RGB)), 12)
            produced = stage / 'talk/clip_000/f_00031.jpg'
            reference = root / 'oracle.jpg'
            oracle(source, 15, reference)
            self.assertLess(mean_difference(produced, reference), 1)
            self.assertNotEqual(pixel(produced), ABSENCE_RGB)
            preflight(stage, narration, 0, frames, 60)
            narration.write_bytes(narration.read_bytes() + b'x')
            with self.assertRaises(AdaptError):
                preflight(stage, narration, 0, frames, 60)

    def test_binding_rejects_resample_overlap_and_invented_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            narration = root / 'master.wav'
            write_wav(narration, 240 * 800)
            source = root / 'person.mp4'
            write_source(source, 30)
            def bind(windows, samples=240 * 800, rate=48000):
                file = root / 'case.wav'
                with wave.open(str(file), 'wb') as handle:
                    handle.setnchannels(2); handle.setsampwidth(2); handle.setframerate(rate)
                    handle.writeframes(tone(samples))
                payload = dict(schema='adu-presenter-binding/1', narration=dict(sha256=sha256(file)),
                               sources=[dict(id='person-a', path=str(source), sha256=sha256(source))], windows=windows)
                path = root / 'case.json'
                path.write_text(json.dumps(payload))
                return path, file
            path, file = bind([dict(id='a', sourceId='person-a', masterStartSeconds=0, masterEndSeconds=0.5, sourceStartSeconds=0),
                               dict(id='b', sourceId='person-a', masterStartSeconds=0.4, masterEndSeconds=0.8, sourceStartSeconds=0)])
            with self.assertRaisesRegex(ValueError, 'overlap'):
                install(path, file, root / 'overlap', 240)
            path, file = bind([dict(id='a', sourceId='person-a', masterStartFrame=0, masterEndFrame=10, sourceStartFrame=0)], rate=44100)
            with self.assertRaisesRegex(ValueError, 'does not resample'):
                install(path, file, root / 'rate', 240)
            path, file = bind([dict(id='a', sourceId='person-a', masterStartFrame=0, masterEndFrame=120, sourceStartFrame=0)])
            with self.assertRaisesRegex(ValueError, 'extends past its presenter source'):
                install(path, file, root / 'tail', 240)

    def test_single_talk_import_still_rejects_wav_and_hashes_its_own_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            narration = root / 'master.wav'
            write_wav(narration, 800)
            project = root / 'project'
            project.mkdir()
            completed = subprocess.run([sys.executable, str(ROOT / 'scripts/import_talk.py'), str(project), str(narration)],
                                       capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn('video', completed.stderr.lower() + completed.stdout.lower())
            self.assertFalse((project / 'voice.wav').exists())
            video = root / 'talk.bin'
            video.write_bytes(b'single-talk-bytes')
            identity = fingerprint_file(video)
            (project / 'import.json').write_text(json.dumps(dict(
                fps=60, frames=10, color=dict(source_sha256=identity['sha256'], source_size_bytes=identity['sizeBytes']))))
            (project / 'talk/clip_000').mkdir(parents=True)
            for index in range(1, 11):
                (project / 'talk/clip_000' / f'f_{index:05d}.jpg').write_bytes(b'x')
            preflight(project, video, 0, 10, 60)
            video.write_bytes(b'different-bytes')
            with self.assertRaisesRegex(AdaptError, 'Narration source changed'):
                preflight(project, video, 0, 10, 60)
            help_text = subprocess.run([sys.executable, str(ROOT / 'scripts/pipeline.py'), 'auto-build', '--help'],
                                       capture_output=True, text=True)
            self.assertEqual(help_text.returncode, 0)
            self.assertIn('plan', help_text.stdout)
            self.assertIn('--presenter-binding', help_text.stdout)
            self.assertIn('--edit-map', help_text.stdout)
            both = subprocess.run([sys.executable, str(ROOT / 'scripts/pipeline.py'), 'auto-build',
                                   str(root / 'missing-plan.json'), str(narration), str(root / 'out'),
                                   '--edit-map', str(root / 'map.json'), '--presenter-binding', str(root / 'binding.json')],
                                  capture_output=True, text=True)
            self.assertEqual(both.returncode, 1)
            self.assertIn('cannot be combined', both.stderr)

    def test_auto_build_uses_master_audio_and_source_window(self):
        entries = library(layout='portrait')
        def entry(style):
            return next(item for item in entries if item['styleId'] == style)
        def segment(item, source, index):
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
            return dict(id=f's-{index}', text='合成测试段落，不是本期口播。', intent=contract['intents'][0],
                        phases=list(contract['requiredPhases']),
                        counts={key: limits['min'] for key, limits in contract.get('cardinality', {}).items()},
                        anchors={role: dict(frame=round((next(cue['at'] for cue in source['cues'] if cue['id'] == cue_id) - source['source']['start']) * 60))
                                 for cue_id, role in contract['cueRoles'].items()},
                        durationFrames=frames,
                        candidates={item['styleId'] + ':' + source['id']: dict(slots=slots)})
        editorial, kinetic = entry('01-D'), entry('01-E')
        first = next(scene for scene in editorial['manifest']['scenes'] if scene['id'] == 'basic-keyword')
        second = next(scene for scene in kinetic['manifest']['scenes'] if scene['id'] == 'claim-and-response')
        segments = [segment(editorial, first, 0), segment(kinetic, second, 1)]
        frames = sum(item['durationFrames'] for item in segments)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            brief = dict(schema='adu-auto-brief/1', layout='portrait', fps=60, brand='synthetic-binding',
                         music={'mode': 'none'}, narrationDuration=frames / 60,
                         faceTracking=dict(mode='fixed', cx=.5, cy=.42, h=.28),
                         allowedStyles=['01-D', '01-E'], segments=segments)
            brief_path = root / 'brief.json'
            brief_path.write_text(json.dumps(brief))
            save(brief_path, root / 'plan')
            narration = root / 'master.wav'
            write_wav(narration, frames * 800 - 100)
            source = root / 'person.mp4'
            write_source(source, 180)
            binding = root / 'binding.json'
            binding.write_text(json.dumps(dict(
                schema='adu-presenter-binding/1', narration=dict(sha256=sha256(narration)),
                sources=[dict(id='person-a', path='person.mp4', sha256=sha256(source))],
                windows=[dict(id='window-a', sourceId='person-a', masterStartFrame=48, masterEndFrame=108, sourceStartFrame=12)]
            )))
            project = root / 'project'
            build_auto(root / 'plan/auto_plan.json', narration, project, presenter_binding=binding)
            with wave.open(str(project / 'voice.wav'), 'rb') as handle:
                body = handle.readframes(handle.getnframes())
            original = tone(frames * 800 - 100)
            self.assertTrue(body.startswith(original))
            self.assertEqual(len(body), frames * 800 * 4)
            self.assertEqual(body[len(original):], b'\x00' * (100 * 4))
            imported = json.loads((project / 'import.json').read_text())
            self.assertEqual(imported['presenterBinding']['narration']['edgeQuantizationSamples'], 100)
            gap = pixel(project / 'talk/clip_000/f_00001.jpg')
            self.assertLess(sum(abs(a - b) for a, b in zip(gap, ABSENCE_RGB)), 12)
            produced = project / 'talk/clip_000/f_00049.jpg'
            reference = root / 'oracle.jpg'
            oracle(source, 12, reference)
            self.assertLess(mean_difference(produced, reference), 1)
            parts = sorted((project / 'compositions').iterdir())
            self.assertEqual(len(parts), 2)
            joined = b''
            for part in parts:
                with wave.open(str(part / 'voice.wav'), 'rb') as handle:
                    joined += handle.readframes(handle.getnframes())
            self.assertEqual(joined, body)
            self.assertEqual([part['style'] for part in json.loads((project / 'macro_plan.json').read_text())['parts']], ['01-D', '01-E'])
            stills = root / 'stills'
            command = ['node', str(ROOT / 'scripts/render_project.mjs'), str(parts[0] / 'index.html'),
                       '--stills-dir', str(stills), '--times', '0.2,1.0']
            browser = os.environ.get('CHROME')
            if not browser and Path('/opt/google/chrome/chrome').is_file():
                browser = '/opt/google/chrome/chrome'
            if browser:
                command.extend(['--browser', browser])
            module = os.environ.get('ADU_PLAYWRIGHT_MODULE')
            if not module and Path('/tmp/pw/node_modules/playwright-core/index.js').is_file():
                module = '/tmp/pw/node_modules/playwright-core/index.js'
            if module:
                command.extend(['--playwright-module', module])
            subprocess.run(command, check=True, capture_output=True, text=True)
            gap_still, window_still = sorted(stills.glob('*.png'))
            self.assertGreater(color_count(gap_still, ABSENCE_RGB, 18), 100)
            self.assertGreater(color_count(gap_still, ABSENCE_RGB, 18), color_count(window_still, ABSENCE_RGB, 18))
            shown = parts[0] / 'talk/clip_000/f_00061.jpg'
            source_color = pixel(shown)
            self.assertGreater(color_count(window_still, source_color, 36), 100)
            self.assertGreater(color_count(window_still, source_color, 36), color_count(gap_still, source_color, 36))


if __name__ == '__main__':
    unittest.main()
