// Global State
let allocationChart = null;
let valueChart = null; // Home value line (loadValueHistory)
let currentScreenerMarket = 'ALL';
let currentAccountCash = 10000.0;
let killSwitchActive = false;        // last known kill-switch state
let lastSavedTradingMode = 'PAPER';  // used to revert the mode <select> when a switch is cancelled
let currentTab = 'portfolio';
let adviceReportsCache = [];         // keeps report objects out of the DOM (no JSON in attributes)

// ------------------------------------------------------------------
// Security helpers: every string that comes from the server (LLM text,
// news-derived text, tickers, names, ...) must go through esc() before it
// is interpolated into an innerHTML template.
// ------------------------------------------------------------------
function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/`/g, '&#96;');
}

// Escapes everything, then re-enables a tiny whitelist of attribute-less
// formatting tags (the Telegram-style <b>/<i>/<code> markup used in review text).
function safeRichText(value) {
  return esc(value).replace(/&lt;(\/?)(b|strong|i|em|u|s|code|pre)&gt;/gi, '<$1$2>');
}

// Number formatting that tolerates null/undefined/strings from the API
function num(value, digits = 2) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : (0).toFixed(digits);
}
function eur(value, digits = 2) {
  const n = Number(value);
  const v = Number.isFinite(n) ? n : 0;
  return `€${v.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits})}`;
}

// Euro amount with an explicit + or - sign
function signedEur(value, digits = 2) {
  const n = Number(value) || 0;
  return `${n > 0 ? '+' : n < 0 ? '-' : ''}${eur(Math.abs(n), digits)}`;
}

// Segmented bar: one slice per holding, cash last
function renderAllocationBar(cashEur, positions, mineEur = 0) {
  const bar = document.getElementById('allocationBar');
  const legend = document.getElementById('allocationLegend');
  if (!bar) return;
  const parts = positions.map((p, i) => ({
    label: String(p.ticker || ''),
    value: Math.max(0, Number(p.current_value_eur) || 0),
    color: ALLOC_COLORS[i % ALLOC_COLORS.length]
  }));
  // Your own holdings: hatched slice between the bot's slices and cash
  parts.push({ label: 'Yours', value: Math.max(0, mineEur), cls: 'alloc-mine' });
  parts.push({ label: 'Cash', value: Math.max(0, cashEur), color: ALLOC_CASH_COLOR });
  const total = parts.reduce((s, p) => s + p.value, 0);
  if (total <= 0) {
    bar.innerHTML = '<span style="flex:1;background:var(--line-strong)"></span>';
    if (legend) legend.innerHTML = '';
    return;
  }
  const shown = parts.filter(p => p.value > 0);
  bar.innerHTML = shown.map(p => `<span${p.cls ? ` class="${p.cls}"` : ''} style="flex:${(p.value / total * 1000).toFixed(0)}${p.color ? `;background:${p.color}` : ''}" title="${esc(p.label)}"></span>`).join('');
  bar.setAttribute('aria-label', 'Capital allocation: ' + shown.map(p => `${p.label} ${(p.value / total * 100).toFixed(0)}%`).join(', '));
  if (legend) {
    legend.innerHTML = positions.length === 0 && mineEur <= 0 ? '' : shown.map(p =>
      `<span><i${p.cls ? ` class="${p.cls}"` : ''}${p.color ? ` style="background:${p.color}"` : ''}></i>${esc(p.label)} <span class="ui-num text-slate-300">${(p.value / total * 100).toFixed(0)}%</span></span>`
    ).join('');
  }
}

// Small badge showing where a committee score came from ("llm" | "rules")
function scoreSourceBadge(source) {
  if (!source) return '';
  const key = String(source).toLowerCase();
  const label = key === 'llm' ? 'AI score' : (key === 'rules' ? 'Rule score' : null);
  if (!label) return '';
  return `<span class="block ui-faint text-[12px] mt-1" title="Score source: ${esc(key)}">${esc(label)}</span>`;
}

// Verdict badge for a committee action (BUY / STRONG_BUY / HOLD / SELL); arrow + word, never colour alone
function adviceVerdictBadge(action) {
  const a = String(action || '').toUpperCase();
  const text = a.replace('_', ' ');
  if (!text) return '';
  if (a === 'STRONG_BUY' || a === 'BUY') return `<span class="ui-badge ui-badge-gain">▲ ${esc(text)}</span>`;
  if (a === 'SELL' || a === 'STRONG_SELL') return `<span class="ui-badge ui-badge-loss">▼ ${esc(text)}</span>`;
  return `<span class="ui-badge">${esc(text)}</span>`;
}

// Mono score out of 100 with a small bar underneath
function adviceScoreBlock(score, label, sourceHtml = '') {
  const n = Number(score);
  const pct = Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
  const shown = Number.isFinite(n) ? esc(Math.round(n)) : '--';
  return `
    <div class="adv-score" title="${esc(label)}">
      <div class="ui-num text-[22px] font-semibold text-slate-50 leading-none">${shown}<span class="ui-faint text-[12px] font-normal">/100</span></div>
      <div class="ui-track mt-2" aria-hidden="true"><span style="width:${pct}%"></span></div>
      <div class="ui-faint text-[12px] mt-1">${esc(label)}</div>
      ${sourceHtml}
    </div>`;
}

// Price-like value from the API, or a dash when missing
function advicePrice(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '—';
  return `${esc(value)}${suffix ? ' ' + esc(suffix) : ''}`;
}

// Initialize on DOM Load
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  applyTheme();
  loadAllData();
  switchTabUi('portfolio');
  updateMoreButton('portfolio');
  initFolds();
  resumePendingJob();

  // Enter key submits the ticker inputs (on-screen keyboard "Go")
  const analyzeInput = document.getElementById('analyzeInput');
  if (analyzeInput) analyzeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runOnDemandAnalysis(); } });
  const triggerInput = document.getElementById('triggerTickerInput');
  if (triggerInput) triggerInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); armTriggerFromInput(); } });

  // Auto-refresh every 30 seconds (skipped while the browser tab is in the background)
  setInterval(() => {
    if (document.hidden) return;
    loadPortfolio();
    loadMarketStatus();
    loadBrokerStatus();
    refreshSystemStatus();
  }, 30000);
});

// Event delegation for all dynamically rendered buttons (no inline handlers with data in them)
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const ticker = el.dataset.ticker || '';
  switch (action) {
    case 'close-position': closePosition(ticker); break;
    case 'toggle-position': toggleHolding(el, ticker); break;
    case 'set-owner': setOwner(ticker, el.dataset.owner, el.closest('.own-switch')); break;
    case 'toggle-holding': toggleHolding(el, ticker); break;
    case 'buy': executePaperBuy(ticker); break;
    case 'arm-trigger': armAITrigger(ticker); break;
    case 'analyze': analyzeDirect(ticker); break;
    case 'inspect': inspectReport(Number(el.dataset.idx)); break;
    case 'cancel-trigger': cancelTrigger(Number(el.dataset.id), ticker); break;
    case 'reset-shadow': resetShadow(el.dataset.key || ''); break;
    case 'new-shadow': openShadowEditor(null); break;
    case 'edit-shadow': openShadowEditor(el.dataset.key || ''); break;
    case 'copy-shadow': copyShadow(el.dataset.key || ''); break;
    case 'delete-shadow': deleteShadow(el.dataset.key || ''); break;
    case 'undo-shadow-change': undoShadowChange(el.dataset.key || '', Number(el.dataset.id)); break;
    case 'open-ticker': openTickerSheet(ticker, el.dataset.source || ''); break;
    default: break;
  }
});

// Fade in the first content that replaces a "Loading..." placeholder.
// Only once: later data refreshes must not flicker.
document.querySelectorAll('[data-loading]').forEach(el => {
  const obs = new MutationObserver(() => {
    obs.disconnect();
    el.removeAttribute('data-loading');
    el.classList.add('just-loaded');
    el.addEventListener('animationend', () => el.classList.remove('just-loaded'), { once: true });
  });
  obs.observe(el, { childList: true, characterData: true, subtree: true });
});

let toastTimer = null;
function showToast(message, kind = false) {
  // kind: true | 'error' => error styling; false | 'info' | 'success' => normal
  const isError = kind === true || kind === 'error';
  const toast = document.getElementById('toast');
  const msg = document.getElementById('toastMsg');
  const icon = document.getElementById('toastIcon');

  msg.textContent = message;
  if (isError) {
    icon.className = 'fa-solid fa-circle-exclamation ui-loss';
  } else if (kind === 'info') {
    icon.className = 'fa-solid fa-circle-info text-accent';
  } else {
    icon.className = 'fa-solid fa-circle-check ui-gain';
  }

  toast.classList.add('is-open');

  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove('is-open');
  }, isError ? 6000 : 3500);
}

// Tab Metadata for Mobile Dropdown & Navigation
const TAB_CONFIG = {
  portfolio: { label: 'Home', icon: 'fa-solid fa-house' },
  advice: { label: 'Daily Advice', icon: 'fa-solid fa-lightbulb' },
  screener: { label: 'Market Screener', icon: 'fa-solid fa-magnifying-glass-chart' },
  debate: { label: 'Committee Debate', icon: 'fa-solid fa-comments' },
  triggers: { label: 'AI Triggers', icon: 'fa-solid fa-crosshairs' },
  history: { label: 'Trade History', icon: 'fa-solid fa-clock-rotate-left' },
  shadow: { label: 'Shadow Paper', icon: 'fa-solid fa-flask' },
  memory: { label: 'Memory Hub', icon: 'fa-solid fa-brain' },
  settings: { label: 'Settings', icon: 'fa-solid fa-sliders' }
};

// Toggle Mobile Dropdown Menu
function toggleMobileDropdown() {
  if (moreIsSettingsOnly()) { switchTab('settings'); return; }
  const menu = document.getElementById('mobileDropdownMenu');
  const backdrop = document.getElementById('mobileDropdownBackdrop');
  const chevron = document.getElementById('mobileDropdownChevron');
  if (!menu) return;
  const moreBtn = document.getElementById('mobileMenuDropdownBtn');
  const isHidden = menu.classList.contains('hidden') || menu.classList.contains('is-closing');
  if (isHidden) {
    clearTimeout(dropdownCloseTimer);
    [menu, backdrop].forEach(el => el && el.classList.remove('hidden', 'is-closing'));
    if (chevron) chevron.classList.add('rotate-180');
    if (moreBtn) moreBtn.setAttribute('aria-expanded', 'true');
  } else {
    closeMobileDropdown();
  }
}

// Close Mobile Dropdown Menu: play the exit, then hide
let dropdownCloseTimer = null;
function closeMobileDropdown() {
  const menu = document.getElementById('mobileDropdownMenu');
  const backdrop = document.getElementById('mobileDropdownBackdrop');
  const chevron = document.getElementById('mobileDropdownChevron');
  const moreBtn = document.getElementById('mobileMenuDropdownBtn');
  if (!menu || menu.classList.contains('hidden') || menu.classList.contains('is-closing')) return;
  const els = [menu, backdrop].filter(Boolean);
  els.forEach(el => el.classList.add('is-closing'));
  clearTimeout(dropdownCloseTimer);
  // Matches --dur-out in style.css
  dropdownCloseTimer = setTimeout(() => els.forEach(el => el.classList.replace('is-closing', 'hidden')), 160);
  if (chevron) chevron.classList.remove('rotate-180');
  if (moreBtn) moreBtn.setAttribute('aria-expanded', 'false');
}

// Global click-outside listener to close mobile dropdown
document.addEventListener('click', (e) => {
  const menu = document.getElementById('mobileDropdownMenu');
  const btn = document.getElementById('mobileMenuDropdownBtn');
  if (menu && !menu.classList.contains('hidden')) {
    if (!menu.contains(e.target) && !btn?.contains(e.target)) {
      closeMobileDropdown();
    }
  }
});

// Escape key listener to close dropdown
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeMobileDropdown();
});

// Tab Switching
// Optional modules come from features.js. If it failed to load, everything stays visible.
const F = window.Features || { MODULES: [], isEnabled: () => true };
const MODULE_TABS = F.MODULES.length ? F.MODULES.map(m => m.id) : ['screener', 'debate', 'shadow', 'memory'];
// Tabs that live in the mobile "More" sheet: enabled modules plus Settings
function moreTabs() {
  return MODULE_TABS.filter(id => F.isEnabled(id)).concat('settings');
}
// Only Settings left: the "More" button opens Settings directly instead of an almost empty sheet
function moreIsSettingsOnly() {
  const tabs = moreTabs();
  return tabs.length === 1 && tabs[0] === 'settings';
}

// Mobile "More" button: label and icon of the current tab inside it, or a plain Settings button
function updateMoreButton(tabId) {
  const btn = document.getElementById('mobileMenuDropdownBtn');
  const mobileLabel = document.getElementById('mobileDropdownActiveText');
  const mobileIcon = document.getElementById('mobileDropdownActiveIcon');
  if (moreIsSettingsOnly()) {
    if (mobileLabel) mobileLabel.textContent = 'Settings';
    if (mobileIcon) mobileIcon.className = 'fa-solid fa-sliders';
    if (btn) { btn.removeAttribute('aria-haspopup'); btn.removeAttribute('aria-expanded'); }
    return;
  }
  if (btn) {
    btn.setAttribute('aria-haspopup', 'dialog');
    if (!btn.hasAttribute('aria-expanded')) btn.setAttribute('aria-expanded', 'false');
  }
  const cfg = TAB_CONFIG[tabId] || { label: tabId, icon: 'fa-solid fa-bars' };
  const inMore = moreTabs().includes(tabId);
  const SHORT = { screener: 'Screener', debate: 'Debate', shadow: 'Shadow', memory: 'Memory', settings: 'Settings' };
  if (mobileLabel) mobileLabel.textContent = inMore ? (SHORT[tabId] || cfg.label) : 'More';
  if (mobileIcon) mobileIcon.className = inMore ? cfg.icon : 'fa-solid fa-table-cells-large';
}

// Restart a one-shot CSS animation class. Fill mode is `backwards`, so content is only
// hidden while the animation runs and always ends at its natural styles.
function replayClass(el, cls) {
  document.querySelectorAll('.' + cls).forEach(n => { if (n !== el) n.classList.remove(cls); });
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  el.addEventListener('animationend', (e) => { if (e.target === el) el.classList.remove(cls); }, { once: true });
}

// Give the first rendered children of a list a 30ms entrance stagger (max 8, rest appear at once).
// Runs once per tab visit: switchTab clears data-staggered, 30 s refreshes find it set and skip.
function staggerChildren(el) {
  if (!el || el.dataset.staggered === '1') return;
  el.dataset.staggered = '1';
  Array.from(el.children).slice(0, 8).forEach((child, i) => {
    child.style.setProperty('--i', i);
    child.classList.add('stagger-item');
    child.addEventListener('animationend', () => child.classList.remove('stagger-item'), { once: true });
  });
}

// Nav items that just became visible (module switched on) slide in
let navSeen = null;
function visibleNavTabs() {
  const out = new Set();
  document.querySelectorAll('.seg-btn[data-tab], .bottom-nav-btn[data-tab], .mobile-dropdown-item[data-tab]').forEach(el => {
    if (el.offsetParent !== null) out.add(el);
  });
  return out;
}
function replayNav(el) {
  el.classList.remove('nav-enter');
  void el.offsetWidth;
  el.classList.add('nav-enter');
  el.addEventListener('animationend', () => el.classList.remove('nav-enter'), { once: true });
}
document.addEventListener('DOMContentLoaded', () => { navSeen = visibleNavTabs(); });

// A module was switched on or off in Settings: update nav at once, leave a tab that just went away
document.addEventListener('modules:changed', () => {
  if (!F.isEnabled(currentTab)) { switchTab('portfolio'); return; }
  switchTabUi(currentTab);
  updateMoreButton(currentTab);
  const now = visibleNavTabs();
  if (navSeen) now.forEach(el => { if (!navSeen.has(el)) replayNav(el); });
  navSeen = now;
});

// Header hairline only once content scrolls under it
(() => {
  const header = document.querySelector('.app-header');
  if (!header) return;
  const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 4);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();

function switchTabUi(tabId) {
  currentTab = tabId;
  // Bottom nav active state ("More" lights up for the tabs that live inside it)
  document.querySelectorAll('.bottom-nav-btn').forEach(btn => {
    const isMore = btn.dataset.more === '1';
    const active = isMore ? moreTabs().includes(tabId) : btn.dataset.tab === tabId;
    btn.classList.toggle('active', active);
    if (active) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  });
}

function switchTab(tabId) {
  // A disabled module (stored tab, deep link, shortcut) lands on Home, never on a blank page
  if (!F.isEnabled(tabId)) tabId = 'portfolio';
  switchTabUi(tabId);
  // Hide all contents
  document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
  const target = document.getElementById(`tab-${tabId}`);
  if (target) target.classList.remove('hidden');
  // A new visit staggers its lists once again; refreshes within a visit do not
  document.querySelectorAll('[data-staggered]').forEach(el => delete el.dataset.staggered);
  if (target) replayClass(target, 'tab-enter');

  // Update desktop nav (segmented control; .active-tab is the raised segment)
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const active = btn.dataset.tab === tabId;
    btn.classList.toggle('active-tab', active);
    if (active) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  });

  updateMoreButton(tabId);

  // Update the "More" sheet tiles: mark the current section
  document.querySelectorAll('.mobile-dropdown-item').forEach(item => {
    const isCurrent = item.dataset.tab === tabId;
    item.classList.toggle('is-current', isCurrent);
    const check = item.querySelector('.mobile-active-check');
    if (check) check.classList.toggle('hidden', !isCurrent);
  });

  // Auto-close dropdown menu
  closeMobileDropdown();

  // Start every section at the top (matters on phones where the page is long)
  window.scrollTo(0, 0);

  // Load specific tab data on switch
  if (tabId === 'portfolio') { loadPortfolio(); loadBrokerStatus(); }
  if (tabId === 'advice') { loadRecommendations(); loadMarketReview(); loadWeeklyReviewComparison(); }
  if (tabId === 'screener') loadScreener();
  if (tabId === 'triggers') loadTriggers();
  if (tabId === 'history') { loadTrades(); loadLearningInsights(); loadCashFlows(); }
  if (tabId === 'shadow') loadShadow();
  if (tabId === 'memory') loadMemoryHub();
  if (tabId === 'settings') { loadSettings(); loadBrokerStatus(); }
}

// Collapsible sections (<details data-fold>): localStorage keeps only the sections a viewer
// set away from their HTML default, so a new default still reaches everyone else.
const FOLD_KEY = 'foldState';
function initFolds() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(FOLD_KEY)) || {}; } catch (e) { /* storage blocked */ }
  if (typeof saved !== 'object' || Array.isArray(saved)) saved = {};
  document.querySelectorAll('details[data-fold]').forEach(d => {
    const id = d.dataset.fold;
    const byDefault = d.open;
    if (typeof saved[id] === 'boolean') d.open = saved[id];
    d.addEventListener('toggle', () => {
      if (d.open === byDefault) delete saved[id]; else saved[id] = d.open;
      try { localStorage.setItem(FOLD_KEY, JSON.stringify(saved)); } catch (e) { /* storage blocked */ }
    });
  });
}

// Shortcuts from Home land on Settings with their group already open
function openSettingsGroup(group) {
  switchTab('settings');
  const el = document.querySelector(`details[data-fold="settings-${group}"]`);
  if (!el) return;
  el.open = true;
  // switchTab scrolls to the top; a lower group would stay below the fold on a phone
  el.scrollIntoView({ block: 'start' });
}

function loadAllData() {
  refreshSystemStatus();
  loadMarketStatus();
  loadBrokerStatus();
  loadPortfolio();
  loadRecommendations();
  loadMarketReview();
  loadWeeklyReviewComparison();
}

// Theme: colour tokens switch by media query in style.css. JS reads them only
// for canvas charts and the browser chrome, and re-reads them when the system theme flips.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Allocation colours: one per holding, cash is a neutral grey (style.css --chart-*)
let ALLOC_COLORS = [];
let ALLOC_CASH_COLOR = '';
function readAllocColors() {
  ALLOC_COLORS = [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar(`--chart-${i}`));
  ALLOC_CASH_COLOR = cssVar('--chart-cash');
}
readAllocColors();

// Line charts: a vertical crosshair at the hovered point. Drawn under the datasets so the
// price line and hover dot stay on top. Hover state tracks the pointer directly: the
// tooltip and hover dot do not tween, so they never trail the cursor.
const crosshairPlugin = {
  id: 'crosshair',
  beforeDatasetsDraw(chart) {
    if (chart.config.type !== 'line') return;
    const active = chart.getActiveElements();
    if (!active.length) return;
    const x = Math.round(active[0].element.x) + 0.5; // half pixel keeps a 1px line crisp
    const { ctx, chartArea: a } = chart;
    ctx.save();
    ctx.strokeStyle = cssVar('--text-3');
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom); ctx.stroke();
    ctx.restore();
  },
  // chart.$readout(index | null): called when the hovered point changes, null on leave
  afterEvent(chart) {
    if (!chart.$readout) return;
    const hit = chart.getActiveElements().find(e => e.datasetIndex === 0);
    const i = hit ? hit.index : null;
    if (i === chart.$hover) return;
    chart.$hover = i;
    chart.$readout(i);
  }
};

// Swap an element's text for a hovered value; null puts the resting text back
function setReadout(el, text) {
  if (text === null) {
    if ('rest' in el.dataset) { el.textContent = el.dataset.rest; delete el.dataset.rest; }
    return;
  }
  if (!('rest' in el.dataset)) el.dataset.rest = el.textContent;
  el.textContent = text;
}
// A data refresh while hovering updates the resting text, not the readout
function setRestingText(el, text) {
  if ('rest' in el.dataset) el.dataset.rest = text;
  else el.textContent = text;
}
if (typeof Chart !== 'undefined') {
  Chart.register(crosshairPlugin);
  Chart.defaults.plugins.tooltip.animation = false;
  Chart.defaults.transitions.active.animation.duration = 0;
}

function styleShadowChart(chart) {
  const [shadow, main] = chart.data.datasets;
  shadow.borderColor = shadow.backgroundColor = cssVar('--accent');
  main.borderColor = main.backgroundColor = cssVar('--text-2');
  chart.options.scales.x.ticks.color = cssVar('--text-3');
  chart.options.scales.y.ticks.color = cssVar('--text-3');
  chart.options.scales.y.grid.color = cssVar('--line-soft');
}

function applyTheme() {
  const dark = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)').matches : true;
  document.documentElement.classList.toggle('dark', dark); // Tailwind `dark:` variants only
  document.querySelectorAll('meta[name="theme-color"]').forEach(m => m.setAttribute('content', cssVar('--bg')));
  readAllocColors();
  if (allocationChart) {
    const ds = allocationChart.data.datasets[0];
    ds.borderColor = cssVar('--surface');
    ds.backgroundColor = [ALLOC_CASH_COLOR, ...ds.data.slice(1).map((_, i) => ALLOC_COLORS[i % ALLOC_COLORS.length])];
    allocationChart.options.plugins.legend.labels.color = cssVar('--text-2');
    allocationChart.update('none');
  }
  Object.values(shadowCharts).forEach(c => { styleShadowChart(c); c.update('none'); });
  if (valueChart) { styleValueChart(); valueChart.update('none'); }
}
if (window.matchMedia) {
  const themeQuery = matchMedia('(prefers-color-scheme: dark)');
  if (themeQuery.addEventListener) themeQuery.addEventListener('change', () => { applyTheme(); loadPortfolio(); });
}
// Position rows the user expanded survive the 30 s refresh
const openPositions = new Set();

// 1. Chart Initialization
function initChart() {
  const canvas = document.getElementById('allocationChart');
  if (!canvas || typeof Chart === 'undefined') return; // CDN blocked: the rest of the page still works
  const ctx = canvas.getContext('2d');
  allocationChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Cash', 'Invested'],
      datasets: [{
        data: [10000, 0],
        backgroundColor: [ALLOC_CASH_COLOR, ALLOC_COLORS[0]],
        borderWidth: 2,
        borderColor: cssVar('--surface')
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: cssVar('--text-2'), font: { size: 12, family: cssVar('--font-sans') }, boxWidth: 12 }
        }
      },
      cutout: '72%'
    }
  });
}

// Bot | Mine slider. Sits in .pos-detail, never inside the .pos-head button.
function ownerNote(ticker, owner) {
  const t = esc(ticker);
  return owner === 'mine' ? `The bot never buys or sells ${t}. Sell it in Trading 212.` : `The bot trades ${t}: stop, target and sell.`;
}
// ticker -> side it is moving to. Survives the list re-render, so a reload cannot hide a flip in flight.
const flipping = new Map();
function ownerBusyNote(ticker, target) {
  return target === 'mine' ? 'Cancelling the stop at Trading 212…' : 'Committee is setting stop and target…';
}
function ownerSwitchHtml(ticker, owner) {
  const t = esc(ticker);
  const target = flipping.get(ticker);
  const busy = target !== undefined;
  if (busy) owner = target;
  const seg = (side, icon, label) => `<button type="button" role="radio" data-set="${side}" data-action="set-owner" data-ticker="${t}" data-owner="${side}" aria-checked="${owner === side}" tabindex="${owner === side ? 0 : -1}"><i class="fa-solid ${busy && owner !== side ? 'fa-circle-notch fa-spin' : `fa-${icon}`}" aria-hidden="true"></i> ${label}</button>`;
  return `
    <div class="mt-3 space-y-1.5">
      <div class="own-switch${busy ? ' busy' : ''}" data-owner="${owner}" role="radiogroup" aria-label="Who trades ${t}"${busy ? ' aria-busy="true"' : ''}>${seg('bot', 'robot', 'Bot')}${seg('mine', 'user', 'Mine')}</div>
      <p class="own-note">${busy ? esc(ownerBusyNote(ticker, target)) : ownerNote(ticker, owner)}</p>
    </div>`;
}

// Open/close a row and remember it across reloads
function toggleHolding(el, ticker) {
  const row = el.closest('.pos-row');
  if (!row) return;
  const open = row.classList.toggle('open');
  row.classList.toggle('just-opened', open);
  el.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) openPositions.add(ticker); else openPositions.delete(ticker);
}

async function setOwner(ticker, owner, sw) {
  if (!sw || flipping.has(ticker) || sw.dataset.owner === owner) return;
  const question = owner === 'mine'
    ? `${ticker} becomes yours. The bot cancels its stop at Trading 212 and never trades ${ticker} again. Continue?`
    : `Let the bot trade ${ticker}? The committee sets a stop and a target, and the bot sells at either. This can take a minute or two.`;
  if (!confirm(question)) return;
  flipping.set(ticker, owner);
  // Re-render draws the busy slider from `flipping`; the same code serves any later reload
  const holder = sw.parentElement;
  holder.outerHTML = ownerSwitchHtml(ticker, owner);
  try {
    const res = await fetch(`/api/holdings/${encodeURIComponent(ticker)}/owner`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ owner })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Could not move ${ticker}`);
    showToast(data.message || 'Done');
  } catch (e) {
    showToast(e.message, true);
  } finally {
    flipping.delete(ticker);
  }
  await loadPortfolio(); // success: new side; error: old side. Never stuck busy.
}

// Arrow keys move between Bot and Mine
document.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const sw = e.target.closest && e.target.closest('.own-switch');
  if (!sw) return;
  e.preventDefault();
  const other = sw.querySelector('[aria-checked="false"]');
  if (other) { other.focus(); other.click(); }
});

// "My holdings": the user's own Trading 212 stocks, kept apart from the bot's
async function renderHoldings(req, show) {
  const section = document.getElementById('holdingsSection');
  const box = document.getElementById('holdingsContainer');
  if (!section || !box) return;
  if (!show) { section.hidden = true; return; }
  let list;
  try {
    list = await req;
  } catch (e) {
    section.hidden = false;
    document.getElementById('holdingsCount').textContent = '';
    box.innerHTML = '<div class="text-center px-6 py-6 text-slate-400 text-sm">Could not load your holdings. Pull to refresh.</div>';
    return;
  }
  if (!Array.isArray(list) || list.length === 0) { section.hidden = true; return; }
  section.hidden = false;
  document.getElementById('holdingsCount').textContent = String(list.length);
  box.innerHTML = list.map(h => {
    const pnl = Number(h.unrealized_pnl_eur) || 0;
    const pnlPct = Number(h.unrealized_pnl_pct);
    const tone = pnl > 0 ? 'ui-gain' : pnl < 0 ? 'ui-loss' : 'ui-muted';
    const arrow = pnl > 0 ? '▲' : pnl < 0 ? '▼' : '';
    const pctText = Number.isFinite(pnlPct) ? `${arrow} ${Math.abs(pnlPct).toFixed(2)}%` : '';
    const isOpen = openPositions.has(h.ticker);
    const shares = `${esc(h.shares)} share${Number(h.shares) === 1 ? '' : 's'}`;
    return `
      <div class="pos-row${isOpen ? ' open' : ''}">
        <button type="button" class="pos-head" data-action="toggle-holding" data-ticker="${esc(h.ticker)}" aria-expanded="${isOpen ? 'true' : 'false'}">
          <span class="pos-mono mine" aria-hidden="true">${esc(String(h.ticker || '').split('.')[0].slice(0, 4))}</span>
          <span class="flex-1 min-w-0">
            <span class="block font-semibold text-base text-slate-50 truncate">${esc(h.ticker)}</span>
            <span class="block text-xs text-slate-500 truncate">${shares}${h.name ? ` · ${esc(h.name)}` : ''}</span>
          </span>
          <span class="text-right shrink-0">
            <span class="block ui-num font-semibold text-[15px] text-slate-50">${eur(h.current_value_eur)}</span>
            <span class="block ui-num text-xs font-semibold ${tone}"><span class="max-md:hidden">${signedEur(pnl)} · </span>${pctText}</span>
          </span>
          <i class="fa-solid fa-chevron-down pos-chevron" aria-hidden="true"></i>
        </button>
        <div class="pos-detail">
          <div class="grid grid-cols-2 gap-x-4 gap-y-2 pt-2">
            <div class="kv"><span>Avg price</span><span>${esc(h.avg_price_native)} ${esc(h.currency)}</span></div>
            <div class="kv"><span>Now</span><span>${esc(h.current_price_native)} ${esc(h.currency)}</span></div>
            <div class="kv col-span-2"><span>P&amp;L</span><span class="${tone}">${signedEur(pnl)}</span></div>
          </div>
          ${ownerSwitchHtml(h.ticker, 'mine')}
          <button type="button" class="ui-btn ui-btn-quiet ui-btn-sm w-full mt-2" data-action="open-ticker" data-ticker="${esc(h.ticker)}" data-source="" aria-label="Chart for ${esc(h.ticker)}"><i class="fa-solid fa-chart-line" aria-hidden="true"></i> Chart</button>
        </div>
      </div>`;
  }).join('');
}

// 2. Load Portfolio & Positions
// Home value line: 30 days of portfolio snapshots, green when the period is up, red when down
function styleValueChart() {
  const ds = valueChart.data.datasets[0];
  const vals = ds.data;
  const up = vals.length < 2 || vals[vals.length - 1] >= vals[0];
  ds.borderColor = cssVar(up ? '--gain' : '--loss');
  ds.backgroundColor = cssVar(up ? '--gain-soft' : '--loss-soft');
}

async function loadValueHistory() {
  const fig = document.querySelector('.hero-chart');
  const canvas = document.getElementById('valueChart');
  const note = document.getElementById('valueChartNote');
  if (!fig || !canvas || !note) return;
  let points = [];
  try {
    const res = await fetch('/api/portfolio/history?days=30');
    if (res.ok) points = await res.json();
  } catch (e) { /* offline: show the empty note */ }
  const empty = !Array.isArray(points) || points.length < 2 || typeof Chart === 'undefined';
  fig.classList.toggle('is-empty', empty);
  if (empty) {
    note.textContent = 'The value line appears once the bot has recorded a few portfolio changes.';
    return;
  }
  setRestingText(note, 'Last 30 days');
  const labels = points.map(p => p[0]);
  const data = points.map(p => p[1]);
  if (!valueChart) {
    valueChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels, datasets: [{ data, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, fill: 'start' }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false } // hovered value shows in the hero figure instead
        },
        scales: { x: { display: false }, y: { display: false, grace: '5%' } }
      }
    });
    const total = document.getElementById('metricTotalVal');
    valueChart.$readout = i => {
      const c = valueChart;
      setReadout(total, i === null ? null : eur(c.data.datasets[0].data[i]));
      setReadout(note, i === null ? null : new Date(c.data.labels[i] + 'Z').toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }));
    };
  } else {
    valueChart.data.labels = labels;
    valueChart.data.datasets[0].data = data;
  }
  styleValueChart();
  valueChart.update('none');
}

async function loadPortfolio() {
  loadValueHistory();
  // Separate promise: a holdings failure must not blank the bot list
  const holdingsReq = fetch('/api/holdings').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  holdingsReq.catch(() => {});
  try {
    const [summaryRes, posRes] = await Promise.all([
      fetch('/api/portfolio'),
      fetch('/api/positions')
    ]);

    const summary = await summaryRes.json();
    const positions = await posRes.json();

    // Update KPI cards
    const pnlVal = positions.reduce((s, p) => s + (p.unrealized_pnl_eur || 0), 0);
    const equityVal = positions.reduce((s, p) => s + (p.current_value_eur || 0), 0);
    const mineEur = Number(summary.user_holdings_eur) || 0;
    // Use the server's total and return (broker account total when live), so Web and Telegram match
    const totalVal = Number(summary.total_value_eur) || 0;
    const investedCap = Number(summary.invested_capital_eur ?? summary.initial_capital_eur) || 0;
    const totalReturnPct = Number(summary.total_return_pct) || 0;

    const isLive = (summary.trading_mode || '').toUpperCase() === 'LIVE';
    const modeBadge = document.getElementById('tradingModeBadge');
    if (modeBadge) {
      modeBadge.textContent = isLive ? 'LIVE' : 'PAPER';
      modeBadge.className = isLive ? 'mode-tag is-live' : 'mode-tag';
    }
    const pnlSub = document.getElementById('metricUnrealizedPnlSub');
    if (pnlSub) {
      pnlSub.textContent = `Open ${isLive ? 'live' : 'paper'} positions`;
    }

    currentAccountCash = summary.total_account_cash_eur ?? summary.cash_eur ?? 0;
    const deployableCash = Number(summary.available_cash_eur ?? summary.usable_capital_eur ?? summary.cash_eur) || 0;
    const protectedReserve = summary.protected_reserve_eur || 0;
    const maxUsagePct = summary.max_capital_usage_pct ?? 100;
    const maxCapEur = summary.max_deployable_capital_eur;

    setRestingText(document.getElementById('metricTotalVal'), eur(totalVal));
    document.getElementById('metricCash').textContent = eur(deployableCash);
    document.getElementById('metricEquity').textContent = eur(equityVal);
    const mineWrap = document.getElementById('metricMineWrap');
    if (mineWrap) {
      mineWrap.hidden = !(mineEur > 0);
      document.getElementById('metricMine').textContent = eur(mineEur);
    }
    const totalCashEl = document.getElementById('metricTotalCash');
    if (totalCashEl) totalCashEl.textContent = eur(Number(summary.cash_eur) || 0);

    // Pending triggers summary (tap to open the Triggers tab)
    const trigCount = Number(summary.pending_triggers_count) || 0;
    const trigReserved = Number(summary.reserved_triggers_eur) || 0;
    const trigText = document.getElementById('pendingTriggersSummaryText');
    const trigSub = document.getElementById('pendingTriggersSummarySub');
    if (trigText) trigText.textContent = `${trigCount} pending trigger${trigCount === 1 ? '' : 's'}`;
    if (trigSub) trigSub.textContent = trigCount > 0 && trigReserved > 0
      ? `${eur(trigReserved)} reserved for limit orders`
      : 'Limit orders waiting for their price';
    setTriggerNavBadge(trigCount);

    const limitBadge = document.getElementById('capitalLimitBadge');
    if (limitBadge) {
      if (maxUsagePct < 100 || (maxCapEur && maxCapEur > 0)) {
        limitBadge.textContent = `${maxUsagePct}%${maxCapEur ? ` · cap ${eur(maxCapEur, 0)}` : ''}`;
        limitBadge.className = 'ui-num text-warn';
      } else {
        limitBadge.textContent = '100%';
        limitBadge.className = 'ui-num text-slate-200';
      }
    }

    const cashSub = document.getElementById('metricCashSub');
    if (cashSub) {
      const reservedTriggers = summary.reserved_triggers_eur || 0;
      const triggersCount = summary.pending_triggers_count || 0;
      const subParts = [];

      if (reservedTriggers > 0) {
        subParts.push(`<span title="Capital earmarked for pending order triggers">${eur(reservedTriggers)} held for ${triggersCount} trigger${triggersCount > 1 ? 's' : ''}</span>`);
      }
      if (protectedReserve > 0) {
        subParts.push(`<span class="text-warn">${eur(protectedReserve)} protected</span>`);
      }

      if (subParts.length > 0) {
        cashSub.innerHTML = subParts.join(' · ');
      } else {
        cashSub.textContent = `of ${eur(currentAccountCash)} account cash`;
      }
    }

    recalcCapitalPreview();
    
    const pnlElem = document.getElementById('metricUnrealizedPnl');
    const returnElem = document.getElementById('metricReturnBadge');

    pnlElem.textContent = signedEur(pnlVal);
    pnlElem.className = `kpi-value ${pnlVal > 0 ? 'ui-gain' : pnlVal < 0 ? 'ui-loss' : ''}`;

    const up = totalReturnPct >= 0;
    const absReturn = investedCap > 0 && summary.total_gain_eur != null ? Number(summary.total_gain_eur) : null;
    const netDeposits = Number(summary.net_deposits_eur) || 0;
    returnElem.innerHTML = `<i class="fa-solid fa-arrow-${up ? 'up' : 'down'}-long text-xs" aria-hidden="true"></i>`
      + (absReturn !== null ? `<span class="ui-num">${signedEur(absReturn)}</span>` : '')
      + `<span class="ui-num">${up ? '+' : ''}${totalReturnPct.toFixed(2)}%</span>`
      + `<span class="text-slate-500 font-medium">all time</span>`
      + (Math.abs(netDeposits) >= 0.01
        ? `<span class="text-slate-500 font-medium ui-num" title="Deposits minus withdrawals, not counted as gain">· excl. ${eur(netDeposits)} deposits</span>`
        : '');
    returnElem.className = `flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-semibold ${up ? 'ui-gain' : 'ui-loss'}`;

    document.getElementById('metricPosCount').textContent = `${positions.length} open position${positions.length === 1 ? '' : 's'}`;
    const posCount = document.getElementById('positionsCount');
    if (posCount) posCount.textContent = String(positions.length);

    // Allocation bar (all screens) + doughnut (desktop)
    renderAllocationBar(Number(summary.cash_eur) || 0, positions, mineEur);

    // Update Chart
    if (allocationChart) {
      if (positions.length > 0) {
        const labels = ['Cash', ...positions.map(p => p.ticker)];
        const dataVals = [Number(summary.cash_eur) || 0, ...positions.map(p => Number(p.current_value_eur) || 0)];
        allocationChart.data.labels = labels;
        allocationChart.data.datasets[0].data = dataVals;
        allocationChart.data.datasets[0].backgroundColor = [ALLOC_CASH_COLOR, ...positions.map((_, i) => ALLOC_COLORS[i % ALLOC_COLORS.length])];
      } else {
        allocationChart.data.labels = ['Cash', 'Invested'];
        allocationChart.data.datasets[0].data = [Number(summary.cash_eur) || 0, 0];
        allocationChart.data.datasets[0].backgroundColor = [ALLOC_CASH_COLOR, ALLOC_COLORS[0]];
      }
      allocationChart.update();
    }

    const ownerSlider = (summary.trading_mode || '').toUpperCase() === 'LIVE' && (summary.broker || '').toUpperCase() === 'TRADING212';
    renderHoldings(holdingsReq, ownerSlider);

    // Render position rows (tap a row to see entry, stops and the sell button)
    const container = document.getElementById('positionsContainer');
    if (positions.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No open positions yet</p>
          <p class="empty-body">The bot buys during market hours when a stock scores high enough. Run a scan to find candidates now.</p>
          <button type="button" onclick="triggerQuickScan()" class="ui-btn ui-btn-primary ui-btn-sm"><i class="fa-solid fa-satellite-dish" aria-hidden="true"></i> Run scan</button>
        </div>`;
      return;
    }

    container.innerHTML = positions.map((pos, i) => {
      const pnl = Number(pos.unrealized_pnl_eur) || 0;
      const pnlPct = Number(pos.unrealized_pnl_pct);
      const tone = pnl > 0 ? 'ui-gain' : pnl < 0 ? 'ui-loss' : 'ui-muted';
      const arrow = pnl > 0 ? '▲' : pnl < 0 ? '▼' : '';
      const pctText = Number.isFinite(pnlPct) ? `${arrow} ${Math.abs(pnlPct).toFixed(2)}%` : esc(pos.unrealized_pnl_pct);
      const mono = String(pos.ticker || '').split('.')[0].slice(0, 4);
      const color = ALLOC_COLORS[i % ALLOC_COLORS.length];
      const isOpen = openPositions.has(pos.ticker);
      const shares = `${esc(pos.shares)} share${Number(pos.shares) === 1 ? '' : 's'}`;
      const weight = totalVal > 0 ? `${((Number(pos.current_value_eur) || 0) / totalVal * 100).toFixed(0)}%<span class="sr-only"> of portfolio</span> · ` : '';
      return `
        <div class="pos-row${isOpen ? ' open' : ''}">
          <button type="button" class="pos-head" data-action="toggle-position" data-ticker="${esc(pos.ticker)}" aria-expanded="${isOpen ? 'true' : 'false'}">
            <span class="pos-mono" style="color:${color}" aria-hidden="true">${esc(mono)}</span>
            <span class="flex-1 min-w-0">
              <span class="block font-semibold text-base text-slate-50 truncate">${esc(pos.ticker)}</span>
              <span class="block text-xs text-slate-500 truncate">${weight}${shares}${pos.name ? ` · ${esc(pos.name)}` : ''}</span>
            </span>
            <span class="text-right shrink-0">
              <span class="block ui-num font-semibold text-[15px] text-slate-50">${eur(pos.current_value_eur)}</span>
              <span class="block ui-num text-xs font-semibold ${tone}"><span class="max-md:hidden">${signedEur(pnl)} · </span>${pctText}</span>
            </span>
            <i class="fa-solid fa-chevron-down pos-chevron" aria-hidden="true"></i>
          </button>
          <div class="pos-detail">
            <div class="grid grid-cols-2 gap-x-4 gap-y-2 pt-2">
              <div class="kv"><span>Entry</span><span>${esc(pos.entry_price_native)} ${esc(pos.currency)}</span></div>
              <div class="kv"><span>Now</span><span>${esc(pos.current_price_native)} ${esc(pos.currency)}</span></div>
              <div class="kv"><span>Stop</span><span class="ui-loss">${pos.stop_loss_native ? esc(pos.stop_loss_native) : '–'}</span></div>
              <div class="kv"><span>Target</span><span class="ui-gain">${pos.take_profit_native ? esc(pos.take_profit_native) : '–'}</span></div>
              <div class="kv col-span-2"><span>P&amp;L</span><span class="${tone}">${signedEur(pnl)}</span></div>
            </div>
            ${ownerSlider ? ownerSwitchHtml(pos.ticker, 'bot') : ''}
            <button type="button" class="ui-btn ui-btn-quiet ui-btn-sm w-full mt-2" data-action="open-ticker" data-ticker="${esc(pos.ticker)}" data-source="" aria-label="Chart for ${esc(pos.ticker)}"><i class="fa-solid fa-chart-line" aria-hidden="true"></i> Chart</button>
            <button type="button" data-action="close-position" data-ticker="${esc(pos.ticker)}" class="ui-btn ui-btn-danger w-full mt-3" aria-label="Sell position ${esc(pos.ticker)}">
              <i class="fa-solid fa-arrow-right-from-bracket"></i> Sell position
            </button>
          </div>
        </div>
      `;
    }).join('');

    staggerChildren(container);
  } catch (e) {
    console.error('Failed to load portfolio:', e);
  }
}

// 3. Load Recommendations (Daily Stock Advice)
async function loadRecommendations() {
  try {
    const res = await fetch('/api/recommendations');
    const reports = await res.json();
    adviceReportsCache = Array.isArray(reports) ? reports : [];
    // Highest conviction first; cards read reports back by index, so sort the cache itself.
    adviceReportsCache.sort((a, b) => (Number(b.consensus_score) || 0) - (Number(a.consensus_score) || 0));
    const container = document.getElementById('adviceContainer');
    const countEl = document.getElementById('adviceCount');
    if (countEl) countEl.textContent = adviceReportsCache.length ? String(adviceReportsCache.length) : '';

    if (adviceReportsCache.length === 0) {
      container.innerHTML = `
        <div class="col-span-full ui-card ui-card-pad adv-prose">
          No advice yet today. Tap <strong class="text-slate-50">Run Scan</strong> in the top bar to find candidates and run the committee.
        </div>`;
      return;
    }

    container.innerHTML = adviceReportsCache.map((r, idx) => {
      const source = scoreSourceBadge(r.score_source || (r.raw_signals && r.raw_signals.score_source));
      const sub = r.name || r.created_at || '';

      return `
        <article class="stock-card ui-card ui-card-pad flex flex-col gap-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-[17px] font-bold text-slate-50">${tickerLink(r.ticker, `report:${r.ticker}`)}</span>
                ${adviceVerdictBadge(r.action)}
              </div>
              ${sub ? `<div class="ui-faint text-[13px] truncate ${r.name ? '' : 'ui-num'}">${esc(sub)}</div>` : ''}
            </div>
            ${adviceScoreBlock(r.consensus_score, 'Conviction', source)}
          </div>

          ${r.summary ? `<p class="adv-prose adv-clamp">${esc(r.summary)}</p>` : ''}

          <div class="ui-inset px-3.5 py-2.5 space-y-1.5">
            <div class="kv"><span>Target</span><span class="${r.target_price ? 'ui-gain' : ''}">${r.target_price ? `▲ ${esc(r.target_price)}` : '—'}</span></div>
            <div class="kv"><span>Stop loss</span><span class="${r.stop_loss ? 'ui-loss' : ''}">${r.stop_loss ? `▼ ${esc(r.stop_loss)}` : '—'}</span></div>
          </div>

          <div class="grid ${F.isEnabled('debate') ? 'grid-cols-3' : 'grid-cols-2'} gap-2 mt-auto">
            <button type="button" data-action="buy" data-ticker="${esc(r.ticker)}" class="ui-btn ui-btn-primary !px-2 !gap-1.5" title="Market buy now">
              <i class="fa-solid fa-cart-shopping"></i> Buy
            </button>
            <button type="button" data-action="arm-trigger" data-ticker="${esc(r.ticker)}" class="ui-btn !px-2 !gap-1.5" title="Arm an AI-calculated limit trigger">
              <i class="fa-solid fa-crosshairs"></i> Trigger
            </button>
            <button type="button" data-action="inspect" data-idx="${idx}" class="ui-btn !px-2 !gap-1.5${F.isEnabled('debate') ? '' : ' hidden'}" title="Open the committee debate">
              <i class="fa-solid fa-comments"></i> Debate
            </button>
          </div>
        </article>
      `;
    }).join('');

    staggerChildren(container);
  } catch (e) {
    console.error('Failed to load recommendations:', e);
  }
}

// 4. Load Market Screener Candidates
async function loadScreener() {
  const container = document.getElementById('screenerContainer');
  container.innerHTML = `
    <div class="col-span-full ui-card ui-card-pad adv-prose flex items-center gap-3">
      <i class="fa-solid fa-spinner fa-spin ui-faint"></i>
      Scanning for momentum setups...
    </div>`;

  try {
    const res = await fetch(`/api/screener?market=${currentScreenerMarket}`);
    const candidates = await res.json();

    if (!Array.isArray(candidates) || candidates.length === 0) {
      container.innerHTML = `<div class="col-span-full ui-card ui-card-pad adv-prose">No qualifying candidates right now.</div>`;
      return;
    }

    container.innerHTML = candidates.map(c => {
      const ret = Number(c.return_5d_pct);
      const retUp = Number.isFinite(ret) && ret >= 0;
      const retHtml = Number.isFinite(ret)
        ? `<span class="${retUp ? 'ui-gain' : 'ui-loss'}">${retUp ? '▲ +' : '▼ '}${esc(c.return_5d_pct)}%</span>`
        : '<span>—</span>';
      const hot = Number(c.rsi) > 70;
      return `
      <article class="stock-card ui-card ui-card-pad flex flex-col gap-3">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0 space-y-1">
            <div class="text-[17px] font-bold text-slate-50">${tickerLink(c.ticker)}</div>
            <div class="ui-faint text-[13px] truncate">${esc(c.name)}</div>
          </div>
          ${adviceScoreBlock(c.pre_score, 'Scanner')}
        </div>

        <div>
          ${c.setup_type ? `<div class="text-[14px] font-semibold text-slate-50">${esc(c.setup_type)}</div>` : ''}
          ${c.notes ? `<p class="adv-prose adv-clamp mt-0.5">${esc(c.notes)}</p>` : ''}
        </div>

        <div class="ui-inset px-3.5 py-2.5 space-y-1.5">
          <div class="kv"><span>Price</span><span>${advicePrice(c.price, c.currency)}</span></div>
          <div class="kv"><span>RSI (14)</span><span${hot ? ' style="color: var(--warn)"' : ''}>${esc(c.rsi)}${hot ? ' · overbought' : ''}</span></div>
          <div class="kv"><span>5 days</span>${retHtml}</div>
        </div>

        <button type="button" data-action="analyze" data-ticker="${esc(c.ticker)}" class="ui-btn ui-btn-primary w-full mt-auto${F.isEnabled('debate') ? '' : ' hidden'}">
          <i class="fa-solid fa-users-viewfinder"></i> Analyze
        </button>
      </article>
    `;
    }).join('');

  } catch (e) {
    console.error('Failed to load screener:', e);
    container.innerHTML = `<div class="col-span-full ui-card ui-card-pad adv-prose ui-loss">Failed to load the screener.</div>`;
  }
}

function filterScreener(market) {
  currentScreenerMarket = market;
  document.querySelectorAll('.screener-filter-btn').forEach(b => {
    const active = b.dataset.market === market;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  loadScreener();
}

// 5. On Demand Analysis & Debate Inspector
async function runOnDemandAnalysis() {
  const input = document.getElementById('analyzeInput');
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) {
    showToast('Please enter a valid stock ticker.', true);
    return;
  }
  await analyzeDirect(ticker);
}

// Committee runs take minutes and the server runs them as background jobs, so the page can be swiped away
// and come back: the job is remembered in localStorage and polled until it is done.
const JOB_KEY = 'pendingCommitteeJob';
const JOB_LOST_MSG = 'Server restarted, the committee run was lost. Please try again.';
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Resolves {status, data, jobId} like a plain request would. Pass `resume` ({job_id, ...}) to skip the POST.
async function runJob(url, body, meta, resume = null) {
  let job = resume;
  if (!job) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (res.status !== 202) {
      const raw = await res.text();
      let data = null;
      try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
      return { status: res.status, data, raw };
    }
    job = { ...meta, job_id: (await res.json()).job_id };
    localStorage.setItem(JOB_KEY, JSON.stringify(job));
  }
  for (;;) {
    try {
      const res = await fetch(`/api/jobs/${job.job_id}`);
      if (res.status === 404) {
        localStorage.removeItem(JOB_KEY);
        return { status: 404, data: { detail: JOB_LOST_MSG }, lost: true };
      }
      if (res.ok) {
        const j = await res.json();
        if (j.status === 'done') {
          localStorage.removeItem(JOB_KEY);
          return { status: j.status_code, data: j.body, jobId: job.job_id };
        }
      }
    } catch (e) { /* app was backgrounded or offline: keep polling */ }
    await sleep(3000);
  }
}

function resumePendingJob() {
  let job = null;
  try { job = JSON.parse(localStorage.getItem(JOB_KEY)); } catch (e) { /* ignore */ }
  if (!job || !job.job_id) return;
  if (job.kind === 'arm') {
    switchTab('triggers');
    armTriggerFlow(job.ticker, { resume: job, button: document.getElementById('armTriggerBtn') });
  }
  else analyzeDirect(job.ticker, job);
}

// Last finished debate, so "Arm trigger" for that ticker reuses its committee report (server enforces max age)
let lastDebateJob = null;

async function analyzeDirect(ticker, resume = null) {
  switchTab('debate');
  document.getElementById('analyzeInput').value = ticker;
  const btn = document.getElementById('analyzeBtn');
  const outputArea = document.getElementById('debateOutputArea');

  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Convening...`;
  btn.disabled = true;

  try {
    const { status, data, jobId } = await runJob('/api/analyze', { ticker: ticker }, { kind: 'analyze', ticker }, resume);

    if (status !== 200) {
      showToast((data && data.detail) || 'Analysis failed', true);
      return;
    }

    lastDebateJob = jobId ? { ticker: data.ticker || ticker, jobId } : null;
    renderDebateReport(data);
    outputArea.classList.remove('hidden');
    showToast(`Committee completed review for ${ticker}!`);

  } catch (e) {
    console.error('Analysis error:', e);
    showToast('Network error during analysis.', true);
  } finally {
    btn.innerHTML = `<i class="fa-solid fa-users-viewfinder"></i> Convene debate`;
    btn.disabled = false;
  }
}

function renderDebateReport(report) {
  // Verdict Card
  const risk = report.risk || {};
  const rr = risk.risk_reward_ratio;
  document.getElementById('verdictCard').innerHTML = `
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-[22px] font-bold text-slate-50 leading-tight">${tickerLink(report.ticker, `report:${report.ticker}`)}</span>
          ${adviceVerdictBadge(report.action)}
        </div>
        ${report.name ? `<div class="ui-faint text-[13px] truncate">${esc(report.name)}</div>` : ''}
      </div>
      ${adviceScoreBlock(report.consensus_score, 'Consensus', scoreSourceBadge(report.score_source))}
    </div>

    ${report.summary ? `<p class="adv-prose">${esc(report.summary)}</p>` : ''}

    <div class="ui-inset px-3.5 py-2.5 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1.5">
      <div class="kv"><span>Price</span><span>${advicePrice(report.price, report.currency)}</span></div>
      <div class="kv"><span>Target</span><span class="${risk.take_profit_price ? 'ui-gain' : ''}">${risk.take_profit_price ? `▲ ${esc(risk.take_profit_price)}` : '—'}</span></div>
      <div class="kv"><span>Stop loss</span><span class="${risk.stop_loss_price ? 'ui-loss' : ''}">${risk.stop_loss_price ? `▼ ${esc(risk.stop_loss_price)}` : '—'}</span></div>
      <div class="kv"><span>Risk / reward</span><span>${rr !== null && rr !== undefined && rr !== '' ? `${esc(rr)} : 1` : '—'}</span></div>
    </div>

    <div class="grid grid-cols-2 gap-2">
      <button type="button" data-action="buy" data-ticker="${esc(report.ticker)}" class="ui-btn ui-btn-primary" title="Market buy now">
        <i class="fa-solid fa-cart-shopping"></i> Buy
      </button>
      <button type="button" data-action="arm-trigger" data-ticker="${esc(report.ticker)}" class="ui-btn" title="Arm an AI-calculated limit trigger">
        <i class="fa-solid fa-crosshairs"></i> Arm trigger
      </button>
    </div>
  `;

  // Specialist agents: badge text via textContent (never innerHTML), arrow/word plus colour
  const setBadge = (id, text, tone, mark) => {
    const el = document.getElementById(id);
    if (!el) return;
    const word = String(text || '');
    const arrow = mark !== undefined ? mark : (tone === 'gain' ? '▲ ' : (tone === 'loss' ? '▼ ' : ''));
    el.textContent = word ? arrow + word : '';
    el.className = `ui-badge${tone ? ' ui-badge-' + tone : ''}${word ? '' : ' hidden'}`;
  };

  const t = report.technical || {};
  setBadge('techBiasBadge', t.bias, t.bias === 'BULLISH' ? 'gain' : (t.bias === 'BEARISH' ? 'loss' : ''));
  document.getElementById('techSummaryText').textContent = t.analysis_summary || '';

  const f = report.fundamental || {};
  setBadge('fundValuationBadge', f.valuation, f.valuation === 'UNDERVALUED' ? 'gain' : (f.valuation === 'OVERVALUED' ? 'loss' : ''));
  document.getElementById('fundSummaryText').textContent = f.analysis_summary || '';

  const s = report.sentiment || {};
  setBadge('sentMoodBadge', s.mood, s.mood === 'POSITIVE' ? 'gain' : (s.mood === 'NEGATIVE' ? 'loss' : ''));
  document.getElementById('sentSummaryText').textContent = s.sentiment_summary || '';

  const r = report.risk || {};
  setBadge('riskApprovedBadge', r.approved ? 'APPROVED' : 'REJECTED', r.approved ? 'gain' : 'loss', r.approved ? '✓ ' : '✕ ');
  document.getElementById('riskSummaryText').textContent = r.risk_summary || '';

  // Transcript
  document.getElementById('debateTranscriptBox').textContent = report.debate_transcript;
}

function inspectReport(idx) {
  const r = adviceReportsCache[idx];
  if (!r) return;
  switchTab('debate');
  document.getElementById('analyzeInput').value = r.ticker;
  document.getElementById('debateOutputArea').classList.remove('hidden');
  renderDebateReport({
    ticker: r.ticker,
    action: r.action,
    consensus_score: r.consensus_score,
    score_source: r.score_source || (r.raw_signals && r.raw_signals.score_source),
    summary: r.summary,
    // Saved advice has no live price or R:R; show a dash instead of an estimate
    price: null,
    currency: '',
    technical: { bias: '', analysis_summary: r.technical_summary || 'Analysis complete' },
    fundamental: { valuation: '', analysis_summary: r.fundamental_summary || 'Valuation recorded' },
    sentiment: { mood: '', sentiment_summary: r.sentiment_summary || 'Sentiment factored' },
    risk: { approved: true, take_profit_price: r.target_price, stop_loss_price: r.stop_loss, risk_reward_ratio: null, risk_summary: r.risk_summary || 'Risk guardrails applied' },
    debate_transcript: r.debate_transcript || 'Full transcript archived.'
  });
  const out = document.getElementById('debateOutputArea');
  if (out && out.scrollIntoView) out.scrollIntoView({ block: 'start' });
}

// 6. Trade History
// Deposits and withdrawals on the live account
async function loadCashFlows() {
  const list = document.getElementById('cashFlowList');
  if (!list) return;
  try {
    const res = await fetch('/api/cash-flows');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const flows = data.flows || [];
    const netEl = document.getElementById('cashFlowNet');
    if (netEl) netEl.textContent = flows.length ? `net ${eur(data.net_deposits_eur)}` : '';
    if (!flows.length) {
      list.innerHTML = '<div class="ui-card ui-card-pad text-[13px] ui-muted">No deposits or withdrawals recorded.</div>';
      return;
    }
    list.innerHTML = flows.map(f => {
      const isIn = Number(f.amount_eur) >= 0;
      return `
        <div class="ui-card ui-card-pad flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="ui-num font-semibold ${isIn ? 'ui-gain' : 'ui-loss'}">${isIn ? 'Deposit' : 'Withdrawal'} ${esc(signedEur(f.amount_eur))}</div>
            <div class="text-xs ui-faint ui-num">${esc(f.timestamp)} · ${f.source === 'manual' ? 'added by you' : f.source === 'broker' ? 'from broker' : 'detected'}</div>
          </div>
          <button type="button" onclick="removeCashFlow(${Number(f.id)})" class="ui-btn ui-btn-quiet ui-icon-btn shrink-0" aria-label="Remove" title="Remove"><i class="fa-solid fa-trash-can"></i></button>
        </div>`;
    }).join('');
  } catch (e) {
    console.error('Error loading deposits', e);
  }
}

async function addCashFlow(event) {
  event.preventDefault();
  const input = document.getElementById('cashFlowAmount');
  const amount = Number(input.value);
  if (!amount) { showToast('Enter an amount, negative for a withdrawal.', true); return; }
  const label = amount > 0 ? `deposit of ${eur(amount)}` : `withdrawal of ${eur(-amount)}`;
  if (!confirm(`Record a ${label}? It will no longer count as gain or loss.`)) return;
  try {
    const res = await fetch('/api/cash-flows', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount_eur: amount })
    });
    if (!res.ok) throw new Error((await res.json()).detail || `HTTP ${res.status}`);
    input.value = '';
    showToast('Recorded.');
    loadCashFlows(); loadPortfolio();
  } catch (e) {
    showToast(`Could not record it: ${e.message}`, true);
  }
}

async function removeCashFlow(id) {
  if (!confirm('Remove this entry? The amount will count as gain or loss again.')) return;
  try {
    const res = await fetch(`/api/cash-flows/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    showToast('Removed.');
    loadCashFlows(); loadPortfolio();
  } catch (e) {
    showToast(`Could not remove it: ${e.message}`, true);
  }
}

async function loadTrades() {
  try {
    const res = await fetch('/api/trades');
    const trades = await res.json();
    const tbody = document.getElementById('tradeHistoryTableBody');
    const countEl = document.getElementById('tradeHistoryCount');
    if (countEl) countEl.textContent = trades.length ? String(trades.length) : '';

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center py-8 ui-faint">No trades executed yet.</td></tr>`;
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const isBuy = t.order_type === 'BUY';
      const typeBadge = isBuy
        ? '<span class="ui-badge ui-badge-accent">BUY</span>'
        : '<span class="ui-badge">SELL</span>';

      let pnlText = '<span class="ui-faint">-</span>';
      if (t.realized_pnl_eur !== null && t.realized_pnl_eur !== undefined) {
        const pnl = Number(t.realized_pnl_eur) || 0;
        const up = pnl >= 0;
        pnlText = `<span class="ui-num font-semibold ${up ? 'ui-gain' : 'ui-loss'}">${up ? '▲ +' : '▼ -'}${esc(eur(Math.abs(pnl)))} (${esc(t.realized_pnl_pct)}%)</span>`;
      }

      return `
        <tr>
          <td data-label="Date" class="py-3 px-4 ui-num ui-faint whitespace-nowrap">${esc(t.executed_at)}</td>
          <td data-label="Type" class="py-3 px-4">${typeBadge}</td>
          <td data-label="Asset" class="py-3 px-4 font-bold" style="color:var(--text)">${tickerLink(t.ticker)}</td>
          <td data-label="Shares" class="py-3 px-4 ui-num">${esc(t.shares)}</td>
          <td data-label="Price" class="py-3 px-4 ui-num"><span>${esc(t.price_native)} <span class="ui-faint text-xs">${esc(t.currency)}</span></span></td>
          <td data-label="Total (EUR)" class="py-3 px-4 ui-num font-semibold" style="color:var(--text)">€${num(t.total_eur)}</td>
          <td data-label="Fee (€)" class="py-3 px-4 ui-num ui-faint">€${(t.fee_eur !== undefined && t.fee_eur !== null) ? num(t.fee_eur) : '0.00'}</td>
          <td data-label="Realized PnL" class="py-3 px-4">${pnlText}</td>
          <td data-label="Rationale" class="cell-wide py-3 px-4 truncate max-w-xs" title="${esc(t.rationale)}">${esc(t.rationale)}</td>
        </tr>
      `;
    }).join('');

  } catch (e) {
    console.error('Failed to load trades:', e);
  }
}

// 7. Trading Actions
function isLiveMode() {
  return (lastBrokerStatus && String(lastBrokerStatus.trading_mode || '').toUpperCase() === 'LIVE') || lastSavedTradingMode === 'LIVE';
}

async function executePaperBuy(ticker) {
  const modeLabel = isLiveMode() ? 'LIVE (real money)' : 'paper';
  if (!confirm(`Execute ${modeLabel} BUY order for ${ticker} at market price?`)) return;

  try {
    const res = await fetch('/api/trade/buy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });

    const data = await res.json();
    if (res.ok) {
      showToast(data.message);
      loadPortfolio();
    } else {
      showToast(data.detail || 'Buy order failed.', true);
    }
  } catch (e) {
    showToast('Network error executing trade.', true);
  }
}

async function closePosition(ticker) {
  const modeLabel = isLiveMode() ? 'LIVE (real money)' : 'paper';
  if (!confirm(`SELL: close the whole ${modeLabel} position in ${ticker} at market price?`)) return;

  try {
    const res = await fetch('/api/trade/sell', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker, reason: 'Manual User Close' })
    });

    const data = await res.json();
    if (res.ok) {
      showToast(data.message);
      loadPortfolio();
    } else {
      showToast(data.detail || 'Close position failed.', true);
    }
  } catch (e) {
    showToast('Network error closing position.', true);
  }
}

async function syncAccountWithBroker() {
  showToast('⏳ Synchronizing portfolio & balances with broker...');
  try {
    const res = await fetch('/api/portfolio/sync', { method: 'POST' });
    if (res.ok) {
      showToast('Broker account state synced successfully.');
      await loadPortfolio();
      await loadBrokerStatus();
    } else {
      showToast('Failed to sync account with broker.', true);
    }
  } catch (e) {
    showToast('Network error syncing with broker.', true);
  }
}

// Broker Connection Status & Diagnostics
let lastBrokerStatus = null;

async function pingBrokerStatus(force = false) {
  return await loadBrokerStatus(force);
}

async function loadBrokerStatus(force = false) {
  const badge = document.getElementById('brokerStatusBadge');
  const dot = document.getElementById('brokerStatusDot');
  const text = document.getElementById('brokerStatusText');
  const mobDot = document.getElementById('mobileBrokerStatusDot');
  const mobText = document.getElementById('mobileBrokerStatusText');
  const pingBtn = document.getElementById('settingsPingBtn');
  const pingIcon = document.getElementById('settingsPingIcon');

  if (force) {
    if (dot) dot.className = 'ui-dot bg-warn animate-pulse';
    if (text) text.textContent = 'Broker: Probing...';
    if (mobDot) mobDot.className = 'ui-dot bg-warn animate-pulse';
    if (mobText) mobText.textContent = 'Checking broker...';
    if (pingIcon) pingIcon.classList.add('fa-spin');
    if (pingBtn) pingBtn.disabled = true;
  }

  try {
    const url = force ? '/api/broker/ping' : '/api/broker/status';
    const method = force ? 'POST' : 'GET';
    const res = await fetch(url, { method });
    let data;
    if (res.ok) {
      data = await res.json();
    } else {
      data = {
        connected: false,
        broker: lastBrokerStatus?.broker || 'BROKER',
        trading_mode: lastBrokerStatus?.trading_mode || 'OFFLINE',
        status: 'OFFLINE',
        message: 'Broker status endpoint awaiting bot restart (HTTP ' + res.status + ').'
      };
    }
    lastBrokerStatus = data;
    if (typeof data.kill_switch_active === 'boolean') {
      killSwitchActive = data.kill_switch_active;
      renderKillSwitch();
    }

    const isConnected = !!data.connected;
    const brokerName = data.broker || 'BROKER';
    const isLive = (data.trading_mode || '').toUpperCase() === 'LIVE';

    // 1. Update Header Badge & Mobile Pill
    if (mobDot && mobText) {
      if (isConnected) {
        mobDot.className = 'ui-dot bg-gain';
        mobText.textContent = brokerName === 'PAPER' ? 'Simulated broker' : `${brokerName} online`;
      } else {
        mobDot.className = 'ui-dot bg-loss';
        mobText.textContent = `${brokerName} offline`;
      }
    }

    if (badge && dot && text) {
      if (isConnected) {
        dot.className = 'ui-dot bg-gain';
        text.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
        badge.className = 'hidden';
        badge.title = `${brokerName} Connected (${data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms + 'ms' : 'Ready'})\nClick to test connection`;
      } else {
        dot.className = 'ui-dot bg-loss';
        text.textContent = `${brokerName}: Offline`;
        badge.className = 'hidden';
        badge.title = `${brokerName} Disconnected: ${data.message || 'Unreachable'}\nClick to retest`;
      }
    }

    // 2. Update Portfolio Tab Disconnect Alert Banner & Connected Strip
    const banner = document.getElementById('brokerDisconnectBanner');
    const bannerDetail = document.getElementById('brokerDisconnectDetail');
    const bannerBroker = document.getElementById('bannerBrokerName');
    const strip = document.getElementById('brokerConnectedStrip');
    const stripBroker = document.getElementById('stripBrokerName');
    const stripEndpoint = document.getElementById('stripBrokerEndpoint');
    const stripLatency = document.getElementById('stripBrokerLatency');
    const stripAccount = document.getElementById('stripBrokerAccount');

    if (banner) {
      if (isLive && !isConnected) {
        banner.classList.remove('hidden');
        if (bannerBroker) bannerBroker.textContent = brokerName;
        if (bannerDetail) {
          bannerDetail.textContent = data.message || `${brokerName} gateway is unreachable. Real-money orders cannot execute.`;
        }
      } else {
        banner.classList.add('hidden');
      }
    }

    if (strip) {
      if (isLive && isConnected) {
        strip.classList.remove('hidden');
        if (stripBroker) stripBroker.textContent = brokerName;
        if (stripEndpoint) stripEndpoint.textContent = data.host && data.port ? `${data.host}:${data.port}` : 'Connected';
        if (stripLatency) stripLatency.textContent = data.latency_ms !== null && data.latency_ms !== undefined ? `${data.latency_ms} ms` : 'Ready';
        if (stripAccount) stripAccount.textContent = data.account_id || (data.accounts && data.accounts.length ? data.accounts.join(', ') : 'Active');
      } else {
        strip.classList.add('hidden');
      }
    }

    // 3. Update Settings Diagnostics Card (if present)
    const diagName = document.getElementById('diagBrokerName');
    const diagDot = document.getElementById('diagStatusDot');
    const diagText = document.getElementById('diagStatusText');
    const diagEndpoint = document.getElementById('diagBrokerEndpoint');
    const diagLatency = document.getElementById('diagBrokerLatency');
    const diagMsg = document.getElementById('diagBrokerMessage');

    if (diagName) {
      diagName.textContent = brokerName === 'TRADING212' ? 'Trading 212' : (brokerName === 'ALPACA' ? 'Alpaca Markets' : 'Paper Trading Engine');
    }
    if (diagDot && diagText) {
      if (isConnected) {
        diagDot.className = 'ui-dot bg-gain';
        diagText.textContent = 'Connected (Online)';
        diagText.className = 'text-gain font-bold';
      } else {
        diagDot.className = 'ui-dot bg-loss';
        diagText.textContent = 'Disconnected (Offline)';
        diagText.className = 'text-loss font-bold';
      }
    }
    if (diagEndpoint) {
      diagEndpoint.textContent = data.host && data.port ? `${data.host}:${data.port}` : (data.broker === 'PAPER' ? 'Local Memory DB' : 'Cloud REST API');
    }
    if (diagLatency) {
      diagLatency.textContent = data.latency_ms !== null && data.latency_ms !== undefined ? `${data.latency_ms} ms` : 'N/A';
    }
    if (diagMsg) {
      diagMsg.textContent = data.message || (isConnected ? 'Connection verified.' : 'Broker offline.');
      diagMsg.className = isConnected ? 'text-gain font-mono text-[11px] leading-relaxed break-words' : 'text-loss font-mono text-[11px] leading-relaxed break-words';
    }

    if (force) {
      if (isConnected) {
        showToast(`${brokerName} Connection Online (${data.latency_ms || 0}ms)`);
      } else {
        showToast(`${brokerName} Connection Offline: ${data.error || 'Check IB Gateway / 2FA'}`, true);
      }
    }
  } catch (err) {
    console.error('Failed to load broker status:', err);
    const mobErrDot = document.getElementById('mobileBrokerStatusDot');
    const mobErrText = document.getElementById('mobileBrokerStatusText');
    if (mobErrDot) mobErrDot.className = 'ui-dot bg-loss';
    if (mobErrText) mobErrText.textContent = 'Broker error';
    if (badge && dot && text) {
      dot.className = 'ui-dot bg-loss';
      text.textContent = 'Broker: Error';
      badge.className = 'hidden';
    }
    if (force) showToast('Failed to contact server for broker status', true);
  } finally {
    if (pingIcon) pingIcon.classList.remove('fa-spin');
    if (pingBtn) pingBtn.disabled = false;
  }
}

// Market Status Loader
async function loadMarketStatus() {
  try {
    const msRes = await fetch('/api/market-status');
    const msData = await msRes.json();
    const badge = document.getElementById('marketStatusBadge');
    const textEl = document.getElementById('marketStatusText');
    if (!badge || !textEl) return;

    const mobMarketText = document.getElementById('mobileMarketStatusText');
    const mobMarketDot = document.getElementById('mobileMarketDot');
    const euOpen = !!msData.eu_market?.is_open;
    const usOpen = !!msData.us_market?.is_open;
    if (mobMarketDot) mobMarketDot.className = (euOpen || usOpen) ? 'ui-dot bg-gain' : 'ui-dot';
    if (msData.is_weekend) {
      textEl.textContent = 'Weekend (Markets Closed)';
      badge.className = 'hidden xl:flex ui-chip text-warn';
      if (mobMarketText) mobMarketText.textContent = 'Weekend · markets closed';
    } else if (msData.eu_market.is_open || msData.us_market.is_open) {
      textEl.textContent = `🇪🇺 EU: ${msData.eu_market.status} | 🇺🇸 US: ${msData.us_market.status}`;
      badge.className = 'hidden xl:flex ui-chip text-gain';
      if (mobMarketText) mobMarketText.textContent = `EU ${euOpen ? 'open' : 'closed'} · US ${usOpen ? 'open' : 'closed'}`;
    } else {
      textEl.textContent = '🇪🇺 EU: CLOSED | 🇺🇸 US: CLOSED';
      badge.className = 'hidden xl:flex ui-chip';
      if (mobMarketText) mobMarketText.textContent = 'EU closed · US closed';
    }
  } catch (err) {
    console.debug('Failed to load market status:', err);
  }
}

// 8. Settings Management
async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();

    document.getElementById('settingAutoBuy').checked = data.auto_buy_enabled;
    renderAutoBuyStatus(data.auto_buy_enabled);

    document.getElementById('settingMinScore').value = data.min_consensus_score;
    document.getElementById('settingMinScore').dataset.loaded = String(data.min_consensus_score);
    document.getElementById('consensusScoreLabel').textContent = `${data.min_consensus_score} / 100`;

    // The learner can move both scores in Bot mode, so always show the server values
    const trigInput = document.getElementById('settingTriggerScore');
    const trigLabel = document.getElementById('triggerScoreLabel');
    if (trigInput && data.trigger_min_score !== undefined) {
      trigInput.value = data.trigger_min_score;
      trigInput.dataset.loaded = String(data.trigger_min_score);
    }
    if (trigLabel && data.trigger_min_score !== undefined) trigLabel.textContent = `${data.trigger_min_score} / 100`;
    renderScoreAutoTune(data.score_auto_tune !== false);

    const posSizePct = (data.position_size_pct * 100).toFixed(1);
    document.getElementById('settingPosSize').value = posSizePct;
    document.getElementById('posSizeLabel').textContent = `${posSizePct}%`;

    if (data.max_positions !== undefined) {
      const mpInput = document.getElementById('settingMaxPositions');
      const mpLabel = document.getElementById('maxPositionsLabel');
      if (mpInput) mpInput.value = data.max_positions;
      if (mpLabel) mpLabel.textContent = `${data.max_positions} slots`;
    }

    const capitalUsagePct = data.max_capital_usage_pct ?? 100;
    const capitalCapEur = data.max_deployable_capital_eur;
    if (document.getElementById('settingCapitalUsagePct')) {
      document.getElementById('settingCapitalUsagePct').value = capitalUsagePct;
      updateCapitalUsageDisplay(capitalUsagePct);
    }
    if (document.getElementById('settingMaxDeployableEur')) {
      document.getElementById('settingMaxDeployableEur').value = (capitalCapEur !== null && capitalCapEur !== undefined) ? capitalCapEur : '';
    }
    recalcCapitalPreview();

    const tgBadge = document.getElementById('telegramStatusBadge');
    if (tgBadge) {
      if (data.has_telegram) {
        tgBadge.textContent = 'Connected';
        tgBadge.className = 'ui-badge ui-badge-gain shrink-0';
      } else {
        tgBadge.textContent = 'Not set up';
        tgBadge.className = 'ui-badge shrink-0';
      }
    }

    const aiBadge = document.getElementById('aiEngineStatusBadge');
    if (aiBadge) {
      if (data.has_antigravity) {
        aiBadge.textContent = 'Antigravity';
        aiBadge.className = 'ui-badge ui-badge-gain shrink-0';
      } else if (data.has_gemini_key) {
        aiBadge.textContent = 'Gemini API';
        aiBadge.className = 'ui-badge ui-badge-gain shrink-0';
      } else {
        aiBadge.textContent = 'Ready';
        aiBadge.className = 'ui-badge shrink-0';
      }
    }

    if (document.getElementById('settingTradingMode') && data.trading_mode) {
      document.getElementById('settingTradingMode').value = data.trading_mode.toUpperCase();
      lastSavedTradingMode = data.trading_mode.toUpperCase();
    }

    if (document.getElementById('settingMarketHours')) {
      document.getElementById('settingMarketHours').checked = data.enforce_market_hours !== false;
    }

    await loadMarketStatus();
    await loadBrokerStatus();
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
}

const SCORE_TUNE_HELP = {
  true: 'The bot adjusts both scores, starting from your values.',
  false: 'Your values stay; the bot leaves both scores alone.'
};

function renderScoreAutoTune(on) {
  document.querySelectorAll('[data-score-tune]').forEach(b => {
    const active = b.dataset.scoreTune === String(on);
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const help = document.getElementById('scoreControlHelp');
  if (help) help.textContent = SCORE_TUNE_HELP[on];
}

function setScoreAutoTune(on) {
  const current = document.querySelector('[data-score-tune="true"]');
  if (current && (current.getAttribute('aria-pressed') === 'true') === on) return;
  renderScoreAutoTune(on);
  saveSettings();
}

function updateCapitalUsageDisplay(val) {
  const lbl = document.getElementById('capitalUsagePctLabel');
  if (lbl) {
    lbl.textContent = `${val}%`;
  }
  recalcCapitalPreview();
}

function recalcCapitalPreview() {
  const pctEl = document.getElementById('settingCapitalUsagePct');
  const capEl = document.getElementById('settingMaxDeployableEur');
  if (!pctEl) return;

  const pct = parseFloat(pctEl.value) || 100;
  const capRaw = capEl ? capEl.value.trim() : '';
  const cap = (capRaw !== '' && !isNaN(parseFloat(capRaw)) && parseFloat(capRaw) > 0) ? parseFloat(capRaw) : null;

  const totalCash = currentAccountCash || 10000.0;
  let deployable = totalCash * (pct / 100.0);
  if (cap !== null) {
    deployable = Math.min(deployable, cap);
  }
  deployable = Math.max(0.0, Math.min(totalCash, deployable));
  const reserve = Math.max(0.0, totalCash - deployable);

  const previewTotal = document.getElementById('previewAccountTotal');
  const previewDep = document.getElementById('previewDeployableCash');
  const previewRes = document.getElementById('previewProtectedReserve');

  if (previewTotal) previewTotal.textContent = `€${totalCash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  if (previewDep) previewDep.textContent = `€${deployable.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  if (previewRes) previewRes.textContent = `€${reserve.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

async function saveSettings() {
  const autoBuy = document.getElementById('settingAutoBuy').checked;
  const minScoreEl = document.getElementById('settingMinScore');
  const minScore = parseInt(minScoreEl.value);
  const posSizePct = parseFloat(document.getElementById('settingPosSize').value) / 100.0;
  const trigScoreEl = document.getElementById('settingTriggerScore');
  const triggerScore = trigScoreEl ? parseInt(trigScoreEl.value, 10) : undefined;
  const scoreTuneBtn = document.querySelector('[data-score-tune="true"]');
  const scoreAutoTune = scoreTuneBtn ? scoreTuneBtn.getAttribute('aria-pressed') === 'true' : undefined;
  const maxPosEl = document.getElementById('settingMaxPositions');
  const maxPositions = maxPosEl ? parseInt(maxPosEl.value, 10) : undefined;
  const enforceHours = document.getElementById('settingMarketHours') ? document.getElementById('settingMarketHours').checked : true;
  const tradingModeEl = document.getElementById('settingTradingMode');
  const tradingMode = tradingModeEl ? tradingModeEl.value : undefined;

  const capPctEl = document.getElementById('settingCapitalUsagePct');
  const capEurEl = document.getElementById('settingMaxDeployableEur');
  const capitalUsagePct = capPctEl ? parseFloat(capPctEl.value) : undefined;
  const capEurRaw = capEurEl ? capEurEl.value.trim() : '';

  try {
    const payload = {
      auto_buy_enabled: autoBuy,
      position_size_pct: posSizePct,
      enforce_market_hours: enforceHours
    };
    // Send a score only if the user moved its slider: otherwise a stale value would undo a learner change
    if (minScoreEl.value !== minScoreEl.dataset.loaded && !isNaN(minScore)) {
      payload.min_consensus_score = minScore;
    }
    if (trigScoreEl && trigScoreEl.value !== trigScoreEl.dataset.loaded && triggerScore !== undefined && !isNaN(triggerScore)) {
      payload.trigger_min_score = triggerScore;
    }
    if (scoreAutoTune !== undefined) {
      payload.score_auto_tune = scoreAutoTune;
    }
    if (maxPositions !== undefined && !isNaN(maxPositions)) {
      payload.max_positions = maxPositions;
    }
    if (capitalUsagePct !== undefined) {
      payload.max_capital_usage_pct = capitalUsagePct;
    }
    if (capEurEl) {
      if (capEurRaw === '' || parseFloat(capEurRaw) <= 0) {
        payload.clear_max_deployable_capital = true;
      } else {
        payload.max_deployable_capital_eur = parseFloat(capEurRaw);
      }
    }
    if (tradingMode) {
      payload.trading_mode = tradingMode;
      payload.live_trading_enabled = (tradingMode === 'LIVE');
    }

    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast('Settings saved successfully.');
      if (payload.min_consensus_score !== undefined) minScoreEl.dataset.loaded = String(minScore);
      if (payload.trigger_min_score !== undefined) trigScoreEl.dataset.loaded = String(triggerScore);
      renderAutoBuyStatus(autoBuy);
      if (tradingMode) lastSavedTradingMode = tradingMode;
      recalcCapitalPreview();
      await loadPortfolio();
      await loadBrokerStatus();
      if (typeof loadTriggers === 'function') await loadTriggers();
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to save settings.', true);
      // Server refused: reload the real values so the form does not lie
      loadSettings();
    }
  } catch (e) {
    showToast('Failed to save settings.', true);
    loadSettings();
  }
}

// Mode switch: LIVE trades real money, so ask first and revert the <select> on cancel
function onTradingModeChange(selectEl) {
  const wanted = String(selectEl.value || '').toUpperCase();
  if (wanted === 'LIVE') {
    const ok = confirm('Switch to LIVE trading?\n\nThe bot will place REAL orders with real money through your broker. Make sure the broker is connected and your risk limits are correct.');
    if (!ok) {
      selectEl.value = lastSavedTradingMode || 'PAPER';
      return;
    }
  }
  saveSettings();
}

// Enabling auto-buy while LIVE lets the bot spend real money without asking each time
function onAutoBuyChange(checkboxEl) {
  if (checkboxEl.checked && (isLiveMode() || lastSavedTradingMode === 'LIVE')) {
    if (!confirm('Enable automated buying in LIVE mode? The committee will place real-money orders on its own.')) {
      checkboxEl.checked = false;
      return;
    }
  }
  saveSettings();
}

// Auto-buy status in every place it is shown (desktop badge, mobile pill, settings)
function renderAutoBuyStatus(enabled) {
  const on = !!enabled;
  const txt = document.getElementById('autoBuyStatusText');
  if (txt) txt.textContent = on ? 'Active' : 'Paused';
  const mobTxt = document.getElementById('mobileAutoBuyText');
  if (mobTxt) mobTxt.textContent = on ? 'On' : 'Off';
  const badge = document.getElementById('autoBuyBadge');
  if (badge) badge.className = on ? 'hidden xl:flex ui-chip text-gain' : 'hidden xl:flex ui-chip';
  const dot = document.getElementById('autoBuyDot');
  if (dot) dot.className = on ? 'ui-dot bg-gain' : 'ui-dot';
  const sw = document.getElementById('mobileAutoBuyDot');
  if (sw) sw.classList.toggle('on', on);
}

// Lightweight status refresh (auto-buy + trading mode) that does not touch the settings form
async function refreshSystemStatus() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const data = await res.json();
    renderAutoBuyStatus(data.auto_buy_enabled);
    if (data.trading_mode) lastSavedTradingMode = String(data.trading_mode).toUpperCase();
  } catch (e) {
    console.debug('System status refresh failed:', e);
  }
}

// Kill switch ---------------------------------------------------------
function renderKillSwitch() {
  const halted = document.getElementById('haltedChip');
  if (halted) halted.classList.toggle('hidden', !killSwitchActive);
  document.querySelectorAll('#killSwitchBtn, #killSwitchBtnDesktop').forEach(btn => {
    const label = btn.querySelector('.kill-switch-label');
    if (killSwitchActive) {
      btn.classList.add('kill-switch-engaged');
      if (label) label.textContent = 'Halted · resume';
      btn.title = 'Kill switch is ACTIVE: new automated buying is blocked. Tap to resume.';
    } else {
      btn.classList.remove('kill-switch-engaged');
      if (label) label.textContent = btn.id === 'killSwitchBtn' ? 'Stop buying' : 'Kill Switch';
      btn.title = 'Emergency kill switch: halt or resume all new automated buying';
    }
  });
}

async function toggleKillSwitch() {
  const activate = !killSwitchActive;
  const msg = activate
    ? 'ACTIVATE the kill switch?\n\nAll new automated buying will be blocked until you resume it. Open positions are not closed.'
    : 'Resume trading? The kill switch will be released and the bot may place new buy orders again.';
  if (!confirm(msg)) return;
  try {
    const res = await fetch(`/api/killswitch?active=${activate ? 'true' : 'false'}`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.success !== false) {
      killSwitchActive = typeof data.kill_switch_active === 'boolean' ? data.kill_switch_active : activate;
      renderKillSwitch();
      showToast(killSwitchActive ? 'Kill switch ACTIVE: new buying halted.' : 'Kill switch released: trading resumed.', killSwitchActive ? true : false);
    } else {
      showToast(data.detail || 'Kill switch request failed.', true);
    }
  } catch (e) {
    showToast('Network error toggling kill switch.', true);
  }
}

function setTriggerNavBadge(count) {
  const b = document.getElementById('bottomNavTriggerBadge');
  if (!b) return;
  if (count > 0) { b.textContent = String(count); b.classList.remove('hidden'); }
  else { b.classList.add('hidden'); }
}

async function reconnectBroker() {
  const btn = document.getElementById('settingsReconnectBtn');
  const icon = document.getElementById('settingsReconnectIcon');
  if (btn) btn.disabled = true;
  if (icon) icon.className = 'fa-solid fa-spinner fa-spin';

  showToast('Connecting to broker...');

  try {
    const res = await fetch('/api/broker/reconnect', { method: 'POST' });
    const data = await res.json();
    if (data.connected) {
      showToast(`Broker connected (${data.latency_ms || 0}ms)!`);
    } else {
      showToast(`Broker connection failed: ${data.error || data.message || 'Offline'}`, true);
    }
    await loadBrokerStatus(true);
    await loadPortfolio();
  } catch (e) {
    showToast('Network error requesting broker reconnection.', true);
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.className = 'fa-solid fa-bolt';
  }
}

// 9. Quick Scan Trigger
async function triggerQuickScan() {
  const btn = document.getElementById('scanBtn');
  const idleHtml = `<i class="fa-solid fa-satellite-dish"></i> <span>Scan</span>`;
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> <span>Scanning</span>`;
  btn.disabled = true;
  let delayReset = false;

  try {
    const res = await fetch('/api/scheduler/trigger', { method: 'POST' });
    if (res.ok) {
      showToast('Market scan started. Inspecting candidates...');
      delayReset = true;
      setTimeout(() => {
        loadAllData();
        btn.innerHTML = idleHtml;
        btn.disabled = false;
      }, 3500);
    } else {
      showToast('Scan could not start (HTTP ' + res.status + ').', true);
    }
  } catch (e) {
    showToast('Scan trigger failed.', true);
  } finally {
    if (!delayReset) {
      btn.innerHTML = idleHtml;
      btn.disabled = false;
    }
  }
}

// 9b. Manual Market Data Refresh
async function refreshMarketData() {
  const btn = document.getElementById('refreshDataBtn');
  const idleHtml = `<i class="fa-solid fa-arrows-rotate"></i>`;
  btn.innerHTML = `<i class="fa-solid fa-arrows-rotate fa-spin"></i>`;
  btn.disabled = true;

  try {
    const res = await fetch('/api/market-data/refresh', { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Market data refreshed: ${data.tickers_refreshed} prices, ${data.triggers_resized} triggers resized, ${data.triggers_executed} bought, ${data.positions_closed} closed.`);
      loadPortfolio();
      loadTriggers();
    } else {
      showToast(data.detail || data.message || 'Market data refresh failed.', true);
    }
  } catch (e) {
    showToast('Network error refreshing market data.', true);
  } finally {
    btn.innerHTML = idleHtml;
    btn.disabled = false;
  }
}

// 10. AI Order Triggers Handling
// Trigger tab helpers
const TRIGGER_TYPE_LABELS = { LIMIT_PULLBACK: 'Pullback buy', BREAKOUT_BUY: 'Breakout buy' };
function triggerTypeLabel(type) {
  return TRIGGER_TYPE_LABELS[type] || String(type || '').replace(/_/g, ' ').toLowerCase();
}
function hasNum(v) { return v !== undefined && v !== null && v !== '' && Number.isFinite(Number(v)); }
function triggerStatusBadge(status) {
  const s = String(status || '').toUpperCase();
  const cls = s === 'TRIGGERED' ? 'ui-badge-gain' : (s === 'EXPIRED' ? 'ui-badge-warn' : '');
  const label = s ? s.charAt(0) + s.slice(1).toLowerCase() : '-';
  return `<span class="ui-badge ${cls}">${esc(label)}</span>`;
}

async function loadTriggers() {
  try {
    const res = await fetch('/api/triggers');
    const triggers = await res.json();

    const pending = triggers.filter(t => t.status === 'PENDING');
    const history = triggers.filter(t => t.status !== 'PENDING');

    // Section counts
    const badge = document.getElementById('pendingTriggersCount');
    if (badge) badge.textContent = String(pending.length);
    const histCount = document.getElementById('triggerHistoryCount');
    if (histCount) histCount.textContent = history.length ? String(history.length) : '';
    setTriggerNavBadge(pending.length);
    const sumText = document.getElementById('pendingTriggersSummaryText');
    if (sumText) sumText.textContent = `${pending.length} pending trigger${pending.length === 1 ? '' : 's'}`;
    const reservedEl = document.getElementById('pendingTriggersReserved');
    if (reservedEl) {
      const reserved = pending.reduce((acc, t) => acc + (Number(t.allocation_eur) || 0), 0);
      reservedEl.textContent = pending.length ? `${eur(reserved)} reserved` : '';
    }

    // Pending triggers
    const pendingContainer = document.getElementById('pendingTriggersContainer');
    if (pendingContainer) {
      if (pending.length === 0) {
        pendingContainer.innerHTML = `
          <div class="col-span-full ui-card ui-card-pad text-center text-[13px] ui-muted space-y-1">
            <p class="font-semibold" style="color:var(--text)">No pending triggers</p>
            <p>Enter a ticker above, or tap Trigger on a Daily Advice card.</p>
          </div>`;
      } else {
        pendingContainer.innerHTML = pending.map(t => {
          const cur = t.currency || 'EUR';
          const currPrice = hasNum(t.current_price)
            ? Number(t.current_price)
            : (hasNum(t.current_price_at_creation) ? Number(t.current_price_at_creation) : Number(t.target_price));
          const distVal = hasNum(t.distance_pct)
            ? Number(t.distance_pct)
            : (currPrice > 0 ? Math.round(((Number(t.target_price) / currPrice) - 1.0) * 10000) / 100 : 0);
          // A pullback buys at or below the limit, a breakout at or above it
          const isBreakout = t.trigger_type === 'BREAKOUT_BUY';
          const reached = isBreakout ? currPrice >= Number(t.target_price) : currPrice <= Number(t.target_price);
          const distAbs = reached ? 0 : Math.abs(distVal);
          // Near the limit reads as accent; more than 5% away as warn
          const isFar = distAbs > 5;
          const distCls = reached ? 'ui-badge-gain' : (isFar ? 'ui-badge-warn' : 'ui-badge-accent');
          const distLabel = reached
            ? 'Limit reached'
            : `${distAbs.toFixed(1)}% ${isBreakout ? 'rise' : 'drop'} to go`;
          // Bar is full at the limit and empty at 10% or more away
          const closeness = Math.max(0, Math.min(100, Math.round(100 - distAbs * 10)));
          const barColor = isFar ? 'var(--warn)' : 'var(--accent)';

          const allocEur = Number(t.allocation_eur) || 0;
          // Shares the buy would get at the limit price (same sizing as the order and the Telegram alert)
          let plannedLine = '';
          if (allocEur > 0 && hasNum(t.planned_shares)) {
            const plannedShares = Number(t.planned_shares);
            plannedLine = plannedShares > 0
              ? `<div class="ui-faint text-xs">≈ <span class="ui-num">${esc(String(plannedShares))}</span> share${plannedShares === 1 ? '' : 's'} · <span class="ui-num">${esc(eur(t.planned_cost_eur))}</span>${t.risk_capped ? ' · risk-capped' : ''}</div>`
              : `<div class="text-xs" style="color:var(--warn)">Budget below 1 share</div>`;
          }
          const subParts = [triggerTypeLabel(t.trigger_type)];
          if (hasNum(t.consensus_score)) subParts.push(`score ${Number(t.consensus_score)}`);

          return `
            <div class="ui-card ui-card-pad flex flex-col gap-3">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="text-base font-bold" style="color:var(--text)">${tickerLink(t.ticker, `trigger:${t.id}`)}</div>
                  <div class="text-[13px] ui-muted truncate">${esc(subParts.join(' · '))}${t.name ? ` · ${esc(t.name)}` : ''}</div>
                </div>
                <span class="ui-badge ${distCls} shrink-0">${esc(distLabel)}</span>
              </div>

              <div class="ui-track" role="img" aria-label="${esc(distLabel)}"><span style="width:${closeness}%;background:${barColor}"></span></div>

              <div class="flex items-baseline justify-between gap-3 text-[13px]">
                <span><span class="ui-faint">Now</span> <span class="ui-num" style="color:var(--text)">${esc(num(currPrice))}</span></span>
                <span><span class="ui-faint">Limit</span> <span class="ui-num font-semibold" style="color:var(--accent-strong)">${esc(num(t.target_price))}</span> <span class="ui-faint text-xs">${esc(cur)}</span></span>
              </div>

              <div class="ui-inset px-3 py-2 space-y-1">
                <div class="kv"><span>Stop loss</span><span>${hasNum(t.stop_loss) ? esc(num(t.stop_loss)) : '-'}</span></div>
                <div class="kv"><span>Take profit</span><span>${hasNum(t.take_profit) ? esc(num(t.take_profit)) : '-'}</span></div>
              </div>

              ${t.ai_rationale ? `<p class="text-[13px] ui-muted line-clamp-2" title="${esc(t.ai_rationale)}">${esc(t.ai_rationale)}</p>` : ''}

              <div class="flex items-center justify-between gap-3 pt-3 border-t border-borderCol">
                <div class="min-w-0 text-[13px] leading-snug">
                  ${allocEur > 0
                    ? `<div class="ui-num font-semibold" style="color:var(--text)">${esc(eur(allocEur))}</div>${plannedLine}`
                    : `<span class="ui-badge ui-badge-warn" title="Higher-scored triggers hold the available cash or position slots. This trigger is funded automatically when cash or a slot frees up."><i class="fa-solid fa-hourglass-half"></i> Unfunded</span>`}
                  <div class="ui-faint text-xs truncate">Armed <span class="ui-num">${esc(t.created_at)}</span></div>
                </div>
                <button type="button" data-action="cancel-trigger" data-id="${esc(t.id)}" data-ticker="${esc(t.ticker)}" class="ui-btn ui-btn-sm ui-btn-danger shrink-0" style="min-height:44px" aria-label="Cancel trigger for ${esc(t.ticker)}">
                  Cancel
                </button>
              </div>
              ${allocEur > 0 && t.risk_capped && t.risk_note ? `<p class="text-[13px] ui-muted leading-snug"><i class="fa-solid fa-circle-info"></i> ${esc(t.risk_note)}</p>` : ''}
            </div>
          `;
        }).join('');
        staggerChildren(pendingContainer);
      }
    }

    // History
    const historyBody = document.getElementById('triggerHistoryTableBody');
    if (historyBody) {
      if (history.length === 0) {
        historyBody.innerHTML = `<tr><td colspan="6" class="text-center py-8 ui-faint">No past triggers yet.</td></tr>`;
      } else {
        historyBody.innerHTML = history.map(t => `
            <tr>
              <td data-label="Ticker" class="py-3 px-4 font-bold" style="color:var(--text)">${tickerLink(t.ticker, `trigger:${t.id}`)}</td>
              <td data-label="Type" class="py-3 px-4">${esc(triggerTypeLabel(t.trigger_type))}</td>
              <td data-label="Target" class="py-3 px-4 ui-num" style="color:var(--text)"><span>${esc(num(t.target_price))} <span class="ui-faint text-xs">${esc(t.currency)}</span></span></td>
              <td data-label="Status" class="py-3 px-4">${triggerStatusBadge(t.status)}</td>
              <td data-label="When" class="py-3 px-4 ui-num ui-faint whitespace-nowrap">${esc(t.triggered_at || t.created_at)}</td>
              <td data-label="Rationale" class="cell-wide py-3 px-4 max-w-xs truncate" title="${esc(t.ai_rationale)}">${esc(t.ai_rationale)}</td>
            </tr>
          `).join('');
      }
    }

  } catch (e) {
    console.error('Failed to load triggers:', e);
  }
}

const ARM_BTN_HTML = `<i class="fa-solid fa-crosshairs"></i> Arm`;

// Same shape the server accepts (src/trading/trigger_gate.normalize_ticker):
// no ^ indices, =X currencies or spaces. Returns null when unusable.
const TRIGGER_TICKER_RE = /^[A-Z0-9][A-Z0-9.\-]{0,14}$/;
function normalizeTriggerTicker(raw) {
  const t = String(raw || '').trim().toUpperCase().replace(/^\$+/, '');
  return TRIGGER_TICKER_RE.test(t) ? t : null;
}

// Inline "why was this not armed" line under the ticker input
function clearTriggerFeedback() {
  const el = document.getElementById('triggerArmFeedback');
  if (el) el.innerHTML = '';
}

// info: { title, message, tone: 'danger' | 'warn', armed, action, score, minScore, summary }
function showTriggerFeedback(info) {
  const el = document.getElementById('triggerArmFeedback');
  if (!el) return;
  const tone = info.tone === 'warn' ? 'warn' : 'danger';
  const badges = [];
  badges.push(`<span class="ui-badge ${tone === 'warn' ? 'ui-badge-warn' : 'ui-badge-loss'}"><i class="fa-solid ${info.armed ? 'fa-triangle-exclamation' : 'fa-ban'}"></i> ${esc(info.title || 'Not armed')}</span>`);
  if (info.action) badges.push(`<span class="ui-badge">${esc(triggerVerdictLabel(info.action))}</span>`);
  if (hasNum(info.score)) {
    badges.push(`<span class="ui-badge ui-num">Score ${esc(info.score)}${hasNum(info.minScore) ? ` / min ${esc(info.minScore)}` : ''}</span>`);
  }
  const summary = info.summary ? String(info.summary).slice(0, 400) : '';
  el.innerHTML = `
    <div class="ui-notice ui-notice-${tone}">
      <div class="flex flex-wrap items-center gap-2">${badges.join('')}</div>
      <p class="mt-1.5" style="color:var(--text)">${esc(info.message)}</p>
      ${summary ? `<p class="mt-1.5 text-xs ui-faint">Committee: ${esc(summary)}</p>` : ''}
    </div>`;
}

function triggerVerdictLabel(action) {
  return String(action || '').replace(/_/g, ' ').toUpperCase();
}

// Turns any failed /api/triggers/from-ai answer into one flat object.
// `detail` can be a string (old FastAPI errors, 401s), an object from the
// trigger gate ({code, message, overridable, action, ...}), a list of pydantic
// validation errors, or missing entirely (non-JSON body from a proxy/crash).
function parseTriggerFailure(status, data, raw) {
  const info = { code: '', message: '', overridable: false, action: '', score: null, minScore: null, summary: '' };
  const detail = data && typeof data === 'object' ? data.detail : undefined;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    info.code = detail.code ? String(detail.code) : '';
    info.message = typeof detail.message === 'string' ? detail.message : '';
    info.overridable = detail.overridable === true;
    info.action = detail.action ? String(detail.action) : '';
    info.score = hasNum(detail.consensus_score) ? Number(detail.consensus_score) : null;
    info.minScore = hasNum(detail.trigger_min_score) ? Number(detail.trigger_min_score) : null;
    info.summary = typeof detail.summary === 'string' ? detail.summary : '';
  } else if (Array.isArray(detail)) {
    const msgs = detail.map(d => (d && typeof d.msg === 'string') ? d.msg : '').filter(Boolean);
    if (msgs.length) info.message = `Invalid request: ${msgs.join('; ')}`;
  } else if (typeof detail === 'string') {
    info.message = detail;
  }
  if (!info.message && data && typeof data.message === 'string') info.message = data.message;
  if (!info.message) {
    if (!data && raw) console.warn('Non-JSON response while arming a trigger:', status, String(raw).slice(0, 200));
    info.message = status >= 500
      ? `The server could not process this request (HTTP ${status}). Try again in a moment.`
      : `Failed to arm trigger (HTTP ${status}).`;
  }
  return info;
}

async function postArmTrigger(ticker, force, reportJobId = null, resume = null) {
  const { status, data, raw, jobId } = await runJob(
    '/api/triggers/from-ai',
    { ticker: ticker, force: !!force, report_job_id: reportJobId },
    { kind: 'arm', ticker }, resume
  );
  if (status === 200 && data && data.success) return { ok: true, status, data, jobId };
  return { ok: false, status, jobId, error: parseTriggerFailure(status, data, raw) };
}

// "Arm anyway?" text for a soft block (verdict is not a buy, or score too low)
function overrideConfirmText(ticker, err) {
  const facts = [];
  if (err.action) facts.push(`verdict ${triggerVerdictLabel(err.action)}`);
  if (hasNum(err.score)) facts.push(hasNum(err.minScore) ? `score ${err.score}, minimum ${err.minScore}` : `score ${err.score}`);
  const head = facts.length ? `${ticker}: ${facts.join(', ')}.` : `${ticker} did not pass the committee check.`;
  return `${head}\n\n${err.message}\n\nArm anyway?`;
}

let triggerArmBusy = false;

// Shared by the Triggers tab input and the Trigger buttons on advice cards.
// Resolves true when a trigger was armed.
async function armTriggerFlow(rawTicker, { button = null, announce = false, resume = null, reportJobId = null } = {}) {
  clearTriggerFeedback();
  const typed = String(rawTicker || '').trim();
  if (!typed) {
    const message = 'Please enter a stock ticker.';
    showTriggerFeedback({ title: 'Not armed', tone: 'danger', message });
    showToast(message, true);
    return false;
  }
  const ticker = normalizeTriggerTicker(typed);
  if (!ticker) {
    const message = `"${typed.slice(0, 20)}" is not a tradable stock ticker. Use a plain symbol such as NVDA or ASML.AS (no ^ indices, =X currencies or spaces).`;
    showTriggerFeedback({ title: 'Not armed', tone: 'danger', message });
    showToast(message, true);
    return false;
  }
  if (triggerArmBusy) {
    showToast('Already arming a trigger, please wait.', 'info');
    return false;
  }
  if (!resume && !confirmArmTrigger(ticker)) return false;

  triggerArmBusy = true;
  if (button) {
    button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Arming`;
    button.disabled = true;
  }
  if (announce) showToast(`Analysing ${ticker} and arming an entry trigger...`, 'info');

  try {
    let out = await postArmTrigger(ticker, false, reportJobId, resume);
    // Soft block (HOLD verdict, low score): the user may override it explicitly
    if (!out.ok && out.status === 422 && out.error.overridable) {
      const err = out.error;
      if (!confirm(overrideConfirmText(ticker, err))) {
        showTriggerFeedback({ title: 'Not armed', tone: 'warn', message: err.message, action: err.action, score: err.score, minScore: err.minScore, summary: err.summary });
        showToast(`${ticker} was not armed.`, 'info');
        return false;
      }
      out = await postArmTrigger(ticker, true, out.jobId);
    }

    if (out.ok) {
      const data = out.data;
      const gate = (data.gate && typeof data.gate === 'object') ? data.gate : null;
      const at = hasNum(data.target_price) ? ` at ${data.target_price}` : '';
      if (gate && gate.code === 'OK_FORCED') {
        const msg = (typeof gate.message === 'string' && gate.message) ? gate.message : `Armed ${ticker} against the committee verdict.`;
        showTriggerFeedback({
          title: 'Armed anyway', tone: 'warn', armed: true, message: msg,
          action: gate.action, score: hasNum(gate.consensus_score) ? Number(gate.consensus_score) : null,
          minScore: hasNum(gate.trigger_min_score) ? Number(gate.trigger_min_score) : null
        });
        showToast(`Trigger armed for ${ticker}${at}, against the committee verdict.`);
      } else {
        showToast(`Trigger armed for ${ticker}${at}.`);
      }
      loadTriggers();
      return true;
    }

    const err = out.error;
    showTriggerFeedback({ title: 'Not armed', tone: 'danger', message: err.message, action: err.action, score: err.score, minScore: err.minScore, summary: err.summary });
    showToast(`${ticker}: ${err.message}`, true);
    return false;
  } catch (e) {
    const message = 'Could not reach the server. Check the connection and try again.';
    showTriggerFeedback({ title: 'Not armed', tone: 'danger', message });
    showToast(message, true);
    return false;
  } finally {
    triggerArmBusy = false;
    if (button) {
      button.innerHTML = ARM_BTN_HTML;
      button.disabled = false;
    }
  }
}

async function armTriggerFromInput() {
  const input = document.getElementById('triggerTickerInput');
  const armed = await armTriggerFlow(input.value, { button: document.getElementById('armTriggerBtn') });
  if (armed) input.value = '';
}

// Arming reserves cash and lets the bot buy on its own later, so ask first
function confirmArmTrigger(ticker) {
  return confirm(`Arm an entry trigger for ${ticker}?\n\nThe committee picks a limit price and cash is reserved for it. The bot buys automatically when the price is reached.`);
}

async function armAITrigger(ticker) {
  const reportJobId = lastDebateJob && lastDebateJob.ticker === ticker ? lastDebateJob.jobId : null;
  await armTriggerFlow(ticker, { announce: true, reportJobId });
}

async function cancelTrigger(triggerId, ticker = '') {
  if (!Number.isFinite(triggerId)) return;
  if (!confirm(`Cancel the pending trigger${ticker ? ` for ${ticker}` : ''} (#${triggerId})? The reserved cash is released.`)) return;
  try {
    const res = await fetch(`/api/triggers/${triggerId}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Trigger #${triggerId} cancelled.`);
      await loadTriggers();
      await loadPortfolio();
    } else {
      showToast(data.detail || data.message || 'Failed to cancel trigger.', true);
    }
  } catch (e) {
    showToast('Error cancelling trigger.', true);
  }
}



// 12. AI Trader Learning & Self-Reflection Handling
async function loadLearningInsights() {
  try {
    const res = await fetch('/api/learning/insights');
    const data = await res.json();

    const winRateEl = document.getElementById('learningWinRate');
    const countsEl = document.getElementById('learningTradeCounts');
    const minScoreEl = document.getElementById('learningMinScore');
    const posSizeEl = document.getElementById('learningPositionSize');
    const riskParamsEl = document.getElementById('learningRiskParams');
    const container = document.getElementById('learningLessonsContainer');

    if (winRateEl) {
      winRateEl.innerText = `${data.win_rate_pct}%`;
      winRateEl.className = data.win_rate_pct >= 50 ? 'kpi-value ui-gain' : 'kpi-value ui-loss';
    }
    if (countsEl) countsEl.innerText = `${data.total_closed_trades} trades · ${data.wins}W / ${data.losses}L`;

    const p = data.active_parameters || {};
    if (minScoreEl) minScoreEl.innerText = `${p.min_consensus_score || 70}/100`;
    if (posSizeEl) posSizeEl.innerText = `${((p.position_size_pct || 0.08) * 100).toFixed(1)}%`;
    if (riskParamsEl) riskParamsEl.innerText = `1:${p.take_profit_rr || 2.1} (${p.atr_stop_multiplier || 1.75}x)`;

    if (container && data.recent_lessons) {
      if (data.recent_lessons.length === 0) {
        container.innerHTML = `<div class="ui-card ui-card-pad text-[13px] ui-muted">
          No post-mortems yet. Lessons appear here when positions are closed.
        </div>`;
      } else {
        container.innerHTML = data.recent_lessons.slice(0, 4).map(l => {
          const isWin = l.outcome === 'WIN';
          const isLoss = l.outcome === 'LOSS';
          const badgeCls = isWin ? 'ui-badge-gain' : (isLoss ? 'ui-badge-loss' : '');
          const arrow = isWin ? '▲' : (isLoss ? '▼' : '•');
          const pnl = Number(l.realized_pnl_eur) || 0;
          const pnlSign = pnl >= 0 ? '+' : '-';
          const pnlCls = pnl > 0 ? 'ui-gain' : (pnl < 0 ? 'ui-loss' : '');
          const pnlPct = Math.abs(Number(l.realized_pnl_pct) || 0);

          return `
            <div class="ui-card ui-card-pad space-y-2.5">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="flex items-center gap-2">
                    <span class="text-base font-bold" style="color:var(--text)">${tickerLink(l.ticker)}</span>
                    <span class="ui-badge ${badgeCls}">${arrow} ${esc(l.outcome)}</span>
                  </div>
                  <div class="text-xs ui-faint ui-num mt-0.5">${esc(l.created_at)}</div>
                </div>
                <div class="text-right shrink-0">
                  <div class="ui-num font-semibold ${pnlCls}">${pnlSign}${esc(eur(Math.abs(pnl)))}</div>
                  <div class="ui-num text-xs ${pnlCls}">${pnlSign}${esc(num(pnlPct, 1))}%</div>
                </div>
              </div>
              <p class="text-[13px] ui-muted"><span class="ui-faint">Diagnosis:</span> ${esc(isLoss ? l.what_went_wrong : l.what_went_well)}</p>
              <div class="ui-inset px-3 py-2 text-[13px]" style="color:var(--text)">
                <span class="ui-label" style="color:var(--accent-strong)"><i class="fa-solid fa-lightbulb mr-1"></i>Lesson</span>
                <p class="mt-1">${esc(l.lesson)}</p>
              </div>
            </div>
          `;
        }).join('');
      }
    }
  } catch (e) {
    console.debug('Error loading learning insights:', e);
  }
}

async function triggerLearningCalibration() {
  showToast('Re-evaluating trade performance and calibrating risk parameters...');
  try {
    const res = await fetch('/api/learning/calibrate', { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Calibration complete. Win rate: ${data.win_rate_pct}%`);
      loadLearningInsights();
      loadSettings();
    } else {
      showToast(data.detail || data.message || 'No trades to calibrate yet.', true);
    }
  } catch (e) {
    showToast('Error calibrating learning parameters.', true);
  }
}

// 8. Market Review & Next Scheduled Session Loader
// Plain Dutch labels; keep in sync with ACTION_NL / _pick_status_nl in src/telegram/bot.py
const REVIEW_SESSION_NL = { OPEN: 'opening', CLOSE: 'slot', REVIEW: 'review', MANUAL_SCAN: 'scan' };
const REVIEW_ACTION_NL = { STRONG_BUY: 'Sterk koopadvies', BUY: 'Koopadvies', HOLD: 'Afwachten', SELL: 'Verkoopadvies', STRONG_SELL: 'Sterk verkoopadvies' };

function reviewActionNl(action) {
  const key = String(action || '').toUpperCase();
  return REVIEW_ACTION_NL[key] || key.replace(/_/g, ' ').toLowerCase();
}

function reviewPickStatusNl(p, minScore) {
  switch (p.status_type) {
    case 'BOUGHT': return '🚀 Gekocht';
    case 'TRIGGER_ARMED':
      return p.entry_trigger_price
        ? `🎯 Koopopdracht klaar: koopt als de koers ${p.entry_trigger_price} ${p.currency || ''} raakt`
        : '🎯 Koopopdracht klaar: koopt zodra de instapkoers geraakt wordt';
    case 'ALREADY_HELD': return '💼 Zit al in de portefeuille, niet bijgekocht';
    case 'TRIGGER_PENDING': return '⏳ Er stond al een koopopdracht klaar, die wacht nog';
    case 'TRIGGER_CANCELLED': return '🛑 Koopopdracht geannuleerd: nieuwe beoordeling steunt de aankoop niet meer';
    case 'INSUFFICIENT_CASH': return '⏸️ Goed genoeg, maar geen vrij geld of vrije plek';
    case 'SHARE_TOO_EXPENSIVE': return '💶 Goed genoeg, maar 1 aandeel kost meer dan het budget per positie';
    case 'REGIME_FILTER': return '🌧️ Goed genoeg, maar de hele markt daalt (index onder 200-daags gemiddelde)';
    default: return `👀 Alleen volgen: score onder ${minScore}, dus nog niet kopen`;
  }
}
let currentMarketFilter = null;

async function loadMarketReview(market = null) {
  if (market !== undefined && market !== null) {
    currentMarketFilter = market || null;   // '' (the "Latest" pill) clears the filter
  }
  document.querySelectorAll('[data-review-market]').forEach(b => {
    const active = (b.dataset.reviewMarket || null) === (currentMarketFilter || null);
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  try {
    const url = currentMarketFilter ? `/api/market-digest/latest?market=${encodeURIComponent(currentMarketFilter)}` : '/api/market-digest/latest';
    const res = await fetch(url);
    const data = await res.json();

    const timestampEl = document.getElementById('digestTimestamp');
    const headlineEl = document.getElementById('digestHeadline');
    const contentEl = document.getElementById('digestContent');
    const nextBadgeEl = document.getElementById('nextSessionBadge');
    const textContainer = document.getElementById('reviewTextContainer');

    if (data.next_session && nextBadgeEl) {
      nextBadgeEl.innerHTML = `<i class="fa-regular fa-clock"></i> Volgende: <span class="ui-num text-slate-50">${esc(data.next_session.formatted)}</span>`;
    }
    const textDetails = document.getElementById('reviewTextDetails');

    if (!data.found) {
      const marketLabel = currentMarketFilter ? `${currentMarketFilter}-` : '';
      if (headlineEl) headlineEl.innerText = `Nog geen ${marketLabel}review vandaag`;
      if (contentEl) contentEl.innerHTML = `<div class="adv-prose">Tik hieronder op <strong class="text-slate-50">${esc(currentMarketFilter || 'US')}-review starten</strong>. De bot beoordeelt dan de kansen van vandaag.</div>`;
      if (timestampEl) timestampEl.innerText = 'Nog niet gedraaid';
      if (textDetails) textDetails.classList.add('hidden');
      return;
    }

    if (timestampEl) timestampEl.innerText = `${data.market || ''} ${REVIEW_SESSION_NL[data.session] || data.session || ''} · ${data.timestamp || 'Vandaag'}`.trim();
    if (headlineEl) headlineEl.innerText = data.headline || 'Review';

    const minScore = data.trigger_min_score || 80;
    const block = (title, body) => `<div><div class="ui-eyebrow mb-1.5">${title}</div>${body}</div>`;

    const summaryHtml = block('In het kort', `<div class="adv-prose">${safeRichText(data.content || '')}</div>`);

    const positions = (data.portfolio_state && data.portfolio_state.positions) || [];
    const holdingsHtml = block(`Bezit (${positions.length})`, positions.length === 0
      ? '<div class="adv-prose ui-faint">Geen aandelen. Alles staat in cash.</div>'
      : '<div class="ui-inset px-3.5 py-2.5 space-y-2.5">' + positions.map(pos => {
          const up = Number(pos.unrealized_pnl_eur) >= 0;
          const levels = [
            pos.stop_loss_native ? `verkoopt onder ${esc(pos.stop_loss_native)}` : '',
            pos.take_profit_native ? `winst nemen bij ${esc(pos.take_profit_native)}` : ''
          ].filter(Boolean).join(' · ');
          return `<div>
            <div class="kv"><span class="ui-num text-slate-50 font-semibold">${tickerLink(pos.ticker)}</span>
              <span class="ui-num ${up ? 'ui-gain' : 'ui-loss'}">${up ? '▲' : '▼'} ${esc(signedEur(pos.unrealized_pnl_eur))} (${up ? '+' : ''}${num(pos.unrealized_pnl_pct, 1)}%)</span></div>
            <div class="ui-faint text-[13px] ui-num">${esc(pos.shares)} st. · gekocht ${esc(pos.entry_price_native)} · nu ${esc(pos.current_price_native)} ${esc(pos.currency)}</div>
            ${levels ? `<div class="ui-faint text-[13px] ui-num">${levels}</div>` : ''}
          </div>`;
        }).join('') + '</div>');

    const picks = (data.top_picks || []).slice(0, 4);
    const picksHtml = block('Kansen', picks.length === 0
      ? '<div class="adv-prose ui-faint">Geen aandelen door de filters gekomen.</div>'
      : `<div class="set-help mb-2">Score = hoe zeker de AI-commissie is (0-100). Vanaf ${esc(minScore)} zet de bot een koopopdracht klaar.</div>
         <div class="ui-list">` + picks.map(p => {
          const risk = p.risk || {};
          return `<div class="p-3.5 space-y-1">
            <div class="flex items-baseline justify-between gap-3">
              <span class="min-w-0 truncate"><span class="text-[15px] font-bold text-slate-50">${tickerLink(p.ticker)}</span> <span class="ui-faint text-[13px]">${esc(p.name || '')}</span></span>
              <span class="ui-num shrink-0"><span class="text-slate-50 font-semibold">${esc(p.consensus_score)}</span><span class="ui-faint">/100</span></span>
            </div>
            <div class="adv-prose">${esc(reviewActionNl(p.action))} · koers ${advicePrice(p.price, p.currency)}</div>
            <div class="adv-prose text-slate-50">${esc(reviewPickStatusNl(p, minScore))}</div>
            ${String(p.action || '').includes('BUY') && risk.stop_loss_price && risk.take_profit_price ? `<div class="ui-faint text-[13px] ui-num">Na kopen: verkoopt onder ${esc(risk.stop_loss_price)} · winst nemen bij ${esc(risk.take_profit_price)}</div>` : ''}
          </div>`;
        }).join('') + '</div>');

    if (contentEl) {
      contentEl.removeAttribute('data-loading');
      contentEl.innerHTML = summaryHtml + holdingsHtml + picksHtml;
    }

    if (textContainer && textDetails && data.review_text) {
      textContainer.innerHTML = safeRichText(data.review_text);
      textDetails.classList.remove('hidden');
    } else if (textDetails) {
      textDetails.classList.add('hidden');
    }
  } catch (e) {
    console.debug('Error loading market review:', e);
  }
}

async function triggerMarketReview(market) {
  showToast(`${market}-review gestart...`);
  try {
    // Record current latest digest id before running
    const prevRes = await fetch(`/api/market-digest/latest?market=${encodeURIComponent(market)}`);
    const prevData = await prevRes.json().catch(() => ({}));
    const prevId = prevData.found ? prevData.id : null;

    const res = await fetch('/api/market-session/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ market: market, session: 'OPEN' })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`${market}-review beoordeelt aandelen. Resultaat volgt zo.`);
      currentMarketFilter = market;

      // Poll every 5s for up to 90s for the review to appear or update
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const checkRes = await fetch(`/api/market-digest/latest?market=${encodeURIComponent(market)}`);
          const checkData = await checkRes.json();
          if (checkData.found && checkData.id !== prevId) {
            clearInterval(poll);
            showToast(`${market}-review is klaar.`);
            loadMarketReview(market);
            loadWeeklyReviewComparison();
            loadRecommendations();
            loadPortfolio();
            return;
          }
        } catch (err) {
          console.debug('Polling check error:', err);
        }

        if (attempts >= 18) { // 90 seconds timeout
          clearInterval(poll);
          loadMarketReview(market);
          loadWeeklyReviewComparison();
          loadRecommendations();
          loadPortfolio();
        }
      }, 5000);
    } else {
      showToast(data.detail || data.message || 'Failed generating market review.', true);
    }
  } catch (e) {
    showToast(`Failed generating ${market} market review.`, true);
  }
}

// 9. Past-Week Review Comparison & Tracked Picks
let weeklyReviewsCache = [];

async function loadWeeklyReviewComparison() {
  const STAT = 'ui-num text-[17px] font-semibold mt-1 truncate';
  try {
    const [compRes, weeklyRes] = await Promise.all([
      fetch('/api/reviews/compare'),
      fetch('/api/reviews/weekly?days=7')
    ]);

    const comp = await compRes.json();
    const weeklyData = await weeklyRes.json();
    weeklyReviewsCache = Array.isArray(weeklyData) ? weeklyData : [];

    // Update Badges
    const winRateEl = document.getElementById('weeklyWinRate');
    const winCountsEl = document.getElementById('weeklyWinCounts');
    const deltaEl = document.getElementById('weeklyPortfolioDelta');
    const rangeEl = document.getElementById('weeklyPortfolioRange');
    const bestPickEl = document.getElementById('weeklyBestPick');
    const bestPickDetailsEl = document.getElementById('weeklyBestPickDetails');
    const sessionsCountEl = document.getElementById('weeklySessionsCount');
    const picksContainer = document.getElementById('weeklyTrackedPicksContainer');
    const archiveCountBadge = document.getElementById('archiveCountBadge');

    if (sessionsCountEl) sessionsCountEl.innerText = comp.total_reviews || 0;
    if (archiveCountBadge) archiveCountBadge.innerText = (weeklyReviewsCache || []).length;

    if (!comp.has_history || !comp.tracked_picks || comp.tracked_picks.length === 0) {
      if (winRateEl) { winRateEl.innerText = '--%'; winRateEl.className = `${STAT} ui-faint`; }
      if (winCountsEl) winCountsEl.innerText = 'Nog geen adviezen';
      if (deltaEl) { deltaEl.innerText = '€0.00 (+0.0%)'; deltaEl.className = `${STAT} text-slate-50`; }
      if (bestPickEl) bestPickEl.innerText = '--';
      if (picksContainer) {
        picksContainer.innerHTML = `<div class="p-3.5 adv-prose">Nog geen adviezen in de afgelopen 7 dagen. Start een review om te beginnen.</div>`;
      }
      renderWeeklyArchive();
      return;
    }

    if (winRateEl) {
      winRateEl.innerText = `${comp.positive_picks_count} van ${comp.total_picks_tracked}`;
      winRateEl.className = `${STAT} ${comp.win_rate_pct >= 50 ? 'ui-gain' : 'ui-loss'}`;
    }
    if (winCountsEl) {
      winCountsEl.innerText = `${num(comp.win_rate_pct, 0)}% van totaal`;
    }

    if (deltaEl) {
      const up = comp.portfolio_delta_eur >= 0;
      const sign = up ? '+' : '';
      deltaEl.innerText = `${up ? '▲' : '▼'} ${sign}€${num(comp.portfolio_delta_eur)} (${sign}${num(comp.portfolio_delta_pct)}%)`;
      deltaEl.className = `${STAT} ${up ? 'ui-gain' : 'ui-loss'}`;
    }
    if (rangeEl && comp.portfolio_trajectory) {
      rangeEl.innerText = comp.portfolio_trajectory;
    }

    if (bestPickEl && comp.best_performer) {
      const bp = comp.best_performer;
      const sign = bp.pnl_pct >= 0 ? '+' : '';
      bestPickEl.innerText = `${bp.ticker} ${sign}${bp.pnl_pct}%`;
      bestPickEl.className = `${STAT} ${bp.pnl_pct >= 0 ? 'ui-gain' : 'ui-loss'}`;
      if (bestPickDetailsEl) bestPickDetailsEl.innerText = `${bp.rec_session} · ${bp.status_badge}`;
    }

    // Render Tracked Picks
    if (picksContainer) {
      picksContainer.innerHTML = comp.tracked_picks.map(p => {
        const isPos = p.pnl_pct >= 0;
        const sign = isPos ? '+' : '';

        const badgeText = String(p.status_badge || '');
        let badgeClass = 'ui-badge';
        if (p.status === 'TARGET') badgeClass = 'ui-badge ui-badge-gain';
        else if (p.status === 'STOP') badgeClass = 'ui-badge ui-badge-loss';

        return `
          <div class="p-3.5 space-y-2">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[15px] font-bold text-slate-50">${tickerLink(p.ticker)}</span>
                  ${badgeText ? `<span class="${badgeClass}">${esc(badgeText)}</span>` : ''}
                </div>
                <div class="ui-faint text-[13px] truncate">${esc(p.name)}</div>
              </div>
              <div class="ui-num text-[15px] font-semibold shrink-0 ${isPos ? 'ui-gain' : 'ui-loss'}">${isPos ? '▲' : '▼'} ${sign}${esc(p.pnl_pct)}%</div>
            </div>
            <div class="space-y-1">
              <div class="kv"><span>Advies gegeven</span><span>${esc(p.rec_session)}</span></div>
              <div class="kv"><span>Koers bij advies</span><span>${advicePrice(p.rec_price, p.currency)}</span></div>
              <div class="kv"><span>Koers nu</span><span>${advicePrice(p.current_price, p.currency)}</span></div>
              ${p.target_price ? `<div class="kv"><span>Winst nemen bij</span><span class="ui-gain">▲ ${esc(p.target_price)}</span></div>` : ''}
              ${p.stop_loss ? `<div class="kv"><span>Verkoopt onder</span><span class="ui-loss">▼ ${esc(p.stop_loss)}</span></div>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }

    renderWeeklyArchive();
  } catch (e) {
    console.debug('Error loading weekly review comparison:', e);
  }
}

function renderWeeklyArchive() {
  const container = document.getElementById('weeklyArchiveContainer');
  if (!container || !weeklyReviewsCache) return;

  if (!Array.isArray(weeklyReviewsCache) || weeklyReviewsCache.length === 0) {
    container.innerHTML = `<div class="adv-prose ui-faint px-1">Nog geen eerdere reviews.</div>`;
    return;
  }

  container.innerHTML = weeklyReviewsCache.map(r => {
    const picks = (r.top_picks || []).map(p => p.ticker).join(', ') || 'Geen'; // escaped below
    return `
      <div class="ui-inset p-3.5 space-y-1.5">
        <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <span class="text-[14px] font-semibold text-slate-50">${esc(r.headline)}</span>
          <span class="ui-num ui-faint text-[12px]">${esc(r.timestamp)}</span>
        </div>
        <div class="adv-prose adv-clamp">${safeRichText(r.content)}</div>
        <div class="ui-faint text-[13px]">Bekeken: <span class="ui-num text-slate-50">${esc(picks)}</span></div>
      </div>
    `;
  }).join('');
}

// ==========================================
// TENCENTDB AGENT MEMORY HUB CLIENT
// ==========================================

async function loadMemoryHub() {
  await Promise.all([
    loadColdStartRecap(),
    loadMemoryAssets()
  ]);
}

async function refreshMemoryHub() {
  showToast('Refreshing memory...', 'info');
  await loadMemoryHub();
  showToast('Memory updated.', 'success');
}

async function loadColdStartRecap() {
  const container = document.getElementById('memoryColdStartText');
  if (!container) return;
  try {
    const res = await fetch('/v3/memory/cold-start');
    const json = await res.json();
    if (json.code === 0 && json.data) {
      container.textContent = json.data.full_prompt_context || json.data.what_had_happened || 'No recent memory recorded.';
    } else {
      container.textContent = 'Failed to load cold start recap.';
    }
  } catch (err) {
    container.textContent = `Error loading cold start memory: ${err.message}`;
  }
}

async function loadMemoryAssets() {
  try {
    const res = await fetch('/v3/memory/assets');
    const json = await res.json();
    if (json.code === 0 && json.data) {
      renderMemoryAssets(json.data);
    }
  } catch (err) {
    console.error('Error fetching memory assets:', err);
  }
}

function renderMemoryAssets(assets) {
  const empty = (text) => `<div class="mem-empty">${esc(text)}</div>`;

  // 1. L1 atoms (facts and constraints)
  const atoms = (assets.chat_memory && assets.chat_memory.l1_atoms) || [];
  const atomsBadge = document.getElementById('atomsCountBadge');
  const atomsContainer = document.getElementById('atomsListContainer');
  if (atomsBadge) atomsBadge.textContent = String(atoms.length);
  if (atomsContainer) {
    if (atoms.length === 0) {
      atomsContainer.innerHTML = empty('No facts extracted yet.');
    } else {
      atomsContainer.innerHTML = atoms.map(a => `
        <div class="mem-row">
          <div class="flex items-start justify-between gap-3">
            <span class="font-semibold text-[15px] min-w-0 break-words">${esc(a.subject)}</span>
            <span class="ui-badge shrink-0">${esc((a.type || 'fact').toLowerCase())}</span>
          </div>
          <p class="text-sm ui-muted mt-1 leading-relaxed">${esc(a.content)}</p>
          <p class="text-xs ui-faint ui-num mt-1.5">${esc(Math.round((a.confidence || 1) * 100))}% confidence</p>
        </div>
      `).join('');
    }
  }

  // 2. Skills
  const skills = assets.skills || [];
  const skillsBadge = document.getElementById('skillsCountBadge');
  const skillsContainer = document.getElementById('skillsListContainer');
  if (skillsBadge) skillsBadge.textContent = String(skills.length);
  if (skillsContainer) {
    if (skills.length === 0) {
      skillsContainer.innerHTML = empty('No skills registered.');
    } else {
      skillsContainer.innerHTML = skills.map(s => `
        <div class="mem-row">
          <div class="flex items-start justify-between gap-3">
            <span class="font-semibold text-[15px] min-w-0 break-words">${esc(s.name)}</span>
            <span class="ui-badge ui-badge-accent shrink-0">${esc(s.category || 'skill')}</span>
          </div>
          <p class="text-sm ui-muted mt-1 leading-relaxed">${esc(s.description)}</p>
          ${(s.execution_steps || []).length ? `<p class="text-xs ui-faint mt-1.5 leading-relaxed">${esc((s.execution_steps || []).join(' → '))}</p>` : ''}
        </div>
      `).join('');
    }
  }

  // 3. Wiki
  const wiki = assets.llm_wiki || [];
  const wikiBadge = document.getElementById('wikiCountBadge');
  const wikiContainer = document.getElementById('wikiListContainer');
  if (wikiBadge) wikiBadge.textContent = String(wiki.length);
  if (wikiContainer) {
    if (wiki.length === 0) {
      wikiContainer.innerHTML = empty('No wiki pages registered.');
    } else {
      wikiContainer.innerHTML = wiki.map(w => `
        <div class="mem-row">
          <div class="flex items-start justify-between gap-3">
            <span class="font-semibold text-[15px] min-w-0 break-words">${esc(w.title)}</span>
            <span class="text-xs ui-faint ui-num shrink-0">${esc(w.page_id)}</span>
          </div>
          <p class="text-sm ui-muted mt-1 leading-relaxed line-clamp-2">${esc((w.content_md || '').split('\n').slice(2, 5).join(' '))}</p>
          ${(w.tags || []).length ? `<p class="text-xs ui-faint mt-1.5">${(w.tags || []).map(t => `#${esc(t)}`).join(' ')}</p>` : ''}
        </div>
      `).join('');
    }
  }

  // 4. L3 core profile & code graph
  const l3 = (assets.chat_memory && assets.chat_memory.l3_core) || {};
  const graph = assets.code_graph || [];
  const graphBadge = document.getElementById('codegraphCountBadge');
  if (graphBadge) graphBadge.textContent = graph.length ? String(graph.length) : '';
  const coreContainer = document.getElementById('coreAndGraphContainer');
  if (coreContainer) {
    coreContainer.innerHTML = `
      <div class="mem-row">
        <div class="flex items-start justify-between gap-3">
          <span class="font-semibold text-[15px] min-w-0 break-words">${esc(l3.persona_name || 'Committee persona')}</span>
          <span class="ui-badge shrink-0">core</span>
        </div>
        ${l3.core_profile ? `<p class="text-sm mt-1 leading-relaxed">${esc(l3.core_profile)}</p>` : ''}
        ${l3.cumulative_wisdom ? `<div class="ui-inset p-3 mt-2.5 text-sm ui-muted leading-relaxed whitespace-pre-wrap">${esc(l3.cumulative_wisdom)}</div>` : ''}
      </div>
      ${graph.length ? `<div class="mem-row" style="padding-bottom: 6px;"><span class="ui-eyebrow">Code graph</span></div>` : ''}
      ${graph.map(g => `
        <div class="mem-row" style="padding-top: 10px; padding-bottom: 10px;">
          <div class="flex items-baseline justify-between gap-3">
            <span class="ui-num text-sm font-semibold min-w-0 break-all">${esc(g.symbol_name)}</span>
            <span class="text-xs ui-faint shrink-0">${esc(g.symbol_type)}</span>
          </div>
          <div class="text-xs ui-faint ui-num mt-0.5 break-all">${esc(g.file_path)}:${esc(g.line_start || 1)}</div>
        </div>
      `).join('')}
    `;
  }
}

async function openExtractModal() {
  const sample = prompt("Text, trade rationale or conversation to extract memory from (leave blank to use recent activity):");
  if (sample === null) return;
  showToast("Extracting memory...", "info");
  try {
    const res = await fetch("/v3/memory/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_text: sample || "Investigated BESI.AS pullback entry at €175.88. Enforced Euronext minimum €100 order size to limit fee drag to 3.8%.",
        sync_to_store: true
      })
    });
    const json = await res.json();
    if (json.code === 0) {
      showToast(`Extraction done: ${json.data.extracted_atoms_count} facts added.`, "success");
      await loadMemoryHub();
    } else {
      showToast(`Extraction failed: ${json.message}`, "error");
    }
  } catch (err) {
    showToast(`Extraction error: ${err.message}`, "error");
  }
}




// Shadow paper portfolios: simulated strategies next to the main portfolio
const shadowCharts = {};

function signedPct(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
  const v = Number(value);
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}${num(Math.abs(v), digits)}%`;
}
function gainCls(value) {
  const v = Number(value);
  return v > 0 ? 'ui-gain' : (v < 0 ? 'ui-loss' : '');
}

function shadowParamChips(p) {
  const chips = [];
  chips.push(`Score &ge; ${esc(p.effective_trigger_min_score)}`);
  if (p.entry_mode === 'market') chips.push('Buy at market');
  else if (p.pullback_scale !== 1) chips.push(`${esc(num(p.pullback_scale * 100, 0))}% of pullback`);
  if (p.stop_scale !== 1) chips.push(`Stop &times;${esc(num(p.stop_scale, 2))}`);
  if (!p.use_take_profit) chips.push('No fixed target');
  else if (p.reward_risk) chips.push(`Target ${esc(num(p.reward_risk, 1))}R`);
  if (p.trailing_stop_pct) chips.push(`Trail ${esc(num(p.trailing_stop_pct * 100, 0))}%`);
  chips.push(`Breakeven at +${esc(num(p.effective_breakeven_pct * 100, 1))}%`);
  if (p.effective_trailing_atr_mult) chips.push(`Trail ${esc(num(p.effective_trailing_atr_mult, 1))}&times; ATR`);
  if (p.effective_risk_per_trade_pct) chips.push(`Risk ${esc(num(p.effective_risk_per_trade_pct, 1))}% per trade`);
  chips.push(p.effective_regime_filter ? 'Regime filter on' : 'No regime filter');
  if (p.whole_shares) chips.push('Whole shares');
  chips.push(`Max ${esc(p.effective_max_positions)} positions`);
  if (p.max_hold_days) chips.push(`Exit after ${esc(p.max_hold_days)}d`);
  if (p.accept_hold) chips.push('Also HOLD verdicts');
  if (p.ignore_risk_veto) chips.push('Ignores risk veto');
  return chips.map(c => `<span class="ui-badge">${c}</span>`).join('');
}

// The shadow keeps the main bot's values from its start; say so when the learner has moved them since
function shadowFrozenNote(p) {
  const f = p.frozen_main_params || {};
  const c = p.current_main_params || {};
  const LABELS = {
    trigger_min_score: v => `score ${v}`,
    breakeven_pct: v => `breakeven +${num((v || 0) * 100, 1)}%`,
    max_positions: v => `max ${v} positions`,
    risk_per_trade_pct: v => `risk ${num(v || 0, 1)}%`,
    trailing_atr_mult: v => `trail ${num(v || 0, 1)}x ATR`,
    regime_filter: v => (v ? 'regime filter on' : 'regime filter off'),
  };
  const diffs = Object.keys(LABELS).filter(k => f[k] !== undefined && c[k] !== undefined && f[k] !== c[k]);
  if (!diffs.length) return '';
  const then = diffs.map(k => LABELS[k](f[k])).join(', ');
  const now = diffs.map(k => LABELS[k](c[k])).join(', ');
  return `<p class="text-xs mt-1" style="color:var(--warn)"><i class="fa-solid fa-snowflake mr-1"></i>Uses the main bot's values from its start (${esc(then)}). Main now: ${esc(now)}. Reset to adopt them.</p>`;
}

function shadowCompareRow(label, mainHtml, shadowHtml) {
  return `<div class="grid grid-cols-3 gap-2 py-2 text-[13px]">
    <span class="ui-muted">${label}</span>
    <span class="ui-num text-right">${mainHtml}</span>
    <span class="ui-num text-right" style="color:var(--text)">${shadowHtml}</span>
  </div>`;
}

function renderShadowStrategy(s, mainLabel) {
  const m = s.main_same_period || {};
  const winRate = v => (v === null || v === undefined) ? '--' : `${esc(num(v, 0))}%`;
  const positions = (s.open_positions || []).map(p => `
    <div class="flex items-center justify-between gap-3 py-2 text-[13px]">
      <div class="min-w-0">
        <span class="font-semibold" style="color:var(--text)">${tickerLink(p.ticker, `shadow_position:${p.id}`)}</span>
        ${p.followed_main ? '' : '<span class="ui-badge ui-badge-accent ml-1">Shadow only</span>'}
        <div class="ui-faint ui-num text-xs">${esc(num(p.entry_price_native))} &rarr; ${esc(num(p.current_price_native))} ${esc(p.currency)} · stop ${p.stop_loss_native ? esc(num(p.stop_loss_native)) : '--'}</div>
      </div>
      <div class="text-right shrink-0 ui-num ${gainCls(p.unrealized_pnl_eur)}">${esc(signedEur(p.unrealized_pnl_eur))}<div class="text-xs">${esc(signedPct(p.unrealized_pnl_pct, 1))}</div></div>
    </div>`).join('');
  const pending = (s.pending_triggers || []).map(t => `
    <div class="flex items-center justify-between gap-3 py-2 text-[13px]">
      <div class="min-w-0">
        <span class="font-semibold" style="color:var(--text)">${tickerLink(t.ticker, `shadow_trigger:${t.id}`)}</span>
        ${t.followed_main ? '' : '<span class="ui-badge ui-badge-accent ml-1">Shadow only</span>'}
        <div class="ui-faint text-xs">${esc(t.trigger_type === 'MARKET' ? 'Buy at market (next open)' : triggerTypeLabel(t.trigger_type))} · score ${esc(t.consensus_score)}</div>
      </div>
      <span class="ui-num shrink-0">${esc(num(t.target_price))} ${esc(t.currency)}</span>
    </div>`).join('');
  const closed = (s.recent_closed || []).map(c => `
    <div class="flex items-center justify-between gap-3 py-2 text-[13px]">
      <div class="min-w-0">
        <span class="font-semibold" style="color:var(--text)">${tickerLink(c.ticker, `shadow_position:${c.id}`)}</span>
        ${c.followed_main ? '' : '<span class="ui-badge ui-badge-accent ml-1">Shadow only</span>'}
        <div class="ui-faint text-xs">${esc(c.closed_at)} · ${esc(c.exit_reason || '')}</div>
      </div>
      <div class="text-right shrink-0 ui-num ${gainCls(c.realized_pnl_eur)}">${esc(signedEur(c.realized_pnl_eur))}<div class="text-xs">${esc(signedPct(c.realized_pnl_pct, 1))}</div></div>
    </div>`).join('');
  const list = (title, html, empty) => `
    <div class="space-y-1">
      <p class="ui-eyebrow px-1">${title}</p>
      <div class="ui-inset px-3 divide-y divide-borderCol">${html || `<p class="py-2 text-[13px] ui-faint">${empty}</p>`}</div>
    </div>`;

  return `
    <div class="ui-card ui-card-pad space-y-4">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="ui-section-title">${esc(s.label)}<span class="ui-badge ui-badge-accent ${s.learning_strength > 0 ? '' : 'hidden'}" data-sh-badge>Learning</span></h3>
          <p class="text-[13px] ui-muted mt-1">${esc(s.description)}</p>
          <p class="text-xs ui-faint mt-1 ui-num">Since ${esc(s.started_at || '--')} · start ${esc(eur(s.initial_capital_eur))}</p>
          ${shadowFrozenNote(s.params || {})}
        </div>
      </div>
      <div class="sh-actions">
        <button type="button" class="ui-btn ui-btn-sm" data-action="edit-shadow" data-key="${esc(s.key)}"><i class="fa-solid fa-sliders" aria-hidden="true"></i> Edit</button>
        <button type="button" class="ui-btn ui-btn-sm" data-action="copy-shadow" data-key="${esc(s.key)}"><i class="fa-regular fa-copy" aria-hidden="true"></i> Copy</button>
        <button type="button" class="ui-btn ui-btn-sm" data-action="reset-shadow" data-key="${esc(s.key)}"><i class="fa-solid fa-rotate-left" aria-hidden="true"></i> Reset</button>
        <button type="button" class="ui-btn ui-btn-sm" data-action="delete-shadow" data-key="${esc(s.key)}" data-label="${esc(s.label)}"><i class="fa-regular fa-trash-can" aria-hidden="true"></i> Delete</button>
      </div>
      <div class="flex flex-wrap gap-1.5">${shadowParamChips(s.params || {})}</div>
      ${shadowStrengthControl(s)}
      <details class="sh-hist" data-key="${esc(s.key)}">
        <summary>Change history</summary>
        <div class="sh-hist-list divide-y divide-borderCol"></div>
      </details>

      <div class="ui-inset px-3 divide-y divide-borderCol">
        <div class="grid grid-cols-3 gap-2 py-2 ui-eyebrow">
          <span>Same period</span><span class="text-right">${esc(mainLabel)}</span><span class="text-right">${esc(s.label)}</span>
        </div>
        ${shadowCompareRow('Return', `<span class="${gainCls(m.return_pct)}">${esc(signedPct(m.return_pct))}</span>`, `<span class="${gainCls(s.return_pct)}">${esc(signedPct(s.return_pct))}</span>`)}
        ${shadowCompareRow('Realized P&amp;L', `<span class="${gainCls(m.realized_pnl_eur)}">${esc(signedEur(m.realized_pnl_eur || 0))}</span>`, `<span class="${gainCls(s.realized_pnl_eur)}">${esc(signedEur(s.realized_pnl_eur || 0))}</span>`)}
        ${shadowCompareRow('Closed trades', esc(m.closed_trades ?? 0), esc(s.closed_trades ?? 0))}
        ${shadowCompareRow('Win rate', winRate(m.win_rate_pct), winRate(s.win_rate_pct))}
        ${shadowCompareRow('Avg. trade', m.avg_trade_eur == null ? '--' : esc(signedEur(m.avg_trade_eur)), s.avg_trade_eur == null ? '--' : esc(signedEur(s.avg_trade_eur)))}
      </div>

      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
        <div class="ui-card kpi"><span class="ui-label">Value</span><span class="kpi-value">${esc(eur(s.total_value_eur))}</span><span class="kpi-sub ui-num">Cash ${esc(eur(s.cash_eur))}</span></div>
        <div class="ui-card kpi"><span class="ui-label">Open P&amp;L</span><span class="kpi-value ${gainCls(s.unrealized_pnl_eur)}">${esc(signedEur(s.unrealized_pnl_eur))}</span><span class="kpi-sub">${esc((s.open_positions || []).length)} positions</span></div>
        <div class="ui-card kpi"><span class="ui-label">Fees</span><span class="kpi-value">${esc(eur(s.fees_eur))}</span><span class="kpi-sub">Simulated commissions</span></div>
        <div class="ui-card kpi"><span class="ui-label">Shadow-only trades</span><span class="kpi-value">${esc(s.shadow_only_trades)}</span><span class="kpi-sub">The main bot skipped these</span></div>
      </div>

      <div class="space-y-2 ${(s.history && s.history.shadow && s.history.shadow.length > 1) ? '' : 'hidden'}">
        <div id="shadowReadout-${esc(s.key)}" class="ts-legend">
          <span><span class="ts-swatch" style="border-top: 2px solid var(--accent)"></span>${esc(s.label)} <b class="ui-num" data-v="0"></b></span>
          <span><span class="ts-swatch" style="border-top: 2px dashed var(--text-2)"></span>${esc(mainLabel)} <b class="ui-num" data-v="1"></b></span>
          <span class="ui-num ml-auto" data-v="at"></span>
        </div>
        <div class="relative h-44"><canvas id="shadowChart-${esc(s.key)}"></canvas></div>
      </div>

      ${list('Open positions', positions, 'No open shadow positions.')}
      ${list('Pending entries', pending, 'No pending shadow entries.')}
      ${list('Recently closed', closed, 'No closed shadow trades yet.')}
    </div>`;
}

function drawShadowChart(s, mainLabel) {
  const canvas = document.getElementById(`shadowChart-${s.key}`);
  if (!canvas || typeof Chart === 'undefined') return;
  if (shadowCharts[s.key]) { shadowCharts[s.key].destroy(); delete shadowCharts[s.key]; }
  const hist = s.history || {};
  if (!hist.shadow || hist.shadow.length < 2) return;
  const labels = hist.shadow.map(p => p[0].slice(5));  // 'MM-DD HH:MM'
  const mainMap = new Map((hist.main || []).map(p => [p[0].slice(5), p[1]]));
  shadowCharts[s.key] = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: s.label, data: hist.shadow.map(p => p[1]), borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: mainLabel, data: labels.map(l => mainMap.has(l) ? mainMap.get(l) : null), borderWidth: 2, borderDash: [4, 4], pointRadius: 0, tension: 0.2, spanGaps: true }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false } // values show in the legend row above the chart
      },
      scales: {
        x: { ticks: { maxTicksLimit: 4, maxRotation: 0, callback(v) { return String(this.getLabelForValue(v)).slice(0, 5); } }, grid: { display: false } },
        y: { ticks: { callback: v => `${v}%` }, grid: {} }
      }
    }
  });
  // Legend row doubles as the readout: latest values at rest, hovered values and time while hovering
  const readout = document.getElementById(`shadowReadout-${s.key}`);
  const chart = shadowCharts[s.key];
  chart.$readout = i => {
    const at = i === null ? labels.length - 1 : i;
    chart.data.datasets.forEach((ds, d) => {
      const v = ds.data[at];
      readout.querySelector(`[data-v="${d}"]`).textContent = v == null ? '--' : signedPct(v);
    });
    readout.querySelector('[data-v="at"]').textContent = i === null ? 'Latest'
      : new Date(hist.shadow[i][0].replace(' ', 'T') + 'Z').toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  };
  chart.$readout(null);
  styleShadowChart(chart);
  chart.update('none');
}

async function loadShadow() {
  const container = document.getElementById('shadowContainer');
  if (!container) return;
  try {
    const res = await fetch('/api/shadow');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    const mainLabel = (data.main && data.main.label) || 'Main';
    shadowData = data;
    if (!data.enabled || !(data.strategies || []).length) {
      const presets = (data.presets || []).map(p => `<li><span class="ui-num" style="color:var(--text)">${esc(p.key)}</span>: ${esc(p.description)}</li>`).join('');
      container.innerHTML = `<div class="ui-card ui-card-pad text-[13px] ui-muted space-y-2">
        <p>No shadow strategy is enabled. Set <span class="ui-num">SHADOW_ENABLED=true</span> and <span class="ui-num">SHADOW_STRATEGIES</span> in .env, then restart.</p>
        <ul class="list-disc pl-5 space-y-1">${presets}</ul>
      </div>`;
      return;
    }
    const openHist = [...container.querySelectorAll('details.sh-hist[open]')].map(d => d.dataset.key);
    container.innerHTML = data.strategies.map(s => renderShadowStrategy(s, mainLabel)).join('');
    data.strategies.forEach(s => drawShadowChart(s, mainLabel));
    container.querySelectorAll('details.sh-hist').forEach(d => {
      if (openHist.includes(d.dataset.key)) { d.open = true; loadShadowHistory(d.dataset.key); }
    });
  } catch (e) {
    container.innerHTML = `<div class="ui-card ui-card-pad text-[13px] ui-muted">Could not load shadow portfolios.</div>`;
    console.debug('Error loading shadow portfolios:', e);
  }
}

async function resetShadow(key) {
  if (!key) return;
  if (!confirm(`Reset the "${key}" shadow portfolio? Its simulated positions and history are deleted and it restarts with fresh capital. The main portfolio is not touched.`)) return;
  try {
    const res = await fetch(`/api/shadow/${encodeURIComponent(key)}/reset`, { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(data.message);
      loadShadow();
    } else {
      showToast(data.detail || data.message || 'Reset failed.', true);
    }
  } catch (e) {
    showToast('Error resetting shadow portfolio.', true);
  }
}

// ------------------------------------------------------------------
// Shadow editor: create / edit / copy / delete, learning strength, change history
// ------------------------------------------------------------------
let shadowData = null;      // last /api/shadow payload
let shadowSchema = null;    // /api/shadow/schema, fetched once
const shadowEditor = { key: null, initial: {}, touched: new Set(), afterClose: null };
const SHADOW_STRENGTH = ['Off', 'Low', 'Medium', 'High'];

async function shadowApi(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || 'Request failed.');
  return data;
}

function shadowStrengthControl(s) {
  const n = s.learning_strength || 0;
  const stops = SHADOW_STRENGTH.map((l, i) => `<span style="--i:${i}" class="${i === n ? 'is-on' : ''}">${l}</span>`).join('');
  return `<div class="sh-strength" data-sh-strength="${esc(s.key)}">
    <label class="ui-label" for="shStrength-${esc(s.key)}">Learning strength</label>
    <input type="range" class="se-range" id="shStrength-${esc(s.key)}" min="0" max="3" step="1" value="${esc(n)}"
      data-action="set-shadow-strength" data-key="${esc(s.key)}" aria-valuetext="${SHADOW_STRENGTH[n]}">
    <div class="sh-stops" aria-hidden="true">${stops}</div>
    <p class="se-note ${n > 0 ? '' : 'hidden'}" data-sh-note>Under 30 closed trades the result is noise. A learning shadow is no longer a clean comparison.</p>
  </div>`;
}

// Stop labels, aria text, badge and note follow the slider in place, so keyboard focus stays on it
function syncShadowStrength(el) {
  const n = Number(el.value);
  const box = el.closest('.sh-strength');
  el.setAttribute('aria-valuetext', SHADOW_STRENGTH[n]);
  box.querySelectorAll('.sh-stops span').forEach((sp, i) => sp.classList.toggle('is-on', i === n));
  box.querySelector('[data-sh-note]').classList.toggle('hidden', n === 0);
  const badge = el.closest('.ui-card').querySelector('[data-sh-badge]');
  if (badge) badge.classList.toggle('hidden', n === 0);
}

async function setShadowStrength(key, value) {
  try {
    await shadowApi('PATCH', `/api/shadow/${encodeURIComponent(key)}`, { learning_strength: value });
    showToast(`Learning ${SHADOW_STRENGTH[value].toLowerCase()}.`);
  } catch (e) {
    showToast(e.message, true);
    loadShadow(); // put the slider back to the saved value
  }
}

async function copyShadow(key) {
  const src = shadowData && (shadowData.strategies || []).find(x => x.key === key);
  const label = prompt('Name for the copy:', src ? `${src.label} copy` : '');
  if (!label || !label.trim()) return;
  try {
    await shadowApi('POST', `/api/shadow/${encodeURIComponent(key)}/copy`, { label: label.trim() });
    showToast('Shadow copied.');
    loadShadow();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function deleteShadow(key) {
  const src = shadowData && (shadowData.strategies || []).find(x => x.key === key);
  if (!confirm(`Delete shadow '${src ? src.label : key}' with all its positions and history?`)) return;
  try {
    await shadowApi('DELETE', `/api/shadow/${encodeURIComponent(key)}`);
    showToast('Shadow deleted.');
    loadShadow();
  } catch (e) {
    showToast(e.message, true);
  }
}

async function loadShadowHistory(key) {
  const d = document.querySelector(`details.sh-hist[data-key="${CSS.escape(key)}"] .sh-hist-list`);
  if (!d) return;
  try {
    const rows = await shadowApi('GET', `/api/shadow/${encodeURIComponent(key)}/history`);
    if (!rows.length) { d.innerHTML = '<p class="py-2 text-[13px] ui-faint">No changes yet.</p>'; return; }
    d.innerHTML = rows.map(r => {
      const diffs = Object.entries(r.changes || {}).map(([k, v]) => `<div class="ui-num">${esc(k)}: ${esc(v.old ?? 'main bot')} &rarr; ${esc(v.new ?? 'main bot')}</div>`).join('');
      const when = r.created_at ? new Date(r.created_at + (r.created_at.endsWith('Z') ? '' : 'Z')).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
      return `<div class="sh-hist-item">
        <div class="min-w-0">
          <div class="ui-faint text-xs">${esc(when)} · ${esc(r.source)}</div>
          ${diffs}
          ${r.reason ? `<div class="ui-muted text-xs">${esc(r.reason)}</div>` : ''}
        </div>
        <button type="button" class="ui-btn ui-btn-sm shrink-0" data-action="undo-shadow-change" data-key="${esc(key)}" data-id="${esc(r.id)}">Undo</button>
      </div>`;
    }).join('');
  } catch (e) {
    d.innerHTML = `<p class="py-2 text-[13px] ui-faint">${esc(e.message)}</p>`;
  }
}

async function undoShadowChange(key, id) {
  try {
    await shadowApi('POST', `/api/shadow/${encodeURIComponent(key)}/history/${id}/undo`);
    showToast('Change undone.');
    loadShadow();
  } catch (e) {
    showToast(e.message, true);
  }
}

function shadowParamField(spec, value) {
  const n = esc(spec.name);
  const nullToggle = spec.nullable && spec.kind !== 'bool' ? `
      <label class="se-row set-toggle-row">
        <span>Use main bot value</span>
        <span class="check-wrap set-toggle"><input type="checkbox" data-se-null="${n}" ${value === null ? 'checked' : ''}><span class="ui-switch" aria-hidden="true"></span></span>
      </label>` : '';
  const note = spec.learnable === false ? '<p class="se-note">Only you can change this. Learning never touches it.</p>' : '';
  let control;
  if (spec.kind === 'bool') {
    // nullable bool (regime_filter): a select keeps "main bot value" as a real third state
    if (spec.nullable) {
      const cur = value === null ? 'null' : String(value);
      control = `<div class="se-row"><label for="se-${n}">${esc(spec.label)}</label>
        <select id="se-${n}" class="ui-input" style="width:auto" data-se-param="${n}">
          <option value="null" ${cur === 'null' ? 'selected' : ''}>Main bot value</option>
          <option value="true" ${cur === 'true' ? 'selected' : ''}>On</option>
          <option value="false" ${cur === 'false' ? 'selected' : ''}>Off</option></select></div>`;
    } else {
      control = `<label class="se-row"><span>${esc(spec.label)}</span>
        <span class="check-wrap set-toggle"><input type="checkbox" data-se-param="${n}" ${value ? 'checked' : ''}><span class="ui-switch" aria-hidden="true"></span></span></label>`;
    }
  } else if (spec.kind === 'choice') {
    control = `<div class="se-row"><label for="se-${n}">${esc(spec.label)}</label>
      <select id="se-${n}" class="ui-input" style="width:auto" data-se-param="${n}">
        ${(spec.choices || []).map(c => `<option value="${esc(c)}" ${c === value ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></div>`;
  } else if ((spec.max - spec.min) / spec.step > 200) {
    // Too many stops for a slider: type the number instead
    control = `<div class="se-row"><label for="se-${n}">${esc(spec.label)}</label>
        <input type="number" id="se-${n}" class="ui-input se-num ui-num" data-se-param="${n}" inputmode="decimal" min="${esc(spec.min)}" max="${esc(spec.max)}" step="${esc(spec.step)}" value="${value === null ? '' : esc(value)}" ${value === null ? 'disabled' : ''}></div>`;
  } else {
    const shown = value === null ? spec.min : value;
    control = `<div class="se-row"><label for="se-${n}">${esc(spec.label)}</label>
        <span class="se-val ui-num" data-se-val="${n}">${value === null ? 'main bot' : esc(value)}</span></div>
      <input type="range" class="se-range" id="se-${n}" data-se-param="${n}" min="${esc(spec.min)}" max="${esc(spec.max)}" step="${esc(spec.step)}" value="${esc(shown)}" ${value === null ? 'disabled' : ''}>`;
  }
  return `<div class="se-param ${value === null && spec.kind !== 'bool' ? 'is-muted' : ''}" data-se-field="${n}">${control}${nullToggle}${note}</div>`;
}

function readShadowEditorValue(spec) {
  const el = document.querySelector(`#seBody [data-se-param="${spec.name}"]`);
  const nul = document.querySelector(`#seBody [data-se-null="${spec.name}"]`);
  if (nul && nul.checked) return null;
  if (spec.kind === 'bool') {
    if (el.tagName === 'SELECT') return el.value === 'null' ? null : el.value === 'true';
    return el.checked;
  }
  if (spec.kind === 'choice') return el.value;
  return spec.kind === 'int' ? parseInt(el.value, 10) : parseFloat(el.value);
}

// New shadow: every param as shown (the sheet is the source of truth). Edit: only what the user touched.
function collectShadowParams(isNew) {
  const params = {};
  for (const sp of shadowSchema) {
    if (!shadowEditor.touched.has(sp.name)) {
      if (isNew) params[sp.name] = shadowEditor.initial[sp.name];
      continue;
    }
    const v = readShadowEditorValue(sp);
    if (typeof v === 'number' && Number.isNaN(v)) throw new Error(`Enter a number for ${sp.label}.`);
    if (isNew || v !== shadowEditor.initial[sp.name]) params[sp.name] = v;
  }
  return params;
}

async function openShadowEditor(key) {
  const dlg = document.getElementById('shadowEditor');
  if (!dlg) return;
  try {
    if (!shadowSchema) shadowSchema = await shadowApi('GET', '/api/shadow/schema');
  } catch (e) { showToast(e.message, true); return; }
  const strategies = (shadowData && shadowData.strategies) || [];
  const src = key ? strategies.find(x => x.key === key) : (strategies.find(x => x.key === 'baseline') || strategies[0]);
  if (key && !src) { showToast('Shadow not found. Refresh and try again.', true); return; }
  const base = (src && src.params) || {};
  shadowEditor.key = key;
  shadowEditor.initial = {};
  shadowEditor.touched = new Set();
  shadowEditor.afterClose = null;
  shadowSchema.forEach(sp => {
    shadowEditor.initial[sp.name] = base[sp.name] === undefined ? (sp.nullable ? null : sp.min) : base[sp.name];
  });
  document.getElementById('seTitle').textContent = key ? `Edit ${src.label}` : 'New shadow';
  document.getElementById('seError').classList.add('hidden');
  document.getElementById('seBody').innerHTML = `
    <div class="se-field"><label class="ui-label" for="seLabel">Name</label>
      <input id="seLabel" class="ui-input" maxlength="60" value="${key ? esc(src.label) : ''}" autocomplete="off"></div>
    <div class="se-field"><label class="ui-label" for="seDesc">Description</label>
      <textarea id="seDesc" class="ui-input" maxlength="300">${key ? esc(src.description) : ''}</textarea></div>
    ${shadowSchema.map(sp => shadowParamField(sp, shadowEditor.initial[sp.name])).join('')}`;
  if (!dlg.open) { dlg.classList.remove('is-closing'); dlg.showModal(); }
  document.getElementById('seLabel').focus();
}

function closeShadowEditor() {
  const dlg = document.getElementById('shadowEditor');
  if (!dlg || !dlg.open || dlg.classList.contains('is-closing')) return;
  dlg.classList.add('is-closing');
  let done = false;
  const finish = () => { if (done) return; done = true; dlg.classList.remove('is-closing'); dlg.close(); };
  dlg.addEventListener('animationend', e => { if (e.target === dlg) finish(); }, { once: true });
  setTimeout(finish, 250);
}

async function saveShadowEditor() {
  const err = document.getElementById('seError');
  err.classList.add('hidden');
  const label = document.getElementById('seLabel').value.trim();
  const description = document.getElementById('seDesc').value.trim();
  if (!label) { err.textContent = 'Enter a name.'; err.classList.remove('hidden'); return; }
  let params;
  try { params = collectShadowParams(!shadowEditor.key); }
  catch (e) { err.textContent = e.message; err.classList.remove('hidden'); return; }
  const btn = document.querySelector('#shadowEditor [data-se-save]');
  btn.disabled = true;
  try {
    if (shadowEditor.key) {
      const body = { label, description };
      if (Object.keys(params).length) body.params = params;
      await shadowApi('PATCH', `/api/shadow/${encodeURIComponent(shadowEditor.key)}`, body);
      showToast('Shadow saved.');
    } else {
      await shadowApi('POST', '/api/shadow', { label, description, params });
      showToast('Shadow created.');
    }
    const editKey = shadowEditor.key;
    await loadShadow();
    shadowEditor.afterClose = () => {
      const target = editKey ? document.querySelector(`[data-action="edit-shadow"][data-key="${CSS.escape(editKey)}"]`)
        : document.querySelector('[data-action="new-shadow"]');
      if (target) target.focus();
    };
    closeShadowEditor();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

(function initShadowEditor() {
  const dlg = document.getElementById('shadowEditor');
  if (!dlg) return;
  dlg.addEventListener('cancel', e => { e.preventDefault(); closeShadowEditor(); });
  dlg.addEventListener('close', () => { const f = shadowEditor.afterClose; shadowEditor.afterClose = null; if (f) f(); });
  dlg.addEventListener('click', e => {
    if (e.target === dlg || e.target.closest('[data-se-close]')) return closeShadowEditor();
    if (e.target.closest('[data-se-save]')) saveShadowEditor();
  });
  dlg.addEventListener('input', e => {
    const el = e.target;
    if (el.dataset.seParam) shadowEditor.touched.add(el.dataset.seParam);
    if (el.type === 'range' && el.dataset.seParam) {
      dlg.querySelector(`[data-se-val="${el.dataset.seParam}"]`).textContent = el.value;
    }
  });
  dlg.addEventListener('change', e => {
    const el = e.target;
    if (el.dataset.seParam) shadowEditor.touched.add(el.dataset.seParam); // selects and checkboxes fire change only
    const name = el.dataset.seNull;
    if (!name) return;
    shadowEditor.touched.add(name);
    const field = dlg.querySelector(`[data-se-field="${name}"]`);
    const input = field.querySelector('input[data-se-param], select[data-se-param]');
    const val = field.querySelector('[data-se-val]');
    if (input) input.disabled = el.checked;
    field.classList.toggle('is-muted', el.checked);
    if (val && input) val.textContent = el.checked ? 'main bot' : input.value;
  });
})();

// Strength slider commits on release; history loads the first time its panel opens
document.addEventListener('input', e => {
  const el = e.target.closest('[data-action="set-shadow-strength"]');
  if (el) syncShadowStrength(el);
});
document.addEventListener('change', e => {
  const el = e.target.closest('[data-action="set-shadow-strength"]');
  if (el) setShadowStrength(el.dataset.key, Number(el.value));
});
document.addEventListener('toggle', e => {
  if (e.target.matches && e.target.matches('details.sh-hist') && e.target.open) loadShadowHistory(e.target.dataset.key);
}, true);

// ------------------------------------------------------------------
// Ticker detail sheet: price chart with trade levels for one ticker
// (/api/ticker/{ticker}/detail). Opens from any data-action="open-ticker".
// ------------------------------------------------------------------
function tickerLink(ticker, source = '') {
  return `<button type="button" class="ticker-link" data-action="open-ticker" data-ticker="${esc(ticker)}" data-source="${esc(source)}">${esc(ticker)}</button>`;
}

const TS_LEVELS = {
  entry: { label: 'Entry', color: '--text-2' },
  limit: { label: 'Limit', color: '--accent' },
  stop: { label: 'Stop', color: '--loss' },
  take_profit: { label: 'Take profit', color: '--gain' },
  high: { label: 'High', color: '--text-3' },
  breakeven_at: { label: 'Breakeven at', color: '--text-3' },
};
const tickerSheet = { ticker: '', source: '', range: '3M', data: null, selected: [], shown: '', chart: null, req: 0, backPending: false };
const tsReducedMotion = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

function tsLineDash(key, set) {
  if (key === 'high' || key === 'breakeven_at') return [2, 3];
  const solid = set.state === 'live' || (key === 'entry' && set.state === 'simulated');
  return solid ? [] : [6, 4];
}

// Inline Chart.js plugin: horizontal level lines with a right-edge label
const tsLevelPlugin = {
  id: 'tsLevels',
  beforeDatasetsDraw(chart, args, opts) { // lines under the price line, so trade markers stay visible
    const lines = opts.lines || [];
    if (!lines.length) return;
    const { ctx, chartArea: a, scales: { y } } = chart;
    ctx.save();
    lines.forEach(l => {
      const py = y.getPixelForValue(l.value);
      ctx.strokeStyle = l.color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(l.dash);
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke();
    });
    ctx.restore();
  },
  afterDatasetsDraw(chart, args, opts) { // labels on top, so the price line never crosses them
    const lines = opts.lines || [];
    if (!lines.length) return;
    const { ctx, chartArea: a, scales: { y } } = chart;
    ctx.save();
    ctx.font = `600 12px ${cssVar('--font-num')}`;
    const labels = lines.map(l => ({ ...l, py: y.getPixelForValue(l.value) }));
    // Nudge labels apart both ways so close levels stay readable, then keep them inside the plot
    labels.sort((p, q) => p.py - q.py);
    for (let i = 1; i < labels.length; i++) labels[i].py = Math.max(labels[i].py, labels[i - 1].py + 16);
    for (let i = labels.length - 2; i >= 0; i--) labels[i].py = Math.min(labels[i].py, labels[i + 1].py - 16);
    labels.forEach(l => { l.py = Math.min(Math.max(l.py, a.top + 8), a.bottom - 8); });
    const bg = cssVar('--surface');
    labels.forEach(l => {
      const w = ctx.measureText(l.text).width + 10;
      const x = a.right - w;
      ctx.fillStyle = bg;
      ctx.fillRect(x, l.py - 8, w, 16);
      ctx.fillStyle = l.color;
      ctx.textBaseline = 'middle';
      ctx.fillText(l.text, x + 5, l.py);
    });
    ctx.restore();
  }
};

function openTickerSheet(ticker, source = '') {
  const dlg = document.getElementById('tickerSheet');
  if (!dlg || !ticker) return;
  Object.assign(tickerSheet, { ticker, source, range: '3M', data: null, selected: [], shown: '' });
  document.getElementById('tsTicker').textContent = ticker;
  ['tsName', 'tsPrice'].forEach(id => { const el = document.getElementById(id); delete el.dataset.rest; el.textContent = ''; }); // Esc while hovering skips the restore
  if (!dlg.open) {
    dlg.classList.remove('is-closing');
    dlg.showModal();
    const push = () => history.pushState({ tickerSheet: true }, '');
    // A history.back() from the previous close may still be in flight; push after it lands
    if (tickerSheet.backPending) window.addEventListener('popstate', push, { once: true });
    else push();
  }
  loadTickerSheet();
}

function closeTickerSheet() {
  const dlg = document.getElementById('tickerSheet');
  if (!dlg || !dlg.open || dlg.classList.contains('is-closing')) return;
  dlg.classList.add('is-closing');
  let done = false;
  const finish = () => { if (done) return; done = true; dlg.classList.remove('is-closing'); dlg.close(); };
  dlg.addEventListener('animationend', e => { if (e.target === dlg) finish(); }, { once: true });
  setTimeout(finish, 250);
}

async function loadTickerSheet() {
  const req = ++tickerSheet.req;
  const { ticker, range } = tickerSheet;
  const dlg = document.getElementById('tickerSheet');
  // Skeleton only on first load; a range switch keeps the chart so it can animate to the new data
  if (!tickerSheet.data || tickerSheet.data.ticker !== ticker) dlg.dataset.state = 'loading';
  dlg.querySelectorAll('[data-range]').forEach(b => {
    b.classList.toggle('active-tab', b.dataset.range === range);
    b.setAttribute('aria-pressed', String(b.dataset.range === range));
  });
  const box = document.getElementById('tsChartBox');
  box.setAttribute('aria-busy', 'true');
  let data = null;
  try {
    const res = await fetch(`/api/ticker/${encodeURIComponent(ticker)}/detail?range=${encodeURIComponent(range)}`);
    if (res.ok) data = await res.json();
  } catch (e) { /* offline: error state below */ }
  if (req !== tickerSheet.req) return; // a newer range or ticker was requested
  box.removeAttribute('aria-busy');
  if (!data) { dlg.dataset.state = 'error'; return; }
  dlg.dataset.state = 'ready';
  const firstLoad = !tickerSheet.data || tickerSheet.data.ticker !== data.ticker;
  tickerSheet.data = data;
  if (data.ticker) tickerSheet.ticker = data.ticker; // server may normalise it; keeps range switches off the skeleton
  if (firstLoad) tsPickDefaultSet();
  document.getElementById('tsTicker').textContent = data.ticker || ticker;
  setRestingText(document.getElementById('tsName'), data.name || '');
  setRestingText(document.getElementById('tsPrice'), hasNum(data.current_price) ? `${num(data.current_price)} ${data.currency || ''}`.trim() : '');
  renderTickerSheet(!firstLoad);
}

function tsPickDefaultSet() {
  const sets = tickerSheet.data.level_sets || [];
  const pick = sets.find(s => s.id === tickerSheet.source)
    || sets.find(s => s.kind === 'position')
    || sets.find(s => s.kind === 'trigger' && s.status === 'PENDING')
    || sets.find(s => s.kind === 'report')
    || sets[0];
  tickerSheet.selected = pick ? [pick.id] : [];
  tickerSheet.shown = pick ? pick.id : '';
}

function renderTickerSheet(animate = false) {
  const d = tickerSheet.data;
  const sets = d.level_sets || [];
  const chosen = sets.filter(s => tickerSheet.selected.includes(s.id));
  const setsEl = document.getElementById('tsSets');
  const setsKey = JSON.stringify(sets.map(s => [s.id, s.label]));
  if (setsEl.dataset.key !== setsKey) { // rebuild only when the list changes, so a pressed chip keeps focus
    setsEl.dataset.key = setsKey;
    setsEl.innerHTML = sets.length
      ? sets.map(s => `<button type="button" class="ui-chip ts-chip" data-set="${esc(s.id)}" aria-pressed="${tickerSheet.selected.includes(s.id)}">${esc(s.label)}</button>`).join('')
      : '<p class="text-sm ui-muted">No trigger, position or advice for this ticker yet.</p>';
  } else {
    setsEl.querySelectorAll('[data-set]').forEach(b => b.setAttribute('aria-pressed', String(tickerSheet.selected.includes(b.dataset.set))));
  }
  renderTickerLegend(renderTickerChart(chosen, animate));
  renderTickerInfo(sets.find(s => s.id === tickerSheet.shown));
}

function renderTickerChart(chosen, animate) {
  const d = tickerSheet.data;
  const hist = Array.isArray(d.history) ? d.history : [];
  const box = document.getElementById('tsChartBox');
  const note = document.getElementById('tsChartNote');
  const empty = !hist.length || typeof Chart === 'undefined';
  box.classList.toggle('is-empty', empty);
  note.hidden = !empty;
  note.textContent = empty ? 'No price data for this range.' : '';
  if (tickerSheet.chart && (empty || !animate)) { tickerSheet.chart.destroy(); tickerSheet.chart = null; }
  if (empty) return [];

  const lines = [];
  chosen.forEach(s => Object.keys(TS_LEVELS).forEach(k => {
    const v = s.levels && s.levels[k];
    if (!hasNum(v) || lines.some(l => l.key === k && l.value === Number(v))) return; // two sets often share a level
    const color = cssVar(TS_LEVELS[k].color);
    lines.push({ key: k, value: Number(v), color, dash: tsLineDash(k, s), text: `${TS_LEVELS[k].label} ${num(v)}` });
  }));

  const labels = hist.map(p => p[0]);
  const closes = hist.map(p => Number(p[1]));
  const t0 = Date.parse(labels[0] + 'Z');
  const times = labels.map(l => Date.parse(l + 'Z'));
  // shortcut: shows every trade (main and all shadows), not only the selected shadow's; match by strategy once trade.source carries the shadow set id
  const trades = (d.trades || []).filter(t => hasNum(t.price) && Date.parse(t.at + 'Z') >= t0).map(t => {
    const at = Date.parse(t.at + 'Z');
    // Bars are stamped at their start: a trade belongs to the last bar at or before it; later trades clamp to the last bar
    const idx = Math.max(0, times.findLastIndex(x => x <= at));
    return { x: labels[idx], y: Number(t.price), sell: String(t.side).toUpperCase() === 'SELL' };
  });

  const all = closes.concat(lines.map(l => l.value), trades.map(t => t.y)).filter(Number.isFinite);
  const lo = Math.min(...all), hi = Math.max(...all);
  const pad = Math.max((hi - lo) * 0.06, hi * 0.005);
  const yMin = lo - pad, yMax = hi + pad;

  const intraday = tickerSheet.range === '1D';
  // Server times are naive UTC; show them in the viewer's time zone
  const fmtX = l => intraday
    ? new Date(l + 'Z').toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
    : new Date(l + 'Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const lineDs = {
    data: closes, borderColor: cssVar('--text'), borderWidth: 1.75, pointRadius: 0, pointHoverRadius: 4,
    pointHoverBackgroundColor: cssVar('--text'), pointHoverBorderColor: cssVar('--surface'), pointHoverBorderWidth: 2, fill: false, tension: 0,
  };
  const tradeDs = {
    data: trades, showLine: false, pointStyle: 'triangle', pointRadius: 6, pointHoverRadius: 6,
    rotation: trades.map(t => t.sell ? 180 : 0),
    backgroundColor: trades.map(t => cssVar(t.sell ? '--loss' : '--gain')),
    borderColor: cssVar('--surface'), borderWidth: 1,
  };
  const anim = tsReducedMotion() ? false : { duration: 200 };

  if (tickerSheet.chart) {
    const c = tickerSheet.chart;
    c.data.labels = labels;
    c.data.datasets[0].data = closes;
    Object.assign(c.data.datasets[1], tradeDs);
    c.options.scales.y.suggestedMin = yMin; c.options.scales.y.suggestedMax = yMax;
    c.options.scales.x.ticks.callback = function (v) { return fmtX(this.getLabelForValue(v)); };
    c.options.plugins.tsLevels.lines = lines;
    c.options.animation = anim;
    c.update();
    return lines;
  }
  tickerSheet.chart = new Chart(document.getElementById('tsChart'), {
    type: 'line',
    data: { labels, datasets: [lineDs, tradeDs] },
    plugins: [tsLevelPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: anim,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false }, // hovered close and time show in the sheet header instead
        tsLevels: { lines },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: cssVar('--text-3'), maxTicksLimit: 5, maxRotation: 0, callback: function (v) { return fmtX(this.getLabelForValue(v)); } } },
        y: { suggestedMin: yMin, suggestedMax: yMax, position: 'left', grid: { color: cssVar('--line-soft') }, ticks: { color: cssVar('--text-3'), maxTicksLimit: 6 } },
      },
    },
  });
  tickerSheet.chart.$readout = i => {
    const c = tickerSheet.chart;
    const at = i === null ? null : new Date(c.data.labels[i] + 'Z').toLocaleString('en-GB', tickerSheet.range === '1D'
      ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
      : { day: 'numeric', month: 'short', year: 'numeric' });
    setReadout(document.getElementById('tsPrice'), i === null ? null : `${num(c.data.datasets[0].data[i])} ${tickerSheet.data.currency || ''}`.trim());
    setReadout(document.getElementById('tsName'), at);
  };
  return lines;
}

function renderTickerLegend(lines) {
  const keys = Object.keys(TS_LEVELS).filter(k => lines.some(l => l.key === k));
  const el = document.getElementById('tsLegend');
  if (!keys.length) { el.innerHTML = ''; return; }
  const dotted = l => l.key === 'high' || l.key === 'breakeven_at';
  const swatch = (color, dash) => `<span class="ts-swatch" style="border-top: 2px ${dash} ${color}"></span>`;
  el.innerHTML = [
    lines.some(l => !dotted(l) && !l.dash.length) ? `<span>${swatch('var(--text-2)', 'solid')}Live</span>` : '',
    lines.some(l => !dotted(l) && l.dash.length) ? `<span>${swatch('var(--text-2)', 'dashed')}Would be, simulated or past</span>` : '',
    ...keys.map(k => `<span>${swatch(`var(${TS_LEVELS[k].color})`, k === 'high' || k === 'breakeven_at' ? 'dotted' : 'solid')}${esc(TS_LEVELS[k].label)}</span>`),
  ].join('');
}

function renderTickerInfo(set) {
  const el = document.getElementById('tsInfo');
  if (!set) { el.innerHTML = ''; return; }
  const price = Number(tickerSheet.data.current_price);
  const lv = set.levels || {};
  const info = set.info || {};
  const dist = v => (hasNum(price) && price > 0) ? signedPct((Number(v) - price) / price * 100, 1) : '--';
  const row = (k, v) => `<div class="ts-row"><dt>${k}</dt><dd class="ui-num">${v}</dd></div>`;
  const badgeCls = { live: 'ui-badge-accent', unfunded: 'ui-badge-warn' }[set.state] || '';
  const stateLabel = { live: 'Live', unfunded: 'Unfunded', would_be: 'Not armed', past: 'Past', simulated: 'Simulated' }[set.state] || set.state;
  const notes = [];
  if (set.state === 'unfunded') notes.push('Waiting for cash or a free slot.');
  if (set.state === 'would_be' && set.kind === 'report') notes.push('Lines show where the trigger would be.');
  if (set.kind === 'position' && hasNum(lv.breakeven_at)) notes.push(`Stop moves to breakeven + 1% at ${num(lv.breakeven_at)} (${dist(lv.breakeven_at)}).`);
  const rows = Object.keys(TS_LEVELS).filter(k => k !== 'breakeven_at' && hasNum(lv[k]))
    .map(k => row(TS_LEVELS[k].label, `${num(lv[k])} <span class="ui-faint">${dist(lv[k])}</span>`));
  if (hasNum(info.allocation_eur)) rows.push(row('Allocation', eur(info.allocation_eur)));
  if (hasNum(info.planned_shares)) rows.push(row('Shares', num(info.planned_shares, 4).replace(/\.?0+$/, '')));
  if (hasNum(info.score)) rows.push(row('Score', esc(info.score)));
  if (hasNum(info.pnl_pct)) rows.push(row('P&amp;L', `<span class="${gainCls(info.pnl_pct)}">${signedPct(info.pnl_pct)}</span>`));
  if (info.exit_reason) rows.push(row('Exit reason', esc(info.exit_reason)));
  if (info.created_at) rows.push(row('Created', esc(String(info.created_at).replace('T', ' ').slice(0, 16))));
  if (info.closed_at) rows.push(row('Closed', esc(String(info.closed_at).replace('T', ' ').slice(0, 16))));
  el.innerHTML = `
    <div class="flex flex-wrap items-center gap-2 mb-2">
      <h3 class="ts-info-title">${esc(set.label)}</h3>
      ${set.status && set.kind.includes('trigger') ? triggerStatusBadge(set.status) : `<span class="ui-badge ${badgeCls}">${esc(stateLabel)}</span>`}
      ${set.trigger_type ? `<span class="text-xs ui-muted">${esc(triggerTypeLabel(set.trigger_type))}</span>` : ''}
    </div>
    ${notes.map(n => `<p class="text-sm ui-muted mb-2">${esc(n)}</p>`).join('')}
    <dl class="ts-rows">${rows.join('')}</dl>
    ${info.rationale ? `<p class="ts-rationale">${esc(info.rationale)}</p>` : ''}`;
}

(function initTickerSheet() {
  const dlg = document.getElementById('tickerSheet');
  if (!dlg) return;
  if (history.state && history.state.tickerSheet) history.replaceState(null, ''); // left over from a reload with the sheet open
  dlg.addEventListener('cancel', e => { e.preventDefault(); closeTickerSheet(); }); // Escape: animate out
  dlg.addEventListener('click', e => {
    if (e.target === dlg || e.target.closest('[data-ts-close]')) return closeTickerSheet(); // backdrop or X
    const r = e.target.closest('[data-range]');
    if (r && r.dataset.range !== tickerSheet.range) { tickerSheet.range = r.dataset.range; loadTickerSheet(); return; }
    if (e.target.closest('[data-ts-retry]')) { loadTickerSheet(); return; }
    const chip = e.target.closest('[data-set]');
    if (chip) {
      const id = chip.dataset.set;
      const sel = tickerSheet.selected;
      tickerSheet.selected = sel.includes(id) ? sel.filter(x => x !== id) : sel.concat(id);
      if (tickerSheet.selected.includes(id)) tickerSheet.shown = id;
      else if (tickerSheet.shown === id) tickerSheet.shown = tickerSheet.selected[tickerSheet.selected.length - 1] || '';
      renderTickerSheet(true);
    }
  });
  // Every close path ends here: drop our history entry if it is still on top
  dlg.addEventListener('close', () => {
    tickerSheet.req++; // ignore responses still in flight
    if (history.state && history.state.tickerSheet) {
      tickerSheet.backPending = true;
      history.back();
    }
  });
  window.addEventListener('popstate', () => {
    if (tickerSheet.backPending) { tickerSheet.backPending = false; return; } // our own back() from 'close'
    if (dlg.open) closeTickerSheet(); // browser back gesture
  });
  // Theme flip: re-read canvas colours by rebuilding the open chart
  if (window.matchMedia) {
    const q = matchMedia('(prefers-color-scheme: dark)');
    if (q.addEventListener) q.addEventListener('change', () => { if (dlg.open && tickerSheet.data) renderTickerSheet(false); });
  }
})();
