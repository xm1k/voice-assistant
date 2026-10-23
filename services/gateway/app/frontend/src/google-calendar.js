export const GOOGLE_CALENDAR_PATH = '/api/v1/integrations/google-calendar';
export function syncMap(items) { return Object.fromEntries(items.map(item => [item.record_id, item])); }
export function canRetrySync(item) { return item?.status === 'failed' && ['delivery_exhausted', 'failed'].includes(item.code) && Boolean(item.operation_id); }
export function syncLabel(item, connection) {
  if (connection?.status === 'unavailable') return 'Google Calendar: сервис временно недоступен';
  if (!connection?.configured) return 'Google Calendar не настроен';
  if (connection.status === 'reconnect_required') return 'Google Calendar: подключите аккаунт заново';
  if (connection.status !== 'connected') return 'Google Calendar: не подключён';
  if (!item) return 'Только в Голосе';
  if (item.error) return `Google Calendar: ${item.error}`;
  return {pending:'Google Calendar: ожидает синхронизации', synced:'Google Calendar: синхронизировано', conflict:'Google Calendar: конфликт — событие изменено в Google', missing:'Google Calendar: событие удалено в Google', failed:'Google Calendar: ошибка синхронизации', disconnected:'Google Calendar: не подключён'}[item?.status] || 'Только в Голосе';
}
export async function retrySync(api, item) {
  if (!canRetrySync(item)) throw new Error('Повтор доступен только для технической ошибки.');
  return api(`${GOOGLE_CALENDAR_PATH}/operations/${encodeURIComponent(item.operation_id)}/retry`, {method:'POST'});
}
