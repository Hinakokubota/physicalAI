"""Browser integration checks against actual generated MP3s. No external requests allowed."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    script = json.loads((ROOT/'narration/narration.ja.json').read_text(encoding='utf-8'))
    assert all((ROOT/s['planned_audio_path']).is_file() for s in script['scenes']), 'Generate all MP3s first'
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
            context = browser.new_context(viewport={'width':1280, 'height':720})
            external = []
            def route(request_route):
                if urlsplit(request_route.request.url).hostname != '127.0.0.1':
                    external.append(request_route.request.url)
                    request_route.abort()
                else:
                    request_route.continue_()
            context.route('**/*', route)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base+'/index.html')
            page.wait_for_selector('iframe.on')
            page.locator('#pai-restart').click()
            page.locator('#pai-audio').click()
            page.wait_for_function('!document.querySelector("audio").paused && document.querySelector("audio").currentTime > .1')
            print('PASS: real scene-01 MP3 plays after user interaction')

            page.locator('#pai-play').click()
            frozen = page.evaluate('({t:PhysicalAI.getState().clock.time,a:document.querySelector("audio").currentTime})')
            page.wait_for_timeout(250)
            later = page.evaluate('({t:PhysicalAI.getState().clock.time,a:document.querySelector("audio").currentTime})')
            assert abs(frozen['t']-later['t']) < .001
            assert abs(frozen['a']-later['a']) < .03
            page.locator('#pai-play').click()
            page.wait_for_function('!document.querySelector("audio").paused')
            print('PASS: video/audio pause and resume together')

            for s in script['scenes']:
                print(f"Checking {s['id']}", flush=True)
                target = s['timeline_start_seconds'] + .5
                page.locator('#pai-seek').evaluate('(e,t)=>{e.value=t;e.dispatchEvent(new Event("input",{bubbles:true}));}', target)
                try:
                    page.wait_for_function('(id)=>PhysicalAI.getState().scene===id && document.querySelector("audio").readyState>=2 && !document.querySelector("audio").paused', arg=s['id'], timeout=10000)
                except Exception:
                    print(page.evaluate('({time:PhysicalAI.getState().clock.time,scene:PhysicalAI.getState().scene,failed:PhysicalAI.getState().failed,pending:PhysicalAI.getState().playPending,paused:document.querySelector("audio").paused,ready:document.querySelector("audio").readyState,source:document.querySelector("audio").currentSrc,status:document.querySelector("#pai-status").textContent})'), flush=True)
                    raise
                page.wait_for_timeout(100)
                state = page.evaluate('({t:PhysicalAI.getState().clock.time,a:document.querySelector("audio").currentTime,src:document.querySelector("audio").dataset.scenePath,slow:PhysicalAI.getState().clock.slow})')
                assert state['src'] == s['planned_audio_path'], state
                assert abs(state['a']-(state['t']-s['timeline_start_seconds'])*2) < .25, state
                assert page.locator('audio').count() == 1
                frame = page.locator('iframe.on').element_handle().content_frame()
                frame.wait_for_function('typeof window.seekTo === "function"')
                assert s['chapter_file'] in frame.url
                assert frame.locator('#pai-narration').count() == 0, 'hidden/child chapters must never play independent audio'
            print('PASS: all 20 real MP3s match the shared video timeline after seeking; one audio player only')

            # Natural scene, chapter, and whole-video transitions (seek near end, then let clock advance).
            for boundary, expected in [(5,'scene-02'), (45,'scene-06'), (85,'scene-11'), (140,'scene-16'), (185,'scene-01')]:
                page.evaluate('(t)=>PhysicalAI.getState().seek(t)', boundary-.1)
                page.wait_for_function('(id)=>PhysicalAI.getState().scene===id', arg=expected)
                page.wait_for_function('!document.querySelector("audio").paused && document.querySelector("audio").readyState>=2')
            print('PASS: natural scene/chapter transitions and final loop')

            page.locator('#pai-chapter').select_option('2')
            page.wait_for_function('PhysicalAI.getState().scene === "scene-11"')
            page.locator('#pai-restart').click()
            page.wait_for_function('PhysicalAI.getState().scene === "scene-01"')
            assert page.evaluate('PhysicalAI.getState().clock.time') < 1
            page.locator('#pai-audio').click()
            assert page.locator('audio').evaluate('(a)=>a.paused')
            page.locator('#pai-audio').click()
            page.wait_for_function('!document.querySelector("audio").paused')
            page.locator('#pai-slower').click()
            assert abs(page.locator('audio').evaluate('(a)=>a.playbackRate') - 2/2.05) < .001
            print('PASS: chapter select, restart, audio ON/OFF, tempo updates')

            for n in range(1,5):
                page.goto(base+f'/physical-ai{n}.html')
                page.locator('#pai-audio').click()
                page.wait_for_function('!document.querySelector("audio").paused && document.querySelector("audio").readyState>=2')
                page.locator('#pai-play').click()
                assert page.locator('audio').evaluate('(a)=>a.paused')
                page.locator('#pai-restart').click()
                page.wait_for_function('!document.querySelector("audio").paused')
            print('PASS: each standalone chapter supports local audio, pause, and restart')
            page.goto(base+'/physical-ai-flyer.html?paper=a4')
            assert page.locator('html').evaluate('(e)=>e.classList.contains("a4")')
            assert page.locator('#pai-controls').count() == 0
            assert not errors, errors
            assert not external, external
            print('PASS: flyer unchanged; zero JavaScript errors; zero external browser requests')
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
