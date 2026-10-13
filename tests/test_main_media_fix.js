// Lightweight behavioral tests; no browser, network or real uploads.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(require('node:path').join(__dirname, '../templates/index.html'), 'utf8');
const names = ['_startMediaUpload', '_hasMediaUploadFailures', '_ensureMediaReady',
  '_payloadContainsInlineMedia', '_drainDraftSaves', 'saveDraft', 'generatePDF'];
const functions = names.map(name => {
  const match = html.match(new RegExp('^(?:async )?function ' + name + '\\(.*?^}', 'ms'));
  assert.ok(match, name);
  return match[0];
}).join('\n');
let requests = [];
let reply;
const fields = {'.photo-data': {value: 'data:image/jpeg;base64,YQ=='},
  '.photo-filename': {value: ''}, '.photo-retry': {style: {display: 'none'}}};
const box = {dataset: {}, querySelector: key => fields[key]};
const context = vm.createContext({
  console, Blob, FormData, AbortController, setTimeout, clearTimeout,
  document: {querySelectorAll: () => [box]},
  dataUrlToBlob: () => new Blob(['image'], {type: 'image/jpeg'}),
  toast() {}, showAutosaveStatus() {}, _hideProgress() {}, _setProgress() {},
  validateForm: () => true,
  collectData: () => ({areas: [{photos: [{img_data: fields['.photo-data'].value,
    photo_filename: fields['.photo-filename'].value}]}]}),
  fetch: async (...args) => {requests.push(args); return reply;},
});
vm.runInContext(`let _pendingUploads = 0, _mediaUploadSequence = 0;
  let _draftSaveDeferredForMedia = false, _draftSaveInFlight = false;
  let _draftSavePending = null, _draftSavePromise = Promise.resolve(true);
  let _pdfGenerationInFlight = false;
  ${functions}`, context);
async function run() {
  reply = {ok: false, status: 500, json: async () => ({error: 'Upload failed'})};
  const original = fields['.photo-data'].value;
  assert.equal(await context._startMediaUpload(box, original, 'photo'), false);
  assert.equal(box.dataset.uploadState, 'failed');
  assert.equal(fields['.photo-data'].value, original, 'failed photo retained');
  assert.equal(fields['.photo-retry'].style.display, '');
  const count = requests.length;
  await context.generatePDF();
  assert.equal(await context.saveDraft(), false);
  assert.equal(requests.length, count, 'failed upload blocks generate and draft requests');
  reply = {ok: true, json: async () => ({ok: true, photo_filename: 'stored.jpg'})};
  assert.equal(await context._startMediaUpload(box, original, 'photo'), true);
  assert.equal(box.dataset.uploadState, 'ready');
  assert.equal(fields['.photo-data'].value, '');
  assert.equal(fields['.photo-filename'].value, 'stored.jpg');
  assert.equal(await context._ensureMediaReady('generate'), true);
  // A late response must not resurrect a photo the user replaced/cleared.
  let resolveUpload;
  context.fetch = () => new Promise(resolve => {resolveUpload = resolve;});
  const pending = context._startMediaUpload(box, original, 'photo');
  box.dataset.uploadToken = 'replacement';
  fields['.photo-filename'].value = 'replacement.jpg';
  resolveUpload(reply);
  assert.equal(await pending, false);
  assert.equal(fields['.photo-filename'].value, 'replacement.jpg');
  console.log('PASS: failed photo retained, generate/save blocked, retry succeeds, stale response ignored');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
