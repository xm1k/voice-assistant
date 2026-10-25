import {YandexCalendarProvider, YandexCalendarSettings, YandexSyncProblems, YandexSyncStatus} from './YandexCalendar';
import {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import ChatHistory from './ChatHistory';
import ErrorBoundary from './ErrorBoundary';
import {createSpeaker} from './speech.js';
import Calendar, {RecordEditor, recordDraft, draftFromRecord} from './Calendar';
import Tasks from './Tasks';
import Reminders from './Reminders';
import {addDays, dateLabel, isoDate} from './dates';
import './app.css';
import {randomUUID, collectPages, createHistoryPager, eventGate, jobErrorMessage, api, addError} from './network';

const mock = import.meta.env.MODE === 'mock';
const demoRecords = [
  {id: 'meeting', kind: 'meeting', title: 'Синхронизация команды', start_at: new Date(Date.now() + 86400000).toISOString(), end_at: new Date(Date.now() + 90000000).toISOString(), status: 'active'},
  {id: 'task', kind: 'task', title: 'Отправить отчёт', due_date: isoDate(new Date()), status: 'active'},
  {id: 'reminder', kind: 'reminder', title: 'Позвонить Ивану', remind_at: new Date(Date.now() + 7200000).toISOString(), status: 'active'},
];
const demoNotices = [{id: 'notice', title: 'Позвонить Ивану', due_at: new Date().toISOString(), read: false}];
const defaultSettings = {theme: 'system', voice: false, browserNotifications: false};
const nav = [['assistant', 'chat', 'Ассистент'], ['calendar', 'calendar', 'Календарь'], ['tasks', 'tasks', 'Задачи'], ['reminders', 'bell', 'Напоминания'], ['settings', 'settings', 'Настройки']];

function AppIcon({name}) {
  const paths = {
    chat: <><path d="M5 5.5h14v10H9l-4 3v-13Z" /><path d="M9 9h6M9 12h4" /></>,
    calendar: <><rect x="4" y="5" width="16" height="15" rx="2" /><path d="M8 3v4M16 3v4M4 9h16M8 13h2M14 13h2M8 16h2" /></>,
    tasks: <><rect x="4" y="4" width="16" height="16" rx="2" /><path d="m8 12 2 2 5-5M8 17h8" /></>,
    bell: <><path d="M6.5 17h11l-1.5-2.5V10a4 4 0 0 0-8 0v4.5L6.5 17Z" /><path d="M10 20h4" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" /></>,
    microphone: <><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M6 11a6 6 0 0 0 12 0M12 17v4M9 21h6" /></>,
  };
  return <svg className="app-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function readSettings() {
  try { const saved = JSON.parse(decodeURIComponent(document.cookie.split('; ').find(item => item.startsWith('golos_ui='))?.split('=')[1] || '')); return Object.fromEntries(Object.entries(defaultSettings).map(([key, fallback]) => [key, saved[key] ?? fallback])); }
  catch { return {...defaultSettings}; }
}

function ErrorList({errors, onDismiss}) {
  return <div className="error-list">{errors.map(message => <div key={message} className="error-notice" role="alert"><p>{message}</p><button type="button" aria-label="Закрыть сообщение об ошибке" onClick={() => onDismiss(message)}>×</button></div>)}</div>;
}

function IconButton({label, children, ...props}) {
  return <button type="button" className="icon-button" aria-label={label} title={label} {...props}>{children}</button>;
}

function Navigation({view, setView, noticeCount, user}) {
  const buttons = () => nav.map(([id, icon, label]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}><AppIcon name={icon} /><span>{label}</span>{id === 'reminders' && noticeCount > 0 && <i>{noticeCount}</i>}</button>);
  return <><aside className="sidebar"><div className="brand"><span>г</span><strong>голос</strong></div><nav>{buttons()}</nav><div className="sidebar-user"><span className="avatar">{user.username.slice(0, 1).toUpperCase()}</span><b>{user.username}</b></div></aside><nav className="mobile-nav">{buttons()}</nav></>;
}

function Composer({onSend, onAudio, busy, mockMode}) {
  const [text, setText] = useState(''), [recording, setRecording] = useState(false);
  const recorder = useRef(null), stream = useRef(null), chunks = useRef([]), fileRef = useRef(null);
  useEffect(() => () => {stream.current?.getTracks().forEach(track => track.stop());}, []);
  const stop = () => recorder.current?.stop();
  const record = async () => {
    if (mockMode) return;
    if (recording) return stop();
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({audio: true}); chunks.current = [];
      recorder.current = new MediaRecorder(stream.current);
      recorder.current.ondataavailable = event => chunks.current.push(event.data);
      recorder.current.onstop = () => {stream.current?.getTracks().forEach(track => track.stop()); setRecording(false); onAudio(new Blob(chunks.current, {type: recorder.current.mimeType}));};
      recorder.current.start(); setRecording(true);
    } catch { onAudio(null, 'Разрешите доступ к микрофону или выберите аудиофайл.'); }
  };
  const submit = async event => {event.preventDefault(); if (recording) return stop(); if (text.trim() && await onSend(text.trim())) setText('');};
  const keyDown = event => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (!busy && (text.trim() || recording)) event.currentTarget.form?.requestSubmit();
  };
  return <div className="composer-wrap"><form className="composer" onSubmit={submit}><IconButton label="Загрузить аудио" onClick={() => fileRef.current?.click()}>＋</IconButton><input ref={fileRef} hidden type="file" accept="audio/*" onChange={event => event.target.files[0] && onAudio(event.target.files[0])} /><textarea value={text} rows="1" maxLength="8000" disabled={busy} onKeyDown={keyDown} onChange={event => setText(event.target.value)} placeholder="Напишите или скажите, что нужно сделать" /><button type="button" className={`voice-button ${recording ? 'recording' : ''}`} onClick={record} disabled={busy || mockMode} aria-label={recording ? 'Остановить запись' : 'Начать запись'}>{recording ? '■' : <AppIcon name="microphone" />}</button><button className="send-button" aria-label="Отправить" disabled={busy || mockMode || (!text.trim() && !recording)}>{recording ? '✓' : '↑'}</button></form><small className="composer-hint">Enter — отправить · Shift+Enter — новая строка</small></div>;
}

function Assistant({messages, pending, onSend, onAudio, busy, mockMode, records, timezone, onRecordsChanged, historyCursor, historyReady, loadingOlder, historyRevision, onEarlier, connected}) {
  const [quickCreate, setQuickCreate] = useState(null);
  const create = kind => setQuickCreate({draft: recordDraft(kind)});
  return <div className="assistant-layout"><section className="chat-screen"><header className="assistant-head"><div><small>{pending ? 'Жду уточнение' : 'Личный помощник'}</small><h1>Ассистент</h1></div><span className={pending ? 'status pending' : 'status'}>{pending ? 'Ответьте на вопрос' : 'Готов'}</span></header><div className="suggestions quick-create"><button onClick={() => create('meeting')}>＋ Встреча</button><button onClick={() => create('reminder')}>＋ Напоминание</button><button onClick={() => create('task')}>＋ Задача</button></div><ChatHistory messages={messages} busy={busy} connected={connected} mockMode={mockMode} timezone={timezone} api={api} onOpen={record => setQuickCreate({record, draft: draftFromRecord(record, timezone)})} cursor={historyCursor} ready={historyReady} loadingOlder={loadingOlder} revision={historyRevision} onEarlier={onEarlier} /><Composer onSend={onSend} onAudio={onAudio} busy={busy || !connected} mockMode={mockMode} /></section><aside className="agenda"><h2>Ближайшее</h2>{records.length ? records.slice(0, 7).map((record, index) => <article key={`${record.id}-${record.occurrence_at || index}`}><small>{record.kind === 'meeting' ? 'Встреча' : record.kind === 'reminder' ? 'Напоминание' : 'Задача'}</small><b>{record.title}</b><time>{dateLabel(record.start_at || record.remind_at || record.due_date, timezone)}</time><YandexSyncStatus record={record} /></article>) : <p>Пока ничего нет.</p>}</aside>{quickCreate && <RecordEditor {...quickCreate} timezone={timezone} api={api} onSaved={onRecordsChanged} onClose={() => setQuickCreate(null)} />}</div>;
}

function Settings({settings, setSettings, user, onTimezone, onNotifications, onClear, onLogout}) {
  const zones = useMemo(() => ['Europe/Moscow', 'UTC', ...(Intl.supportedValuesOf?.('timeZone') || [])].filter((zone, index, all) => all.indexOf(zone) === index), []);
  const [timezone, setTimezone] = useState(user.timezone), [saved, setSaved] = useState(false), [error, setError] = useState('');
  const [provider, setProvider] = useState(''), [providers, setProviders] = useState([]), [providerSaved, setProviderSaved] = useState(false);
  const update = (key, value) => setSettings({...settings, [key]: value});
  const saveTimezone = async () => {try {setError(''); await onTimezone(timezone); setSaved(true); setTimeout(() => setSaved(false), 1500);} catch (exception) {setSaved(false); setError(exception.message);}};
  useEffect(() => {if (!mock) api('/api/v1/settings').then(data => {setProvider(data.llm_provider || ''); setProviders(data.llm_providers || []);}).catch(exception => setError(exception.message));}, []);
  const saveProvider = async () => {try {setError(''); await api('/api/v1/settings', {method: 'PUT', body: JSON.stringify({llm_provider: provider})}); setProviderSaved(true); setTimeout(() => setProviderSaved(false), 1500);} catch (exception) {setProviderSaved(false); setError(exception.message);}};
  return <section className="page settings">{error && <p className="form-error" role="alert">{error}</p>}<header><small>Ваш аккаунт</small><h1>Настройки</h1></header><div className="settings-section"><h2>Внешний вид</h2><p>Единственная визуальная настройка — тема приложения.</p><div className="theme-picker">{[['system', 'Системная'], ['light', 'Светлая'], ['dark', 'Тёмная']].map(([value, label]) => <button key={value} className={settings.theme === value ? 'active' : ''} onClick={() => update('theme', value)}>{label}</button>)}</div></div><div className="settings-section"><h2>Ассистент</h2><label className="toggle"><input type="checkbox" checked={settings.voice} onChange={event => update('voice', event.target.checked)} /><span>Озвучивать ответы</span></label>{'Notification' in window && <label className="toggle"><input type="checkbox" checked={settings.browserNotifications} onChange={event => onNotifications(event.target.checked)} /><span>Уведомления браузера</span></label>}</div>{providers.length > 0 && <div className="settings-section"><h2>Провайдер ИИ</h2><p>Языковая модель, которую использует ассистент.</p><div className="inline-control"><select value={provider} onChange={event => setProvider(event.target.value)}>{providers.map(p => <option key={p.name} value={p.name}>{p.label}</option>)}</select><button className="secondary-button" onClick={saveProvider}>{providerSaved ? 'Сохранено' : 'Сохранить'}</button></div></div>}<YandexCalendarSettings /><div className="settings-section"><h2>Время</h2><p>Часовой пояс используется для голосовых команд и календаря.</p><div className="inline-control"><select value={timezone} onChange={event => setTimezone(event.target.value)}>{zones.map(zone => <option key={zone}>{zone}</option>)}</select><button className="secondary-button" onClick={saveTimezone}>{saved ? 'Сохранено' : 'Сохранить'}</button></div></div><div className="settings-section"><h2>История</h2><p>Ассистент хранит одну непрерывную историю и использует последние сообщения и выбранные записи как контекст.</p><button className="secondary-button" onClick={onClear}>Очистить историю и контекст</button></div><div className="settings-section account-row"><div><h2>{user.username}</h2><p>{user.timezone}</p></div><button className="danger-button" onClick={onLogout}>Выйти из аккаунта</button></div></section>;
}

function Login({onLogin}) {
  const [registering, setRegistering] = useState(false), [error, setError] = useState('');
  const zones = useMemo(() => ['Europe/Moscow', 'UTC', ...(Intl.supportedValuesOf?.('timeZone') || [])].filter((zone, index, array) => array.indexOf(zone) === index), []);
  const submit = async event => {event.preventDefault(); try {setError(''); await onLogin(registering ? 'register' : 'login', Object.fromEntries(new FormData(event.currentTarget)));} catch (exception) {setError(exception.message);}};
  return <main className="login"><form onSubmit={submit}><span className="login-mark">г</span><h1>Голос</h1><p>Один помощник для задач, встреч и напоминаний.</p><label>Логин<input name="username" required minLength="3" autoComplete="username" /></label><label>Пароль<input name="password" type="password" required minLength="8" autoComplete="current-password" /></label>{registering && <label>Часовой пояс<select name="timezone" defaultValue="Europe/Moscow">{zones.map(zone => <option value={zone} key={zone}>{zone}</option>)}</select></label>}{error && <p className="form-error" role="alert">{error}</p>}<button className="primary-action">{registering ? 'Создать аккаунт' : 'Войти'}</button><button type="button" className="text-action" onClick={() => setRegistering(!registering)}>{registering ? 'У меня есть аккаунт' : 'Создать аккаунт'}</button></form></main>;
}

function App() {
  const [user, setUser] = useState(mock ? {id: 'demo', username: 'Денис', timezone: 'Europe/Moscow'} : null);
  const [view, setView] = useState('assistant'), [pending, setPending] = useState(false), [records, setRecords] = useState(mock ? demoRecords : []), [taskRecords, setTaskRecords] = useState(mock ? demoRecords.filter(item => item.kind === 'task') : []), [notices, setNotices] = useState(mock ? demoNotices : []), [busy, setBusy] = useState(false), [settings, setSettings] = useState(readSettings());
  const [reminderRecords, setReminderRecords] = useState(mock ? demoRecords.filter(item => item.kind === 'reminder') : []);
  const [errors, setErrors] = useState([]), [connected, setConnected] = useState(mock), [revision, setRevision] = useState(0);
  const [history, setHistory] = useState({messages: mock ? [{id:'demo', role:'assistant', text:'Добрый день. Что запланируем?'}] : [], cursor:null, ready:mock, loadingOlder:false, revision:0});
  const {messages, cursor:historyCursor, ready:historyReady, loadingOlder, revision:historyRevision} = history;
  const historyPager = useRef(null);
  if (!historyPager.current) historyPager.current = createHistoryPager(api, setHistory, history);
  const speaker = useMemo(() => createSpeaker(user?.id), [user?.id]);
  useEffect(() => {if (!settings.voice) speaker.cancel();}, [settings.voice, speaker]);
  useEffect(() => () => speaker.cancel(), [speaker]);
  const knownNotices = useRef(new Set()), noticesReady = useRef(false), activeJobs = useRef(new Map()), completedJobs = useRef(new Set()), acceptEvent = useRef(eventGate()), latest = useRef({});
  const pendingSend = useRef(null);
  const setError = exception => {if (exception) setErrors(current => addError(current, exception));};
  const fail = exception => setError(exception);
  const dismissError = message => setErrors(current => current.filter(item => item !== message));
  const reloadOverview = async () => {
    if (mock) return;
    const start = isoDate(new Date()), end = isoDate(addDays(new Date(), 90));
    const [plans, reminderData, tasks, reminders] = await Promise.all([collectPages(api, `/api/v1/records?period_start=${start}&period_end=${end}`), collectPages(api, '/api/v1/notifications'), collectPages(api, '/api/v1/tasks'), collectPages(api, '/api/v1/reminders')]);
    const fresh = reminderData.filter(item => !item.read && !knownNotices.current.has(item.id));
    if (noticesReady.current && settings.browserNotifications && 'Notification' in window && Notification.permission === 'granted') fresh.forEach(item => new Notification('Голос', {body: item.title}));
    reminderData.forEach(item => knownNotices.current.add(item.id)); noticesReady.current = true;
    window.dispatchEvent(new Event('yandex-calendar-refresh')); setRecords(plans); setReminderRecords(reminders); setTaskRecords(tasks); setNotices(reminderData); setRevision(value => value + 1);
  };
  const loadHistory = (earlier = false, replace = false) => historyPager.current.load({older:earlier, replace});
  const updateBusy = () => setBusy(activeJobs.current.size ? ([...activeJobs.current.values()].includes('transcription') ? 'transcription' : 'assistant') : false);
  const handleJob = async job => {
    const id = job.id || job.job_id;
    if (completedJobs.current.has(id)) {activeJobs.current.delete(id); updateBusy(); return;}
    if (!['succeeded', 'failed', 'cancelled'].includes(job.status)) {activeJobs.current.set(id, job.kind || activeJobs.current.get(id) || 'assistant'); updateBusy(); return;}
    activeJobs.current.delete(id); completedJobs.current.add(id);
    const assistantId = job.result?.assistant_job_id;
    if (job.status === 'succeeded' && assistantId && !completedJobs.current.has(assistantId)) activeJobs.current.set(assistantId, 'assistant');
    updateBusy();
    if (job.status === 'cancelled') return;
    if (job.status === 'failed') {if (job.result?.message_id) await loadHistory(); else setError(jobErrorMessage(job.error)); return;}
    if (assistantId) {
      // The server already persisted the transcript and queued the assistant.
      await Promise.all([loadHistory(), assistantId ? api(`/api/v1/jobs/${assistantId}`).then(handleJob) : Promise.resolve()]);
    } else {
      setPending(Boolean(job.result?.pending));
      if (settings.voice && job.result?.answer) await speaker.speakOnce(job.result.message_id || id, job.result.answer);
      await Promise.all([loadHistory(), reloadOverview()]);
    }
  };
  const reconcile = async () => {
    const [chat, jobs] = await Promise.all([api('/api/v1/chat'), collectPages(api, '/api/v1/jobs?status=active'), loadHistory(false, true), reloadOverview()]);
    setPending(Boolean(chat.pending));
    for (const id of activeJobs.current.keys()) await handleJob(await api(`/api/v1/jobs/${id}`));
    for (const job of jobs) await handleJob(job);
    updateBusy();
  };
  useEffect(() => {document.documentElement.dataset.theme = settings.theme; const secure = location.protocol === 'https:' ? '; Secure' : ''; document.cookie = `golos_ui=${encodeURIComponent(JSON.stringify(settings))}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;}, [settings]);
  useEffect(() => {if (!mock) api('/api/v1/auth/me').then(setUser).catch(exception => {if (exception.status !== 401) setError(exception.message);});}, []);
  latest.current = {reconcile, handleJob, reloadOverview};
  useEffect(() => {
    if (!user || mock) return;
    let disposed = false, socket, retry, delay = 1000;
    const open = () => {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/events`);
      socket.onopen = () => {if (disposed) return; delay=1000; setConnected(true); window.dispatchEvent(new Event('yandex-calendar-refresh')); latest.current.reconcile().catch(fail);};
      socket.onmessage = event => {
        try {
          const update = JSON.parse(event.data); if (!acceptEvent.current(update.id)) return;
          if (update.type === 'message') historyPager.current.merge([update.data]);
          else if (update.type === 'job') latest.current.handleJob(update.data).catch(fail);
          else if (update.type === 'history_cleared') {historyPager.current.reset(); speaker.cancel(); setPending(false); activeJobs.current.clear(); pendingSend.current = null; setBusy(false);}
          else if (update.type === 'yandex_calendar_sync') window.dispatchEvent(new Event('yandex-calendar-refresh'));
          else if (['notification', 'records_changed'].includes(update.type)) latest.current.reloadOverview().catch(fail);
        } catch (exception) {fail(exception);}
      };
      socket.onclose = () => {if (disposed) return; setConnected(false); setBusy(false); retry=setTimeout(open,delay); delay=Math.min(15000,delay*2);};
      socket.onerror = () => socket.close();
    };
    open();
    return () => {disposed=true; clearTimeout(retry); socket?.close();};
  }, [user?.id]);
  const send = async text => {
    if (mock || !connected || busy) return;
    setBusy('assistant'); setError('');
    let accepted = false;
    if (pendingSend.current?.text !== text) pendingSend.current = {text, request_id:randomUUID()};
    try {const job = await api('/api/v1/chat/messages', {method:'POST',body:JSON.stringify(pendingSend.current)}); accepted = true; pendingSend.current = null; activeJobs.current.set(job.job_id, 'assistant'); await handleJob(await api(`/api/v1/jobs/${job.job_id}`)); await loadHistory(); return true;}
    catch (exception) {fail(exception); setBusy(false); return accepted;}
  };
  const upload = async (file, audioError) => {
    if (audioError) return setError(audioError);
    if (!file || mock || busy || !connected) return;
    setBusy('transcription'); setError('');
    try {const body=new FormData(); body.append('file',file,file.name || 'recording.webm'); const job=await api('/api/v1/audio',{method:'POST',headers:{'Idempotency-Key':randomUUID()},body}); activeJobs.current.set(job.job_id, 'transcription'); await handleJob(await api(`/api/v1/jobs/${job.job_id}`));}
    catch (exception) {fail(exception); setBusy(false);}
  };
  const login = async (mode, data) => setUser(await api(`/api/v1/auth/${mode}`, {method: 'POST', body: JSON.stringify(data)}));
  const logout = async () => {if (!mock) await api('/api/v1/auth/logout', {method: 'POST'}); location.reload();};
  const readNotice = async id => {if (!mock) await api(`/api/v1/notifications/${id}/read`, {method: 'POST'}); setNotices(current => current.map(item => item.id === id ? {...item, read: true} : item));};
  const setBrowserNotifications = async enabled => {if (!enabled) return setSettings({...settings, browserNotifications: false}); if (!('Notification' in window)) return; const permission = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission; setSettings({...settings, browserNotifications: permission === 'granted'}); if (permission !== 'granted') setError('Разрешите уведомления для этого сайта в настройках браузера.');};
  const updateTimezone = async timezone => {if (mock) return setUser({...user, timezone}); setUser(await api('/api/v1/auth/me', {method: 'PATCH', body: JSON.stringify({timezone})})); await reloadOverview();};
  const clearHistory = async () => {if (!window.confirm('Очистить историю разговора и текущий контекст? Задачи и встречи останутся.')) return; if (!mock) await api('/api/v1/chat/messages', {method: 'DELETE'}); historyPager.current.reset(); speaker.cancel(); setPending(false); setBusy(false); activeJobs.current.clear(); pendingSend.current = null;};
  if (!user) return <><ErrorList errors={errors} onDismiss={dismissError} /><Login onLogin={login} /></>;
  const unread = notices.filter(item => !item.read).length;
  return <YandexCalendarProvider key={user.id} mockMode={mock}><div className="shell"><Navigation view={view} setView={setView} noticeCount={unread} user={user} /><main className="main"><YandexSyncProblems /><ErrorList errors={errors} onDismiss={dismissError} />{view === 'assistant' && <Assistant connected={connected} historyCursor={historyCursor} onEarlier={() => loadHistory(true)} historyReady={historyReady} loadingOlder={loadingOlder} historyRevision={historyRevision} messages={messages} pending={pending} onSend={send} onAudio={upload} busy={busy} mockMode={mock} records={records} timezone={user.timezone} onRecordsChanged={reloadOverview} />}{view === 'calendar' && <Calendar revision={revision} timezone={user.timezone} api={api} mockRecords={mock ? demoRecords : null} onChanged={reloadOverview} />}{view === 'tasks' && <Tasks records={taskRecords} timezone={user.timezone} api={api} onChanged={reloadOverview} mockMode={mock} />}{view === 'reminders' && <Reminders api={api} items={notices} records={reminderRecords} timezone={user.timezone} onRead={id => readNotice(id).catch(fail)} onChanged={reloadOverview} mockMode={mock} />}{view === 'settings' && <Settings settings={settings} setSettings={setSettings} user={user} onTimezone={updateTimezone} onNotifications={enabled => setBrowserNotifications(enabled).catch(fail)} onClear={() => clearHistory().catch(fail)} onLogout={() => logout().catch(fail)} />}</main></div></YandexCalendarProvider>;
}

createRoot(document.querySelector('#root')).render(<ErrorBoundary><App /></ErrorBoundary>);
