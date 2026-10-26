'use strict';
// Run with Node 22+ (24 LTS recommended): node --test tests/test_dashboard.js. No exchange/network access.
const test = require('node:test');
const assert = require('node:assert/strict');
const {escapeHtml, errorMessage, requestJson, singleFlight, latestTask, selectedValue, manualOrderDraft, researchReportHtml, updateSummary, updateFooter} = require('../static/dashboard-core.js');

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
