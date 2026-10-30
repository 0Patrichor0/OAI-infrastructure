const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8').split('/* ---------------------------------------------------------------- 启动 */')[0];
function fixture() {
  const elements = new Map(), sources = [], timers = new Map();
  const payload = {items: [], db: {}, cfg: {}, nodes: [], ok: true, slices: [], ues: [], smf: [], amf: [], totals: {}, sbi: {}, units_ok: 8};
  let next = 1;
  const context = vm.createContext({
    document: {querySelector(key) {
      if (!elements.has(key)) elements.set(key, {value:'', innerHTML: '', textContent: '', style: {}, className: '', scrollHeight: 0, classList: {contains: () => false}, querySelectorAll:()=>[]});
      return elements.get(key);
    }, createElement: () => ({})},
    EventSource: class {constructor(url) {this.url=url;this.closed=false;sources.push(this);}close(){this.closed=true;}},
    fetch: async () => ({ok: true, status: 200, json: async () => ({...payload, id: 'aaaaaaaaaaaa'})}),
    localStorage: {setItem(){},getItem(){return null;}},
    setTimeout(fn) {const id=next++;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    setInterval(){}, AbortController, console, alert(){}, confirm(){return true;}, Promise,
  });
  vm.runInContext(source, context);
  return {context, elements, sources, timers, payload};
}
async function tick(){for(let i=0;i<4;i++)await new Promise(setImmediate);}
(async()=>{
  let f=fixture();
  await vm.runInContext('runJob("/api/persist/apply", "test", {text:"fixture"})',f.context);
  assert.equal(f.sources.length,1);
  f.sources[0].onerror();
  assert.equal(f.sources[0].closed,false,'temporary stream disconnect must not abandon job');
  console.log('PASS: job survives stream disconnect');

  f=fixture();
  f.context.fetch=async()=>({ok:false,status:400,json:async()=>({id:'bbbbbbbbbbbb',error:'fixture validation failed'})});
  await vm.runInContext('runJob("/api/persist/apply", "test", {})',f.context);
  assert.equal(f.sources.length,0,'HTTP failure must not create a tracked job');
  assert.match(f.elements.get('#apply-hint').textContent,/validation failed/);
  console.log('PASS: HTTP failure surfaced without false job');

  f=fixture();let calls=0, release;
  const first=new Promise(resolve=>{release=resolve;});
  f.context.fetch=async()=>{calls++;if(calls===1)await first;return {ok:true,status:200,json:async()=>({units_ok:8,upf_workers:'1,2',ts:'fixture'})};};
  vm.runInContext('refreshSystem(); refreshSystem();',f.context);
  assert.equal(calls,1,'polling must not overlap requests for one section');
  release();await tick();assert.equal(calls,2,'refresh requested during a request must run afterwards');
  console.log('PASS: refreshes serialized and follow-up retained');

  f=fixture();
  f.payload.slices=[{sst:3,sd:'000003',network_ready:true},{sst:1,sd:'000123',network_ready:false}];
  f.payload.ues=[{imsi:'208950000000097',slice:{sst:3,sd:'000003'},has_auth:true,has_subscription:true,is_active_ue:true,can_add_node:false,status_reason:'已在线'}];
  f.payload.suggested_imsi='208950000000132';
  await vm.runInContext('refreshSlices();',f.context);
  assert.match(f.elements.get('#ue-slice-choice').innerHTML,/3\/000003/);
  assert.doesNotMatch(f.elements.get('#ue-slice-choice').innerHTML,/1\/000123/);
  vm.runInContext('bindSliceForms(); runJob = async (url,label,body)=>{globalThis.sent={url,body};};',f.context);
  f.elements.get('#btn-new-imsi').onclick();
  assert.equal(f.elements.get('#ue-imsi').value,'208950000000132');
  assert.equal(f.elements.get('#ue-sd').value,'000003');
  f.context.document.querySelector('#ue-5qi').value='6';f.context.document.querySelector('#ue-ambr').value='37Mbps';
  await f.elements.get('#btn-add-ue').onclick();
  assert.equal(f.context.sent.url,'/api/ues');
  assert.equal(f.context.sent.body.fiveQI,6);assert.equal(f.context.sent.body.ambr_dl,'37Mbps');
  f.context.sent=null;f.elements.get('#ue-imsi').value='208950000000097';
  await f.elements.get('#btn-add-ue').onclick();
  assert.equal(f.context.sent,null);assert.match(f.elements.get('#apply-hint').textContent,/已在线/);
  f.context.document.querySelector('#slice-imsi').value='';f.elements.get('#btn-act-ue').onclick();
  assert.equal(f.context.sent,null,'switch must not silently reuse add-UE IMSI');
  console.log('PASS: ready-slice choices, unused IMSI, QoS, online identity guard, independent switch form');
  f.context.sent=null;
  f.context.document.querySelector('#ran-mcs-value').value='20';
  f.elements.get('#btn-ran-mcs-set').onclick();
  assert.equal(f.context.sent.url,'/api/ran/mcs');assert.equal(f.context.sent.body.max_mcs,20);
  f.context.sent=null;f.elements.get('#ran-mcs-value').value='20.5';
  f.elements.get('#btn-ran-mcs-set').onclick();assert.equal(f.context.sent,null);
  f.payload.state={pid:123,dl_max_mcs:20,dl_min_mcs:0,revision:1};
  await f.elements.get('#btn-ran-mcs-read').onclick();
  assert.match(f.elements.get('#ran-mcs-hint').textContent,/PID 123/);
  assert.match(f.elements.get('#ran-mcs-hint').textContent,/上限 20/);
  console.log('PASS: E2 MCS form sends integer parameter, rejects fraction, and displays DU readback');
  assert.match(f.elements.get('#prb-slice-choice').innerHTML,/3\/000003/);
  assert.doesNotMatch(f.elements.get('#prb-slice-choice').innerHTML,/1\/000123/);
  f.context.document.querySelector('#prb-slice-choice').value='3/000003';
  f.context.document.querySelector('#prb-percent').value='30';
  f.context.sent=null;f.elements.get('#btn-prb-set').onclick();
  assert.equal(f.context.sent.url,'/api/ran/prb');
  assert.deepEqual(JSON.parse(JSON.stringify(f.context.sent.body)),{sst:3,sd:'000003',enabled:true,max_prb_percent:30});
  f.context.sent=null;f.elements.get('#prb-percent').value='30.5';
  f.elements.get('#btn-prb-set').onclick();assert.equal(f.context.sent,null);
  f.elements.get('#btn-prb-clear').onclick();assert.equal(f.context.sent.body.enabled,false);
  assert.equal(f.context.sent.body.max_prb_percent,undefined);
  f.payload.state={pid:123,enabled:true,max_prb_percent:30,limit_prbs:31,bwp_prbs:106,matched_users:2,peak_new_prbs:31,violations:0};
  await f.elements.get('#btn-prb-read').onclick();
  assert.match(f.elements.get('#prb-hint').textContent,/31\/106 PRB/);
  assert.match(f.elements.get('#prb-hint').textContent,/同切片 UE 2/);
  console.log('PASS: PRB slice selection, integer ceiling, clear action, and live counters');
  f=fixture();let resolveRead;
  f.context.fetch=async()=>({ok:true,status:200,json:()=>new Promise(resolve=>{resolveRead=resolve;})});
  f.context.document.querySelector('#prb-slice-choice').value='3/000003';
  vm.runInContext('readPrbPolicy();',f.context);await tick();
  f.elements.get('#prb-slice-choice').value='1/000005';
  f.context.document.querySelector('#prb-hint').textContent='new selection';
  resolveRead({state:{pid:123,enabled:true,max_prb_percent:30}});await tick();
  assert.equal(f.elements.get('#prb-hint').textContent,'new selection');
  console.log('PASS: delayed PRB read cannot overwrite another selected slice');
})().catch(error=>{console.error(error);process.exitCode=1;});
