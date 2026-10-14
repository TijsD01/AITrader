// Global State
let allocationChart = null;
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
function renderAllocationBar(cashEur, positions) {
  const bar = document.getElementById('allocationBar');
  const legend = document.getElementById('allocationLegend');
  if (!bar) return;
  const parts = positions.map((p, i) => ({
    label: String(p.ticker || ''),
    value: Math.max(0, Number(p.current_value_eur) || 0),
    color: ALLOC_COLORS[i % ALLOC_COLORS.length]
  }));
  parts.push({ label: 'Cash', value: Math.max(0, cashEur), color: ALLOC_CASH_COLOR });
  const total = parts.reduce((s, p) => s + p.value, 0);
  if (total <= 0) {
    bar.innerHTML = '<span style="flex:1;background:var(--line-strong)"></span>';
    if (legend) legend.innerHTML = '';
    return;
  }
  const shown = parts.filter(p => p.value > 0);
  bar.innerHTML = shown.map(p => `<span style="flex:${(p.value / total * 1000).toFixed(0)};background:${p.color}" title="${esc(p.label)}"></span>`).join('');
  bar.setAttribute('aria-label', 'Capital allocation: ' + shown.map(p => `${p.label} ${(p.value / total * 100).toFixed(0)}%`).join(', '));
  if (legend) {
    legend.innerHTML = positions.length === 0 ? '' : shown.map(p =>
      `<span><i style="background:${p.color}"></i>${esc(p.label)} <span class="ui-num text-slate-300">${(p.value / total * 100).toFixed(0)}%</span></span>`
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
      <div class="ui-num text-[22px] font-semibold text-white leading-none">${shown}<span class="ui-faint text-[12px] font-normal">/100</span></div>
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
  loadAllData();
  switchTabUi('portfolio');

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
    case 'toggle-position': {
      const row = el.closest('.pos-row');
      if (row) {
        const open = row.classList.toggle('open');
        el.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) openPositions.add(ticker); else openPositions.delete(ticker);
      }
      break;
    }
    case 'buy': executePaperBuy(ticker); break;
    case 'arm-trigger': armAITrigger(ticker); break;
    case 'analyze': analyzeDirect(ticker); break;
    case 'inspect': inspectReport(Number(el.dataset.idx)); break;
    case 'cancel-trigger': cancelTrigger(Number(el.dataset.id), ticker); break;
    case 'reset-shadow': resetShadow(el.dataset.key || ''); break;
    default: break;
  }
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

  toast.classList.remove('translate-y-[-100px]', 'opacity-0');
  toast.classList.add('translate-y-0', 'opacity-100');

  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove('translate-y-0', 'opacity-100');
    toast.classList.add('translate-y-[-100px]', 'opacity-0');
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
  const menu = document.getElementById('mobileDropdownMenu');
  const backdrop = document.getElementById('mobileDropdownBackdrop');
  const chevron = document.getElementById('mobileDropdownChevron');
  if (!menu) return;
  const moreBtn = document.getElementById('mobileMenuDropdownBtn');
  const isHidden = menu.classList.contains('hidden');
  if (isHidden) {
    menu.classList.remove('hidden');
    if (backdrop) backdrop.classList.remove('hidden');
    if (chevron) chevron.classList.add('rotate-180');
    if (moreBtn) moreBtn.setAttribute('aria-expanded', 'true');
  } else {
    closeMobileDropdown();
  }
}

// Close Mobile Dropdown Menu
function closeMobileDropdown() {
  const menu = document.getElementById('mobileDropdownMenu');
  const backdrop = document.getElementById('mobileDropdownBackdrop');
  const chevron = document.getElementById('mobileDropdownChevron');
  const moreBtn = document.getElementById('mobileMenuDropdownBtn');
  if (menu) menu.classList.add('hidden');
  if (backdrop) backdrop.classList.add('hidden');
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
const MORE_TABS = ['screener', 'debate', 'shadow', 'memory', 'settings'];

function switchTabUi(tabId) {
  currentTab = tabId;
  // Bottom nav active state ("More" lights up for the tabs that live inside it)
  document.querySelectorAll('.bottom-nav-btn').forEach(btn => {
    const isMore = btn.dataset.more === '1';
    const active = isMore ? MORE_TABS.includes(tabId) : btn.dataset.tab === tabId;
    btn.classList.toggle('active', active);
    if (active) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  });
}

function switchTab(tabId) {
  switchTabUi(tabId);
  // Hide all contents
  document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
  const target = document.getElementById(`tab-${tabId}`);
  if (target) target.classList.remove('hidden');

  // Update desktop nav
  document.querySelectorAll('.tab-btn').forEach(btn => {
    if (btn.dataset.tab === tabId) {
      btn.classList.add('active-tab', 'text-emerald-400');
      btn.classList.remove('text-slate-400');
    } else {
      btn.classList.remove('active-tab', 'text-emerald-400');
      btn.classList.add('text-slate-400');
    }
  });

  // Update mobile dropdown button header label and icon
  const cfg = TAB_CONFIG[tabId] || { label: tabId, icon: 'fa-solid fa-bars' };
  const mobileLabel = document.getElementById('mobileDropdownActiveText');
  const mobileIcon = document.getElementById('mobileDropdownActiveIcon');
  const inMore = MORE_TABS.includes(tabId);
  const SHORT = { screener: 'Screener', debate: 'Debate', shadow: 'Shadow', memory: 'Memory', settings: 'Settings' };
  if (mobileLabel) mobileLabel.textContent = inMore ? (SHORT[tabId] || cfg.label) : 'More';
  if (mobileIcon) mobileIcon.className = inMore ? cfg.icon : 'fa-solid fa-table-cells-large';

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
  if (tabId === 'history') { loadTrades(); loadLearningInsights(); }
  if (tabId === 'shadow') loadShadow();
  if (tabId === 'memory') loadMemoryHub();
  if (tabId === 'settings') { loadSettings(); loadBrokerStatus(); }
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

// Allocation colours: one per holding, cash is a neutral grey
const ALLOC_COLORS = ['#8EA2FF', '#6ED3E8', '#F5B454', '#C4A6FF', '#4ADE9B', '#FF9F7A', '#7FB2FF', '#E9D86E'];
const ALLOC_CASH_COLOR = '#3A4459';
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
        borderColor: '#121722'
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: '#A3ADBD', font: { size: 12, family: "'IBM Plex Sans', system-ui, sans-serif" }, boxWidth: 12 }
        }
      },
      cutout: '72%'
    }
  });
}

// 2. Load Portfolio & Positions
async function loadPortfolio() {
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
    const totalVal = (summary.cash_eur || 0) + equityVal;
    const rawReturn = summary.initial_capital_eur ? (((totalVal / summary.initial_capital_eur) - 1.0) * 100) : (summary.total_return_pct ?? 0);
    const totalReturnPct = Number.isFinite(rawReturn) ? rawReturn : 0;

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

    document.getElementById('metricTotalVal').textContent = eur(totalVal);
    document.getElementById('metricCash').textContent = eur(deployableCash);
    document.getElementById('metricEquity').textContent = eur(equityVal);
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
        limitBadge.className = 'ui-num text-amber-300';
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
        subParts.push(`<span class="text-amber-300">${eur(protectedReserve)} protected</span>`);
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
    const initialCap = Number(summary.initial_capital_eur) || 0;
    const absReturn = initialCap ? totalVal - initialCap : null;
    returnElem.innerHTML = `<i class="fa-solid fa-arrow-${up ? 'up' : 'down'}-long text-xs" aria-hidden="true"></i>`
      + (absReturn !== null ? `<span class="ui-num">${signedEur(absReturn)}</span>` : '')
      + `<span class="ui-num">${up ? '+' : ''}${totalReturnPct.toFixed(2)}%</span>`
      + `<span class="text-slate-500 font-medium">all time</span>`;
    returnElem.className = `flex items-center gap-2 text-sm font-semibold ${up ? 'ui-gain' : 'ui-loss'}`;

    document.getElementById('metricPosCount').textContent = `${positions.length} open position${positions.length === 1 ? '' : 's'}`;
    const posCount = document.getElementById('positionsCount');
    if (posCount) posCount.textContent = String(positions.length);

    // Allocation bar (all screens) + doughnut (desktop)
    renderAllocationBar(Number(summary.cash_eur) || 0, positions);

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

    // Render position rows (tap a row to see entry, stops and the sell button)
    const container = document.getElementById('positionsContainer');
    if (positions.length === 0) {
      container.innerHTML = `
        <div class="text-center px-6 py-10 text-slate-400 text-sm">
          <i class="fa-solid fa-seedling text-xl mb-2 text-slate-500 block"></i>
          No open positions yet. The committee opens positions during market hours or when you buy from Advice.
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
      return `
        <div class="pos-row${isOpen ? ' open' : ''}">
          <button type="button" class="pos-head" data-action="toggle-position" data-ticker="${esc(pos.ticker)}" aria-expanded="${isOpen ? 'true' : 'false'}">
            <span class="pos-mono" style="color:${color}">${esc(mono)}</span>
            <span class="flex-1 min-w-0">
              <span class="block font-semibold text-[15px] text-white truncate">${esc(pos.ticker)}</span>
              <span class="block text-xs text-slate-500 truncate">${shares}${pos.name ? ` · ${esc(pos.name)}` : ''}</span>
            </span>
            <span class="text-right shrink-0">
              <span class="block ui-num font-semibold text-[15px] text-white">${eur(pos.current_value_eur)}</span>
              <span class="block ui-num text-xs font-semibold ${tone}">${pctText}</span>
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
            <button type="button" data-action="close-position" data-ticker="${esc(pos.ticker)}" class="ui-btn ui-btn-danger w-full mt-3" aria-label="Sell position ${esc(pos.ticker)}">
              <i class="fa-solid fa-arrow-right-from-bracket"></i> Sell position
            </button>
          </div>
        </div>
      `;
    }).join('');

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
    const container = document.getElementById('adviceContainer');
    const countEl = document.getElementById('adviceCount');
    if (countEl) countEl.textContent = adviceReportsCache.length ? String(adviceReportsCache.length) : '';

    if (adviceReportsCache.length === 0) {
      container.innerHTML = `
        <div class="col-span-full ui-card ui-card-pad adv-prose">
          No advice yet today. Tap <strong class="text-white">Run Scan</strong> in the top bar to find candidates and run the committee.
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
                <span class="text-[17px] font-bold text-white">${esc(r.ticker)}</span>
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

          <div class="grid grid-cols-3 gap-2 mt-auto">
            <button type="button" data-action="buy" data-ticker="${esc(r.ticker)}" class="ui-btn ui-btn-primary !px-2 !gap-1.5" title="Market buy now">
              <i class="fa-solid fa-cart-shopping"></i> Buy
            </button>
            <button type="button" data-action="arm-trigger" data-ticker="${esc(r.ticker)}" class="ui-btn !px-2 !gap-1.5" title="Arm an AI-calculated limit trigger">
              <i class="fa-solid fa-crosshairs"></i> Trigger
            </button>
            <button type="button" data-action="inspect" data-idx="${idx}" class="ui-btn !px-2 !gap-1.5" title="Open the committee debate">
              <i class="fa-solid fa-comments"></i> Debate
            </button>
          </div>
        </article>
      `;
    }).join('');

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
            <div class="text-[17px] font-bold text-white">${esc(c.ticker)}</div>
            <div class="ui-faint text-[13px] truncate">${esc(c.name)}</div>
          </div>
          ${adviceScoreBlock(c.pre_score, 'Scanner')}
        </div>

        <div>
          ${c.setup_type ? `<div class="text-[14px] font-semibold text-white">${esc(c.setup_type)}</div>` : ''}
          ${c.notes ? `<p class="adv-prose adv-clamp mt-0.5">${esc(c.notes)}</p>` : ''}
        </div>

        <div class="ui-inset px-3.5 py-2.5 space-y-1.5">
          <div class="kv"><span>Price</span><span>${advicePrice(c.price, c.currency)}</span></div>
          <div class="kv"><span>RSI (14)</span><span${hot ? ' style="color: var(--warn)"' : ''}>${esc(c.rsi)}${hot ? ' · overbought' : ''}</span></div>
          <div class="kv"><span>5 days</span>${retHtml}</div>
        </div>

        <button type="button" data-action="analyze" data-ticker="${esc(c.ticker)}" class="ui-btn ui-btn-primary w-full mt-auto">
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

async function analyzeDirect(ticker) {
  switchTab('debate');
  document.getElementById('analyzeInput').value = ticker;
  const btn = document.getElementById('analyzeBtn');
  const outputArea = document.getElementById('debateOutputArea');

  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Convening...`;
  btn.disabled = true;

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });

    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || 'Analysis failed', true);
      return;
    }

    const report = await res.json();
    renderDebateReport(report);
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
          <span class="text-[22px] font-bold text-white leading-tight">${esc(report.ticker)}</span>
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
          <td data-label="Asset" class="py-3 px-4 font-bold" style="color:var(--text)">${esc(t.ticker)}</td>
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
    if (dot) dot.className = 'ui-dot bg-amber-400 animate-pulse';
    if (text) text.textContent = 'Broker: Probing...';
    if (mobDot) mobDot.className = 'ui-dot bg-amber-400 animate-pulse';
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
        mobDot.className = 'ui-dot bg-emerald-400';
        mobText.textContent = brokerName === 'PAPER' ? 'Simulated broker' : `${brokerName} online`;
      } else {
        mobDot.className = 'ui-dot bg-rose-400';
        mobText.textContent = `${brokerName} offline`;
      }
    }

    if (badge && dot && text) {
      if (isConnected) {
        dot.className = 'ui-dot bg-emerald-400';
        text.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
        badge.className = 'hidden';
        badge.title = `${brokerName} Connected (${data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms + 'ms' : 'Ready'})\nClick to test connection`;
      } else {
        dot.className = 'ui-dot bg-rose-400';
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
      diagName.textContent = brokerName === 'IBKR' ? 'IBKR (Interactive Brokers)' : (brokerName === 'ALPACA' ? 'Alpaca Markets' : 'Paper Trading Engine');
    }
    if (diagDot && diagText) {
      if (isConnected) {
        diagDot.className = 'ui-dot bg-emerald-400';
        diagText.textContent = 'Connected (Online)';
        diagText.className = 'text-emerald-400 font-bold';
      } else {
        diagDot.className = 'ui-dot bg-rose-400';
        diagText.textContent = 'Disconnected (Offline)';
        diagText.className = 'text-rose-400 font-bold';
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
      diagMsg.className = isConnected ? 'text-emerald-300 font-mono text-[11px] leading-relaxed break-words' : 'text-rose-300 font-mono text-[11px] leading-relaxed break-words';
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
    if (mobErrDot) mobErrDot.className = 'ui-dot bg-rose-400';
    if (mobErrText) mobErrText.textContent = 'Broker error';
    if (badge && dot && text) {
      dot.className = 'ui-dot bg-rose-400';
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
    if (mobMarketDot) mobMarketDot.className = (euOpen || usOpen) ? 'ui-dot bg-emerald-400' : 'ui-dot';
    if (msData.is_weekend) {
      textEl.textContent = 'Weekend (Markets Closed)';
      badge.className = 'hidden xl:flex ui-chip text-amber-300';
      if (mobMarketText) mobMarketText.textContent = 'Weekend · markets closed';
    } else if (msData.eu_market.is_open || msData.us_market.is_open) {
      textEl.textContent = `🇪🇺 EU: ${msData.eu_market.status} | 🇺🇸 US: ${msData.us_market.status}`;
      badge.className = 'hidden xl:flex ui-chip text-emerald-300';
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
    document.getElementById('consensusScoreLabel').textContent = `${data.min_consensus_score} / 100`;

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

    if (document.getElementById('settingIbkrHost') && data.ibkr_host) {
      document.getElementById('settingIbkrHost').value = data.ibkr_host;
    }
    if (document.getElementById('settingIbkrPort') && data.ibkr_port) {
      document.getElementById('settingIbkrPort').value = data.ibkr_port;
    }
    if (document.getElementById('settingIbkrClientId') && data.ibkr_client_id !== undefined) {
      document.getElementById('settingIbkrClientId').value = data.ibkr_client_id;
    }
    if (document.getElementById('settingIbkrAccountId')) {
      document.getElementById('settingIbkrAccountId').value = data.ibkr_account_id || '';
    }

    await loadMarketStatus();
    await loadBrokerStatus();
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
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
  const minScore = parseInt(document.getElementById('settingMinScore').value);
  const posSizePct = parseFloat(document.getElementById('settingPosSize').value) / 100.0;
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
      min_consensus_score: minScore,
      position_size_pct: posSizePct,
      enforce_market_hours: enforceHours
    };
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

    const ibkrHostEl = document.getElementById('settingIbkrHost');
    const ibkrPortEl = document.getElementById('settingIbkrPort');
    const ibkrClientIdEl = document.getElementById('settingIbkrClientId');
    const ibkrAccountIdEl = document.getElementById('settingIbkrAccountId');

    if (ibkrHostEl && ibkrHostEl.value.trim()) payload.ibkr_host = ibkrHostEl.value.trim();
    if (ibkrPortEl && !isNaN(parseInt(ibkrPortEl.value))) payload.ibkr_port = parseInt(ibkrPortEl.value);
    if (ibkrClientIdEl && !isNaN(parseInt(ibkrClientIdEl.value))) payload.ibkr_client_id = parseInt(ibkrClientIdEl.value);
    if (ibkrAccountIdEl) payload.ibkr_account_id = ibkrAccountIdEl.value.trim();

    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast('Settings saved successfully.');
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
  if (badge) badge.className = on ? 'hidden xl:flex ui-chip text-emerald-300' : 'hidden xl:flex ui-chip';
  const dot = document.getElementById('autoBuyDot');
  if (dot) dot.className = on ? 'ui-dot bg-emerald-400' : 'ui-dot';
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

async function saveBrokerConfig() {
  const host = document.getElementById('settingIbkrHost')?.value.trim();
  const port = parseInt(document.getElementById('settingIbkrPort')?.value);
  const clientId = parseInt(document.getElementById('settingIbkrClientId')?.value);
  const accountId = document.getElementById('settingIbkrAccountId')?.value.trim();

  const payload = {};
  if (host) payload.ibkr_host = host;
  if (!isNaN(port) && port > 0) payload.ibkr_port = port;
  if (!isNaN(clientId)) payload.ibkr_client_id = clientId;
  if (accountId !== undefined) payload.ibkr_account_id = accountId;

  const btn = document.getElementById('saveBrokerConfigBtn');
  if (btn) btn.disabled = true;

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast('IBKR Broker settings saved successfully.');
      await loadBrokerStatus(true);
    } else {
      const err = await res.json();
      showToast(err.detail || 'Failed to save broker configuration.', true);
    }
  } catch (e) {
    showToast('Network error saving broker configuration.', true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function reconnectBroker() {
  const btn = document.getElementById('settingsReconnectBtn');
  const icon = document.getElementById('settingsReconnectIcon');
  if (btn) btn.disabled = true;
  if (icon) icon.className = 'fa-solid fa-spinner fa-spin';

  showToast('Connecting to IBKR Gateway...');

  try {
    const res = await fetch('/api/broker/reconnect', { method: 'POST' });
    const data = await res.json();
    if (data.connected) {
      showToast(`IBKR connected successfully (${data.latency_ms || 0}ms)!`);
    } else {
      showToast(`IBKR connection failed: ${data.error || data.message || 'Offline'}`, true);
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
          const subParts = [triggerTypeLabel(t.trigger_type)];
          if (hasNum(t.consensus_score)) subParts.push(`score ${Number(t.consensus_score)}`);

          return `
            <div class="ui-card ui-card-pad flex flex-col gap-3">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="text-base font-bold truncate" style="color:var(--text)">${esc(t.ticker)}</div>
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
                    ? `<div class="ui-num font-semibold" style="color:var(--text)">${esc(eur(allocEur))}</div>`
                    : `<span class="ui-badge ui-badge-warn" title="Higher-scored triggers hold the available cash or position slots. This trigger is funded automatically when cash or a slot frees up."><i class="fa-solid fa-hourglass-half"></i> Unfunded</span>`}
                  <div class="ui-faint text-xs truncate">Armed <span class="ui-num">${esc(t.created_at)}</span></div>
                </div>
                <button type="button" data-action="cancel-trigger" data-id="${esc(t.id)}" data-ticker="${esc(t.ticker)}" class="ui-btn ui-btn-sm ui-btn-danger shrink-0" style="min-height:44px" aria-label="Cancel trigger for ${esc(t.ticker)}">
                  Cancel
                </button>
              </div>
            </div>
          `;
        }).join('');
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
              <td data-label="Ticker" class="py-3 px-4 font-bold" style="color:var(--text)">${esc(t.ticker)}</td>
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

async function armTriggerFromInput() {
  const input = document.getElementById('triggerTickerInput');
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) {
    showToast('Please enter a stock ticker.', true);
    return;
  }

  if (!confirmArmTrigger(ticker)) return;

  const btn = document.getElementById('armTriggerBtn');
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Arming`;
  btn.disabled = true;

  try {
    const res = await fetch('/api/triggers/from-ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Trigger armed for ${ticker}.`);
      input.value = '';
      loadTriggers();
    } else {
      showToast(data.detail || data.message || 'Failed to arm trigger.', true);
    }
  } catch (e) {
    showToast('Error arming trigger.', true);
  } finally {
    btn.innerHTML = ARM_BTN_HTML;
    btn.disabled = false;
  }
}

// Arming reserves cash and lets the bot buy on its own later, so ask first
function confirmArmTrigger(ticker) {
  return confirm(`Arm an entry trigger for ${ticker}?\n\nThe committee picks a limit price and cash is reserved for it. The bot buys automatically when the price is reached.`);
}

async function armAITrigger(ticker) {
  if (!confirmArmTrigger(ticker)) return;
  showToast(`Analysing ${ticker} and arming an entry trigger...`);
  try {
    const res = await fetch('/api/triggers/from-ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`Trigger armed for ${ticker} at ${data.target_price}.`);
      loadTriggers();
    } else {
      showToast(data.detail || data.message || 'Failed to arm trigger.', true);
    }
  } catch (e) {
    showToast('Error arming trigger.', true);
  }
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
                    <span class="text-base font-bold" style="color:var(--text)">${esc(l.ticker)}</span>
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
      nextBadgeEl.innerHTML = `<i class="fa-regular fa-clock"></i> Next: <span class="ui-num text-white">${esc(data.next_session.formatted)}</span>`;
    }

    if (!data.found) {
      const marketLabel = currentMarketFilter ? `${currentMarketFilter} ` : '';
      if (headlineEl) headlineEl.innerText = `No ${marketLabel}review yet today`;
      if (contentEl) contentEl.innerHTML = `Tap <strong class="text-white">Run ${esc(currentMarketFilter || 'US')} review</strong> below to score today's candidates and generate advice.`;
      if (timestampEl) timestampEl.innerText = 'Pending';
      if (textContainer) textContainer.classList.add('hidden');
      return;
    }

    if (timestampEl) timestampEl.innerText = `${data.market || ''} ${data.session || ''} · ${data.timestamp || 'Today'}`.trim();
    if (headlineEl) headlineEl.innerText = data.headline || 'Market Session Review';

    let picksHtml = '';
    if (data.top_picks && data.top_picks.length > 0) {
      picksHtml = '<div class="mt-3 flex flex-wrap gap-2">' + data.top_picks.slice(0, 4).map(p =>
        `<span class="ui-chip"><span class="ui-num text-white font-semibold">${esc(p.ticker)}</span> ${esc(String(p.action || '').replace('_', ' '))} <span class="ui-num">${esc(p.consensus_score)}</span></span>`
      ).join('') + '</div>';
    }

    let holdingsHtml = '';
    const positions = (data.portfolio_state && data.portfolio_state.positions) || [];
    if (positions.length > 0) {
      holdingsHtml = '<div class="mt-3"><div class="ui-eyebrow mb-1.5">Holdings</div><div class="ui-inset px-3.5 py-2.5 space-y-1.5">' +
        positions.map(pos => {
          const up = pos.unrealized_pnl_eur >= 0;
          const sign = up ? '+' : '';
          return `<div class="kv"><span><span class="ui-num text-white">${esc(pos.ticker)}</span> <span class="ui-num">${esc(pos.shares)} @ ${esc(pos.entry_price_native)} ${esc(pos.currency)}</span></span>
            <span class="${up ? 'ui-gain' : 'ui-loss'}">${up ? '▲' : '▼'} ${sign}€${num(pos.unrealized_pnl_eur)} (${sign}${num(pos.unrealized_pnl_pct, 1)}%)</span></div>`;
        }).join('') + '</div></div>';
    }

    if (contentEl) {
      contentEl.innerHTML = `<span>${safeRichText(data.content || '')}</span>${picksHtml}${holdingsHtml}`;
    }

    if (textContainer && data.review_text) {
      textContainer.innerHTML = safeRichText(data.review_text);
      textContainer.classList.remove('hidden');
    } else if (textContainer) {
      textContainer.classList.add('hidden');
    }
  } catch (e) {
    console.debug('Error loading market review:', e);
  }
}

async function triggerMarketReview(market) {
  showToast(`Starting the ${market} review in the background...`);
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
      showToast(`${market} review is analyzing candidates. Results will appear shortly.`);
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
            showToast(`${market} review is ready.`);
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
      if (winCountsEl) winCountsEl.innerText = '0 picks evaluated';
      if (deltaEl) { deltaEl.innerText = '€0.00 (+0.0%)'; deltaEl.className = `${STAT} text-white`; }
      if (bestPickEl) bestPickEl.innerText = '--';
      if (picksContainer) {
        picksContainer.innerHTML = `<div class="p-3.5 adv-prose">No picks recorded in the past 7 days yet. Run a market review to start tracking.</div>`;
      }
      renderWeeklyArchive();
      return;
    }

    if (winRateEl) {
      winRateEl.innerText = `${comp.win_rate_pct}%`;
      winRateEl.className = `${STAT} ${comp.win_rate_pct >= 50 ? 'ui-gain' : 'ui-loss'}`;
    }
    if (winCountsEl) {
      winCountsEl.innerText = `${comp.positive_picks_count}/${comp.total_picks_tracked} picks in profit`;
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
        if (badgeText.includes('TARGET')) badgeClass = 'ui-badge ui-badge-gain';
        else if (badgeText.includes('STOP')) badgeClass = 'ui-badge ui-badge-loss';

        return `
          <div class="p-3.5 space-y-2">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[15px] font-bold text-white">${esc(p.ticker)}</span>
                  ${badgeText ? `<span class="${badgeClass}">${esc(badgeText)}</span>` : ''}
                </div>
                <div class="ui-faint text-[13px] truncate">${esc(p.name)}</div>
              </div>
              <div class="ui-num text-[15px] font-semibold shrink-0 ${isPos ? 'ui-gain' : 'ui-loss'}">${isPos ? '▲' : '▼'} ${sign}${esc(p.pnl_pct)}%</div>
            </div>
            <div class="space-y-1">
              <div class="kv"><span>Advised</span><span>${esc(p.rec_session)}</span></div>
              <div class="kv"><span>At advice</span><span>${advicePrice(p.rec_price, p.currency)}</span></div>
              <div class="kv"><span>Now</span><span>${advicePrice(p.current_price, p.currency)}</span></div>
              ${p.target_price ? `<div class="kv"><span>Target</span><span class="ui-gain">▲ ${esc(p.target_price)}</span></div>` : ''}
              ${p.stop_loss ? `<div class="kv"><span>Stop loss</span><span class="ui-loss">▼ ${esc(p.stop_loss)}</span></div>` : ''}
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

function toggleWeeklyArchive() {
  const container = document.getElementById('weeklyArchiveContainer');
  const icon = document.getElementById('weeklyArchiveIcon');
  if (!container) return;

  const isHidden = container.classList.contains('hidden');
  if (isHidden) {
    container.classList.remove('hidden');
    if (icon) icon.className = 'fa-solid fa-chevron-down text-[12px]';
    renderWeeklyArchive();
  } else {
    container.classList.add('hidden');
    if (icon) icon.className = 'fa-solid fa-chevron-right text-[12px]';
  }
}

function renderWeeklyArchive() {
  const container = document.getElementById('weeklyArchiveContainer');
  if (!container || !weeklyReviewsCache) return;

  if (!Array.isArray(weeklyReviewsCache) || weeklyReviewsCache.length === 0) {
    container.innerHTML = `<div class="adv-prose ui-faint px-1">No past reviews archived yet.</div>`;
    return;
  }

  container.innerHTML = weeklyReviewsCache.map(r => {
    const picks = (r.top_picks || []).map(p => p.ticker).join(', ') || 'None'; // escaped below
    return `
      <div class="ui-inset p-3.5 space-y-1.5">
        <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <span class="text-[14px] font-semibold text-white">${esc(r.market)} ${esc(r.session)} · ${esc(r.headline)}</span>
          <span class="ui-num ui-faint text-[12px]">${esc(r.timestamp)}</span>
        </div>
        <div class="adv-prose adv-clamp">${safeRichText(r.content)}</div>
        <div class="ui-faint text-[13px]">Picks: <span class="ui-num text-white">${esc(picks)}</span></div>
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
        raw_text: sample || "Investigated BESI.AS pullback entry at €175.88. Enforced Euronext minimum €100 order size to limit IBKR tiered commission drag to 3.8%.",
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
        <span class="font-semibold" style="color:var(--text)">${esc(p.ticker)}</span>
        ${p.followed_main ? '' : '<span class="ui-badge ui-badge-accent ml-1">Shadow only</span>'}
        <div class="ui-faint ui-num text-xs">${esc(num(p.entry_price_native))} &rarr; ${esc(num(p.current_price_native))} ${esc(p.currency)} · stop ${p.stop_loss_native ? esc(num(p.stop_loss_native)) : '--'}</div>
      </div>
      <div class="text-right shrink-0 ui-num ${gainCls(p.unrealized_pnl_eur)}">${esc(signedEur(p.unrealized_pnl_eur))}<div class="text-xs">${esc(signedPct(p.unrealized_pnl_pct, 1))}</div></div>
    </div>`).join('');
  const pending = (s.pending_triggers || []).map(t => `
    <div class="flex items-center justify-between gap-3 py-2 text-[13px]">
      <div class="min-w-0">
        <span class="font-semibold" style="color:var(--text)">${esc(t.ticker)}</span>
        ${t.followed_main ? '' : '<span class="ui-badge ui-badge-accent ml-1">Shadow only</span>'}
        <div class="ui-faint text-xs">${esc(t.trigger_type === 'MARKET' ? 'Buy at market (next open)' : triggerTypeLabel(t.trigger_type))} · score ${esc(t.consensus_score)}</div>
      </div>
      <span class="ui-num shrink-0">${esc(num(t.target_price))} ${esc(t.currency)}</span>
    </div>`).join('');
  const closed = (s.recent_closed || []).map(c => `
    <div class="flex items-center justify-between gap-3 py-2 text-[13px]">
      <div class="min-w-0">
        <span class="font-semibold" style="color:var(--text)">${esc(c.ticker)}</span>
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
          <h3 class="ui-section-title">${esc(s.label)}</h3>
          <p class="text-[13px] ui-muted mt-1">${esc(s.description)}</p>
          <p class="text-xs ui-faint mt-1 ui-num">Since ${esc(s.started_at || '--')} · start ${esc(eur(s.initial_capital_eur))}</p>
          ${shadowFrozenNote(s.params || {})}
        </div>
        <button type="button" class="ui-btn ui-btn-sm shrink-0" style="min-height:44px" data-action="reset-shadow" data-key="${esc(s.key)}">
          <i class="fa-solid fa-rotate-left"></i> Reset
        </button>
      </div>
      <div class="flex flex-wrap gap-1.5">${shadowParamChips(s.params || {})}</div>

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

      <div class="relative h-44 ${(s.history && s.history.shadow && s.history.shadow.length > 1) ? '' : 'hidden'}"><canvas id="shadowChart-${esc(s.key)}"></canvas></div>

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
        { label: s.label, data: hist.shadow.map(p => p[1]), borderColor: '#8EA2FF', backgroundColor: '#8EA2FF', borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: mainLabel, data: labels.map(l => mainMap.has(l) ? mainMap.get(l) : null), borderColor: '#A3ADBD', backgroundColor: '#A3ADBD', borderWidth: 2, borderDash: [4, 4], pointRadius: 0, tension: 0.2, spanGaps: true }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { color: '#A3ADBD', boxWidth: 12 } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${signedPct(c.parsed.y)}` } }
      },
      scales: {
        x: { ticks: { color: '#7D8799', maxTicksLimit: 4, maxRotation: 0, callback(v) { return String(this.getLabelForValue(v)).slice(0, 5); } }, grid: { display: false } },
        y: { ticks: { color: '#7D8799', callback: v => `${v}%` }, grid: { color: '#1E2533' } }
      }
    }
  });
}

async function loadShadow() {
  const container = document.getElementById('shadowContainer');
  if (!container) return;
  try {
    const res = await fetch('/api/shadow');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Request failed');
    const mainLabel = (data.main && data.main.label) || 'Main';
    if (!data.enabled || !(data.strategies || []).length) {
      const presets = (data.presets || []).map(p => `<li><span class="ui-num" style="color:var(--text)">${esc(p.key)}</span>: ${esc(p.description)}</li>`).join('');
      container.innerHTML = `<div class="ui-card ui-card-pad text-[13px] ui-muted space-y-2">
        <p>No shadow strategy is enabled. Set <span class="ui-num">SHADOW_ENABLED=true</span> and <span class="ui-num">SHADOW_STRATEGIES</span> in .env, then restart.</p>
        <ul class="list-disc pl-5 space-y-1">${presets}</ul>
      </div>`;
      return;
    }
    container.innerHTML = data.strategies.map(s => renderShadowStrategy(s, mainLabel)).join('');
    data.strategies.forEach(s => drawShadowChart(s, mainLabel));
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
