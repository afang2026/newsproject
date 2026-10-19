(function () {
  var allItems = [];
  var generatedAt = null;
  var loadFailed = false;
  var loadPromise = null;

  function load() {
    if (loadPromise) return loadPromise;
    loadPromise = (async function () {
      try {
        var res = await fetch('feeds.json', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        var data = await res.json();
        allItems = Array.isArray(data.items) ? data.items : [];
        generatedAt = data.generatedAt || null;
        loadFailed = false;
      } catch (e) {
        allItems = [];
        generatedAt = null;
        loadFailed = true;
      }
      return { items: allItems, generatedAt: generatedAt, failed: loadFailed };
    })();
    return loadPromise;
  }

  async function loadConfig() {
    try {
      var res = await fetch('feeds.config.json', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var data = await res.json();
      return Array.isArray(data.feeds) ? data.feeds : [];
    } catch (e) {
      return [];
    }
  }

  function getSources() {
    var seen = {};
    allItems.forEach(function (it) { if (it.source) seen[it.source] = true; });
    return Object.keys(seen).sort();
  }

  function applyFilter(search, source) {
    var q = (search || '').trim().toLowerCase();
    return allItems
      .filter(function (it) { return source === 'all' || it.source === source; })
      .filter(function (it) {
        if (!q) return true;
        var hay = ((it.title || '') + ' ' + (it.summary || '')).toLowerCase();
        return hay.indexOf(q) !== -1;
      })
      .sort(function (a, b) { return new Date(b.published) - new Date(a.published); });
  }

  function safeHref(raw) {
    try {
      var u = new URL(raw, location.href);
      if (u.protocol === 'http:' || u.protocol === 'https:') return u.href;
    } catch (e) {}
    return '#';
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' · ' +
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function render(listEl, items) {
    listEl.innerHTML = '';
    items.forEach(function (it) {
      var li = document.createElement('li');
      li.className = 'feed-item';

      var link = document.createElement('a');
      link.className = 'feed-title';
      link.href = safeHref(it.link);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = it.title || '(untitled)';

      var meta = document.createElement('p');
      meta.className = 'feed-meta';
      meta.textContent = (it.source || '') + (it.section ? ' · ' + it.section : '') + ' · ' + formatDate(it.published);

      var summary = document.createElement('p');
      summary.className = 'feed-summary';
      summary.textContent = it.summary || '';

      li.appendChild(link);
      li.appendChild(meta);
      li.appendChild(summary);
      listEl.appendChild(li);
    });
  }

  window.RD = window.RD || {};
  window.RD.feeds = {
    load: load,
    loadConfig: loadConfig,
    getSources: getSources,
    applyFilter: applyFilter,
    render: render,
    formatDate: formatDate
  };
})();
