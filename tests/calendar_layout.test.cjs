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

const run = (expression) => JSON.parse(vm.runInContext(`JSON.stringify(${expression})`, context));

test('free time subtracts merged busy ranges from overlapping set windows', () => {
  assert.deepEqual(run('subtractMinuteRanges([[540, 1020], [600, 660]], [[600, 720], [630, 660], [660, 780]])'),
    [[540, 600], [780, 1020]]);
  assert.deepEqual(run('subtractMinuteRanges([[1320, 1435]], [[1410, 1440]])'), [[1320, 1410]]);
  assert.deepEqual(run('subtractMinuteRanges([[540, 600]], [])'), [[540, 600]]);
  assert.deepEqual(run('subtractMinuteRanges([[540, 600]], [[500, 700]])'), []);
});

test('free summary reports how much of one window remains free', () => {
  const summary = (start, end, free) => run(`freeSummaryText(freeMinutesWithin(${start}, ${end}, ${JSON.stringify(free)}), ${end - start})`);
  assert.equal(summary(540, 1020, [[540, 1020]]), 'All free');
  assert.equal(summary(540, 1020, []), 'No free time');
  assert.equal(summary(540, 1020, [[540, 600], [780, 1020]]), '5h free');
  assert.equal(summary(540, 1020, [[540, 600], [780, 1010]]), '4h 50m free');
  assert.equal(summary(540, 600, [[540, 570], [900, 960]]), '30m free');
});

test('picker moves off the submit button, then beside its container', () => {
  const viewport = {width: 800, height: 900};
  const size = {width: 280, height: 160};
  const input = {left: 40, right: 200, top: 400, bottom: 440};
  const submit = {left: 300, right: 380, top: 480, bottom: 520};
  assert.deepEqual(run(`choosePopupPosition(${JSON.stringify(input)}, ${JSON.stringify(size)}, ${JSON.stringify(viewport)}, null)`),
    {left: 40, top: 446});
  assert.deepEqual(run(`choosePopupPosition(${JSON.stringify(input)}, ${JSON.stringify(size)}, ${JSON.stringify(viewport)}, ${JSON.stringify(submit)})`),
    {left: 40, top: 234});
  const nearTop = {left: 40, right: 200, top: 60, bottom: 100};
  const container = {left: 20, right: 400, top: 20, bottom: 300};
  const lowSubmit = {left: 40, right: 380, top: 110, bottom: 150};
  assert.deepEqual(run(`choosePopupPosition(${JSON.stringify(nearTop)}, ${JSON.stringify(size)}, ${JSON.stringify(viewport)}, ${JSON.stringify(lowSubmit)}, ${JSON.stringify(container)})`),
    {left: 406, top: 60});
});
