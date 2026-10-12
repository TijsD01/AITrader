// Global State
let allocationChart = null;
let currentScreenerMarket = 'ALL';
let currentAccountCash = 10000.0;

// Initialize on DOM Load
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  loadAllData();
  // Auto-refresh every 30 seconds
  setInterval(() => {
    loadPortfolio();
    loadMarketStatus();
    loadBrokerStatus();
  }, 30000);
});

function showToast(message, isError = false) {
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

  setTimeout(() => {
    toast.classList.remove('translate-y-0', 'opacity-100');
    toast.classList.add('translate-y-[-100px]', 'opacity-0');
  }, 3500);
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
  const isHidden = menu.classList.contains('hidden');
  if (isHidden) {
    menu.classList.remove('hidden');
    if (backdrop) backdrop.classList.remove('hidden');
    if (chevron) chevron.classList.add('rotate-180');
  } else {
    menu.classList.add('hidden');
    if (backdrop) backdrop.classList.add('hidden');
    if (chevron) chevron.classList.remove('rotate-180');
  }
}

// Close Mobile Dropdown Menu
function closeMobileDropdown() {
  const menu = document.getElementById('mobileDropdownMenu');
  const backdrop = document.getElementById('mobileDropdownBackdrop');
  const chevron = document.getElementById('mobileDropdownChevron');
  if (menu) menu.classList.add('hidden');
  if (backdrop) backdrop.classList.add('hidden');
  if (chevron) chevron.classList.remove('rotate-180');
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
function switchTab(tabId) {
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
  if (mobileLabel) mobileLabel.textContent = cfg.label;
  if (mobileIcon) mobileIcon.className = `${cfg.icon} text-emerald-400 text-xs`;

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
  loadMarketStatus();
  loadBrokerStatus();
  loadPortfolio();
  loadRecommendations();
  loadMarketReview();
  loadWeeklyReviewComparison();
}

// 1. Chart Initialization
function initChart() {
  const ctx = document.getElementById('allocationChart').getContext('2d');
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
          labels: { color: '#94a3b8', font: { size: 11 } }
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
        ? 'text-xs px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-400 font-semibold uppercase border border-rose-500/30'
        : 'text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 font-semibold uppercase border border-emerald-500/30';
    }
    const pnlSub = document.getElementById('metricUnrealizedPnlSub');
    if (pnlSub) {
      pnlSub.textContent = `Open ${isLive ? 'live' : 'paper'} positions`;
    }

    currentAccountCash = summary.total_account_cash_eur ?? summary.cash_eur ?? 0;
    const deployableCash = summary.available_cash_eur ?? summary.usable_capital_eur ?? summary.cash_eur;
    const protectedReserve = summary.protected_reserve_eur || 0;
    const maxUsagePct = summary.max_capital_usage_pct ?? 100;
    const maxCapEur = summary.max_deployable_capital_eur;

    document.getElementById('metricTotalVal').textContent = `€${totalVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    document.getElementById('metricCash').textContent = `€${deployableCash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    document.getElementById('metricEquity').textContent = `€${equityVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

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
        subParts.push(`<span class="text-sky-400 font-medium" title="Capital earmarked for pending order triggers"><i class="fa-solid fa-clock"></i> €${reservedTriggers.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})} reserved (${triggersCount} trigger${triggersCount > 1 ? 's' : ''})</span>`);
      }
      if (protectedReserve > 0) {
        subParts.push(`<span class="text-amber-400 font-medium"><i class="fa-solid fa-shield-halved"></i> €${protectedReserve.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})} reserved</span>`);
      }

      if (subParts.length > 0) {
        cashSub.innerHTML = `${subParts.join(' · ')} · Total €${currentAccountCash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
      } else {
        cashSub.textContent = `Full buffer · Total account cash €${currentAccountCash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
      }
    }

    recalcCapitalPreview();
    
    const pnlElem = document.getElementById('metricUnrealizedPnl');
    const returnElem = document.getElementById('metricReturnBadge');

    const sign = pnlVal >= 0 ? '+' : '';
    pnlElem.textContent = `${sign}€${pnlVal.toFixed(2)}`;
    pnlElem.className = `text-xl sm:text-2xl font-bold mt-1 ${pnlVal >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;

    const retSign = totalReturnPct >= 0 ? '+' : '';
    returnElem.innerHTML = `<i class="fa-solid fa-arrow-trend-${totalReturnPct >= 0 ? 'up' : 'down'}"></i> ${retSign}${totalReturnPct.toFixed(2)}% total return`;
    returnElem.className = `text-xs font-semibold mt-2 flex items-center gap-1 ${totalReturnPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
    
    document.getElementById('metricPosCount').textContent = `${positions.length} open holdings`;

    // Update Chart
    if (allocationChart) {
      if (positions.length > 0) {
        const labels = ['Cash', ...positions.map(p => p.ticker)];
        const dataVals = [summary.cash_eur, ...positions.map(p => p.current_value_eur)];
        const colors = ['#059669', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#06b6d4', '#f97316', '#6366f1'];
        allocationChart.data.labels = labels;
        allocationChart.data.datasets[0].data = dataVals;
        allocationChart.data.datasets[0].backgroundColor = colors.slice(0, labels.length);
      } else {
        allocationChart.data.labels = ['Cash', 'Invested'];
        allocationChart.data.datasets[0].data = [summary.cash_eur, 0];
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
      const pnlColor = pos.unrealized_pnl_eur >= 0 ? 'text-emerald-400' : 'text-rose-400';
      const pnlSign = pos.unrealized_pnl_eur >= 0 ? '+' : '';
      return `
        <div class="p-3.5 sm:p-4 rounded-xl bg-slate-900/70 border border-borderCol hover:border-slate-700 transition-colors flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-bold text-white text-sm sm:text-base">${pos.ticker}</span>
              <span class="text-xs text-slate-400 hidden sm:inline">${pos.name || ''}</span>
              <span class="text-[11px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">${pos.shares} shs</span>
            </div>
            <div class="text-xs text-slate-400 mt-1 flex flex-wrap gap-x-3 gap-y-1">
              <span>Entry: <strong class="text-slate-200">${pos.entry_price_native} ${pos.currency}</strong></span>
              <span>Current: <strong class="text-white">${pos.current_price_native} ${pos.currency}</strong></span>
              <span>Value: <strong class="text-emerald-400">€${pos.current_value_eur}</strong></span>
            </div>
            <div class="text-xs text-slate-400 mt-1.5 flex flex-wrap gap-2">
              ${pos.stop_loss_native ? `<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 text-[11px] font-medium"><i class="fa-solid fa-shield"></i> Stop: ${pos.stop_loss_native}</span>` : ''}
              ${pos.take_profit_native ? `<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px] font-medium"><i class="fa-solid fa-bullseye"></i> Target: ${pos.take_profit_native}</span>` : ''}
            </div>
          </div>

          <div class="flex items-center justify-between sm:justify-end gap-4 border-t sm:border-t-0 border-borderCol/50 pt-2 sm:pt-0">
            <div class="sm:text-right">
              <div class="text-sm font-bold ${pnlColor}">${pnlSign}€${pos.unrealized_pnl_eur.toFixed(2)}</div>
              <div class="text-xs font-semibold ${pnlColor}">${pnlSign}${pos.unrealized_pnl_pct}%</div>
            </div>
            <button onclick="closePosition('${pos.ticker}')" class="px-3 py-1.5 rounded-lg bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30 text-xs font-semibold flex items-center gap-1 active:scale-95 transition-all">
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
    const container = document.getElementById('adviceContainer');

    if (reports.length === 0) {
      container.innerHTML = `
        <div class="col-span-full text-center py-12 text-slate-400 text-sm bg-cardBg border border-borderCol rounded-xl p-6">
          <i class="fa-solid fa-compass text-3xl mb-3 text-slate-500 block"></i>
          No daily reports generated yet today. Click <strong>"Run Scan"</strong> in the top bar to run your first candidate discovery & agent debate!
        </div>`;
      return;
    }

    container.innerHTML = reports.map(r => {
      const isBuy = r.action === 'STRONG_BUY' || r.action === 'BUY';
      const actionBadge = isBuy
        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30'
        : (r.action === 'SELL' ? 'bg-rose-500/20 text-rose-400 border-rose-500/30' : 'bg-slate-700 text-slate-300 border-slate-600');

      return `
        <div class="stock-card bg-cardBg border border-borderCol rounded-xl p-4 sm:p-5 shadow-sm space-y-4 flex flex-col justify-between">
          <div class="space-y-2.5">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-base font-bold text-white">${r.ticker}</span>
                <span class="text-[11px] font-bold px-2.5 py-0.5 rounded-full border ${actionBadge}">${r.action.replace('_', ' ')}</span>
              </div>
              <div class="text-right">
                <div class="text-xs font-bold text-emerald-400">${r.consensus_score} <span class="text-slate-400 text-[10px]">/ 100</span></div>
                <div class="text-[10px] text-slate-500">Conviction</div>
              </div>
            </div>

            <p class="text-xs text-slate-300 leading-relaxed">${r.summary || ''}</p>

            <div class="grid grid-cols-2 gap-2 text-[11px] bg-slate-900/60 p-2.5 rounded-lg border border-borderCol">
              <div>
                <span class="text-slate-500 block">Target Profit</span>
                <span class="text-emerald-400 font-semibold">${r.target_price ? r.target_price : 'N/A'}</span>
              </div>
              <div>
                <span class="text-slate-500 block">Stop Loss</span>
                <span class="text-rose-400 font-semibold">${r.stop_loss ? r.stop_loss : 'N/A'}</span>
              </div>
            </div>
          </div>

          <div class="flex items-center gap-1.5 pt-2 border-t border-borderCol/60">
            <button onclick="inspectReport('${encodeURIComponent(JSON.stringify(r))}')" class="flex-1 py-1.5 px-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium flex items-center justify-center gap-1 transition-all" title="View multi-agent debate">
              <i class="fa-solid fa-comments text-cyan-400"></i> Debate
            </button>
            <button onclick="armAITrigger('${r.ticker}')" class="flex-1 py-1.5 px-2 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 text-xs font-bold flex items-center justify-center gap-1 transition-all" title="Arm AI-calculated limit price trigger">
              <i class="fa-solid fa-crosshairs text-indigo-400"></i> Trigger
            </button>
            <button onclick="executePaperBuy('${r.ticker}')" class="flex-1 py-1.5 px-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold flex items-center justify-center gap-1 shadow-sm active:scale-95 transition-all" title="Execute immediate market buy">
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
              <span class="font-bold text-white text-sm sm:text-base">${c.ticker}</span>
              <span class="text-xs text-slate-400 ml-1.5">${c.name}</span>
            </div>
            <span class="text-xs font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
              Score ${c.pre_score}
            </span>
          </div>

          <div class="flex items-center justify-between text-xs text-slate-400 mt-2">
            <span>Price: <strong class="text-white">${c.price} ${c.currency}</strong></span>
            <span>RSI(14): <strong class="${c.rsi > 70 ? 'text-amber-400' : 'text-slate-200'}">${c.rsi}</strong></span>
            <span>5d: <strong class="${c.return_5d_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${c.return_5d_pct >= 0 ? '+' : ''}${c.return_5d_pct}%</strong></span>
          </div>

          <div class="text-[11px] text-slate-300 mt-2 bg-slate-900/70 p-2 rounded border border-borderCol/60">
            <div class="font-semibold text-cyan-400 mb-0.5">${c.setup_type}</div>
            <div class="text-slate-400">${c.notes}</div>
          </div>
        </div>

        <button onclick="analyzeDirect('${c.ticker}')" class="w-full py-1.5 px-3 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border border-emerald-500/30 text-xs font-bold flex items-center justify-center gap-1.5 transition-all">
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
      b.className = 'screener-filter-btn px-3 py-1 rounded-md bg-emerald-600 text-white';
    } else {
      b.className = 'screener-filter-btn px-3 py-1 rounded-md text-slate-400 hover:text-white';
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
  document.getElementById('verdictCard').innerHTML = `
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-borderCol/60 pb-4">
      <div>
        <div class="flex items-center gap-3">
          <h2 class="text-xl font-bold text-white">${report.ticker}</h2>
          <span class="text-xs text-slate-400">${report.name || ''}</span>
          <span class="text-xs px-2.5 py-0.5 rounded-full font-bold border ${actionColor}">${report.action.replace('_', ' ')}</span>
        </div>
        <p class="text-xs text-slate-300 mt-1 max-w-2xl">${report.summary}</p>
      </div>

      <div class="flex items-center gap-3 self-start sm:self-auto">
        <div class="text-right">
          <div class="text-2xl font-black text-emerald-400">${report.consensus_score}</div>
          <div class="text-[10px] text-slate-400 uppercase">Consensus Score</div>
        </div>
        <button onclick="executePaperBuy('${report.ticker}')" class="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs shadow-md active:scale-95 transition-all">
          <i class="fa-solid fa-cart-shopping mr-1"></i> Execute Trade
        </button>
      </div>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs mt-3">
      <div><span class="text-slate-500">Current Price:</span> <strong class="text-white">${report.price} ${report.currency}</strong></div>
      <div><span class="text-slate-500">Target Profit:</span> <strong class="text-emerald-400">${report.risk.take_profit_price}</strong></div>
      <div><span class="text-slate-500">Stop Loss:</span> <strong class="text-rose-400">${report.risk.stop_loss_price}</strong></div>
      <div><span class="text-slate-500">Risk-to-Reward:</span> <strong class="text-cyan-400">${report.risk.risk_reward_ratio}:1</strong></div>
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

function inspectReport(reportJsonStr) {
  const r = JSON.parse(decodeURIComponent(reportJsonStr));
  switchTab('debate');
  document.getElementById('analyzeInput').value = r.ticker;
  document.getElementById('debateOutputArea').classList.remove('hidden');
  renderDebateReport({
    ticker: r.ticker,
    action: r.action,
    consensus_score: r.consensus_score,
    summary: r.summary,
    price: r.target_price ? (r.target_price * 0.92).toFixed(2) : 100,
    currency: 'USD',
    technical: { bias: 'EVALUATED', analysis_summary: r.technical_summary || 'Analysis complete' },
    fundamental: { valuation: 'ASSESSED', analysis_summary: r.fundamental_summary || 'Valuation recorded' },
    sentiment: { mood: 'REVIEWED', sentiment_summary: r.sentiment_summary || 'Sentiment factored' },
    risk: { approved: true, take_profit_price: r.target_price, stop_loss_price: r.stop_loss, risk_reward_ratio: 2.1, risk_summary: r.risk_summary || 'Risk guardrails applied' },
    debate_transcript: r.debate_transcript || 'Full transcript archived.'
  });
}

// 6. Trade History
async function loadTrades() {
  try {
    const res = await fetch('/api/trades');
    const trades = await res.json();
    const tbody = document.getElementById('tradeHistoryTableBody');

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center py-8 text-slate-400">No trades executed yet.</td></tr>`;
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const isBuy = t.order_type === 'BUY';
      const typeBadge = isBuy
        ? '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold text-xs">BUY</span>'
        : '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-bold text-xs">SELL</span>';

      const pnlText = t.realized_pnl_eur !== null
        ? `<span class="${t.realized_pnl_eur >= 0 ? 'text-emerald-400' : 'text-rose-400'} font-semibold">${t.realized_pnl_eur >= 0 ? '+' : ''}€${t.realized_pnl_eur.toFixed(2)} (${t.realized_pnl_pct}%)</span>`
        : '<span class="text-slate-500">-</span>';

      return `
        <tr class="hover:bg-slate-800/40 transition-colors">
          <td class="py-3 px-4 text-slate-400 whitespace-nowrap">${t.executed_at}</td>
          <td class="py-3 px-4">${typeBadge}</td>
          <td class="py-3 px-4 font-bold text-white">${t.ticker}</td>
          <td class="py-3 px-4 text-slate-300">${t.shares}</td>
          <td class="py-3 px-4 text-slate-300">${t.price_native} ${t.currency}</td>
          <td class="py-3 px-4 text-slate-200 font-semibold">€${t.total_eur.toFixed(2)}</td>
          <td class="py-3 px-4 text-slate-400 font-mono text-xs">€${(t.fee_eur !== undefined && t.fee_eur !== null) ? Number(t.fee_eur).toFixed(2) : '0.00'}</td>
          <td class="py-3 px-4">${pnlText}</td>
          <td class="py-3 px-4 text-slate-400 text-xs truncate max-w-xs">${t.rationale || ''}</td>
        </tr>
      `;
    }).join('');

  } catch (e) {
    console.error('Failed to load trades:', e);
  }
}

// 7. Trading Actions
async function executePaperBuy(ticker) {
  if (!confirm(`Execute paper trade BUY order for ${ticker}?`)) return;

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
  if (!confirm(`Close open position for ${ticker} at market price?`)) return;

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
    if (mobDot) mobDot.className = 'w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping';
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

    const isConnected = !!data.connected;
    const brokerName = data.broker || 'BROKER';
    const isLive = (data.trading_mode || '').toUpperCase() === 'LIVE';

    // 1. Update Header Badge & Mobile Pill
    if (mobDot && mobText) {
      if (isConnected) {
        mobDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse';
        mobText.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
      } else {
        mobDot.className = 'w-1.5 h-1.5 rounded-full bg-rose-500';
        mobText.textContent = `${brokerName}: Offline`;
      }
    }

    if (badge && dot && text) {
      if (isConnected) {
        dot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
        text.textContent = isLive ? `${brokerName}: Online` : `${brokerName}: Active`;
        badge.className = 'flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-medium transition-colors cursor-pointer hover:border-emerald-400';
        badge.title = `${brokerName} Connected (${data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms + 'ms' : 'Ready'})\nClick to test connection`;
      } else {
        dot.className = 'w-2 h-2 rounded-full bg-rose-500';
        text.textContent = `${brokerName}: Offline`;
        badge.className = 'flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium transition-colors cursor-pointer hover:border-rose-400';
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
    if (badge && dot && text) {
      dot.className = 'w-2 h-2 rounded-full bg-rose-500';
      text.textContent = 'Broker: Error';
      badge.className = 'flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-medium';
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
      badge.className = 'hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-amber-400 text-xs font-medium';
      if (mobMarketText) mobMarketText.textContent = 'Weekend (Closed)';
    } else if (msData.eu_market.is_open || msData.us_market.is_open) {
      textEl.textContent = `🇪🇺 EU: ${msData.eu_market.status} | 🇺🇸 US: ${msData.us_market.status}`;
      badge.className = 'hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-medium';
      if (mobMarketText) mobMarketText.textContent = 'Markets: Open';
    } else {
      textEl.textContent = '🇪🇺 EU: CLOSED | 🇺🇸 US: CLOSED';
      badge.className = 'hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-slate-400 text-xs font-medium';
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
    document.getElementById('autoBuyStatusText').textContent = data.auto_buy_enabled ? 'Active' : 'Paused';
    const mobAutoBuy = document.getElementById('mobileAutoBuyText');
    if (mobAutoBuy) mobAutoBuy.textContent = data.auto_buy_enabled ? 'On' : 'Off';
    document.getElementById('autoBuyBadge').className = data.auto_buy_enabled
      ? 'flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-medium'
      : 'flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 text-slate-400 text-xs font-medium';

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
      document.getElementById('autoBuyStatusText').textContent = autoBuy ? 'Active' : 'Paused';
      const mobAutoBuy = document.getElementById('mobileAutoBuyText');
      if (mobAutoBuy) mobAutoBuy.textContent = autoBuy ? 'On' : 'Off';
      recalcCapitalPreview();
      await loadPortfolio();
      await loadBrokerStatus();
      if (typeof loadTriggers === 'function') await loadTriggers();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Failed to save settings.', true);
    }
  } catch (e) {
    showToast('Failed to save settings.', true);
  }
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
        btn.innerHTML = `<i class="fa-solid fa-radar"></i> <span class="hidden sm:inline">Run Scan</span>`;
        btn.disabled = false;
      }, 3500);
    }
  } catch (e) {
    showToast('Scan trigger failed.', true);
    btn.innerHTML = `<i class="fa-solid fa-radar"></i> <span class="hidden sm:inline">Run Scan</span>`;
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
async function loadTriggers() {
  try {
    const res = await fetch('/api/triggers');
    const triggers = await res.json();
    
    const pending = triggers.filter(t => t.status === 'PENDING');
    const history = triggers.filter(t => t.status !== 'PENDING');

    // Update count badge
    const badge = document.getElementById('pendingTriggersCount');
    if (badge) {
      badge.textContent = `${pending.length} Active`;
    }

    // Render Pending Triggers
    const pendingContainer = document.getElementById('pendingTriggersContainer');
    if (pendingContainer) {
      if (pending.length === 0) {
        pendingContainer.innerHTML = `
          <div class="col-span-full text-center py-10 text-slate-400 text-xs sm:text-sm bg-slate-900/30 border border-borderCol/60 rounded-xl p-6">
            <i class="fa-solid fa-crosshairs text-3xl mb-2 text-indigo-400/60 block"></i>
            No active limit triggers armed.<br>
            Enter a ticker above or click <strong>"Trigger"</strong> on any Daily Advice card to arm an AI limit order.
          </div>`;
      } else {
        pendingContainer.innerHTML = pending.map(t => {
          const isPullback = t.trigger_type === 'LIMIT_PULLBACK';
          const typeBadge = isPullback
            ? 'bg-indigo-500/20 text-indigo-300 border-indigo-500/40'
            : 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40';
          const currPrice = (t.current_price !== undefined && t.current_price !== null)
            ? t.current_price
            : (t.current_price_at_creation !== undefined && t.current_price_at_creation !== null ? t.current_price_at_creation : t.target_price);
          const distVal = (t.distance_pct !== undefined && t.distance_pct !== null)
            ? t.distance_pct
            : (currPrice > 0 ? Math.round(((t.target_price / currPrice) - 1.0) * 10000) / 100 : 0);
          const distSign = distVal > 0 ? '+' : '';
          const distColor = Math.abs(distVal) <= 1.0 ? 'text-emerald-400' : 'text-slate-300';

          return `
            <div class="bg-cardBg border border-indigo-500/30 rounded-xl p-4 shadow-sm space-y-3.5 flex flex-col justify-between hover:border-indigo-500/60 transition-colors">
              <div class="space-y-2">
                <div class="flex items-center justify-between">
                  <div class="flex items-center gap-2">
                    <span class="text-base font-bold text-white">${t.ticker}</span>
                    <span class="text-[10px] font-bold px-2 py-0.5 rounded-full border ${typeBadge}">
                      ${isPullback ? '📉 Pullback' : '📈 Breakout'}
                    </span>
                  </div>
                  <span class="text-[10px] text-slate-500 font-mono">#${t.id}</span>
                </div>
                ${t.allocation_eur > 0 ? '' : `
                <div class="text-[11px] font-semibold text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5" title="Higher-scored triggers hold the available cash or position slots. This trigger is funded automatically when cash or a slot frees up.">
                  <i class="fa-solid fa-hourglass-half"></i> Unfunded, waiting for cash
                </div>`}

                <div class="grid grid-cols-2 gap-2 bg-slate-900/70 p-2.5 rounded-lg border border-borderCol text-xs">
                  <div>
                    <span class="text-slate-500 text-[10px] block">Trigger Price</span>
                    <span class="text-indigo-400 font-bold">${t.target_price} ${t.currency}</span>
                  </div>
                  <div>
                    <span class="text-slate-500 text-[10px] block">Current Live Price</span>
                    <span class="text-white font-semibold">${currPrice} ${t.currency || 'EUR'}</span>
                  </div>
                  <div>
                    <span class="text-slate-500 text-[10px] block">Distance to Trigger</span>
                    <span class="${distColor} font-bold">${distSign}${distVal}%</span>
                  </div>
                  <div>
                    <span class="text-slate-500 text-[10px] block">Alloc (EUR)</span>
                    <span class="text-white font-semibold">€${t.allocation_eur.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                  </div>
                </div>

                <div class="flex justify-between text-[11px] text-slate-400 px-1">
                  <span>SL: <strong class="text-rose-400">${t.stop_loss || 'N/A'}</strong></span>
                  <span>TP: <strong class="text-emerald-400">${t.take_profit || 'N/A'}</strong></span>
                  <span>Score: <strong class="text-indigo-300">${t.consensus_score || 75}</strong></span>
                </div>

                <p class="text-[11px] text-slate-300 bg-slate-900/40 p-2 rounded border border-borderCol/40 italic">
                  "${t.ai_rationale || 'AI calculated tactical entry.'}"
                </p>
              </div>

              <div class="pt-2 border-t border-borderCol/50 flex justify-end">
                <button onclick="cancelTrigger(${t.id})" class="text-xs text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 px-2.5 py-1 rounded transition-colors flex items-center gap-1">
                  <i class="fa-solid fa-ban"></i> Cancel Trigger
                </button>
              </div>
            </div>
          `;
        }).join('');
      }
    }

    // Render History
    const historyBody = document.getElementById('triggerHistoryTableBody');
    if (historyBody) {
      if (history.length === 0) {
        historyBody.innerHTML = `<tr><td colspan="6" class="text-center py-6 text-slate-500">No past triggers found.</td></tr>`;
      } else {
        historyBody.innerHTML = history.map(t => {
          const statusBadge = t.status === 'TRIGGERED'
            ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">TRIGGERED</span>'
            : '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400">CANCELLED</span>';

          return `
            <tr class="hover:bg-slate-800/40 transition-colors">
              <td class="py-2.5 px-3 font-bold text-white">${t.ticker}</td>
              <td class="py-2.5 px-3 text-slate-400">${t.trigger_type}</td>
              <td class="py-2.5 px-3 font-semibold text-indigo-300">${t.target_price} ${t.currency}</td>
              <td class="py-2.5 px-3">${statusBadge}</td>
              <td class="py-2.5 px-3 text-slate-400 font-mono text-[11px]">${t.triggered_at || t.created_at}</td>
              <td class="py-2.5 px-3 text-slate-300 text-[11px] max-w-xs truncate" title="${t.ai_rationale || ''}">${t.ai_rationale || ''}</td>
            </tr>
          `;
        }).join('');
      }
    }

  } catch (e) {
    console.error('Failed to load triggers:', e);
  }
}

async function armTriggerFromInput() {
  const input = document.getElementById('triggerTickerInput');
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) {
    showToast('Please enter a stock ticker.', true);
    return;
  }

  const btn = document.getElementById('armTriggerBtn');
  btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Analyzing...`;
  btn.disabled = true;

  try {
    const res = await fetch('/api/triggers/from-ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`🎯 AI Trigger armed for ${ticker}!`);
      input.value = '';
      loadTriggers();
    } else {
      showToast(data.detail || data.message || 'Failed to arm trigger.', true);
    }
  } catch (e) {
    showToast('Error arming trigger.', true);
  } finally {
    btn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Calculate & Arm Trigger`;
    btn.disabled = false;
  }
}

async function armAITrigger(ticker) {
  showToast(`⏳ AI analyzing ${ticker} and arming optimal entry trigger...`);
  try {
    const res = await fetch('/api/triggers/from-ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`🎯 AI Trigger armed for ${ticker} at ${data.target_price}!`);
      loadTriggers();
    } else {
      showToast(data.detail || data.message || 'Failed to arm trigger.', true);
    }
  } catch (e) {
    showToast('Error arming trigger.', true);
  }
}

async function cancelTrigger(triggerId) {
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
      winRateEl.className = data.win_rate_pct >= 50 ? 'text-lg font-bold text-emerald-400 mt-0.5' : 'text-lg font-bold text-rose-400 mt-0.5';
    }
    if (countsEl) countsEl.innerText = `${data.total_closed_trades} Trades (${data.wins}W / ${data.losses}L)`;

    const p = data.active_parameters || {};
    if (minScoreEl) minScoreEl.innerText = `${p.min_consensus_score || 70}/100`;
    if (posSizeEl) posSizeEl.innerText = `${((p.position_size_pct || 0.08) * 100).toFixed(1)}%`;
    if (riskParamsEl) riskParamsEl.innerText = `1:${p.take_profit_rr || 2.1} (${p.atr_stop_multiplier || 1.75}x)`;

    if (container && data.recent_lessons) {
      if (data.recent_lessons.length === 0) {
        container.innerHTML = `<div class="text-slate-500 italic p-3 bg-slate-900/40 rounded-lg border border-slate-800/80">
          No trade post-mortems recorded yet. Lessons will appear here automatically when positions exit.
        </div>`;
      } else {
        container.innerHTML = data.recent_lessons.slice(0, 4).map(l => {
          const isWin = l.outcome === 'WIN';
          const isLoss = l.outcome === 'LOSS';
          const badgeCol = isWin ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : (isLoss ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' : 'bg-slate-500/10 text-slate-400 border-slate-500/20');
          const icon = isWin ? 'fa-arrow-trend-up' : (isLoss ? 'fa-arrow-trend-down' : 'fa-minus');
          const pnlSign = (l.realized_pnl_eur >= 0) ? '+' : '';

          return `
            <div class="p-3 bg-slate-900/60 rounded-lg border border-slate-800 space-y-1.5">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded text-[11px] font-bold border ${badgeCol}">
                    <i class="fa-solid ${icon} mr-1"></i>${l.outcome} ${l.ticker}
                  </span>
                  <span class="font-semibold text-slate-200">${pnlSign}€${l.realized_pnl_eur.toFixed(2)} (${pnlSign}${l.realized_pnl_pct.toFixed(1)}%)</span>
                </div>
                <span class="text-[10px] text-slate-500 font-mono">${l.created_at}</span>
              </div>
              <div class="text-[11px] text-slate-300">
                <span class="text-slate-400 font-medium">Diagnosis:</span> ${isLoss ? l.what_went_wrong : l.what_went_well}
              </div>
              <div class="text-[11px] text-purple-300 font-medium bg-purple-950/30 px-2 py-1 rounded border border-purple-900/40">
                <i class="fa-solid fa-lightbulb mr-1 text-amber-400"></i><b>Lesson:</b> ${l.lesson}
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
  showToast('⏳ Re-evaluating trade performance and calibrating risk parameters...');
  try {
    const res = await fetch('/api/learning/calibrate', { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success) {
      showToast(`⚙️ Calibration complete! Win Rate: ${data.win_rate_pct}%`);
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
      nextBadgeEl.innerHTML = `<i class="fa-regular fa-clock mr-1"></i> Next: <b>${data.next_session.formatted}</b>`;
    }

    if (!data.found) {
      const marketLabel = currentMarketFilter ? `${currentMarketFilter} ` : '';
      if (headlineEl) headlineEl.innerText = `No ${marketLabel}Market Reviews Recorded Today`;
      if (contentEl) contentEl.innerHTML = `Click <b>"Run ${currentMarketFilter || 'US'} Review"</b> above to evaluate top momentum candidates and generate daily advice.`;
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
          ${p.ticker}: ${p.action} (${p.consensus_score}/100)
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
            <b class="text-white">${pos.ticker}</b> (${pos.shares} shs @ ${pos.entry_price_native} ${pos.currency}) ➔ <span class="${pnlColor} font-bold">${pnlSign}€${(pos.unrealized_pnl_eur || 0).toFixed(2)} (${pnlSign}${(pos.unrealized_pnl_pct || 0).toFixed(1)}%)</span>
          </span>`;
        }).join('') + '</div></div>';
    }

    if (contentEl) {
      contentEl.innerHTML = `<span>${data.content || ''}</span>${picksHtml}${holdingsHtml}`;
    }

    if (textContainer && data.review_text) {
      textContainer.innerHTML = data.review_text;
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
        if (p.status_badge.includes('TARGET')) {
          badgeClass = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
        } else if (p.status_badge.includes('STOP')) {
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
                <span class="font-bold text-white text-sm">${p.ticker}</span>
                <span class="text-xs text-slate-400">${p.name || ''}</span>
                <span class="text-[10px] px-2 py-0.5 rounded border ${badgeClass} font-semibold">${p.status_badge}</span>
              </div>
              <div class="text-xs text-slate-400 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                <span>Advised In: <strong class="text-slate-200">${p.rec_session}</strong></span>
                <span>Rec Price: <strong class="text-slate-300">${p.rec_price} ${p.currency}</strong></span>
                <span>Live Price: <strong class="text-white">${p.current_price} ${p.currency}</strong></span>
                ${p.target_price ? `<span>TP: <code class="text-emerald-400">${p.target_price}</code></span>` : ''}
                ${p.stop_loss ? `<span>SL: <code class="text-rose-400">${p.stop_loss}</code></span>` : ''}
              </div>
            </div>
            <div class="sm:text-right">
              <div class="text-sm font-bold ${pnlCol}">${sign}${p.pnl_pct}%</div>
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
    const picks = (r.top_picks || []).map(p => p.ticker).join(', ') || 'None';
    return `
      <div class="p-3 bg-slate-900/80 rounded-lg border border-slate-800 text-xs space-y-1">
        <div class="flex items-center justify-between">
          <span class="font-bold text-slate-200">${r.market} ${r.session} — ${r.headline}</span>
          <span class="text-[10px] font-mono text-slate-500">${r.timestamp}</span>
        </div>
        <div class="text-slate-400">${r.content}</div>
        <div class="text-[11px] text-slate-500">Picks: <strong class="text-slate-300">${picks}</strong></div>
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
            <span class="font-bold text-cyan-300 font-mono">[${(a.type || 'fact').toUpperCase()}] ${a.subject || ''}</span>
            <span class="text-[10px] text-slate-500">${Math.round((a.confidence || 1) * 100)}% conf</span>
          </div>
          <div class="text-slate-300">${a.content}</div>
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
            <span class="font-bold text-emerald-300">${s.name}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-mono">${s.category || 'skill'}</span>
          </div>
          <div class="text-slate-400">${s.description}</div>
          <div class="text-[11px] text-slate-500 bg-slate-950/60 p-2 rounded border border-slate-900">
            <strong class="text-slate-400">Steps:</strong> ${(s.execution_steps || []).join(' → ')}
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
            <span>${w.title}</span>
            <span class="text-[10px] font-mono text-slate-500">${w.page_id}</span>
          </div>
          <div class="text-slate-400 line-clamp-2">${(w.content_md || '').split('\n').slice(2, 5).join(' ')}</div>
          <div class="flex flex-wrap gap-1 mt-1">
            ${(w.tags || []).map(t => `<span class="text-[9px] px-1.5 py-0.2 bg-blue-500/10 text-blue-400 rounded">#${t}</span>`).join('')}
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
          <span>${l3.persona_name || 'Committee Persona'}</span>
          <span class="text-[10px] text-purple-400 bg-purple-500/10 px-1.5 py-0.5 rounded font-mono">L3 Core</span>
        </div>
        <div class="text-slate-300 font-semibold">${l3.core_profile || ''}</div>
        <div class="text-slate-400 text-[11px] bg-slate-950/60 p-2 rounded border border-slate-900 whitespace-pre-wrap">${l3.cumulative_wisdom || ''}</div>
      </div>
      <div class="space-y-1.5">
        <div class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Indexed Code-Graph Symbols</div>
        ${graph.map(g => `
          <div class="p-2 bg-slate-950/60 border border-slate-800/80 rounded text-[11px] flex items-center justify-between">
            <div>
              <span class="text-emerald-400 font-mono font-bold">${g.symbol_name}</span>
              <span class="text-slate-500 text-[10px] ml-1">(${g.symbol_type})</span>
            </div>
            <span class="text-slate-500 font-mono text-[10px]">${g.file_path}:${g.line_start || 1}</span>
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


