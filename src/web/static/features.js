// Optional modules (Screener, Debate, Shadow, Memory): which ones this browser shows.
// Loaded before app.js. Also loaded by node tests, so every DOM/storage access is guarded.
(function () {
  'use strict';

  const STORAGE_KEY = 'alphaagent.modules.v1';

  const MODULES = [
    { id: 'screener', label: 'Screener', icon: 'fa-solid fa-magnifying-glass-chart', description: 'Scan the universe for setups.', defaultOn: false },
    { id: 'debate', label: 'Debate', icon: 'fa-solid fa-comments', description: 'Run the committee on one ticker.', defaultOn: false },
    { id: 'shadow', label: 'Shadow', icon: 'fa-solid fa-flask', description: 'Simulated parameter variants.', defaultOn: false },
    { id: 'memory', label: 'Memory', icon: 'fa-solid fa-brain', description: 'Agent memory and code graph.', defaultOn: false },
  ];
  const IDS = MODULES.map(m => m.id);

  // Pure: stored value -> full state. Anything not a boolean falls back to the default.
  function resolveModules(raw) {
    const src = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    const out = {};
    for (const m of MODULES) out[m.id] = typeof src[m.id] === 'boolean' ? src[m.id] : m.defaultOn;
    return out;
  }

  function load() {
    try {
      if (typeof localStorage === 'undefined') return resolveModules(null);
      return resolveModules(JSON.parse(localStorage.getItem(STORAGE_KEY)));
    } catch (e) {
      return resolveModules(null); // storage blocked or not JSON
    }
  }

  let state = load();

  function isEnabled(id) {
    return !IDS.includes(id) || state[id] === true;
  }

  function renderToggles() {
    const list = document.getElementById('moduleToggleList');
    if (!list) return;
    if (!list.querySelector('input[data-module-toggle]')) {
      // Labels are constants from MODULES, nothing user-supplied
      list.innerHTML = MODULES.map(m => `
        <div class="set-row">
          <div class="min-w-0">
            <label for="moduleToggle-${m.id}" class="set-label"><i class="${m.icon} mr-2" aria-hidden="true"></i>${m.label}</label>
            <span class="set-help">${m.description}</span>
          </div>
          <span class="check-wrap set-toggle">
            <input type="checkbox" id="moduleToggle-${m.id}" data-module-toggle="${m.id}">
            <span class="ui-switch" aria-hidden="true"></span>
          </span>
        </div>`).join('');
      list.querySelectorAll('input[data-module-toggle]').forEach(input => {
        input.addEventListener('change', () => setEnabled(input.dataset.moduleToggle, input.checked));
      });
    }
    list.querySelectorAll('input[data-module-toggle]').forEach(input => {
      input.checked = isEnabled(input.dataset.moduleToggle);
    });
  }

  function applyModules() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('[data-module]').forEach(el => {
      const on = isEnabled(el.dataset.module);
      // Sections: tab switching owns their visibility, so only ever hide them here
      if (el.classList.contains('tab-content')) {
        if (!on) el.classList.add('hidden');
      } else {
        el.classList.toggle('hidden', !on);
      }
    });
    renderToggles();
  }

  function setEnabled(id, on) {
    if (!IDS.includes(id)) return;
    state = resolveModules({ ...state, [id]: on === true });
    try {
      if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) { /* storage blocked: keep the change for this page view */ }
    applyModules();
    if (typeof document !== 'undefined' && typeof CustomEvent !== 'undefined') {
      document.dispatchEvent(new CustomEvent('modules:changed', { detail: { ...state } }));
    }
  }

  const api = { MODULES, STORAGE_KEY, resolveModules, isEnabled, setEnabled, applyModules };

  if (typeof window !== 'undefined') window.Features = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyModules);
    else applyModules();
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
