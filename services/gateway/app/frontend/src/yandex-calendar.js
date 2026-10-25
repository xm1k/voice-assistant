export const YANDEX_CALENDAR_PATH = '/api/v1/integrations/yandex-calendar';
export function syncMap(items) { return Object.fromEntries(items.map(item => [item.record_id, item])); }
export function canRetrySync(item) { return item?.status === 'failed' && ['delivery_exhausted', 'failed'].includes(item.code) && Boolean(item.operation_id); }
export function syncLabel(item, connection) {
  if (connection?.status === 'unavailable') return 'Яндекс Календарь: сервис временно недоступен';
  if (!connection?.configured) return 'Яндекс Календарь не настроен';
  if (connection.status === 'reconnect_required') return 'Яндекс Календарь: подключите аккаунт заново';
  if (connection.status !== 'connected') return 'Яндекс Календарь: не подключён';
  if (!item) return 'Только в Голосе';
  if (item.error) return `Яндекс Календарь: ${item.error}`;
  return {pending:'Яндекс Календарь: ожидает синхронизации', synced:'Яндекс Календарь: синхронизировано', conflict:'Яндекс Календарь: конфликт — событие изменено в Яндексе', missing:'Яндекс Календарь: событие удалено в Яндексе', failed:'Яндекс Календарь: ошибка синхронизации', disconnected:'Яндекс Календарь: не подключён'}[item?.status] || 'Только в Голосе';
}
export async function retrySync(api, item) {
  if (!canRetrySync(item)) throw new Error('Повтор доступен только для технической ошибки.');
  return api(`${YANDEX_CALENDAR_PATH}/operations/${encodeURIComponent(item.operation_id)}/retry`, {method:'POST'});
}

export function credentialsAllowed(location) { return location.protocol === 'https:' || location.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(location.hostname); }
export async function discoverCalendars(api, credentials, secure, clearPassword) {
  try {
    if (!secure) throw new Error('HTTPS required');
    const pending = api(`${YANDEX_CALENDAR_PATH}/discover`, {method:'POST',body:JSON.stringify(credentials)});
    clearPassword();
    return await pending;
  } finally {clearPassword();}
}
