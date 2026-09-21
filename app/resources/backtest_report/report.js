/* 回測報表的瀏覽器端邏輯。統計(computeStats)與繪圖(drawChart)從
   scripts/options_backtest/backtest_dashboard.html 複製而來，改動：
   (1) 整包放進 IIFE，只對外露出 window.BtReport，不污染全域；
   (2) 不再讀 CSV，資料由 Python 用 BtReport.upsertRuns() 推進來，所有回測都留在瀏覽器記憶體裡，
       切換回測完全在前端完成，不回伺服器；
   (3) 只剩「一列 = 一個單邊價差」這一種交易格式；
   (4) 所有 DOM 查找都限定在 #bt-report-root 底下；配色改深色主題。
   dialog 的 tab_panels 是 keep-alive 但「第一次進入才渲染」，所以資料進來時 DOM 可能還沒出現，
   一律用 whenReady() 輪詢等 root 出現再動作。 */
(function () {
'use strict';

const root = () => document.getElementById('bt-report-root');
const $ = id => { const r = root(); return r ? r.querySelector('#' + id) : null; };

let runs = [];          // [{id,name,ticker,start,end,description,updated_at,trades,benchmark}]，最近更新的在前
let currentId = null;

function whenReady(fn, tries = 80) {
  if (root() && $('datasetSelect')) fn();
  else if (tries > 0) setTimeout(() => whenReady(fn, tries - 1), 100);
}

function bindOnce() {
  const r = root();
  if (r.dataset.bound === '1') return;   // 同一個 DOM 只綁一次；DOM 被重建(新節點)就會重新綁
  r.dataset.bound = '1';
  $('datasetSelect').addEventListener('change', () => { currentId = $('datasetSelect').value; render(currentId); });
  $('contractsSelect').addEventListener('change', () => render(currentId));
  $('prevRun').addEventListener('click', () => step(-1));
  $('nextRun').addEventListener('click', () => step(1));
}

function step(delta) {
  if (!runs.length) return;
  const i = Math.max(0, runs.findIndex(r => r.id === currentId));
  currentId = runs[(i + delta + runs.length) % runs.length].id;
  refresh();
}

function rebuildSelect() {
  const sel = $('datasetSelect');
  sel.innerHTML = runs.map(r => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.name)}(${escapeHtml(r.ticker)})</option>`).join('');
  if (currentId && runs.some(r => r.id === currentId)) sel.value = currentId;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function prepareRun(raw) {
  const trades = raw.trades.map(t => ({ ...t, entry_date: new Date(t.entry_date), exit_date: new Date(t.exit_date) }));
  trades.sort((a, b) => a.exit_date - b.exit_date);
  const benchmark = (raw.benchmark || []).map(([d, c]) => ({ date: new Date(d), close: c })).sort((a, b) => a.date - b.date);
  // equity: [[日期, 每股毛損益]]，逐日「已平倉 + 未平倉浮動損益」(舊存檔沒有這欄位，是空陣列)
  const equity = (raw.equity || []).map(([d, g]) => ({ date: new Date(d), gross: g })).sort((a, b) => a.date - b.date);
  return { ...raw, trades, benchmark, equity };
}

function refresh() {
  whenReady(() => {
    bindOnce();
    if (currentId && !runs.some(r => r.id === currentId)) currentId = null;
    if (!currentId && runs.length) currentId = runs[0].id;
    rebuildSelect();
    render(currentId);
  });
}

window.BtReport = {
  // 新增或覆蓋(以 id 為準)一批回測；不會切換目前顯示的那一個，要切換用 show()。
  upsertRuns(list) {
    for (const raw of list) {
      const run = prepareRun(raw);
      const i = runs.findIndex(r => r.id === run.id);
      if (i >= 0) runs[i] = run; else runs.push(run);
    }
    runs.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
    refresh();
  },
  removeRun(id) { runs = runs.filter(r => r.id !== id); refresh(); },
  renameRun(id, name) { const r = runs.find(x => x.id === id); if (r) { r.name = name; refresh(); } },
  show(id) { currentId = id; refresh(); },
  refresh,
  has(id) { return runs.some(r => r.id === id); },
};


const sum = arr => arr.reduce((a, b) => a + b, 0);
const avg = arr => arr.length ? sum(arr) / arr.length : 0;
const DAY = 86400000;

// IBKR Pro Fixed 美股選擇權費率(跟 app/models/backtest/spec.py 的 IBKR_RATE_PER_CONTRACT/
// IBKR_MIN_PER_LEG 一致)：USD 0.65/口，combo單每一腳分開套用最低 USD 1.00。
const IBKR_RATE = 0.65;
const IBKR_MIN_PER_LEG = 1.00;
const CONTRACT_MULTIPLIER = 100;

// 一列 = 一邊：價差 2 腳(短+長)、裸賣 1 腳(K_long 是 null)，開倉+平倉各一次，每腳套用最低收費。
function commissionOf(t, contracts) {
  const legs = t.K_long == null ? 1 : 2;
  return legs * 2 * Math.max(IBKR_RATE * contracts, IBKR_MIN_PER_LEG);
}

// 所有數字都是「扣掉IBKR手續費之後」算的：pnl/margin 是每股單位，乘
// CONTRACT_MULTIPLIER*contracts換算成美元，再扣掉每筆開倉+平倉的手續費。commission
// 不依賴存檔裡的pnl_usd欄位，即時算，口數選單才能馬上切換重算整份報表。
function computeStats(trades, contracts) {
  const rows = trades.map(t => ({ t, net: t.pnl * CONTRACT_MULTIPLIER * contracts - commissionOf(t, contracts) }));
  const n = rows.length;
  const wins = rows.filter(r => r.net > 0);
  const losses = rows.filter(r => r.net <= 0);
  const winRate = n ? wins.length / n : 0;
  const avgWin = avg(wins.map(r => r.net));
  const avgLoss = avg(losses.map(r => r.net));
  const totalWin = sum(wins.map(r => r.net));
  const totalLoss = sum(losses.map(r => r.net));
  const profitFactor = totalLoss !== 0 ? totalWin / Math.abs(totalLoss) : Infinity;
  const winLossRatio = avgLoss !== 0 ? avgWin / Math.abs(avgLoss) : Infinity;
  const expectancy = winRate * avgWin + (1 - winRate) * avgLoss;
  const totalPnl = sum(rows.map(r => r.net));           // 淨損益(已扣手續費)
  const grossUsd = sum(trades.map(t => t.pnl * CONTRACT_MULTIPLIER * contracts));
  const commissionUsd = sum(trades.map(t => commissionOf(t, contracts)));
  // t.margin 是每一列開倉當下「整組同時持有的部位」的每股保證金(引擎算的，見 engine.position_margin)。
  const avgMargin = avg(trades.map(t => t.margin)) * CONTRACT_MULTIPLIER * contracts;
  const peakMargin = trades.length ? Math.max(...trades.map(t => t.margin)) * CONTRACT_MULTIPLIER * contracts : 0;
  const returnOnMargin = avgMargin ? totalPnl / avgMargin : NaN;

  let cum = 0;
  const equitySeries = rows.map(r => { cum += r.net; return { date: r.t.exit_date, y: cum }; });

  let peak = -Infinity, peakDate = trades.length ? trades[0].entry_date : null, maxDDDays = 0;
  const ddSeries = equitySeries.map(p => {
    if (p.y >= peak) { peak = p.y; peakDate = p.date; }
    const dd = p.y - peak;
    const days = (p.date - peakDate) / DAY;
    if (days > maxDDDays) maxDDDays = days;
    return { date: p.date, y: dd };
  });
  const maxDD = ddSeries.length ? Math.min(...ddSeries.map(p => p.y)) : 0;

  let curWin = 0, curLoss = 0, maxWinStreak = 0, maxLossStreak = 0;
  rows.forEach(r => {
    if (r.net > 0) { curWin++; curLoss = 0; } else { curLoss++; curWin = 0; }
    maxWinStreak = Math.max(maxWinStreak, curWin);
    maxLossStreak = Math.max(maxLossStreak, curLoss);
  });

  const byYear = {};
  rows.forEach(r => {
    const y = r.t.exit_date.getFullYear();
    byYear[y] ??= { trades: 0, wins: 0, pnl: 0 };
    byYear[y].trades++;
    if (r.net > 0) byYear[y].wins++;
    byYear[y].pnl += r.net;
  });

  const byReason = {};
  rows.forEach(r => {
    byReason[r.t.exit_reason] ??= { count: 0, pnl: 0 };
    byReason[r.t.exit_reason].count++;
    byReason[r.t.exit_reason].pnl += r.net;
  });

  const first = trades[0], last = trades[trades.length - 1];
  const years = first && last ? (last.exit_date - first.entry_date) / (365.25 * DAY) : NaN;
  const annualizedReturn = (years > 0 && returnOnMargin > -1) ? Math.pow(1 + returnOnMargin, 1 / years) - 1 : NaN;

  let best = null, worst = null;
  rows.forEach(r => {
    if (!best || r.net > best.net) best = r;
    if (!worst || r.net < worst.net) worst = r;
  });

  return {
    n, winRate, avgWin, avgLoss, profitFactor, winLossRatio, expectancy,
    totalPnl, avgMargin, peakMargin, returnOnMargin, years, annualizedReturn,
    equitySeries, ddSeries, maxDD, maxDDDays,
    maxWinStreak, maxLossStreak, byYear, byReason,
    best: best ? { ...best.t, pnl: best.net } : null,
    worst: worst ? { ...worst.t, pnl: worst.net } : null,
    firstDate: first ? first.entry_date : null, lastDate: last ? last.exit_date : null,
    contracts, grossUsd, commissionUsd,
  };
}

// 含未平倉浮動損益的逐日權益(mark-to-market)。引擎存的是每股毛損益(已平倉 + 未平倉浮動，不含手續費)，
// 這裡換成美元並扣掉「到當天為止已平倉交易」的手續費——跟已實現權益曲線同一套規則(手續費在出場那天才
// 扣)，所以兩條線在每一筆出場日會重合，中間的落差就是未平倉部位的浮動損益。trades 已依 exit_date 排序。
// 回傳 null 代表這份回測沒有逐日權益(舊存檔，重跑一次才會有)。
function computeMtm(equity, trades, contracts) {
  if (!equity.length) return null;
  const mult = CONTRACT_MULTIPLIER * contracts;
  let ti = 0, realizedGross = 0, cumCommission = 0;
  let peak = 0, maxDD = 0, maxDDDate = null, maxFloat = 0, maxFloatDate = null;
  const series = [], ddSeries = [];
  equity.forEach(p => {
    while (ti < trades.length && trades[ti].exit_date <= p.date) {
      realizedGross += trades[ti].pnl * mult;
      cumCommission += commissionOf(trades[ti], contracts);
      ti++;
    }
    const y = p.gross * mult - cumCommission;
    const floating = p.gross * mult - realizedGross;   // 未平倉部位目前的浮動損益(不含手續費)
    peak = Math.max(peak, y);
    const dd = y - peak;
    if (dd < maxDD) { maxDD = dd; maxDDDate = p.date; }
    if (floating < maxFloat) { maxFloat = floating; maxFloatDate = p.date; }
    series.push({ date: p.date, y });
    ddSeries.push({ date: p.date, y: dd });
  });
  return { series, ddSeries, maxDD, maxDDDate, maxFloat, maxFloatDate };
}

function fmtNum(v, d = 3) { return Number.isFinite(v) ? v.toFixed(d) : '—'; }
function fmtUsd(v) { return Number.isFinite(v) ? (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'; }
function fmtPct(v, d = 1) { return Number.isFinite(v) ? (v * 100).toFixed(d) + '%' : '—'; }
function fmtDate(d) { return d instanceof Date && !isNaN(d) ? d.toISOString().slice(0, 10) : '—'; }
function cls(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); }

function render(id) {
  const run = runs.find(r => r.id === id);
  if (!run) {
    $('empty').style.display = '';
    $('report').style.display = 'none';
    $('runInfo').textContent = '';
    return;
  }
  const trades = run.trades;
  $('empty').style.display = 'none';
  $('report').style.display = 'block';
  const contracts = parseInt($('contractsSelect').value, 10) || 1;
  const s = computeStats(trades, contracts);
  const mtm = computeMtm(run.equity, trades, contracts);

  $('runInfo').textContent = `${run.name} · ${run.ticker} · ${run.start} ~ ${run.end}`;
  $('strategyDesc').innerHTML = (run.description || []).map(l => `<div class="desc-line">${escapeHtml(l)}</div>`).join('');

  const intradayN = trades.filter(t => t.fill_mode === 'intraday').length;
  $('fillModeHint').textContent = intradayN === 0
    ? '停利/停損：全部用收盤價成交'
    : `停利/停損：${intradayN} 筆盤中觸價成交、${trades.length - intradayN} 筆收盤價成交(到期天數出場一律收盤價)`;

  const cards = [
    ['交易次數', s.n, ''],
    ['期間', `${fmtDate(s.firstDate)} ~ ${fmtDate(s.lastDate)}`, ''],
    ['勝率(扣手續費後)', fmtPct(s.winRate), ''],
    [`毛利(${contracts}口)`, fmtUsd(s.grossUsd), cls(s.grossUsd)],
    ['IBKR手續費', fmtUsd(-s.commissionUsd), 'neg'],
    [`淨損益(${contracts}口)`, fmtUsd(s.totalPnl), cls(s.totalPnl)],
    ['初始保證金建議', fmtUsd(s.avgMargin), ''],
    ['淨報酬率(對保證金)', fmtPct(s.returnOnMargin), cls(s.returnOnMargin)],
    ['年化報酬率(概算)', fmtPct(s.annualizedReturn), cls(s.annualizedReturn)],
    ['淨最大回撤(已平倉)', fmtUsd(s.maxDD), 'neg'],
    ['最大回撤(含未平倉)', mtm ? `${fmtUsd(mtm.maxDD)}${mtm.maxDDDate ? ` (${fmtDate(mtm.maxDDDate)})` : ''}` : '—', 'neg'],
    ['最大浮虧(未平倉部位)', mtm ? `${fmtUsd(mtm.maxFloat)}${mtm.maxFloatDate ? ` (${fmtDate(mtm.maxFloatDate)})` : ''}` : '—', 'neg'],
  ];
  $('statCards').innerHTML = cards.map(([label, value, c]) => `
    <div class="card"><div class="label">${label}</div><div class="value ${c}">${value}</div></div>
  `).join('');

  // 初始保證金建議的公式說明。數字本身是引擎逐列算好存進去的(t.margin)，這裡只解釋算法。
  $('marginNote').innerHTML = `
    <b>初始保證金建議</b> = 平均(每列開倉當下「整組同時持有的部位」的每股保證金) × ${CONTRACT_MULTIPLIER} 股 × ${contracts} 口
    = <b>${fmtUsd(s.avgMargin)}</b>；本次單列最大 ${fmtUsd(s.peakMargin)}。<br>
    • 這是簡化的 Reg-T 估算，用開倉當天的現價，之後不隨價格變動重算；實際保證金要求以 IBKR 帳戶顯示為準。<br>
    • 價差邊的需求 = 價差寬度；裸賣邊的需求 = max(20% × 現價 − 價外距離, 10% × 參考價) + 該邊權利金
    (參考價：put 用履約價、call 用現價)。<br>
    • put 邊、call 邊不會同時虧損(現價只會越過其中一邊)，所以整組只取需求最大的那一邊，不是兩邊相加；
    另一邊如果是裸賣，再加上它的權利金(裸雙賣的 Reg-T 算法)；最後扣掉整組收到的全部權利金。<br>
    • Iron Condor 化簡後 = 較寬的寬度 − 兩邊權利金合計；裸雙賣 = 兩邊裸賣需求較大的那一個。
    Jade Lizard/Twisted Sister 是上述規則的混合。<br>
    • 同一天同時開的兩邊(兩列)共用同一個整組數字；只剩一邊持有時，就只算那一邊。<br>
    • 裸賣沒有虧損上限，這個保證金只是資金占用的估計，不是最大虧損。<br>
    • 「淨報酬率(對保證金)」和「年化報酬率」都是拿這個建議值當分母。`;

  $('riskCards').innerHTML = [
    ['單筆期望值(淨)', fmtUsd(s.expectancy), cls(s.expectancy)],
    ['平均獲利(淨)', fmtUsd(s.avgWin), 'pos'],
    ['平均虧損(淨)', fmtUsd(s.avgLoss), 'neg'],
    ['盈虧比(平均獲利/|平均虧損|)', fmtNum(s.winLossRatio, 2), ''],
    ['Profit Factor(總獲利/|總虧損|)', fmtNum(s.profitFactor, 2), ''],
    ['平均保證金', fmtUsd(s.avgMargin), ''],
    ['最大回撤持續天數', Math.round(s.maxDDDays) + ' 天', ''],
    ['最大連續獲利', s.maxWinStreak + ' 筆', 'pos'],
    ['最大連續虧損', s.maxLossStreak + ' 筆', 'neg'],
    ['最佳單筆(淨)', s.best ? `${fmtUsd(s.best.pnl)} (${fmtDate(s.best.exit_date)})` : '—', 'pos'],
    ['最差單筆(淨)', s.worst ? `${fmtUsd(s.worst.pnl)} (${fmtDate(s.worst.exit_date)})` : '—', 'neg'],
  ].map(([label, value, c]) => `
    <div class="card"><div class="label">${label}</div><div class="value ${c}" style="font-size:15px">${value}</div></div>
  `).join('');

  $('eqBadge').textContent = fmtUsd(s.totalPnl);
  $('ddBadge').textContent = mtm ? `MDD 已平倉 ${fmtUsd(s.maxDD)} · 含未平倉 ${fmtUsd(mtm.maxDD)}` : `MDD ${fmtUsd(s.maxDD)}`;

  // buy-and-hold對照線：拿策略「平均保證金」當作同樣的投入資金，同一天買進標的、一路抱到底，
  // 這樣兩條線才是同樣的資金水位在比，不是選擇權保證金的損益直接對比買一股的損益(基準不同、不公平)。
  let buyHoldSeries = [];
  if (run.benchmark.length && s.firstDate && s.lastDate) {
    const inRange = run.benchmark.filter(p => p.date >= s.firstDate && p.date <= s.lastDate);
    if (inRange.length > 1) {
      const S0 = inRange[0].close;
      buyHoldSeries = inRange.map(p => ({ date: p.date, y: s.avgMargin * (p.close / S0 - 1) }));
    }
  }
  const eqSeriesList = [{ key: 'realized', data: s.equitySeries, color: '#3b82f6', label: `策略(已平倉淨利，${contracts}口)` }];
  if (mtm) eqSeriesList.push({ key: 'mtm', data: mtm.series, color: '#f59e0b', label: '策略(含未平倉浮動損益，逐日收盤)' });
  if (buyHoldSeries.length) {
    eqSeriesList.push({ key: 'buyhold', data: buyHoldSeries, color: '#22c55e', dashed: true,
      label: `Buy&Hold ${run.ticker}(投入同樣資金${fmtUsd(s.avgMargin)})` });
  }
  drawChart('equityChart', 'eqTooltip', eqSeriesList, {
    zeroLine: true, legendId: 'eqLegend',
    legendNote: mtm ? '' : '(這份回測存檔沒有逐日浮動損益資料，重跑一次即可產生)',
  });
  const ddSeriesList = [{ key: 'dd_realized', data: s.ddSeries, color: '#ef4444', fillDown: true, label: '回撤(已平倉)' }];
  if (mtm) ddSeriesList.push({ key: 'dd_mtm', data: mtm.ddSeries, color: '#f59e0b', label: '回撤(含未平倉)' });
  drawChart('ddChart', 'ddTooltip', ddSeriesList, { zeroLine: true, legendId: 'ddLegend' });

  const years = Object.keys(s.byYear).sort();
  $('yearlyTable').innerHTML = `
    <thead><tr><th>年度</th><th>交易次數</th><th>勝率</th><th>淨損益</th></tr></thead>
    <tbody>${years.map(y => {
      const r = s.byYear[y];
      return `<tr><td>${y}</td><td>${r.trades}</td><td>${fmtPct(r.wins / r.trades)}</td><td class="${cls(r.pnl)}">${fmtUsd(r.pnl)}</td></tr>`;
    }).join('')}
    <tr style="font-weight:600"><td>總計</td><td>${s.n}</td><td>${fmtPct(s.winRate)}</td><td class="${cls(s.totalPnl)}">${fmtUsd(s.totalPnl)}</td></tr>
    </tbody>`;

  const reasonLabel = {
    group_dte: '整組到期天數', group_take_profit: '整組停利', group_stop_loss: '整組停損',
    leg_dte: '單邊到期天數', leg_take_profit: '單邊停利', leg_stop_loss: '單邊停損',
  };
  $('reasonTable').innerHTML = `
    <thead><tr><th>出場原因</th><th>次數</th><th>佔比</th><th>淨損益</th></tr></thead>
    <tbody>${Object.entries(s.byReason).map(([reason, r]) => `
      <tr><td>${reasonLabel[reason] || reason}</td><td>${r.count}</td><td>${fmtPct(r.count / s.n)}</td><td class="${cls(r.pnl)}">${fmtUsd(r.pnl)}</td></tr>
    `).join('')}
    <tr style="font-weight:600"><td>總計</td><td>${s.n}</td><td>${fmtPct(1)}</td><td class="${cls(s.totalPnl)}">${fmtUsd(s.totalPnl)}</td></tr>
    </tbody>`;

  $('streakTable').innerHTML = `
    <tbody>
      <tr><td>最大連續獲利次數</td><td class="pos">${s.maxWinStreak}</td></tr>
      <tr><td>最大連續虧損次數</td><td class="neg">${s.maxLossStreak}</td></tr>
    </tbody>`;

  const netOf = t => t.pnl * CONTRACT_MULTIPLIER * contracts - commissionOf(t, contracts);
  $('tradesTable').innerHTML = `
    <thead><tr>
      <th>腳</th><th>進場</th><th>出場</th><th>短履約價</th><th>長履約價</th>
      <th>收信用</th><th>平倉值</th><th>損益</th><th>保證金</th><th>出場原因</th><th>成交</th><th>淨損益$(${contracts}口)</th>
    </tr></thead>
    <tbody>${trades.map(t => `
      <tr>
        <td>${t.side}</td>
        <td>${fmtDate(t.entry_date)}</td><td>${fmtDate(t.exit_date)}</td>
        <td>${t.K_short}</td><td>${t.K_long ?? '—'}</td>
        <td>${fmtNum(t.entry_credit)}</td><td>${fmtNum(t.exit_value)}</td>
        <td class="${cls(t.pnl)}">${fmtNum(t.pnl)}</td><td>${fmtNum(t.margin, 2)}</td>
        <td>${reasonLabel[t.exit_reason] || t.exit_reason}</td>
        <td>${t.fill_mode === 'intraday' ? '盤中' : '收盤'}</td>
        <td class="${cls(netOf(t))}">${netOf(t).toFixed(2)}</td>
      </tr>
    `).join('')}</tbody>`;
}

// seriesList: [{data:[{date,y}], color, label, fillDown}]，可以疊多條線(策略淨值 vs buy-and-hold)。
// 各圖表目前被使用者點掉(隱藏)的曲線，key 是曲線的 `key` 欄位，依圖表(svgId)分開記。放在模組層級，
// 切換回測/口數重畫時維持使用者的選擇，不會每次都全部跳回顯示。
const hiddenSeries = {};

// 畫折線圖。`allSeries` 每條曲線要有穩定的 `key`(不能用會隨口數變的 label 當識別)。圖例每一項是按鈕，
// 點一下切換顯示/隱藏；隱藏的曲線不畫、不參與 Y 軸縮放和游標提示，圖例上變淡加刪除線。
// `legendNote` 是圖例後面附帶的一行說明文字(例如舊存檔缺資料的提示)，重畫時要跟著重建，所以放在這裡。
function drawChart(svgId, tooltipId, allSeries, { zeroLine, legendId, legendNote } = {}) {
  const svg = $(svgId);
  const tooltip = $(tooltipId);
  svg.innerHTML = '';
  tooltip.style.display = 'none';
  allSeries = allSeries.filter(s => s.data && s.data.length);
  const hidden = (hiddenSeries[svgId] ??= new Set());
  if (legendId) {
    const legendEl = $(legendId);
    if (legendEl) {
      legendEl.innerHTML = allSeries.map(s => {
        const off = hidden.has(s.key);
        return `<button type="button" class="legend-toggle" data-key="${s.key}" aria-pressed="${!off}"
          title="${off ? '點一下顯示' : '點一下隱藏'}"
          style="display:inline-flex;align-items:center;gap:4px;margin-right:14px;font-size:12px;color:var(--muted);
                 background:none;border:0;padding:2px 0;cursor:pointer;font-family:inherit;
                 opacity:${off ? 0.4 : 1};text-decoration:${off ? 'line-through' : 'none'}">
          <span style="width:10px;height:10px;border-radius:50%;background:${s.color};display:inline-block"></span>${s.label}
        </button>`;
      }).join('') + (legendNote ? `<span style="font-size:12px;color:var(--muted)">${legendNote}</span>` : '');
      legendEl.querySelectorAll('.legend-toggle').forEach(btn => btn.addEventListener('click', () => {
        const key = btn.dataset.key;
        if (hidden.has(key)) hidden.delete(key); else hidden.add(key);
        drawChart(svgId, tooltipId, allSeries, { zeroLine, legendId, legendNote });
      }));
    }
  }
  const seriesList = allSeries.filter(s => !hidden.has(s.key));
  if (!seriesList.length) return;

  const W = 800, H = 260, padL = 56, padR = 12, padT = 14, padB = 24;
  const allPoints = seriesList.flatMap(s => s.data);
  const xs = allPoints.map(p => p.date.getTime());
  const ys = allPoints.map(p => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys, 0), yMax = Math.max(...ys, 0);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const xScale = t => padL + (t - xMin) / (xMax - xMin || 1) * (W - padL - padR);
  const yScale = v => H - padB - (v - yMin) / (yMax - yMin || 1) * (H - padT - padB);

  const ns = 'http://www.w3.org/2000/svg';
  const g = document.createElementNS(ns, 'g');

  [yMax, 0, yMin].forEach(v => {
    const y = yScale(v);
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', padL); line.setAttribute('x2', W - padR);
    line.setAttribute('y1', y); line.setAttribute('y2', y);
    line.setAttribute('stroke', v === 0 && zeroLine ? '#4b5563' : '#2b3040');
    line.setAttribute('stroke-width', '1');
    g.appendChild(line);
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', padL - 6); label.setAttribute('y', y + 4);
    label.setAttribute('text-anchor', 'end'); label.setAttribute('font-size', '10');
    label.setAttribute('fill', '#9ca3af');
    label.textContent = v.toFixed(0);
    g.appendChild(label);
  });

  [{ t: xMin, i: 0 }, { t: xMax, i: 1 }].forEach(({ t, i }) => {
    const label = document.createElementNS(ns, 'text');
    label.setAttribute('x', xScale(t));
    label.setAttribute('y', H - 6);
    label.setAttribute('text-anchor', i === 0 ? 'start' : 'end');
    label.setAttribute('font-size', '10');
    label.setAttribute('fill', '#9ca3af');
    label.textContent = fmtDate(new Date(t));
    g.appendChild(label);
  });

  seriesList.forEach(s => {
    const data = s.data;
    const pathD = data.map((p, i) => `${i === 0 ? 'M' : 'L'}${xScale(p.date.getTime())},${yScale(p.y)}`).join(' ');
    if (s.fillDown) {
      const areaD = `${pathD} L${xScale(data[data.length - 1].date.getTime())},${yScale(0)} L${xScale(data[0].date.getTime())},${yScale(0)} Z`;
      const area = document.createElementNS(ns, 'path');
      area.setAttribute('d', areaD);
      area.setAttribute('fill', s.color);
      area.setAttribute('opacity', '0.12');
      g.appendChild(area);
    }
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', pathD);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', s.color);
    path.setAttribute('stroke-width', '1.6');
    if (s.dashed) path.setAttribute('stroke-dasharray', '5,4');
    g.appendChild(path);
  });

  svg.appendChild(g);

  const markers = seriesList.map(s => {
    const m = document.createElementNS(ns, 'circle');
    m.setAttribute('r', '3.5'); m.setAttribute('fill', s.color); m.setAttribute('display', 'none');
    svg.appendChild(m);
    return m;
  });
  const vline = document.createElementNS(ns, 'line');
  vline.setAttribute('y1', padT); vline.setAttribute('y2', H - padB);
  vline.setAttribute('stroke', '#4b5563'); vline.setAttribute('stroke-width', '1');
  vline.setAttribute('display', 'none');
  svg.appendChild(vline);

  const overlay = document.createElementNS(ns, 'rect');
  overlay.setAttribute('x', padL); overlay.setAttribute('y', padT);
  overlay.setAttribute('width', W - padL - padR); overlay.setAttribute('height', H - padT - padB);
  overlay.setAttribute('fill', 'transparent');
  overlay.addEventListener('mousemove', (evt) => {
    const rect = svg.getBoundingClientRect();
    const svgX = (evt.clientX - rect.left) / rect.width * W;
    const t = xMin + (svgX - padL) / (W - padL - padR) * (xMax - xMin);
    let anyPx = null, anyPy = null;
    const lines = [];
    seriesList.forEach((s, si) => {
      let nearest = s.data[0], best = Infinity;
      for (const p of s.data) {
        const diff = Math.abs(p.date.getTime() - t);
        if (diff < best) { best = diff; nearest = p; }
      }
      const px = xScale(nearest.date.getTime()), py = yScale(nearest.y);
      markers[si].setAttribute('cx', px); markers[si].setAttribute('cy', py); markers[si].setAttribute('display', '');
      lines.push(`${s.label} ${fmtDate(nearest.date)}  ${nearest.y.toFixed(0)}`);
      anyPx = px; anyPy = py;
    });
    vline.setAttribute('x1', anyPx); vline.setAttribute('x2', anyPx); vline.setAttribute('display', '');
    tooltip.style.display = 'block';
    tooltip.style.left = (anyPx / W * rect.width) + 'px';
    tooltip.style.top = (anyPy / H * rect.height) + 'px';
    tooltip.innerHTML = lines.join('<br>');
  });
  overlay.addEventListener('mouseleave', () => {
    markers.forEach(m => m.setAttribute('display', 'none'));
    vline.setAttribute('display', 'none');
    tooltip.style.display = 'none';
  });
  svg.appendChild(overlay);
}
})();
