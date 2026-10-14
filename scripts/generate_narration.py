#!/usr/bin/env python3
"""Generate local Japanese MP3s with edge-tts. No API key is used."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
FORMAT_VERSION = 2


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        temporary = Path(f.name)
    os.replace(temporary, path)


def load_script(root):
    data = json.loads((root / 'narration/narration.ja.json').read_text(encoding='utf-8'))
    previous_end = 0
    seen = set()
    slow = data['timeline']['default_slow']
    if slow <= 0:
        raise ValueError('default_slow must be positive')
    for scene in data['scenes']:
        sid = scene['id']
        start, end = scene['timeline_start_seconds'], scene['timeline_end_seconds']
        if sid in seen or start != previous_end or end <= start:
            raise ValueError(f'{sid}: scene boundaries or IDs are invalid')
        seen.add(sid)
        previous_end = end
        if scene['planned_audio_path'] != f'audio/{sid}.mp3':
            raise ValueError(f'{sid}: audio path must be audio/{sid}.mp3')
        cues = scene['narration_cues']
        if not cues or cues[0]['timeline_start_seconds'] != start:
            raise ValueError(f'{sid}: first cue must start with the scene')
        if ''.join(c['text'] for c in cues) != scene['narration_text']:
            raise ValueError(f'{sid}: cue texts must concatenate to narration_text; edit both together')
        previous = start - 1
        for cue in cues:
            t = cue['timeline_start_seconds']
            if not previous < t < end or not cue['text'].strip():
                raise ValueError(f'{sid}: cue times must increase within the scene')
            previous = t
    if previous_end != data['timeline']['duration_seconds']:
        raise ValueError('Timeline duration does not match scenes')
    return data


def fingerprint(scene, settings, slow):
    content = {'version': FORMAT_VERSION, 'settings': settings, 'slow': slow,
               'start': scene['timeline_start_seconds'], 'end': scene['timeline_end_seconds'],
               'text': scene['narration_text'], 'cues': scene['narration_cues']}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export_timeline(root, data, manifest=None):
    # Only local paths and timing data enter the browser; no generation service configuration.
    runtime = {'duration': data['timeline']['duration_seconds'],
               'audioSlow': data['timeline']['default_slow'],
               'scenes': [{'id': s['id'], 'start': s['timeline_start_seconds'],
                           'end': s['timeline_end_seconds'], 'path': s['planned_audio_path'],
                           'file': s['chapter_file']} for s in data['scenes']]}
    wall = 0
    for scene, result in zip(data['scenes'], runtime['scenes']):
        entry = (manifest or {}).get('scenes', {}).get(scene['id'], {})
        anchors = entry.get('render_to_audio')
        if anchors:
            result['anchors'] = anchors
            result['wallStart'] = wall
            result['wallDuration'] = anchors[-1][1]
        else:
            result['anchors'] = [[result['start'], 0], [result['end'], (result['end']-result['start'])*runtime['audioSlow']]]
            result['wallStart'] = wall
            result['wallDuration'] = result['anchors'][-1][1]
        wall += result['wallDuration']
    runtime['wallDuration'] = round(wall, 6)
    target = root / 'assets/narration-data.js'
    target.parent.mkdir(exist_ok=True)
    target.write_text('window.PhysicalAINarrationData = '+json.dumps(runtime, ensure_ascii=False, indent=2)+';\n', encoding='utf-8')


def duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                            check=True, capture_output=True, text=True)
    seconds = float(result.stdout)
    if seconds <= 0:
        raise ValueError(f'{path.name}: empty audio')
    return seconds


def speech_text(text):
    # Explicit pronunciations; the approved original text remains in the editable JSON.
    return text.replace('LLM', 'エルエルエム').replace('UV', 'ユーブイ').replace('AI', 'エーアイ')


def normalized(text):
    return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith(('P', 'Z')))


def silence_cuts(path, maximum_pause=0.4):
    """Only cut the middle of measured quiet pauses; never cut voiced words."""
    import re
    result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'info', '-i', str(path),
        '-af', 'silencedetect=noise=-48dB:d=0.7', '-f', 'null', '-'],
        check=True, capture_output=True, text=True)
    starts = [float(v) for v in re.findall(r'silence_start: ([\d.]+)', result.stderr)]
    ends = [float(v) for v in re.findall(r'silence_end: ([\d.]+)', result.stderr)]
    return [(a+maximum_pause/2, b-maximum_pause/2) for a,b in zip(starts,ends) if a>0.15 and b-a>0.7]


def retime(seconds, cuts):
    return seconds - sum(max(0, min(seconds, b)-a) for a,b in cuts)


async def generate_continuous_scene(root, scene, settings, slow, proxy, key, force=False):
    import truststore
    truststore.inject_into_ssl()
    import edge_tts
    cache = root/'audio/.cache'/key
    cache.mkdir(parents=True, exist_ok=True)
    raw, meta = cache/'continuous.mp3', cache/'words.jsonl'
    if force or not raw.exists() or not meta.exists():
        temp, temporary_meta = raw.with_suffix('.part.mp3'), meta.with_suffix('.part.jsonl')
        try:
            await edge_tts.Communicate(speech_text(scene['narration_text']), settings['voice'],
                rate=settings['rate'], pitch=settings['pitch'], volume=settings['volume'],
                boundary='WordBoundary', proxy=proxy, connect_timeout=15, receive_timeout=60
            ).save(str(temp), str(temporary_meta))
            duration(temp)
            os.replace(temp, raw); os.replace(temporary_meta, meta)
        finally:
            temp.unlink(missing_ok=True); temporary_meta.unlink(missing_ok=True)
    words = [json.loads(line) for line in meta.read_text(encoding='utf-8').splitlines()]
    words = [w for w in words if w['type']=='WordBoundary']
    expected_text = normalized(speech_text(scene['narration_text']))
    if not words or normalized(''.join(w['text'] for w in words)) != expected_text:
        raise ValueError(f"{scene['id']}: word timing does not match the narration")
    cuts = silence_cuts(raw, settings.get('maximum_pause_seconds', 0.4))
    actual = duration(raw)
    speech_end = (words[-1]['offset']+words[-1]['duration'])/1e7
    # Keep a short breathing space between scenes, without padding to the old visual slots.
    rendered_duration = retime(min(actual, speech_end+settings.get('scene_gap_seconds',0.45)), cuts)
    anchors = [[scene['timeline_start_seconds'],0.0]]
    position = 0
    word_positions = []
    for word in words:
        word_positions.append((position, retime(word['offset']/1e7,cuts)))
        position += len(normalized(word['text']))
    position = len(normalized(speech_text(scene['narration_cues'][0]['text'])))
    for cue in scene['narration_cues'][1:]:
        matched = next((offset for char,offset in word_positions if char>=position), None)
        if matched is None or matched <= anchors[-1][1] or matched >= rendered_duration:
            raise ValueError(f"{scene['id']}: invalid cue timing")
        anchors.append([cue['timeline_start_seconds'],round(matched,6)])
        position += len(normalized(speech_text(cue['text'])))
    anchors.append([scene['timeline_end_seconds'],round(rendered_duration,6)])
    spans, cursor = [], 0.0
    for a,b in cuts:
        if a >= actual: break
        spans.append((cursor,a));cursor=b
    if cursor < actual: spans.append((cursor,actual))
    filters=[f'[0:a]atrim=start={a:.8f}:end={b:.8f},asetpts=PTS-STARTPTS[c{i}]' for i,(a,b) in enumerate(spans)]
    filters.append(''.join(f'[c{i}]' for i in range(len(spans)))+
        f'concat=n={len(spans)}:v=0:a=1,apad,atrim=duration={rendered_duration:.8f}[out]')
    target = root/scene['planned_audio_path']; temp = target.with_suffix('.part.mp3')
    try:
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(raw),
            '-filter_complex',';'.join(filters),'-map','[out]','-ar','24000','-ac','1',
            '-c:a','libmp3lame','-b:a','96k',str(temp)],check=True,capture_output=True)
        measured=duration(temp)
        if abs(measured-rendered_duration)>.2:
            raise ValueError('Encoded duration differs from narration timing')
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True)
    return {'fingerprint':key,'path':scene['planned_audio_path'],'audio_sha256':digest(target),
            'duration_seconds':round(measured,3),'playback_seconds':round(rendered_duration,6),
            'render_to_audio':anchors,'speech_source_seconds':actual,
            'removed_pause_seconds':round(sum(b-a for a,b in cuts),3),
            'cue_measurements':[]}


async def generate_scene(root, scene, settings, slow, proxy, key, force=False):
    if settings.get('flow_mode') == 'continuous':
        return await generate_continuous_scene(root, scene, settings, slow, proxy, key, force)
    # Trust the OS certificate store (including an injected corporate/cloud proxy CA).
    # truststore preserves certificate and hostname verification; it never disables TLS checks.
    import truststore
    truststore.inject_into_ssl()
    import edge_tts
    sid = scene['id']
    cache = root / 'audio/.cache' / key
    cache.mkdir(parents=True, exist_ok=True)
    clips, filters, measurements = [], [], []
    cues = scene['narration_cues']
    for i, cue in enumerate(cues):
        clip = cache / f'{i:02d}.mp3'
        # A completed clip is atomically published. Incomplete network responses are never reused.
        if force or not clip.exists():
            temp = clip.with_suffix('.part.mp3')
            try:
                await edge_tts.Communicate(speech_text(cue['text']), settings['voice'],
                    rate=settings['rate'], pitch=settings['pitch'], volume=settings['volume'],
                    proxy=proxy, connect_timeout=15, receive_timeout=45).save(str(temp))
                duration(temp)
                os.replace(temp, clip)
            finally:
                temp.unlink(missing_ok=True)
        actual = duration(clip)
        following = cues[i+1]['timeline_start_seconds'] if i+1 < len(cues) else scene['timeline_end_seconds']
        slot = (following - cue['timeline_start_seconds']) * slow
        available = slot - 0.15
        if available <= 0:
            raise ValueError(f'{sid} cue {i+1}: slot is too short')
        rate = max(1.0, actual / available)
        if rate > settings['maximum_fit_speed']:
            raise ValueError(f'{sid} cue {i+1}: speech is {actual:.2f}s, slot is {slot:.2f}s. '
                             'Shorten this cue or move its boundary; audio was not truncated.')
        filters.append(f'[{i}:a]atempo={rate:.8f},apad,atrim=duration={slot:.8f},asetpts=PTS-STARTPTS[c{i}]')
        measurements.append({'timeline_start_seconds': cue['timeline_start_seconds'],
                             'source_speech_seconds': round(actual, 3),
                             'fit_speed': round(rate, 4), 'slot_seconds': slot})
        clips.append(clip)
    filters.append(''.join(f'[c{i}]' for i in range(len(clips)))+
                   f'concat=n={len(clips)}:v=0:a=1[out]')
    target = root / scene['planned_audio_path']
    temp = target.with_suffix('.part.mp3')
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-y']
    for clip in clips:
        command += ['-i', str(clip)]
    command += ['-filter_complex', ';'.join(filters), '-map', '[out]',
                '-ar', '24000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '96k', str(temp)]
    try:
        subprocess.run(command, check=True, capture_output=True)
        seconds = duration(temp)
        expected = (scene['timeline_end_seconds']-scene['timeline_start_seconds'])*slow
        if abs(seconds-expected) > 0.2:
            raise ValueError(f'{sid}: encoded duration differs from the timeline')
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {'fingerprint': key, 'path': scene['planned_audio_path'],
            'audio_sha256': digest(target), 'duration_seconds': round(seconds, 3),
            'cue_measurements': measurements}


async def run(args):
    root = args.root.resolve()
    data = load_script(root)
    settings = dict(data['generation'])
    if args.voice:
        settings['voice'] = args.voice
    if args.rate:
        settings['rate'] = args.rate
    if settings['engine'] != 'edge-tts':
        raise ValueError('Only edge-tts is supported')
    manifest_path = root / 'audio/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'schema_version': 1, 'scenes': {}}
    export_timeline(root, data, manifest)
    if args.sync_data:
        print('Exported local browser timeline; no speech service was contacted.')
        return 0
    slow = data['timeline']['default_slow']
    valid_ids = {s['id'] for s in data['scenes']}
    if args.scene and not set(args.scene) <= valid_ids:
        raise ValueError('Unknown scene ID')
    pending = []
    for scene in data['scenes']:
        if args.scene and scene['id'] not in args.scene:
            continue
        key = fingerprint(scene, settings, slow)
        target = root / scene['planned_audio_path']
        old = manifest['scenes'].get(scene['id'], {})
        current = (old.get('fingerprint') == key and target.exists() and
                   old.get('audio_sha256') == digest(target))
        if current and not args.force:
            print(f"{scene['id']}: unchanged; reusing MP3")
        else:
            pending.append((scene, key))
    if args.check:
        for scene, _ in pending:
            print(f"{scene['id']}: MP3 missing or out of date")
        return 1 if pending else 0
    if not pending:
        print('All selected MP3s are current. No speech service was contacted.')
        return 0
    for tool in ['ffmpeg', 'ffprobe']:
        if not shutil.which(tool):
            raise ValueError(f'{tool} is required; install FFmpeg before generating audio')
    proxy = args.proxy or os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    for scene, key in pending:
        print(f"{scene['id']}: generating Japanese MP3", flush=True)
        entry = await generate_scene(root, scene, settings, slow, proxy, key, force=args.force)
        manifest['scenes'][scene['id']] = entry
        manifest['audio_slow'] = slow
        manifest['settings'] = settings
        # Persist each completed scene so an interrupted run can resume without repeated requests.
        write_json(manifest_path, manifest)
        export_timeline(root, data, manifest)
        print(f"{scene['id']}: saved ({entry['duration_seconds']:.2f}s)", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--scene', action='append', help='Generate only this scene ID; repeatable')
    parser.add_argument('--voice', help='Override the JSON voice, e.g. ja-JP-KeitaNeural')
    parser.add_argument('--rate', help='Override the JSON speech rate, e.g. --rate=-5%%')
    parser.add_argument('--proxy', help='HTTPS proxy; defaults to HTTPS_PROXY')
    parser.add_argument('--force', action='store_true', help='Regenerate selected MP3s and their speech clips')
    parser.add_argument('--check', action='store_true', help='Check missing/stale MP3s without any service requests')
    parser.add_argument('--sync-data', action='store_true', help='Export browser scene data only; no audio generation')
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Interrupted. Completed scenes and clips are retained.', file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print proxy URLs or exception request URLs that may contain authentication.
        if isinstance(exc, (ValueError, FileNotFoundError, ImportError)):
            message = str(exc)
        else:
            message = (f'{type(exc).__name__}: speech generation or encoding failed. '
                       'Check access to speech.platform.bing.com over HTTPS/WebSocket and FFmpeg. '
                       'No API key is needed. Completed MP3s were retained.')
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
