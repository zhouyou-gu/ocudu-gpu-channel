import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source=readFileSync(new URL('../../apps/dashboard/index.html',import.meta.url),'utf8');
const start=source.indexOf('    function markStatusUnavailable()');
assert.ok(start>=0, 'disconnect handler must exist');
const end=source.indexOf('    async function poll()',start);
let rendered;
const pills={};
const original={ran:{state:'connected',ue_list:[{rnti:1}]},ran_gnbs:{gnb0:{state:'connected',ue_list:[{rnti:1}]},gnb1:{state:'connected',ue_list:[]}},telemetry:{link:{seqno:7}},delivery:{backend_usable:true,control_received:true,
  links:[{link_id:'link',applied:true,usable:true,warming_up:true}]}};
const context={lastStatus:original,render:value=>{rendered=value;},$:id=>id,
  setPill:(id,ok,text)=>{pills[id]={ok,text};}};
vm.runInNewContext(source.slice(start,end)+'\nmarkStatusUnavailable();',context);
assert.equal(rendered.delivery.backend_usable,false);
assert.equal(rendered.delivery.links[0].stale,true);
assert.equal(rendered.delivery.links[0].warming_up,false);
assert.equal(rendered.delivery.control_received,true);
assert.equal(rendered.telemetry.link.seqno,7);
assert.equal(rendered.ran_gnbs.gnb0.state,'disconnected');
assert.equal(rendered.ran_gnbs.gnb1.ue_list_stale,true);
assert.equal(rendered.ran_gnbs.gnb0.ue_list[0].rnti,1);
assert.equal(original.ran_gnbs.gnb0.state,'connected');
assert.equal(original.delivery.backend_usable,true, 'do not mutate prior backend evidence');
assert.equal(pills.backendPill.ok,false);
assert.equal(pills.sionnaPill.ok,false);
console.log('HTTP disconnect clears readiness and preserves historical evidence');

// Status cards must not fetch the raw telemetry history already served by
// the chart endpoint: at 500 Hz this exceeded the status request timeout.
let requestedUrl;
const pollContext={statusInFlight:false,AbortSignal,render:()=>{},markStatusUnavailable:()=>{throw Error('unexpected disconnect');},
  fetch:async url=>{requestedUrl=url;return {ok:true,json:async()=>({})};}};
const pollEnd=source.indexOf('    // Chart-only refresh:',end);
await vm.runInNewContext(source.slice(end,pollEnd)+'\npoll();',pollContext);
assert.equal(requestedUrl,'/api/status?history_ms=0');
assert.equal(pollContext.statusInFlight,false);
console.log('Status polling excludes raw chart history');

// A status-card rebuild must keep the chart response, rather than replacing
// the canvases with the status endpoint's empty history window.
const chartHistory={telemetry:[{link_id:'gnb0>ue0:serving',observed_unix_ms:123}]};
let drawnHistory;
const runtimeElements={};
const runtimeContext={lastRealtime:{history:chartHistory,gpu_history:[]},lastStatus:{history:{telemetry:[]}},
  $:id=>(runtimeElements[id]??={}),fmt:()=>'-',fmtMib:()=>'-',fmtRate:()=>'-',
  renderHardware:()=>'',renderGpuProcess:()=>'',renderMemorySummary:()=>'',
  drawResourceHistory:(_usage,history)=>{drawnHistory=history;}};
const redrawStart=source.indexOf('    function redrawCharts()');
const redrawEnd=source.indexOf('    // Delegated',redrawStart);
const runtimeStart=source.indexOf('    function renderRuntime(');
const runtimeEnd=source.indexOf('    // Node colours',runtimeStart);
vm.runInNewContext(source.slice(redrawStart,redrawEnd)+source.slice(runtimeStart,runtimeEnd)+
  '\nrenderRuntime(null,{}, {}, {links:[]}, {telemetry:[]}, null);',runtimeContext);
assert.equal(drawnHistory,chartHistory);
console.log('Status-card rebuild preserves chart history');
