/*
 * The page's own cost estimate, pulled out of notula_ui.html so it can't drift
 * from what the dialog shows. Python has the same arithmetic in cloud.estimate;
 * the two must agree, and test_cloud.py pins the Python side.
 *
 *     node tests/test_cloudest.js
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'assets', 'notula_ui.html'), 'utf8');

function pull(name) {
  const m = html.match(new RegExp('function ' + name + '\\([^)]*\\)\\{[\\s\\S]*?\\n\\}'));
  if (!m) { console.error(`FAIL  could not find ${name}() in the page`); process.exit(1); }
  return m[0];
}
const src = ['estimateUsd', 'fmtUsd', 'fmtClock', 'fmtRate', 'pendingSeconds', 'etaSeconds'].map(pull).join('\n');
const fns = new Function(src + '; return {estimateUsd, fmtUsd, fmtClock, fmtRate, pendingSeconds, etaSeconds};')();

let fail = [];
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}  ${detail === undefined ? '' : detail}`);
  if (!cond) fail.push(name);
}

check('an hour at $0.006/min is $0.36', Math.abs(fns.estimateUsd(3600, 0.006) - 0.36) < 1e-9);
check('seconds round up, like the API bills', Math.abs(fns.estimateUsd(59.2, 0.003) - 0.003) < 1e-9);
check('nothing costs nothing', fns.estimateUsd(0, 0.006) === 0);
check('garbage length is treated as zero', fns.estimateUsd('abc', 0.006) === 0);

check('cents, not floats', fns.fmtUsd(0.36) === '$0.36', fns.fmtUsd(0.36));
check('a tiny clip', fns.fmtUsd(0.004) === 'less than $0.01', fns.fmtUsd(0.004));
check('zero', fns.fmtUsd(0) === '$0.00');
check('thousands separator', fns.fmtUsd(1234.5) === '$1,234.50', fns.fmtUsd(1234.5));

check('clock under an hour', fns.fmtClock(754) === '12:34', fns.fmtClock(754));
check('clock over an hour', fns.fmtClock(3661) === '1:01:01', fns.fmtClock(3661));
check('clock rounds', fns.fmtClock(59.6) === '1:00', fns.fmtClock(59.6));

check('rate: three decimals', fns.fmtRate(0.006) === '$0.006', fns.fmtRate(0.006));
check('rate: four decimals when needed', fns.fmtRate(0.0045) === '$0.0045', fns.fmtRate(0.0045));
check('rate: whole cents', fns.fmtRate(0.01) === '$0.01', fns.fmtRate(0.01));

// saved parts only count for the model, language and part length they were made with
const R = {model: 'd', lang: 'id', chunk_s: 300, done_s: 1800};
check('saved parts are not priced again', fns.pendingSeconds(5842, R, 'd', ' ID ', 300) === 4042);
check('...but not for another model', fns.pendingSeconds(5842, R, 'p', 'id', 300) === 5842);
check('...or another language', fns.pendingSeconds(5842, R, 'd', 'en', 300) === 5842);
check('...or another part length', fns.pendingSeconds(5842, R, 'd', 'id', 600) === 5842);
check('no saved parts sends everything', fns.pendingSeconds(600, null, 'd', 'id', 300) === 600);
check('never below zero', fns.pendingSeconds(100, R, 'd', 'id', 300) === 0);

// mirrors cloud.eta_seconds (13.1 min for a 5842 s meeting on diarize)
const eta = fns.etaSeconds(5842, 0.42, true, 300, 4) / 60;
check('eta: a 97-minute meeting, diarized, four at a time', Math.abs(eta - 13.1) < 0.05, eta.toFixed(2));
check('eta: nothing takes no time', fns.etaSeconds(0, 0.42, true, 300, 4) === 0);
const seq = fns.etaSeconds(5842, 0.42, true, 300, 1) / 60;
check('eta: one part at a time takes every part in turn', Math.abs(seq - 20 * 131 / 60) < 0.05, seq.toFixed(2));

console.log();
console.log('FAILED:', fail.length ? fail : 'none');
process.exit(fail.length ? 1 : 0);
