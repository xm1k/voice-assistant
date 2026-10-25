import {useEffect, useState, useSyncExternalStore} from 'react';
import Calendar, {RecordEditor, draftFromRecord, recordDraft} from './Calendar';
import ChatHistory from './ChatHistory';
import Tasks from './Tasks';
import Reminders from './Reminders';
import {createMockStore} from './mock-store';
import './mock-workspace.css';

const unchanged = async () => {};
const tabs = [['assistant', 'Чат'], ['reminders', 'Напоминания'], ['tasks', 'Задачи']];

export default function MockWorkspace({Navigation, Composer, Settings, initialSettings}) {
  const [store] = useState(createMockStore);
  const records = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const [view, setView] = useState('assistant');
  const [settings, setSettings] = useState(initialSettings);
  const [user, setUser] = useState({username: 'Денис', timezone: 'Europe/Moscow'});
  const [desktop, setDesktop] = useState(() => matchMedia('(min-width: 721px)').matches);
  const [messages, setMessages] = useState(() => [{id: 'welcome', role: 'assistant', text: 'Неделя перед вами. Добавьте встречу в календарь или напишите, что нужно запланировать. Здесь можно попробовать интерфейс — данные сохраняются до перезагрузки.'}, {id: 'demo-cards', role: 'assistant', text: 'Примеры записей на этой неделе. Нажмите «Открыть», чтобы посмотреть или изменить запись.', cards: ['meeting', 'task', 'reminder'].map(kind => ({action: 'found', record: store.getSnapshot().find(record => record.kind === kind)}))}]);
  const [unread, setUnread] = useState(false);
  const [editor, setEditor] = useState(null);
  useEffect(() => {
    const media = matchMedia('(min-width: 721px)');
    const update = () => setDesktop(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {document.documentElement.dataset.theme = settings.theme;}, [settings.theme]);
  const select = next => {setView(next); if (next === 'assistant') setUnread(false);};
  const send = async text => {
    setMessages(current => [...current,
      {id: crypto.randomUUID(), role: 'user', text},
      {id: crypto.randomUUID(), role: 'assistant', text: 'Это демонстрационный ответ. Чтобы изменить расписание, выберите время в календаре или откройте запись. Задачи и напоминания можно добавить на соседних вкладках.'},
    ]);
    if (view !== 'assistant') setUnread(true);
    return true;
  };
  const clearHistory = () => {
    if (!window.confirm('Очистить историю разговора и текущий контекст? Задачи и встречи останутся.')) return;
    setMessages([]); setUnread(false);
  };
  const calendar = <Calendar timezone={user.timezone} api={store.api} mockRecords={records} onChanged={unchanged} initialMode={desktop ? 'week' : 'month'} />;
  const chat = <><header className="assistant-head"><div><small>Помощник по встречам</small><h1>Ассистент</h1></div><div className="chat-header-actions"><span className="status">Демо</span><button type="button" className="clear-chat-button" onClick={clearHistory} title="Очистить историю и контекст">Очистить чат</button></div></header><div className="suggestions quick-create">{[['meeting', 'Встреча'], ['reminder', 'Напоминание'], ['task', 'Задача']].map(([kind, label]) => <button key={kind} onClick={() => setEditor({draft: recordDraft(kind)})}>＋ {label}</button>)}</div><ChatHistory messages={messages} connected ready mockMode timezone={user.timezone} api={store.api} onOpen={record => setEditor({record, draft: draftFromRecord(record, user.timezone)})} /></>;
  const content = view === 'tasks'
    ? <Tasks records={records} timezone={user.timezone} api={store.api} onChanged={unchanged} />
    : view === 'reminders'
      ? <Reminders records={records} items={[]} timezone={user.timezone} api={store.api} onChanged={unchanged} onRead={unchanged} />
      : chat;
  const configuration = <Settings settings={settings} setSettings={setSettings} user={user} onTimezone={timezone => setUser(current => ({...current, timezone}))} onNotifications={enabled => setSettings(current => ({...current, browserNotifications: enabled}))} onLogout={() => location.reload()} />;
  return <div className="shell mock-shell">
    {!desktop && <Navigation view={view} setView={select} noticeCount={0} user={user} />}
    {desktop && <header className="workspace-header"><button className="workspace-brand" onClick={() => select('assistant')}><span>г</span><b>голос</b></button><span className="workspace-purpose">Встречи и планы</span><span className="workspace-demo">Демо · изменения до перезагрузки</span><button className="workspace-settings" onClick={() => select(view === 'settings' ? 'assistant' : 'settings')}>{view === 'settings' ? 'К расписанию' : 'Настройки'}</button><span className="avatar" title={user.username}>Д</span></header>}
    <main className="main mock-main">
      {view === 'settings' ? configuration : !desktop && view === 'calendar' ? calendar : <div className="workspace">
        {desktop && <div className="workspace-calendar">{calendar}</div>}
        <section className="workspace-assistant">
          {desktop && <nav className="workspace-tabs" aria-label="Панель помощника">{tabs.map(([id, label]) => <button key={id} aria-current={view === id ? 'page' : undefined} className={view === id ? 'active' : ''} onClick={() => select(id)}>{label}{id === 'assistant' && unread && <span className="unread-dot" aria-label="Новое сообщение" />}</button>)}</nav>}
          <div className={`workspace-content ${view === 'assistant' ? 'workspace-chat' : ''}`}>{content}</div>
          <Composer onSend={send} onAudio={() => send('Демонстрационный пример расшифровки аудио')} mockMode />
        </section>
      </div>}
    </main>
    {editor && <RecordEditor {...editor} timezone={user.timezone} api={store.api} onSaved={unchanged} onClose={() => setEditor(null)} />}
  </div>;
}
