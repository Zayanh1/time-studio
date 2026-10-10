const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/index.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
let wall = Date.parse('2026-10-09T12:00:00Z');
class FakeDate extends Date { constructor(...a){super(...(a.length?a:[wall]))} static now(){return wall} }
const elements = new Map();
function el(id){if(!elements.has(id))elements.set(id,{value:'',checked:false,textContent:'',style:{},dataset:{},files:[],classList:{add(){},remove(){},toggle(){},contains(){return false}},setAttribute(){},focus(){},append(){},prepend(){},replaceChildren(){}});return elements.get(id)}
el('mode').value='freeze';el('theme').value='forest';
const ctx=vm.createContext({Date:FakeDate,console,performance:{now:()=>0},setInterval(){},document:{getElementById:el,querySelectorAll:()=>[],addEventListener(){},body:{style:{}}},URL,Blob});
vm.runInContext(source,ctx);
function run(code){return vm.runInContext(code,ctx)}
run("$('datetime').value='2024-02-29T23:59:58';$('offset').value='+05:30';$('seconds').checked=true;apply()");
assert.equal(el('clock-time').textContent,'11:59:58');
const initial=run('clockInstant()');
el('mode').value='live';run('changeMode()');wall+=3000;run('render()');
assert.equal(el('clock-time').textContent,'12:00:01');
assert.match(el('clock-date').textContent,/March 1/);
el('mode').value='freeze';run('changeMode()');wall+=600000;
assert.equal(run('clockInstant()'),initial+3000,'pausing preserves elapsed time');
el('mode').value='live';run('changeMode()');wall+=3600000;
assert.equal(run('clockInstant()'),initial+3603000,'wall elapsed catches up after suspension');
assert.equal(run('selected()'),'2024-02-29T23:59:58+05:30','export remains explicit selected timestamp');
for(const [date,offset] of [['2025-02-29T12:00','+00:00'],['2024-04-31T12:00','+00:00'],['2024-01-01T24:00','+00:00'],['2024-01-01T12:00','+14:01'],['2024-01-01T12:00','-15:00'],['0000-01-01T12:00','+00:00']]){
 assert.throws(()=>run(`parseSelection(${JSON.stringify(date)},${JSON.stringify(offset)})`));
}
assert.equal(run("parseSelection('2024-02-29T12:30',' -14:00 ')"),'2024-02-29T12:30:00-14:00');
run("$('datetime').value='2026-10-09T00:00:00';$('offset').value='-04:00';$('mode').value='freeze';$('hours24').checked=true;apply()");
assert.equal(el('clock-time').textContent,'00:00:00');
run("$('hours24').checked=false;render()");assert.equal(el('clock-time').textContent,'12:00:00');assert.equal(el('clock-ampm').textContent,' AM');
console.log('Clock checks passed: pause/resume, suspension, midnight rollover, explicit export, leap days, offsets, 12/24-hour display.');
