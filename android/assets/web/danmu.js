/* FnPlayer 弹幕渲染引擎：Canvas 轨道 + 视频时间轴同步。
 *
 * 设计要点：
 * - 动画推进量取自 video.currentTime 的增量（而非 Date.now），
 *   因此暂停 / 缓冲 / 倍速 / 拖拽进度条都会自然同步，无需额外状态机。
 * - 时间轴跳变（seek）时清空在屏弹幕并二分定位到新位置，避免弹幕洪流。
 * - 滚动弹幕按轨道分配，只有上一弹幕尾部离开安全区才复用该轨道，避免重叠。
 */
'use strict';

(function (global) {
  const DEFAULTS = {
    enabled: true,
    opacity: 0.85,
    fontSize: 24,
    area: 0.75,
    speed: 8.0,       // 滚动弹幕穿越整屏所需秒数
    offset: 0.0,      // 时间偏移（秒），正数=延后
    bold: false,
  };

  class DanmuEngine {
    constructor(canvas, video, options = {}) {
      this.canvas = canvas;
      this.video = video;
      this.ctx = canvas.getContext('2d');
      this.opts = Object.assign({}, DEFAULTS, options || {});
      this.list = [];            // [{t, mode, color, text}]，按 t 升序
      this.cursor = 0;
      this.active = [];          // 在屏弹幕
      this.lanes = { scroll: [], top: [], bottom: [] };
      this.lastVT = -1;
      this.enabled = this.opts.enabled;
      this.running = false;
      this._raf = 0;
      this._lastFrame = 0;
      this.W = 0; this.H = 0;
      this._resize = () => this.resize();
      this._ro = (typeof ResizeObserver !== 'undefined')
        ? new ResizeObserver(this._resize) : null;
      if (this._ro && canvas.parentElement) this._ro.observe(canvas.parentElement);
      window.addEventListener('resize', this._resize);
      this.resize();
      this.start();
    }

    /* ---------------- 尺寸 ---------------- */
    resize() {
      const p = this.canvas.parentElement;
      const w = Math.max(1, Math.round(p ? p.clientWidth : this.canvas.clientWidth));
      const h = Math.max(1, Math.round(p ? p.clientHeight : this.canvas.clientHeight));
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      this.W = w; this.H = h;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.canvas.style.width = w + 'px';
      this.canvas.style.height = h + 'px';
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._measureAll();
    }

    _font() {
      const o = this.opts;
      return (o.bold ? '700 ' : '600 ') + o.fontSize + 'px "Microsoft YaHei", "PingFang SC", sans-serif';
    }

    _measure(item) {
      this.ctx.font = this._font();
      item.w = this.ctx.measureText(item.text).width;
      return item.w;
    }

    _measureAll() {
      this.ctx.font = this._font();
      for (const c of this.list) c.w = this.ctx.measureText(c.text).width;
      for (const a of this.active) a.w = this.ctx.measureText(a.text).width;
    }

    /* ---------------- 配置 ---------------- */
    setOptions(patch = {}) {
      const needMeasure = ('fontSize' in patch) || ('bold' in patch);
      Object.assign(this.opts, patch);
      if ('enabled' in patch) this.enabled = !!patch.enabled;
      if (needMeasure) this._measureAll();
      this.canvas.style.opacity = this.enabled ? '1' : '0';
      if (!this.enabled) this._clearActive();
    }

    setEnabled(on) { this.setOptions({ enabled: !!on }); }

    get count() { return this.list.length; }
    get activeCount() { return this.active.length; }

    /* ---------------- 数据 ---------------- */
    load(comments) {
      this.list = (Array.isArray(comments) ? comments : [])
        .filter((c) => c && typeof c.text === 'string' && c.text)
        .map((c) => ({
          t: Math.max(0, Number(c.t) || 0),
          mode: ['scroll', 'top', 'bottom'].includes(c.mode) ? c.mode : 'scroll',
          color: /^#[0-9a-f]{6}$/i.test(c.color) ? c.color : '#ffffff',
          text: String(c.text).slice(0, 200),
          w: 0,
        }))
        .sort((a, b) => a.t - b.t);
      this._measureAll();
      this.reset();
    }

    /** 本地发送一条弹幕，立即显示，并在之后 seek 回来时仍能出现。 */
    send(text, color = '#ffffff') {
      const t = Math.max(0, (this.video.currentTime || 0) + this.opts.offset);
      const item = { t, mode: 'scroll', color, text: String(text).slice(0, 200), w: 0 };
      this._measure(item);
      const i = this.list.findIndex((c) => c.t > t);
      if (i < 0) this.list.push(item); else this.list.splice(i, 0, item);
      // 已立即上屏，跳过时间轴重复投放
      this.cursor = Math.max(this.cursor, (i < 0 ? this.list.length : i + 1));
      this._spawn(item, t, true);
      this._draw();
      return item;
    }

    reset() {
      this._clearActive();
      this.lastVT = -1;
      this.cursor = this._indexAt((this.video.currentTime || 0) + this.opts.offset);
      this.canvas.style.opacity = this.enabled ? '1' : '0';
    }

    clear() {
      this.list = [];
      this.reset();
    }

    _clearActive() {
      this.active.length = 0;
      this.lanes = { scroll: [], top: [], bottom: [] };
    }

    _indexAt(t) {
      let lo = 0, hi = this.list.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.list[mid].t < t) lo = mid + 1; else hi = mid;
      }
      return lo;
    }

    /* ---------------- 轨道 ---------------- */
    _laneCount() {
      const laneH = this.opts.fontSize * 1.45;
      return Math.max(1, Math.floor((this.H * Math.min(1, this.opts.area)) / laneH));
    }

    _spawn(item, logicalTime, force = false) {
      const n = this._laneCount();
      const laneH = this.opts.fontSize * 1.45;
      const mode = item.mode;
      let lane = -1;

      if (mode === 'scroll') {
        const pool = this.lanes.scroll;
        while (pool.length < n) pool.push(-1e9);
        let best = -1, bestTail = Infinity;
        for (let i = 0; i < n; i++) {
          if (pool[i] < bestTail) { bestTail = pool[i]; best = i; }
        }
        // 最空轨道的尾部仍在安全区内时丢弃该弹幕，避免叠字
        if (!force && bestTail > this.W - 24) return null;
        lane = best;
        const x = force ? Math.max(0, this.W - item.w - 20) : this.W;
        const speedPx = (this.W + item.w) / (this.opts.speed * 1000);
        pool[lane] = x + item.w;
        const a = { ...item, x, y: 8 + lane * laneH, mode: 'scroll', vx: speedPx };
        this.active.push(a);
        return a;
      }

      const pool = mode === 'top' ? this.lanes.top : this.lanes.bottom;
      while (pool.length < n) pool.push(-1e9);
      const hold = this.opts.speed * 0.65 * 1000; // 视频时间毫秒
      for (let i = 0; i < n; i++) {
        if (pool[i] <= logicalTime * 1000) { lane = i; break; }
      }
      if (lane < 0) return null;
      pool[lane] = logicalTime * 1000 + hold;
      const row = mode === 'top' ? lane : (n - 1 - lane);
      const a = { ...item, x: Math.max(0, (this.W - item.w) / 2),
                  y: 8 + row * laneH, mode, born: logicalTime, hold };
      this.active.push(a);
      return a;
    }

    /* ---------------- 主循环 ---------------- */
    start() {
      if (this.running) return;
      this.running = true;
      this._lastFrame = 0;
      const loop = () => {
        if (!this.running) return;
        this._tick();
        this._raf = requestAnimationFrame(loop);
      };
      this._raf = requestAnimationFrame(loop);
    }

    stop() {
      this.running = false;
      if (this._raf) cancelAnimationFrame(this._raf);
      this._raf = 0;
      this._clearActive();
      this.ctx.clearRect(0, 0, this.W, this.H);
    }

    _tick() {
      const raw = this.video.currentTime;
      const vt = isFinite(raw) ? raw : 0;   // 未加载媒体时也能渲染本地弹幕
      const logical = vt + this.opts.offset;

      let dt = 0;
      if (this.lastVT < 0) {
        // 首次或换集：定位光标，不补发历史弹幕
        this.cursor = this._indexAt(logical - 0.05);
      } else if (vt < this.lastVT - 0.35 || vt - this.lastVT > 1.5) {
        // 拖动进度条 / 换集：清屏并重新定位
        this._clearActive();
        this.cursor = this._indexAt(logical - 0.05);
      } else {
        dt = (vt - this.lastVT) * 1000;
      }
      this.lastVT = vt;

      if (!this.enabled || !this.list.length) {
        this.ctx.clearRect(0, 0, this.W, this.H);
        return;
      }

      // 1) 推进在屏弹幕，并按当前实际位置重算每条轨道的「尾端」
      const keep = [];
      const n = this._laneCount();
      const laneH = this.opts.fontSize * 1.45;
      const tails = new Array(n).fill(-1e9);
      for (const a of this.active) {
        if (a.mode === 'scroll') {
          a.x -= a.vx * dt;
          if (a.x + a.w <= -20) continue;
          const lane = Math.max(0, Math.min(n - 1, Math.round((a.y - 8) / laneH)));
          tails[lane] = Math.max(tails[lane], a.x + a.w);
        } else if (logical * 1000 - a.born * 1000 >= a.hold) {
          continue;
        }
        keep.push(a);
      }
      this.active = keep;
      this.lanes.scroll = tails;

      // 2) 按时间轴投放新弹幕（seek 后已过期太久的不补发）
      while (this.cursor < this.list.length && this.list[this.cursor].t <= logical + 0.02) {
        const item = this.list[this.cursor++];
        if (logical - item.t < 1.2) this._spawn(item, logical);
      }

      this._draw();
    }

    _draw() {
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.W, this.H);
      ctx.font = this._font();
      ctx.textBaseline = 'top';
      ctx.lineJoin = 'round';
      ctx.globalAlpha = Math.max(0.15, Math.min(1, this.opts.opacity));
      ctx.lineWidth = Math.max(2, this.opts.fontSize / 9);
      ctx.strokeStyle = 'rgba(0,0,0,.72)';
      for (const a of this.active) {
        ctx.strokeText(a.text, a.x, a.y);
        ctx.fillStyle = a.color;
        ctx.fillText(a.text, a.x, a.y);
      }
      ctx.globalAlpha = 1;
    }

    destroy() {
      this.stop();
      if (this._ro) this._ro.disconnect();
      window.removeEventListener('resize', this._resize);
    }
  }

  global.DanmuEngine = DanmuEngine;
})(window);
