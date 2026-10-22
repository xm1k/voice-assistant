import {useLayoutEffect, useRef, useState} from 'react';
import ChatMessage from './ChatMessage';
import {captureScrollAnchor, restoreScrollAnchor, nearBottom} from './chat-scroll.js';

export default function ChatHistory({messages, busy, connected, mockMode, timezone, api, onOpen, cursor, ready, loadingOlder, revision, onEarlier}) {
  const viewport = useRef(null), initialized = useRef(false), following = useRef(true), anchor = useRef(null), inFlight = useRef(false);
  const previous = useRef({revision, last: null, busy: false});
  const [error, setError] = useState('');
  useLayoutEffect(() => {
    const container = viewport.current;
    if (!container || !ready) return;
    const last = messages.at(-1)?.id;
    if (!initialized.current || previous.current.revision !== revision) {
      container.scrollTop = container.scrollHeight;
      initialized.current = true; following.current = true; anchor.current = null;
    } else if (anchor.current) {
      restoreScrollAnchor(container, anchor.current);
      if (!loadingOlder) anchor.current = null;
    } else if (following.current && (last !== previous.current.last || busy !== previous.current.busy)) {
      container.scrollTop = container.scrollHeight;
    }
    previous.current = {revision, last, busy};
  }, [messages, ready, loadingOlder, revision, busy]);
  const earlier = async () => {
    if (!ready || !cursor || loadingOlder || inFlight.current || !viewport.current) return;
    inFlight.current = true; following.current = false;
    anchor.current = captureScrollAnchor(viewport.current); setError('');
    try {await onEarlier();}
    catch (exception) {anchor.current = null; setError(exception.message || 'Не удалось загрузить предыдущие сообщения.');}
    finally {inFlight.current = false;}
  };
  const scroll = () => {
    if (!initialized.current) return;
    following.current = nearBottom(viewport.current);
    if (viewport.current.scrollTop < 80 && !error) earlier();
  };
  return <div ref={viewport} className="messages" role="region" aria-label="История сообщений" onScroll={scroll} onWheel={event => {if (event.deltaY < 0 && viewport.current.scrollTop < 80 && !error) earlier();}}>
    {cursor ? <button type="button" className="secondary-button" disabled={loadingOlder} onClick={earlier}>{loadingOlder ? 'Загружаю…' : 'Загрузить предыдущие'}</button> : null}
    {error ? <p role="alert" className="form-error">{error}</p> : null}
    {!connected && !mockMode ? <p role="status">Соединение восстанавливается…</p> : null}
    {!ready ? <p role="status">Загружаю историю…</p> : messages.length === 0 ? <div className="empty-chat"><span>●</span><h2>Чем помочь?</h2><p>Задачи, встречи и напоминания можно формулировать обычными словами.</p></div> : messages.map((message, index) => <ChatMessage key={message.id || `${message.role}-${index}`} message={message} timezone={timezone} api={api} onOpen={onOpen} />)}
    {busy ? <article className="bubble assistant processing"><small>Ассистент</small><p>Думаю<span className="dots">•••</span></p></article> : null}
  </div>;
}
