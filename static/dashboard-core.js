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
    return {text: `CryptoPilot ${latest} is available (you have ${current}). This copy runs from source, so it cannot install updates itself. Download and run the installer from the releases page once; after that, updates install from here.`, install: false};
  }
  // The footer's standing answer to "did the last update check work?", so a result never only flashes by in a toast.
  function updateFooter(status, formatTime = seconds => new Date(seconds * 1000).toLocaleTimeString()) {
    if (!status) return '';
    if (status.disabled) return 'Update checks are turned off';
    if (status.phase === 'downloading' || status.phase === 'installing') return 'Updating…';
    if (status.error) return 'Update check: ' + String(status.error);
    if (status.available) return `Version ${String(status.latest_version || '')} available`;
    if (status.checked_at) return 'Up to date · checked ' + formatTime(status.checked_at);
    return '';
  }
  // What a click on "Check for updates" reports. A check that never ran is not "up to date".
  function updateToast(status) {
    if (!status) return {text: 'Could not check for updates.', error: true};
    if (status.disabled) return {text: 'Update checks are turned off (CRYPTOPILOT_NO_UPDATE_CHECK=1).', error: false};
    if (status.error) return {text: String(status.error), error: true};
    if (status.available) return {text: `CryptoPilot ${String(status.latest_version || '')} is available.`, error: false};
    return {text: 'CryptoPilot is up to date.', error: false};
  }
  // Latched entry stops the operator can clear with the release phrase, from /api/state's risk block.
  function latchedStops(risk) {
    const stops = [];
    if (risk?.drawdown_stop) stops.push({action: 'clear_drawdown_stop', label: 'drawdown stop'});
    if (risk?.realized_stop) stops.push({action: 'clear_realized_stop', label: 'daily realised-loss stop'});
    return stops;
  }
  // A decimal amount from the micro status (a string such as '24.9750'). Never hides a sign or a
  // non-zero amount behind rounding: precision grows up to 8 places, then '<0.00000001' with its sign.
  function microAmount(value, places = 4, signed = false) {
    if (value === null || value === undefined || value === '') return 'unknown';
    const n = Number(value);
    if (!Number.isFinite(n)) return 'unknown';
    let digits = Math.max(0, Math.min(8, places)), text = Math.abs(n).toFixed(digits);
    while (n !== 0 && Number(text) === 0 && digits < 8) text = Math.abs(n).toFixed(++digits);
    const sign = n < 0 ? '-' : signed && n > 0 ? '+' : '';
    return n !== 0 && Number(text) === 0 ? sign + '<0.00000001' : sign + text;
  }
  function microDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === '') return '—';   // unknown, never "0 s"
    const s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) return '—';
    if (s < 90) return Math.round(s) + ' s';
    if (s < 5400) return Math.round(s / 60) + ' min';
    return (s / 3600).toFixed(1) + ' h';
  }
  const MICRO_STATES = {ENTRY_RESERVED: 'Entry reserved', ENTRY_SUBMITTED: 'Entry sent', ENTRY_UNKNOWN: 'Entry outcome unknown · review',
    HOLDING: 'Holding', EXIT_SUBMITTED: 'Exit sent', EXIT_UNKNOWN: 'Exit outcome unknown · review', REVIEW: 'Review required'};
  const MICRO_OWNING = ['HOLDING', 'EXIT_SUBMITTED', 'EXIT_UNKNOWN'];
  // The confirmation for Apply recovery…: it repeats the venue record the recovery will book.
  function microRecoveryPrompt(item) {
    return 'Apply the recovery for ' + String(item?.symbol ?? '') + ' (' + String(item?.state ?? '') + ')? It books exactly what the paper venue record shows'
      + ' (the bot must be stopped): ' + String(item?.explanation || 'no explanation is available; refresh the dashboard first.') + ' Type exactly: I HAVE REVIEWED THIS CYCLE';
  }
  // Everything the micro panel shows, from /api/state's micro block, as plain strings for escaping.
  // null hides the panel (micro not selected and nothing open). Estimates and confirmed figures stay apart.
  function microView(micro) {
    if (!micro || typeof micro !== 'object') return null;
    const cycles = Array.isArray(micro.cycles) ? micro.cycles : [];
    if (!micro.active && !cycles.length) return null;
    if (micro.error) return {headline: 'Micro status unavailable', warnings: [String(micro.error)], budget: [], pairs: [], cycles: [], results: [], counters: [], recovery: [], recoveryNotes: [], holding: 0, latched: false, notes: []};
    const warnings = [];
    if (micro.latch) warnings.push('Micro stop latched: ' + String(micro.latch.reason || 'loss limit') + '. New micro entries are blocked until it is cleared with RESUME TRADING; open cycles keep their exits.');
    const blockers = Array.isArray(micro.review_block) ? micro.review_block : [];
    for (const item of blockers) warnings.push('Review required for ' + String(item.symbol) + ' (' + (MICRO_STATES[item.state] || String(item.state)) + '): ' + String(item.explanation || '') + ' New micro entries are blocked.');
    if (micro.last_error) warnings.push('Latest micro problem: ' + String(micro.last_error));
    if (!micro.active && cycles.length) warnings.push('Micro cycles are open while another strategy is selected; select Micro again to manage their exits.');
    const b = micro.budget || {};
    const budget = [
      ['All-in cap per entry', microAmount(b.cap, 2) + ' USDT (fees included · absolute cap ' + microAmount(b.absolute_cap, 2) + ')'],
      ['Micro allocation', microAmount(b.allocation, 2) + ' USDT over ' + String(b.max_cycles ?? '—') + ' cycle(s)'],
      ['Reserved by entries in flight', microAmount(b.reserved) + ' USDT'],
      ['Basis of open cycles', microAmount(b.committed_basis) + ' USDT'],
    ];
    const openBySymbol = new Map(cycles.map(cycle => [cycle.symbol, cycle]));
    const pairs = (Array.isArray(micro.pairs) ? micro.pairs : []).map(row => {
      const sizing = row.sizing || {}, book = row.book || {}, waiting = row.waiting || {}, decision = row.decision || {};
      const open = openBySymbol.get(row.symbol);
      const eligibility = !row.sizing ? 'Not checked yet'
        : sizing.eligible ? 'Eligible · ' + microAmount(sizing.max_debit) + ' USDT all-in (' + microAmount(sizing.gross_quote) + ' order + ' + microAmount(sizing.fee_reserve) + ' fee reserve)'
          : String(sizing.code || 'Not eligible') + ': ' + String(sizing.detail || '');
      const minimum = sizing.minimum_feasible_budget != null ? microAmount(sizing.minimum_feasible_budget, 2) + ' USDT'
        : sizing.minimum_feasible_detail ? String(sizing.minimum_feasible_detail) : '—';
      const freshness = book.best_bid != null ? String(book.source || 'book') + ' · ' + microDuration(book.age_s) + ' old' + (book.state === 'stale' ? ' (stale)' : '')
          + ' · spread ' + (book.spread_bps != null && Number.isFinite(Number(book.spread_bps)) ? Number(book.spread_bps).toFixed(1) : '—') + ' bps'
        : 'No fresh order book';
      const checks = (Array.isArray(decision.checks) ? decision.checks : []).map(check => (check.passed ? '✓ ' : '✗ ') + String(check.check || '') + (check.detail ? ': ' + String(check.detail) : ''));
      return {symbol: String(row.symbol), selected: row.selected !== false,
        state: open ? (MICRO_STATES[open.state] || String(open.state)) : 'Flat', eligibility, eligible: !!sizing.eligible, minimum, freshness,
        waiting: open ? String(open.last_reason || '') : String(waiting.status || decision.status || 'Waiting for data') + (waiting.waiting_s != null ? ' · for ' + microDuration(waiting.waiting_s) : ''),
        regime: String(row.regime || '—'), error: row.error ? String(row.error) : '', checks};
    });
    const maxBookAge = Number(micro.params?.micro_book_max_age_seconds);
    const cycleRows = cycles.map(cycle => {
      // Only a cycle that owns base has an exit estimate; anything else attached to it is not its own.
      const owning = MICRO_OWNING.includes(cycle.state) && Number(cycle.owned_base) > 0;
      const estimate = owning ? cycle.estimate || {} : {}, verdict = owning ? cycle.exit_verdict || {} : {};
      const shown = estimate.est_net_pnl != null, age = cycle.estimate_age_s;
      const known = age !== null && age !== undefined && age !== '' && Number.isFinite(Number(age));
      const stale = shown && (!micro.running || (known && Number.isFinite(maxBookAge) && Number(age) > maxBookAge));
      const estimateNote = !shown ? '' : stale
        ? 'Stale: estimated ' + (known ? microDuration(age) + ' ago' : 'before the bot stopped') + (!micro.running ? '; the bot is stopped, so it is not re-estimated' : '')
        : known ? 'Estimated ' + microDuration(age) + ' ago' : '';
      return {symbol: String(cycle.symbol), cycle_id: String(cycle.cycle_id), state: MICRO_STATES[cycle.state] || String(cycle.state), strategy: String(cycle.strategy || '—'),
        owned: microAmount(cycle.owned_base, 8), debit: cycle.entry_debit != null ? microAmount(cycle.entry_debit) + ' USDT (simulated fill)' : 'not filled',
        estimate: shown ? microAmount(estimate.est_net_pnl, 4, true) + ' ± ' + microAmount(estimate.uncertainty, 4) + ' USDT (estimated, ' + String(estimate.depth_model || cycle.depth_model || 'model') + ')' : 'No fresh estimate',
        prices: 'entry ' + (cycle.entry_price != null ? microAmount(cycle.entry_price, 2) : '—') + ' · break-even bid ' + (estimate.break_even_bid != null ? microAmount(estimate.break_even_bid, 2) : '—') + ' · target bid ' + (estimate.target_bid != null ? microAmount(estimate.target_bid, 2) : '—'),
        estimate_note: estimateNote, estimate_stale: stale,
        target: microAmount(cycle.target_net, 4, true) + ' / -' + microAmount(cycle.stop_net),
        age: cycle.age_s != null ? microDuration(cycle.age_s) + ' of ' + microDuration(cycle.max_hold_s)
          : 'not booked' + (cycle.pending_s != null ? ' · reserved ' + microDuration(cycle.pending_s) + ' ago' : ''),
        next: cycle.review_reason ? String(cycle.review_reason) : verdict.reason ? String(verdict.reason) : String(cycle.last_reason || ''),
        holding: cycle.state === 'HOLDING'};
    });
    const r = micro.results || {};
    const results = [
      ['Closed cycles', String(r.closed_cycles ?? 0) + ' (' + String(r.wins ?? 0) + ' up · ' + String(r.losses ?? 0) + ' down · ' + String(r.flat ?? 0) + ' flat)'],
      ['Net result (simulated)', microAmount(r.realized_net_total, 4, true) + ' USDT' + (r.realized_unconfirmed_count ? ' · ' + r.realized_unconfirmed_count + ' unconfirmed' : '')],
      ['Average per cycle', r.average_net != null ? microAmount(r.average_net, 4, true) + ' USDT' : '—'],
      ['Worst / best cycle', (r.worst_loss != null ? microAmount(r.worst_loss, 4, true) : '—') + ' / ' + (r.best_gain != null ? microAmount(r.best_gain, 4, true) : '—')],
      ['Maximum drawdown', microAmount(r.max_drawdown) + ' USDT'],
      ['Fees paid', microAmount(r.fees_paid) + ' USDT' + (r.fees_share_of_gross_gains != null ? ' · ' + (Number(r.fees_share_of_gross_gains) * 100).toFixed(1) + '% of gains before fees' : '')],
      ['Turnover', microAmount(r.turnover, 2) + ' USDT'],
      ['Average hold', r.average_hold_s != null ? microDuration(r.average_hold_s) : '—'],
      ['Exits near break-even', String(r.near_break_even_exits ?? 0)],
    ];
    const counters = Object.entries(micro.counters || {}).sort((a, b) => b[1] - a[1]).slice(0, 12).map(([code, count]) => String(code) + ' ' + String(count));
    const triggers = Object.entries(r.exit_triggers || {}).map(([trigger, count]) => String(trigger) + ' ' + String(count));
    const recovery = blockers.map(item => ({cycle_id: String(item.cycle_id), symbol: String(item.symbol), state: MICRO_STATES[item.state] || String(item.state), explanation: String(item.explanation || '')}));
    // What the recovery checks found that has no cycle to apply here (an orphan venue order, a released reservation).
    const blocking = new Set(blockers.map(item => String(item.cycle_id)));
    const recoveryNotes = [...new Set((Array.isArray(micro.recovery) ? micro.recovery : [])
      .filter(item => item && typeof item === 'object' && !(item.cycle_id != null && blocking.has(String(item.cycle_id))))
      .map(item => (item.symbol ? String(item.symbol) + ': ' : '') + String(item.explanation || item.error || 'review required')))];
    const notes = [String(micro.paper_only_reason || 'PAPER only.'), String(r.note || ''), String(micro.heuristic_note || '')].filter(Boolean);
    if (triggers.length) notes.push('Exit triggers: ' + triggers.join(' · '));
    return {headline: !micro.active ? 'Micro cycles open (strategy changed)' : micro.running ? 'Running · PAPER micro cycles' : 'Stopped · Start bot begins PAPER micro cycles for the selected pairs',
      warnings, budget, pairs, cycles: cycleRows, results, counters, recovery, recoveryNotes, notes,
      holding: cycleRows.filter(cycle => cycle.holding).length, latched: !!micro.latch};
  }
  const api = {escapeHtml, errorMessage, requestJson, singleFlight, latestTask, selectedValue, manualOrderDraft, researchReportHtml, updateSummary, updateFooter, updateToast, latchedStops, microAmount, microDuration, microView, microRecoveryPrompt};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CryptoPilotUI = api;
})(typeof window === 'undefined' ? globalThis : window);
