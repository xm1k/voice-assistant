import {addDays, isoDate, startOfWeek, zonedIso} from './dates.js';

export function createMockStore(now = new Date()) {
  const week = startOfWeek(now);
  const date = day => isoDate(addDays(week, day));
  const time = (day, hour) => zonedIso(date(day), hour, 'Europe/Moscow');
  let records = [
    ...[
      [0, '10:00', '11:00', 'Планы команды на неделю'],
      [1, '11:00', '12:00', 'Обсуждение нового проекта'],
      [2, '14:00', '15:00', 'Встреча с Алексеем'],
      [3, '10:00', '11:30', 'Презентация для клиента'],
      [4, '12:00', '13:00', 'Итоги недели'],
      [6, '11:00', '12:00', 'Кофе с командой'],
    ].map(([day, start, end, title], i) => ({id: `meeting-${i}`, kind: 'meeting', title, start_at: time(day, start), end_at: time(day, end), status: 'active', version: 1})),
    {id: 'task-1', kind: 'task', title: 'Подготовить вопросы к встрече', due_date: isoDate(now), status: 'active', version: 1},
    {id: 'task-2', kind: 'task', title: 'Отправить материалы клиенту', due_date: null, status: 'active', version: 1},
    {id: 'reminder-1', kind: 'reminder', title: 'Позвонить Ивану', remind_at: time(6, '16:00'), status: 'active', version: 1},
  ];
  const listeners = new Set();
  const publish = () => listeners.forEach(listener => listener());
  return {
    getSnapshot: () => records,
    subscribe(listener) {listeners.add(listener); return () => listeners.delete(listener);},
    async api(path, options = {}) {
      const url = new URL(path, 'http://mock.local');
      const match = url.pathname.match(/^\/api\/v1\/records(?:\/([^/]+))?$/);
      if (!match) throw new Error('Это действие недоступно в демонстрации.');
      const id = match[1] && decodeURIComponent(match[1]);
      const method = options.method || 'GET';
      const body = options.body ? JSON.parse(options.body) : {};
      if (!id && method === 'GET') return {items: records, next_cursor: null};
      if (!id && method === 'POST') {
        const record = {...body, id: crypto.randomUUID(), status: 'active', version: 1};
        records = [...records, record]; publish(); return record;
      }
      const existing = records.find(record => record.id === id);
      if (!existing) throw new Error('Запись больше недоступна.');
      if (method === 'GET') return existing;
      if (method === 'PATCH') {
        const updated = {...existing, ...body.fields, version: existing.version + 1};
        records = records.map(record => record.id === id ? updated : record); publish(); return updated;
      }
      if (method === 'DELETE') {records = records.filter(record => record.id !== id); publish(); return {};}
      throw new Error('Это действие недоступно в демонстрации.');
    },
  };
}
