// Lightweight DOM checks; no browser or server required.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../templates/reports.html'), 'utf8');
const script = html.slice(html.indexOf('function groupTimesheetWarnings('), html.indexOf('function workforceSourceLabel('));
function element() {
  return {children:[], style:{}, classList:{toggle(){}}, appendChild(child){this.children.push(child);},
    replaceChildren(){this.children=[];}};
}
const box = element();
const context = vm.createContext({document:{getElementById:()=>box, createElement:element, createTextNode:text=>({textContent:text})}});
vm.runInContext(script, context);
const result = {source_manifest:[{source_id:'a',filename:'Attendance.xlsx'}], warnings:[], unresolved:[], identity_matches:[]};
for (const date of ['2026-08-01','2026-08-02']) {
  const details = {employee:'Anggi',date,statuses:['sick','nonpresent'],observations:[{source_id:'a',sheet:'Sheet1',row:132,status:'sick'}]};
  result.warnings.push({code:'employee_date_status_conflict',message:'Conflict',details});
  result.unresolved.push({type:'employee_date_status_conflict',...details});
  result.identity_matches.push({daily_name:'M. Ali',timesheet_name:'Muhammad Ali',date,method:'name_variant'});
}
result.warnings.push({code:'missing_role',message:'Missing position',details:{employee:'Amran',sheet:'Sheet1',row:137}});
result.unresolved.push({type:'missing_role',employee:'Amran',sheet:'Sheet1',row:137});
result.warnings.push({code:'matched_name_variants',message:'Unique variants matched'});
result.warnings.push({code:'unknown_code',message:'<img src=x onerror=alert(1)>'});
const original = JSON.stringify(result);
const groups = context.groupTimesheetWarnings(result);
const conflicts = groups.find(g=>g.key==='attendance').items;
assert.equal(conflicts.length,1);
assert.equal(conflicts[0].dates.length,2);
assert(conflicts[0].refs.some(r=>r.includes('Attendance.xlsx / Sheet1 / row 132')));
assert.equal(groups.find(g=>g.key==='details').items.length,1);
assert.equal(groups.find(g=>g.key==='matches').items.length,1);
context.renderTimesheetWarnings(result);
assert(box.children.length > 1);
function walk(node) {
  assert.equal(Object.hasOwn(node,'innerHTML'),false);
  return [node.textContent || '', ...(node.children || []).flatMap(walk)];
}
assert(walk(box).includes('<img src=x onerror=alert(1)>'));
assert.equal(JSON.stringify(result),original);
context.renderTimesheetWarnings(null);
assert.equal(box.children.length,0);
console.log('Timesheet warning grouping, deduplication, source references, safe rendering and unchanged input: passed.');
