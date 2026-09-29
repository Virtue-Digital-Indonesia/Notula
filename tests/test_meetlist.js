/*
 * Exercise the meetings list filter, using the real function pulled out of
 * notula_ui.html rather than a copy — a copy would drift.
 *
 *     node tests/test_meetlist.js
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'assets', 'notula_ui.html'), 'utf8');

// take filteredMeetings() verbatim from the page
const m = html.match(/function filteredMeetings\(\)\{[\s\S]*?\n\}/);
if (!m) { console.error('FAIL  could not find filteredMeetings() in the page'); process.exit(1); }

let fail = [];
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}  ${detail === undefined ? '' : detail}`);
  if (!cond) fail.push(name);
}

// minimal DOM: the function only reads two control values
let controls = { meet_q: { value: '' }, meet_days: { value: '0' } };
const $ = (id) => controls[id];
let MEETINGS = [];
const filteredMeetings = new Function('$', 'MEETINGS',
  m[0] + '; return filteredMeetings();');
const run = () => filteredMeetings($, MEETINGS);

const DAY = 86400;
const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
const t0 = midnight.getTime() / 1000;

MEETINGS = [
  { id: 'a', name: 'Weekly sync',        ts: t0 + 3600 },        // today
  { id: 'b', name: 'Standup with Dinda', ts: t0 - 2 * DAY },     // 2 days ago
  { id: 'c', name: 'CKB kickoff',        ts: t0 - 10 * DAY },    // 10 days ago
  { id: 'd', name: 'Board review',       ts: t0 - 60 * DAY },    // 60 days ago
  { id: 'e', name: 'no timestamp',       ts: 0 },                // legacy row
];

// ---- no filters -----------------------------------------------------------
check('no filter shows everything', run().length === 5, run().length);

// ---- search ---------------------------------------------------------------
controls.meet_q.value = 'sync';
check('search matches a name', run().map(x => x.id).join() === 'a', run().map(x => x.id));

controls.meet_q.value = 'DINDA';
check('search is case-insensitive', run().map(x => x.id).join() === 'b');

controls.meet_q.value = '  kickoff  ';
check('search trims whitespace', run().map(x => x.id).join() === 'c');

controls.meet_q.value = 'zzz';
check('no match yields nothing', run().length === 0);

controls.meet_q.value = '';

// ---- date -----------------------------------------------------------------
controls.meet_days.value = '1';
check('today only', run().map(x => x.id).join() === 'a', run().map(x => x.id));

controls.meet_days.value = '7';
check('last 7 days', run().map(x => x.id).join() === 'a,b', run().map(x => x.id));

controls.meet_days.value = '30';
check('last 30 days', run().map(x => x.id).join() === 'a,b,c', run().map(x => x.id));

controls.meet_days.value = '90';
check('last 90 days', run().map(x => x.id).join() === 'a,b,c,d', run().map(x => x.id));

check('a row with no timestamp is hidden by a date filter',
  !run().some(x => x.id === 'e'));

controls.meet_days.value = '0';
check('"any date" brings the undated row back', run().some(x => x.id === 'e'));

// ---- combined -------------------------------------------------------------
// 's' matches "Weekly sync" and "Standup…" but not "CKB kickoff"/"Board review",
// so date and search each have to exclude something for this to come out right
controls.meet_q.value = 's';
controls.meet_days.value = '90';
check('search alone', run().map(x => x.id).join() === 'a,b', run().map(x => x.id));
controls.meet_days.value = '7';
check('search and date combine',
  run().map(x => x.id).join() === 'a,b', run().map(x => x.id));
controls.meet_q.value = 'board'; controls.meet_days.value = '7';
check('date can exclude a search match', run().length === 0, run().map(x => x.id));

// a meeting recorded earlier today, near midnight, must still count as "today"
controls.meet_q.value = ''; controls.meet_days.value = '1';
MEETINGS = [{ id: 'x', name: 'early', ts: t0 + 60 }];
check('midnight-relative, not last-24-hours', run().length === 1);

console.log('\nFAILED:', fail.length ? fail : 'none');
process.exit(fail.length ? 1 : 0);
