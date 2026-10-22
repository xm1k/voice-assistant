const pad = value => String(value).padStart(2, '0');

export function isoDate(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function parseDate(value) {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day);
}

export function addDays(date, amount) {
  const result = new Date(date);
  result.setDate(result.getDate() + amount);
  return result;
}

export function startOfWeek(date) {
  const result = new Date(date);
  const offset = (result.getDay() + 6) % 7;
  result.setDate(result.getDate() - offset);
  result.setHours(0, 0, 0, 0);
  return result;
}

export function zonedParts(value, timezone) {
  if (!value) return null;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(value));
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return {date: `${map.year}-${map.month}-${map.day}`, time: `${map.hour}:${map.minute}`};
}

export function recordDate(record, timezone) {
  if (record.due_date) return record.due_date;
  return zonedParts(record.start_at || record.remind_at, timezone)?.date || '';
}

export function dateLabel(value, timezone) {
  if (!value) return 'Без даты';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return parseDate(value).toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'});
  }
  return new Date(value).toLocaleString('ru-RU', {
    timeZone: timezone, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

export function timeLabel(value, timezone) {
  return value ? new Date(value).toLocaleTimeString('ru-RU', {timeZone: timezone, hour: '2-digit', minute: '2-digit'}) : '';
}

function offsetAt(date, timezone) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date);
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return Date.UTC(+map.year, +map.month - 1, +map.day, +map.hour, +map.minute, +map.second) - date.getTime();
}

export function zonedIso(date, time, timezone) {
  const [year, month, day] = date.split('-').map(Number);
  const [hour, minute] = time.split(':').map(Number);
  const wallClock = Date.UTC(year, month - 1, day, hour, minute, 0);
  let instant = new Date(wallClock);
  instant = new Date(wallClock - offsetAt(instant, timezone));
  instant = new Date(wallClock - offsetAt(instant, timezone));
  return instant.toISOString();
}
