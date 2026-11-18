import test from 'node:test';
import assert from 'node:assert/strict';
import { scannerCandidates, isObdCandidate, connectionLabel } from '../src/features/settings/obd_model.js';

test('registration or BLE connection alone never claims data reception', () => {
  assert.equal(connectionLabel({registered: true, state: 'receiving', fresh: false}), 'waiting');
  assert.equal(connectionLabel({registered: true, state: 'waiting_data', fresh: true}), 'waiting');
  assert.equal(connectionLabel({registered: true, state: 'receiving', fresh: true}), 'receiving');
  assert.equal(connectionLabel({registered: false, state: 'receiving', fresh: true}), 'not_registered');
  assert.equal(connectionLabel({configError: true}), 'configuration_error');
});
test('ambiguous scanners remain separate and unknown names remain selectable', () => {
  const a={address:'A',name:'OBD',rssi:-40}, b={address:'B',name:'OBD',rssi:-70}, c={address:'C',name:'Unlisted',rssi:-10};
  const input=[c,a,b];
  assert.deepEqual(scannerCandidates(input,'B'),[b,a,c]);
  assert.deepEqual(input,[c,a,b]);
  assert.equal(isObdCandidate(c),false);
  assert.equal(isObdCandidate({uuids:['E7810A71-73AE-499D-8C15-FAA9AEF0C3F2']}),true);
});
