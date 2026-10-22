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
  return errorMessage(error);
}

const fieldNames = {title:'Название', start_at:'Начало', end_at:'Окончание', remind_at:'Время напоминания', timezone:'Часовой пояс', username:'Логин', password:'Пароль', due_date:'Срок', period_start:'Начало периода', period_end:'Конец периода', work_start:'Начало рабочего интервала', work_end:'Конец рабочего интервала', recurrence:'Повторение'};
const knownMessages = {
  'Invalid credentials':'Неверный логин или пароль.',
  'Username already registered':'Этот логин уже занят.',
  'Session expired':'Сессия истекла. Войдите снова.',
  'Authentication required':'Войдите в аккаунт.',
  'Invalid session':'Войдите в аккаунт снова.',
  'Audio exceeds 25 MB':'Аудиофайл должен быть не больше 25 МБ.',
  'Audio is empty':'Аудиофайл пуст. Запишите аудио заново.',
  'Meeting requires start_at < end_at':'Окончание встречи должно быть позже начала.',
  'Empty title':'Укажите название записи.',
  'Invalid timezone':'Укажите существующий часовой пояс.',
  'work_end must exceed work_start':'Конец рабочего интервала должен быть позже начала.',
};

function safeMessage(value) {
  if (typeof value !== 'string') return '';
  if (knownMessages[value]) return knownMessages[value];
  if (/\b402\b|Payment Required/i.test(value)) return 'Провайдер модели отклонил запрос по оплате. Проверьте баланс и лимит ключа API. Изменения не сохранены.';
  if (!/[а-яё]/i.test(value) || /https?:\/\/|Traceback|validation error|input_value|Bearer|api[_-]?key|sk-[a-z0-9]|[{}]|\btoken\b/i.test(value)) return '';
  return value.slice(0, 2000);
}

export function errorMessage(error, status = error?.status) {
  const detail = error?.detail ?? error;
  const issues = Array.isArray(detail) ? detail : detail?.errors;
  if (Array.isArray(issues) && issues.length) {
    return [...new Set(issues.map(issue => {
      const name = [...(issue.loc || [])].reverse().map(part => fieldNames[part]).find(Boolean);
      const code = issue.code || issue.type;
      const reason = safeMessage(issue.message || issue.msg) || ({missing:'Обязательное поле не заполнено.', string_too_short:'Значение слишком короткое.', string_too_long:'Значение слишком длинное.', extra_forbidden:'Неизвестное поле.', literal_error:'Выберите допустимое значение.'}[code]) || 'Проверьте значение.';
      return name ? `${name}: ${reason}` : reason;
    }))].join(' ');
  }
  const message = safeMessage(typeof detail === 'string' ? detail : detail?.message) || safeMessage(error?.message);
  if (message) return message;
  return ({401:'Не удалось войти. Проверьте данные или войдите снова.', 403:'Недостаточно прав для этого действия.', 404:'Запись больше недоступна.', 409:'Данные изменились. Обновите страницу и повторите действие.', 413:'Файл слишком большой.', 422:'Проверьте заполненные поля.', 429:'Слишком много запросов. Попробуйте немного позже.', 500:'Ошибка сервера. Попробуйте позже.', 502:'Сервис временно недоступен. Попробуйте позже.', 503:'Сервис временно недоступен. Попробуйте позже.', 504:'Сервер не успел ответить. Попробуйте позже.'})[status] || 'Не удалось выполнить действие. Проверьте соединение и попробуйте ещё раз.';
}

export async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  let response;
  try {response = await fetch(path, {...options, headers, credentials:'same-origin'});}
  catch (cause) {throw new Error(errorMessage(cause), {cause});}
  let data;
  try {data = await response.json();}
  catch {if (response.ok) throw new Error('Сервер вернул некорректный ответ. Обновите страницу и попробуйте снова.');}
  if (!response.ok) {
    const error = new Error(errorMessage(data?.detail ?? data, response.status));
    error.status = response.status;
    throw error;
  }
  return data;
}

export function addError(current, exception) {
  const message = errorMessage(exception);
  return current.includes(message) ? current : [...current, message];
}
