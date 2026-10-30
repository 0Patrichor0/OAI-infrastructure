const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8').split('/* ---------------------------------------------------------------- 启动 */')[0];
function fixture(){
  const elements=new Map(),requests=[],sources=[];
  const el=k=>{if(!elements.has(k))elements.set(k,{value:'',textContent:'',innerHTML:'',disabled:false,style:{},classList:{add(){},remove(){},contains(){return false;}}});return elements.get(k);};
  const ctx=vm.createContext({document:{querySelector:el},Date,Promise,AbortController,console,
    setTimeout(){return 1;},clearTimeout(){},localStorage:{setItem(){},getItem(){return null;}},
    EventSource:class{constructor(url){sources.push(this);}close(){}},
    fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options?.body||'{}')});return {ok:true,status:200,json:async()=>({id:'job'})};}});
  vm.runInContext(source,ctx);return {ctx,el,requests,sources};
}
function preview(valid=true){return {valid,ok:true,token:valid?'ticket':null,expires_at:Date.now()/1000+180,sampled_at:Date.now()/1000,
  intent:{action:'set_ran_dl_mcs'},changes:[{label:'MCS',current:28,target:20}],checks:[{check:'DU 下限',ok:valid,detail:'<unsafe>'}],
  affected_ues:[{node:'ue1',imsi:'208950000000097',ip:'12.1.1.2'}],warnings:['影响全小区'],plan:['E2 热更新']};}
async function tick(){for(let i=0;i<4;i++)await new Promise(setImmediate);}
(async()=>{
  let f=fixture();f.ctx.fetch=async(url,options)=>{f.requests.push({url,body:JSON.parse(options?.body||'{}')});return {ok:true,json:async()=>url==='/api/preflight'?preview():{id:'job'}};};
  await vm.runInContext('runJob("/api/apply","test",{text:"MCS20"})',f.ctx);
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].url,'/api/preflight');assert.equal(f.sources.length,0);
  assert.match(f.el('#preview-body').innerHTML,/>28</);assert.match(f.el('#preview-body').innerHTML,/>20</);
  assert.match(f.el('#preview-body').innerHTML,/208950000000097/);assert.doesNotMatch(f.el('#preview-body').innerHTML,/<unsafe>/);
  await vm.runInContext('confirmPreview()',f.ctx);assert.equal(f.requests[1].body.preflight_token,'ticket');assert.equal(f.sources.length,1);
  await vm.runInContext('confirmPreview()',f.ctx);assert.equal(f.requests.length,2,'double click must not submit twice');
  console.log('PASS preview before write; current/target/UE shown; escaped details; exact ticket sent once');
  f=fixture();f.ctx.fetch=async()=>({ok:true,json:async()=>preview(false)});
  await vm.runInContext('runJob("/api/ran/mcs","test",{max_mcs:2});',f.ctx);
  assert.equal(f.el('#btn-preview-confirm').disabled,true);await vm.runInContext('confirmPreview()',f.ctx);assert.equal(f.sources.length,0);
  console.log('PASS failed checks block submission');
  f=fixture();let release;f.ctx.fetch=()=>new Promise(r=>release=r);
  const pending=vm.runInContext('previewJob("/api/ran/mcs","test",{max_mcs:20})',f.ctx);
  vm.runInContext('previewSequence++;preparedSubmission=null;',f.ctx);
  release({ok:true,json:async()=>preview()});await pending;
  assert.equal(f.el('#btn-preview-confirm').disabled,true);
  console.log('PASS cancelled preview cannot become executable after delayed response');
  f=fixture();vm.runInContext(`globalThis.view=effectsHTML({before:{ues:[{node:'ue1',imsi:'123',dl_mbps:null,rtt_ms:null}]},after:{ues:[{node:'ue1',imsi:'123',dl_mbps:0.5,rtt_ms:8}]}})`,f.ctx);
  assert.match(f.ctx.view,/未获取/);assert.match(f.ctx.view,/>0.5</);assert.match(f.ctx.view,/不代表最大容量/);
  console.log('PASS missing metrics remain unavailable and measured data displayed');
})().catch(e=>{console.error(e);process.exitCode=1;});
