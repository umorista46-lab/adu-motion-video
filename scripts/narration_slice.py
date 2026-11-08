"""Internal frame-exact slices of a freshly imported, immutable master narration."""
from copy import deepcopy
from pathlib import Path
import os
import shutil
import wave
from adapt_project import read_json, require, integer
from color_management import fingerprint_file


def preflight(master: Path, talk: Path, start: int, end: int, fps: int):
    report = read_json(master / 'import.json')
    require(report['fps'] == fps, 'Master import FPS differs from child clock')
    integer(start, 'slice.start'); integer(end, 'slice.end', minimum=1)
    require(start < end <= report['frames'], 'Narration slice lies outside the master frame clock')
    binding = report.get('presenterBinding')
    if binding:
        narration = binding.get('narration') or {}
        require(fingerprint_file(talk) == dict(sha256=narration.get('sha256'), sizeBytes=narration.get('sizeBytes')),
                'Narration master changed after presenter binding')
    else:
        color = report['color']
        require(fingerprint_file(talk) == dict(sha256=color['source_sha256'], sizeBytes=color['source_size_bytes']),
                'Narration source changed after the master import')
    return dict(fps=fps, expectedFrames=end-start, frames=end-start, duration=(end-start)/fps,
                masterStartFrame=start, masterEndFrame=end, masterFrames=report['frames'],
                stage='slice-of-verified-master-import')


def install(master: Path, stage: Path, start: int, end: int, fps: int):
    report = deepcopy(read_json(master / 'import.json'))
    sequence = stage / 'talk/clip_000'; sequence.mkdir()
    for i, source_frame in enumerate(range(start + 1, end + 1), 1):
        src = master / 'talk/clip_000' / f'f_{source_frame:05d}.jpg'
        require(src.is_file(), 'Missing master narration frame: ' + str(source_frame))
        dst = sequence / f'f_{i:05d}.jpg'
        # These are freshly generated immutable frames, not user files.
        try: os.link(src, dst)
        except OSError: shutil.copy2(src, dst)
    with wave.open(str(master / 'voice.wav'), 'rb') as src:
        rate, channels, width = src.getframerate(), src.getnchannels(), src.getsampwidth()
        require(width == 2 and src.getcomptype() == 'NONE' and channels in (1, 2),
                'Master voice must be 16-bit mono or stereo PCM')
        require(rate % fps == 0, 'Master voice sample rate must divide evenly across the output frame')
        per = rate // fps
        a, b = start * per, end * per
        require(b <= src.getnframes(), 'Master narration audio is shorter than its frame clock')
        src.setpos(a); audio = src.readframes(b - a)
        with wave.open(str(stage / 'voice.wav'), 'wb') as dst:
            dst.setnchannels(channels); dst.setsampwidth(width); dst.setframerate(rate)
            dst.writeframes(audio)
    (stage / 'talkmap.js').write_text('const TALKF=["clip_000"];const TALKMAP=' +
                                   __import__('json').dumps([[0,i+1] for i in range(end-start)], separators=(',',':')) + ';\n')
    report.update(frames=end-start, duration=(end-start)/fps,
                  slice=dict(startFrame=start, endFrame=end, masterFrames=report['frames'], retimed=False))
    if (master/'narration_edit_map.json').is_file():
        shutil.copy2(master/'narration_edit_map.json',stage/'narration_edit_map.json')
        report.update(clock='edited-narration',cutsApplied=True,playbackRateChanged=False)
    (stage / 'import.json').write_text(__import__('json').dumps(report, ensure_ascii=False, indent=2) + '\n')
