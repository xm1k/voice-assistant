import {useState} from 'react';
import {ACTION_NAMES, KIND_NAMES, STATUS_NAMES, cardDateLabel, openCardRecord} from './chat-cards.js';

function ChatRecordCard({card, timezone, api, onOpen}) {
  const [loading, setLoading] = useState(false), [error, setError] = useState('');
  const record = card.record;
  const open = async () => {
    if (loading) return;
    setLoading(true); setError('');
    try { onOpen(await openCardRecord(api, card)); }
    catch (exception) { setError(exception.message); }
    finally { setLoading(false); }
  };
  return <section className={`chat-record-card kind-${record.kind}`} aria-label={`${KIND_NAMES[record.kind]}: ${record.title}`}>
    <div className="chat-record-meta"><span>{KIND_NAMES[record.kind]}</span><span>{ACTION_NAMES[card.action]}</span></div>
    <h3>{record.title}</h3>
    <p className="chat-record-date">{cardDateLabel(record, timezone)}</p>
    {record.start_at || record.remind_at ? <small className="chat-record-zone">{timezone}</small> : null}
    <div className="chat-record-footer"><span>{STATUS_NAMES[record.status]}{record.recurrence ? ' · Повторяется' : ''}{record.occurrence_at ? ' · Выбранное повторение' : ''}</span>
      {card.action !== 'deleted' ? <button type="button" disabled={loading} onClick={open} aria-label={`Открыть: ${record.title}`}>{loading ? 'Открываю…' : 'Открыть'}</button> : null}
    </div>
    {record.conflicts?.length > 0 ? <p className="chat-record-conflict">Есть пересечение с другой встречей.</p> : null}
    {error ? <p className="form-error" role="alert">{error}</p> : null}
  </section>;
}

export default function ChatMessage({message, timezone, api, onOpen}) {
  const cards = message.role === 'assistant' ? message.cards || [] : [];
  return <article className={`bubble ${message.role}`} data-message-id={message.id}>
    <small>{message.role === 'user' ? 'Вы' : 'Ассистент'}</small>
    <p>{message.text}</p>
    {cards.length > 0 ? <details className="chat-records" open={cards.length <= 3}><summary>Записи ({cards.length})</summary><div className="chat-records-list">{cards.map((card, index) => <ChatRecordCard key={`${card.action}-${card.record.id}-${card.record.occurrence_at || ''}-${index}`} card={card} timezone={timezone} api={api} onOpen={onOpen} />)}</div></details> : null}
  </article>;
}
