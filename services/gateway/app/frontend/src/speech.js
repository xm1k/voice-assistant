// Browser-native coordination: one reply must not speak in two tabs or after HTTP/WS redelivery.
export function createSpeaker(userId, env = globalThis) {
  const seen = new Set(), storageKey = `golos_spoken_v1:${userId}`;
  return {
    async speakOnce(id, text) {
      if (!id || !text || seen.has(id) || !env.speechSynthesis || !env.SpeechSynthesisUtterance) return false;
      seen.add(id);
      const speak = () => {
        let saved = [];
        try {const value = JSON.parse(env.localStorage?.getItem(storageKey) || '[]'); if (Array.isArray(value)) saved = value;} catch { /* Storage can be disabled. In-memory protection still applies. */ }
        if (saved.includes(id)) return false;
        try {env.localStorage?.setItem(storageKey, JSON.stringify([...saved.slice(-199), id]));} catch { /* Same-tab protection remains available. */ }
        const utterance = new env.SpeechSynthesisUtterance(text);
        utterance.lang = 'ru-RU';
        env.speechSynthesis.speak(utterance);
        return true;
      };
      try {
        return env.navigator?.locks?.request ? await env.navigator.locks.request(storageKey, speak) : speak();
      } catch {return false;}
    },
    cancel() {env.speechSynthesis?.cancel();},
  };
}
