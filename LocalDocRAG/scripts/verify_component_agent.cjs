// Uses the actual component TypeScript tools and the running local API.
// Node resolves the component's existing Babel/fake-indexeddb dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '..');
const component = path.resolve(root, '../../component');
const fromComponent = createRequire(path.join(component, 'package.json'));
fromComponent('@babel/register')({
  cwd: component, extensions: ['.ts', '.tsx'],
  presets: [[fromComponent.resolve('@babel/preset-env'), { targets: { node: 'current' } }], fromComponent.resolve('@babel/preset-typescript')],
  only: [filename => filename.toLowerCase().startsWith(component.toLowerCase())],
});
global.indexedDB = fromComponent('./artifacts/agent-test-deps/node_modules/fake-indexeddb').indexedDB;
const agent = path.join(component, 'src/core/common/ActionDesign/Agent');
const { createLocalDocRagKnowledgeSource } = require(path.join(agent, 'business/knowledge/BusinessKnowledgeSource'));
const { BusinessRuntime } = require(path.join(agent, 'business/BusinessRuntime'));
const { knowledgeSearchTool } = require(path.join(agent, 'tools/KnowledgeSearchTool'));
const { knowledgeReadTool } = require(path.join(agent, 'tools/KnowledgeReadTool'));
const { createSha256Hash } = require(path.join(agent, 'contracts/ManifestHash'));
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'data/training-manual.json'), 'utf8'));
const base = process.env.LOCALDOC_BASE || 'http://127.0.0.1:7860';
const realFetch = global.fetch;
const requests = [];
const args = process.argv.slice(2);
assert(args.length === 0 || (args.length === 2 && args[0] === '--output' && args[1]),
  'Usage: node scripts/verify_component_agent.cjs [--output new-report.json]');
const startedAt = new Date().toISOString();
const reportPath = args.length ? path.resolve(args[1]) : path.join(root, 'data',
  `component-agent-verification-${startedAt.replace(/[-:.]/g, '')}-${randomUUID()}.json`);
assert(!fs.existsSync(reportPath), 'The output report already exists; choose a new file');
global.fetch = (url, options) => {
  assert(String(url).startsWith('/localdoc/api/'));
  requests.push(String(url));
  return realFetch(base + String(url).replace(/^\/localdoc/, ''), options);
};

(async () => {
  const source = createLocalDocRagKnowledgeSource({ apiPrefix: '/localdoc/api', knowledgeBaseId: manifest.knowledge_base_id,
    processing: { retrieval: 'local', model: 'configured_gateway' } });
  const scope = { environment: base, userId: 'local-developer', appId: 'training', roles: ['user'] };
  const runtime = new BusinessRuntime('localdoc-live-check', scope, 'local-session', { release: async () => {} }, undefined,
    { entry: true, knowledge: true, observe: false, input: false, commit: false }, false, source);
  const actionContext = { executionDomain: 'action_design', businessKnowledgeSource: source };
  const businessContext = { executionDomain: 'business_operation', business: runtime };
  const searchArgs = { source: 'business', query: 'FS-5.4.2.7-008 发起岗位培训', limit: 5 };
  const actionSearch = await knowledgeSearchTool.execute(searchArgs, actionContext);
  const businessSearch = await knowledgeSearchTool.execute(searchArgs, businessContext);
  assert(actionSearch.success && businessSearch.success, 'Both domains must search the real shared source');
  assert.equal(actionSearch.data.knowledgeSource, 'business');
  assert.equal(businessSearch.data.knowledgeSource, 'business');
  assert.equal(actionSearch.data.resultVersion, businessSearch.data.resultVersion, 'Both searches must use the same active snapshot');
  assert.deepEqual(actionSearch.data.results.map(row => row.id), businessSearch.data.results.map(row => row.id),
    'Both searches must return the same evidence ids');
  const discoveredRow = actionSearch.data.results[0];
  assert(discoveredRow.fullTableId, 'A searched table row must expose its complete-table id before reading');
  assert.match(discoveredRow.readHint, /knowledge_read/);
  assert.equal(discoveredRow.fullTableId, businessSearch.data.results[0].fullTableId);
  const rowId = discoveredRow.id;
  const row = await source.get(rowId, scope);
  assert(row.content.includes('电子签名') && row.content.includes('部门审批'));
  const fullId = row.metadata.fullTableId || /localdoc:[a-f0-9-]{36}/.exec(row.content)?.[0];
  assert(fullId, 'Specific row must link to the complete operation');
  assert.equal(discoveredRow.fullTableId, fullId, 'Discovery and reading must link the same complete table');
  assert.match(fullId, /^localdoc:[a-f0-9-]{36}$/);
  assert.equal(await runtime.hasCompleteKnowledgeRead(), false);
  async function readAll(context) {
    const maxChars = 1400;
    let offset = 0, contentVersion, sourceVersion, provenance, totalChars, content = '', pages = 0;
    do {
      const result = await knowledgeReadTool.execute({ id: fullId, offset, maxChars,
        ...(contentVersion ? { contentVersion } : {}) }, context);
      assert(result.success, 'Each page must be readable through the actual knowledge_read tool');
      const page = result.data;
      assert.equal(page.id, fullId);
      assert.equal(page.offset, offset, 'Pages must start at the exact continuation offset');
      assert.equal(page.returnedChars, page.content.length, 'Returned character count must match the actual page');
      assert.equal(page.returnedChars, Math.min(maxChars, page.totalChars - offset), 'Each page must preserve the requested text range');
      if (contentVersion) {
        assert.equal(page.contentVersion, contentVersion, 'All pages must use the same content hash');
        assert.equal(page.metadata.sourceVersion, sourceVersion, 'All pages must use the same source version');
        assert.deepEqual(page.metadata.source, provenance, 'All pages must preserve the same provenance');
        assert.equal(page.totalChars, totalChars, 'All pages must use the same total character count');
      } else {
        sourceVersion = page.metadata.sourceVersion;
        provenance = page.metadata.source;
        totalChars = page.totalChars;
      }
      content += page.content;
      contentVersion = page.contentVersion;
      assert.equal(page.nextOffset, content.length < totalChars ? content.length : null,
        'Continuation must be contiguous and end only after all text has been read');
      offset = page.nextOffset;
      pages++;
      if (context === businessContext && offset !== null) assert.equal(await runtime.hasCompleteKnowledgeRead(), false);
      assert(pages < 100, 'Reading must terminate');
    } while (offset !== null);
    assert(pages >= 2, 'The real operation must exercise continuation reads');
    assert.equal(content.length, totalChars, 'No source text may be omitted by pagination');
    assert.equal(pages, Math.ceil(totalChars / maxChars), 'Page count must match the complete text length');
    return { content, contentVersion, sourceVersion, provenance, pages };
  }
  const actionRead = await readAll(actionContext);
  assert.equal(await runtime.hasCompleteKnowledgeRead(), false, 'Action reads must not complete business task state');
  const businessRead = await readAll(businessContext);
  assert.deepEqual(actionRead, businessRead, 'Both domains must read identical content, source version and provenance');
  assert.equal(await runtime.hasCompleteKnowledgeRead(), true);
  const full = await source.get(fullId, scope);
  assert.equal(businessRead.content, full.content, 'Paged text must preserve all conditions');
  assert.equal(businessRead.sourceVersion, manifest.version_id, 'Both domains must use the current imported version');
  assert.equal(businessRead.contentVersion, createSha256Hash(full));
  for (const suffix of ['001','002','003','005','007','008','009','010']) assert(businessRead.content.includes('FS-5.4.2.7-' + suffix));
  assert.equal(full.metadata.source.operation_complete, true);
  assert.equal(full.metadata.source.table_complete, true);
  assert.equal(full.metadata.status, 'evidence_only');
  for (const context of [actionContext, businessContext]) {
    const changed = await knowledgeReadTool.execute({ id: fullId, offset: 0, contentVersion: 'wrong-version' }, context);
    assert.equal(changed.success, false);
    assert.match(changed.error, /TEXT_CHANGED/);
  }
  await assert.rejects(() => runtime.assertKnowledgeCurrent({ operationId: 'unapproved', knowledge: [{ id: fullId, sourceVersion: full.metadata.sourceVersion, contentVersion: createSha256Hash(full) }] }), /KNOWLEDGE_UNRESOLVED/);
  const report = { passed: true, started_at: startedAt, completed_at: new Date().toISOString(), real_backend: base,
    knowledge_base_id: manifest.knowledge_base_id, version_id: businessRead.sourceVersion,
    searched_row: rowId, full_operation: fullId, pages: businessRead.pages, pages_per_domain: { action_design: actionRead.pages, business_operation: businessRead.pages },
    total_chars: businessRead.content.length, requests: requests.length, result_version: actionSearch.data.resultVersion,
    content_version: businessRead.contentVersion, shared_search_snapshot: true, shared_read_version_and_provenance: true,
    complete_table_discoverable_from_search: true,
    action_reads_do_not_complete_business_state: true, full_conditions_preserved: true,
    changed_version_rejected: true, unreviewed_execution_blocked: true, report_path: reportPath };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify(report, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
