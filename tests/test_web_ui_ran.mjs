import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source=readFileSync(new URL('../apps/dashboard/index.html',import.meta.url),'utf8');
// Execute the shipped renderer, columns and formatters, not a copied implementation.
const helpers=source.split('\n').filter(line=>/^\s*const (finite|fmt|esc) =/.test(line)).join('\n');
const start=source.indexOf('    const RAN_COLUMNS=');
const end=source.indexOf('    function renderNodeCards',start);
assert.ok(start>=0&&end>start);
const elements={ranState:{},ranCards:{}};
const context=vm.createContext({$:id=>elements[id]});
vm.runInContext(helpers+'\n'+source.slice(start,end),context);
const feed={state:'connected',messages:2,ue_reports:1,age_seconds:0,
  ue_list_age_seconds:0,ue_list_stale:false,ue_list:[{pci:1,rnti:17921,dl_brate:12000}]};
function render(gnbs,legacy=feed){
  context.feeds=gnbs;context.legacy=legacy;
  vm.runInContext('renderRanCards(legacy,feeds)',context);
  return elements.ranCards.innerHTML;
}
let html=render({gnb0:feed,gnb1:feed});
assert.equal(elements.ranState.textContent,'2/2 gNB scheduler feeds fresh');
assert.equal((html.match(/<section /g)||[]).length,2);
assert.equal((html.match(/0x4601/g)||[]).length,2);
assert.ok(html.includes('[&quot;gnb0&quot;,1,17921]'));
assert.ok(html.includes('[&quot;gnb1&quot;,1,17921]'));
const second=html.slice(html.indexOf('<section data-gnb-id="gnb1"'));
html=render({gnb0:{...feed,ue_list_current_connection:false},gnb1:feed});
assert.equal(elements.ranState.textContent,'1/2 gNB scheduler feeds fresh');
assert.ok(html.includes('Connected · waiting for scheduler report'));
assert.ok(html.includes('last reported values, not current ones'));
assert.equal(html.slice(html.indexOf('<section data-gnb-id="gnb1"')),second);
html=render({gnb0:{...feed,state:'disconnected',error:'test outage'},gnb1:feed});
assert.equal(elements.ranState.textContent,'1/2 gNB scheduler feeds fresh');
assert.equal(html.slice(html.indexOf('<section data-gnb-id="gnb1"')),second);
assert.ok(html.includes('last reported values, not current ones'));
html=render({gnb0:{...feed,ue_list:[],ue_list_stale:true,ue_list_age_seconds:6},gnb1:feed});
assert.ok(html.includes('Last scheduler report contained no UEs.'));
assert.ok(html.includes('Connected · scheduler data stale'));
assert.ok(!html.includes('scheduler is reporting no connected UEs.'));
render({gnb0:feed,gnb1:feed});
assert.equal(elements.ranState.textContent,'2/2 gNB scheduler feeds fresh');
html=render(undefined);
assert.equal((html.match(/<section /g)||[]).length,1);
assert.ok(html.includes('12.0 kb/s'));
html=render({'<gnb>':{...feed,ue_list:[]}});
assert.ok(html.includes('&lt;gnb&gt;'));
assert.ok(!html.includes('<h3><gnb>'));
render({gnb0:{state:'subscribed',ue_reports:0,ue_list:[]}});
assert.equal(elements.ranState.textContent,'0/1 gNB scheduler feeds fresh');
assert.ok(elements.ranCards.innerHTML.includes('Waiting for gNB scheduler metrics.'));
console.log('RAN grouping, independent failure/staleness/recovery and legacy rendering passed');
