import {randomUUID, collectPages} from './network';
import {useEffect, useMemo, useRef, useState} from 'react';
import {addDays, isoDate, parseDate, recordDate, startOfWeek, timeLabel, zonedIso, zonedParts} from './dates';
import {completeRecord, deleteRecord} from './record-actions.js';

const WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const HOURS = Array.from({length: 24}, (_, index) => index);
const KIND_LABEL = {meeting: 'Встреча', reminder: 'Напоминание', task: 'Задача'};

export function recordDraft(kind = 'meeting', date = isoDate(new Date()), hour = 10) {
  const end = `${String((hour + 1) % 24).padStart(2, '0')}:00`;
  const endDate = hour === 23 ? isoDate(addDays(parseDate(date), 1)) : date;
  return {kind, title: '', notes: '', date: kind === 'task' ? '' : date, start: `${String(hour).padStart(2, '0')}:00`, end, endDate};
}

export function draftFromRecord(record, timezone) {
  const start = zonedParts(record.start_at || record.remind_at, timezone);
  const end = zonedParts(record.end_at, timezone);
  return {
    kind: record.kind,
    title: record.title,
    notes: record.notes || '',
    date: record.kind === 'task' ? record.due_date || '' : start?.date || isoDate(new Date()),
    start: start?.time || '10:00',
    end: end?.time || (record.kind === 'meeting' ? '11:00' : ''),
    endDate: end?.date || start?.date || isoDate(new Date()),
  };
}

export function recordFields(draft, timezone) {
  const base = {kind: draft.kind, title: draft.title.trim(), notes: draft.notes.trim() || null};
  if (draft.kind === 'task') return {...base, due_date: draft.date || null, start_at: null, end_at: null, remind_at: null};
  const start = zonedIso(draft.date, draft.start, timezone);
  if (draft.kind === 'reminder') return {...base, due_date: null, start_at: null, end_at: null, remind_at: start};
  return {...base, due_date: null, start_at: start, end_at: zonedIso(draft.endDate || draft.date, draft.end, timezone), remind_at: null};
}

export function RecordEditor({draft: initialDraft, record, timezone, onClose, onSaved, api, lockKind = false}) {
  const [draft, setDraft] = useState(initialDraft);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const update = (key, next) => setDraft(current => {
    if (key === 'date' && current.kind === 'meeting' && next) {
      const days = Math.max(0, Math.round((Date.parse(`${current.endDate || current.date}T00:00:00Z`) - Date.parse(`${current.date}T00:00:00Z`)) / 86400000) || 0);
      return {...current, date: next, endDate: isoDate(addDays(parseDate(next), days))};
    }
    return {...current, [key]: next};
  });
  const fields = () => recordFields(draft, timezone);
  const submit = async event => {
    event.preventDefault();
    setSaving(true); setError('');
    try {
      if (record) {
        await api(`/api/v1/records/${record.id}`, {
          method: 'PATCH', headers: {'Idempotency-Key': randomUUID()},
          body: JSON.stringify({fields: fields(), version: record.version, ...(record.recurrence ? {scope: record.occurrence_at ? 'occurrence' : 'series', occurrence_at: record.occurrence_at} : {})}),
        });
      } else {
        await api('/api/v1/records', {method: 'POST', headers: {'Idempotency-Key': randomUUID()}, body: JSON.stringify(fields())});
      }
      await onSaved(); onClose();
    } catch (exception) { setError(exception.message); }
    finally { setSaving(false); }
  };
  const remove = async () => {
    if (!record || !window.confirm(`Удалить «${record.title}»?`)) return;
    setSaving(true); setError('');
    try {
      await deleteRecord(api, record);
      await onSaved(); onClose();
    } catch (exception) { setError(exception.message); setSaving(false); }
  };
  const complete = async () => {
    setSaving(true); setError('');
    try {
      await completeRecord(api, record);
      await onSaved(); onClose();
    } catch (exception) { setError(exception.message); setSaving(false); }
  };
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <form className="event-modal" onSubmit={submit}>
      <div className="modal-head"><div><small>{record ? 'Редактирование' : lockKind ? `Новое ${KIND_LABEL[draft.kind].toLowerCase()}` : 'Новая запись'}</small><h2>{record ? record.title : `Добавить: ${KIND_LABEL[draft.kind].toLowerCase()}`}</h2></div><button type="button" className="quiet-button" onClick={onClose}>Закрыть</button></div>
      {!record && !lockKind && <div className="kind-switch" role="tablist" aria-label="Тип записи">{Object.entries(KIND_LABEL).map(([kind, label]) => <button type="button" role="tab" aria-selected={draft.kind === kind} key={kind} className={draft.kind === kind ? 'active' : ''} onClick={() => setDraft(current => ({...current, kind, date: kind !== 'task' && !current.date ? isoDate(new Date()) : current.date}))}>{label}</button>)}</div>}
      <label>Название<input autoFocus required maxLength="500" value={draft.title} onChange={event => update('title', event.target.value)} /></label>
      <div className="form-row"><label>{draft.kind === 'task' ? 'Срок (необязательно)' : 'Дата'}<input type="date" required={draft.kind !== 'task'} value={draft.date} onChange={event => update('date', event.target.value)} /></label>{draft.kind !== 'task' && <label>{draft.kind === 'reminder' ? 'Время' : 'Начало'}<input type="time" required value={draft.start} onChange={event => update('start', event.target.value)} /></label>}{draft.kind === 'meeting' && <label>Дата окончания<input type="date" required min={draft.date} value={draft.endDate || draft.date} onChange={event => update('endDate', event.target.value)} /></label>}{draft.kind === 'meeting' && <label>Конец<input type="time" required min={(draft.endDate || draft.date) === draft.date ? draft.start : undefined} value={draft.end} onChange={event => update('end', event.target.value)} /></label>}</div>
      <label>Заметка<textarea rows="3" maxLength="5000" value={draft.notes} onChange={event => update('notes', event.target.value)} placeholder="Необязательно" /></label>
      {record?.recurrence && <p className="form-note">{record.occurrence_at ? 'Изменение применяется только к выбранному повторению.' : 'Изменение применяется ко всей серии.'}</p>}
      {error && <p className="form-error">{error}</p>}
      <div className="modal-actions">{record && <button type="button" className="danger-button" disabled={saving} onClick={remove}>Удалить</button>}{(record?.kind === 'task' || record?.kind === 'reminder') && record.status !== 'completed' && <button type="button" className="secondary-button" disabled={saving} onClick={complete}>Выполнено</button>}<span /><button className="primary-action" disabled={saving}>{saving ? 'Сохраняю…' : 'Сохранить'}</button></div>
    </form>
  </div>;
}

function EventButton({record, timezone, compact = false, onClick}) {
  const time = timeLabel(record.start_at || record.remind_at, timezone);
  return <button className={`calendar-event kind-${record.kind} ${record.status === 'completed' ? 'completed' : ''}`} onClick={event => {event.stopPropagation(); onClick(record);}} title={record.title}><span>{time}</span><b>{record.title}</b>{!compact && record.recurrence && <i>↻</i>}</button>;
}

export default function Calendar({timezone, api, mockRecords, onChanged, revision}) {
  const [mode, setMode] = useState('month');
  const [cursor, setCursor] = useState(new Date());
  const [records, setRecords] = useState(mockRecords || []);
  const [loading, setLoading] = useState(false), [error, setError] = useState('');
  const [editor, setEditor] = useState(null);
  const weekScroll = useRef(null);
  const range = useMemo(() => {
    if (mode === 'week') {
      const start = startOfWeek(cursor);
      return {start, end: addDays(start, 6), days: Array.from({length: 7}, (_, index) => addDays(start, index))};
    }
    const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    const start = startOfWeek(first);
    return {start, end: addDays(start, 41), days: Array.from({length: 42}, (_, index) => addDays(start, index))};
  }, [cursor, mode]);
  const load = async () => {
    if (mockRecords) { setRecords(mockRecords); return; }
    setLoading(true);
    try { setError(''); setRecords(await collectPages(api, `/api/v1/records?period_start=${isoDate(range.start)}&period_end=${isoDate(range.end)}`)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load().catch(exception => setError(exception.message)); }, [range.start.getTime(), range.end.getTime(), revision]);
  const byDay = useMemo(() => {
    const result = new Map();
    records.forEach(record => {const key = recordDate(record, timezone); result.set(key, [...(result.get(key) || []), record]);});
    return result;
  }, [records, timezone]);
  const hasUntimedRecords = records.some(record => record.due_date);
  useEffect(() => {
    if (mode !== 'week') return;
    const frame = requestAnimationFrame(() => {
      if (weekScroll.current) weekScroll.current.scrollTop = 8 * 60;
    });
    return () => cancelAnimationFrame(frame);
  }, [mode, range.start.getTime()]);
  const move = direction => setCursor(current => mode === 'month' ? new Date(current.getFullYear(), current.getMonth() + direction, 1) : addDays(current, direction * 7));
  const title = mode === 'month'
    ? cursor.toLocaleDateString('ru-RU', {month: 'long', year: 'numeric'})
    : `${range.start.toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'})} — ${range.end.toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'})}`;
  const saved = async () => {await load(); await onChanged?.();};
  return <section className="calendar-page">{error && <p className="form-error" role="alert">{error}</p>}
    <header className="calendar-toolbar"><div><h1>{title}</h1><span>{loading ? 'Обновляю…' : `${records.length} событий`}</span></div><div className="toolbar-actions"><button onClick={() => setCursor(new Date())}>Сегодня</button><button aria-label="Назад" onClick={() => move(-1)}>←</button><button aria-label="Вперёд" onClick={() => move(1)}>→</button><div className="segmented"><button className={mode === 'month' ? 'active' : ''} onClick={() => setMode('month')}>Месяц</button><button className={mode === 'week' ? 'active' : ''} onClick={() => setMode('week')}>Неделя</button></div><button className="calendar-add" onClick={() => setEditor({draft: recordDraft()})}>＋ <span>Создать</span></button></div></header>
    {mode === 'month' ? <div className="month-calendar">
      {WEEKDAYS.map(day => <div className="weekday" key={day}>{day}</div>)}
      {range.days.map(day => {const date = isoDate(day), items = byDay.get(date) || [], outside = day.getMonth() !== cursor.getMonth(); return <div className={`month-cell ${outside ? 'outside' : ''} ${date === isoDate(new Date()) ? 'today' : ''}`} key={date} onClick={() => setEditor({draft: recordDraft('meeting', date)})}><span className="day-number">{day.getDate()}</span><div>{items.slice(0, 4).map((record, index) => <EventButton compact key={`${record.id}-${record.occurrence_at || index}`} record={record} timezone={timezone} onClick={selected => setEditor({record: selected, draft: draftFromRecord(selected, timezone)})} />)}{items.length > 4 && <small className="more-events">ещё {items.length - 4}</small>}</div></div>;})}
    </div> : <div className="week-scroll" ref={weekScroll}><div className={`week-calendar ${hasUntimedRecords ? 'has-untimed' : ''}`}>
      <div className="week-head-spacer" />{range.days.map(day => <div className={`week-day-head ${isoDate(day) === isoDate(new Date()) ? 'today' : ''}`} key={isoDate(day)}><small>{WEEKDAYS[(day.getDay() + 6) % 7]}</small><b>{day.getDate()}</b></div>)}
      {hasUntimedRecords && <><div className="all-day-label">без времени</div>{range.days.map(day => <div className="all-day-cell" key={isoDate(day)}>{(byDay.get(isoDate(day)) || []).filter(record => record.due_date).map((record, index) => <EventButton compact key={`${record.id}-${index}`} record={record} timezone={timezone} onClick={selected => setEditor({record: selected, draft: draftFromRecord(selected, timezone)})} />)}</div>)}</>}
      <div className="week-time-grid"><div className="hour-labels">{HOURS.map(hour => <span key={hour}>{String(hour).padStart(2, '0')}:00</span>)}</div><div className="week-slots">{HOURS.flatMap(hour => range.days.map((day, dayIndex) => <button key={`${hour}-${dayIndex}`} style={{gridColumn: dayIndex + 1, gridRow: hour + 1}} onClick={() => setEditor({draft: recordDraft('meeting', isoDate(day), hour)})} aria-label={`Создать запись ${isoDate(day)} в ${hour}:00`} />))}</div>{records.filter(record => record.start_at || record.remind_at).map((record, index) => {const parts = zonedParts(record.start_at || record.remind_at, timezone); const dayIndex = range.days.findIndex(day => isoDate(day) === parts?.date); if (dayIndex < 0) return null; const [hour, minute] = parts.time.split(':').map(Number); const start = hour * 60 + minute; const endParts = zonedParts(record.end_at, timezone); const duration = endParts ? Math.max(30, (+endParts.time.slice(0, 2) * 60 + +endParts.time.slice(3)) - start) : 45; return <button key={`${record.id}-${record.occurrence_at || index}`} className={`week-event kind-${record.kind}`} style={{'--day': dayIndex, '--start': start, '--duration': duration}} onClick={() => setEditor({record, draft: draftFromRecord(record, timezone)})}><span>{parts.time}</span><b>{record.title}</b></button>;})}</div>
    </div></div>}
    {editor && <RecordEditor {...editor} timezone={timezone} api={api} onSaved={saved} onClose={() => setEditor(null)} />}
  </section>;
}
