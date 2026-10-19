(function () {
  function initFeedPage() {
    var searchEl = document.getElementById('feedSearch');
    var sourceEl = document.getElementById('feedSource');
    var listEl = document.getElementById('feedList');
    var emptyEl = document.getElementById('feedEmpty');
    var errorEl = document.getElementById('feedLoadError');
    var countEl = document.getElementById('feedCount');

    function refresh() {
      var items = RD.feeds.applyFilter(searchEl.value, sourceEl.value);
      RD.feeds.render(listEl, items);
      emptyEl.hidden = items.length !== 0;
      countEl.textContent = items.length + (items.length === 1 ? ' article' : ' articles');
    }

    var searchTimer = null;
    searchEl.addEventListener('input', function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(refresh, 150);
    });
    sourceEl.addEventListener('change', refresh);

    RD.feeds.load().then(function (result) {
      errorEl.hidden = !result.failed;
      RD.feeds.getSources().forEach(function (src) {
        var opt = document.createElement('option');
        opt.value = src;
        opt.textContent = src;
        sourceEl.appendChild(opt);
      });
      refresh();
    });
  }

  function initSettingsPage() {
    RD.feeds.load().then(function (result) {
      document.getElementById('feedsLastUpdated').textContent = result.generatedAt
        ? 'Last updated: ' + new Date(result.generatedAt).toLocaleString()
        : 'Last updated: feeds.json not generated yet';
    });

    RD.feeds.loadConfig().then(function (feeds) {
      var listEl = document.getElementById('sourceConfigList');
      listEl.innerHTML = '';
      feeds.forEach(function (f) {
        var li = document.createElement('li');
        var b = document.createElement('b');
        b.textContent = f.source + ' — ';
        li.appendChild(b);
        li.appendChild(document.createTextNode(f.section || ''));
        listEl.appendChild(li);
      });
    });

    document.getElementById('exportBtn').addEventListener('click', function () {
      var payload = RD.state.exportAll();
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'reading-desk-backup-' + new Date().toISOString().slice(0, 10) + '.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });

    var importBtn = document.getElementById('importBtn');
    var importFile = document.getElementById('importFile');
    importBtn.addEventListener('click', function () { importFile.click(); });
    importFile.addEventListener('change', function () {
      var file = importFile.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var parsed;
        try {
          parsed = JSON.parse(reader.result);
        } catch (e) {
          alert('That file is not valid JSON.');
          importFile.value = '';
          return;
        }
        RD.ui.confirmAction(
          'Import data?',
          'This overwrites everything currently stored in this browser for Reading Desk. This cannot be undone.',
          function () {
            try { RD.state.importAll(parsed); }
            catch (e) { alert('Import failed: ' + e.message); }
          }
        );
        importFile.value = '';
      };
      reader.readAsText(file);
    });

    document.getElementById('clearAllBtn').addEventListener('click', function () {
      RD.ui.confirmAction(
        'Clear all data?',
        'This permanently deletes everything Reading Desk has stored in this browser. This cannot be undone.',
        function () { RD.state.clearAll(); }
      );
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    RD.state.init();
    RD.ui.initClock();
    RD.ui.initThemeToggle();
    RD.ui.initTabs();
    RD.ui.initCollapsibleCards();
    initFeedPage();
    initSettingsPage();
  });
})();
