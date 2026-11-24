const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('CSRF is attached only to same-origin mutations, including Request inputs', async () => {
  const calls = [];
  const window = { location: new URL('https://planner.example.invalid/account'), fetch: async (...args) => { calls.push(args); return {}; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/security.js'), 'utf8'), {
    window, URL, Request, Headers, document: { querySelector: () => ({ content: 'synthetic-csrf' }), addEventListener: () => {} },
  });
  await window.fetch('/account/email', { method: 'POST' });
  assert.equal(calls[0][1].headers.get('X-CSRF-Token'), 'synthetic-csrf');
  await window.fetch('https://other.example.invalid/path', { method: 'POST' });
  assert.equal(calls[1][1].headers, undefined);
  await window.fetch('/account', { method: 'GET' });
  assert.equal(calls[2][1].headers, undefined);
  await window.fetch(new Request('https://planner.example.invalid/api/notifications/test', { method: 'DELETE', headers: { Accept: 'application/json' } }));
  assert.equal(calls[3][1].headers.get('X-CSRF-Token'), 'synthetic-csrf');
  assert.equal(calls[3][1].headers.get('Accept'), 'application/json');
});
