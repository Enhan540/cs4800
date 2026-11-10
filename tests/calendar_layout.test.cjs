const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(0, source.indexOf('document.addEventListener("DOMContentLoaded"')), context);
const layout = (windows) => JSON.parse(vm.runInContext(
  `JSON.stringify(calendarBusyLayout(${JSON.stringify(windows)}))`, context,
));

test('overlapping and nested busy intervals occupy distinct lanes', () => {
  const result = layout([{start: 540, end: 660}, {start: 570, end: 600}, {start: 575, end: 590}]);
  assert.deepEqual(result.map(({lane, lanes}) => [lane, lanes]), [[0, 3], [1, 3], [2, 3]]);
});
test('touching intervals use full width; chains reuse lanes without collisions', () => {
  const result = layout([{start: 540, end: 600}, {start: 570, end: 630}, {start: 600, end: 660}, {start: 660, end: 720}]);
  assert.deepEqual(result.map(({lane, lanes}) => [lane, lanes]), [[0, 2], [1, 2], [0, 2], [0, 1]]);
});
test('hiding overlapping calendars restores full width and all hidden is valid', () => {
  assert.deepEqual(layout([{start: 540, end: 600}]).map(({lane, lanes}) => [lane, lanes]), [[0, 1]]);
  assert.deepEqual(layout([]), []);
});
test('unsorted and identical short intervals keep stable distinct positions', () => {
  const result = layout([{start: 600, end: 660}, {start: 540, end: 541}, {start: 540, end: 541}]);
  assert.deepEqual(result.map(({index, lane, lanes}) => [index, lane, lanes]), [[1, 0, 2], [2, 1, 2], [0, 0, 1]]);
});
