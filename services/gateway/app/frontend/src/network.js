export function randomUUID() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c =>
    (c ^ (Math.random() * 16 >> c / 4)).toString(16)
  );
}

export async function collectPages(api, path) {
  const items = [], seen = new Set(); let cursor;
  do {
    const url = new URL(path, 'http://local');
    if (cursor) url.searchParams.set('cursor', cursor);
    const page = await api(url.pathname + url.search);
    if (!Array.isArray(page.items)) throw new Error('Некорректный ответ сервера');
    items.push(...page.items); cursor = page.next_cursor;
    if (cursor && seen.has(cursor)) throw new Error('Repeated pagination cursor');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}
export function mergeMessages(current, incoming) {
  return [...new Map([...current, ...incoming].map(item => [item.id, item])).values()]
    .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || String(a.id).localeCompare(String(b.id)));
}
export function eventGate(limit = 2000) {
  const seen = new Set();
  return id => {if (!id) return true; if (seen.has(id)) return false; seen.add(id); if (seen.size > limit) seen.delete(seen.values().next().value); return true;};
}

export function createHistoryPager(api, onChange, initial = {}) {
  let state = {messages: [], cursor: null, ready: false, loadingOlder: false, revision: 0, ...initial};
  let epoch = 0, latest = null, older = null;
  const notify = () => onChange({...state});
  const merge = items => {
    state.messages = mergeMessages(state.messages, items);
    if (latest?.replace) latest.live.push(...items);
    notify();
  };
  const load = ({older: previous = false, replace = false} = {}) => {
    if (previous && (!state.ready || !state.cursor)) return Promise.resolve();
    if (previous && older) return older.promise;
    if (!previous && latest && (!replace || latest.replace)) return latest.promise;
    if (replace) {epoch++; older = null; state.loadingOlder = false;}
    const request = {epoch, replace, live: [], cursor: previous ? state.cursor : null};
    if (previous) {older = request; state.loadingOlder = true; notify();}
    else latest = request;
    request.promise = (async () => {
      try {
        const params = new URLSearchParams({direction: 'older'});
        if (request.cursor) params.set('cursor', request.cursor);
        const page = await api(`/api/v1/chat/messages?${params}`);
        if (request.epoch !== epoch) return;
        if (!Array.isArray(page.items)) throw new Error('Некорректный ответ истории');
        if (previous && page.next_cursor && page.next_cursor === request.cursor) throw new Error('Repeated pagination cursor');
        state.messages = mergeMessages(replace ? page.items : state.messages, replace ? request.live : page.items);
        if (previous || replace || !state.ready) state.cursor = page.next_cursor;
        state.ready = true;
        if (replace) state.revision++;
        notify();
      } catch (error) {
        if (request.epoch === epoch) throw error;
      } finally {
        if (older === request) {older = null; state.loadingOlder = false; notify();}
        if (latest === request) latest = null;
      }
    })();
    return request.promise;
  };
  return {load, merge, reset() {
    epoch++; latest = null; older = null;
    state = {messages: [], cursor: null, ready: true, loadingOlder: false, revision: state.revision + 1};
    notify();
  }};
}

export function jobErrorMessage(error) {
  if (/\b402\b|Payment Required/i.test(error || '')) return 'Провайдер модели отклонил запрос по оплате. Проверьте баланс и лимит оплаты ключа API у провайдера. Изменения не сохранены.';
  if (/https?:\/\/|validation error|Traceback/i.test(error || '')) return 'Не удалось обработать запрос. Проверьте доступность модели и попробуйте ещё раз. Изменения не сохранены.';
  return error || 'Запрос отменён или завершился ошибкой. Попробуйте ещё раз.';
}
