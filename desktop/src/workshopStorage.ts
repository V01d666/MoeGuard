import type {State} from './workshop.ts';

export type WorkshopStorage = {save(state: State): Promise<void>};

export function createCheckpoint(storage: WorkshopStorage, failed: () => void = () => {}, saved: () => void = () => {}) {
  let pending: State | undefined;
  let active: Promise<void> | undefined;
  function drain() {
    if (active) return active;
    active = Promise.resolve().then(async () => {
      while (pending) {
        const writing = pending;
        pending = undefined;
        try {await storage.save(writing); saved();}
        catch (error) {pending ??= writing; failed(); throw error;}
      }
    }).finally(() => {active = undefined;});
    return active;
  }
  return {
    schedule(state: State) {pending = state; void drain().catch(() => {});},
    async flush() {while (active || pending) await drain();},
  };
}
