import {useEffect, useState} from 'react';
import Calendar, {RecordEditor, draftFromRecord, recordDraft} from './Calendar';
import ChatHistory from './ChatHistory';
import Tasks from './Tasks';
import Reminders from './Reminders';
import './workspace.css';

const tabs = [['assistant', 'Чат'], ['reminders', 'Напоминания'], ['tasks', 'Задачи']];

// Both the demo and the live app use this layout; data and actions stay with their owner.
export default function Workspace({
  Navigation, Composer, user, view, setView, settingsContent, alerts,
  api, onRecordsChanged, revision, mockRecords, mockMode = false,
  taskRecords, reminderRecords, notices, onRead, noticeCount = 0,
  messages, pending, connected, busy, onSend, onAudio, onClear,
  historyCursor, historyReady, loadingOlder, historyRevision, onEarlier,
}) {
  const [desktop, setDesktop] = useState(() => typeof matchMedia === 'undefined' || matchMedia('(min-width: 721px)').matches);
  const [editor, setEditor] = useState(null);
  const panelView = view === 'calendar' ? 'assistant' : view;
  const lastAnswer = messages.findLast(message => message.role === 'assistant')?.id;
  const [seenAnswer, setSeenAnswer] = useState(lastAnswer);
  const chatVisible = view !== 'settings' && (desktop ? panelView === 'assistant' : view === 'assistant');
  const unread = Boolean(lastAnswer && lastAnswer !== seenAnswer && !chatVisible);
  useEffect(() => {
    const media = matchMedia('(min-width: 721px)');
    const update = () => setDesktop(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {if (chatVisible) setSeenAnswer(lastAnswer);}, [chatVisible, lastAnswer]);
  const calendar = <Calendar revision={revision} timezone={user.timezone} api={api} mockRecords={mockRecords} onChanged={onRecordsChanged} initialMode={desktop ? 'week' : 'month'} />;
  const chat = <>
    <header className="assistant-head"><div><small>{pending ? 'Жду уточнение' : 'Помощник по встречам'}</small><h1>Ассистент</h1></div><div className="chat-header-actions"><span className={pending ? 'status pending' : 'status'}>{mockMode ? 'Демо' : pending ? 'Ответьте на вопрос' : connected ? 'Готов' : 'Нет соединения'}</span><button type="button" className="clear-chat-button" onClick={onClear} title="Очистить историю и контекст">Очистить чат</button></div></header>
    <div className="suggestions quick-create">{[['meeting', 'Встреча'], ['reminder', 'Напоминание'], ['task', 'Задача']].map(([kind, label]) => <button key={kind} onClick={() => setEditor({draft: recordDraft(kind)})}>＋ {label}</button>)}</div>
    <ChatHistory messages={messages} busy={busy} connected={connected} mockMode={mockMode} timezone={user.timezone} api={api} onOpen={record => setEditor({record, draft: draftFromRecord(record, user.timezone)})} cursor={historyCursor} ready={historyReady} loadingOlder={loadingOlder} revision={historyRevision} onEarlier={onEarlier} />
  </>;
  const content = panelView === 'tasks'
    ? <Tasks records={taskRecords} timezone={user.timezone} api={api} onChanged={onRecordsChanged} />
    : panelView === 'reminders'
      ? <Reminders records={reminderRecords} items={notices} timezone={user.timezone} api={api} onChanged={onRecordsChanged} onRead={onRead} />
      : chat;
  return <div className="shell workspace-shell">
    {!desktop && <Navigation view={view} setView={setView} noticeCount={noticeCount} user={user} />}
    {desktop && <header className="workspace-header"><button className="workspace-brand" onClick={() => setView('assistant')}><span>г</span><b>голос</b></button><span className="workspace-purpose">Встречи и планы</span><span className="workspace-connection">{mockMode ? 'Демо · изменения до перезагрузки' : !connected ? 'Соединение восстанавливается…' : ''}</span><button className="workspace-settings" onClick={() => setView(view === 'settings' ? 'assistant' : 'settings')}>{view === 'settings' ? 'К расписанию' : 'Настройки'}</button><span className="avatar" title={user.username}>{user.username.slice(0, 1).toUpperCase()}</span></header>}
    <main className="main workspace-main">
      {alerts}
      {view === 'settings' ? settingsContent : !desktop && view === 'calendar' ? calendar : <div className="workspace">
        {desktop && <div className="workspace-calendar">{calendar}</div>}
        <section className="workspace-assistant">
          {desktop && <nav className="workspace-tabs" aria-label="Панель помощника">{tabs.map(([id, label]) => <button key={id} aria-current={panelView === id ? 'page' : undefined} className={panelView === id ? 'active' : ''} onClick={() => setView(id)}>{label}{((id === 'assistant' && unread) || (id === 'reminders' && noticeCount > 0)) && <span className="unread-dot" aria-label={id === 'assistant' ? 'Новое сообщение' : 'Новые уведомления'} />}</button>)}</nav>}
          {desktop && panelView !== 'assistant' && pending && <p className="workspace-notice" role="status">Помощник ждёт уточнения в чате.</p>}
          <div className={`workspace-content ${panelView === 'assistant' ? 'workspace-chat' : ''}`}>{content}</div>
          {(desktop || view === 'assistant') && <Composer onSend={onSend} onAudio={onAudio} busy={Boolean(busy) || !connected} mockMode={mockMode} />}
        </section>
      </div>}
    </main>
    {editor && <RecordEditor {...editor} timezone={user.timezone} api={api} onSaved={onRecordsChanged} onClose={() => setEditor(null)} />}
  </div>;
}
