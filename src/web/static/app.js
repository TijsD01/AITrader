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

// Small badge showing where a committee score came from ("llm" | "rules")
function scoreSourceBadge(source) {
  if (!source) return '';
  const key = String(source).toLowerCase();
  const label = key === 'llm' ? 'AI-oordeel' : (key === 'rules' ? 'Rekenregel' : null);
  if (!label) return '';
  const cls = key === 'llm'
    ? 'bg-purple-500/15 text-purple-300 border-purple-500/30'
    : 'bg-slate-700/60 text-slate-300 border-slate-600';
  return `<span class="inline-block text-[10px] font-semibold px-1.5 py-0.5 rounded border ${cls}" title="Score source: ${esc(key)}">${esc(label)}</span>`;
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
    case 'buy': executePaperBuy(ticker); break;
    case 'arm-trigger': armAITrigger(ticker); break;
    case 'analyze': analyzeDirect(ticker); break;
    case 'inspect': inspectReport(Number(el.dataset.idx)); break;
    case 'cancel-trigger': cancelTrigger(Number(el.dataset.id), ticker); break;
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
    icon.className = 'fa-solid fa-circle-exclamation text-rose-400';
  } else {
    icon.className = 'fa-solid fa-circle-check text-emerald-400';
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
  portfolio: { label: 'Portfolio', icon: 'fa-solid fa-wallet' },
  advice: { label: 'Daily Advice', icon: 'fa-solid fa-lightbulb' },
  screener: { label: 'Market Screener', icon: 'fa-solid fa-magnifying-glass-chart' },
  debate: { label: 'Committee Debate', icon: 'fa-solid fa-comments' },
  triggers: { label: 'AI Triggers', icon: 'fa-solid fa-crosshairs' },
  history: { label: 'Trade History', icon: 'fa-solid fa-clock-rotate-left' },
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
const MORE_TABS = ['screener', 'debate', 'memory', 'settings'];

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
  const SHORT = { screener: 'Screener', debate: 'Debate', memory: 'Memory', settings: 'Settings' };
  if (mobileLabel) mobileLabel.textContent = inMore ? (SHORT[tabId] || cfg.label) : 'More';
  if (mobileIcon) mobileIcon.className = inMore ? cfg.icon : 'fa-solid fa-ellipsis';

  // Update mobile dropdown items active state & checkmark
  document.querySelectorAll('.mobile-dropdown-item').forEach(item => {
    const isCurrent = item.dataset.tab === tabId;
    const check = item.querySelector('.mobile-active-check');
    if (isCurrent) {
      item.classList.add('bg-emerald-500/15', 'text-emerald-400', 'border', 'border-emerald-500/30');
      item.classList.remove('text-slate-300', 'hover:bg-slate-800/70');
      if (check) check.classList.remove('hidden');
    } else {
      item.classList.remove('bg-emerald-500/15', 'text-emerald-400', 'border', 'border-emerald-500/30');
      item.classList.add('text-slate-300', 'hover:bg-slate-800/70');
      if (check) check.classList.add('hidden');
    }
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
        backgroundColor: ['#059669', '#3b82f6'],
        borderWidth: 2,
        borderColor: '#131b2e'
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: '#94a3b8', font: { size: 12 }, boxWidth: 14 }
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
      modeBadge.className = isLive
        ? 'text-xs px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-400 font-semibold uppercase tracking-wider shrink-0 border border-rose-500/30'
        : 'text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 font-semibold uppercase tracking-wider shrink-0 border border-emerald-500/30';
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

    // Pending triggers summary (tap to open the Triggers tab)
    const trigCount = Number(summary.pending_triggers_count) || 0;
    const trigReserved = Number(summary.reserved_triggers_eur) || 0;
    const trigText = document.getElementById('pendingTriggersSummaryText');
    const trigSub = document.getElementById('pendingTriggersSummarySub');
    if (trigText) trigText.textContent = `${trigCount} pending trigger${trigCount === 1 ? '' : 's'}`;
    if (trigSub) trigSub.textContent = trigCount > 0 && trigReserved > 0
      ? `${eur(trigReserved)} reserved for limit orders`
      : 'Automated limit orders waiting for their price';
    setTriggerNavBadge(trigCount);

    const limitBadge = document.getElementById('capitalLimitBadge');
    if (limitBadge) {
      if (maxUsagePct < 100 || (maxCapEur && maxCapEur > 0)) {
        limitBadge.textContent = `${maxUsagePct}% limit${maxCapEur ? ` (Cap €${maxCapEur})` : ''}`;
        limitBadge.className = 'text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 font-mono border border-amber-500/20';
      } else {
        limitBadge.textContent = '100%';
        limitBadge.className = 'text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-mono border border-emerald-500/20';
      }
    }

    const cashSub = document.getElementById('metricCashSub');
    if (cashSub) {
      const reservedTriggers = summary.reserved_triggers_eur || 0;
      const triggersCount = summary.pending_triggers_count || 0;
      const subParts = [];

      if (reservedTriggers > 0) {
        subParts.push(`<span class="text-sky-400 font-medium" title="Capital earmarked for pending order triggers"><i class="fa-solid fa-clock"></i> ${eur(reservedTriggers)} reserved (${triggersCount} trigger${triggersCount > 1 ? 's' : ''})</span>`);
      }
      if (protectedReserve > 0) {
        subParts.push(`<span class="text-amber-400 font-medium"><i class="fa-solid fa-shield-halved"></i> ${eur(protectedReserve)} reserved</span>`);
      }

      if (subParts.length > 0) {
        cashSub.innerHTML = `${subParts.join(' · ')} · Total ${eur(currentAccountCash)}`;
      } else {
        cashSub.textContent = `Full buffer · Total account cash ${eur(currentAccountCash)}`;
      }
    }

    recalcCapitalPreview();
    
    const pnlElem = document.getElementById('metricUnrealizedPnl');
    const returnElem = document.getElementById('metricReturnBadge');

    const sign = pnlVal >= 0 ? '+' : '';
    pnlElem.textContent = `${sign}€${pnlVal.toFixed(2)}`;
    pnlElem.className = `text-xl sm:text-2xl font-bold mt-1 truncate ${pnlVal >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;

    const retSign = totalReturnPct >= 0 ? '+' : '';
    returnElem.innerHTML = `<i class="fa-solid fa-arrow-trend-${totalReturnPct >= 0 ? 'up' : 'down'}"></i> ${retSign}${totalReturnPct.toFixed(2)}% total return`;
    returnElem.className = `text-[11px] sm:text-xs font-semibold mt-1.5 sm:mt-2 flex items-center gap-1 ${totalReturnPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
    
    document.getElementById('metricPosCount').textContent = `${positions.length} open holdings`;

    // Update Chart
    if (allocationChart) {
      if (positions.length > 0) {
        const labels = ['Cash', ...positions.map(p => p.ticker)];
        const dataVals = [Number(summary.cash_eur) || 0, ...positions.map(p => Number(p.current_value_eur) || 0)];
        const colors = ['#059669', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4', '#f97316', '#6366f1'];
        allocationChart.data.labels = labels;
        allocationChart.data.datasets[0].data = dataVals;
        allocationChart.data.datasets[0].backgroundColor = colors.slice(0, labels.length);
      } else {
        allocationChart.data.labels = ['Cash', 'Invested'];
        allocationChart.data.datasets[0].data = [Number(summary.cash_eur) || 0, 0];
        allocationChart.data.datasets[0].backgroundColor = ['#059669', '#3b82f6'];
      }
      allocationChart.update();
    }

    // Render Position Cards
    const container = document.getElementById('positionsContainer');
    if (positions.length === 0) {
      container.innerHTML = `
        <div class="text-center py-10 text-slate-400 text-xs sm:text-sm">
          <i class="fa-solid fa-hourglass-start text-2xl mb-2 text-slate-500 block"></i>
          No active holdings yet. The automated agent committee will open positions during market hours or on-demand!
        </div>`;
      return;
    }

    container.innerHTML = positions.map(pos => {
      const pnl = Number(pos.unrealized_pnl_eur) || 0;
      const pnlColor = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
      const pnlSign = pnl >= 0 ? '+' : '';
      return `
        <div class="p-3.5 sm:p-4 rounded-xl bg-slate-900/70 border border-borderCol hover:border-slate-700 transition-colors space-y-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="font-bold text-white text-base">${esc(pos.ticker)}</span>
                <span class="text-[11px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">${esc(pos.shares)} shs</span>
              </div>
              ${pos.name ? `<div class="text-xs text-slate-400 truncate">${esc(pos.name)}</div>` : ''}
            </div>
            <div class="text-right shrink-0">
              <div class="text-base font-bold ${pnlColor}">${pnlSign}€${num(pnl)}</div>
              <div class="text-xs font-semibold ${pnlColor}">${pnlSign}${esc(pos.unrealized_pnl_pct)}%</div>
            </div>
          </div>

          <div class="text-xs text-slate-400 grid grid-cols-2 sm:flex sm:flex-wrap gap-x-4 gap-y-1">
            <span>Entry: <strong class="text-slate-200">${esc(pos.entry_price_native)} ${esc(pos.currency)}</strong></span>
            <span>Now: <strong class="text-white">${esc(pos.current_price_native)} ${esc(pos.currency)}</strong></span>
            <span class="col-span-2 sm:col-span-1">Value: <strong class="text-emerald-400">€${esc(pos.current_value_eur)}</strong></span>
          </div>

          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="text-xs flex flex-wrap gap-2">
              ${pos.stop_loss_native ? `<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 text-[11px] font-medium"><i class="fa-solid fa-shield"></i> Stop: ${esc(pos.stop_loss_native)}</span>` : ''}
              ${pos.take_profit_native ? `<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px] font-medium"><i class="fa-solid fa-bullseye"></i> Target: ${esc(pos.take_profit_native)}</span>` : ''}
            </div>
            <button type="button" data-action="close-position" data-ticker="${esc(pos.ticker)}" class="px-4 min-h-[44px] rounded-lg bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30 text-xs font-semibold flex items-center justify-center gap-1.5 active:scale-95 transition-all" aria-label="Close position ${esc(pos.ticker)}">
              <i class="fa-solid fa-xmark"></i> Close
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

    if (adviceReportsCache.length === 0) {
      container.innerHTML = `
        <div class="col-span-full text-center py-12 text-slate-400 text-sm bg-cardBg border border-borderCol rounded-xl p-6">
          <i class="fa-solid fa-compass text-3xl mb-3 text-slate-500 block"></i>
          No daily reports generated yet today. Click <strong>"Run Scan"</strong> in the top bar to run your first candidate discovery & agent debate!
        </div>`;
      return;
    }

    container.innerHTML = adviceReportsCache.map((r, idx) => {
      const isBuy = r.action === 'STRONG_BUY' || r.action === 'BUY';
      const actionText = String(r.action || '').replace('_', ' ');
      const actionBadge = isBuy
        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30'
        : (r.action === 'SELL' ? 'bg-rose-500/20 text-rose-400 border-rose-500/30' : 'bg-slate-700 text-slate-300 border-slate-600');

      return `
        <div class="stock-card bg-cardBg border border-borderCol rounded-xl p-4 sm:p-5 shadow-sm space-y-4 flex flex-col justify-between">
          <div class="space-y-2.5">
            <div class="flex items-center justify-between gap-2">
              <div class="flex flex-wrap items-center gap-2 min-w-0">
                <span class="text-base font-bold text-white">${esc(r.ticker)}</span>
                <span class="text-[11px] font-bold px-2.5 py-0.5 rounded-full border ${actionBadge}">${esc(actionText)}</span>
              </div>
              <div class="text-right shrink-0">
                <div class="text-sm font-bold text-emerald-400">${esc(r.consensus_score)} <span class="text-slate-400 text-[10px]">/ 100</span></div>
                <div class="text-[10px] text-slate-500">Conviction</div>
                ${scoreSourceBadge(r.score_source || (r.raw_signals && r.raw_signals.score_source))}
              </div>
            </div>

            <p class="text-xs text-slate-300 leading-relaxed">${esc(r.summary)}</p>

            <div class="grid grid-cols-2 gap-2 text-[11px] bg-slate-900/60 p-2.5 rounded-lg border border-borderCol">
              <div>
                <span class="text-slate-500 block">Target Profit</span>
                <span class="text-emerald-400 font-semibold">${r.target_price ? esc(r.target_price) : 'N/A'}</span>
              </div>
              <div>
                <span class="text-slate-500 block">Stop Loss</span>
                <span class="text-rose-400 font-semibold">${r.stop_loss ? esc(r.stop_loss) : 'N/A'}</span>
              </div>
            </div>
          </div>

          <div class="grid grid-cols-3 gap-2 pt-2 border-t border-borderCol/60">
            <button type="button" data-action="inspect" data-idx="${idx}" class="min-h-[44px] px-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium flex items-center justify-center gap-1 transition-all" title="View multi-agent debate">
              <i class="fa-solid fa-comments text-cyan-400"></i> Debate
            </button>
            <button type="button" data-action="arm-trigger" data-ticker="${esc(r.ticker)}" class="min-h-[44px] px-2 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 text-xs font-bold flex items-center justify-center gap-1 transition-all" title="Arm AI-calculated limit price trigger">
              <i class="fa-solid fa-crosshairs text-indigo-400"></i> Trigger
            </button>
            <button type="button" data-action="buy" data-ticker="${esc(r.ticker)}" class="min-h-[44px] px-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold flex items-center justify-center gap-1 shadow-sm active:scale-95 transition-all" title="Execute immediate market buy">
              <i class="fa-solid fa-cart-shopping"></i> Buy
            </button>
          </div>
        </div>
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
    <div class="col-span-full text-center py-10 text-slate-400 text-xs sm:text-sm">
      <i class="fa-solid fa-spinner fa-spin text-2xl mb-2 text-emerald-400 block"></i>
      Scanning liquid universe for high momentum setups...
    </div>`;

  try {
    const res = await fetch(`/api/screener?market=${currentScreenerMarket}`);
    const candidates = await res.json();

    if (candidates.length === 0) {
      container.innerHTML = `<div class="col-span-full text-center py-8 text-slate-400 text-sm">No qualifying breakout candidates found in this session.</div>`;
      return;
    }

    container.innerHTML = candidates.map(c => `
      <div class="stock-card bg-cardBg border border-borderCol rounded-xl p-4 shadow-sm space-y-3 flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between">
            <div>
              <span class="font-bold text-white text-sm sm:text-base">${esc(c.ticker)}</span>
              <span class="text-xs text-slate-400 ml-1.5">${esc(c.name)}</span>
            </div>
            <span class="text-xs font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20 whitespace-nowrap" title="Pre-filter score from the technical scanner (not the committee score)">
              Scanner-score ${esc(c.pre_score)}
            </span>
          </div>

          <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-slate-400 mt-2">
            <span>Price: <strong class="text-white">${esc(c.price)} ${esc(c.currency)}</strong></span>
            <span>RSI(14): <strong class="${c.rsi > 70 ? 'text-amber-400' : 'text-slate-200'}">${esc(c.rsi)}</strong></span>
            <span>5d: <strong class="${c.return_5d_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${c.return_5d_pct >= 0 ? '+' : ''}${esc(c.return_5d_pct)}%</strong></span>
          </div>

          <div class="text-[11px] text-slate-300 mt-2 bg-slate-900/70 p-2 rounded border border-borderCol/60">
            <div class="font-semibold text-cyan-400 mb-0.5">${esc(c.setup_type)}</div>
            <div class="text-slate-400">${esc(c.notes)}</div>
          </div>
        </div>

        <button type="button" data-action="analyze" data-ticker="${esc(c.ticker)}" class="w-full min-h-[44px] px-3 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border border-emerald-500/30 text-xs font-bold flex items-center justify-center gap-1.5 transition-all">
          <i class="fa-solid fa-users-viewfinder"></i> Analyze with Committee
        </button>
      </div>
    `).join('');

  } catch (e) {
    console.error('Failed to load screener:', e);
    container.innerHTML = `<div class="col-span-full text-center py-8 text-rose-400 text-sm">Failed to load screener.</div>`;
  }
}

function filterScreener(market) {
  currentScreenerMarket = market;
  document.querySelectorAll('.screener-filter-btn').forEach(b => {
    if (b.dataset.market === market) {
      b.className = 'screener-filter-btn px-3 py-2 rounded-md bg-emerald-600 text-white';
    } else {
      b.className = 'screener-filter-btn px-3 py-2 rounded-md text-slate-400 hover:text-white';
    }
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

  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Convening Committee...`;
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
    btn.innerHTML = `<i class="fa-solid fa-users-viewfinder"></i> Convene Debate`;
    btn.disabled = false;
  }
}

function renderDebateReport(report) {
  const isBuy = report.action === 'STRONG_BUY' || report.action === 'BUY';
  const actionColor = isBuy ? 'text-emerald-400 bg-emerald-500/20 border-emerald-500/30' : (report.action === 'SELL' ? 'text-rose-400 bg-rose-500/20 border-rose-500/30' : 'text-amber-400 bg-amber-500/20 border-amber-500/30');

  // Verdict Card
  const risk = report.risk || {};
  const actionText = String(report.action || '').replace('_', ' ');
  document.getElementById('verdictCard').innerHTML = `
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-borderCol/60 pb-4">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 class="text-xl font-bold text-white">${esc(report.ticker)}</h2>
          <span class="text-xs text-slate-400">${esc(report.name)}</span>
          <span class="text-xs px-2.5 py-0.5 rounded-full font-bold border ${actionColor}">${esc(actionText)}</span>
        </div>
        <p class="text-xs text-slate-300 mt-1 max-w-2xl">${esc(report.summary)}</p>
      </div>

      <div class="flex items-center justify-between sm:justify-end gap-3">
        <div class="text-left sm:text-right">
          <div class="text-2xl font-black text-emerald-400">${esc(report.consensus_score)}</div>
          <div class="text-[10px] text-slate-400 uppercase">Consensus Score</div>
          ${scoreSourceBadge(report.score_source)}
        </div>
        <button type="button" data-action="buy" data-ticker="${esc(report.ticker)}" class="px-4 min-h-[44px] rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs shadow-md active:scale-95 transition-all">
          <i class="fa-solid fa-cart-shopping mr-1"></i> Execute Trade
        </button>
      </div>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs mt-3">
      <div><span class="text-slate-500 block sm:inline">Current Price:</span> <strong class="text-white">${esc(report.price)} ${esc(report.currency)}</strong></div>
      <div><span class="text-slate-500 block sm:inline">Target Profit:</span> <strong class="text-emerald-400">${esc(risk.take_profit_price)}</strong></div>
      <div><span class="text-slate-500 block sm:inline">Stop Loss:</span> <strong class="text-rose-400">${esc(risk.stop_loss_price)}</strong></div>
      <div><span class="text-slate-500 block sm:inline">Risk-to-Reward:</span> <strong class="text-cyan-400">${esc(risk.risk_reward_ratio)}:1</strong></div>
    </div>
  `;

  // Specialist Agents
  const t = report.technical;
  document.getElementById('techBiasBadge').textContent = t.bias;
  document.getElementById('techBiasBadge').className = `text-xs px-2 py-0.5 rounded font-bold ${t.bias === 'BULLISH' ? 'bg-emerald-500/20 text-emerald-400' : (t.bias === 'BEARISH' ? 'bg-rose-500/20 text-rose-400' : 'bg-slate-700 text-slate-300')}`;
  document.getElementById('techSummaryText').textContent = t.analysis_summary;

  const f = report.fundamental;
  document.getElementById('fundValuationBadge').textContent = f.valuation;
  document.getElementById('fundValuationBadge').className = `text-xs px-2 py-0.5 rounded font-bold ${f.valuation === 'UNDERVALUED' ? 'bg-purple-500/20 text-purple-400' : 'bg-slate-700 text-slate-300'}`;
  document.getElementById('fundSummaryText').textContent = f.analysis_summary;

  const s = report.sentiment;
  document.getElementById('sentMoodBadge').textContent = s.mood;
  document.getElementById('sentMoodBadge').className = `text-xs px-2 py-0.5 rounded font-bold ${s.mood === 'POSITIVE' ? 'bg-amber-500/20 text-amber-400' : 'bg-slate-700 text-slate-300'}`;
  document.getElementById('sentSummaryText').textContent = s.sentiment_summary;

  const r = report.risk;
  document.getElementById('riskApprovedBadge').textContent = r.approved ? 'APPROVED' : 'REJECTED';
  document.getElementById('riskApprovedBadge').className = `text-xs px-2 py-0.5 rounded font-bold ${r.approved ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'}`;
  document.getElementById('riskSummaryText').textContent = r.risk_summary;

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
    price: r.target_price ? (r.target_price * 0.92).toFixed(2) : 100,
    currency: 'USD',
    technical: { bias: 'EVALUATED', analysis_summary: r.technical_summary || 'Analysis complete' },
    fundamental: { valuation: 'ASSESSED', analysis_summary: r.fundamental_summary || 'Valuation recorded' },
    sentiment: { mood: 'REVIEWED', sentiment_summary: r.sentiment_summary || 'Sentiment factored' },
    risk: { approved: true, take_profit_price: r.target_price, stop_loss_price: r.stop_loss, risk_reward_ratio: 2.1, risk_summary: r.risk_summary || 'Risk guardrails applied' },
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
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-amber-400 animate-ping';
    if (text) text.textContent = 'Broker: Probing...';
    if (mobDot) mobDot.className = 'w-2 h-2 rounded-full bg-amber-400 animate-ping';
    if (mobText) mobText.textContent = 'Broker: Probing...';
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
        mobDot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
        mobText.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
      } else {
        mobDot.className = 'w-2 h-2 rounded-full bg-rose-500';
        mobText.textContent = `${brokerName}: Offline`;
      }
    }

    if (badge && dot && text) {
      if (isConnected) {
        dot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
        text.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
        badge.className = 'hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-medium transition-colors cursor-pointer hover:border-emerald-400';
        badge.title = `${brokerName} Connected (${data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms + 'ms' : 'Ready'})\nClick to test connection`;
      } else {
        dot.className = 'w-2 h-2 rounded-full bg-rose-500';
        text.textContent = `${brokerName}: Offline`;
        badge.className = 'hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium transition-colors cursor-pointer hover:border-rose-400';
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
        diagDot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
        diagText.textContent = 'Connected (Online)';
        diagText.className = 'text-emerald-400 font-bold';
      } else {
        diagDot.className = 'w-2 h-2 rounded-full bg-rose-500';
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
    if (mobErrDot) mobErrDot.className = 'w-2 h-2 rounded-full bg-rose-500';
    if (mobErrText) mobErrText.textContent = 'Broker: Error';
    if (badge && dot && text) {
      dot.className = 'w-2 h-2 rounded-full bg-rose-500';
      text.textContent = 'Broker: Error';
      badge.className = 'hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium';
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
    if (msData.is_weekend) {
      textEl.textContent = 'Weekend (Markets Closed)';
      badge.className = 'hidden lg:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-amber-400 text-xs font-medium';
      if (mobMarketText) mobMarketText.textContent = 'Weekend (Closed)';
    } else if (msData.eu_market.is_open || msData.us_market.is_open) {
      textEl.textContent = `🇪🇺 EU: ${msData.eu_market.status} | 🇺🇸 US: ${msData.us_market.status}`;
      badge.className = 'hidden lg:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-medium';
      if (mobMarketText) mobMarketText.textContent = 'Markets: Open';
    } else {
      textEl.textContent = '🇪🇺 EU: CLOSED | 🇺🇸 US: CLOSED';
      badge.className = 'hidden lg:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-slate-400 text-xs font-medium';
      if (mobMarketText) mobMarketText.textContent = 'Markets: Closed';
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
    if (data.has_telegram) {
      tgBadge.textContent = 'Connected & Active';
      tgBadge.className = 'px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-400';
    } else {
      tgBadge.textContent = 'Not Configured (Optional)';
      tgBadge.className = 'px-2 py-0.5 rounded text-xs font-bold bg-slate-800 text-slate-400';
    }

    const aiBadge = document.getElementById('aiEngineStatusBadge');
    if (aiBadge) {
      if (data.has_antigravity) {
        aiBadge.textContent = 'Antigravity Plan Active (Zero API Cost)';
        aiBadge.className = 'px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-400';
      } else if (data.has_gemini_key) {
        aiBadge.textContent = 'Gemini API Active';
        aiBadge.className = 'px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-400';
      } else {
        aiBadge.textContent = 'Active (Quant & LLM Ready)';
        aiBadge.className = 'px-2 py-0.5 rounded text-xs font-bold bg-slate-800 text-slate-300';
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
  if (badge) {
    badge.className = on
      ? 'hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-medium'
      : 'hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-slate-400 text-xs font-medium';
  }
  const dot = document.getElementById('autoBuyDot');
  if (dot) dot.className = on ? 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse' : 'w-2 h-2 rounded-full bg-slate-500';
  const pill = document.getElementById('mobileAutoBuyPill');
  if (pill) {
    pill.className = on
      ? 'md:hidden flex items-center gap-2 min-h-[44px] px-3 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 cursor-pointer'
      : 'md:hidden flex items-center gap-2 min-h-[44px] px-3 rounded-full bg-slate-800 border border-slate-700 text-slate-400 cursor-pointer';
  }
  const mobDot = document.getElementById('mobileAutoBuyDot');
  if (mobDot) mobDot.className = on ? 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse' : 'w-2 h-2 rounded-full bg-slate-500';
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
      if (label) label.textContent = 'HALTED - Resume';
      btn.title = 'Kill switch is ACTIVE: new automated buying is blocked. Tap to resume.';
    } else {
      btn.classList.remove('kill-switch-engaged');
      if (label) label.textContent = 'Kill Switch';
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
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> <span>Scanning...</span>`;
  btn.disabled = true;

  try {
    const res = await fetch('/api/scheduler/trigger', { method: 'POST' });
    if (res.ok) {
      showToast('Market scan initiated! Inspecting candidates...');
      setTimeout(() => {
        loadAllData();
        btn.innerHTML = `<i class="fa-solid fa-satellite-dish"></i> <span class="hidden sm:inline">Run Scan</span>`;
        btn.disabled = false;
      }, 3500);
    }
  } catch (e) {
    showToast('Scan trigger failed.', true);
    btn.innerHTML = `<i class="fa-solid fa-satellite-dish"></i> <span class="hidden sm:inline">Run Scan</span>`;
    btn.disabled = false;
  }
}

// 9b. Manual Market Data Refresh
async function refreshMarketData() {
  const btn = document.getElementById('refreshDataBtn');
  const idleHtml = `<i class="fa-solid fa-arrows-rotate text-xs sm:text-sm"></i> <span class="hidden sm:inline">Refresh Data</span>`;
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> <span class="hidden sm:inline">Refreshing...</span>`;
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
          const distAbs = Math.abs(distVal);
          // Near the limit reads as accent; more than 5% away as warn
          const isFar = distAbs > 5;
          const distCls = isFar ? 'ui-badge-warn' : 'ui-badge-accent';
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
                <span class="ui-badge ${distCls} shrink-0">${esc(distAbs.toFixed(1))}% away</span>
              </div>

              <div class="ui-track" role="img" aria-label="${esc(distAbs.toFixed(1))}% from the limit price"><span style="width:${closeness}%;background:${barColor}"></span></div>

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

async function armAITrigger(ticker) {
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
    currentMarketFilter = market;
  }
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
      nextBadgeEl.innerHTML = `<i class="fa-regular fa-clock mr-1"></i> Next: <b>${esc(data.next_session.formatted)}</b>`;
    }

    if (!data.found) {
      const marketLabel = currentMarketFilter ? `${currentMarketFilter} ` : '';
      if (headlineEl) headlineEl.innerText = `No ${marketLabel}Market Reviews Recorded Today`;
      if (contentEl) contentEl.innerHTML = `Click <b>"Run ${esc(currentMarketFilter || 'US')} Review"</b> above to evaluate top momentum candidates and generate daily advice.`;
      if (timestampEl) timestampEl.innerText = 'Pending';
      if (textContainer) textContainer.classList.add('hidden');
      return;
    }

    if (timestampEl) timestampEl.innerText = `${data.market || ''} ${data.session || ''} • ${data.timestamp || 'Today'}`.trim();
    if (headlineEl) headlineEl.innerText = data.headline || 'Market Session Review';

    let picksHtml = '';
    if (data.top_picks && data.top_picks.length > 0) {
      picksHtml = '<div class="mt-2 flex flex-wrap gap-2">' + data.top_picks.slice(0, 4).map(p => 
        `<span class="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 text-[11px] font-mono font-semibold text-emerald-400">
          ${esc(p.ticker)}: ${esc(p.action)} (${esc(p.consensus_score)}/100)
        </span>`
      ).join('') + '</div>';
    }

    let holdingsHtml = '';
    const positions = (data.portfolio_state && data.portfolio_state.positions) || [];
    if (positions.length > 0) {
      holdingsHtml = '<div class="mt-2.5 pt-2 border-t border-slate-800"><div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1.5"><i class="fa-solid fa-briefcase text-emerald-400 mr-1"></i> Current Stock Holdings:</div><div class="flex flex-wrap gap-2">' +
        positions.map(pos => {
          const pnlSign = pos.unrealized_pnl_eur >= 0 ? '+' : '';
          const pnlColor = pos.unrealized_pnl_eur >= 0 ? 'text-emerald-400' : 'text-rose-400';
          return `<span class="px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-700/80 text-[11px] font-mono">
            <b class="text-white">${esc(pos.ticker)}</b> (${esc(pos.shares)} shs @ ${esc(pos.entry_price_native)} ${esc(pos.currency)}) ➔ <span class="${pnlColor} font-bold">${pnlSign}€${num(pos.unrealized_pnl_eur)} (${pnlSign}${num(pos.unrealized_pnl_pct, 1)}%)</span>
          </span>`;
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
  showToast(`⏳ Launching ${market} Opening Review in background...`);
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
      showToast(`🚀 ${market} Review is analyzing candidates. Results will appear shortly...`);
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
            showToast(`✅ ${market} Market Review generated successfully!`);
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
  try {
    const [compRes, weeklyRes] = await Promise.all([
      fetch('/api/reviews/compare'),
      fetch('/api/reviews/weekly?days=7')
    ]);

    const comp = await compRes.json();
    weeklyReviewsCache = await weeklyRes.json();

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
      if (winRateEl) { winRateEl.innerText = '--%'; winRateEl.className = 'text-lg font-bold text-slate-400 mt-0.5'; }
      if (winCountsEl) winCountsEl.innerText = '0 picks evaluated';
      if (deltaEl) deltaEl.innerText = '€0.00 (+0.0%)';
      if (bestPickEl) bestPickEl.innerText = '--';
      if (picksContainer) {
        picksContainer.innerHTML = `<div class="text-slate-500 italic p-3 bg-slate-900/40 rounded-lg border border-slate-800/80">
          No historical picks recorded in past 7 days yet. Run an Opening Review to start tracking.
        </div>`;
      }
      return;
    }

    if (winRateEl) {
      winRateEl.innerText = `${comp.win_rate_pct}%`;
      winRateEl.className = comp.win_rate_pct >= 50 ? 'text-lg font-bold text-emerald-400 mt-0.5' : 'text-lg font-bold text-rose-400 mt-0.5';
    }
    if (winCountsEl) {
      winCountsEl.innerText = `${comp.positive_picks_count}/${comp.total_picks_tracked} picks in profit`;
    }

    if (deltaEl) {
      const sign = comp.portfolio_delta_eur >= 0 ? '+' : '';
      deltaEl.innerText = `${sign}€${comp.portfolio_delta_eur.toFixed(2)} (${sign}${comp.portfolio_delta_pct.toFixed(2)}%)`;
      deltaEl.className = comp.portfolio_delta_eur >= 0 ? 'text-lg font-bold text-emerald-400 mt-0.5' : 'text-lg font-bold text-rose-400 mt-0.5';
    }
    if (rangeEl && comp.portfolio_trajectory) {
      rangeEl.innerText = comp.portfolio_trajectory;
    }

    if (bestPickEl && comp.best_performer) {
      const bp = comp.best_performer;
      const sign = bp.pnl_pct >= 0 ? '+' : '';
      bestPickEl.innerText = `${bp.ticker} (${sign}${bp.pnl_pct}%)`;
      if (bestPickDetailsEl) bestPickDetailsEl.innerText = `${bp.rec_session} | ${bp.status_badge}`;
    }

    // Render Tracked Picks
    if (picksContainer) {
      picksContainer.innerHTML = comp.tracked_picks.map(p => {
        const isPos = p.pnl_pct >= 0;
        const sign = isPos ? '+' : '';
        const pnlCol = isPos ? 'text-emerald-400' : 'text-rose-400';
        
        let badgeClass = 'bg-slate-700/50 text-slate-300 border-slate-600';
        const badgeText = String(p.status_badge || '');
        if (badgeText.includes('TARGET')) {
          badgeClass = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
        } else if (badgeText.includes('STOP')) {
          badgeClass = 'bg-rose-500/20 text-rose-300 border-rose-500/30';
        } else if (isPos) {
          badgeClass = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
        } else {
          badgeClass = 'bg-rose-500/10 text-rose-400 border-rose-500/20';
        }

        return `
          <div class="p-3 bg-slate-900/60 rounded-lg border border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
            <div>
              <div class="flex items-center gap-2">
                <span class="font-bold text-white text-sm">${esc(p.ticker)}</span>
                <span class="text-xs text-slate-400">${esc(p.name)}</span>
                <span class="text-[10px] px-2 py-0.5 rounded border ${badgeClass} font-semibold">${esc(p.status_badge)}</span>
              </div>
              <div class="text-xs text-slate-400 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                <span>Advised In: <strong class="text-slate-200">${esc(p.rec_session)}</strong></span>
                <span>Rec Price: <strong class="text-slate-300">${esc(p.rec_price)} ${esc(p.currency)}</strong></span>
                <span>Live Price: <strong class="text-white">${esc(p.current_price)} ${esc(p.currency)}</strong></span>
                ${p.target_price ? `<span>TP: <code class="text-emerald-400">${esc(p.target_price)}</code></span>` : ''}
                ${p.stop_loss ? `<span>SL: <code class="text-rose-400">${esc(p.stop_loss)}</code></span>` : ''}
              </div>
            </div>
            <div class="sm:text-right">
              <div class="text-sm font-bold ${pnlCol}">${sign}${esc(p.pnl_pct)}%</div>
              <div class="text-[10px] text-slate-500">Performance vs Rec</div>
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
    if (icon) icon.className = 'fa-solid fa-chevron-down text-[10px]';
    renderWeeklyArchive();
  } else {
    container.classList.add('hidden');
    if (icon) icon.className = 'fa-solid fa-chevron-right text-[10px]';
  }
}

function renderWeeklyArchive() {
  const container = document.getElementById('weeklyArchiveContainer');
  if (!container || !weeklyReviewsCache) return;

  if (weeklyReviewsCache.length === 0) {
    container.innerHTML = `<div class="text-slate-500 text-xs italic">No past reviews archived yet.</div>`;
    return;
  }

  container.innerHTML = weeklyReviewsCache.map(r => {
    const picks = (r.top_picks || []).map(p => p.ticker).join(', ') || 'None'; // escaped below
    return `
      <div class="p-3 bg-slate-900/80 rounded-lg border border-slate-800 text-xs space-y-1">
        <div class="flex flex-wrap items-center justify-between gap-1">
          <span class="font-bold text-slate-200">${esc(r.market)} ${esc(r.session)} — ${esc(r.headline)}</span>
          <span class="text-[10px] font-mono text-slate-500">${esc(r.timestamp)}</span>
        </div>
        <div class="text-slate-400">${safeRichText(r.content)}</div>
        <div class="text-[11px] text-slate-500">Picks: <strong class="text-slate-300">${esc(picks)}</strong></div>
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
  showToast('Refreshing TencentDB Agent Memory...', 'info');
  await loadMemoryHub();
  showToast('Memory Hub updated.', 'success');
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
  // 1. L1 Atoms
  const atoms = (assets.chat_memory && assets.chat_memory.l1_atoms) || [];
  const atomsBadge = document.getElementById('atomsCountBadge');
  const atomsContainer = document.getElementById('atomsListContainer');
  if (atomsBadge) atomsBadge.textContent = `${atoms.length} Atoms`;
  if (atomsContainer) {
    if (atoms.length === 0) {
      atomsContainer.innerHTML = `<div class="text-xs text-slate-500 italic p-3">No atoms extracted yet.</div>`;
    } else {
      atomsContainer.innerHTML = atoms.map(a => `
        <div class="p-2.5 bg-slate-900/70 border border-slate-800 rounded-lg text-xs space-y-1">
          <div class="flex items-center justify-between">
            <span class="font-bold text-cyan-300 font-mono">[${esc((a.type || 'fact').toUpperCase())}] ${esc(a.subject)}</span>
            <span class="text-[10px] text-slate-500">${esc(Math.round((a.confidence || 1) * 100))}% conf</span>
          </div>
          <div class="text-slate-300">${esc(a.content)}</div>
        </div>
      `).join('');
    }
  }

  // 2. Skills
  const skills = assets.skills || [];
  const skillsBadge = document.getElementById('skillsCountBadge');
  const skillsContainer = document.getElementById('skillsListContainer');
  if (skillsBadge) skillsBadge.textContent = `${skills.length} Skills`;
  if (skillsContainer) {
    if (skills.length === 0) {
      skillsContainer.innerHTML = `<div class="text-xs text-slate-500 italic p-3">No skill playbooks registered.</div>`;
    } else {
      skillsContainer.innerHTML = skills.map(s => `
        <div class="p-3 bg-slate-900/70 border border-slate-800 rounded-lg text-xs space-y-1.5">
          <div class="flex items-center justify-between">
            <span class="font-bold text-emerald-300">${esc(s.name)}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-mono">${esc(s.category || 'skill')}</span>
          </div>
          <div class="text-slate-400">${esc(s.description)}</div>
          <div class="text-[11px] text-slate-500 bg-slate-950/60 p-2 rounded border border-slate-900">
            <strong class="text-slate-400">Steps:</strong> ${esc((s.execution_steps || []).join(' → '))}
          </div>
        </div>
      `).join('');
    }
  }

  // 3. Wiki
  const wiki = assets.llm_wiki || [];
  const wikiBadge = document.getElementById('wikiCountBadge');
  const wikiContainer = document.getElementById('wikiListContainer');
  if (wikiBadge) wikiBadge.textContent = `${wiki.length} Pages`;
  if (wikiContainer) {
    if (wiki.length === 0) {
      wikiContainer.innerHTML = `<div class="text-xs text-slate-500 italic p-3">No wiki pages registered.</div>`;
    } else {
      wikiContainer.innerHTML = wiki.map(w => `
        <div class="p-3 bg-slate-900/70 border border-slate-800 rounded-lg text-xs space-y-1">
          <div class="font-bold text-blue-300 flex items-center justify-between">
            <span>${esc(w.title)}</span>
            <span class="text-[10px] font-mono text-slate-500">${esc(w.page_id)}</span>
          </div>
          <div class="text-slate-400 line-clamp-2">${esc((w.content_md || '').split('\n').slice(2, 5).join(' '))}</div>
          <div class="flex flex-wrap gap-1 mt-1">
            ${(w.tags || []).map(t => `<span class="text-[10px] px-1.5 py-0.5 bg-blue-500/10 text-blue-400 rounded">#${esc(t)}</span>`).join('')}
          </div>
        </div>
      `).join('');
    }
  }

  // 4. L3 Core & Code-Graph
  const l3 = (assets.chat_memory && assets.chat_memory.l3_core) || {};
  const graph = assets.code_graph || [];
  const coreContainer = document.getElementById('coreAndGraphContainer');
  if (coreContainer) {
    coreContainer.innerHTML = `
      <div class="p-3 bg-slate-900/70 border border-slate-800 rounded-lg text-xs space-y-1.5 mb-2">
        <div class="font-bold text-purple-300 flex items-center justify-between">
          <span>${esc(l3.persona_name || 'Committee Persona')}</span>
          <span class="text-[10px] text-purple-400 bg-purple-500/10 px-1.5 py-0.5 rounded font-mono">L3 Core</span>
        </div>
        <div class="text-slate-300 font-semibold">${esc(l3.core_profile)}</div>
        <div class="text-slate-400 text-[11px] bg-slate-950/60 p-2 rounded border border-slate-900 whitespace-pre-wrap">${esc(l3.cumulative_wisdom)}</div>
      </div>
      <div class="space-y-1.5">
        <div class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Indexed Code-Graph Symbols</div>
        ${graph.map(g => `
          <div class="p-2 bg-slate-950/60 border border-slate-800/80 rounded text-[11px] flex flex-wrap items-center justify-between gap-1 break-all">
            <div>
              <span class="text-emerald-400 font-mono font-bold">${esc(g.symbol_name)}</span>
              <span class="text-slate-500 text-[10px] ml-1">(${esc(g.symbol_type)})</span>
            </div>
            <span class="text-slate-500 font-mono text-[10px]">${esc(g.file_path)}:${esc(g.line_start || 1)}</span>
          </div>
        `).join('')}
      </div>
    `;
  }
}

async function openExtractModal() {
  const sample = prompt("Enter text, trade rationale, or interaction to extract assets from (or leave blank to extract from recent system activity):");
  if (sample === null) return;
  showToast("Running TencentDB Agent Memory Asset Extraction...", "info");
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
      showToast(`Asset extraction completed: ${json.data.extracted_atoms_count} atoms extracted!`, "success");
      await loadMemoryHub();
    } else {
      showToast(`Extraction failed: ${json.message}`, "error");
    }
  } catch (err) {
    showToast(`Extraction error: ${err.message}`, "error");
  }
}


