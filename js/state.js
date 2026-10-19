(function () {
  var K = {
    meta: 'rd.meta',
    settings: 'rd.settings',
    ui: 'rd.ui',
    articlesRead: 'rd.articlesRead',
    gaps: 'rd.gaps',
    topics: 'rd.topics',
    picksCache: 'rd.picksCache',
    skips: 'rd.skips',
    tasks: 'rd.tasks',
    rituals: 'rd.rituals',
    ritLog: 'rd.ritLog'
  };
  var SCHEMA_VERSION = 1;

  var DEFAULTS = {};
  DEFAULTS[K.meta] = { schemaVersion: SCHEMA_VERSION };
  DEFAULTS[K.settings] = { theme: 'light' };
  DEFAULTS[K.ui] = { lastTab: 'today', collapsed: {} };
  DEFAULTS[K.articlesRead] = [];
  DEFAULTS[K.gaps] = [];
  DEFAULTS[K.topics] = [];
  DEFAULTS[K.picksCache] = null;
  DEFAULTS[K.skips] = [];
  DEFAULTS[K.tasks] = [];
  DEFAULTS[K.rituals] = [];
  DEFAULTS[K.ritLog] = {};

  function get(key, fallback) {
    var raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    try { return JSON.parse(raw); } catch (e) { return fallback; }
  }

  function set(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  function migrate() {
    var meta = get(K.meta, null);
    if (!meta) {
      set(K.meta, { schemaVersion: SCHEMA_VERSION });
      return;
    }
    if (meta.schemaVersion < SCHEMA_VERSION) {
      // future migration steps keyed by meta.schemaVersion go here
      meta.schemaVersion = SCHEMA_VERSION;
      set(K.meta, meta);
    }
  }

  function init() {
    Object.keys(DEFAULTS).forEach(function (key) {
      if (localStorage.getItem(key) === null) set(key, DEFAULTS[key]);
    });
    migrate();
  }

  function getSettings() { return get(K.settings, DEFAULTS[K.settings]); }
  function saveSettings(obj) { set(K.settings, obj); }
  function getUi() { return get(K.ui, DEFAULTS[K.ui]); }
  function saveUi(obj) { set(K.ui, obj); }

  function exportAll() {
    var data = {};
    Object.keys(K).forEach(function (name) { data[name] = get(K[name], DEFAULTS[K[name]]); });
    return { schemaVersion: SCHEMA_VERSION, exportedAt: new Date().toISOString(), data: data };
  }

  var ARRAY_KEYS = ['articlesRead', 'gaps', 'topics', 'skips', 'tasks', 'rituals'];
  var OBJECT_KEYS = ['meta', 'settings', 'ui', 'ritLog'];

  function isPlainObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function importAll(payload) {
    if (!isPlainObject(payload) || !isPlainObject(payload.data)) {
      throw new Error('Invalid backup file');
    }
    var snapshot = {};
    Object.keys(K).forEach(function (name) { snapshot[name] = localStorage.getItem(K[name]); });
    try {
      Object.keys(K).forEach(function (name) {
        if (!(name in payload.data)) return;
        var value = payload.data[name];
        if (ARRAY_KEYS.indexOf(name) !== -1 && !Array.isArray(value)) return;
        if (OBJECT_KEYS.indexOf(name) !== -1 && !isPlainObject(value)) return;
        set(K[name], value);
      });
    } catch (e) {
      Object.keys(snapshot).forEach(function (name) {
        if (snapshot[name] === null) localStorage.removeItem(K[name]);
        else localStorage.setItem(K[name], snapshot[name]);
      });
      throw e;
    }
    migrate();
    location.reload();
  }

  function clearAll() {
    Object.keys(K).forEach(function (name) { localStorage.removeItem(K[name]); });
    location.reload();
  }

  window.RD = window.RD || {};
  window.RD.state = {
    K: K,
    init: init,
    get: get,
    set: set,
    getSettings: getSettings,
    saveSettings: saveSettings,
    getUi: getUi,
    saveUi: saveUi,
    exportAll: exportAll,
    importAll: importAll,
    clearAll: clearAll
  };
})();
