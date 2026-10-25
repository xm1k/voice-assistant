import {useEffect, useState, useSyncExternalStore} from 'react';
import Workspace from './Workspace';
import {createMockStore} from './mock-store';

const unchanged = async () => {};

export default function MockWorkspace({Navigation, Composer, Settings, initialSettings}) {
  const [store] = useState(createMockStore);
  const records = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const [view, setView] = useState('assistant');
  const [settings, setSettings] = useState(initialSettings);
  const [user, setUser] = useState({username: 'Денис', timezone: 'Europe/Moscow'});
  const [messages, setMessages] = useState(() => [{id: 'welcome', role: 'assistant', text: 'Неделя перед вами. Добавьте встречу в календарь или напишите, что нужно запланировать. Здесь можно попробовать интерфейс — данные сохраняются до перезагрузки.'}, {id: 'demo-cards', role: 'assistant', text: 'Примеры записей на этой неделе. Нажмите «Открыть», чтобы посмотреть или изменить запись.', cards: ['meeting', 'task', 'reminder'].map(kind => ({action: 'found', record: store.getSnapshot().find(record => record.kind === kind)}))}]);
  useEffect(() => {document.documentElement.dataset.theme = settings.theme;}, [settings.theme]);
  const send = async text => {
    setMessages(current => [...current,
      {id: crypto.randomUUID(), role: 'user', text},
      {id: crypto.randomUUID(), role: 'assistant', text: 'Это демонстрационный ответ. Чтобы изменить расписание, выберите время в календаре или откройте запись. Задачи и напоминания можно добавить на соседних вкладках.'},
    ]);
    return true;
  };
  const clearHistory = () => {
    if (!window.confirm('Очистить историю разговора и текущий контекст? Задачи и встречи останутся.')) return;
    setMessages([]);
  };
  const configuration = <Settings settings={settings} setSettings={setSettings} user={user} onTimezone={timezone => setUser(current => ({...current, timezone}))} onNotifications={enabled => setSettings(current => ({...current, browserNotifications: enabled}))} onLogout={() => location.reload()} />;
  return <Workspace Navigation={Navigation} Composer={Composer} user={user} view={view} setView={setView}
    settingsContent={configuration} api={store.api} onRecordsChanged={unchanged} mockRecords={records} mockMode
    taskRecords={records} reminderRecords={records} notices={[]} onRead={unchanged}
    messages={messages} connected historyReady onSend={send}
    onAudio={() => send('Демонстрационный пример расшифровки аудио')} onClear={clearHistory} />;
}
