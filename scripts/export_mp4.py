#!/usr/bin/env python3
"""Render the existing Canvas animation deterministically, with local narration, to MP4."""
import argparse
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def scene_at_wall(scenes, seconds):
    return next((s for s in scenes if seconds < s['wallStart']+s['wallDuration']), scenes[-1])


def export(args):
    from playwright.sync_api import sync_playwright
    root = args.root.resolve()
    subprocess.run(['python', str(root/'scripts/generate_narration.py'), '--root', str(root), '--check'], check=True)
    timeline_js = (root/'assets/narration-data.js').read_text(encoding='utf-8')
    data = json.loads(timeline_js.split('=',1)[1].strip().removesuffix(';'))
    seconds = min(data['wallDuration'], args.seconds) if args.seconds else data['wallDuration']
    frames = math.ceil(seconds * args.fps)
    output = args.output.resolve()
    if output.exists() and not args.overwrite:
        raise ValueError(f'{output} already exists; use --overwrite to replace it')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix('.partial.mp4')
    server = ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(root)))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='physicalai-export-') as temp_dir:
            temp = Path(temp_dir)
            wave = temp/'narration.wav'
            audio_command = ['ffmpeg','-nostdin','-v','error','-y']
            filters = []
            for i, scene in enumerate(data['scenes']):
                audio_command += ['-i',str(root/scene['path'])]
                filters.append(f"[{i}:a]apad,atrim=duration={scene['wallDuration']:.8f},asetpts=PTS-STARTPTS[a{i}]")
            filters.append(''.join(f'[a{i}]' for i in range(len(data['scenes'])))+
                           f"concat=n={len(data['scenes'])}:v=0:a=1[out]")
            audio_command += ['-filter_complex',';'.join(filters),'-map','[out]',
                              '-ar','24000','-ac','1','-c:a','pcm_s16le',str(wave)]
            subprocess.run(audio_command,check=True,capture_output=True)
            command = ['ffmpeg','-nostdin','-v','error','-y','-f','image2pipe','-vcodec','mjpeg',
                '-framerate',str(args.fps),'-i','pipe:0','-i',str(wave),
                '-map','0:v:0','-map','1:a:0','-c:v','libx264','-threads','2',
                '-preset','fast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k',
                '-t',str(seconds),'-movflags','+faststart',str(temporary_output)]
            with (temp/'encoder.log').open('wb') as log, sync_playwright() as p:
                launch = {'headless':True,'args':['--no-sandbox']}
                chromium = args.chromium or shutil.which('chromium') or shutil.which('google-chrome')
                if chromium:
                    launch['executable_path'] = chromium
                browser = p.chromium.launch(**launch)
                context = browser.new_context(viewport={'width':1280,'height':720},device_scale_factor=1)
                external, errors = [], []
                def route(request_route):
                    if urlsplit(request_route.request.url).hostname != '127.0.0.1':
                        external.append(request_route.request.url); request_route.abort()
                    else: request_route.continue_()
                context.route('**/*',route)
                # No live timers are needed: each Canvas frame is rendered at its exact export time.
                context.add_init_script('window.requestAnimationFrame = () => 0;')
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                encoder = subprocess.Popen(command,stdin=subprocess.PIPE,stderr=log)
                current = None
                try:
                    for number in range(frames):
                        wall = number/args.fps
                        scene = scene_at_wall(data['scenes'],wall)
                        if current != scene['file']:
                            current = scene['file']
                            response=page.goto(f'http://127.0.0.1:{server.server_port}/{current}')
                            if response.status != 200: raise RuntimeError('Chapter could not be loaded')
                            page.evaluate('document.fonts.ready')
                        encoded=page.evaluate('''(wall) => {
                            window.seekTo(window.PhysicalAI.wallToRender(wall));
                            return document.querySelector('canvas').toDataURL('image/jpeg',0.94).split(',')[1];
                        }''',wall)
                        if errors: raise RuntimeError('; '.join(errors))
                        if external: raise RuntimeError('Unexpected external request while rendering')
                        encoder.stdin.write(base64.b64decode(encoded))
                        if number % (args.fps*10) == 0:
                            print(f'Rendered {wall:.0f} / {seconds:.1f}s ({number}/{frames} frames)',flush=True)
                    encoder.stdin.close()
                    code=encoder.wait()
                    if code:
                        raise RuntimeError((temp/'encoder.log').read_text())
                finally:
                    if encoder.poll() is None:
                        encoder.terminate(); encoder.wait()
                    browser.close()
            probe=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams','-show_format',
                '-of','json',str(temporary_output)],check=True,capture_output=True,text=True).stdout)
            streams={s['codec_type']:s for s in probe['streams']}
            if not {'video','audio'} <= streams.keys(): raise RuntimeError('Missing video or audio stream')
            if streams['video']['width'] != 1280 or streams['video']['height'] != 720:
                raise RuntimeError('Wrong video dimensions')
            if abs(float(probe['format']['duration'])-seconds)>.15:
                raise RuntimeError('Video duration does not match narration')
            os.replace(temporary_output,output)
            print(f'Saved {output}: {seconds:.3f}s, 1280x720, {args.fps}fps, H.264 + AAC, {output.stat().st_size} bytes')
    finally:
        server.shutdown();server.server_close()
        temporary_output.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,default=ROOT/'exports/physicalAI-narrated.mp4')
    parser.add_argument('--fps',type=int,default=30)
    parser.add_argument('--seconds',type=float,help='Export a short preview only')
    parser.add_argument('--chromium',help='Chromium executable path')
    parser.add_argument('--overwrite',action='store_true')
    args=parser.parse_args()
    if not 1<=args.fps<=60: parser.error('fps must be between 1 and 60')
    if args.seconds is not None and args.seconds<=0: parser.error('seconds must be positive')
    export(args)


if __name__=='__main__':
    main()
