"""Offline regression tests. Test tones are confined to temporary directories."""
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('generator', ROOT/'scripts/generate_narration.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'narration').mkdir()
        self.data = json.loads((ROOT/'narration/narration.ja.json').read_text(encoding='utf-8'))
        self.data['generation'].pop('flow_mode', None)
        self.save()
        self.calls = []
        calls = self.calls

        class SpeechFixture:
            def __init__(self, text, voice, **kwargs):
                calls.append((text, voice))
                self.text = text

            async def save(self, path, metadata_path=None):
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                                '-i', 'sine=frequency=440:duration=0.6', '-ar', '24000',
                                '-c:a', 'libmp3lame', path], check=True, capture_output=True)
                if metadata_path:
                    words = [{'type':'WordBoundary','offset':i*50000,'duration':40000,'text':c}
                             for i,c in enumerate(generator.normalized(self.text))]
                    Path(metadata_path).write_text('\n'.join(json.dumps(w) for w in words),encoding='utf-8')

        self.modules = patch.dict(sys.modules, {
            'edge_tts': types.SimpleNamespace(Communicate=SpeechFixture),
            'truststore': types.SimpleNamespace(inject_into_ssl=lambda: None),
        })
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.temp.cleanup()

    def save(self):
        (self.root/'narration/narration.ja.json').write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')

    def run_generator(self, **changes):
        args = types.SimpleNamespace(root=self.root, voice=None, rate=None, sync_data=False,
                                     scene=['scene-01'], check=False, force=False, proxy=None)
        for key, value in changes.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(generator.run(args))

    def test_incremental_and_corrupt_audio(self):
        self.assertEqual(self.run_generator(), 0)
        self.assertEqual(len(self.calls), 2)
        mp3 = self.root/'audio/scene-01.mp3'
        self.assertAlmostEqual(generator.duration(mp3), 10, delta=.2)
        self.assertEqual(self.run_generator(), 0)
        self.assertEqual(len(self.calls), 2, 'unchanged script must not call speech service')
        self.assertEqual(self.run_generator(check=True), 0)
        scene = self.data['scenes'][0]
        scene['narration_cues'][0]['text'] += '変更。'
        scene['narration_text'] = ''.join(c['text'] for c in scene['narration_cues'])
        self.save()
        self.assertEqual(self.run_generator(check=True), 1)
        self.assertEqual(self.run_generator(), 0)
        self.assertEqual(len(self.calls), 4)
        self.assertFalse((self.root/'audio/scene-02.mp3').exists())
        mp3.write_bytes(b'corrupt')
        self.assertEqual(self.run_generator(check=True), 1)
        self.assertEqual(self.run_generator(), 0)
        self.assertEqual(len(self.calls), 4, 'completed speech clips can be reused to repair output')

    def test_invalid_script_is_rejected_before_generation(self):
        self.data['scenes'][0]['narration_text'] += '不一致'
        self.save()
        with self.assertRaisesRegex(ValueError, 'concatenate'):
            self.run_generator()
        self.assertEqual(self.calls, [])

    def test_missing_audio_check_does_not_generate(self):
        self.assertEqual(self.run_generator(check=True), 1)
        self.assertEqual(self.calls, [])

    def test_failed_request_preserves_completed_mp3(self):
        self.assertEqual(self.run_generator(), 0)
        target = self.root/'audio/scene-01.mp3'
        original = target.read_bytes()
        manifest = (self.root/'audio/manifest.json').read_bytes()
        scene = self.data['scenes'][0]
        scene['narration_cues'][0]['text'] += '変更。'
        scene['narration_text'] = ''.join(c['text'] for c in scene['narration_cues'])
        self.save()

        class FailedSpeech:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                Path(path).write_bytes(b'incomplete')
                raise RuntimeError('simulated service failure')

        with patch.dict(sys.modules, {'edge_tts': types.SimpleNamespace(Communicate=FailedSpeech)}):
            with self.assertRaisesRegex(RuntimeError, 'simulated service failure'):
                self.run_generator()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual((self.root/'audio/manifest.json').read_bytes(), manifest)
        self.assertFalse(list((self.root/'audio/.cache').rglob('*.part.mp3')))

    def test_continuous_speech_and_word_aligned_timeline(self):
        self.data['generation']['flow_mode'] = 'continuous'
        self.save()
        self.assertEqual(self.run_generator(),0)
        self.assertEqual(len(self.calls),1,'One contextual request per scene, not per fragment')
        manifest=json.loads((self.root/'audio/manifest.json').read_text())
        entry=manifest['scenes']['scene-01']
        self.assertLess(entry['playback_seconds'],2,'Do not pad speech to the old ten-second scene slot')
        anchors=entry['render_to_audio']
        self.assertEqual(anchors[0],[0,0])
        self.assertEqual(anchors[-1][0],5)
        self.assertEqual(anchors[1][0],2.4)
        self.assertTrue(all(a[1]<b[1] for a,b in zip(anchors,anchors[1:])))
        self.assertEqual(self.run_generator(),0)
        self.assertEqual(len(self.calls),1,'Unchanged continuous speech must be reused')


if __name__ == '__main__':
    unittest.main()
