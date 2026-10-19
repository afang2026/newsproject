(function () {
  function initClock() {
    var timeEl = document.getElementById('clockTime');
    var metaEl = document.getElementById('clockMeta');
    function tick() {
      var now = new Date();
      timeEl.textContent = now.toLocaleTimeString([], { hour12: false });
      metaEl.textContent = now.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
    }
    tick();
    setInterval(tick, 1000);
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  }

  function initThemeToggle() {
    var settings = RD.state.getSettings();
    applyTheme(settings.theme);
    document.getElementById('themeToggle').addEventListener('click', function () {
      var s = RD.state.getSettings();
      s.theme = s.theme === 'dark' ? 'light' : 'dark';
      RD.state.saveSettings(s);
      applyTheme(s.theme);
    });
  }

  function initTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
    var tablist = document.querySelector('.tabs');

    function activate(name) {
      tabs.forEach(function (t) {
        var active = t.dataset.tab === name;
        t.setAttribute('aria-selected', active ? 'true' : 'false');
        t.setAttribute('tabindex', active ? '0' : '-1');
      });
      document.querySelectorAll('.page').forEach(function (p) {
        p.hidden = p.id !== 'page-' + name;
      });
      var ui = RD.state.getUi();
      ui.lastTab = name;
      RD.state.saveUi(ui);
    }

    tabs.forEach(function (t) {
      t.addEventListener('click', function () { activate(t.dataset.tab); });
    });

    tablist.addEventListener('keydown', function (e) {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(e.key) === -1) return;
      e.preventDefault();
      var idx = tabs.indexOf(document.activeElement);
      if (idx === -1) idx = tabs.findIndex(function (t) { return t.getAttribute('aria-selected') === 'true'; });
      var nextIdx;
      if (e.key === 'ArrowLeft') nextIdx = (idx - 1 + tabs.length) % tabs.length;
      else if (e.key === 'ArrowRight') nextIdx = (idx + 1) % tabs.length;
      else if (e.key === 'Home') nextIdx = 0;
      else nextIdx = tabs.length - 1;
      tabs[nextIdx].focus();
      activate(tabs[nextIdx].dataset.tab);
    });

    activate(RD.state.getUi().lastTab || 'today');
  }

  function initCollapsibleCards() {
    document.querySelectorAll('.card[data-collapse-key]').forEach(function (card) {
      var key = card.dataset.collapseKey;
      var head = card.querySelector('.card-head');
      var ui = RD.state.getUi();
      var isCollapsed = !!(ui.collapsed && ui.collapsed[key]);
      if (isCollapsed) card.classList.add('collapsed');
      head.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');

      function toggle() {
        card.classList.toggle('collapsed');
        var nowCollapsed = card.classList.contains('collapsed');
        head.setAttribute('aria-expanded', nowCollapsed ? 'false' : 'true');
        var u = RD.state.getUi();
        u.collapsed = u.collapsed || {};
        u.collapsed[key] = nowCollapsed;
        RD.state.saveUi(u);
      }

      head.addEventListener('click', toggle);
      head.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggle();
        }
      });
    });
  }

  var lastFocused = null;

  function openModal(id) {
    var modal = document.getElementById(id);
    lastFocused = document.activeElement;
    modal.setAttribute('aria-hidden', 'false');
    var box = modal.querySelector('.modal-box');
    if (box) box.focus();
  }

  function closeModal(id) {
    document.getElementById(id).setAttribute('aria-hidden', 'true');
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
    lastFocused = null;
  }

  function confirmAction(title, message, onConfirm) {
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    var okBtn = document.getElementById('confirmOk');
    var freshOk = okBtn.cloneNode(true);
    okBtn.parentNode.replaceChild(freshOk, okBtn);
    freshOk.addEventListener('click', function () {
      closeModal('confirmModal');
      onConfirm();
    });
    openModal('confirmModal');
  }

  document.addEventListener('click', function (e) {
    var closer = e.target.closest('[data-close]');
    if (!closer) return;
    var modal = closer.closest('.modal');
    if (modal) closeModal(modal.id);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal[aria-hidden="false"]').forEach(function (m) { closeModal(m.id); });
      return;
    }
    if (e.key !== 'Tab') return;
    var openModalEl = document.querySelector('.modal[aria-hidden="false"]');
    if (!openModalEl) return;
    var focusables = Array.prototype.slice.call(
      openModalEl.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    );
    if (!focusables.length) return;
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  window.RD = window.RD || {};
  window.RD.ui = {
    initClock: initClock,
    initThemeToggle: initThemeToggle,
    initTabs: initTabs,
    initCollapsibleCards: initCollapsibleCards,
    openModal: openModal,
    closeModal: closeModal,
    confirmAction: confirmAction
  };
})();
