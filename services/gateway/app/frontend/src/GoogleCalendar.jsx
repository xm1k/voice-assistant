import {createContext, useCallback, useContext, useEffect, useRef, useState} from 'react';
import {api, collectPages} from './network';
import {GOOGLE_CALENDAR_PATH as base, syncMap, syncLabel, canRetrySync, retrySync} from './google-calendar.js';
const Context = createContext({connection:{configured:false,status:'disconnected'}, items:{}});
export function GoogleCalendarProvider({mockMode, children}) {
  const [connection, setConnection] = useState({configured:false,status:'disconnected'}), [items,setItems] = useState({}), [error,setError] = useState(''), [outcome,setOutcome] = useState('');
  const serial = useRef(0);
  const reload = useCallback(async () => {
    if (mockMode) return;
    const request = ++serial.current;
    try {
      const [next, rows] = await Promise.all([api(base), collectPages(api, `${base}/sync`)]);
      if (request !== serial.current) return;
      setConnection(next); setItems(syncMap(rows)); setError('');
    } catch {if (request === serial.current) setError('Не удалось обновить состояние Google Calendar.');}
  }, [mockMode]);
  useEffect(() => {
    const url = new URL(location.href), result = url.searchParams.get('google_calendar');
    if (result) {setOutcome(result === 'connected' ? 'Google Calendar подключён. Состояние синхронизации показано отдельно.' : 'Не удалось подключить Google Calendar. Повторите подключение в настройках.'); url.searchParams.delete('google_calendar'); history.replaceState(null,'',url.pathname+url.search+url.hash);}
    reload();
    window.addEventListener('google-calendar-refresh', reload);
    return () => {serial.current++; window.removeEventListener('google-calendar-refresh',reload);};
  }, [reload]);
  const retry = async item => {try {await retrySync(api,item); await reload();} catch {setError('Не удалось повторить синхронизацию. Обновите состояние и попробуйте снова.');}};
  return <Context.Provider value={{connection,items,reload,retry,setError}}>{outcome && <p className="google-notice" role="status">{outcome}<button type="button" onClick={() => setOutcome('')} aria-label="Закрыть результат подключения">×</button></p>}{error && <p className="google-notice form-error" role="alert">{error}<button type="button" onClick={reload}>Обновить</button></p>}{children}</Context.Provider>;
}
export function GoogleSyncStatus({record, compact=false}) {
  const {connection,items,retry} = useContext(Context);
  if (record?.kind !== 'meeting') return null;
  const item=items[record.id], label=syncLabel(item,connection);
  return <span className="google-sync-status" title={label}>{compact ? (item?.status === 'synced' && connection.status === 'connected' ? 'Google ✓' : !item ? 'Только в Голосе' : 'Google ·') : label}{!compact && canRetrySync(item) && <button type="button" onClick={() => retry(item)}>Повторить синхронизацию</button>}</span>;
}
export function GoogleCalendarSettings() {
  const {connection,reload,setError} = useContext(Context), [busy,setBusy] = useState(false);
  const act = async disconnect => {setBusy(true); try {if (disconnect) {await api(base,{method:'DELETE'}); await reload();} else {const {url}=await api(`${base}/connect`,{method:'POST'}); const target=new URL(url); if(target.protocol !== 'https:' || target.hostname !== 'accounts.google.com') throw new Error(); location.assign(target.href);}} catch {setError('Не удалось изменить подключение Google Calendar.');} finally {setBusy(false);}};
  return <div className="settings-section"><h2>Google Calendar</h2><p>Встречи из «Голоса» отправляются в отдельный календарь «Голос». Изменения в Google не переносятся обратно.</p><p>{connection.status === 'unavailable' ? 'Сервис временно недоступен.' : !connection.configured ? 'Интеграция отключена: администратор ещё не настроил Google OAuth.' : connection.status === 'connected' ? 'Аккаунт подключён.' : connection.status === 'reconnect_required' ? 'Нужно подключить аккаунт заново.' : 'Аккаунт не подключён.'}</p>{connection.status !== 'connected' && <button type="button" className="secondary-button" disabled={busy || !connection.configured} onClick={() => act(false)}>{connection.status === 'reconnect_required' ? 'Подключить заново' : 'Подключить Google Calendar'}</button>}{['connected','reconnect_required'].includes(connection.status) && <button type="button" className="danger-button" disabled={busy} onClick={() => act(true)}>Отключить</button>}</div>;
}
export function GoogleSyncProblems() {
  const {connection,items,retry} = useContext(Context);
  const problems=Object.values(items).filter(item => ['failed','conflict','missing'].includes(item.status) || ['reconnect_required','calendar_unavailable'].includes(item.code));
  if (!problems.length) return null;
  return <details className="google-problems" open><summary>Проблемы Google Calendar ({problems.length})</summary><ul>{problems.map(item => <li key={item.record_id}><b>{item.title || 'Удалённая встреча'}</b>: {syncLabel(item,connection)}{canRetrySync(item) && <button type="button" onClick={() => retry(item)}>Повторить синхронизацию</button>}</li>)}</ul></details>;
}
