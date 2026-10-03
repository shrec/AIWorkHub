const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.textContent = ''; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) { this.children.push(...(node.tag === 'fragment' ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get text() { return this.textContent + this.children.map(child => child.text).join(' '); }
  set innerHTML(_) { throw new Error('Unsafe HTML rendering'); }
}

for (const relative of ['media/app.js', '../src/aiworkhub/dashboard_static/dashboard.js']) {
  const source = fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');
  const start = source.indexOf('function renderUsage(snapshot)');
  const end = source.indexOf('\nfunction ', start + 1);
  const list = new Element('div');
  const context = {
    elements: { usageList: list },
    document: { createDocumentFragment: () => new Element('fragment') },
    createElement: (tag, cls, text = '') => { const node = new Element(tag); node.className = cls; node.textContent = text; return node; },
    numberValue: value => Number(value) || 0,
    formatCount: value => String(value),
    formatMoney: value => '$' + Number(value).toFixed(2),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  const transcript = {
    status: 'measured', source: 'claude_code_transcripts',
    totals: { sessions: 12, subagent_transcripts: 2,
      main: { api_calls: 1, input_tokens: 100, output_tokens: 10, cache_read_input_tokens: 20, cache_creation_input_tokens: 5 },
      subagent: { api_calls: 2, input_tokens: 70, output_tokens: 7, cache_read_input_tokens: 30, cache_creation_input_tokens: 6 } },
    total_count: 12, returned_count: 10, truncated: true,
  };
  const render = (totals = {}, section = transcript, byRunner = {}) => {
    const snapshot = { cost_usage: { totals, ledger: { aggregates: { by_runner: byRunner }, claude_code_sessions: section } } };
    const before = JSON.stringify(snapshot);
    context.renderUsage(snapshot);
    assert.equal(JSON.stringify(snapshot), before);
    return list.text;
  };
  let text = render();
  assert.match(text, /Claude Code transcript usage/);
  assert.match(text, /repository history/i);
  assert.match(text, /unpriced/i);
  assert.match(text, /not task-attributed/i);
  assert.match(text, /Main: 1 calls/);
  assert.match(text, /Subagents: 2 calls/);
  assert.match(text, /Main: 1 calls \| 100 input \| 10 output \| 20 cache-read \| 5 cache-creation tokens/);
  assert.match(text, /Subagents: 2 calls \| 70 input \| 7 output \| 30 cache-read \| 6 cache-creation tokens/);
  assert.match(text, /12 sessions/);
  assert.match(text, /10 of 12/);
  assert.match(text, /truncated/);
  assert.doesNotMatch(text, /\$0\.00/);
  text = render({ available: true, records: 1, input_tokens: 9, output_tokens: 3, cost_usd: 6.5, cost_known_records: 1, usage_observed_records: 1 });
  assert.match(text, /\$6\.50/);
  assert.match(text, /Input 9/);
  text = render({ available: true, cost_usd: 0, cost_known_records: 1 });
  assert.match(text, /\$0\.00/);
  text = render({ available: true, cost_usd: 0, cost_known_records: 0 });
  assert.doesNotMatch(text, /\$0\.00/);
  for (const section of [null, { status: 'unknown' }, { ...transcript, source: 'unverified' }, { ...transcript, totals: null }]) {
    assert.doesNotMatch(render({}, section), /Claude Code transcript usage|Main: 0|Subagents: 0/);
  }
  const corrupt = { ...transcript, totals: { ...transcript.totals, subagent: {} }, returned_count: 100 };
  text = render({}, corrupt);
  assert.match(text, /Subagents: unavailable/);
  assert.match(text, /12 of 12/);
  text = render({}, { ...transcript, returned_count: -1 });
  assert.match(text, /Session detail coverage unavailable/);
  text = render({}, { ...transcript, totals: { ...transcript.totals, main: { ...transcript.totals.main, api_calls: '1' } } });
  assert.match(text, /Main: unavailable/);
  text = render({}, { ...transcript, total_count: 0, returned_count: 0, truncated: false });
  assert.match(text, /0 of 0 — not truncated/);
  text = render({ available: true, cost_known_records: 0 }, undefined, { '<img onerror=evil>': { records: 1, cost_known_records: 0 } });
  assert.match(text, /<img onerror=evil>/);
  assert.doesNotMatch(text, /\$0\.00/);
  console.log(`${relative}: transcript usage DOM tests passed`);
}

for (const relative of ['media/app.js', '../src/aiworkhub/dashboard_static/dashboard.js']) {
  const source = fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');
  const extract = name => {
    const start = source.indexOf('function ' + name + '(');
    assert.ok(start >= 0, name + ' production function missing');
    const end = source.indexOf('\nfunction ', start + 1);
    return source.slice(start, end < 0 ? source.length : end);
  };
  const nodes = new Map();
  const context = {
    Intl,
    numberValue: value => Number(value) || 0,
    formatCount: value => String(value || 0),
    formatRelativeTime: () => '',
    formatBytes: value => String(value || 0),
    setTileHealth() {},
    elements: { lastSync: {}, headerStorageManaged: {}, headerStorageFree: {} },
    document: { querySelector(id) {
      if (!nodes.has(id)) nodes.set(id, { textContent: '', title: '' });
      return nodes.get(id);
    } },
  };
  vm.createContext(context);
  vm.runInContext(extract('formatMoney') + '\n' + extract('renderSummary'), context);
  const complete = { available: true, records: 1, cost_known_records: 1, cost_unknown_records: 0, cost_complete: true, cost_usd: 0 };
  const partial = { ...complete, records: 3, cost_unknown_records: 2, cost_complete: false, cost_usd: 6.5 };
  const renderHeader = totals => {
    const snapshot = totals === undefined ? {} : { cost_usage: { totals } };
    const before = JSON.stringify(snapshot);
    context.renderSummary(snapshot);
    assert.equal(JSON.stringify(snapshot), before, 'header never changes canonical totals');
    return nodes.get('#metric-cost');
  };
  let header = renderHeader(partial);
  assert.equal(header.textContent, 'Known ' + context.formatMoney(6.5));
  assert.match(header.title, /1 of 3 records/);
  assert.match(header.title, /2 records unpriced/);
  header = renderHeader(complete);
  assert.equal(header.textContent, context.formatMoney(0), 'observed zero remains currency');
  assert.match(header.title, /1 of 1 records/);
  assert.doesNotMatch(header.title, /unpriced/);
  for (const invalid of [
    undefined, {}, { ...complete, available: false }, { ...complete, available: 'true' },
    { ...complete, records: 0, cost_known_records: 0 },
    { ...partial, cost_known_records: 0, records: 2 },
    { ...complete, records: '1' }, { ...complete, cost_known_records: '1' },
    { ...complete, cost_unknown_records: '0' }, { ...complete, records: -1 },
    { ...complete, cost_known_records: -1 }, { ...complete, cost_unknown_records: -1 },
    { ...complete, records: 1.5 }, { ...complete, cost_known_records: 0.5 },
    { ...complete, cost_unknown_records: 0.5 },
    { ...complete, records: Number.MAX_SAFE_INTEGER + 1 },
    { ...complete, records: Number.MAX_SAFE_INTEGER, cost_known_records: Number.MAX_SAFE_INTEGER, cost_unknown_records: 1 },
    { ...complete, cost_unknown_records: NaN }, { ...complete, cost_known_records: Infinity },
    { ...complete, records: 2 }, { ...complete, cost_complete: false },
    { ...partial, cost_complete: true }, { ...complete, cost_complete: undefined },
    { ...complete, cost_usd: null }, { ...complete, cost_usd: '0' },
    { ...complete, cost_usd: -1 }, { ...complete, cost_usd: NaN },
    { ...complete, cost_usd: Infinity }, { ...complete, cost_usd: undefined },
  ]) {
    renderHeader(partial); // unavailable must reset an earlier amount and coverage
    header = renderHeader(invalid);
    assert.equal(header.textContent, 'Cost unavailable');
    assert.equal(header.title, 'Cost unavailable');
  }
  assert.equal(renderHeader(partial).textContent, 'Known ' + context.formatMoney(6.5));
  console.log(relative + ': header cost coverage DOM tests passed');
}
for (const relative of ['extension.js', '../src/aiworkhub/dashboard_static/index.html']) {
  const source = fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');
  assert.match(source, /id="metric-cost">Cost unavailable<\/span>/);
}
