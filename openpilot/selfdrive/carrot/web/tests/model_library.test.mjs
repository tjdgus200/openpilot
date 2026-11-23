import assert from 'node:assert/strict';
import test from 'node:test';
import { renderLibrary } from '../src/features/tools/model_library.js';
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.events = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelector(tag) { return this.children.find(e => e.tag === tag) || this.children.map(e => e.querySelector?.(tag)).find(Boolean); }
}
function setupDocument() {
  const elements = new Map();
  for (const id of ['externalModelLibrary', 'modelLibraryDialog', 'modelLibraryEditor',
    'modelLibraryTitle', 'btnCloseModelLibrary', 'egpuModelCard', 'egpuModelState', 'egpuModelAmount']) {
    elements.set(id, new Element('div'));
  }
  elements.get('egpuModelCard').hidden = true;
  globalThis.document = { getElementById: id => elements.get(id), createElement: tag => new Element(tag) };
  return { root: elements.get('externalModelLibrary'), editor: elements.get('modelLibraryEditor'), elements };
}
const data = { effective: 'n16', selected: 'flip-flop', reboot_required: true, models: [
  { id: 'n16', name: 'N16 Nav', noo: true, hidden: false, protected: true, egpu_bytes: 100, jetson_bytes: null },
  { id: 'flip-flop', name: 'Flip Flop', noo: false, hidden: false, protected: true, egpu_bytes: 200, jetson_bytes: 300 },
] };
test('model choice distinguishes current/pending/NOO and preserves in-flight choice across polls', t => {
  const old = globalThis.document;
  t.after(() => { globalThis.document = old; });
  const { root, editor, elements } = setupDocument();
  renderLibrary(data);
  const select = editor.querySelector('select');
  assert.equal(select.value, 'flip-flop');
  assert.deepEqual(select.children.map(x => x.value), ['n16', 'flip-flop']);
  assert.match(select.children[0].textContent, /NOO supported/);
  assert.match(select.children[1].textContent, /NOO not supported/);
  assert.match(root.querySelector('p').textContent, /This boot N16/);
  assert.equal(root.querySelector('strong').textContent, 'Flip Flop');
  assert.equal(root.querySelector('select'), undefined);
  assert.equal(root.querySelector('button').textContent, 'Change model');
  assert.equal(root.children.length, 2);
  root.querySelector('button').events.click();
  assert.equal(elements.get('modelLibraryDialog').open, true);
  elements.get('btnCloseModelLibrary').events.click();
  assert.equal(elements.get('modelLibraryDialog').open, false);
  assert.equal(root.children.some(x => x.className === 'model-library__row'), false);
  select.value = 'n16';
  select.disabled = true;
  renderLibrary({ ...data, jetson_inventory_fresh: true });
  assert.equal(editor.querySelector('select').value, 'n16');
  renderLibrary(data, true);
  assert.equal(editor.querySelector('select').value, 'flip-flop');
});

test('selection alone saves next boot and exposes no deletion action', async t => {
  const old = { document: globalThis.document, postJson: globalThis.postJson };
  t.after(() => Object.assign(globalThis, old));
  const { root, editor, elements } = setupDocument();
  const calls = [];
  globalThis.postJson = async (url, body) => { calls.push({url, body}); return {library: {...data, selected: body.model}}; };
  renderLibrary(data, true);
  const select = editor.querySelector('select');
  select.value = 'n16';
  await select.events.change();
  assert.deepEqual(calls, [{url: '/api/egpu/model/library', body: {action:'select',model:'n16'}}]);
  assert.equal(root.querySelector('button').textContent, 'Change model');
  assert.equal(editor.querySelector('select').value, 'n16');
});

test('failed selection restores the saved choice', async t => {
  const old = { document: globalThis.document, postJson: globalThis.postJson };
  t.after(() => Object.assign(globalThis, old));
  const { root, editor, elements } = setupDocument();
  globalThis.postJson = async () => { throw new Error('offline'); };
  renderLibrary(data, true);
  const select = editor.querySelector('select');
  select.value = 'n16';
  await select.events.change();
  assert.equal(editor.querySelector('select').value, 'flip-flop');
  assert.ok(!editor.querySelector('select').disabled);
});


test('status updates preserve the open dialog and focused selection across inventory polls', t => {
  const old = globalThis.document;
  t.after(() => { globalThis.document = old; });
  const { root, editor, elements } = setupDocument();
  const card = elements.get('egpuModelCard');
  card.hidden = false;
  card.dataset.state = 'downloading';
  elements.get('egpuModelState').textContent = 'Downloading';
  elements.get('egpuModelAmount').textContent = '25%';
  renderLibrary({ ...data, selected: 'n16', reboot_required: false });
  root.querySelector('button').events.click();
  const button = root.querySelector('button');
  const select = editor.querySelector('select');
  assert.equal(root.querySelector('p').textContent, 'Downloading · 25%');
  card.dataset.state = 'error';
  elements.get('egpuModelState').textContent = 'eGPU unavailable';
  elements.get('egpuModelAmount').textContent = '';
  renderLibrary({ ...data, selected: 'n16', reboot_required: false, jetson_inventory_fresh: true });
  assert.equal(editor.querySelector('select'), select);
  assert.equal(root.querySelector('button'), button);
  assert.equal(elements.get('modelLibraryDialog').open, true);
  assert.equal(root.dataset.state, 'error');
  assert.equal(root.querySelector('p').textContent, 'eGPU unavailable');
});

const internalModel = {
  models: [{ id: 'cd210', name: 'CD210' }, { id: 'polaris-vega-2', name: 'Polaris Vega 2' }],
  selected: 'cd210', effective: 'cd210', reboot_required: false, state: 'ready',
};

test('PV5 distinguishes internal readiness and next-boot choice', t => {
  const old = globalThis.document;
  t.after(() => { globalThis.document = old; });
  const { editor, elements } = setupDocument();
  const internal = { ...internalModel, selected: 'polaris-vega-2', reboot_required: true, state: 'preparing' };
  renderLibrary({ ...data, internal_model: internal });
  const choices = editor.children.filter(x => x.tag === 'label').map(label => label.children[0]);
  assert.deepEqual(choices.map(select => select.value), ['flip-flop', 'polaris-vega-2']);
  assert.deepEqual(choices[1].children.map(option => option.textContent), ['CD210', 'Polaris Vega 2']);
  assert.equal(elements.get('modelLibraryTitle').textContent, 'Driving models');
  const copy = editor.children.map(x => x.textContent);
  assert.ok(copy.includes('This boot CD210'));
  assert.ok(copy.includes('Preparing the internal model'));
  assert.ok(copy.includes('Saved. Reboot comma to apply.'));

  renderLibrary({ ...data, internal_model: { ...internal, reboot_required: false, state: 'fallback' } });
  assert.ok(editor.children.some(x => x.textContent === 'Using CD210 as the fallback'));
  assert.ok(!editor.children.some(x => x.textContent === 'Saved. Reboot comma to apply.'));
  renderLibrary(data);
  assert.equal(editor.children.filter(x => x.tag === 'label').length, 1);
  assert.equal(elements.get('modelLibraryTitle').textContent, 'External driving model');
});

test('internal saving protects both choices from polls and sends only the internal selection', async t => {
  const old = { document: globalThis.document, postJson: globalThis.postJson, showAppToast: globalThis.showAppToast };
  t.after(() => Object.assign(globalThis, old));
  const { editor } = setupDocument();
  const posted = [];
  const toasts = [];
  let respond;
  globalThis.postJson = (url, body) => {
    posted.push({url, body});
    return new Promise(resolve => { respond = resolve; });
  };
  globalThis.showAppToast = message => toasts.push(message);
  const library = { ...data, internal_model: internalModel };
  renderLibrary(library);
  const [external, internal] = editor.children.filter(x => x.tag === 'label').map(label => label.children[0]);
  internal.value = 'polaris-vega-2';
  const pending = internal.events.change();
  assert.ok(external.disabled && internal.disabled);
  renderLibrary({ ...library, selected: 'n16' });
  assert.equal(editor.querySelector('select'), external);
  assert.equal(internal.value, 'polaris-vega-2');
  await external.events.change();
  assert.deepEqual(posted, [{
    url: '/api/egpu/model/library', body: { action: 'select_internal', model: 'polaris-vega-2' },
  }]);
  respond({ library: { ...library, internal_model: { ...internalModel, selected: 'polaris-vega-2', reboot_required: true } } });
  await pending;
  const choices = editor.children.filter(x => x.tag === 'label').map(label => label.children[0]);
  assert.deepEqual(choices.map(select => select.value), ['flip-flop', 'polaris-vega-2']);
  assert.ok(choices.every(select => !select.disabled));
  assert.deepEqual(toasts, ['Saved. Reboot comma to apply.']);
});
