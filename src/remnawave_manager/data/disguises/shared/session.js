// Persistent sessions have no timer. An empty durable value records an explicit
// logout, so an older tab cannot resurrect its legacy sessionStorage session.
export function persistentSession(key, errorKey = 'storageError') {
  let last;
  return {
    get() {
      try {
        const saved = localStorage.getItem(key);
        if (saved !== null) return (last = saved);
        const legacy = sessionStorage.getItem(key) || '';
        last = legacy;
        if (legacy) {
          localStorage.setItem(key, legacy);
          sessionStorage.removeItem(key);
        }
        return last;
      } catch {
        if (last !== undefined) return last;
        throw new Error(errorKey);
      }
    },
    set(id) {
      try {
        localStorage.setItem(key, id || '');
      } catch {
        throw new Error(errorKey);
      }
      last = id || '';
      try { sessionStorage.removeItem(key); } catch {}
    },
  };
}
