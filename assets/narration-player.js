/* One clock controls Canvas chapters and local MP3s. No remote speech calls. */
(() => {
  'use strict';
  const data = window.PhysicalAINarrationData;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const format = t => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  let owner = null;

  function interpolate(value, points, inverse = false) {
    const x = inverse ? 1 : 0, y = inverse ? 0 : 1;
    for (let i=1; i<points.length; i++) {
      if (value <= points[i][x]) {
        const a=points[i-1], b=points[i];
        return a[y]+(b[y]-a[y])*clamp((value-a[x])/(b[x]-a[x]),0,1);
      }
    }
    return points.at(-1)[y];
  }
  function renderToWall(t) {
    const scene = data.scenes.find(s => t >= s.start && t < s.end) || data.scenes.at(-1);
    return scene.wallStart + interpolate(t, scene.anchors);
  }
  function wallToRender(t) {
    const scene = data.scenes.find(s => t >= s.wallStart && t < s.wallStart+s.wallDuration) || data.scenes.at(-1);
    return interpolate(t-scene.wallStart, scene.anchors, true);
  }

  class Timeline {
    constructor(start, end, slow) {
      this.start = start; this.end = end; this.slow = slow;
      this.held = start; this.epoch = performance.now(); this.playing = true;
      this.revision = 0;
    }
    get time() {
      if (!this.playing) return this.held;
      const start = renderToWall(this.start), end = renderToWall(this.end);
      const elapsed = (performance.now()-this.epoch)/1000 * data.audioSlow/this.slow;
      return wallToRender(start+((renderToWall(this.held)-start+elapsed) % (end-start)));
    }
    seek(t) {
      this.held = clamp(t, this.start, this.end - .001);
      this.epoch = performance.now(); this.revision++;
    }
    setPlaying(value) {
      this.held = this.time; this.epoch = performance.now();
      this.playing = value; this.revision++;
    }
    setSlow(value) {
      this.held = this.time; this.epoch = performance.now();
      this.slow = clamp(Math.round(value * 100) / 100, .5, 3); this.revision++;
    }
  }

  function initialSlow() {
    const m = /[?#&]slow=([\d.]+)/.exec(location.search + location.hash);
    return m && +m[1] > 0 ? clamp(+m[1], .5, 3) : data.audioSlow;
  }

  function mount(clock, playlist) {
    const style = document.createElement('style');
    style.textContent = `
      #pai-controls{position:fixed;z-index:10;bottom:4px;left:50%;transform:translateX(-50%);
        display:flex;align-items:center;gap:6px;max-width:calc(100vw - 16px);padding:4px 8px;
        border:1px solid #8fa6c866;border-radius:12px;background:#06142ee8;color:#fff;
        font:12px "Yu Gothic UI","Meiryo",sans-serif;box-sizing:border-box;white-space:nowrap}
      #pai-controls button,#pai-controls select{font:inherit;color:inherit;background:#183657;
        border:1px solid #8fa6c866;border-radius:6px;padding:3px 7px;min-height:26px;cursor:pointer}
      #pai-controls button[aria-pressed=true]{background:#24566a;border-color:#7fe7ff}
      #pai-controls input{width:150px;min-width:48px;accent-color:#7fe7ff}
      #pai-status{position:fixed;z-index:11;bottom:48px;left:50%;transform:translateX(-50%);
        max-width:90vw;padding:7px 12px;background:#06142ef2;color:#fff;border-radius:8px;
        font:13px "Yu Gothic UI","Meiryo",sans-serif;text-align:center}
      #pai-status:empty{display:none}
      @media(max-width:650px){#pai-controls{gap:3px;font-size:11px}#pai-controls input{width:60px}
        #pai-time{display:none}#pai-controls button{padding:3px 4px}}
      @media print{#pai-controls,#pai-status{display:none}}
    `;
    document.head.append(style);
    const bar = document.createElement('div');
    bar.id = 'pai-controls'; bar.setAttribute('role', 'group'); bar.setAttribute('aria-label', '再生操作');
    bar.innerHTML = `<button id="pai-play" type="button">一時停止</button>
      <button id="pai-restart" type="button">最初から</button>
      <button id="pai-audio" type="button" aria-pressed="false">音声ON</button>
      <input id="pai-seek" type="range" aria-label="再生位置" min="${clock.start}" max="${clock.end - .001}" step="0.1">
      <span id="pai-time"></span>
      <button id="pai-slower" type="button" aria-label="ゆっくり再生">↑</button>
      <span id="pai-slow"></span>
      <button id="pai-faster" type="button" aria-label="速く再生">↓</button>`;
    if (playlist) {
      const select = document.createElement('select');
      select.id = 'pai-chapter'; select.setAttribute('aria-label', '章を選択');
      ['OP・第1章', '第2章', '第3章', '第4章・案内'].forEach((label, i) => {
        const option = document.createElement('option'); option.value = i; option.textContent = label;
        select.append(option);
      });
      bar.append(select);
    }
    const status = document.createElement('div');
    status.id = 'pai-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    document.body.append(bar, status);
    const get = id => document.getElementById(id);
    const audio = document.createElement('audio');
    audio.id = 'pai-narration'; audio.preload = 'auto'; audio.setAttribute('aria-hidden', 'true');
    audio.preservesPitch = true; document.body.append(audio);
    const state = {clock, playlist, audio, enabled: false, scene: null, failed: null,
                   generation: 0, playPending: false, loading: false, blobURL: null, status};
    const soundCache = new Map();
    owner = state;
    const message = text => { status.textContent = text; };

    function syncAudio(force = false) {
      const t = clock.time;
      const scene = data.scenes.find(s => t >= s.start && t < s.end);
      if (!state.enabled || !scene) { audio.pause(); return; }
      if (state.scene !== scene.id) {
        audio.pause(); state.generation++; state.playPending = false;
        state.scene = scene.id; state.failed = null;
        const generation = state.generation;
        state.loading = true;
        audio.dataset.scenePath = scene.path;
        // Fetch the whole local MP3 once. A blob makes seeking reliable even on Python's
        // basic HTTP server, which does not implement byte-range responses.
        const loadSound = soundCache.has(scene.path)
          ? Promise.resolve(soundCache.get(scene.path))
          : fetch(scene.path, {cache: 'no-cache'}).then(response => {
              if (!response.ok) throw new Error('Local MP3 is missing');
              return response.blob();
            }).then(blob => { soundCache.set(scene.path, blob); return blob; });
        loadSound.then(blob => {
          if (generation !== state.generation) return;
          if (state.blobURL) URL.revokeObjectURL(state.blobURL);
          state.blobURL = URL.createObjectURL(blob);
          audio.src = state.blobURL; audio.load();
          state.loading = false; syncAudio(true);
        }).catch(() => {
          if (generation !== state.generation) return;
          state.loading = false; state.failed = scene.id;
          message(`音声ファイル ${scene.path} を生成してから、再度音声ONを押してください。`);
        });
        return;
      }
      if (state.loading || state.failed === scene.id) return;
      audio.playbackRate = data.audioSlow / clock.slow;
      const expected = renderToWall(t)-scene.wallStart;
      if (audio.readyState >= 1) {
        const target = Math.min(expected, Math.max(0, audio.duration - .02));
        if (force || (!audio.seeking && Math.abs(audio.currentTime - target) > .18)) audio.currentTime = target;
      }
      if (!clock.playing) { audio.pause(); return; }
      // The padded MP3 ends at the scene boundary. Do not repeatedly restart a finished file.
      if (Number.isFinite(audio.duration) && expected >= audio.duration - .02) { audio.pause(); return; }
      if (audio.paused && !state.playPending) {
        const generation = state.generation;
        state.playPending = true;
        audio.play().then(() => {
          if (generation !== state.generation) return;
          state.playPending = false;
          if (!state.enabled || !clock.playing) audio.pause();
        }).catch(error => {
          if (generation !== state.generation) return;
          state.playPending = false;
          if (error.name === 'AbortError') return;
          state.failed = scene.id;
          message(error.name === 'NotAllowedError'
            ? '音声ONを押して、ブラウザの音声再生を許可してください。'
            : `音声を再生できません。${scene.path} を生成してから再度音声ONを押してください。`);
        });
      }
    }
    state.syncAudio = syncAudio;
    audio.addEventListener('loadedmetadata', () => syncAudio(true));
    audio.addEventListener('error', () => {
      state.failed = state.scene; state.playPending = false;
      message('音声ファイルが見つからないか読み込めません。生成スクリプトを実行後、音声を入れ直してください。');
    });
    audio.addEventListener('playing', () => { if (state.enabled) message(''); });

    function seek(t, resume = false) {
      audio.pause(); clock.seek(t);
      if (resume) clock.setPlaying(true);
      state.syncVideo?.(); syncAudio(true); updateUI();
    }
    function toggle() {
      clock.setPlaying(!clock.playing); syncAudio(true); updateUI();
    }
    function toggleAudio() {
      state.enabled = !state.enabled; state.failed = null;
      if (state.enabled) { state.scene = null; syncAudio(true); }
      else { audio.pause(); message(''); }
      updateUI();
    }
    function tempo(delta) { clock.setSlow(clock.slow + delta); syncAudio(true); updateUI(); }
    function updateUI() {
      get('pai-play').textContent = clock.playing ? '一時停止' : '再生';
      get('pai-audio').textContent = state.enabled ? '音声OFF' : '音声ON';
      get('pai-audio').setAttribute('aria-pressed', String(state.enabled));
      get('pai-seek').value = clock.time;
      get('pai-time').textContent = `${format(renderToWall(clock.time)-renderToWall(clock.start))} / ${format(renderToWall(clock.end)-renderToWall(clock.start))}`;
      get('pai-time').title = '通常テンポの再生時刻';
      get('pai-slow').textContent = `表示×${clock.slow.toFixed(2)}`;
      if (playlist) get('pai-chapter').value = playlist.findIndex(c => clock.time >= c.start && clock.time < c.end);
    }
    function handleKey(e) {
      if (e.ctrlKey || e.metaKey || e.altKey || e.target?.closest?.('input,select,button,textarea')) return;
      if (e.code === 'Space') toggle();
      else if (e.code === 'Home') seek(clock.start);
      else if (e.code === 'ArrowRight') seek(clock.time + 1);
      else if (e.code === 'ArrowLeft') seek(clock.time - 1);
      else if (e.code === 'ArrowUp') tempo(.05);
      else if (e.code === 'ArrowDown') tempo(-.05);
      else if (playlist && /^(Digit|Numpad)[1-4]$/.test(e.code)) seek(playlist[+e.code.slice(-1)-1].start, true);
      else return;
      e.preventDefault();
    }
    state.seek = seek; state.toggle = toggle; state.handleKey = handleKey;
    get('pai-play').addEventListener('click', toggle);
    get('pai-restart').addEventListener('click', () => seek(clock.start, true));
    get('pai-audio').addEventListener('click', toggleAudio);
    get('pai-seek').addEventListener('input', e => seek(+e.target.value));
    get('pai-slower').addEventListener('click', () => tempo(.05));
    get('pai-faster').addEventListener('click', () => tempo(-.05));
    if (playlist) get('pai-chapter').addEventListener('change', e => seek(playlist[+e.target.value].start, true));
    addEventListener('keydown', handleKey);
    updateUI();
    function tick() {
      state.syncVideo?.(); syncAudio(); updateUI(); requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    return state;
  }

  function startPlaylist(frames) {
    const files = [...new Set(data.scenes.map(s => s.file))];
    const chapters = files.map(file => {
      const scenes = data.scenes.filter(s => s.file === file);
      return {file, start: scenes[0].start, end: scenes.at(-1).end};
    });
    const state = mount(new Timeline(0, data.duration, initialSlow()), chapters);
    let visible = 0, current = -1;
    function load(frame, i) {
      if (+frame.dataset.chapter === i && frame.hasAttribute('src')) return;
      frame.dataset.chapter = i;
      frame.src = `${chapters[i].file}#once&hold`;
    }
    state.syncVideo = () => {
      const i = chapters.findIndex(c => state.clock.time >= c.start && state.clock.time < c.end);
      if (i === current) return;
      current = i;
      const ready = frames.findIndex(f => f.hasAttribute('src') && +f.dataset.chapter === i);
      if (ready >= 0) visible = ready;
      else load(frames[visible], i);
      frames.forEach((f, j) => { f.className = j === visible ? 'on' : ''; });
      load(frames[1-visible], (i+1) % chapters.length);
    };
    state.syncVideo();
  }

  function createChapter(start, end) {
    // Embedded chapters borrow the parent's actual clock; they never create audio players.
    const parentState = window.parent !== window ? window.parent.PhysicalAI?.getState() : null;
    if (parentState?.playlist) {
      const clock = parentState.clock;
      addEventListener('keydown', e => parentState.handleKey(e));
      return {get time() { const t = clock.time; return t >= start && t < end ? t : start; },
              seek: t => parentState.seek(t), clock};
    }
    const clock = new Timeline(start, end, initialSlow());
    const state = mount(clock, null);
    const readT = () => {
      const m = /[?#&]t=([\d.]+)/.exec(location.search + location.hash);
      if (m) { clock.setPlaying(false); state.seek(+m[1]); }
    };
    readT(); addEventListener('hashchange', readT);
    return {get time() { return clock.time; }, seek: t => {clock.setPlaying(false); state.seek(t);}, clock};
  }

  window.PhysicalAI = {startPlaylist, createChapter, renderToWall, wallToRender, getState: () => owner};
})();
