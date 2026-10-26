'use strict';
const $ = id => document.getElementById(id);
const {escapeHtml: esc, requestJson, singleFlight, latestTask, selectedValue, manualOrderDraft, researchReportHtml, updateSummary, updateFooter, updateToast, latchedStops} = CryptoPilotUI;
let state = null, online = false, chartSymbol = 'BTC/USDT', chartTF = '1h', chartData = [], btData = [];
let lastSuccess = 0, pollTimer = null, modalFocus = null;
let orderDraft = null, orderFocus = null;
let researchResult = null;
const busy = new Set();
const chartRequest = latestTask();
const fmt = (n, d = 2) => n == null || !Number.isFinite(Number(n)) ? '—' : Number(n).toLocaleString('en-GB', {maximumFractionDigits: d, minimumFractionDigits: d});
const fprice = n => { n = Number(n); return !Number.isFinite(n) || n <= 0 ? '—' : n < .0001 ? n.toPrecision(5) : n < 1 ? n.toFixed(7) : n.toLocaleString('en-GB', {maximumFractionDigits: 3}); };
const colorVal = n => n > 0 ? 'green' : n < 0 ? 'red' : '';
function toast(message, error = false) {
  const el = $('toast'); el.textContent = message; el.className = 'toast' + (error ? ' error' : ''); el.style.display = 'block';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.style.display = 'none'; }, 6500);
}
function api(path, method = 'GET', body, extra = {}) {
  return requestJson(path, {method, body, token: CSRF, ...extra});
}
function connectionStatus(error) {
  const banner = $('connectionBanner'); banner.hidden = online;
  banner.textContent = online ? '' : 'Connection unavailable. Displayed values are stale' + (lastSuccess ? ' (last update ' + new Date(lastSuccess).toLocaleTimeString() + ')' : '') + '. ' + (error || 'Connecting to the local service…');
  $('serviceStatus').textContent = online ? 'Updated ' + new Date(lastSuccess).toLocaleTimeString() : 'Connection unavailable';
  updateControls();
}
function updateControls() {
  const s = state, idle = !!s && online && !s.running && !s.stopping && !s.control_busy, pending = busy.size > 0;
  for (const id of ['startBtn', 'scanBtn', 'saveBtn', 'paperBtn', 'liveBtn', 'resetBtn', 'connectBtn', 'disconnectBtn', 'backtestBtn']) $(id).disabled = !s || !online || pending;
  if (!s) { $('stopBtn').disabled = false; return; }
  $('startBtn').disabled ||= !idle || (s.mode === 'live' && s.live_halted);
  $('scanBtn').disabled ||= !idle;
  $('backtestBtn').disabled ||= !idle;
  for (const id of ['saveBtn', 'paperBtn', 'liveBtn', 'resetBtn', 'connectBtn', 'disconnectBtn']) $(id).disabled ||= !idle;
  const settingsReview = !!s.settings_health?.review_required;
  $('liveBtn').disabled ||= s.live_halted || settingsReview;
  $('startBtn').disabled ||= s.mode === 'live' && settingsReview;
  $('resetBtn').disabled ||= s.mode !== 'paper';
  $('connectBtn').disabled ||= s.mode === 'live'; $('disconnectBtn').disabled ||= s.mode === 'live';
  $('exchange').disabled = !idle || s.mode === 'live' || pending;
  // A stale connection must not disable the user's ability to retry a stop.
  $('stopBtn').disabled = busy.has('stop') || (online && !s.running && !s.stopping && !s.control_busy && !busy.has('order'));
  // The kill switch is always available while the service answers; it never waits for an idle engine.
  const engaged = !!s.risk?.kill_switch?.engaged;
  $('killBtn').disabled = !online || busy.has('risk');
  $('killBtn').textContent = engaged ? '⛔ Release kill switch…' : '⛔ Kill switch';
  $('killBtn').classList.toggle('selected', engaged);
  $('clearStopBtn').hidden = !latchedStops(s.risk).length;
  $('clearStopBtn').disabled = !online || busy.has('risk');
  $('confirmLive').disabled = !idle || pending || settingsReview || !$('eligible').checked || $('liveConfirm').value !== 'ENABLE LIVE TRADING';
  const orderReady = idle && !pending && !(s.mode === 'live' && (!s.connected || s.live_halted));
  $('orderBuy').disabled = !orderReady || (s.mode === 'live' && settingsReview);
  $('orderSell').disabled = !orderReady || !s.positions.some(position => position.symbol === $('orderSymbol').value);
  $('orderSymbol').disabled = !idle || pending;
  $('orderBudget').disabled = !idle || pending;
  const contextChanged = !!orderDraft && (s.mode !== orderDraft.mode || s.exchange !== orderDraft.exchange || s.manual_order_context !== orderDraft.expected_context || (settingsReview && orderDraft.side === 'buy'));
  $('orderContextChanged').hidden = !contextChanged;
  $('confirmOrder').disabled = !orderReady || !orderDraft || contextChanged || $('orderConfirm').value !== 'PLACE LIVE ' + orderDraft.side.toUpperCase();
}
async function action(name, path, data, message, configRefresh = false) {
  if (busy.has(name)) return;
  busy.add(name); updateControls();
  let accepted = false;
  try {
    await api(path, 'POST', data, {timeoutMs: name === 'scan' ? 120000 : 30000});
    accepted = true;
    if (message) toast(message);
  } catch (error) { toast(error.message, true); }
  finally {
    // A timed-out mutation can have committed. Always re-read state and never retry it automatically.
    // A rejected settings save keeps the typed values on screen for correction.
    await refresh(configRefresh && accepted, true); busy.delete(name); updateControls();
  }
}
function feedNote(f) {
  if (!f || f.state === 'off') return f && f.enabled ? ' Streaming quotes start with the bot.' : '';
  if (f.state === 'live') return ' Streaming quotes live (' + f.quoted + ' of ' + f.symbols + ' pairs, ' + f.messages + ' updates' + (f.reconnects ? ', ' + f.reconnects + ' reconnects' : '') + ').';
  return ' Streaming quotes ' + f.state + (f.last_error ? ' (' + f.last_error + ')' : '') + '; REST polling in use.';
}

function readSettings() {
  return {exchange: $('exchange').value, watchlist: $('symbols').value.split(/[\s,;]+/).map(x => x.trim().toUpperCase()).filter(Boolean), auto_discovery: $('autoDiscovery').checked, market_filters: $('marketFilters').checked, streaming_quotes: $('streamingQuotes').checked, time_horizon: $('timeHorizon').value, scan_seconds: +$('scanSeconds').value, protective_poll_seconds: +$('protectiveSeconds').value, risk_pct: +$('riskPct').value, max_position_pct: +$('maxPositionPct').value, max_exposure_pct: +$('maxExposurePct').value, max_portfolio_risk_pct: +$('maxPortfolioRisk').value, data_max_age_seconds: +$('dataMaxAge').value, max_positions: +$('maxPositions').value, max_meme_position_pct: +$('maxMemePosition').value, max_meme_exposure_pct: +$('maxMemeExposure').value, daily_loss_limit_pct: +$('dailyLossPct').value, min_quote_volume: +$('minVolume').value, max_order_notional_usdt: +$('maxOrderNotional').value, max_asset_notional_usdt: +$('maxAssetNotional').value, max_orders_per_minute: +$('maxOrdersPerMinute').value, daily_realized_loss_limit_pct: +$('dailyRealizedLoss').value, max_drawdown_pct: +$('maxDrawdown').value};
}
function syncExchangeGuide() { $('apiGuide').href = $('exchange').value === 'mexc' ? 'https://www.mexc.com/user/openapi' : 'https://www.kraken.com/u/security/api'; }
function fillSettings(c) {
  const fields = {exchange: 'exchange', time_horizon: 'timeHorizon', scan_seconds: 'scanSeconds', protective_poll_seconds: 'protectiveSeconds', risk_pct: 'riskPct', max_position_pct: 'maxPositionPct', max_exposure_pct: 'maxExposurePct', max_portfolio_risk_pct: 'maxPortfolioRisk', data_max_age_seconds: 'dataMaxAge', max_positions: 'maxPositions', max_meme_position_pct: 'maxMemePosition', max_meme_exposure_pct: 'maxMemeExposure', daily_loss_limit_pct: 'dailyLossPct', min_quote_volume: 'minVolume', max_order_notional_usdt: 'maxOrderNotional', max_asset_notional_usdt: 'maxAssetNotional', max_orders_per_minute: 'maxOrdersPerMinute', daily_realized_loss_limit_pct: 'dailyRealizedLoss', max_drawdown_pct: 'maxDrawdown'};
  for (const [key, id] of Object.entries(fields)) $(id).value = c[key];
  $('symbols').value = c.watchlist.join(', '); $('autoDiscovery').checked = c.auto_discovery; $('marketFilters').checked = c.market_filters !== false; $('streamingQuotes').checked = c.streaming_quotes === true; syncExchangeGuide();
}
function render(s) {
  $('settingsRecovery').hidden = !s.settings_health?.review_required;
  $('settingsRecoveryText').textContent = s.settings_health?.review_required
    ? (s.settings_health.using_last_known_good ? 'Saved settings were damaged. The last validated limits are shown. ' : 'Saved settings need review. ') + 'Live activation is blocked. Review every displayed risk limit and Save settings to approve them.' : '';
  $('runDot').className = 'dot' + (s.running || s.manual_order_active ? ' on' : ''); $('runText').textContent = s.stopping ? 'Stopping…' : s.manual_order_active ? 'Order in progress…' : s.running ? 'Running' : 'Stopped';
  $('modeBadge').className = 'pill' + (s.mode === 'live' ? ' live' : ''); $('modeBadge').textContent = s.mode.toUpperCase() + ' MODE';
  $('equity').textContent = s.equity == null ? 'Unavailable' : fmt(s.equity); $('cash').textContent = s.cash == null ? 'Unavailable' : fmt(s.cash);
  $('equityCaption').textContent = s.valuation_stale ? 'Stale valuation · last quote or entry estimate' : 'USDT · bid valuation after estimated exit costs + cash';
  $('equityCaption').className = 'caption' + (s.valuation_stale ? ' amber' : '');
  $('cashCaption').textContent = s.mode === 'paper' ? 'USDT · simulated cash' : s.balance_updated_at ? 'Cached balance · updated ' + new Date(s.balance_updated_at * 1000).toLocaleTimeString() : 'Exchange balance unavailable';
  $('realized').textContent = (s.realized >= 0 ? '+' : '') + fmt(s.realized); $('realized').className = 'number ' + colorVal(s.realized); $('positionCount').textContent = s.positions.length;
  $('lastScan').textContent = s.last_scan ? 'Last scan: ' + new Date(s.last_scan).toLocaleTimeString() : 'Last scan: not yet';
  $('monitorStatus').textContent = !s.running ? 'Automatic stops are inactive while the bot is stopped.' : !s.positions.length ? 'No tracked positions need exit monitoring.'
    : 'Exit quote check target: every ' + (s.config.protective_poll_seconds || 5) + ' seconds. '
      + (s.last_successful_monitor ? 'Latest successful quote: ' + new Date(s.last_successful_monitor).toLocaleTimeString() + '. ' : 'Waiting for a successful held-position quote. ')
      + 'Network requests and exchange limits can delay checks.' + feedNote(s.feed);
  $('connectStatus').textContent = s.connected ? '● Verified (' + s.credential_source + ')' : s.credentials_stored ? '◌ Saved keys · not yet verified' : '○ Not connected';
  $('connectStatus').className = 'sub ' + (s.connected ? 'green' : '');
  const unresolved = Array.isArray(s.unresolved_orders) ? s.unresolved_orders.length : Number(s.unresolved_orders || 0);
  const legacy = Array.isArray(s.legacy_live_records) ? s.legacy_live_records.length > 0 : !!s.legacy_live_records;
  const warnings = [s.risk?.kill_switch?.engaged ? 'KILL SWITCH ENGAGED: new orders are blocked' + (s.risk.kill_switch.allow_exits ? '; protective exits continue.' : ' and exits are blocked too.') + ' Release it from the kill switch button when the situation is understood.' : '', s.risk?.drawdown_stop ? 'Drawdown stop latched: new entries are blocked until it is cleared from the Clear latched stop button with the phrase RESUME TRADING.' : '', s.risk?.realized_stop ? 'Daily realised-loss stop latched: new entries are blocked until it is cleared from the Clear latched stop button with the phrase RESUME TRADING. After clearing, a further loss the same day latches it again.' : '', s.live_halted ? 'Live trading halted. Review exchange orders and holdings before enabling live trading. Paper simulation remains available.' : '', s.valuation_stale ? 'Portfolio valuation uses stale or unavailable quotes; current exposure may differ.' : '', s.positions.length && !s.running ? 'Automatic exits are inactive while the bot is stopped. Start the bot to monitor software stops.' : '', s.monitor_error ? 'Exit quote monitoring reported a problem; review activity and exchange holdings.' : '', s.risk_block ? String(s.risk_block) : '', unresolved ? unresolved + ' unresolved order(s) require reconciliation.' : '', legacy ? 'Previous live records need manual exchange reconciliation.' : '', s.error ? 'Latest scan reported a problem; review activity.' : ''].filter(Boolean);
  $('riskStatus').hidden = warnings.length === 0; $('riskStatus').textContent = warnings.join(' ');
  $('modeNotice').textContent = s.mode === 'paper' ? 'PAPER simulation enabled. No real buy or sell orders will be sent.' + (s.live_halted ? ' Live trading remains halted pending manual reconciliation.' : '') : s.live_halted ? 'LIVE HALTED: Inspect exchange orders and holdings manually before enabling live trading.' : 'LIVE mode is armed. Start bot submits REAL SPOT orders automatically. Manual orders require a separate confirmation. The bot must be running for software stops.';
  $('orderMode').textContent = s.mode === 'live' ? 'REAL MONEY · live armed' : 'PAPER · simulated orders';
  $('orderMode').className = 'sub ' + (s.mode === 'live' ? 'red' : '');
  $('modeState').textContent = s.mode === 'live' ? 'REAL MONEY' : 'Simulation active'; $('paperBtn').className = s.mode === 'paper' ? 'selected' : ''; $('liveBtn').className = 'danger ' + (s.mode === 'live' ? 'selected' : '');
  $('marketCount').textContent = s.markets.length ? '· ' + s.markets.length + ' scanned' : '';
  $('markets').innerHTML = s.markets.length ? s.markets.map(m => `<tr><td><button class="ticker" data-choose="${esc(m.symbol)}">${esc(m.symbol)}</button></td><td><span class="status">${esc(m.kind || '—')}</span></td><td>${fprice(m.price)}</td><td><b class="${m.candidate ? 'green' : ''}">${esc(m.score ?? 0)}/100</b></td><td>${esc(m.rsi ?? '—')}</td><td>${m.atr_pct != null ? fmt(m.atr_pct) + '%' : '—'}</td><td>${m.volume != null ? fmt(m.volume, 0) : '—'}</td><td><span class="status ${m.candidate ? 'buy' : String(m.status).startsWith('Scan') ? 'error' : ''}">${esc(m.status)}</span></td></tr>`).join('') : '<tr><td colspan="8" class="empty">Not scanned yet. Click Scan now.</td></tr>';
  $('positions').innerHTML = s.positions.length ? s.positions.map(p => `<tr><td><strong>${esc(p.symbol)}</strong>${p.dust ? '<br><span class="amber">Below exchange minimum</span>' : ''}</td><td>${fmt(p.qty, 7)}</td><td>${fprice(p.entry_price)}</td><td>${fprice(p.mark)}${p.mark_stale || p.mark == null || (p.price_age_seconds != null && p.price_age_seconds > (s.config.data_max_age_seconds || 120)) ? '<br><span class="amber">Quote stale</span>' : ''}</td><td class="red">${fprice(p.stop)}</td><td class="green">${fprice(p.target)}</td><td class="${colorVal(p.unrealized)}">${p.unrealized != null && p.unrealized >= 0 ? '+' : ''}${fmt(p.unrealized)}</td></tr>`).join('') : '<tr><td colspan="7" class="empty">No open positions in this mode.</td></tr>';
  $('trades').innerHTML = s.trades.length ? s.trades.map(t => `<tr><td>${esc(new Date(t.created_at).toLocaleString('en-GB', {timeZone: 'UTC'}))}</td><td>${esc(t.symbol)}</td><td class="${t.side === 'BUY' ? 'green' : 'amber'}">${esc(t.side)}</td><td>${fmt(t.quantity, 7)}</td><td>${fprice(t.price)}</td><td class="${colorVal(t.realized)}">${t.realized == null ? '—' : fmt(t.realized)}</td><td>${esc(t.reason || '')}</td></tr>`).join('') : '<tr><td colspan="7" class="empty">No trades in this mode yet.</td></tr>';
  $('activity').innerHTML = s.audit.length ? s.audit.map(a => `<li class="${['error', 'trade', 'warning'].includes(a.level) ? a.level : ''}"><time>${esc(new Date(a.created_at).toLocaleTimeString())}</time><span>${esc(a.message)}</span></li>`).join('') : '<li><span>No activity recorded.</span></li>';
  const choices = [...new Set([...s.config.watchlist, ...s.markets.map(x => x.symbol), ...s.positions.map(x => x.symbol)])];
  for (const [id, fallback] of [['btSymbol', chartSymbol], ['chartPair', chartSymbol], ['orderSymbol', chartSymbol]]) {
    const value = selectedValue(choices, $(id).value, fallback);
    // Preserve the actual option nodes while a user navigates a live dropdown.
    const existing = [...$(id).options].map(option => option.value);
    if (existing.length !== choices.length || existing.some((item, index) => item !== choices[index])) {
      $(id).innerHTML = choices.map(sym => `<option value="${esc(sym)}">${esc(sym)}</option>`).join('');
    }
    $(id).value = value;
  }
  updateControls();
}
const refreshState = singleFlight(async (fill = false) => {
  try {
    const s = await api('/state'); const first = !state; const changed = first || s.config.exchange !== state.config.exchange;
    const previousScan = state?.last_scan; state = s; online = true; lastSuccess = Date.now(); render(s); connectionStatus();
    if (first || fill || changed) fillSettings(s.config);
    if (first || changed || ![...$('chartPair').options].some(o => o.value === chartSymbol)) selectChart(s.config.watchlist[0] || 'BTC/USDT');
    else if (s.last_scan !== previousScan) selectChart(chartSymbol);
    return true;
  } catch (error) { online = false; connectionStatus(error.message); return false; }
});
async function refresh(fill = false, force = false) {
  const pending = refreshState.pending();
  if (force && pending) await pending;
  return refreshState(fill);
}
async function poll() {
  await refresh(); clearTimeout(pollTimer); pollTimer = setTimeout(poll, document.hidden ? 30000 : state?.mode === 'live' || state?.running ? 2000 : 10000);
}
let chartLoaded = null;
async function selectChart(symbol) {
  chartSymbol = symbol; $('chartSymbol').textContent = symbol; $('chartPair').value = symbol;
  // A refresh of the same pair keeps the last frame on screen until the new one arrives;
  // a different selection must never show another pair's candles.
  if (!chartLoaded || chartLoaded.symbol !== symbol || chartLoaded.tf !== chartTF) { chartData = []; chartLoaded = null; draw(); }
  $('chartStatus').textContent = 'Loading completed candles…';
  try {
    const result = await chartRequest(signal => api('/history?symbol=' + encodeURIComponent(symbol) + '&timeframe=' + chartTF, 'GET', undefined, {signal, timeoutMs: 45000}));
    if (!result) return;
    chartData = result.value.candles; chartLoaded = {symbol, tf: chartTF};
    $('chartStatus').textContent = chartData.length + ' completed candles · updated ' + new Date().toLocaleTimeString(); draw();
  } catch (error) { $('chartStatus').textContent = 'Chart unavailable: ' + error.message + (chartData.length ? ' · showing the previous frame' : ''); }
}
function canvasSetup(canvas) {
  const dpr = Math.min(2, window.devicePixelRatio || 1), box = canvas.getBoundingClientRect(); canvas.width = Math.round(box.width * dpr); canvas.height = Math.round(box.height * dpr);
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr); return {ctx, w: box.width, h: box.height};
}
function linePlot(canvas, series, keys, colors, format) {
  const {ctx, w, h} = canvasSetup(canvas); ctx.clearRect(0, 0, w, h); ctx.fillStyle = '#0c1926'; ctx.fillRect(0, 0, w, h);
  const values = series.flatMap(row => keys.map(k => row[k]).filter(v => v != null && Number.isFinite(Number(v))).map(Number));
  if (series.length < 2 || !values.length || w < 80) { ctx.fillStyle = '#8098aa'; ctx.fillText('No data loaded', 16, 25); return; }
  const l = 64, r = 14, t = 16, b = 27; let lo = Math.min(...values), hi = Math.max(...values); const pad = (hi - lo) * .1 || Math.max(.00001, hi * .01); lo -= pad; hi += pad;
  const py = v => t + (hi - v) / (hi - lo) * (h - t - b), px = i => l + i / (series.length - 1) * (w - l - r); ctx.font = '11px Segoe UI';
  for (let i = 0; i <= 4; i++) { const y = t + i / 4 * (h - t - b); ctx.strokeStyle = '#243448'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(l, y); ctx.lineTo(w - r, y); ctx.stroke(); ctx.fillStyle = '#8da0b4'; ctx.fillText(format(hi - (hi - lo) * i / 4), 4, y + 4); }
  keys.forEach((key, j) => {
    ctx.beginPath(); ctx.strokeStyle = colors[j]; ctx.lineWidth = j === 0 ? 2 : 1.5; ctx.globalAlpha = j === 0 ? 1 : .78; let started = false;
    series.forEach((row, i) => { const value = row[key]; if (value == null || !Number.isFinite(Number(value))) { started = false; return; } const y = py(Number(value)); if (!started) ctx.moveTo(px(i), y); else ctx.lineTo(px(i), y); started = true; }); ctx.stroke(); ctx.globalAlpha = 1;
  });
  for (const fraction of [0, .33, .66, 1]) { const i = Math.round(fraction * (series.length - 1)), date = new Date(series[i].t ?? series[i].time); ctx.fillStyle = '#8399ad'; ctx.fillText(date.toLocaleDateString('en-GB', {day: '2-digit', month: 'short'}), Math.max(l, px(i) - 20), h - 6); }
}
function draw() { linePlot($('priceChart'), chartData, ['close', 'ema20', 'ema50'], ['#49dfc1', '#69abff', '#e7b96f'], fprice); linePlot($('btChart'), btData, ['equity'], ['#49dfc1'], n => fmt(n, 0)); }
function closeOrderModal() {
  $('orderModal').classList.remove('show'); $('orderConfirm').value = ''; orderDraft = null; orderFocus?.focus();
}
async function submitOrder(draft, confirmation = '') {
  if (busy.size || !online || state?.mode !== draft.mode || state?.exchange !== draft.exchange || state?.manual_order_context !== draft.expected_context) { toast('Trading state changed. Review the order again.', true); return; }
  busy.add('order'); updateControls();
  $('orderResult').textContent = 'Submitting one ' + draft.mode.toUpperCase() + ' ' + draft.side.toUpperCase() + ' order. Waiting for a confirmed result…';
  try {
    const {mode, exchange, ...payload} = draft;
    const result = await api('/order', 'POST', {...payload, confirmation}, {timeoutMs: 90000});
    if (!['filled', 'partial'].includes(result.status)) throw new Error('Order result is not confirmed. Review activity and exchange order history before submitting again.');
    $('orderResult').textContent = result.status === 'partial'
      ? result.mode.toUpperCase() + ' ' + result.side.toUpperCase() + ' partially filled for ' + result.symbol + '. Remaining tracked quantity: ' + fmt(result.remaining_quantity, 8) + '. Review order history before another order.'
      : result.mode.toUpperCase() + ' ' + result.side.toUpperCase() + ' confirmed for ' + result.symbol + '. Review the actual fill in order history.';
    toast(result.status === 'partial' ? 'Partial fill confirmed. A tracked position remains.' : 'Order fill confirmed.');
  } catch (error) { $('orderResult').textContent = error.message; toast(error.message, true); }
  finally {
    // Keep the submitted request ID, never resend automatically after a timeout.
    await refresh(false, true); busy.delete('order'); updateControls();
  }
}
function prepareOrder(side) {
  try {
    if (!online || busy.size) throw new Error('Wait for the local service and other actions to finish.');
    const draft = {...manualOrderDraft(state, $('orderSymbol').value, side, $('orderBudget').value), request_id: crypto.randomUUID()};
    if (draft.mode === 'paper') { submitOrder(draft); return; }
    orderFocus = document.activeElement; orderDraft = draft;
    $('orderSummary').textContent = side === 'buy'
      ? 'On ' + draft.exchange.toUpperCase() + ', buy ' + draft.symbol + ' using a total budget of ' + fmt(draft.quote_amount) + ' USDT including estimated fees. The market fill price and quantity can change.'
      : 'On ' + draft.exchange.toUpperCase() + ', sell the entire bot-managed ' + draft.symbol + ' position in this exchange account. The market fill price can change; unrelated holdings are excluded.';
    $('orderPhrase').textContent = 'Type exactly: PLACE LIVE ' + side.toUpperCase();
    $('orderConfirm').value = ''; $('orderModal').classList.add('show'); updateControls(); $('orderConfirm').focus();
  } catch (error) { toast(error.message, true); }
}
function trapModal(event, element, close) {
  if (event.key === 'Escape') close();
  if (event.key === 'Tab') {
    const items = [...element.querySelectorAll('input,button')].filter(el => !el.disabled), first = items[0], last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
}
function closeModal() { $('liveModal').classList.remove('show'); $('liveConfirm').value = ''; $('eligible').checked = false; modalFocus?.focus(); }
$('orderBuy').onclick = () => prepareOrder('buy'); $('orderSell').onclick = () => prepareOrder('sell');
$('orderSymbol').onchange = updateControls; $('orderConfirm').oninput = updateControls;
$('cancelOrder').onclick = closeOrderModal;
$('confirmOrder').onclick = () => {
  const draft = orderDraft, confirmation = $('orderConfirm').value;
  if (!draft || confirmation !== 'PLACE LIVE ' + draft.side.toUpperCase()) return;
  closeOrderModal(); submitOrder(draft, confirmation);
};
$('orderModal').addEventListener('keydown', event => trapModal(event, $('orderModal'), closeOrderModal));
$('scanBtn').onclick = () => action('scan', '/scan'); $('startBtn').onclick = () => action('start', '/start'); $('stopBtn').onclick = () => action('stop', '/stop', undefined, 'Stop requested. Check running status before closing the program.');
$('saveBtn').onclick = () => action('config', '/config', {settings: readSettings()}, 'Settings saved.', true);
$('killBtn').onclick = () => {
  const engaged = !!state?.risk?.kill_switch?.engaged;
  if (!engaged) {
    const choice = prompt('Engage the kill switch now? New orders stop immediately, even while the bot runs.\n\nPress OK to keep protective exits (stops and targets) running, or type ALL to block exits as well. Cancel aborts.', '');
    if (choice === null) return;
    const allowExits = choice.trim().toUpperCase() !== 'ALL';
    action('risk', '/risk', {action: 'engage', allow_exits: allowExits, reason: 'Dashboard kill switch'}, allowExits ? 'Kill switch engaged. New orders are blocked; exits continue.' : 'Kill switch engaged. All orders, including exits, are blocked.');
    return;
  }
  const phrase = prompt('Release the kill switch? Type exactly: RESUME TRADING');
  if (phrase === null) return;
  action('risk', '/risk', {action: 'release', confirmation: phrase}, 'Kill switch released.');
};
$('clearStopBtn').onclick = async () => {
  const stops = latchedStops(state?.risk);
  if (!stops.length) return;
  const names = stops.map(stop => stop.label).join(' and the ');
  const phrase = prompt(`Clear the ${names}? New entries can resume. Review the day's losses first. Type exactly: RESUME TRADING`);
  if (phrase === null) return;
  for (const stop of stops) await action('risk', '/risk', {action: stop.action, confirmation: phrase}, `Cleared the ${stop.label}.`);
};
$('connectBtn').onclick = async () => {
  if (busy.has('connect')) return;
  const key = $('apiKey').value.trim(), secret = $('apiSecret').value.trim();
  try {
    if (!key || !secret) throw new Error('Enter both API key and secret.');
    if ($('exchange').value !== state?.config.exchange) throw new Error('Save the exchange selection before connecting.');
    $('apiKey').value = ''; $('apiSecret').value = '';
    await action('connect', '/connect', {api_key: key, api_secret: secret, remember: $('remember').checked}, 'Exchange account verified.');
  } catch (error) { toast(error.message, true); }
  finally { $('apiKey').value = ''; $('apiSecret').value = ''; }
};
$('disconnectBtn').onclick = () => { if (confirm('Disconnect and delete keys saved by this application?')) action('disconnect', '/disconnect'); };
$('paperBtn').onclick = () => action('mode', '/mode', {mode: 'paper'});
$('liveBtn').onclick = () => { modalFocus = document.activeElement; $('liveConfirm').value = ''; $('eligible').checked = false; $('liveModal').classList.add('show'); updateControls(); $('eligible').focus(); };
$('cancelLive').onclick = closeModal;
$('eligible').onchange = updateControls; $('liveConfirm').oninput = updateControls;
$('confirmLive').onclick = async () => { await action('mode', '/mode', {mode: 'live', confirmation: $('liveConfirm').value, eligible: $('eligible').checked}); if (state?.mode === 'live') { closeModal(); toast('LIVE mode armed. Start bot can place real orders.'); } };
$('liveModal').addEventListener('keydown', event => trapModal(event, $('liveModal'), closeModal));
$('resetBtn').onclick = () => { if (confirm('Delete paper trades and reset the simulated balance?')) action('reset', '/reset-paper'); };
$('exchange').onchange = syncExchangeGuide; $('chartPair').onchange = () => selectChart($('chartPair').value);
$('markets').addEventListener('click', event => { const button = event.target.closest('[data-choose]'); if (button) selectChart(button.dataset.choose); });
document.querySelectorAll('[data-tf]').forEach(button => { button.onclick = () => { chartTF = button.dataset.tf; document.querySelectorAll('[data-tf]').forEach(el => el.classList.toggle('selected', el === button)); selectChart(chartSymbol); }; });
$('backtestBtn').onclick = async () => {
  if (busy.has('backtest')) return;
  busy.add('backtest'); updateControls(); btData = []; researchResult = null; $('downloadResearch').disabled = true; $('researchDiagnostics').hidden = true; draw(); $('backtestResults').textContent = 'Computing historical results and research checks…';
  const symbol = $('btSymbol').value, timeframe = $('btTf').value;
  try {
    const res = await api('/backtest', 'POST', {symbol, timeframe}, {timeoutMs: 120000}); btData = res.equity_curve;
    researchResult = {product: 'CryptoPilot', report_version: 1, symbol, timeframe, generated_at: new Date().toISOString(), results: res};
    $('downloadResearch').disabled = false;
    $('researchDiagnostics').innerHTML = researchReportHtml(res.research_report);
    $('researchDiagnostics').hidden = !res.research_report;
    const metrics = [['Strategy return', fmt(res.return_pct) + '%'], ['Buy & hold (net costs)', fmt(res.buy_hold_pct) + '%'], ['Max drawdown', fmt(res.max_drawdown_pct) + '%'], ['Closed trades', String(res.closed_trades)], ['Win rate', fmt(res.win_rate_pct, 1) + '%'], ['Ending USDT', fmt(res.ending_equity)]];
    const range = res.data_range ? ' · ' + new Date(res.data_range.start_ms).toLocaleDateString('en-GB') + ' to ' + new Date(res.data_range.end_ms).toLocaleDateString('en-GB') : '';
    $('backtestResults').innerHTML = `<p class="micro">${esc(symbol)} · ${esc(timeframe)}${esc(range)}</p><div class="bt-metrics">${metrics.map(([label, value]) => `<div><small>${esc(label)}</small><b>${esc(value)}</b></div>`).join('')}</div><p class="micro">${esc(res.warning)}</p>`; draw();
  } catch (error) { $('backtestResults').textContent = 'Backtest unavailable: ' + error.message; }
  finally { busy.delete('backtest'); updateControls(); }
};
$('reviewSettings').onclick = () => { $('riskSettings').scrollIntoView({behavior: 'smooth', block: 'start'}); $('riskPct').focus({preventScroll: true}); };
$('downloadResearch').onclick = () => {
  if (!researchResult) return;
  const blob = new Blob([JSON.stringify(researchResult, null, 2) + '\n'], {type: 'application/json'});
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'CryptoPilot-research-' + researchResult.symbol.replace(/[^A-Za-z0-9_-]/g, '-') + '-' + researchResult.timeframe + '.json';
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
let updateStatus = null;
function showUpdate(status) {
  updateStatus = status;
  $('appVersion').textContent = 'CryptoPilot ' + (status?.current_version || '');
  const footer = updateFooter(status);
  $('updateState').textContent = footer ? ' · ' + footer : '';
  const summary = updateSummary(status);
  $('updateBanner').hidden = !summary;
  if (!summary) return;
  $('updateText').textContent = summary.text;
  $('installUpdate').hidden = !summary.install;
  $('updateNotes').href = status.release_page;
}
async function checkUpdates(force) {
  try {
    const status = await api(force ? '/update/check' : '/update', force ? 'POST' : 'GET', force ? {force: true} : undefined, {timeoutMs: 30000});
    showUpdate(status);
    if (force) { const result = updateToast(status); toast(result.text, result.error); }
  } catch (error) { if (force) toast(error.message, true); }
}
$('checkUpdates').onclick = () => checkUpdates(true);
$('installUpdate').onclick = async () => {
  if (busy.has('update')) return;
  if (state?.running || state?.manual_order_active) { toast('Stop the bot and wait for any order to finish before updating.', true); return; }
  if (!confirm('Download and install CryptoPilot ' + updateStatus.latest_version + '? CryptoPilot will close, update and reopen. Automatic stops are inactive until it is running again.')) return;
  busy.add('update'); $('installUpdate').disabled = true;
  showUpdate({...updateStatus, phase: 'downloading'});
  try {
    await api('/update/install', 'POST', undefined, {timeoutMs: 600000});
    showUpdate({...updateStatus, phase: 'installing'});
  } catch (error) { toast(error.message, true); showUpdate({...updateStatus, phase: 'idle'}); }
  finally { busy.delete('update'); $('installUpdate').disabled = false; }
};
window.addEventListener('resize', draw);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(pollTimer); poll(); } });
connectionStatus(); draw(); poll(); checkUpdates(false);
// The server caches the release check for an hour, so this asks GitHub at most hourly.
setInterval(() => { if (!busy.has('update')) checkUpdates(false); }, 3600000);


