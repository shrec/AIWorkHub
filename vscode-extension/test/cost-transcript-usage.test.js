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
