#!/usr/bin/env python3
"""Bind one PCM narration master to sparse presenter windows.

``auto-build PLAN TALK OUTPUT`` keeps a single narration master. Without
``--presenter-binding``, TALK is the existing audio+video talk and this module
is not used. With a binding, TALK is 48 kHz stereo PCM and presenter pictures
are copied only from the named source intervals, one output frame per 1/60 s,
with no speed change. Every other frame is one shared absence plate, not a
frame from either presenter source.

This is not a narration edit map. An edit map deletes intervals from one
audio+video source and shortens the clock. Windows here do not cut the master.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import wave

SCHEMA = 'adu-presenter-binding/1'
FRAME_SIZE = (720, 1280)
ABSENCE_RGB = (18, 18, 18)
GEOMETRY = ('fps=60:start_time=0,'
            'scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,'
            'pad=720:1280:(ow-iw)/2:(oh-ih)/2')


def fail(message: str) -> None:
    raise ValueError(message)


def run(args: list[str]) -> None:
    result = subprocess.run([str(item) for item in args], capture_output=True, text=True)
    if result.returncode:
        fail(f'{args[0]} failed: {result.stderr[-2000:]}')


def fingerprint(path: Path) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return dict(sha256=digest.hexdigest(), sizeBytes=path.stat().st_size)


def frame_of(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        fail(f'{label} must be a nonnegative finite number')
    frame = int((Decimal(str(value)) * 60).to_integral_value(rounding=ROUND_HALF_UP))
    if frame < 0:
        fail(f'{label} is outside the 60fps grid')
    return frame


def sha256_text(value) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        fail('sha256 must be 64 lowercase hex characters')
    return value


def probe_video(path: Path) -> dict:
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, text=True)
    if result.returncode:
        fail(f'Cannot read presenter source {path.name}: {result.stderr[-500:]}')
    data = json.loads(result.stdout)
    video = [stream for stream in data.get('streams', []) if stream.get('codec_type') == 'video']
    if len(video) != 1:
        fail(f'{path.name} needs exactly one video stream')
    stream = video[0]
    if stream.get('color_transfer') in ('arib-std-b67', 'smpte2084'):
        fail(f'{path.name} is HDR. Presenter binding does not tone-map; supply the existing SDR export.')
    duration = stream.get('duration') or data.get('format', {}).get('duration')
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        fail(f'{path.name} has no finite video duration')
    if not math.isfinite(duration) or duration <= 0:
        fail(f'{path.name} has no positive video duration')
    return dict(duration=duration)


def window_frames(window: dict, index: int) -> dict:
    label = f'windows[{index}]'
    if not isinstance(window, dict):
        fail(label + ' must be an object')
    ident = window.get('id')
    if not isinstance(ident, str) or not ident.strip():
        fail(label + ' needs an id')
    source_id = window.get('sourceId')
    if not isinstance(source_id, str) or not source_id.strip():
        fail(label + ' needs sourceId')
    if 'masterStartFrame' in window or 'masterEndFrame' in window or 'sourceStartFrame' in window:
        for key in ('masterStartFrame', 'masterEndFrame', 'sourceStartFrame'):
            if type(window.get(key)) is not int:
                fail(f'{label}.{key} must be an integer frame on the 60fps grid')
        master_start, master_end = window['masterStartFrame'], window['masterEndFrame']
        source_start = window['sourceStartFrame']
    else:
        for key in ('masterStartSeconds', 'masterEndSeconds', 'sourceStartSeconds'):
            if key not in window:
                fail(label + ' needs 60fps frame fields or ' + key)
        master_start = frame_of(window['masterStartSeconds'], label + '.masterStartSeconds')
        master_end = frame_of(window['masterEndSeconds'], label + '.masterEndSeconds')
        source_start = frame_of(window['sourceStartSeconds'], label + '.sourceStartSeconds')
    if master_start >= master_end:
        fail(label + ' must cover at least one output frame')
    if source_start < 0:
        fail(label + ' source start is negative')
    return dict(id=ident, sourceId=source_id, masterStartFrame=master_start, masterEndFrame=master_end,
                sourceStartFrame=source_start, length=master_end - master_start)


def load(binding: Path, narration: Path, frames: int) -> dict:
    binding, narration = Path(binding), Path(narration)
    if not binding.is_file():
        fail('Presenter binding file is missing')
    if not narration.is_file():
        fail('Narration master is missing')
    if type(frames) is not int or frames < 1:
        fail('Presenter binding needs the plan frame count')
    try:
        raw = json.loads(binding.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        fail(f'Presenter binding is not JSON: {exc}')
    if not isinstance(raw, dict) or raw.get('schema') != SCHEMA:
        fail('Presenter binding schema must be ' + SCHEMA)
    if raw.get('fps', 60) != 60:
        fail('Presenter binding uses the 60fps output grid')
    declared = raw.get('narration')
    if not isinstance(declared, dict):
        fail('Presenter binding narration identity is missing')
    narration_sha = sha256_text(declared.get('sha256'))
    identity = fingerprint(narration)
    if identity['sha256'] != narration_sha:
        fail('Narration master bytes do not match presenter binding narration.sha256')
    try:
        with wave.open(str(narration), 'rb') as audio:
            if audio.getnchannels() != 2 or audio.getsampwidth() != 2 or audio.getframerate() != 48000:
                fail('Narration master must already be 48 kHz stereo 16-bit PCM. Binding does not resample or remix it.')
            if audio.getcomptype() != 'NONE':
                fail('Narration master must be uncompressed PCM')
            samples = audio.getnframes()
    except wave.Error as exc:
        fail(f'Narration master is not PCM WAV: {exc}')
    expected = frames * 800
    delta = expected - samples
    if delta == 0:
        edge = trimmed = 0
    elif 0 < delta < 800:
        edge, trimmed = delta, 0
    elif -800 < delta < 0:
        edge, trimmed = 0, -delta
    else:
        fail(f'Narration master is {samples} samples; the plan needs {expected}. '
             'Only a sub-frame grid remainder under 800 samples can be padded or trimmed. '
             'Refusing to stretch, splice, or retime the master.')
    sources = raw.get('sources')
    windows = raw.get('windows')
    if not isinstance(sources, list) or not sources or not isinstance(windows, list) or not windows:
        fail('Presenter binding needs at least one source and one window')
    prepared = []
    seen_ids = set()
    root = binding.parent
    for index, source in enumerate(sources):
        label = f'sources[{index}]'
        if not isinstance(source, dict):
            fail(label + ' must be an object')
        ident = source.get('id')
        if not isinstance(ident, str) or not ident.strip() or ident in seen_ids:
            fail(label + ' needs a unique id')
        seen_ids.add(ident)
        location = source.get('path')
        if not isinstance(location, str) or not location.strip():
            fail(label + ' needs a path')
        file = Path(location).expanduser()
        if not file.is_absolute():
            file = root / file
        file = file.resolve()
        if not file.is_file():
            fail(label + ' file is missing')
        file_identity = fingerprint(file)
        if file_identity['sha256'] != sha256_text(source.get('sha256')):
            fail(label + ' bytes do not match its sha256')
        media = probe_video(file)
        prepared.append(dict(id=ident, path=file, **file_identity, duration=media['duration']))
    by_id = {item['id']: item for item in prepared}
    parsed = []
    seen_windows = set()
    for index, window in enumerate(windows):
        item = window_frames(window, index)
        if item['id'] in seen_windows:
            fail('Duplicate presenter window id: ' + item['id'])
        seen_windows.add(item['id'])
        source = by_id.get(item['sourceId'])
        if source is None:
            fail('Presenter window uses an unknown source: ' + item['sourceId'])
        if item['masterEndFrame'] > frames:
            fail(item['id'] + ' extends past the narration plan')
        source_end = (item['sourceStartFrame'] + item['length']) / 60
        if source_end > source['duration'] + 1 / 60:
            fail(item['id'] + ' extends past its presenter source. Binding does not freeze or invent the missing frames.')
        parsed.append(item)
    parsed.sort(key=lambda item: item['masterStartFrame'])
    for left, right in zip(parsed, parsed[1:]):
        if right['masterStartFrame'] < left['masterEndFrame']:
            fail(f"Presenter windows overlap: {left['id']} and {right['id']}")
    used = {item['sourceId'] for item in parsed}
    unused = [item['id'] for item in prepared if item['id'] not in used]
    if unused:
        fail('Presenter source is not used by any window: ' + ', '.join(unused))
    return dict(narration=narration, narrationIdentity=identity, samples=samples, expectedSamples=expected,
                edgeQuantizationSamples=edge, trimmedTailSamples=trimmed, sources=prepared, windows=parsed, frames=frames)


def validate(binding: Path, narration: Path, frames: int) -> dict:
    return load(binding, narration, frames)


def _plate(path: Path) -> None:
    red, green, blue = ABSENCE_RGB
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
         '-i', f'color=c=0x{red:02x}{green:02x}{blue:02x}:s={FRAME_SIZE[0]}x{FRAME_SIZE[1]}:r=1:d=0.1',
         '-frames:v', '1', '-q:v', '2', path])


def _link(source: Path, dest: Path) -> None:
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def _extract(source: Path, start_frame: int, count: int, dest: Path) -> None:
    dest.mkdir()
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-ss', f'{start_frame / 60:.6f}',
         '-map', '0:v:0', '-an', '-vf', GEOMETRY, '-frames:v', str(count), '-q:v', '3',
         str(dest / 'f_%05d.jpg')])
    frames = sorted(dest.glob('f_*.jpg'))
    if len(frames) != count:
        fail(f'Presenter extract produced {len(frames)} of {count} frames. Refusing to clone a missing frame.')


def install(binding: Path, narration: Path, stage: Path, frames: int) -> dict:
    plan = load(binding, narration, frames)
    stage = Path(stage)
    talk = stage / 'talk'
    mapping = stage / 'talkmap.js'
    stub = '// Optional data absent. Generated data may replace this stub.'
    if talk.exists() and any(talk.iterdir()):
        fail('talk/ already contains media; bind into a fresh project')
    if mapping.exists() and mapping.read_text(encoding='utf-8').strip() != stub:
        fail('talkmap.js already contains project data; bind into a fresh project')
    for name in ('voice.wav', 'import.json'):
        if (stage / name).exists():
            fail(name + ' already exists; bind into a fresh project')
    sequence = talk / 'clip_000'
    sequence.mkdir(parents=True)
    plate = stage / '.presenter-absence.jpg'
    _plate(plate)
    covered = set()
    for window in plan['windows']:
        covered.update(range(window['masterStartFrame'], window['masterEndFrame']))
    for frame in range(plan['frames']):
        if frame in covered:
            continue
        _link(plate, sequence / f'f_{frame + 1:05d}.jpg')
    by_id = {item['id']: item for item in plan['sources']}
    for window in plan['windows']:
        token = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in window['id'])[:40]
        extract_dir = stage / f'.presenter-{window["masterStartFrame"]}-{token}'
        _extract(by_id[window['sourceId']]['path'], window['sourceStartFrame'], window['length'], extract_dir)
        for offset, extracted in enumerate(sorted(extract_dir.glob('f_*.jpg'))):
            extracted.replace(sequence / f'f_{window["masterStartFrame"] + offset + 1:05d}.jpg')
        shutil.rmtree(extract_dir)
    plate.unlink()
    written = sorted(sequence.glob('f_*.jpg'))
    if len(written) != plan['frames']:
        fail(f'Presenter grid has {len(written)} frames; the plan needs {plan["frames"]}')
    with wave.open(str(plan['narration']), 'rb') as src, wave.open(str(stage / 'voice.wav'), 'wb') as dst:
        dst.setnchannels(2)
        dst.setsampwidth(2)
        dst.setframerate(48000)
        pcm = src.readframes(plan['samples'])
        if plan['trimmedTailSamples']:
            pcm = pcm[:plan['expectedSamples'] * 4]
        elif plan['edgeQuantizationSamples']:
            pcm += b'\x00' * (4 * plan['edgeQuantizationSamples'])
        dst.writeframes(pcm)
    mapping.write_text('const TALKF=["clip_000"];const TALKMAP=' +
                       json.dumps([[0, i + 1] for i in range(plan['frames'])], separators=(',', ':')) + ';\n',
                       encoding='utf-8')
    gap = next((frame for frame in written if int(frame.stem.split('_')[1]) - 1 not in covered), None)
    receipt = dict(
        schema='adu-presenter-binding-import/1', fps=60, frames=plan['frames'], duration=plan['frames'] / 60,
        frame_width=FRAME_SIZE[0], frame_height=FRAME_SIZE[1],
        presenterBinding=dict(
            schema=SCHEMA,
            narration=dict(**plan['narrationIdentity'], sampleRate=48000, channels=2, sampleWidthBytes=2,
                           samples=plan['samples'], outputSamples=plan['expectedSamples'],
                           edgeQuantizationSamples=plan['edgeQuantizationSamples'],
                           trimmedTailSamples=plan['trimmedTailSamples'],
                           takenFromPresenterSources=False,
                           policy='PCM samples copied in order. A remainder under one 60fps frame may be digital silence or a trimmed tail; samples are not resampled, spliced, or retimed.'),
            sources=[dict(id=item['id'], sha256=item['sha256'], sizeBytes=item['sizeBytes'],
                          durationSeconds=item['duration']) for item in plan['sources']],
            windows=[dict(id=item['id'], sourceId=item['sourceId'], masterStartFrame=item['masterStartFrame'],
                          masterEndFrame=item['masterEndFrame'], sourceStartFrame=item['sourceStartFrame'],
                          sourceStartSeconds=item['sourceStartFrame'] / 60,
                          advance='one output frame uses the next 1/60s of that source; speed stays 1')
                     for item in plan['windows']],
            absence=dict(rgb=list(ABSENCE_RGB), frames=plan['frames'] - len(covered),
                         sha256=fingerprint(gap)['sha256'] if gap else None,
                         policy='Frames outside windows are copies of one generated flat plate. They are not presenter-source frames.'),
            extraction=dict(filter=GEOMETRY, audio='ignored',
                            policy='Nearest 60fps sample, lanczos fit into the existing 720x1280 talk box. No frozen tail and no second clock.')
        )
    )
    (stage / 'import.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return receipt
