'use strict';
// Run with Node 22+ (24 LTS recommended): node --test tests/test_dashboard.js. No exchange/network access.
const test = require('node:test');
const assert = require('node:assert/strict');
const {escapeHtml, errorMessage, requestJson, singleFlight, latestTask, selectedValue, manualOrderDraft, researchReportHtml, updateSummary, updateFooter, updateToast, microAmount, microView} = require('../static/dashboard-core.js');

test('untrusted server strings and validation details are safe for display', () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)">\''), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;');
  const message = errorMessage({detail: [{msg: 'Too short', input: 'EXAMPLE-SECRET', ctx: {secret: 'EXAMPLE-SECRET'}}]});
  assert.equal(message, 'Too short');
  assert.ok(!message.includes('SECRET'));
});

test('requests carry action token and deliberately disable browser caching', async () => {
  let called;
  const result = await requestJson('/mode', {method: 'POST', token: 'local-token', body: {mode: 'paper'}, fetchImpl: async (...args) => {
    called = args; return {ok: true, json: async () => ({ok: true})};
  }});
  assert.deepEqual(result, {ok: true});
  assert.equal(called[0], '/api/mode');
  assert.equal(called[1].headers['X-CryptoPilot-Token'], 'local-token');
  assert.equal(called[1].cache, 'no-store');
  assert.equal(called[1].body, '{"mode":"paper"}');
});

test('a hung mutation times out without automatically retrying or claiming failure', async () => {
  let calls = 0;
  await assert.rejects(requestJson('/start', {method: 'POST', timeoutMs: 5, fetchImpl: (_url, options) => {
    calls++;
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true}));
  }}), /may have completed/);
  assert.equal(calls, 1);
});

test('invalid JSON and validation failures yield useful bounded messages', async () => {
  await assert.rejects(requestJson('/state', {fetchImpl: async () => ({ok: true, json: async () => { throw Error('bad json'); }})}), /invalid response/);
  await assert.rejects(requestJson('/config', {fetchImpl: async () => ({ok: false, json: async () => ({detail: [{msg: 'Invalid risk value', input: 'private'}]})})}), /Invalid risk value/);
});

test('polling deduplicates overlapping work, then permits a fresh refresh', async () => {
  let resolve, calls = 0;
  const run = singleFlight(() => { calls++; return new Promise(done => { resolve = done; }); });
  const first = run(), second = run();
  assert.equal(first, second);
  await Promise.resolve();
  assert.equal(calls, 1); resolve(10); assert.equal(await first, 10);
  const third = run(); await Promise.resolve(); assert.equal(calls, 2); resolve(20); assert.equal(await third, 20);
});

test('chart changes cancel old requests and ignore late responses', async () => {
  const run = latestTask(); let finishOld, oldSignal;
  const old = run(signal => { oldSignal = signal; return new Promise(done => { finishOld = done; }); });
  const fresh = await run(async () => 'ETH/USDT');
  finishOld('BTC/USDT');
  assert.equal(oldSignal.aborted, true);
  assert.equal(await old, null);
  assert.deepEqual(fresh, {value: 'ETH/USDT'});
});

test('background state updates preserve a chosen backtest pair', () => {
  assert.equal(selectedValue(['BTC/USDT', 'ETH/USDT'], 'ETH/USDT', 'BTC/USDT'), 'ETH/USDT');
  assert.equal(selectedValue(['BTC/USDT'], 'REMOVED/USDT', 'BTC/USDT'), 'BTC/USDT');
  assert.equal(selectedValue([], 'ETH/USDT', 'BTC/USDT'), '');
});

test('manual order draft enforces stopped mode, live arming and finite buy budget', () => {
  const context = '00000000-0000-4000-8000-000000000001';
  const paper = {mode: 'paper', exchange:'mexc', manual_order_context:context, positions: []};
  assert.deepEqual(manualOrderDraft(paper, 'btc/usdt', 'buy', '25'), {symbol:'BTC/USDT',side:'buy',quote_amount:25,confirmation:'',mode:'paper',exchange:'mexc',expected_context:context});
  for (const value of ['', '0', '-1', 'NaN', 'Infinity', '1000001']) assert.throws(() => manualOrderDraft(paper, 'BTC/USDT', 'buy', value), /positive USDT/);
  for (const busy of [{running:true}, {stopping:true}, {control_busy:true}]) assert.throws(() => manualOrderDraft({...paper,...busy}, 'BTC/USDT', 'buy', '25'), /Stop the bot/);
  assert.throws(() => manualOrderDraft({...paper,mode:'live'}, 'BTC/USDT', 'buy', '25'), /explicitly enable/);
  assert.throws(() => manualOrderDraft({...paper,mode:'live',connected:true,live_halted:true}, 'BTC/USDT', 'buy', '25'), /explicitly enable/);
  assert.equal(manualOrderDraft({...paper,mode:'live',connected:true}, 'BTC/USDT', 'buy', '25').mode, 'live');
  assert.throws(() => manualOrderDraft({...paper,mode:'live',connected:true,settings_health:{review_required:true}}, 'BTC/USDT', 'buy', '25'), /recovered settings/);
  assert.throws(() => manualOrderDraft({...paper,manual_order_context:''}, 'BTC/USDT', 'buy', '25'), /Refresh the dashboard/);
});

test('manual sell draft closes only a tracked USDT position and ignores buy budget', () => {
  const context = '00000000-0000-4000-8000-000000000001';
  const paper = {mode:'paper',exchange:'mexc',manual_order_context:context,positions:[{symbol:'ETH/USDT',qty:1}]};
  assert.throws(() => manualOrderDraft(paper, 'BTC/USDT', 'sell', '25'), /no tracked position/);
  assert.throws(() => manualOrderDraft(paper, 'ETH/USDT:USDT', 'sell', '25'), /spot pair/);
  assert.deepEqual(manualOrderDraft(paper, 'ETH/USDT', 'sell', 'invalid'), {symbol:'ETH/USDT',side:'sell',quote_amount:null,confirmation:'',mode:'paper',exchange:'mexc',expected_context:context});
});

test('research scenarios distinguish holdout, full-period stress and missing data', () => {
  const sample = {available:true, return_pct:1.5, max_drawdown_pct:2, closed_trades:3, data_range:{start_ms:1700000000000,end_ms:1700003600000},sample_notes:['Too few trades for a reliable estimate.']};
  const html = researchReportHtml({development:sample,holdout:{available:false,reason:'Insufficient history'},double_cost:sample,notes:['<img src=x onerror=alert(1)>']});
  assert.match(html,/Later holdout/); assert.match(html,/Full period · double costs/);
  assert.match(html,/Insufficient history/); assert.match(html,/not establish an independent profitable edge/);
  assert.ok(html.includes('&lt;img')); assert.ok(!html.includes('<img'));
  assert.equal((html.match(/Too few trades/g)||[]).length,1);
  assert.equal(researchReportHtml(null),'');
});

test('unavailable research metrics are not displayed as positive or zero returns', () => {
  const html = researchReportHtml({development:{available:true,return_pct:null,max_drawdown_pct:Infinity,closed_trades:NaN},holdout:{available:false},double_cost:{available:false}});
  assert.match(html,/—%/); assert.ok(!html.includes('Infinity')); assert.ok(!html.includes('NaN'));
});

test('the footer always says what the last update check found', () => {
  const time = () => '01:32';
  assert.equal(updateFooter(null, time), '');
  assert.equal(updateFooter({disabled: true}, time), 'Update checks are turned off');
  assert.equal(updateFooter({error: 'No published release was found.', checked_at: 1}, time), 'Update check: No published release was found.');
  assert.equal(updateFooter({available: true, latest_version: '1.5.29', checked_at: 1}, time), 'Version 1.5.29 available');
  assert.equal(updateFooter({available: false, checked_at: 1}, time), 'Up to date · checked 01:32');
  assert.equal(updateFooter({phase: 'installing', available: true}, time), 'Updating…');
});

test('a copy run from source is told how to get in-app updates', () => {
  const source = updateSummary({available: true, can_install: false, latest_version: '1.5.29', current_version: '1.5.0'});
  assert.equal(source.install, false);
  assert.match(source.text, /runs from source/);
  assert.match(source.text, /installer from the releases page once/);
  assert.equal(updateSummary({available: true, can_install: true, latest_version: '1.5.29', current_version: '1.4.0'}).install, true);
  assert.equal(updateSummary({available: false, error: 'No published release was found.'}), null);
});

test('checking for updates never reports up to date when no check ran', () => {
  assert.deepEqual(updateToast({disabled: true, available: false, error: null}),
    {text: 'Update checks are turned off (CRYPTOPILOT_NO_UPDATE_CHECK=1).', error: false});
  assert.deepEqual(updateToast({error: 'Could not reach GitHub to check for updates.'}),
    {text: 'Could not reach GitHub to check for updates.', error: true});
  assert.equal(updateToast({available: true, latest_version: '1.5.31'}).text, 'CryptoPilot 1.5.31 is available.');
  assert.equal(updateToast({available: false, checked_at: 1}).text, 'CryptoPilot is up to date.');
});

test('latched entry stops map to the clear actions the risk endpoint accepts', () => {
  const {latchedStops} = require('../static/dashboard-core.js');
  assert.deepEqual(latchedStops(undefined), []);
  assert.deepEqual(latchedStops({drawdown_stop: null, realized_stop: null}), []);
  assert.deepEqual(latchedStops({drawdown_stop: '2026-10-26T01:00:00Z', realized_stop: '2026-10-26T02:00:00Z'}).map(stop => stop.action),
    ['clear_drawdown_stop', 'clear_realized_stop']);
});

test('every element the dashboard script looks up exists in the page', () => {
  // A missing id makes a top-level handler assignment throw, and the dashboard then loads no state at all.
  const fs = require('node:fs'), path = require('node:path');
  const folder = path.join(__dirname, '..', 'static');
  const script = fs.readFileSync(path.join(folder, 'dashboard.js'), 'utf8');
  const page = fs.readFileSync(path.join(folder, 'index.html'), 'utf8');
  const ids = new Set([...page.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
  const used = [...new Set([...script.matchAll(/\$\('([A-Za-z0-9_-]+)'\)/g)].map(match => match[1]))];
  assert.ok(used.length > 50);
  assert.deepEqual(used.filter(id => !ids.has(id)), []);
});

test('micro amounts never hide a sign or a non-zero value behind rounding', () => {
  assert.equal(microAmount('-0.00003'), '-0.00003');
  assert.equal(microAmount('0'), '0.0000');
  assert.equal(microAmount('1e-12'), '<0.00000001');
  assert.equal(microAmount('-1e-12'), '-<0.00000001');
  assert.equal(microAmount('0.004', 4, true), '+0.0040');
  assert.equal(microAmount('-0.0001', 4, true), '-0.0001');
  assert.equal(microAmount(null), 'unknown');
  assert.equal(microAmount('not a number'), 'unknown');
});

const microState = () => ({
  active: true, running: false, paper_only: true, paper_only_reason: 'The micro-trading mode is PAPER only in this build.',
  budget: {cap: '25', absolute_cap: '25', allocation: '50', reserved: '0', committed_basis: '24.97', max_cycles: 2},
  latch: null, review_block: [], last_error: null, counters: {NO_ECONOMIC_EDGE: 7, SPREAD: 2},
  pairs: [
    {symbol: 'BTC/USDT', selected: true, regime: 'ranging', waiting: {code: 'NO_ECONOMIC_EDGE', status: 'Required move exceeds the expected range', waiting_s: 120},
     sizing: {eligible: true, max_debit: '24.9750', gross_quote: '24.95', fee_reserve: '0.0250', minimum_feasible_budget: '1.01'},
     book: {best_bid: '100', best_ask: '100.01', spread_bps: 1, age_s: 0.4, source: 'rest_depth'},
     decision: {checks: [{check: 'spread', passed: true, detail: '1.0 bps'}, {check: 'edge', passed: false, detail: '<b>needs 30 bps</b>'}]}},
    {symbol: 'PEPE/USDT', selected: false, sizing: {eligible: false, code: 'BUDGET_BELOW_VENUE_MINIMUM', detail: 'Venue minimum 30 USDT', minimum_feasible_budget: '30.05'}},
  ],
  cycles: [{cycle_id: 'cyc_aaaaaaaaaaaaaaaaaaaaaaaa', symbol: 'ETH/USDT', state: 'HOLDING', strategy: 'range_reversion', owned_base: '0.0099',
            entry_debit: '24.97', target_net: '0.05', stop_net: '0.2', age_s: 300, max_hold_s: 1800,
            estimate: {est_net_pnl: '-0.00003', depth_model: 'book', uncertainty: '0.0125', break_even_bid: '2506.77',
                       target_bid: '2511.78'}, entry_price: '2500.51', exit_verdict: {reason: 'hold'}}],
  results: {closed_cycles: 3, wins: 1, losses: 2, flat: 0, realized_net_total: '-0.0123', average_net: '-0.0041', worst_loss: '-0.02',
            best_gain: '0.015', max_drawdown: '0.03', fees_paid: '0.15', fees_share_of_gross_gains: 10, turnover: '150',
            average_hold_s: 600, near_break_even_exits: 1, exit_triggers: {take_profit: 1, stop_loss: 2}, note: 'PAPER simulated fills.'},
});

test('the micro panel separates estimates from simulated fills and explains ineligible pairs', () => {
  assert.equal(microView(null), null);
  assert.equal(microView({active: false, cycles: []}), null);
  const view = microView(microState());
  assert.match(view.headline, /Stopped/);
  assert.equal(view.holding, 1);
  assert.match(view.budget[0][1], /25\.00 USDT \(fees included/);
  const [btc, pepe] = view.pairs;
  assert.match(btc.eligibility, /^Eligible · 24\.9750 USDT all-in \(24\.9500 order \+ 0\.0250 fee reserve\)/);
  assert.match(btc.waiting, /for 2 min$/);
  assert.equal(btc.checks.length, 2);
  assert.match(btc.checks[1], /^✗ edge: <b>/);           // plain text: the renderer escapes it
  assert.equal(pepe.selected, false);
  assert.match(pepe.eligibility, /^BUDGET_BELOW_VENUE_MINIMUM: Venue minimum 30 USDT/);
  assert.equal(pepe.minimum, '30.05 USDT');
  const [eth] = view.cycles;
  assert.equal(eth.estimate, '-0.00003 ± 0.0125 USDT (estimated, book)');
  assert.equal(eth.prices, 'entry 2500.51 · break-even bid 2506.77 · target bid 2511.78');
  assert.equal(eth.debit, '24.9700 USDT (simulated fill)');
  assert.equal(view.results[1][1], '-0.0123 USDT');
  assert.ok(view.notes.some(note => /PAPER only/.test(note)));
  assert.deepEqual(view.counters, ['NO_ECONOMIC_EDGE 7', 'SPREAD 2']);
});

test('micro stops and unknown outcomes are shown as blocking warnings with a recovery action', () => {
  const state = microState();
  state.latch = {reason: '4 consecutive losing micro cycles'};
  state.review_block = [{cycle_id: 'cyc_bbbbbbbbbbbbbbbbbbbbbbbb', symbol: 'ETH/USDT', state: 'EXIT_UNKNOWN', explanation: 'The paper venue reports the exit order as closed.'}];
  const view = microView(state);
  assert.equal(view.latched, true);
  assert.ok(view.warnings.some(text => /Micro stop latched: 4 consecutive/.test(text) && /RESUME TRADING/.test(text)));
  assert.ok(view.warnings.some(text => /Review required for ETH\/USDT \(Exit outcome unknown/.test(text)));
  assert.deepEqual(view.recovery.map(item => item.cycle_id), ['cyc_bbbbbbbbbbbbbbbbbbbbbbbb']);
  const unavailable = microView({active: true, error: 'micro status unavailable: OperationalError'});
  assert.deepEqual(unavailable.warnings, ['micro status unavailable: OperationalError']);
});
