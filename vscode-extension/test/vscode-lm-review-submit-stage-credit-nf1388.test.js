'use strict';

const assert = require('assert');
const path = require('path');
const Module = require('module');

const fakeVscode = {
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class {
    constructor(callId, content) {
      this.callId = callId;
      this.content = content;
    }
  },
  LanguageModelChatMessage: {
    User: (content) => ({ role: 'user', content }),
    Assistant: (content) => ({ role: 'assistant', content }),
  },
};

let internals;
{
  const extensionPath = path.join(__dirname, '..', 'extension.js');
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === 'vscode') return fakeVscode;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    delete require.cache[require.resolve(extensionPath)];
    internals = require(extensionPath).__testInternals;
  } finally {
    Module._load = originalLoad;
  }
}

const SUBMIT = 'aiworkhub_worker_quality_review_submit';
const SOURCE_GRAPH = 'aiworkhub_worker_source_graph_query';

function responseWithParts(parts) {
  const iterator = parts[Symbol.iterator]();
  return {
    stream: {
      [Symbol.asyncIterator]() {
        return {
          next: async () => {
            const entry = iterator.next();
            return entry.done ? { done: true } : { done: false, value: entry.value };
          },
        };
      },
    },
  };
}

async function forcedReviewOffersOnlySubmit() {
  const toolsPerTurn = [];
  let storedLastMessage = null;
  const model = {
    capabilities: { toolCalling: true },
    async sendRequest(messages, options) {
      const tools = Array.isArray(options && options.tools)
        ? options.tools.map((tool) => (typeof tool === 'string' ? tool : tool && tool.name))
        : [];
      toolsPerTurn.push(tools);
      const turn = toolsPerTurn.length;
      const lastMessage = messages[messages.length - 1];
      if (turn === 5) storedLastMessage = JSON.stringify(lastMessage);
      if (turn <= 4) {
        return responseWithParts([{
          callId: `review-sg-${turn}`,
          name: SOURCE_GRAPH,
          input: { mode: 'focus', query: `q-${turn}`, workflow_stage: 'review' },
        }]);
      }
      return responseWithParts([{
        callId: `review-submit-${turn}`,
        name: SUBMIT,
        input: { packet_sha256: 'e'.repeat(64), lens: 'correctness', findings: [] },
      }]);
    },
  };
  const transport = async (call) => {
    if (call && call.name === SUBMIT) {
      return { ok: true, durable: true, submission_id: 'd'.repeat(64) };
    }
    return { ok: true, content: 'graph' };
  };
  const request = {
    requestId: 'c'.repeat(32),
    request_kind: 'quality_review',
    prompt: 'bounded review',
    allowedWrites: [],
    path_contracts: {},
  };
  await internals.runVscodeLmAgent(model, request, undefined, transport);
  assert.strictEqual(toolsPerTurn.length, 5);
  assert.ok(toolsPerTurn[0].includes(SOURCE_GRAPH));
  assert.deepStrictEqual(toolsPerTurn[3], [SUBMIT]);
  assert.deepStrictEqual(toolsPerTurn[4], [SUBMIT]);
  assert.ok(typeof storedLastMessage === 'string' && storedLastMessage.includes(`Call ${SUBMIT} now`));
}

function stagedEditIsProgress() {
  const guard = internals.createVscodeLmSourceGraphGuard();
  const receipt = (sha) => ({
    ok: true,
    schema_id: 'aiworkhub.vscode_lm.staged_edit_receipt.v1',
    operation: 'replace_range',
    path: 'src/a.py',
    content_sha256: sha.repeat(64),
  });
  guard.staged(receipt('a'), true);
  assert.strictEqual(guard.stageRevision(), 0);
  guard.staged(receipt('b'), true);
  assert.strictEqual(guard.stageRevision(), 1);
  guard.staged(receipt('b'), true);
  assert.strictEqual(guard.stageRevision(), 1);
  guard.staged(receipt('c'), false);
  assert.strictEqual(guard.stageRevision(), 1);
}

async function secondStageRefundsATurn(toolCalling) {
  const MAX = internals.constants.VSCODE_LM_MAX_POST_SOURCE_TURNS;
  const stageCalls = [
    { name: 'aiworkhub_manager_semantic_edit_stage', input: { operation: 'create', file_path: 'tests/a.py', content: 'x = 1\n' } },
    { name: 'aiworkhub_manager_semantic_edit_stage', input: { operation: 'create', file_path: 'tests/b.py', content: 'x = 1\n' } },
  ];
  const sourceGraphCalls = [];
  for (let i = 0; i < MAX; i += 1) {
    sourceGraphCalls.push({ name: SOURCE_GRAPH, input: { mode: 'body', query: `legacy_${i}`, target: 'src/app.js' } });
  }
  const plan = [...stageCalls, ...sourceGraphCalls].map((call) => [call]);
  let turns = 0;
  const model = {
    capabilities: { toolCalling },
    async sendRequest() {
      const turn = turns;
      turns += 1;
      if (turn >= plan.length) throw new Error('unplanned turn');
      const batch = plan[turn];
      if (toolCalling) {
        return responseWithParts(batch.map((call, index) => ({
          callId: `planned-${turn}-${index}`,
          name: call.name,
          input: call.input,
        })));
      }
      const value = batch
        .map((call) => JSON.stringify({
          schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
          name: call.name,
          input: call.input,
        }))
        .join('\n');
      return responseWithParts([{ value }]);
    },
  };
  const transport = async () => ({ ok: true });
  const request = {
    requestId: 'e'.repeat(32),
    request_kind: 'worker',
    allowedWrites: ['tests/a.py', 'tests/b.py'],
    initial_source_graph_result: { ok: true, content: 'injected graph' },
    initial_source_graph_request: { mode: 'focus', query: 'app' },
    path_contracts: {
      'tests/a.py': { action: 'create', parent_existed: false, line_count: 0, current_sha256: '' },
      'tests/b.py': { action: 'create', parent_existed: false, line_count: 0, current_sha256: '' },
    },
  };
  const run = toolCalling ? internals.runVscodeLmAgent : internals.runVscodeLmTextProtocol;
  const finalPayload = await run(model, request, undefined, transport);
  const result = JSON.parse(finalPayload);
  assert.strictEqual(turns, MAX + 1);
  assert.strictEqual(result.creates.length, 2);
}

function promptStagesWithoutPrepare() {
  const prompt = internals.glmTextToolProtocolPrompt('edit', ['src/a.py']);
  assert.ok(typeof prompt === 'string' && prompt.includes('it needs no prior prepare'));
  assert.ok(!prompt.includes('aiworkhub_worker_semantic_edit_prepare then'));
}

(async () => {
  try {
    await forcedReviewOffersOnlySubmit();
    stagedEditIsProgress();
    await secondStageRefundsATurn(false);
    await secondStageRefundsATurn(true);
    promptStagesWithoutPrepare();
    console.log('vscode-lm-review-submit-stage-credit-nf1388: ok');
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
})();
