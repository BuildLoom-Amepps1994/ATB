/* Small, dependency-free primitives shared by the dashboard and offline tests. */
(function (root) {
  'use strict';
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }
  function errorMessage(payload) {
    const detail = payload?.detail ?? payload?.error;
    if (typeof detail === 'string') return detail.slice(0, 500);
    if (Array.isArray(detail)) {
      // Pydantic also returns the submitted input; never echo that to the screen.
      return detail.map(item => typeof item?.msg === 'string' ? item.msg : 'Invalid input').join('; ').slice(0, 500);
    }
    return 'The local service could not complete the request.';
  }
  async function requestJson(path, options = {}) {
    const {method = 'GET', body, token, timeoutMs = 15000, signal, fetchImpl = globalThis.fetch} = options;
    const controller = new AbortController();
    let timedOut = false;
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, {once: true});
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const headers = {'Accept': 'application/json', 'X-CryptoPilot-Token': token};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const response = await fetchImpl('/api' + path, {
        method, headers, signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      let payload;
      try { payload = await response.json(); }
      catch { throw new Error('The local service returned an invalid response.'); }
      if (!response.ok) throw new Error(errorMessage(payload));
      return payload;
    } catch (error) {
      if (timedOut) throw new Error(method === 'GET'
        ? 'The local service timed out. The displayed data may be stale.'
        : 'The request timed out and may have completed. Check the dashboard and activity before retrying.');
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
  function singleFlight(work) {
    let pending = null;
    const run = (...args) => {
      if (!pending) pending = Promise.resolve().then(() => work(...args)).finally(() => { pending = null; });
      return pending;
    };
    run.pending = () => pending;
    return run;
  }
  function latestTask() {
    let sequence = 0, controller = null;
    return async work => {
      const current = ++sequence;
      controller?.abort();
      controller = new AbortController();
      try {
        const value = await work(controller.signal);
        return current === sequence ? {value} : null;
      } catch (error) {
        if (current !== sequence) return null;
        throw error;
      }
    };
  }
  function selectedValue(choices, previous, fallback) {
    return choices.includes(previous) ? previous : choices.includes(fallback) ? fallback : choices[0] ?? '';
  }
  function manualOrderDraft(state, symbol, side, quoteValue) {
    if (!state || state.running || state.stopping || state.control_busy) throw new Error('Stop the bot and wait for other actions to finish before a manual order.');
    if (!['paper', 'live'].includes(state.mode)) throw new Error('Trading mode is unavailable.');
    if (state.mode === 'live' && (!state.connected || state.live_halted)) throw new Error('Connect and explicitly enable live trading before placing a live order.');
    if (state.mode === 'live' && side === 'buy' && state.settings_health?.review_required) throw new Error('Review and save the recovered settings before adding live exposure.');
    if (typeof state.manual_order_context !== 'string' || !/^[0-9a-f-]{36}$/i.test(state.manual_order_context) || !state.exchange) throw new Error('Refresh the dashboard before placing an order.');
    if (!['buy', 'sell'].includes(side)) throw new Error('Choose buy or sell.');
    symbol = String(symbol || '').trim().toUpperCase();
    if (!/^[A-Z0-9]+\/USDT$/.test(symbol)) throw new Error('Choose an active USDT spot pair.');
    if (side === 'sell' && !state.positions.some(position => position.symbol === symbol)) throw new Error('There is no tracked position in this pair and trading mode to sell.');
    const quote = side === 'buy' ? Number(quoteValue) : null;
    if (side === 'buy' && (!Number.isFinite(quote) || quote <= 0 || quote > 1000000)) throw new Error('Enter a positive USDT buy budget, up to 1,000,000. Portfolio limits may allow less.');
    return {symbol, side, quote_amount: quote, confirmation: '', mode: state.mode,
      exchange: state.exchange, expected_context: state.manual_order_context};
  }
  function researchReportHtml(report) {
    if (!report || typeof report !== 'object') return '';
    const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-GB', {maximumFractionDigits: 2}) : '—';
    const date = value => typeof value === 'number' && Number.isFinite(value) && !Number.isNaN(new Date(value).getTime()) ? new Date(value).toISOString().slice(0, 10) : '—';
    const cells = [['Earlier period', report.development], ['Later holdout', report.holdout], ['Full period · double costs', report.double_cost]].map(([label, sample]) => {
      if (!sample?.available) return `<tr><td>${escapeHtml(label)}</td><td colspan="3">${escapeHtml(sample?.reason || 'Insufficient data')}</td></tr>`;
      return `<tr><td>${escapeHtml(label)}<br><small>${date(sample.data_range?.start_ms)} to ${date(sample.data_range?.end_ms)} UTC</small></td><td>${number(sample.return_pct)}%</td><td>${number(sample.max_drawdown_pct)}%</td><td>${number(sample.closed_trades)}<br><small>Trend data: ${number(sample.coverage?.higher_ready_pct)}% ready</small></td></tr>`;
    }).join('');
    const notes = [report.development, report.holdout, report.double_cost].flatMap(sample => Array.isArray(sample?.sample_notes) ? sample.sample_notes : []);
    if (Array.isArray(report.notes)) notes.push(...report.notes);
    return '<h3>Research checks</h3><div class="tablebox"><table><thead><tr><th>Period / scenario</th><th>Return</th><th>Drawdown</th><th>Closed trades</th></tr></thead><tbody>' + cells + '</tbody></table></div>'
      + '<p>Fixed rules · chronological 70/30 split · each period starts with simulated cash. These checks do not establish an independent profitable edge.</p>'
      + [...new Set(notes)].map(note => `<p>${escapeHtml(note)}</p>`).join('');
  }
  // What the update banner says, given /api/update. null hides the banner.
  function updateSummary(status) {
    if (!status || status.disabled) return null;
    const latest = String(status.latest_version || ''), current = String(status.current_version || '');
    if (status.phase === 'downloading') return {text: `Downloading and verifying CryptoPilot ${latest}…`, install: false};
    if (status.phase === 'installing') return {text: `Installing CryptoPilot ${latest}. This window will stop responding; CryptoPilot reopens in a new tab when Setup finishes.`, install: false};
    if (!status.available) return null;
    if (status.can_install) return {text: `CryptoPilot ${latest} is available (you have ${current}). Stop the bot, then install: the app closes, updates and reopens. Portfolio, settings and saved keys are kept.`, install: true};
    return {text: `CryptoPilot ${latest} is available (you have ${current}). Download the installer from the releases page.`, install: false};
  }
  const api = {escapeHtml, errorMessage, requestJson, singleFlight, latestTask, selectedValue, manualOrderDraft, researchReportHtml, updateSummary};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CryptoPilotUI = api;
})(typeof window === 'undefined' ? globalThis : window);
