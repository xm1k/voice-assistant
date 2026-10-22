import {randomUUID} from './network.js';
function recordScope(record) {
  return record.recurrence ? {scope: record.occurrence_at ? 'occurrence' : 'series', ...(record.occurrence_at ? {occurrence_at: record.occurrence_at} : {})} : {};
}

export function completeRecord(api, record) {
  return api(`/api/v1/records/${encodeURIComponent(record.id)}`, {
    method: 'PATCH', headers: {'Idempotency-Key': randomUUID()},
    body: JSON.stringify({fields: {status: 'completed'}, version: record.version, ...recordScope(record)}),
  });
}

export function deleteRecord(api, record) {
  const params = new URLSearchParams(recordScope(record));
  if (record.version) params.set('version', record.version);
  return api(`/api/v1/records/${encodeURIComponent(record.id)}?${params}`, {
    method: 'DELETE', headers: {'Idempotency-Key': randomUUID()},
  });
}
