import {initial, type State} from './workshop.ts';

// A separate demo-only database: never the installed client's profile or credential store.
export function openDemo(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open('moeguard-workshop-dev-v1', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('snapshot');
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
export function restore(db: IDBDatabase): Promise<State> {
  return new Promise((resolve, reject) => {
    const r = db.transaction('snapshot').objectStore('snapshot').get('current');
    r.onsuccess = () => resolve(r.result ?? initial());
    r.onerror = () => reject(r.error);
  });
}
export function persist(db: IDBDatabase, state: State): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = db.transaction('snapshot', 'readwrite');
    t.oncomplete = () => resolve();
    t.onabort = () => reject(t.error);
    t.objectStore('snapshot').put(state, 'current');
  });
}
