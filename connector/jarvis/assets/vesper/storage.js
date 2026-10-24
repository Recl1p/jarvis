/* Storage fallback for the JARVIS dashboard, loaded before every other script.
 *
 * When a browser blocks site data (a "block all cookies / site data" setting, some private modes), touching
 * window.localStorage throws a SecurityError, and the dashboard used to fail to start at all (a blank page).
 * In that case this swaps in an in-memory store with the same API: everything works for the visit, it just
 * isn't remembered. A store that reads fine but refuses writes is only replaced while it is empty, so a full
 * quota never hides settings the user already has. */
(() => {
  const memory = () => {
    const m = new Map();
    return {
      getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null),
      setItem: (k, v) => { m.set(String(k), String(v)); },
      removeItem: (k) => { m.delete(String(k)); },
      clear: () => { m.clear(); },
      key: (i) => [...m.keys()][i] ?? null,
      get length() { return m.size; },
    };
  };
  const usable = (name) => {
    let store;
    try { store = window[name]; store.getItem('__vesper_probe__'); } catch (e) { return false; }
    try { store.setItem('__vesper_probe__', '1'); store.removeItem('__vesper_probe__'); return true; }
    catch (e) { try { return store.length > 0; } catch (e2) { return false; } }
  };
  for (const name of ['localStorage', 'sessionStorage']) {
    if (usable(name)) continue;
    try { Object.defineProperty(window, name, { value: memory(), configurable: true, enumerable: true }); } catch (e) {}
  }
})();
