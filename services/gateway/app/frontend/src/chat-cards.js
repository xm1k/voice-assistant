import {parseDate} from './dates.js';

export const KIND_NAMES = {task: 'Задача', meeting: 'Встреча', reminder: 'Напоминание'};
export const ACTION_NAMES = {created: 'Добавлено', updated: 'Обновлено', deleted: 'Удалено', found: 'Найдено'};
export const STATUS_NAMES = {active: 'Запланировано', completed: 'Выполнено', cancelled: 'Отменено'};

export function cardDateLabel(record, timezone) {
  if (record.due_date) return parseDate(record.due_date).toLocaleDateString('ru-RU', {day: 'numeric', month: 'long', year: 'numeric'});
  const start = record.start_at || record.remind_at;
  if (!start) return 'Без даты';
  const format = new Intl.DateTimeFormat('ru-RU', {timeZone: timezone, dateStyle: 'medium', timeStyle: 'short'});
  const label = format.format(new Date(start));
  return record.end_at ? `${label} — ${format.format(new Date(record.end_at))}` : label;
}

export async function openCardRecord(api, card) {
  if (card.action === 'deleted') throw new Error('Запись удалена');
  const params = new URLSearchParams();
  if (card.record.occurrence_at) params.set('occurrence_at', card.record.occurrence_at);
  try {
    return await api(`/api/v1/records/${encodeURIComponent(card.record.id)}${params.size ? `?${params}` : ''}`);
  } catch (error) {
    if (error.status === 404) throw new Error('Запись больше недоступна. Она могла быть удалена.');
    throw error;
  }
}
