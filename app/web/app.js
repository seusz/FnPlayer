/* FnPlayer 前端：搜索 / 详情 / 内置 hls.js 播放器 / 历史 / 设置 */
'use strict';

const TOKEN = document.querySelector('meta[name="fn-token"]').content;
const $ = (id) => document.getElementById(id);
const S = {
  view: 'discover', page: 1, keyword: '', settings: {}, sources: [],
  detail: null, info: null, hls: null, lastReport: 0,
  switchTried: 0, autoLineActive: false, theme: 'dark',
  danmu: null, danmuMeta: null,
};

/* ------------------------------------------------------------ 基础工具 */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const method = (opts.method || 'GET').toUpperCase();
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (['POST', 'PATCH', 'DELETE', 'PUT'].includes(method)) headers['X-Fn-Token'] = TOKEN;
  let res;
  try {
    res = await fetch(path, {
      method, headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) { throw new Error('无法连接本地服务：' + e.message); }
  if (!res.ok && res.status !== 502) {
    let msg = 'HTTP ' + res.status;
    try { msg = (await res.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function b64url(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function posterUrl(u) {
  if (!u || !/^https?:\/\//i.test(u)) return '/static/placeholder.svg';
  return '/api/poster?u=' + b64url(u) + '&t=' + encodeURIComponent(TOKEN);
}
function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const pad = (n) => String(n).padStart(2, '0');
  return (h ? h + ':' + pad(m) : pad(m)) + ':' + pad(s);
}
let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}
function setTheme(name) {
  S.theme = name;
  document.documentElement.dataset.theme = name;
  try { localStorage.setItem('fn-theme', name); } catch (_) {}
}
function showView(name) {
  if (S.view === 'player' && name !== 'player') stopPlayback();
  S.view = name;
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (name === 'player' && S.danmu) setTimeout(() => S.danmu.resize(), 60);
}

/* ------------------------------------------------------------ 海报墙 */
function cardHtml(it) {
  const sub = [it.year, it.area, it.type_name].filter(Boolean).join(' · ') || '在线资源';
  const badge = it.score ? `<span class="badge score">★ ${esc(it.score)}</span>` : '';
  const meta = (it.line_count ? it.line_count + ' 条线路' : '') +
    (it.episode_count > 1 ? ' · ' + it.episode_count + ' 集' : '');
  return `<div class="card" data-id="${esc(it.id)}" title="${esc(it.title)}">
    <div class="card-poster">
      ${badge}
      <img loading="lazy" src="${posterUrl(it.poster)}" alt="${esc(it.title)}">
      <button class="play-fab" title="播放">▶</button>
      <div class="card-meta">${esc(meta)}</div>
    </div>
    <div class="card-title">${esc(it.title)}</div>
    <div class="card-sub">${esc(sub)}</div>
  </div>`;
}
function renderGrid(items, append) {
  const grid = $('posterGrid');
  grid.innerHTML = (append ? grid.innerHTML : '') + items.map(cardHtml).join('');
  $('emptyState').hidden = true;
  if (!grid.innerHTML) $('emptyState').hidden = false;
}
function showEmpty() {
  $('posterGrid').innerHTML = '';
  $('emptyState').hidden = false;
  $('discoverMeta').textContent = '';
}
async function loadDiscover(pg, append) {
  if (!S.sources.length) { showEmpty(); return; }
  $('loadingState').hidden = false;
  $('discoverMeta').textContent = '正在加载第 ' + pg + ' 页…';
  try {
    const r = await api('/api/discover?pg=' + pg);
    renderGrid(r.items || [], append);
    $('discoverTitle').textContent = '发现';
    $('discoverMeta').textContent = (r.items || []).length + ' 部 · 第 ' + pg + ' 页';
    $('loadMore').hidden = false;
    S.page = pg;
  } catch (e) {
    $('discoverMeta').textContent = '加载失败：' + e.message;
  } finally { $('loadingState').hidden = true; }
}
async function doSearch() {
  const q = $('searchInput').value.trim();
  if (!q) return;
  S.keyword = q;
  if (!S.sources.length) { showEmpty(); return; }
  showView('discover');
  $('discoverTitle').textContent = '搜索：' + q;
  $('discoverMeta').textContent = '正在跨接口聚合…';
  $('posterGrid').innerHTML = '';
  $('loadMore').hidden = true;
  $('loadingState').hidden = false;
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(q));
    renderGrid(r.items || [], false);
    $('discoverMeta').textContent = (r.items || []).length
      ? '找到 ' + r.items.length + ' 部 · ' + r.elapsed_ms + ' ms'
      : '没有找到结果（可尝试其它关键词或检查接口）';
    if (!(r.items || []).length) $('emptyState').hidden = false;
  } catch (e) {
    $('discoverMeta').textContent = '搜索失败：' + e.message;
  } finally { $('loadingState').hidden = true; }
}

/* ------------------------------------------------------------ 详情 */
async function openDetail(id) {
  showView('detail');
  $('detailBody').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载详情…</div>';
  try {
    const d = await api('/api/title/' + encodeURIComponent(id));
    S.detail = d;
    const lines = Object.keys(d.lines || {});
    const firstLine = lines[0] || '';
    const eps = d.lines[firstLine] || [];
    $('detailBody').innerHTML = `
      <div class="detail-hero">
        <img class="detail-poster" src="${posterUrl(d.poster)}" alt="">
        <div class="detail-info">
          <h1>${esc(d.title)}</h1>
          <div class="tags">
            ${d.year ? `<span class="tag">${esc(d.year)}</span>` : ''}
            ${d.area ? `<span class="tag">${esc(d.area)}</span>` : ''}
            ${d.type_name ? `<span class="tag">${esc(d.type_name)}</span>` : ''}
            ${d.score ? `<span class="tag">★ ${esc(d.score)}</span>` : ''}
            <span class="tag">${lines.length} 条线路</span>
          </div>
          <div class="detail-desc">${esc(d.overview || '暂无简介')}</div>
          ${d.actor ? `<div class="detail-desc">主演：${esc(d.actor)}</div>` : ''}
          <div class="detail-actions">
            <button class="primary-btn" id="detailPlay">▶ 立即播放</button>
            <select class="select" id="detailLine">${lines.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join('')}</select>
          </div>
        </div>
      </div>
      <div class="section-title">选集 <span class="muted" id="detailEpCount">${eps.length} 集</span></div>
      <div class="episode-grid" id="detailEps"></div>`;
    renderDetailEps(firstLine);
    $('detailPlay').onclick = () => startPlay(d.id, $('detailLine').value, 0);
    $('detailLine').onchange = () => renderDetailEps($('detailLine').value);
  } catch (e) {
    $('detailBody').innerHTML = '<div class="empty-state"><h2>' + esc(e.message) + '</h2></div>';
  }
}
function renderDetailEps(line) {
  const d = S.detail;
  const eps = (d.lines[line] || []);
  $('detailEpCount').textContent = eps.length + ' 集';
  $('detailEps').innerHTML = eps.map((e, i) =>
    `<button class="ep-btn ${i === 0 ? 'active' : ''}" data-ep="${i}">${esc(e.name)}</button>`).join('');
  $('detailEps').querySelectorAll('.ep-btn').forEach((b) => {
    b.onclick = () => startPlay(d.id, $('detailLine').value, Number(b.dataset.ep));
  });
}

/* ------------------------------------------------------------ 播放器 */
function destroyHls() {
  if (S.hls) { try { S.hls.destroy(); } catch (_) {} S.hls = null; }
}
function stopPlayback() {
  const v = $('video');
  reportProgress(true);
  if (S.danmu) S.danmu.clear();
  try { v.pause(); } catch (_) {}
  destroyHls();
  v.removeAttribute('src');
  try { v.load(); } catch (_) {}
}
function overlay(text) {
  if (text) { $('overlayText').textContent = text; $('playerOverlay').hidden = false; }
  else $('playerOverlay').hidden = true;
}
function playerError(text) {
  const box = $('playerError');
  box.hidden = false; box.innerHTML = esc(text);
}
async function startPlay(id, line, ep, isSwitch) {
  // 切集 / 换线前保存上一集的进度（播放器视图内才有效）
  if (S.view === 'player' && S.info) reportProgress(true);
  if (!isSwitch) { S.switchTried = 0; S.autoLineActive = false; }
  showView('player');
  overlay('正在解析播放地址…');
  $('playerError').hidden = true;
  $('resumeToast').hidden = true;
  let info;
  try {
    info = await api('/api/play', { method: 'POST', body: { id, line, ep } });
    if (info.error) throw new Error(info.error);
  } catch (e) {
    overlay('播放失败：' + e.message);
    playerError('播放失败：' + e.message + '\n可在右侧切换线路，或到设置中检查接口可用性。');
    return;
  }
  S.info = info;
  S.lastReport = 0;
  $('playerTitle').textContent = info.title;
  $('playerEpisode').textContent = info.episode;
  $('lineSelect').innerHTML = info.lines.map((l) =>
    `<option value="${esc(l)}" ${l === info.line ? 'selected' : ''}>${esc(l)}</option>`).join('');
  $('epCount').textContent = info.episodes.length + ' 集';
  $('episodeGrid').innerHTML = info.episodes.map((e) =>
    `<button class="ep-btn ${e.index === info.ep_index ? 'active' : ''}" data-ep="${e.index}" title="${esc(e.name)}">${esc(e.name)}</button>`).join('');
  $('episodeGrid').querySelectorAll('.ep-btn').forEach((b) => {
    b.onclick = () => startPlay(info.id, $('lineSelect').value, Number(b.dataset.ep));
  });
  attachStream(info);
  autoLoadDanmu(info);
}

function attachStream(info) {
  const v = $('video');
  destroyHls();
  v.removeAttribute('src');
  try { v.load(); } catch (_) {}
  const onReady = () => {
    overlay(null);
    if (info.resume > 3 && isFinite(v.duration) && info.resume < v.duration - 12) {
      v.currentTime = info.resume;
      const t = $('resumeToast');
      t.textContent = '已从 ' + fmtTime(info.resume) + ' 继续播放';
      t.hidden = false;
      setTimeout(() => { t.hidden = true; }, 4200);
    }
    v.play().catch(() => { toast('点击画面开始播放'); });
  };
  if (info.kind === 'hls' && window.Hls && Hls.isSupported()) {
    S.hls = new Hls({
      enableWorker: true, lowLatencyMode: true, maxBufferLength: 30,
      maxMaxBufferLength: 120, backBufferLength: 30,
      fragLoadingTimeOut: 20000, manifestLoadingTimeOut: 20000,
    });
    S.hls.on(Hls.Events.MANIFEST_PARSED, onReady);
    S.hls.on(Hls.Events.ERROR, (evt, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        overlay('网络错误，正在重试…');
        S.hls.startLoad();
        return;
      }
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR && S.switchTried === 0) {
        S.switchTried++;
        try { S.hls.recoverMediaError(); return; } catch (_) {}
      }
      handleFatalError('播放出错：' + (data.details || '未知错误'), info);
    });
    S.hls.loadSource(info.url);
    S.hls.attachMedia(v);
  } else if (info.kind === 'hls') {
    // 支持原生 HLS 的内核（Safari 等）
    v.src = info.url;
    v.addEventListener('loadedmetadata', onReady, { once: true });
  } else {
    v.src = info.url;
    v.addEventListener('loadedmetadata', onReady, { once: true });
    v.addEventListener('error', () => handleFatalError('无法加载该媒体地址', info), { once: true });
  }
}
function handleFatalError(msg, info) {
  const auto = S.settings.auto_switch_line !== false && S.switchTried < 3;
  if (auto && info.lines && info.lines.length > 1) {
    const idx = info.lines.indexOf(info.line);
    const next = info.lines[(idx + 1) % info.lines.length];
    S.switchTried++;
    playerError(msg + '\n正在自动切换到：' + next);
    toast('自动切换线路');
    setTimeout(() => startPlay(info.id, next, info.ep_index, true), 700);
  } else {
    overlay(msg);
    playerError(msg + '\n请手动切换线路重试。');
  }
}

function reportProgress(force) {
  const info = S.info, v = $('video');
  if (!info || !isFinite(v.currentTime) || v.currentTime <= 0) return;
  const now = Date.now();
  if (!force && now - S.lastReport < 5000) return;
  S.lastReport = now;
  api('/api/history', {
    method: 'POST',
    body: {
      id: info.id, title: info.title, poster: info.poster || '',
      line: info.line, episode: info.episode,
      ts: v.currentTime, duration: isFinite(v.duration) ? v.duration : 0,
    },
  }).catch(() => {});
}
function togglePlay() {
  const v = $('video');
  if (v.paused) v.play().catch(() => {}); else v.pause();
}
function seekBy(sec) {
  const v = $('video');
  if (!isFinite(v.duration)) return;
  v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + sec));
}

/* ------------------------------------------------------------ 历史 */
async function loadHistory() {
  showView('history');
  $('historyList').innerHTML = '<div class="loading"><span class="spinner"></span> 加载中…</div>';
  try {
    const r = await api('/api/history');
    const items = r.items || [];
    $('historyList').innerHTML = items.length ? items.map((h) => {
      const pct = h.duration > 0 ? Math.min(100, Math.round(h.ts / h.duration * 100)) : 0;
      return `<div class="history-card" data-title="${esc(h.title)}" data-id="${esc(h.id)}" data-line="${esc(h.line)}" data-ep="${esc(h.episode)}">
        <img loading="lazy" src="${posterUrl(h.poster)}" alt="">
        <div class="history-info">
          <h3>${esc(h.title)}</h3>
          <div class="muted">${esc(h.episode || '正片')}</div>
          <div class="history-bar"><i style="width:${pct}%"></i></div>
          <div class="muted">${fmtTime(h.ts)} / ${fmtTime(h.duration)} · ${pct}%</div>
        </div>
        <button class="del-btn" data-del="${esc(h.id)}" title="删除记录">✕</button>
      </div>`;
    }).join('') : '<div class="empty-state"><h2>暂无播放记录</h2><p>看过的影片会出现在这里，进度保存在本机。</p></div>';
    $('historyList').querySelectorAll('.history-card').forEach((c) => {
      c.onclick = async (e) => {
        if (e.target.dataset.del) {
          await api('/api/history/' + encodeURIComponent(e.target.dataset.del), { method: 'DELETE' });
          loadHistory(); return;
        }
        await resumeFromHistory(c.dataset);
      };
    });
  } catch (e) { $('historyList').innerHTML = '<div class="empty-state"><h2>' + esc(e.message) + '</h2></div>'; }
}
async function resumeFromHistory(d) {
  toast('正在重新定位片源…');
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(d.title));
    const hit = (r.items || []).find((x) => x.title === d.title) || (r.items || [])[0];
    if (!hit) { toast('未找到该片源，请手动搜索'); return; }
    const detail = await api('/api/title/' + hit.id);
    let line = Object.keys(detail.lines).includes(d.line) ? d.line : Object.keys(detail.lines)[0];
    const eps = detail.lines[line] || [];
    let idx = eps.findIndex((e) => e.name === d.ep);
    if (idx < 0) idx = 0;
    startPlay(hit.id, line, idx);
  } catch (e) { toast('恢复失败：' + e.message); }
}

/* ------------------------------------------------------------ 弹幕 */
function danmuOpts() {
  const st = S.settings || {};
  const num = (v, d) => (v === undefined || v === null || v === '' ? d : Number(v) || d);
  return {
    enabled: st.danmu_enabled !== false && !!String(st.danmu_api || '').trim(),
    opacity: num(st.danmu_opacity, 0.85),
    fontSize: num(st.danmu_font_size, 24),
    area: num(st.danmu_area, 0.75),
    speed: num(st.danmu_speed, 8),
    offset: num(st.danmu_offset, 0),
    bold: !!st.danmu_bold,
  };
}

function initDanmu() {
  if (S.danmu || typeof DanmuEngine === 'undefined') return;
  S.danmu = new DanmuEngine($('danmuCanvas'), $('video'), danmuOpts());
  syncDanmuPanel();
}

function dpStatus(text, cls) {
  const e = $('dpStatus');
  if (!e) return;
  e.textContent = text;
  e.className = 'dp-status' + (cls ? ' ' + cls : '');
}

let _danmuSaveTimer = 0, _danmuPending = {};
function saveDanmuPrefs(patch) {
  Object.assign(S.settings, patch);
  Object.assign(_danmuPending, patch);
  clearTimeout(_danmuSaveTimer);
  _danmuSaveTimer = setTimeout(async () => {
    const p = _danmuPending; _danmuPending = {};
    try {
      const r = await api('/api/settings', { method: 'POST', body: p });
      S.settings = r.settings;
    } catch (_) {}
  }, 420);
}

function syncDanmuPanel() {
  const o = danmuOpts();
  const set = (id, val, text) => {
    $(id).value = val;
    $(id + 'Val').textContent = text;
  };
  set('dpOpacity', o.opacity, Math.round(o.opacity * 100) + '%');
  set('dpFont', o.fontSize, o.fontSize + 'px');
  set('dpArea', o.area, Math.round(o.area * 100) + '%');
  set('dpSpeed', o.speed, o.speed + 's');
  set('dpOffset', o.offset, (o.offset > 0 ? '+' : '') + o.offset + 's');
  $('btnDanmu').classList.toggle('on', o.enabled);
}

async function loadDanmuComment(episodeId, label) {
  if (episodeId === undefined || episodeId === null || episodeId === '') return;
  dpStatus('正在获取弹幕…');
  try {
    const r = await api('/api/danmu/comment/' + encodeURIComponent(episodeId));
    if (!r.ok) throw new Error(r.error || '获取失败');
    initDanmu();
    S.danmu.load(r.comments || []);
    S.danmuMeta = { episodeId, label };
    dpStatus('已加载 ' + r.count + ' 条弹幕' + (label ? ' · ' + label : ''), 'ok');
    $('btnDanmu').classList.toggle('on', S.danmu.enabled);
  } catch (e) { dpStatus('弹幕获取失败：' + e.message, 'bad'); }
}

function renderAnimeList(items) {
  const box = $('dpAnimes');
  box.hidden = false;
  box.innerHTML = items.length ? items.map((a) =>
    `<button class="dp-item" data-anime="${esc(a.animeId)}" data-title="${esc(a.title)}">
       ${esc(a.title)}<small>${esc(a.typeDescription || a.type || a.source || '')} · ${esc(a.episodeCount || 0)} 集</small>
     </button>`).join('')
    : '<div class="dp-status">没有匹配结果，换个关键词试试</div>';
  box.querySelectorAll('[data-anime]').forEach((b) => {
    b.onclick = () => openDanmuEpisodes(b.dataset.anime, b.dataset.title);
  });
}

async function openDanmuEpisodes(animeId, title) {
  dpStatus('正在读取剧集…');
  try {
    const r = await api('/api/danmu/bangumi/' + encodeURIComponent(animeId));
    if (!r.ok) throw new Error(r.error || '读取失败');
    const box = $('dpEpisodes');
    box.hidden = false;
    box.innerHTML = (r.episodes || []).length ? r.episodes.map((e, i) =>
      `<button class="dp-item" data-ep="${esc(e.episodeId)}" data-name="${esc(e.title)}">
         ${esc(e.title || ('第 ' + (i + 1) + ' 集'))}
       </button>`).join('')
      : '<div class="dp-status">该条目没有可选剧集</div>';
    dpStatus((r.title || title) + ' · ' + (r.episodes || []).length + ' 集，请选择', 'ok');
    box.querySelectorAll('[data-ep]').forEach((b) => {
      b.onclick = async () => {
        box.querySelectorAll('.dp-item').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        await loadDanmuComment(b.dataset.ep, (r.title || title) + ' ' + b.dataset.name);
      };
    });
  } catch (e) { dpStatus('剧集读取失败：' + e.message, 'bad'); }
}

async function autoLoadDanmu(info) {
  initDanmu();
  if (S.danmu) S.danmu.clear();
  const st = S.settings || {};
  if (!String(st.danmu_api || '').trim()) {
    dpStatus('未配置弹幕服务：请在「设置 → 弹幕」填写地址');
    return;
  }
  if (st.danmu_enabled === false) { dpStatus('弹幕已关闭，可在控制条开启'); return; }
  if (!st.danmu_auto) { dpStatus('自动匹配已关闭，可在弹幕面板手动搜索'); return; }
  const multi = (info.episodes || []).length > 1;
  const ep = (info.ep_index || 0) + 1;
  const fileName = multi
    ? info.title + ' S01E' + String(ep).padStart(2, '0')
    : info.title;
  dpStatus('正在自动匹配：' + fileName);
  try {
    const m = await api('/api/danmu/match', { method: 'POST', body: { fileName } });
    if (m.ok && m.matched && (m.items || []).length) {
      const hit = m.items[0];
      await loadDanmuComment(hit.episodeId,
        [hit.title, hit.episodeTitle].filter(Boolean).join(' '));
      return;
    }
    // match 未命中时回退到「剧名搜索 → 首个条目 → 对应集」
    const s = await api('/api/danmu/search?keyword=' + encodeURIComponent(info.title));
    if (!s.ok) throw new Error(s.error || '搜索失败');
    const items = s.items || [];
    renderAnimeList(items);
    if (!items.length) { dpStatus('未找到匹配弹幕，可手动搜索', 'bad'); return; }
    const b = await api('/api/danmu/bangumi/' + encodeURIComponent(items[0].animeId));
    if (!b.ok) throw new Error(b.error || '读取失败');
    const eps = b.episodes || [];
    if (!eps.length) { dpStatus('该条目没有可选剧集', 'bad'); return; }
    const target = eps[Math.min(info.ep_index || 0, eps.length - 1)];
    await loadDanmuComment(target.episodeId,
      [items[0].title, target.title].filter(Boolean).join(' '));
  } catch (e) { dpStatus('弹幕匹配失败：' + e.message, 'bad'); }
}

function toggleDanmu() {
  initDanmu();
  if (!S.danmu) return;
  const on = !S.danmu.enabled;
  S.danmu.setEnabled(on);
  $('btnDanmu').classList.toggle('on', on);
  saveDanmuPrefs({ danmu_enabled: on });
  toast(on ? '弹幕已开启' : '弹幕已关闭');
}

/* ------------------------------------------- 本地弹幕服务（便携版） */
function renderServiceStatus(r, busy) {
  const badge = $('svcBadge');
  badge.className = 'svc-badge';
  if (busy) { badge.textContent = '处理中…'; return; }
  if (!r.available) { badge.textContent = '组件缺失'; badge.classList.add('err'); }
  else if (r.error) { badge.textContent = '启动失败'; badge.classList.add('err'); }
  else if (r.running) { badge.textContent = '运行中'; badge.classList.add('on'); }
  else badge.textContent = '已停止';
  const parts = [];
  if (!r.available) parts.push(r.reason || '缺少独立工作区');
  else if (r.running) parts.push(`服务运行中 · ${r.base} · PID ${r.pid || '外部进程'}`);
  else parts.push('服务未运行');
  if (r.error) parts.push(r.error);
  $('svcStatusText').textContent = parts.join('　|　');
  $('svcLog').textContent = r.log_tail || '（暂无日志）';
  $('setDanmuService').checked = !!r.enabled;
}
let svcPoll = 0;
async function refreshDanmuService() {
  try {
    const r = await api('/api/danmu/service');
    renderServiceStatus(r);
    // 播放器刚启动、服务还在拉起时，短暂轮询几秒把状态刷新到「运行中」
    if (r.enabled && r.available && !r.running && !r.error && svcPoll < 5) {
      svcPoll++;
      setTimeout(refreshDanmuService, 1200);
    } else svcPoll = 0;
  } catch (e) {
    $('svcStatusText').textContent = '状态读取失败：' + e.message;
  }
}
async function callDanmuService(enabled, restart) {
  const payload = {
    enabled,
    danmu_service_port: Number($('setDanmuPort').value) || 9321,
    danmu_service_token: $('setDanmuToken').value.trim() || '87654321',
  };
  renderServiceStatus({}, true);
  $('svcStatusText').textContent = enabled
    ? '正在启动本地弹幕服务（首次约需数秒）…' : '正在停止本地弹幕服务…';
  try {
    if (restart) {
      await api('/api/danmu/service', { method: 'POST', body: { enabled: false } });
    }
    const r = await api('/api/danmu/service', { method: 'POST', body: payload });
    renderServiceStatus(r);
    if (enabled && r.ok) {
      $('setDanmuApi').value = r.base;
      S.settings = S.settings || {};
      S.settings.danmu_api = r.base;
      toast('本地弹幕服务已启动');
    } else if (enabled) {
      toast('启动失败：' + (r.error || '未知错误'));
    } else {
      if ($('setDanmuApi').value.startsWith('http://127.0.0.1:')) $('setDanmuApi').value = '';
      toast('本地弹幕服务已停止');
    }
  } catch (e) {
    $('svcStatusText').textContent = '操作失败：' + e.message;
    toast('操作失败：' + e.message);
    refreshDanmuService();
  }
}

/* ------------------------------------------------------ 局域网访问 */
let lanState = null;

function genLanKey() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  const buf = new Uint8Array(8);
  (window.crypto || window.msCrypto).getRandomValues(buf);
  return Array.from(buf, (b) => chars[b % chars.length]).join('');
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
    toast('已复制访问地址');
  } catch (_) { toast('复制失败，请手动选择地址'); }
}

function setRemoteReadOnly(on) {
  $('lanRemoteHint').hidden = !on;
  document.querySelectorAll('#view-settings .settings-cols, ' +
    '#view-settings .danmu-settings-panel, ' +
    '#view-settings .lan-settings-panel .svc-box').forEach((el) => {
    el.classList.toggle('readonly', on);
  });
  document.querySelectorAll('#view-settings input, #view-settings textarea, ' +
    '#view-settings button').forEach((el) => { el.disabled = !!on; });
}

function renderLan(r) {
  lanState = r;
  const badge = $('lanBadge');
  badge.className = 'svc-badge';
  if (r.active) { badge.textContent = '已开放局域网'; badge.classList.add('on'); }
  else if (r.error) { badge.textContent = '绑定异常'; badge.classList.add('err'); }
  else badge.textContent = '仅本机';
  $('setLanEnabled').checked = !!r.enabled;
  $('setLanPort').value = r.configured_port || 18790;
  $('setLanKey').value = r.password || '';

  const parts = [];
  if (r.active) parts.push(`正在监听 ${r.bind}:${r.port}`);
  else if (r.enabled) parts.push('配置已开启，等待应用后生效');
  else parts.push('仅监听 127.0.0.1，局域网设备无法访问');
  if (r.need_restart) parts.push('设置与实际状态不一致，请点「应用并重启」');
  if (r.error) parts.push(r.error);
  if (r.enabled && !r.password_set) parts.push('⚠ 未设置访问码：同网段任何设备都能打开');
  if (!r.local) parts.push('局域网设备：管理功能不可用');
  $('lanStatusText').textContent = parts.join('　|　');

  const box = $('lanAccessBox');
  const list = $('lanUrls');
  const qr = $('lanQr');
  if (r.active && r.local && (r.urls || []).length) {
    box.hidden = false;
    list.innerHTML = r.urls.map((u) =>
      `<div class="lan-url"><code>${esc(u.url)}</code>` +
      `<span class="muted">${esc(u.ip)}</span>` +
      `<button class="ghost-btn" data-copy="${esc(u.url)}">复制</button></div>`).join('');
    list.querySelectorAll('[data-copy]').forEach((b) => {
      b.onclick = () => copyText(b.dataset.copy);
    });
    qr.style.display = '';
    qr.onerror = () => {
      qr.style.display = 'none';
      $('lanQrTip').textContent = '二维码组件不可用，请手动输入地址';
    };
    qr.src = '/api/lan/qr?i=0&t=' + Date.now();
    $('lanQrTip').textContent = '扫码直接打开（含访问码）';
  } else if (r.active && !r.local) {
    box.hidden = false;
    list.innerHTML = '<p class="hint">当前设备已连上，直接用浏览器访问本页即可。</p>';
    qr.style.display = 'none';
    $('lanQrTip').textContent = '';
  } else {
    box.hidden = true;
  }
}

async function refreshLan() {
  try { renderLan(await api('/api/lan')); }
  catch (e) { $('lanStatusText').textContent = '状态读取失败：' + e.message; }
}

function probePortImage(host, port) {
  // 跨端口 fetch 会被 CORS 挡住；图片加载不受此限制，可用来探测新端口
  return new Promise((resolve) => {
    const img = new Image();
    let settled = false;
    const done = (ok) => { if (!settled) { settled = true; resolve(ok); } };
    const timer = setTimeout(() => done(false), 1600);
    img.onload = () => { clearTimeout(timer); done(true); };
    img.onerror = () => { clearTimeout(timer); done(false); };
    img.src = `http://${host}:${port}/favicon.ico?probe=${Date.now()}`;
  });
}

async function waitAndReload(port, oldEpoch) {
  const host = location.hostname || '127.0.0.1';
  const current = Number(location.port) || port;
  // 先给旧服务一点时间退出，避免把重启前的响应当成新实例
  await new Promise((r) => setTimeout(r, 900));
  for (let n = 0; n < 45; n++) {
    try {
      const res = await fetch(`http://${host}:${current}/api/health`,
        { cache: 'no-store' });
      if (res.ok) {
        const j = await res.json();
        if (!j.epoch || j.epoch > oldEpoch) {
          location.replace(`http://${host}:${current}/?view=settings`);
          return;
        }
      }
    } catch (_) { /* 服务重启中 */ }
    // 改了端口（或端口被占用顺延）时，探测候选端口
    if (n >= 2) {
      const cand = [];
      for (let p = Number(port); p < Number(port) + 6; p++) {
        if (p !== current) cand.push(p);
      }
      const hits = await Promise.all(cand.map(async (p) =>
        (await probePortImage(host, p)) ? p : 0));
      const hit = hits.find(Boolean);
      if (hit) {
        location.replace(`http://${host}:${hit}/?view=settings`);
        return;
      }
    }
    await new Promise((r) => setTimeout(r, 350));
  }
  $('lanStatusText').textContent = '服务重启超时，请查看 data/launch.log 或手动重启 FnPlayer';
  $('lanApplyBtn').disabled = false;
}

async function applyLan() {
  if (lanState && lanState.local === false) {
    toast('请在 FnPlayer 主机上修改局域网设置');
    return;
  }
  const enabled = $('setLanEnabled').checked;
  const port = Number($('setLanPort').value) || 18790;
  let key = $('setLanKey').value.trim();
  if (enabled && !key) { key = genLanKey(); $('setLanKey').value = key; }
  if (!confirm('应用后服务会短暂重启，当前播放会中断。确定继续？')) return;
  let epoch = 0;
  try { epoch = (await api('/api/health')).epoch || 0; } catch (_) {}
  $('lanStatusText').textContent = '正在保存并重新绑定监听地址…';
  $('lanApplyBtn').disabled = true;
  try {
    const r = await api('/api/lan/apply', {
      method: 'POST', body: { enabled, port, password: key },
    });
    await waitAndReload(r.port, epoch);
  } catch (e) {
    $('lanApplyBtn').disabled = false;
    $('lanStatusText').textContent = '应用失败：' + e.message;
    toast('应用失败：' + e.message);
  }
}

/* ------------------------------------------------------------ 设置 */
async function loadSettings() {
  showView('settings');
  try {
    const lanInfo = await api('/api/lan');
    renderLan(lanInfo);
    if (!lanInfo.local) {
      // 局域网设备：管理接口一律被服务端拒绝，这里直接切成只读视图
      setRemoteReadOnly(true);
      $('lanRemoteHint').hidden = false;
      $('lanStatusText').textContent = '当前由局域网设备打开，管理功能仅主机可用。';
      return;
    }
    const [st, src] = await Promise.all([api('/api/settings'), api('/api/sources')]);
    S.settings = st; S.sources = src.items || [];
    $('setResume').checked = !!st.auto_resume;
    $('setNext').checked = !!st.auto_next;
    $('setAutoLine').checked = st.auto_switch_line !== false;
    $('setTimeout').value = st.search_timeout || 9;
    $('setLimit').value = st.max_per_keyword || 24;
    $('setExternal').value = st.external_player || '';
    $('setProxy').value = st.source_proxy || '';
    $('setDanmuApi').value = st.danmu_api || '';
    $('setDanmuOn').checked = st.danmu_enabled !== false;
    $('setDanmuAuto').checked = st.danmu_auto !== false;
    $('setDanmuService').checked = !!st.danmu_service_enabled;
    $('setDanmuPort').value = st.danmu_service_port || 9321;
    $('setDanmuToken').value = st.danmu_service_token || '87654321';
    $('junkWords').value = (st.junk_keywords || []).join('、');
    renderSources(S.sources);
    refreshDanmuService();
  } catch (e) { toast('设置加载失败：' + e.message); }
}
function renderSources(items) {
  S.sources = items;
  $('srcTable').querySelector('tbody').innerHTML = items.length ? items.map((s) =>
    `<tr data-id="${esc(s.id)}">
      <td>${esc(s.name)}</td>
      <td class="api-cell" title="${esc(s.api)}">${esc(s.api)}</td>
      <td><input type="checkbox" class="switch" data-toggle="${esc(s.id)}" ${s.enabled ? 'checked' : ''}></td>
      <td class="src-status"><span class="status-dot"></span><span class="muted">未测</span></td>
      <td><button class="del-btn" data-remove="${esc(s.id)}" title="删除">✕</button></td>
    </tr>`).join('')
    : '<tr><td colspan="5" class="muted">尚未添加接口。添加后即可搜索与播放。</td></tr>';
  $('srcTable').querySelectorAll('[data-toggle]').forEach((c) => {
    c.onchange = async () => {
      await api('/api/sources/' + encodeURIComponent(c.dataset.toggle),
        { method: 'PATCH', body: { enabled: c.checked } });
      const r = await api('/api/sources'); renderSources(r.items || []);
    };
  });
  $('srcTable').querySelectorAll('[data-remove]').forEach((b) => {
    b.onclick = async () => {
      await api('/api/sources/' + encodeURIComponent(b.dataset.remove), { method: 'DELETE' });
      const r = await api('/api/sources'); renderSources(r.items || []);
    };
  });
}

/* ------------------------------------------------------------ 事件绑定 */
function bind() {
  document.querySelectorAll('.nav-btn').forEach((b) => {
    b.onclick = () => {
      if (b.dataset.view === 'discover') loadDiscover(1, false);
      else if (b.dataset.view === 'history') loadHistory();
      else loadSettings();
    };
  });
  $('brandHome').onclick = () => loadDiscover(1, false);
  $('searchBtn').onclick = doSearch;
  $('searchInput').onkeydown = (e) => { if (e.key === 'Enter') doSearch(); };
  $('themeBtn').onclick = () => setTheme(S.theme === 'dark' ? 'light' : 'dark');
  $('goSettings').onclick = loadSettings;
  $('loadMore').onclick = () => loadDiscover(S.page + 1, true);
  $('posterGrid').onclick = (e) => {
    const c = e.target.closest('.card');
    if (c) openDetail(c.dataset.id);
  };
  $('detailBack').onclick = () => { showView('discover'); };
  $('playerBack').onclick = () => { if (S.detail) openDetail(S.detail.id); else showView('discover'); };

  // 播放器控件
  $('btnPlay').onclick = togglePlay;
  $('video').onclick = togglePlay;
  $('btnPrev').onclick = () => { if (S.info) startPlay(S.info.id, $('lineSelect').value, Math.max(0, S.info.ep_index - 1)); };
  $('btnNext').onclick = () => { if (S.info) startPlay(S.info.id, $('lineSelect').value, S.info.ep_index + 1); };
  $('btnMute').onclick = () => { const v = $('video'); v.muted = !v.muted; $('btnMute').textContent = v.muted ? '🔇' : '🔊'; };
  $('volume').oninput = (e) => { const v = $('video'); v.volume = Number(e.target.value); v.muted = false; };
  $('rate').onchange = (e) => { $('video').playbackRate = Number(e.target.value); };
  $('btnPip').onclick = async () => {
    const v = $('video');
    try { if (document.pictureInPictureElement) await document.exitPictureInPicture(); else await v.requestPictureInPicture(); }
    catch (_) { toast('当前环境不支持画中画'); }
  };
  $('btnExternal').onclick = async () => {
    if (!S.info) return;
    try {
      const r = await api('/api/open-external', { method: 'POST', body: { url: S.info.url } });
      if (!r.ok) toast(r.message || '打开失败');
    } catch (e) { toast(e.message); }
  };
  $('btnFull').onclick = () => {
    const el = $('videoWrap');
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen && el.requestFullscreen();
  };
  $('lineSelect').onchange = () => {
    if (S.info) startPlay(S.info.id, $('lineSelect').value, S.info.ep_index);
  };
  const progress = $('progress');
  const doSeek = (e) => {
    const v = $('video');
    if (!isFinite(v.duration) || !v.duration) return;
    const r = progress.getBoundingClientRect();
    v.currentTime = Math.max(0, Math.min(v.duration, (e.clientX - r.left) / r.width * v.duration));
  };
  progress.onclick = doSeek;
  progress.addEventListener('pointerdown', (e) => {
    progress.setPointerCapture(e.pointerId);
    const move = (ev) => doSeek(ev);
    const up = () => { progress.removeEventListener('pointermove', move); };
    progress.addEventListener('pointermove', move);
    progress.addEventListener('pointerup', up, { once: true });
  });
  const v = $('video');
  v.addEventListener('play', () => { $('btnPlay').textContent = '❚❚'; });
  v.addEventListener('pause', () => { $('btnPlay').textContent = '▶'; reportProgress(true); });
  v.addEventListener('timeupdate', () => {
    const d = v.duration || 0, c = v.currentTime || 0;
    $('played').style.width = (d ? c / d * 100 : 0) + '%';
    $('thumb').style.left = (d ? c / d * 100 : 0) + '%';
    $('timeLabel').textContent = fmtTime(c) + ' / ' + fmtTime(d);
    if (v.buffered.length && d) {
      $('buffered').style.width = (v.buffered.end(v.buffered.length - 1) / d * 100) + '%';
    }
    reportProgress(false);
  });
  v.addEventListener('ended', () => {
    reportProgress(true);
    if (S.settings.auto_next !== false && S.info &&
        S.info.ep_index + 1 < S.info.episodes.length) {
      toast('播放下一集');
      startPlay(S.info.id, $('lineSelect').value, S.info.ep_index + 1);
    }
  });
  document.addEventListener('keydown', (e) => {
    if (S.view !== 'player') return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag)) return;
    if (e.code === 'Space' || e.key.toLowerCase() === 'k') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); seekBy(-5); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); seekBy(5); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); v.volume = Math.min(1, v.volume + .05); $('volume').value = v.volume; }
    else if (e.key === 'ArrowDown') { e.preventDefault(); v.volume = Math.max(0, v.volume - .05); $('volume').value = v.volume; }
    else if (e.key.toLowerCase() === 'f') $('btnFull').click();
    else if (e.key.toLowerCase() === 'm') $('btnMute').click();
    else if (e.key.toLowerCase() === 'd') { e.preventDefault(); toggleDanmu(); }
    else if (e.key === 'Escape' && document.fullscreenElement) document.exitFullscreen();
  });

  // 弹幕
  $('btnDanmu').onclick = () => {
    if (!String((S.settings || {}).danmu_api || '').trim()) {
      $('danmuPanel').hidden = false;
      dpStatus('未配置弹幕服务：请在「设置 → 弹幕」填写地址', 'bad');
      return;
    }
    toggleDanmu();
  };
  $('btnDanmuPanel').onclick = () => {
    const p = $('danmuPanel');
    p.hidden = !p.hidden;
    if (!p.hidden) {
      syncDanmuPanel();
      if (S.info && !$('dpKeyword').value) $('dpKeyword').value = S.info.title || '';
    }
  };
  $('dpClose').onclick = () => { $('danmuPanel').hidden = true; };
  $('dpSearch').onclick = async () => {
    const kw = $('dpKeyword').value.trim();
    if (!kw) { toast('请输入剧名关键词'); return; }
    dpStatus('正在搜索：' + kw);
    $('dpEpisodes').hidden = true;
    try {
      const r = await api('/api/danmu/search?keyword=' + encodeURIComponent(kw));
      if (!r.ok) throw new Error(r.error || '搜索失败');
      renderAnimeList(r.items || []);
      dpStatus('找到 ' + (r.items || []).length + ' 个条目，点选后可选集', 'ok');
    } catch (e) { dpStatus('搜索失败：' + e.message, 'bad'); }
  };
  $('dpKeyword').onkeydown = (e) => { if (e.key === 'Enter') $('dpSearch').click(); };
  const slider = (id, key, fmt, transform) => {
    $(id).oninput = () => {
      const val = Number($(id).value);
      $(id + 'Val').textContent = fmt(val);
      initDanmu();
      const patch = {}; patch[key] = val;
      S.danmu.setOptions(transform(val));
      saveDanmuPrefs(patch);
      if (key === 'danmu_offset') S.danmu.reset();
    };
  };
  slider('dpOpacity', 'danmu_opacity', (v) => Math.round(v * 100) + '%', (v) => ({ opacity: v }));
  slider('dpFont', 'danmu_font_size', (v) => v + 'px', (v) => ({ fontSize: v }));
  slider('dpArea', 'danmu_area', (v) => Math.round(v * 100) + '%', (v) => ({ area: v }));
  slider('dpSpeed', 'danmu_speed', (v) => v + 's', (v) => ({ speed: v }));
  slider('dpOffset', 'danmu_offset', (v) => (v > 0 ? '+' : '') + v + 's', (v) => ({ offset: v }));
  $('dpSend').onclick = () => {
    const t = $('dpSendText').value.trim();
    if (!t) return;
    initDanmu();
    if (!S.danmu.enabled) {
      S.danmu.setEnabled(true);
      $('btnDanmu').classList.add('on');
      saveDanmuPrefs({ danmu_enabled: true });
    }
    S.danmu.send(t, '#ffd54a');
    $('dpSendText').value = '';
  };
  $('dpSendText').onkeydown = (e) => { if (e.key === 'Enter') $('dpSend').click(); };
  $('dpReload').onclick = () => { if (S.info) autoLoadDanmu(S.info); };
  $('dpClear').onclick = () => {
    initDanmu();
    S.danmu.clear();
    S.danmuMeta = null;
    dpStatus('已清空当前弹幕');
  };
  $('setDanmuService').onchange = (e) => callDanmuService(e.target.checked, false);
  $('lanGenKeyBtn').onclick = () => {
    $('setLanKey').value = genLanKey();
    toast('已生成访问码，点「应用并重启」后生效');
  };
  $('lanApplyBtn').onclick = applyLan;
  $('svcRestartBtn').onclick = () => {
    $('setDanmuService').checked = true;
    callDanmuService(true, true);
  };
  $('probeDanmuBtn').onclick = async () => {
    const v = $('setDanmuApi').value.trim();
    if (!v) { $('danmuProbeMsg').textContent = '请先填写地址'; return; }
    $('danmuProbeMsg').textContent = '正在测试…';
    try {
      const r = await api('/api/danmu/probe', { method: 'POST', body: { api: v } });
      $('danmuProbeMsg').textContent = r.ok
        ? '连接正常（HTTP ' + r.status + '）'
        : (r.error || '连接失败');
    } catch (e) { $('danmuProbeMsg').textContent = e.message; }
  };

  // 设置
  $('addSrcBtn').onclick = async () => {
    const name = $('srcName').value.trim(), apiUrl = $('srcApi').value.trim();
    if (!apiUrl) { toast('请填写接口地址'); return; }
    try {
      const r = await api('/api/sources', { method: 'POST', body: { name, api: apiUrl } });
      if (!r.ok) { toast(r.message || '添加失败'); return; }
      $('srcName').value = ''; $('srcApi').value = '';
      renderSources(r.items || []);
      toast('已添加接口');
    } catch (e) { toast(e.message); }
  };
  $('bulkBtn').onclick = async () => {
    const text = $('bulkText').value.trim();
    if (!text) { $('bulkMsg').textContent = '请先粘贴接口清单'; return; }
    $('bulkMsg').textContent = '正在导入…';
    try {
      const r = await api('/api/sources/bulk', { method: 'POST', body: { text } });
      if (!r.ok) { $('bulkMsg').textContent = r.message || '导入失败'; return; }
      renderSources(r.items || []);
      const bad = (r.skipped || []).length;
      $('bulkMsg').textContent = `新增 ${r.added} 条` + (bad ? `，跳过 ${bad} 条` : '');
      if (r.added) $('bulkText').value = '';
      toast(bad ? `导入完成：新增 ${r.added}，跳过 ${bad}` : `已导入 ${r.added} 条接口`);
    } catch (e) { $('bulkMsg').textContent = e.message; }
  };
  $('testAllBtn').onclick = async () => {
    $('testMsg').textContent = '正在测速…';
    try {
      const r = await api('/api/sources/test-all', { method: 'POST', body: {} });
      (r.items || []).forEach((it) => {
        const row = document.querySelector(`#srcTable tr[data-id="${CSS.escape(it.id)}"] .src-status`);
        if (row) row.innerHTML = `<span class="status-dot ${it.ok ? 'ok' : 'bad'}"></span>` +
          `<span class="muted">${esc(it.ok ? (it.elapsed_ms + 'ms · ' + it.count + ' 条') : (it.error || '不可用'))}</span>`;
      });
      $('testMsg').textContent = '测速完成';
    } catch (e) { $('testMsg').textContent = e.message; }
  };
  $('saveSettings').onclick = async () => {
    const patch = {
      auto_resume: $('setResume').checked,
      auto_next: $('setNext').checked,
      auto_switch_line: $('setAutoLine').checked,
      search_timeout: Number($('setTimeout').value) || 9,
      max_per_keyword: Number($('setLimit').value) || 24,
      external_player: $('setExternal').value.trim(),
      source_proxy: $('setProxy').value.trim(),
      source_proxy_mode: $('setProxy').value.trim() ? 'on' : 'off',
      danmu_api: $('setDanmuApi').value.trim(),
      danmu_enabled: $('setDanmuOn').checked,
      danmu_auto: $('setDanmuAuto').checked,
      theme: S.theme,
    };
    try {
      const r = await api('/api/settings', { method: 'POST', body: patch });
      S.settings = r.settings;
      if (S.danmu) { S.danmu.setOptions(danmuOpts()); syncDanmuPanel(); }
      $('settingsMsg').textContent = '已保存 ✓';
      setTimeout(() => { $('settingsMsg').textContent = ''; }, 2200);
    } catch (e) { toast(e.message); }
  };
  $('saveJunk').onclick = async () => {
    const r = await api('/api/junk-keywords', { method: 'POST', body: { keywords: $('junkWords').value } });
    $('junkWords').value = (r.items || []).join('、'); toast('过滤词已保存');
  };
  $('resetJunk').onclick = async () => {
    const r = await api('/api/junk-keywords', { method: 'POST', body: { reset: true } });
    $('junkWords').value = (r.items || []).join('、'); toast('已恢复默认');
  };
  $('exitApp').onclick = async () => {
    if (!confirm('确定要退出 FnPlayer 吗？')) return;
    try {
      await fetch('/api/shutdown', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Fn-Token': TOKEN },
        body: '{}',
      });
    } catch (_) {}
    toast('正在退出…');
    setTimeout(() => { try { window.close(); } catch (_) {} }, 600);
  };
  // 不在 beforeunload 主动退出：按 F5 刷新页面不应关闭整个应用。
  // 关闭窗口由桌面壳检测浏览器进程退出后收尾，也可在设置页点「退出应用」。
}

/* ------------------------------------------------------------ 启动 */
(async function init() {
  setTheme(localStorage.getItem('fn-theme') || 'dark');
  bind();
  try {
    const r = await api('/api/bootstrap');
    S.settings = r.settings || {};
    setTheme(S.settings.theme || S.theme);
    if (r.config_file) $('dataDirHint').textContent = r.config_file;
    initDanmu();
    if (S.danmu) { S.danmu.setOptions(danmuOpts()); syncDanmuPanel(); }
    const s = await api('/api/sources');
    S.sources = s.items || [];
    const params = new URLSearchParams(location.search);
    const q = params.get('q');
    const v = params.get('view');
    if (v === 'settings') loadSettings();
    else if (v === 'history') loadHistory();
    else if (q) { $('searchInput').value = q; doSearch(); }
    else if (!r.has_sources) showEmpty();
    else loadDiscover(1, false);
  } catch (e) {
    showEmpty();
    $('discoverMeta').textContent = '本地服务异常：' + e.message;
  }
})();
