/* FnPlayer Android - mobile UI. Talks only to the in-app 127.0.0.1 service. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var TOKEN = window.__FN_TOKEN__ || '';
  var state = {
    view: 'home', config: {}, sourceCount: 0, detail: null, line: '',
    play: null, hls: null, rate: 1, discoverPg: 1, history: [],
    videoBound: false, seeking: false, lastSave: 0, ctrlTimer: null,
    danmu: null, danmuMeta: null, danmuStatus: {}, dmSaveTimer: 0, dmPending: {},
    favorites: [], searchQuery: '', searchHtml: '', searchInfo: '',
    detailFrom: 'home', lastEp: -1
  };
  // 每个导航步骤记录视图与滚动位置。返回时只切换显示、不重建页面内容，
  // 因此「搜索结果 → 详情 → 播放」会一层一层退回，而不是跳回初始首页。
  var nav = [{ v: 'home', y: 0 }];
  var RATES = [0.75, 1, 1.25, 1.5, 2];

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-Fn-Token': TOKEN } };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ('HTTP ' + r.status));
        });
      }
      return r.json();
    });
  }
  function post(path, body) { return api(path, { method: 'POST', body: body || {} }); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function imgUrl(u) {
    return u ? '/api/poster?u=' + encodeURIComponent(u) + '&t=' + encodeURIComponent(TOKEN)
      : '/static/placeholder.svg';
  }
  var toastTimer = null;
  function toast(msg) {
    var t = $('toast');
    t.textContent = msg;
    t.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2200);
  }
  function fmtTime(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), q = s % 60;
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return (h ? h + ':' + pad(m) : pad(m)) + ':' + pad(q);
  }
  function emptyHtml(title, text, btn, act) {
    return '<div class="empty"><span class="big">' + esc(title) + '</span>' + esc(text) +
      (btn ? '<div class="btn-row" style="justify-content:center"><button class="btn primary" data-act="' + esc(act) + '">' + esc(btn) + '</button></div>' : '') +
      '</div>';
  }
  function cardsHtml(items, removable) {
    return (items || []).map(function (t) {
      var meta = [t.year, t.type, t.area].filter(Boolean).join(' · ') ||
        ((t.providers || []).join(' / '));
      return '<div class="card" data-act="detail" data-id="' + esc(t.id) + '">' +
        '<div class="poster"><img loading="lazy" src="' + esc(imgUrl(t.poster)) +
        '" onerror="this.src=\'/static/placeholder.svg\'">' +
        (removable ? '<button class="fav-remove" data-act="unfav" data-id="' + esc(t.id) +
          '" title="取消收藏">×</button>' : '') +
        (t.remarks ? '<span class="tag">' + esc(t.remarks) + '</span>' : '') + '</div>' +
        '<div class="name">' + esc(t.title) + '</div>' +
        '<div class="meta">' + esc(meta) + '</div></div>';
    }).join('');
  }
  function historyHtml(items) {
    return (items || []).map(function (h) {
      var pct = (h.duration > 0 && h.ts > 0) ? Math.min(100, h.ts / h.duration * 100) : 0;
      return '<div class="history-item" data-act="continue" data-id="' + esc(h.id) + '">' +
        '<div class="poster"><img src="' + esc(imgUrl(h.poster)) + '" onerror="this.src=\'/static/placeholder.svg\'"></div>' +
        '<div class="info"><b>' + esc(h.title) + '</b><small>' +
        esc([h.episode, h.line].filter(Boolean).join(' · ')) + ' · ' + fmtTime(h.ts) + '</small>' +
        '<div class="progress"><i style="width:' + pct.toFixed(1) + '%"></i></div></div></div>';
    }).join('');
  }

  /* --------------------------------------------------------------- navigation */
  function scrollTop() {
    return window.pageYOffset || document.documentElement.scrollTop || 0;
  }
  function show(view) {
    var views = document.querySelectorAll('.view');
    for (var i = 0; i < views.length; i++) {
      views[i].classList.toggle('active', views[i].id === 'view-' + view);
    }
    var full = view === 'player';
    $('topbar').style.display = full ? 'none' : '';
    $('tabbar').style.display = full ? 'none' : '';
    document.body.classList.toggle('immersive', full);
    // 搜索页/详情页不是独立 tab，高亮它们来源的频道。
    var tab = view;
    if (view === 'search') tab = 'home';
    else if (view === 'detail') tab = (state.detailFrom === 'favorites' || state.detailFrom === 'history')
      ? state.detailFrom : 'home';
    var tabs = document.querySelectorAll('.tab');
    for (var j = 0; j < tabs.length; j++) {
      tabs[j].classList.toggle('active', tabs[j].getAttribute('data-tab') === tab);
    }
    state.view = view;
    if (view === 'player' && state.danmu) setTimeout(function () { state.danmu.resize(); }, 60);
  }
  /** 压入一个导航层级，同时记下当前页的滚动位置。 */
  function go(view) {
    if (state.view === view) return;
    nav[nav.length - 1].y = scrollTop();
    nav.push({ v: view, y: 0 });
    show(view);
    window.scrollTo(0, 0);
  }
  function switchTab(name) {
    if (state.view === 'player') stopPlayback();
    closeSheet();
    closeDanmuPanel();
    nav = name === 'home' ? [{ v: 'home', y: 0 }]
      : [{ v: 'home', y: 0 }, { v: name, y: 0 }];
    show(name);
    window.scrollTo(0, 0);
    if (name === 'home') refreshHome();
    else if (name === 'history') loadHistory();
    else if (name === 'favorites') loadFavorites();
    else if (name === 'settings') loadSettings();
  }
  /** 返回时只切换显示，不重建视图 DOM，搜索结果与滚动位置原样保留。 */
  function restoreView(entry) {
    show(entry.v);
    setTimeout(function () { window.scrollTo(0, entry.y || 0); }, 0);
    if (entry.v === 'home') refreshContinueBlock();
    else if (entry.v === 'history') loadHistory();
    else if (entry.v === 'favorites') loadFavorites();
    else if (entry.v === 'search' && !$('searchBody').innerHTML.trim() && state.searchHtml) {
      $('searchBody').innerHTML = state.searchHtml;
      $('searchInfo').textContent = state.searchInfo;
    } else if (entry.v === 'detail' && state.detail) renderDetail();
  }
  function exitFullscreen() {
    var d = document;
    try {
      if (d.fullscreenElement || d.webkitFullscreenElement) {
        if (d.exitFullscreen) d.exitFullscreen();
        else if (d.webkitExitFullscreen) d.webkitExitFullscreen();
      }
    } catch (e) {}
    if (window.FnNative && FnNative.requestLandscape) FnNative.requestLandscape(false);
  }
  window.__fnBack = function () {
    // 浮层优先逐层关闭，避免按一次返回键把面板和页面一起退出。
    if ($('sheet').classList.contains('show')) { closeSheet(); return true; }
    if ($('dmPanel').classList.contains('show')) { closeDanmuPanel(); return true; }
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      exitFullscreen();
      return true;
    }
    if (state.view === 'player') stopPlayback();
    if (nav.length > 1) {
      nav.pop();
      restoreView(nav[nav.length - 1]);
      return true;
    }
    return false;
  };
  window.__fnPause = function () {
    var v = $('video');
    if (v && !v.paused) { v.pause(); saveProgress(); }
  };

  /* -------------------------------------------------------------------- home */
  function boot() {
    Promise.all([
      api('/api/bootstrap'),
      api('/api/history').catch(function () { return []; }),
      api('/api/favorites').catch(function () { return { items: [] }; })
    ]).then(function (a) {
      var b = a[0];
      state.config = b.config || {};
      state.history = a[1] || [];
      state.favorites = (a[2] && a[2].items) || [];
      state.sourceCount = b.source_count || 0;
      $('configPath').textContent = b.config_file || '—';
      updateSourceInfo();
      refreshDanmuService();
      refreshHome();
    }).catch(function (e) {
      $('homeContent').innerHTML = emptyHtml('本地服务未就绪', e.message || '请重启应用');
    });
  }
  function updateSourceInfo() {
    $('sourceInfo').textContent = state.sourceCount
      ? ('已启用 ' + state.sourceCount + ' 个播放源 · 仅本机服务')
      : '尚未添加播放源，请先到「设置」添加';
  }
  function refreshHome() {
    $('homeContent').innerHTML = '<div id="homeContinue"></div>' +
      (state.sourceCount ? '' : emptyHtml('还没有播放源',
        '在「设置 → 播放源」添加你自己的苹果CMS V10 接口，或粘贴电脑版 config.json 一键导入。',
        '去设置', 'go-settings'));
    renderContinue();
    if (state.sourceCount) loadDiscover(1, true);
  }
  function renderContinue() {
    var box = $('homeContinue');
    if (!box) return;
    box.innerHTML = state.history.length
      ? '<div class="block-title"><span>继续观看</span><span class="sub">最近 ' +
        Math.min(3, state.history.length) + ' 条</span></div>' + historyHtml(state.history.slice(0, 3))
      : '';
  }
  /** 返回首页时只更新「继续观看」，不重绘片单、不丢滚动位置。 */
  function refreshContinueBlock() {
    return api('/api/history').then(function (items) {
      state.history = items || [];
      renderContinue();
    }).catch(function () {});
  }
  function loadDiscover(pg, replace) {
    var box = $('homeContent');
    if (replace || pg === 1) {
      var title = '<div class="block-title"><span>最近更新</span><span class="sub" id="pageLabel">第 ' + pg + ' 页</span></div>';
      if (replace) box.insertAdjacentHTML('beforeend', title + '<div class="grid" id="discoverGrid"><div class="empty">正在加载片单…</div></div>');
      else box.insertAdjacentHTML('beforeend', title + '<div class="grid" id="discoverGrid"></div>');
    }
    api('/api/discover?pg=' + pg).then(function (r) {
      state.discoverPg = pg;
      var grid = $('discoverGrid');
      if (!grid) return;
      var old = grid.querySelector('.empty');
      if (old) old.remove();
      var items = r.items || [];
      grid.insertAdjacentHTML('beforeend', cardsHtml(items));
      if ($('pageLabel')) $('pageLabel').textContent = '第 ' + pg + ' 页';
      var more = $('btnMore');
      if (more) more.remove();
      box.insertAdjacentHTML('beforeend', '<div class="btn-row" id="btnMore"><button class="btn ghost" data-act="load-more">加载更多</button></div>');
      if (!items.length && pg === 1) {
        grid.innerHTML = '<div class="empty">片单为空，试试上方搜索框</div>';
      }
    }).catch(function (e) {
      var grid = $('discoverGrid');
      if (grid) grid.innerHTML = '<div class="empty">' + esc(e.message || '片单加载失败') + '</div>';
    });
  }
  function activeSearchInput() {
    return state.view === 'search' ? $('searchInput2') : $('searchInput');
  }
  /** 搜索结果进入独立的导航层级，返回时按 播放 → 详情 → 结果 → 首页 逐层退回。 */
  function doSearch() {
    var box = activeSearchInput();
    var q = box ? box.value.trim() : '';
    if (!q) { toast('请输入片名'); return; }
    if (!state.sourceCount) { toast('请先添加播放源'); switchTab('settings'); return; }
    if (box === $('searchInput2')) $('searchInput').value = q;
    else $('searchInput2').value = q;
    state.searchQuery = q;
    $('searchInfo').textContent = '正在搜索「' + q + '」…';
    $('searchBody').innerHTML = '<div class="empty">正在搜索「' + esc(q) + '」…</div>';
    state.searchHtml = $('searchBody').innerHTML;
    state.searchInfo = $('searchInfo').textContent;
    if (state.view !== 'search') go('search');
    else window.scrollTo(0, 0);
    api('/api/search?q=' + encodeURIComponent(q)).then(function (r) {
      if (state.searchQuery !== q) return;
      var items = r.items || [];
      var html = '<div class="block-title"><span>搜索结果</span><span class="sub">' +
        items.length + ' 部 · ' + r.elapsed_ms + ' ms</span></div>';
      html += items.length ? '<div class="grid">' + cardsHtml(items) + '</div>'
        : emptyHtml('没有找到结果', '换个关键词，或检查播放源是否可用。');
      html += '<div class="btn-row"><button class="btn ghost" data-act="back">返回片单</button></div>';
      $('searchBody').innerHTML = html;
      state.searchHtml = html;
      state.searchInfo = '关键词「' + q + '」· 可修改后重新搜索';
      $('searchInfo').textContent = state.searchInfo;
    }).catch(function (e) {
      if (state.searchQuery !== q) return;
      $('searchBody').innerHTML = emptyHtml('搜索失败', e.message || '网络异常');
      state.searchHtml = $('searchBody').innerHTML;
    });
  }

  /* ------------------------------------------------------------------ detail */
  function openDetail(id) {
    api('/api/title?tid=' + encodeURIComponent(id)).then(function (t) {
      if (state.view !== 'player') state.detailFrom = state.view;
      state.detail = t;
      state.line = Object.keys(t.lines || {})[0] || '';
      state.lastEp = -1;
      renderDetail();
      syncFavButton();
      go('detail');
      window.scrollTo(0, 0);
    }).catch(function (e) { toast(e.message || '详情已失效'); });
  }
  function renderDetail() {
    var t = state.detail;
    if (!t) return;
    $('detailTitle').textContent = t.title || '影片详情';
    var facts = [t.year, t.area, t.type, t.score ? '评分 ' + t.score : '', t.remarks]
      .filter(Boolean).map(function (x) { return '<span>' + esc(x) + '</span>'; }).join('');
    var lines = Object.keys(t.lines || {});
    var chips = lines.map(function (n) {
      return '<button class="chip' + (n === state.line ? ' active' : '') +
        '" data-act="line" data-line="' + esc(n) + '">' + esc(n) + '</button>';
    }).join('');
    var eps = t.lines && t.lines[state.line] ? t.lines[state.line] : [];
    var epHtml = eps.map(function (e, i) {
      return '<button class="ep' + (state.lastEp === i ? ' active' : '') +
        '" data-act="play-ep" data-ep="' + i + '" title="' + esc(e.name) + '">' + esc(e.name) + '</button>';
    }).join('');
    var html = '<div class="detail-hero"><div class="poster"><img src="' +
      esc(imgUrl(t.poster)) + '" onerror="this.src=\'/static/placeholder.svg\'"></div>' +
      '<div class="detail-info"><h1>' + esc(t.title) + '</h1><div class="facts">' + facts + '</div>' +
      '<p class="hint">' + esc((t.providers || []).join(' / ')) + '</p></div></div>' +
      '<div class="desc" id="descText">' + esc(t.overview || '暂无简介') + '</div>' +
      '<button class="desc-toggle" data-act="toggle-desc">展开简介</button>' +
      '<div class="block-title"><span>播放线路</span><span class="sub">' + lines.length + ' 条</span></div>' +
      '<div class="line-tabs">' + chips + '</div>' +
      '<div class="block-title"><span>选集</span><span class="sub">共 ' + eps.length + ' 集</span></div>' +
      '<div class="ep-grid">' + epHtml + '</div>' +
      '<button class="btn primary" data-act="play-ep" data-ep="0" style="width:100%">立即播放</button>';
    $('detailBody').innerHTML = html;
  }

  /* ----------------------------------------------------------------- player */
  function requestPlay(ep, line, push) {
    if (!state.detail) return;
    $('playerLoading').textContent = '正在解析播放地址…';
    post('/api/play', { tid: state.detail.id, line: line || state.line, ep: ep || 0 })
      .then(function (r) {
        state.play = r;
        state.line = r.line;
        state.lastEp = r.ep_index;
        if (push) go('player');
        setupVideo(r);
      }).catch(function (e) {
        toast(e.message || '解析失败，请切换线路');
        $('playerLoading').textContent = e.message || '解析失败';
      });
  }
  function startPlay(ep, line) { requestPlay(ep, line, true); }
  function switchEpisode(ep, line) { closeSheet(); requestPlay(ep, line, false); }

  function setupVideo(r) {
    destroyHls();
    var v = $('video');
    $('playerTitle').textContent = r.title + (r.episode ? ' · ' + r.episode : '');
    $('playerFoot').innerHTML = '<div>' + esc(r.title) + ' · ' + esc(r.line) + ' · ' + esc(r.episode) + '</div>' +
      '<div class="row"><button class="pbtn" data-act="episodes">切换选集</button>' +
      '<button class="pbtn" data-act="lines">切换线路</button></div>';
    $('playerLoading').classList.remove('hide');
    state.lastSave = 0;
    var resume = Number(r.resume || 0);
    var applyResume = function () {
      $('playerLoading').classList.add('hide');
      if (resume > 5 && v.duration > resume + 10) { try { v.currentTime = resume; } catch (e) {} }
      v.play().catch(function () {});
      scheduleControls();
    };
    if (r.kind === 'hls' && window.Hls && Hls.isSupported()) {
      var hls = new Hls({ maxBufferLength: 30, maxMaxBufferLength: 60,
        enableWorker: true, fragLoadingMaxRetry: 3, manifestLoadingMaxRetry: 2,
        xhrSetup: function (xhr) {
          try { xhr.setRequestHeader('X-Fn-Token', TOKEN); } catch (e) {}
        } });
      state.hls = hls;
      hls.on(Hls.Events.MANIFEST_PARSED, applyResume);
      hls.on(Hls.Events.ERROR, function (evt, data) {
        if (!data || !data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else {
          $('playerLoading').textContent = '播放失败，请切换线路或选集';
          $('playerLoading').classList.remove('hide');
        }
      });
      hls.loadSource(r.url);
      hls.attachMedia(v);
    } else {
      v.src = r.url;
      var once = function () { v.removeEventListener('loadedmetadata', once); applyResume(); };
      v.addEventListener('loadedmetadata', once);
    }
    bindVideo(v);
    if (window.FnNative && FnNative.setKeepScreenOn) FnNative.setKeepScreenOn(true);
    // 弹幕服务已在应用启动时后台就绪，这里直接发起匹配，不阻塞视频起播。
    autoLoadDanmu(r);
  }
  function destroyHls() {
    if (state.hls) { try { state.hls.destroy(); } catch (e) {} state.hls = null; }
  }
  function bindVideo(v) {
    if (state.videoBound) return;
    state.videoBound = true;
    v.addEventListener('timeupdate', function () {
      var d = v.duration || 0;
      $('timeNow').textContent = fmtTime(v.currentTime);
      $('timeTotal').textContent = fmtTime(d);
      if (!state.seeking && d > 0) $('seekBar').value = String(v.currentTime / d * 100);
      if (Math.abs(v.currentTime - state.lastSave) >= 5) {
        state.lastSave = v.currentTime;
        saveProgress();
      }
    });
    v.addEventListener('play', function () {
      $('btnPlayPause').textContent = '暂停';
      $('playerLoading').classList.add('hide');
      scheduleControls();
    });
    v.addEventListener('pause', function () {
      $('btnPlayPause').textContent = '播放';
      saveProgress();
    });
    v.addEventListener('ended', function () {
      if (!state.play) return;
      var next = state.play.ep_index + 1;
      if (state.play.episodes && next < state.play.episodes.length) {
        toast('即将播放下一集');
        switchEpisode(next, state.play.line);
      }
    });
  }
  function saveProgress() {
    var v = $('video');
    if (!state.play || !v || !isFinite(v.duration) || v.duration <= 0) return;
    post('/api/history', {
      id: state.play.id, title: state.play.title, poster: state.play.poster || '',
      episode: state.play.episode, line: state.play.line,
      ts: v.currentTime, duration: v.duration, at: Date.now()
    }).catch(function () {});
  }
  function stopPlayback() {
    saveProgress();
    var v = $('video');
    try { v.pause(); } catch (e) {}
    destroyHls();
    try { v.removeAttribute('src'); } catch (e) {}
    if (state.danmu) state.danmu.clear();
    state.danmuMeta = null;
    closeDanmuPanel();
    if (window.FnNative && FnNative.setKeepScreenOn) FnNative.setKeepScreenOn(false);
    if (window.FnNative && FnNative.requestLandscape) FnNative.requestLandscape(false);
    if (state.ctrlTimer) clearTimeout(state.ctrlTimer);
    state.play = null;
  }

  function scheduleControls() {
    if (state.ctrlTimer) clearTimeout(state.ctrlTimer);
    $('playerControls').classList.remove('hide');
    state.ctrlTimer = setTimeout(function () {
      if (state.view === 'player' && !$('video').paused) $('playerControls').classList.add('hide');
    }, 3200);
  }
  function toggleControls() {
    var c = $('playerControls');
    if (c.classList.contains('hide')) { c.classList.remove('hide'); scheduleControls(); }
    else { c.classList.add('hide'); if (state.ctrlTimer) clearTimeout(state.ctrlTimer); }
  }
  function toggleFullscreen() {
    var el = $('playerStage');
    var entering = !(document.fullscreenElement || document.webkitFullscreenElement);
    try {
      if (!entering) {
        if (document.exitFullscreen) document.exitFullscreen();
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      } else if (el.requestFullscreen) el.requestFullscreen();
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
      else if ($('video').webkitEnterFullscreen) $('video').webkitEnterFullscreen();
    } catch (e) {}
    // 进入全屏才切横屏；退出全屏立刻恢复竖屏。
    if (window.FnNative && FnNative.requestLandscape) FnNative.requestLandscape(entering);
  }

  /* ------------------------------------------------------------------- sheet */
  function openSheet(title, html) {
    $('sheetTitle').textContent = title;
    $('sheetBody').innerHTML = html;
    $('sheet').classList.add('show');
    $('sheetMask').classList.add('show');
  }
  function closeSheet() {
    $('sheet').classList.remove('show');
    $('sheetMask').classList.remove('show');
  }
  function episodesSheet() {
    if (!state.play) return;
    var items = state.play.episodes || [];
    var html = '<div class="ep-grid">' + items.map(function (e) {
      return '<button class="ep' + (e.index === state.play.ep_index ? ' active' : '') +
        '" data-act="sheet-ep" data-ep="' + e.index + '">' + esc(e.name) + '</button>';
    }).join('') + '</div>';
    openSheet('选集 · ' + state.play.title, html);
  }
  function linesSheet() {
    if (!state.play) return;
    var html = '<div class="line-tabs" style="flex-wrap:wrap">' +
      (state.play.lines || []).map(function (n) {
        return '<button class="chip' + (n === state.play.line ? ' active' : '') +
          '" data-act="sheet-line" data-line="' + esc(n) + '">' + esc(n) + '</button>';
      }).join('') + '</div><p class="hint">切换线路会重新解析当前集，播放进度会保留。</p>';
    openSheet('播放线路', html);
  }

  /* ------------------------------------------------------------------ danmaku */
  function dmNum(v, d) {
    return (v === undefined || v === null || v === '') ? d : (Number(v) || d);
  }
  function danmuOpts() {
    var c = state.config || {};
    return {
      enabled: c.danmu_enabled !== false,
      opacity: dmNum(c.danmu_opacity, 0.85),
      fontSize: dmNum(c.danmu_font_size, 20),
      area: dmNum(c.danmu_area, 0.75),
      speed: dmNum(c.danmu_speed, 8),
      offset: dmNum(c.danmu_offset, 0),
      bold: !!c.danmu_bold
    };
  }
  function initDanmu() {
    if (state.danmu || typeof DanmuEngine === 'undefined') return;
    state.danmu = new DanmuEngine($('danmuCanvas'), $('video'), danmuOpts());
    syncDanmuPanel();
  }
  function dmStatus(text, cls) {
    var e = $('dmStatus');
    if (!e) return;
    e.textContent = text;
    e.className = 'dm-status' + (cls ? ' ' + cls : '');
  }
  function syncDanmuPanel() {
    var o = danmuOpts();
    var set = function (id, val, txt) { $(id).value = val; $(id + 'Val').textContent = txt; };
    set('dmOpacity', o.opacity, Math.round(o.opacity * 100) + '%');
    set('dmFont', o.fontSize, o.fontSize + 'px');
    set('dmArea', o.area, Math.round(o.area * 100) + '%');
    set('dmSpeed', o.speed, o.speed + 's');
    set('dmOffset', o.offset, (o.offset > 0 ? '+' : '') + o.offset + 's');
    $('dmToggle').textContent = o.enabled ? '关闭弹幕' : '开启弹幕';
    $('btnDanmu').classList.toggle('on', o.enabled);
  }
  function saveDanmuPrefs(patch) {
    var k;
    for (k in patch) if (patch.hasOwnProperty(k)) { state.config[k] = patch[k]; state.dmPending[k] = patch[k]; }
    clearTimeout(state.dmSaveTimer);
    state.dmSaveTimer = setTimeout(function () {
      var p = state.dmPending; state.dmPending = {};
      post('/api/settings', p).then(function (c) { state.config = c; }).catch(function () {});
    }, 420);
  }
  function loadDanmuComment(epId, label) {
    if (epId === undefined || epId === null || epId === '') return Promise.resolve(null);
    dmStatus('正在获取弹幕…');
    return api('/api/danmu/comment/' + encodeURIComponent(epId)).then(function (r) {
      if (!r.ok) throw new Error(r.error || '获取失败');
      initDanmu();
      state.danmu.load(r.comments || []);
      state.danmuMeta = { episodeId: epId, label: label };
      if (!r.count) {
        dmStatus('已匹配到剧集，但返回 0 条弹幕（上游暂未取到，可点「重新匹配」重试）' +
          (label ? ' · ' + label : ''), 'bad');
      } else {
        dmStatus('已加载 ' + r.count + ' 条弹幕' + (label ? ' · ' + label : ''), 'ok');
      }
      syncDanmuPanel();
      return r;
    }).catch(function (e) {
      dmStatus('弹幕获取失败：' + (e.message || '网络异常'), 'bad');
      return null;
    });
  }
  /**
   * 同一集常常有多个平台的候选（腾讯/优酷/咪咕…），第一个候选可能取不到
   * 弹幕正文。这里按顺序试到第一个有弹幕的候选为止。
   */
  function tryDanmuCandidates(items, idx) {
    if (!items || idx >= items.length) return Promise.resolve(null);
    var hit = items[idx];
    var label = [hit.title, hit.episodeTitle].filter(Boolean).join(' ');
    return loadDanmuComment(hit.episodeId, label).then(function (r) {
      if (r && (r.count || 0) > 0) return r;
      if (idx + 1 < items.length) {
        dmStatus('该候选没有弹幕，正在尝试下一个来源…');
        return tryDanmuCandidates(items, idx + 1);
      }
      return r;
    }).catch(function () {
      return tryDanmuCandidates(items, idx + 1);
    });
  }
  function renderAnimeList(items) {
    var box = $('dmAnimes');
    box.hidden = false;
    box.innerHTML = items.length ? items.map(function (a) {
      return '<button class="dm-item" data-anime="' + esc(a.animeId) + '" data-title="' + esc(a.title) + '">' +
        esc(a.title) + '<small>' + esc(a.typeDescription || a.type || a.source || '') +
        ' · ' + (a.episodeCount || 0) + ' 集</small></button>';
    }).join('') : '<div class="dm-status">没有匹配结果，换个关键词试试</div>';
    var bs = box.querySelectorAll('[data-anime]');
    for (var i = 0; i < bs.length; i++) {
      bs[i].onclick = (function (b) {
        return function () { openDanmuEpisodes(b.getAttribute('data-anime'), b.getAttribute('data-title')); };
      })(bs[i]);
    }
  }
  function openDanmuEpisodes(animeId, title) {
    dmStatus('正在读取剧集…');
    return api('/api/danmu/bangumi/' + encodeURIComponent(animeId)).then(function (r) {
      if (!r.ok) throw new Error(r.error || '读取失败');
      var box = $('dmEpisodes');
      box.hidden = false;
      var eps = r.episodes || [];
      box.innerHTML = eps.length ? eps.map(function (e, i) {
        return '<button class="dm-item" data-ep="' + esc(e.episodeId) + '" data-name="' + esc(e.title) + '">' +
          esc(e.title || ('第 ' + (i + 1) + ' 集')) + '</button>';
      }).join('') : '<div class="dm-status">该条目没有可选剧集</div>';
      dmStatus((r.title || title) + ' · ' + eps.length + ' 集，请选择', 'ok');
      var bs = box.querySelectorAll('[data-ep]');
      for (var i = 0; i < bs.length; i++) {
        bs[i].onclick = (function (b) {
          return function () {
            for (var j = 0; j < bs.length; j++) bs[j].classList.remove('active');
            b.classList.add('active');
            loadDanmuComment(b.getAttribute('data-ep'), (r.title || title) + ' ' + b.getAttribute('data-name'));
          };
        })(bs[i]);
      }
    }).catch(function (e) { dmStatus('剧集读取失败：' + (e.message || '网络异常'), 'bad'); });
  }
  function autoLoadDanmu(info) {
    if (!info || !info.title) return;
    initDanmu();
    if (state.danmu) state.danmu.clear();
    state.danmuMeta = null;
    var c = state.config || {};
    if (c.danmu_enabled === false) { dmStatus('弹幕已关闭，可在弹幕面板开启'); return; }
    if (c.danmu_auto === false) { dmStatus('自动匹配已关闭，可在弹幕面板手动搜索'); return; }
    var multi = (info.episodes || []).length > 1;
    var ep = (info.ep_index || 0) + 1;
    var fileName = multi ? info.title + ' S01E' + (ep < 10 ? '0' + ep : ep) : info.title;
    dmStatus('正在自动匹配：' + fileName);
    return post('/api/danmu/match', { fileName: fileName }).then(function (m) {
      if (m.ok && m.matched && (m.items || []).length) {
        return tryDanmuCandidates(m.items, 0);
      }
      var why = m.reason || m.remote_reason || '上游弹幕库没有该片名';
      if (m.remote_error) why += '（完整服务：' + m.remote_error + '）';
      if ((m.tried || []).length > 1) why += ' · 已尝试 ' + m.tried.length + ' 个片名变体';
      return api('/api/danmu/search?keyword=' + encodeURIComponent(info.title)).then(function (s) {
        var items = (s && s.items) || [];
        renderAnimeList(items);
        if (!items.length) {
          dmStatus('未找到匹配弹幕 · ' + why + '；可手动搜索，或在设置里配置完整弹幕服务', 'bad');
          return;
        }
        return api('/api/danmu/bangumi/' + encodeURIComponent(items[0].animeId)).then(function (b) {
          var eps = (b && b.episodes) || [];
          if (!eps.length) { dmStatus('该条目没有可选剧集', 'bad'); return; }
          var target = eps[Math.min(info.ep_index || 0, eps.length - 1)];
          return loadDanmuComment(target.episodeId, [items[0].title, target.title].filter(Boolean).join(' '));
        });
      });
    }).catch(function (e) {
      dmStatus('弹幕服务不可达：' + (e.message || '网络异常') +
        '（检查设置里的服务地址；若提示 429 请稍后重试）', 'bad');
    });
  }
  function toggleDanmu() {
    initDanmu();
    if (!state.danmu) return;
    var on = !state.danmu.enabled;
    state.danmu.setEnabled(on);
    saveDanmuPrefs({ danmu_enabled: on });
    syncDanmuPanel();
    toast(on ? '弹幕已开启' : '弹幕已关闭');
  }
  function refreshDanmuService() {
    return api('/api/danmu/status').then(function (r) {
      state.danmuStatus = r || {};
      var core = r.core || {};
      var txt;
      if (r.ready === 'ok') {
        txt = '服务已就绪 · ' + r.base;
      } else if (r.ready === 'error') {
        txt = r.ready_text || '上游暂不可达';
      } else {
        txt = '服务正在后台启动（无需手动开启）';
      }
      if (core.ready) {
        txt = '内置弹幕核心已就绪 · 多平台聚合（豆瓣/腾讯/优酷/爱奇艺/咪咕/B站）';
      } else if (core.state === 'installing' || core.state === 'starting') {
        txt = '内置弹幕核心' + (core.state === 'installing' ? '安装中' : '启动中')
          + '：' + (core.message || '请稍候');
      } else if (core.state === 'failed') {
        txt = '内置弹幕核心未启动（' + (core.message || '未知原因') + '），已回退内置镜像';
      }
      $('dmSvcStatus').textContent = txt;
      var bits = [];
      if (r.coverage_note) bits.push(r.coverage_note);
      if (core.node_version) bits.push('Node ' + core.node_version);
      if (core.sources) bits.push('数据源：' + core.sources);
      if (r.last_match_reason) bits.push('上次匹配：' + r.last_match_reason);
      if (r.upstream_error) bits.push('上游错误：' + r.upstream_error);
      var note = bits.join(' / ');
      if ($('setDanmuNote')) $('setDanmuNote').textContent = note;
      if ($('dmCoverageNote')) $('dmCoverageNote').textContent = note;
      // 已加载弹幕时保留条数信息，避免打开面板把「已加载 N 条」覆盖掉
      if (state.danmuMeta && state.danmu) {
        dmStatus('已加载 ' + state.danmu.count + ' 条弹幕 · ' + txt,
          r.ready === 'ok' ? 'ok' : '');
      } else {
        dmStatus(txt, r.ready === 'ok' ? 'ok' : (r.ready === 'error' ? 'bad' : ''));
      }
      return r;
    }).catch(function () { $('dmSvcStatus').textContent = '状态读取失败'; });
  }
  function openDanmuPanel() {
    initDanmu();
    syncDanmuPanel();
    $('dmPanel').classList.add('show');
    $('dmMask').classList.add('show');
    if (state.ctrlTimer) clearTimeout(state.ctrlTimer);
    $('playerControls').classList.remove('hide');
    refreshDanmuService();
  }
  function closeDanmuPanel() {
    $('dmPanel').classList.remove('show');
    $('dmMask').classList.remove('show');
    scheduleControls();
  }

  /* -------------------------------------------------------------- favorites */
  function isFav(id) {
    for (var i = 0; i < state.favorites.length; i++) {
      if (state.favorites[i] && state.favorites[i].id === id) return true;
    }
    return false;
  }
  function syncFavButton() {
    var b = $('btnFav');
    if (!b || !state.detail) return;
    var on = isFav(state.detail.id);
    b.textContent = on ? '★ 已收藏' : '☆ 收藏';
    b.classList.toggle('on', on);
  }
  function renderFavorites() {
    var box = $('favoritesBody');
    if (!box) return;
    box.innerHTML = state.favorites.length
      ? '<div class="block-title"><span>共 ' + state.favorites.length + ' 部</span>' +
        '<span class="sub">点海报打开 · 右上角 × 取消</span></div>' +
        '<div class="grid">' + cardsHtml(state.favorites, true) + '</div>'
      : emptyHtml('还没有收藏', '在影片详情页点右上角「收藏」，或从首页搜索一部影片。', '去首页', 'go-home');
  }
  function loadFavorites() {
    return api('/api/favorites').then(function (r) {
      state.favorites = (r && r.items) || [];
      renderFavorites();
      syncFavButton();
    }).catch(function (e) { toast(e.message || '读取收藏失败'); });
  }
  function toggleFav() {
    if (!state.detail || !state.detail.id) return;
    var t = state.detail;
    if (isFav(t.id)) {
      post('/api/favorites/remove', { id: t.id }).then(function (r) {
        state.favorites = (r && r.items) || [];
        syncFavButton();
        if (state.detailFrom === 'favorites') renderFavorites();
        toast('已取消收藏');
      }).catch(function (e) { toast(e.message || '操作失败'); });
    } else {
      // item 用于卡片展示，snapshot 是完整详情（含线路），落盘后重启仍可打开。
      post('/api/favorites', { item: t, snapshot: t }).then(function (r) {
        state.favorites = (r && r.items) || [];
        syncFavButton();
        toast('已加入我的收藏');
      }).catch(function (e) { toast(e.message || '收藏失败'); });
    }
  }
  function removeFav(id) {
    post('/api/favorites/remove', { id: id }).then(function (r) {
      state.favorites = (r && r.items) || [];
      renderFavorites();
      syncFavButton();
      toast('已取消收藏');
    }).catch(function (e) { toast(e.message || '操作失败'); });
  }

  /* ----------------------------------------------------------------- history */
  function loadHistory() {
    api('/api/history').then(function (items) {
      state.history = items || [];
      $('historyBody').innerHTML = state.history.length
        ? historyHtml(state.history)
        : emptyHtml('还没有播放记录', '搜索一部影片开始观看吧。');
    }).catch(function (e) { toast(e.message || '读取失败'); });
  }
  function continueFrom(id) {
    api('/api/title?tid=' + encodeURIComponent(id)).then(function (t) {
      state.detailFrom = state.view;
      state.detail = t;
      state.line = Object.keys(t.lines || {})[0] || '';
      state.lastEp = -1;
      renderDetail();
      syncFavButton();
      go('detail');
      window.scrollTo(0, 0);
    }).catch(function () {
      // 后端已做「磁盘快照 → 按片名重搜」两级恢复；这里再兜一层，
      // 任何情况下都不把缓存失效提示直接抛给用户。
      var h = null;
      for (var i = 0; i < state.history.length; i++) if (state.history[i].id === id) h = state.history[i];
      if (!h || !h.title) { toast('这条记录已失效，请重新搜索'); return; }
      toast('正在重新查找「' + h.title + '」…');
      api('/api/search?q=' + encodeURIComponent(h.title)).then(function (r) {
        var items = r.items || [];
        if (!items.length) { toast('未找到该片，请手动搜索'); return; }
        openDetail(items[0].id);
      }).catch(function () { toast('重新查找失败，请手动搜索'); });
    });
  }

  /* ----------------------------------------------------------------- settings */
  function loadSettings() {
    api('/api/settings').then(function (c) {
      state.config = c || {};
      $('setResume').checked = c.auto_resume !== false;
      $('setTimeout').value = c.search_timeout || 9;
      $('setDanmuEnabled').checked = c.danmu_enabled !== false;
      $('setDanmuAuto').checked = c.danmu_auto !== false;
      $('setDanmuApi').value = c.danmu_api || '';
      if (state.danmu) { state.danmu.setOptions(danmuOpts()); syncDanmuPanel(); }
    }).catch(function () {});
    refreshDanmuService();
    loadSources();
  }
  function loadSources() {
    api('/api/sources').then(function (r) {
      var items = r.items || [];
      state.sourceCount = items.filter(function (s) { return s.enabled !== false; }).length;
      updateSourceInfo();
      $('sourceList').innerHTML = items.length ? items.map(function (s, i) {
        var off = s.enabled === false;
        return '<div class="source-row' + (off ? ' off' : '') + '"><div class="info"><b>' +
          esc(s.name) + '</b><small>' + esc(s.api) + '</small></div>' +
          '<button class="icon-btn" data-act="toggle-source" data-i="' + i + '">' + (off ? '启用' : '停用') + '</button>' +
          '<button class="icon-btn" data-act="delete-source" data-i="' + i + '">删除</button></div>';
      }).join('') : '<div class="empty">还没有播放源</div>';
    }).catch(function (e) { toast(e.message || '读取失败'); });
  }
  function firstApiFromText(text) {
    var m = String(text || '').match(/https?:\/\/[^\s,;"']+/);
    return m ? m[0] : '';
  }

  /* ------------------------------------------------------------------ events */
  function findAct(node) {
    while (node && node !== document) {
      if (node.getAttribute && node.getAttribute('data-act')) return node;
      node = node.parentNode;
    }
    return null;
  }
  function bind() {
    $('app').addEventListener('click', function (e) {
      var el = findAct(e.target);
      if (el) {
        var act = el.getAttribute('data-act');
        if (act === 'go-settings') { switchTab('settings'); return; }
        if (act === 'go-home') { switchTab('home'); return; }
        if (act === 'back') { window.__fnBack(); return; }
        if (act === 'toggle-fav') { toggleFav(); return; }
        if (act === 'unfav') { removeFav(el.getAttribute('data-id')); return; }
        if (act === 'detail') { openDetail(el.getAttribute('data-id')); return; }
        if (act === 'continue') { continueFrom(el.getAttribute('data-id')); return; }
        if (act === 'line') { state.line = el.getAttribute('data-line'); renderDetail(); return; }
        if (act === 'toggle-desc') {
          var d = $('descText');
          var open = d.classList.toggle('open');
          el.textContent = open ? '收起简介' : '展开简介';
          return;
        }
        if (act === 'play-ep') { startPlay(parseInt(el.getAttribute('data-ep'), 10), state.line); return; }
        if (act === 'load-more') { loadDiscover(state.discoverPg + 1, false); return; }
        if (act === 'episodes') { episodesSheet(); return; }
        if (act === 'lines') { linesSheet(); return; }
        if (act === 'sheet-ep') { switchEpisode(parseInt(el.getAttribute('data-ep'), 10), state.play && state.play.line); return; }
        if (act === 'sheet-line') { switchEpisode(state.play ? state.play.ep_index : 0, el.getAttribute('data-line')); return; }
        if (act === 'toggle-source') {
          post('/api/sources/toggle', { index: parseInt(el.getAttribute('data-i'), 10),
            enabled: !(el.textContent === '停用') }).then(loadSources);
          return;
        }
        if (act === 'delete-source') {
          post('/api/sources/delete', { index: parseInt(el.getAttribute('data-i'), 10) }).then(loadSources);
          return;
        }
      }
      var tab = null, n = e.target;
      while (n && n !== document) {
        if (n.getAttribute && n.getAttribute('data-tab')) { tab = n; break; }
        n = n.parentNode;
      }
      if (tab) switchTab(tab.getAttribute('data-tab'));
    });

    $('btnSearch').onclick = doSearch;
    $('btnSearch2').onclick = doSearch;
    [ $('searchInput'), $('searchInput2') ].forEach(function (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.keyCode === 13) { e.preventDefault(); doSearch(); }
      });
    });
    $('sheetClose').onclick = closeSheet;
    $('sheetMask').onclick = closeSheet;

    $('btnPlayPause').onclick = function () {
      var v = $('video');
      if (v.paused) v.play().catch(function () { toast('无法开始播放'); });
      else v.pause();
    };
    $('btnRate').onclick = function () {
      var i = RATES.indexOf(state.rate);
      state.rate = RATES[(i + 1) % RATES.length];
      $('video').playbackRate = state.rate;
      $('btnRate').textContent = '倍速 ' + state.rate.toFixed(2).replace(/0$/, '');
      toast('播放速度 ' + state.rate + 'x');
    };
    $('btnFullscreen').onclick = toggleFullscreen;
    $('playerStage').addEventListener('click', function (e) {
      var tag = e.target.tagName;
      if (tag === 'BUTTON' || tag === 'INPUT') return;
      toggleControls();
    });
    $('seekBar').addEventListener('input', function () {
      state.seeking = true;
      var d = $('video').duration || 0;
      $('timeNow').textContent = fmtTime(Number(this.value) / 100 * d);
    });
    $('seekBar').addEventListener('change', function () {
      var v = $('video');
      if (v.duration > 0) v.currentTime = Number(this.value) / 100 * v.duration;
      state.seeking = false;
      scheduleControls();
    });

    $('btnAddSource').onclick = function () {
      var apiUrl = $('newApi').value.trim();
      if (!firstApiFromText(apiUrl)) { toast('请填写完整的 http(s) 接口地址'); return; }
      post('/api/sources', { name: $('newName').value.trim(), api: apiUrl })
        .then(function () {
          $('newName').value = ''; $('newApi').value = '';
          toast('已添加'); loadSources(); boot();
        }).catch(function (e) { toast(e.message || '添加失败'); });
    };
    $('btnBulk').onclick = function () {
      var text = $('bulkText').value;
      if (!text.trim()) { toast('请先粘贴播放源'); return; }
      post('/api/sources/bulk', { text: text }).then(function (r) {
        toast('成功导入 ' + r.added + ' 个播放源');
        $('bulkText').value = '';
        loadSources(); boot();
      }).catch(function (e) { toast(e.message || '导入失败'); });
    };
    $('btnTestApi').onclick = function () {
      var url = firstApiFromText($('bulkText').value) || $('newApi').value.trim();
      if (!url) { toast('请先填写或粘贴一个接口地址'); return; }
      toast('正在测试…');
      post('/api/sources/test', { api: url }).then(function (r) {
        toast(r.ok ? ('接口可用，返回 ' + r.count + ' 条 · ' + (r.sample || '')) : ('不可用：' + (r.error || '无结果')));
      }).catch(function (e) { toast(e.message || '测试失败'); });
    };
    $('btnSaveSettings').onclick = function () {
      post('/api/settings', {
        auto_resume: $('setResume').checked,
        search_timeout: Math.max(3, Math.min(30, parseInt($('setTimeout').value, 10) || 9))
      }).then(function () { toast('设置已保存'); }).catch(function (e) { toast(e.message || '保存失败'); });
    };
    $('btnImportConfig').onclick = function () {
      var text = $('configText').value.trim();
      if (!text) { toast('请先粘贴配置内容'); return; }
      post('/api/config/import', { text: text }).then(function () {
        toast('配置已导入'); boot(); loadSettings();
      }).catch(function (e) { toast('配置格式错误：' + (e.message || '')); });
    };
    $('btnExportConfig').onclick = function () {
      api('/api/bootstrap').then(function (b) {
        $('configText').value = JSON.stringify(b.config, null, 2);
        $('configText').select();
        toast('当前配置已填入文本框，可长按复制');
      });
    };
    $('btnClearHistory').onclick = function () {
      post('/api/history/clear', {}).then(function () {
        state.history = []; loadHistory(); toast('播放记录已清空');
      });
    };
    $('btnClearFavorites').onclick = function () {
      if (!state.favorites.length) { toast('收藏夹是空的'); return; }
      post('/api/favorites/clear', {}).then(function (r) {
        state.favorites = (r && r.items) || [];
        renderFavorites(); syncFavButton(); toast('收藏已清空');
      }).catch(function (e) { toast(e.message || '操作失败'); });
    };

    /* ---------------- 弹幕面板 ---------------- */
    $('btnDanmu').onclick = openDanmuPanel;
    $('dmClose').onclick = closeDanmuPanel;
    $('dmMask').onclick = closeDanmuPanel;
    $('dmToggle').onclick = toggleDanmu;
    $('dmReload').onclick = function () {
      if (state.play) autoLoadDanmu(state.play);
      else toast('请先开始播放');
    };
    $('dmClear').onclick = function () {
      if (state.danmu) state.danmu.clear();
      dmStatus('已清屏');
    };
    var doDanmuSearch = function () {
      var kw = $('dmKeyword').value.trim();
      if (!kw) { toast('请输入片名'); return; }
      dmStatus('正在搜索「' + kw + '」…');
      api('/api/danmu/search?keyword=' + encodeURIComponent(kw)).then(function (r) {
        var items = (r && r.items) || [];
        renderAnimeList(items);
        dmStatus(items.length ? ('找到 ' + items.length + ' 个条目，请选择') : '没有匹配结果',
          items.length ? 'ok' : 'bad');
      }).catch(function (e) { dmStatus('搜索失败：' + (e.message || '网络异常'), 'bad'); });
    };
    $('dmSearchBtn').onclick = doDanmuSearch;
    $('dmKeyword').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.keyCode === 13) { e.preventDefault(); doDanmuSearch(); }
    });
    var sendDanmu = function () {
      var t = $('dmText').value.trim();
      if (!t) return;
      initDanmu();
      if (!state.danmu.enabled) {
        state.danmu.setEnabled(true);
        saveDanmuPrefs({ danmu_enabled: true });
        syncDanmuPanel();
      }
      state.danmu.send(t, '#ffd54a');
      $('dmText').value = '';
    };
    $('dmSendBtn').onclick = sendDanmu;
    $('dmText').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.keyCode === 13) { e.preventDefault(); sendDanmu(); }
    });
    var sliders = [
      ['dmOpacity', 'danmu_opacity', 'opacity', function (v) { return Math.round(v * 100) + '%'; }],
      ['dmFont', 'danmu_font_size', 'fontSize', function (v) { return v + 'px'; }],
      ['dmArea', 'danmu_area', 'area', function (v) { return Math.round(v * 100) + '%'; }],
      ['dmSpeed', 'danmu_speed', 'speed', function (v) { return v + 's'; }],
      ['dmOffset', 'danmu_offset', 'offset', function (v) { return (v > 0 ? '+' : '') + v + 's'; }]
    ];
    for (var si = 0; si < sliders.length; si++) {
      (function (row) {
        $(row[0]).addEventListener('input', function () {
          var val = Number(this.value);
          initDanmu();
          var ep = {}; ep[row[2]] = val;
          state.danmu.setOptions(ep);
          if (row[1] === 'danmu_offset') state.danmu.reset();
          $(row[0] + 'Val').textContent = row[3](val);
          var patch = {}; patch[row[1]] = val;
          saveDanmuPrefs(patch);
        });
      })(sliders[si]);
    }

    /* ---------------- 设置页弹幕 ---------------- */
    $('btnDanmuSave').onclick = function () {
      var patch = {
        danmu_enabled: $('setDanmuEnabled').checked,
        danmu_auto: $('setDanmuAuto').checked,
        danmu_api: $('setDanmuApi').value.trim()
      };
      post('/api/settings', patch).then(function (c) {
        state.config = c;
        initDanmu();
        state.danmu.setOptions(danmuOpts());
        syncDanmuPanel();
        toast('弹幕设置已保存');
        return post('/api/danmu/prewarm', {});
      }).then(function () { refreshDanmuService(); })
        .catch(function (e) { toast(e.message || '保存失败'); });
    };
    $('btnDanmuProbe').onclick = function () {
      var v = $('setDanmuApi').value.trim();
      if (!v) { toast('内置服务无需测试，直接播放即可'); return; }
      toast('正在测试…');
      post('/api/danmu/probe', { api: v }).then(function (r) {
        if (r.ok) {
          toast('测试通过 · HTTP ' + r.status + ' · 返回 ' + (r.items || 0) +
            ' 条 · ' + (r.ms || 0) + ' ms');
        } else {
          toast('不可用：' + (r.error || ('HTTP ' + r.status)));
        }
      }).catch(function (e) { toast(e.message || '测试失败'); });
    };
    $('btnDanmuRecheck').onclick = function () {
      toast('正在重新检测…');
      post('/api/danmu/prewarm', {}).then(refreshDanmuService)
        .catch(function () { refreshDanmuService(); });
    };
  }

  bind();
  boot();
})();
