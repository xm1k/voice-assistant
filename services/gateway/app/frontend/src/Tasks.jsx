import {randomUUID} from './network.js';
import {useState} from 'react';
import {dateLabel, isoDate} from './dates';

function taskDraft(record) {
  return {
    title: record?.title || '',
    due_date: record?.due_date || '',
    notes: record?.notes || '',
  };
}

function TaskEditor({record, api, onClose, onSaved}) {
  const [draft, setDraft] = useState(taskDraft(record));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const update = (key, value) => setDraft(current => ({...current, [key]: value}));
  const occurrence = record?.recurrence ? {scope: 'occurrence', occurrence_at: record.occurrence_at || null} : {};
  const submit = async event => {
    event.preventDefault(); setSaving(true); setError('');
    const fields = {title: draft.title.trim(), due_date: draft.due_date || null, notes: draft.notes.trim() || null};
    try {
      if (record) await api(`/api/v1/records/${record.id}`, {method: 'PATCH', headers: {'Idempotency-Key': randomUUID()}, body: JSON.stringify({fields, ...occurrence})});
      else await api('/api/v1/records', {method: 'POST', headers: {'Idempotency-Key': randomUUID()}, body: JSON.stringify({kind: 'task', ...fields})});
      await onSaved(); onClose();
    } catch (exception) { setError(exception.message); setSaving(false); }
  };
  const remove = async () => {
    if (!record || !window.confirm(`Удалить задачу «${record.title}»?`)) return;
    setSaving(true); setError('');
    try {
      const query = new URLSearchParams();
      if (occurrence.scope) query.set('scope', occurrence.scope);
      if (occurrence.occurrence_at) query.set('occurrence_at', occurrence.occurrence_at);
      await api(`/api/v1/records/${record.id}?${query}`, {method: 'DELETE', headers: {'Idempotency-Key': randomUUID()}});
      await onSaved(); onClose();
    } catch (exception) { setError(exception.message); setSaving(false); }
  };
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <form className="event-modal task-modal" onSubmit={submit}>
      <div className="modal-head"><div><small>{record ? 'Редактирование' : 'Новая задача'}</small><h2>{record?.title || 'Добавить задачу'}</h2></div><button type="button" className="quiet-button" onClick={onClose}>Закрыть</button></div>
      <label>Название<input autoFocus required maxLength="500" value={draft.title} onChange={event => update('title', event.target.value)} /></label>
      <label>Срок <small>необязательно</small><input type="date" value={draft.due_date} onChange={event => update('due_date', event.target.value)} /></label>
      <p className="form-note">Без срока задача сохранится во «Входящих» — дату можно добавить позже.</p>
      <label>Заметка<textarea rows="3" maxLength="5000" value={draft.notes} onChange={event => update('notes', event.target.value)} placeholder="Необязательно" /></label>
      {record?.recurrence && <p className="form-note">Изменение применяется только к выбранному повторению.</p>}
      {error && <p className="form-error">{error}</p>}
      <div className="modal-actions">{record && <button type="button" className="danger-button" disabled={saving} onClick={remove}>Удалить</button>}<span /><button className="primary-action" disabled={saving}>{saving ? 'Сохраняю…' : 'Сохранить'}</button></div>
    </form>
  </div>;
}

function dueText(record, timezone, today) {
  if (!record.due_date) return 'Входящие · без срока';
  if (record.due_date < today) return `Просрочено · ${dateLabel(record.due_date, timezone)}`;
  if (record.due_date === today) return 'Сегодня';
  return `Срок: ${dateLabel(record.due_date, timezone)}`;
}

export default function Tasks({records, timezone, api, onChanged, mockMode}) {
  const [filter, setFilter] = useState('active');
  const [editor, setEditor] = useState(null);
  const [working, setWorking] = useState(null);
  const [quickTitle, setQuickTitle] = useState('');
  const [error, setError] = useState('');
  const tasks = records.filter(record => record.kind === 'task');
  const active = tasks.filter(record => record.status !== 'completed');
  const completed = tasks.filter(record => record.status === 'completed');
  const today = isoDate(new Date());
  const groups = [
    ['overdue', 'Просрочено', active.filter(record => record.due_date && record.due_date < today)],
    ['today', 'Сегодня', active.filter(record => record.due_date === today)],
    ['upcoming', 'Предстоящие', active.filter(record => record.due_date && record.due_date > today)],
    ['inbox', 'Входящие', active.filter(record => !record.due_date)],
  ];
  const changeStatus = async record => {
    if (mockMode) return;
    setWorking(record.id); setError('');
    try {
      await api(`/api/v1/records/${record.id}`, {
        method: 'PATCH', headers: {'Idempotency-Key': randomUUID()},
        body: JSON.stringify({fields: {status: record.status === 'completed' ? 'active' : 'completed'}, ...(record.recurrence ? {scope: 'occurrence', occurrence_at: record.occurrence_at || null} : {})}),
      });
      await onChanged();
    } catch (exception) { setError(exception.message); }
    finally { setWorking(null); }
  };
  const quickAdd = async event => {
    event.preventDefault();
    const title = quickTitle.trim();
    if (!title || mockMode) return;
    setWorking('new'); setError('');
    try {
      await api('/api/v1/records', {method: 'POST', headers: {'Idempotency-Key': randomUUID()}, body: JSON.stringify({kind: 'task', title, due_date: null})});
      setQuickTitle(''); await onChanged();
    } catch (exception) { setError(exception.message); }
    finally { setWorking(null); }
  };
  const taskCard = (record, index) => <article key={`${record.id}-${record.occurrence_at || index}`} className={`${record.status === 'completed' ? 'completed' : ''} ${record.due_date && record.due_date < today && record.status !== 'completed' ? 'overdue' : ''}`}>
    <button className="task-check" disabled={working === record.id || mockMode} onClick={() => changeStatus(record)} aria-label={record.status === 'completed' ? 'Вернуть в работу' : 'Завершить задачу'}>{record.status === 'completed' ? '✓' : ''}</button>
    <button className="task-content" onClick={() => setEditor({record})}><b>{record.title}</b><span>{dueText(record, timezone, today)}</span>{record.notes && <small>{record.notes}</small>}</button>
    <div className="task-actions"><button className="task-status" disabled={working === record.id || mockMode} onClick={() => changeStatus(record)}>{record.status === 'completed' ? 'Вернуть' : 'Завершить'}</button><button className="task-edit" onClick={() => setEditor({record})}>Изменить</button></div>
  </article>;
  return <section className="page tasks-page">
    <header className="page-title-row"><div><small>Личный список дел</small><h1>Задачи</h1><p className="task-summary">{active.length} в работе · {groups[0][2].length} просрочено · {groups[3][2].length} без срока</p></div><button className="primary-action" onClick={() => setEditor({})}>＋ <span>Добавить задачу</span></button></header>
    <form className="task-quick-add" onSubmit={quickAdd}><input value={quickTitle} onChange={event => setQuickTitle(event.target.value)} placeholder="Добавить во Входящие…" aria-label="Название новой задачи" /><button disabled={!quickTitle.trim() || working === 'new' || mockMode}>{working === 'new' ? 'Добавляю…' : 'Добавить'}</button></form>
    {error && <p className="task-error">{error}</p>}
    <div className="tabs"><button className={filter === 'active' ? 'active' : ''} onClick={() => setFilter('active')}>В работе <span>{active.length}</span></button><button className={filter === 'completed' ? 'active' : ''} onClick={() => setFilter('completed')}>Архив <span>{completed.length}</span></button></div>
    {filter === 'active' ? <div className="task-groups">{groups.map(([id, title, items]) => items.length > 0 && <section className={`task-group ${id}`} key={id}><header><h2>{title}</h2><span>{items.length}</span></header><div className="task-list">{items.map(taskCard)}</div></section>)}{active.length === 0 && <div className="empty-card">Всё выполнено. Новые задачи без срока попадут во «Входящие».</div>}</div> : <section className="task-group archive"><header><h2>Выполненные</h2><span>{completed.length}</span></header><div className="task-list">{completed.length ? completed.map(taskCard) : <div className="empty-card">Архив пока пуст.</div>}</div></section>}
    {editor && <TaskEditor record={editor.record} api={api} onClose={() => setEditor(null)} onSaved={onChanged} />}
  </section>;
}
