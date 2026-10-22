import {useState} from 'react';
import {RecordEditor, draftFromRecord, recordDraft} from './Calendar';
import {dateLabel} from './dates';
import {completeRecord, deleteRecord} from './record-actions.js';

export function selectReminders(records, filter) {
  const status = filter === 'completed' ? 'completed' : 'active';
  return records.filter(item => item.kind === 'reminder' && (item.status || 'active') === status);
}

export default function Reminders({items, records, timezone, onRead, onChanged, mockMode, api}) {
  const [filter, setFilter] = useState('upcoming');
  const [editor, setEditor] = useState(null);
  const [working, setWorking] = useState(false), [error, setError] = useState('');
  const listed = selectReminders(records, filter);
  const act = async (item, action) => {
    if (working || mockMode) return;
    setWorking(true); setError('');
    try {
      const params = new URLSearchParams();
      if (item.occurrence_at) params.set('occurrence_at', item.occurrence_at);
      const record = await api(`/api/v1/records/${encodeURIComponent(item.record_id || item.id)}${params.size ? `?${params}` : ''}`);
      if (record.kind !== 'reminder') throw new Error('Эта запись больше не является напоминанием.');
      if (action === 'edit') {
        setEditor({record, draft: draftFromRecord(record, timezone)});
      } else {
        if (action === 'delete') {
          if (!window.confirm(`Удалить напоминание «${record.title}»?`)) return;
          await deleteRecord(api, record);
        } else if (record.status !== 'completed') {
          await completeRecord(api, record);
        }
        await onChanged();
      }
    } catch (exception) {
      setError(exception.status === 404 ? 'Напоминание больше недоступно. Возможно, оно удалено.' : exception.message);
    } finally { setWorking(false); }
  };
  const actions = item => <div className="reminder-actions">
    {item.status !== 'completed' ? <button type="button" disabled={working || mockMode} onClick={() => act(item, 'complete')} aria-label={`Выполнено: ${item.title}`}>Выполнено</button> : null}
    <button type="button" disabled={working || mockMode} onClick={() => act(item, 'edit')} aria-label={`Изменить напоминание: ${item.title}`}>Изменить</button>
    <button type="button" className="reminder-delete" disabled={working || mockMode} onClick={() => act(item, 'delete')} aria-label={`Удалить напоминание: ${item.title}`}>Удалить</button>
  </div>;
  const content = filter !== 'history'
    ? (listed.length ? listed.map((item, index) => <article key={`${item.id}-${item.occurrence_at || index}`}><span className="record-mark" /><div><b>{item.title}</b><time>{dateLabel(item.remind_at, timezone)}</time></div>{actions(item)}</article>) : <div className="empty-card">{filter === 'completed' ? 'Пока нет выполненных напоминаний.' : 'Нет предстоящих напоминаний.'}</div>)
    : (items.length ? items.map(item => <article className={item.read ? 'is-read' : ''} key={item.id}><span className="record-mark" /><div><b>{item.title}</b><time>{dateLabel(item.due_at, timezone)}</time></div><div className="reminder-history-actions">{!item.read ? <button type="button" disabled={working || mockMode} onClick={() => onRead(item.id)}>Прочитано</button> : null}{actions(item)}</div></article>) : <div className="empty-card">Напоминания ещё не срабатывали.</div>);
  return <section className="page reminders-page">
    <header className="page-title-row"><div><small>Не пропустить</small><h1>Напоминания</h1></div><button className="primary-action" disabled={working || mockMode} onClick={() => setEditor({draft: recordDraft('reminder')})}>＋ <span>Создать напоминание</span></button></header>
    <div className="tabs"><button className={filter === 'upcoming' ? 'active' : ''} onClick={() => setFilter('upcoming')}>Предстоящие</button><button className={filter === 'completed' ? 'active' : ''} onClick={() => setFilter('completed')}>Выполненные</button><button className={filter === 'history' ? 'active' : ''} onClick={() => setFilter('history')}>Сработавшие</button></div>
    {error ? <p className="form-error" role="alert">{error}</p> : null}
    <div className="simple-list">{content}</div>
    {editor ? <RecordEditor {...editor} lockKind timezone={timezone} api={api} onSaved={onChanged} onClose={() => setEditor(null)} /> : null}
  </section>;
}
