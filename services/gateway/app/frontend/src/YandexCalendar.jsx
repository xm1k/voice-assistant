import {createContext, useCallback, useContext, useEffect, useRef, useState} from 'react';
import {api, collectPages} from './network';
import {YANDEX_CALENDAR_PATH as base, syncMap, syncLabel, canRetrySync, retrySync} from './yandex-calendar.js';
import {credentialsAllowed, discoverCalendars} from './yandex-calendar.js';
const Context = createContext({connection:{configured:false,status:'disconnected'}, items:{}});
export function YandexCalendarProvider({mockMode, children}) {
  const [connection, setConnection] = useState({configured:false,status:'disconnected'}), [items,setItems] = useState({}), [error,setError] = useState('');
  const serial = useRef(0);
  const reload = useCallback(async () => {
    if (mockMode) return;
    const request = ++serial.current;
    try {
      const [next, rows] = await Promise.all([api(base), collectPages(api, `${base}/sync`)]);
      if (request !== serial.current) return;
      setConnection(next); setItems(syncMap(rows)); setError('');
    } catch {if (request === serial.current) setError('Не удалось обновить состояние Яндекс Календаря.');}
  }, [mockMode]);
  useEffect(() => {
    reload(); window.addEventListener('yandex-calendar-refresh', reload);
    return () => {serial.current++; window.removeEventListener('yandex-calendar-refresh', reload);};
  }, [reload]);
  const retry = async item => {try {await retrySync(api,item); await reload();} catch {setError('Не удалось повторить синхронизацию. Обновите состояние и попробуйте снова.');}};
  return <Context.Provider value={{connection,items,reload,retry,setError}}>{error && <p className="yandex-notice form-error" role="alert">{error}<button type="button" onClick={reload}>Обновить</button></p>}{children}</Context.Provider>;
}
export function YandexSyncStatus({record, compact=false}) {
  const {connection,items,retry} = useContext(Context);
  if (record?.kind !== 'meeting' || !connection.configured || connection.status !== 'connected') return null;
  const item=items[record.id], label=syncLabel(item,connection);
  if (compact) return <span className={`yandex-sync-status yandex-sync-icon sync-${item?.status || 'local'}`} title={label} role="img" aria-label={label}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{item?.status === 'pending' ? <><path d="M20 7a9 9 0 0 0-15.5-1L3 8m0-5v5h5"/><path d="M4 17a9 9 0 0 0 15.5 1L21 16m0 5v-5h-5"/></> : <><path d="M6 18a4 4 0 0 1-.8-7.9A7 7 0 0 1 18.6 8a5 5 0 0 1 .4 10"/>{item?.status === 'synced' ? <path d="m9 16 3 3 5-5"/> : !item || item.status === 'disconnected' ? <path d="m9 15 6 6m0-6-6 6"/> : <><path d="M12 13v4"/><path d="M12 20h.01"/></>}</>}</svg></span>;
  return <span className="yandex-sync-status" title={label}>{label}{canRetrySync(item) && <button type="button" onClick={() => retry(item)}>Повторить синхронизацию</button>}</span>;
}
export function YandexCalendarSettings() {
  const {connection,reload,setError} = useContext(Context);
  const [busy,setBusy] = useState(false), [username,setUsername] = useState(''), [password,setPassword] = useState(''), [discovery,setDiscovery] = useState(null), [calendar,setCalendar] = useState('');
  const secure = typeof location !== 'undefined' && credentialsAllowed(location);
  const discover = async event => {
    event.preventDefault(); setBusy(true); setDiscovery(null); setCalendar('');
    try { const result = await discoverCalendars(api, {username,password}, secure, () => setPassword('')); setDiscovery(result); await reload(); }
    catch {setError('Не удалось проверить доступ. Проверьте логин и пароль приложения Яндекса.');}
    finally {setPassword(''); setBusy(false);}
  };
  const connect = async () => {setBusy(true); try {await api(`${base}/connect`,{method:'POST',body:JSON.stringify({generation:discovery.generation,calendar_id:calendar})}); setDiscovery(null); await reload();} catch {setError('Не удалось подключить выбранный календарь. Повторите проверку доступа.');} finally {setBusy(false);}};
  const disconnect = async () => {setBusy(true); try {await api(base,{method:'DELETE'}); setDiscovery(null); setPassword(''); await reload();} catch {setError('Не удалось отключить Яндекс Календарь.');} finally {setBusy(false);}};
  return <YandexConnectionForm connection={connection} busy={busy} secure={secure} username={username} password={password} discovery={discovery} calendar={calendar} onUsername={setUsername} onPassword={setPassword} onCalendar={setCalendar} onDiscover={discover} onConnect={connect} onDisconnect={disconnect} />;
}
export function YandexCalendarSyncButton({onFeedback}) {
  const {connection,reload} = useContext(Context);
  const [busy,setBusy] = useState(false);
  const synchronize = async () => {
    setBusy(true); onFeedback(null);
    try {
      const result = await api(`${base}/sync`, {method:'POST'});
      onFeedback({message: `Поставлено на синхронизацию: ${result.queued}. Результат появится в статусах встреч.`});
      await reload();
    } catch {onFeedback({error: true, message: 'Не удалось запустить синхронизацию встреч.'});}
    finally {setBusy(false);}
  };
  if (!connection.configured || connection.status !== 'connected') return null;
  return <div className="yandex-chat-sync"><button type="button" className="yandex-sync-button" disabled={busy} aria-busy={busy} aria-label={busy ? 'Синхронизация встреч…' : 'Синхронизировать встречи с Яндекс Календарём'} onClick={synchronize} title="Синхронизировать встречи с Яндекс Календарём"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 7a9 9 0 0 0-15.5-1L3 8m0-5v5h5"/><path d="M4 17a9 9 0 0 0 15.5 1L21 16m0 5v-5h-5"/></svg></button></div>;
}
export function YandexConnectionForm({connection,busy=false,secure=false,username='',password='',discovery=null,calendar='',onUsername,onPassword,onCalendar,onDiscover,onConnect,onDisconnect}) {
  return <div className="settings-section yandex-settings"><h2>Яндекс Календарь</h2><p>Наш сервис — источник данных для выбранного календаря Яндекса. Ручные изменения связанных встреч в Яндексе перезаписываются при синхронизации.</p>
    <p>{!connection.configured ? 'Интеграция отключена: администратор ещё не настроил сервис.' : connection.status === 'connected' ? `Подключён календарь: ${connection.calendar_name || 'Яндекс'}.` : connection.status === 'reconnect_required' ? 'Доступ отозван. Проверьте пароль приложения и подключите календарь заново.' : connection.status === 'select_calendar' ? 'Выберите календарь после проверки доступа. До подключения встречи не отправляются.' : connection.status === 'unavailable' ? 'Сервис временно недоступен.' : 'Календарь не подключён.'}</p>
    <details className="yandex-password-help"><summary>Как получить пароль приложения</summary><ol><li>Откройте <a href="https://id.yandex.ru/security/app-passwords" target="_blank" rel="noopener noreferrer">пароли приложений в Яндекс ID</a> и войдите в нужный аккаунт.</li><li>Если потребуется, разрешите использование паролей приложений.</li><li>Создайте пароль типа «Календарь», задайте название, например «Голос».</li><li>Скопируйте выданный пароль и вставьте его в поле ниже. Обычный пароль аккаунта не подходит.</li></ol><p>Сохраните пароль сразу: Яндекс показывает его только при создании. Если потеряли пароль, создайте новый.</p></details>
    {connection.status !== 'connected' && <form onSubmit={onDiscover} autoComplete="off"><label>Логин Яндекса<input value={username} onChange={event => onUsername(event.target.value)} required autoComplete="off" disabled={busy || !connection.configured || !secure} /></label><label>Пароль приложения «Календарь»<input type="password" value={password} onChange={event => onPassword(event.target.value)} required autoComplete="new-password" disabled={busy || !connection.configured || !secure} /></label>{!secure && <p role="alert">Для ввода пароля откройте приложение по HTTPS. Локальная разработка доступна на localhost.</p>}<button className="secondary-button" disabled={busy || !connection.configured || !secure}>Проверить доступ</button></form>}
    {discovery && <div><label>Календарь<select value={calendar} onChange={event => onCalendar(event.target.value)} disabled={busy}><option value="">Выберите календарь</option>{discovery.calendars.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{!discovery.calendars.length && <p>Нет календарей с правом записи. Создайте отдельный календарь в Яндексе и повторите проверку.</p>}<button type="button" className="secondary-button" disabled={busy || !calendar} onClick={onConnect}>Подключить выбранный календарь</button></div>}
    {['connected','reconnect_required','select_calendar'].includes(connection.status) && <button type="button" className="danger-button" disabled={busy} onClick={onDisconnect}>Отключить</button>}
  </div>;
}
export function YandexSyncProblems() {
  const {connection,items,retry} = useContext(Context);
  const problems=Object.values(items).filter(item => ['failed','conflict','missing'].includes(item.status) || ['reconnect_required','calendar_unavailable'].includes(item.code));
  if (!problems.length && connection.status !== 'reconnect_required') return null;
  return <details className="yandex-problems" open><summary>Проблемы Яндекс Календаря ({problems.length})</summary>{connection.status === 'reconnect_required' && <p role="alert">Доступ к Яндекс Календарю отозван. Подключите календарь заново в настройках.</p>}<ul>{problems.map(item => <li key={item.record_id}><b>{item.title || 'Удалённая встреча'}</b>: {syncLabel(item,connection)}{canRetrySync(item) && <button type="button" onClick={() => retry(item)}>Повторить синхронизацию</button>}</li>)}</ul></details>;
}
