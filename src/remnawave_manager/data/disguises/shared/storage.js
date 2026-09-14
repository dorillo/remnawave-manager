const prefix = 'disguise:';
export const storage = {
  get(key, fallback = null) { try { const value = localStorage.getItem(prefix + key); return value == null ? fallback : JSON.parse(value); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(prefix + key, JSON.stringify(value)); return true; } catch { return false; } },
  remove(key) { try { localStorage.removeItem(prefix + key); } catch { /* storage disabled */ } },
};
export const toggleSet = (key, id) => { const values = new Set(storage.get(key, [])); values.has(id) ? values.delete(id) : values.add(id); storage.set(key, [...values]); return values; };
export const getTheme = () => storage.get('theme', 'light');
export const setTheme = (theme) => { storage.set('theme', theme); document.documentElement.dataset.theme = theme; };
