'use strict';

const DEFAULT_POLL_INTERVAL_MS = 30000;
const MIN_POLL_INTERVAL_MS = 5000;

class DashboardEventConsumer {
  constructor(options) {
    const opts = options || {};
    this._fetchEvents = typeof opts.fetchEvents === 'function' ? opts.fetchEvents : async () => ({ events: [], last_seq: 0, head_seq: 0, resync_required: false });
    this._onEvents = typeof opts.onEvents === 'function' ? opts.onEvents : () => {};
    this._onResync = typeof opts.onResync === 'function' ? opts.onResync : () => {};
    const requested = Number.isFinite(opts.pollIntervalMs) ? Math.max(0, opts.pollIntervalMs) : DEFAULT_POLL_INTERVAL_MS;
    this._pollIntervalMs = requested === 0 ? 0 : Math.max(MIN_POLL_INTERVAL_MS, requested);
    this._setInterval = typeof opts.setInterval === 'function' ? opts.setInterval : setInterval;
    this._clearInterval = typeof opts.clearInterval === 'function' ? opts.clearInterval : clearInterval;
    this._lastSeq = 0;
    this._seenAny = false;
    this._pending = [];
    this._flushPending = false;
    this._timer = null;
    this._stopped = false;
    this._resyncQueued = false;
  }

  start() {
    if (this._stopped) return;
    this.poll();
    if (this._pollIntervalMs > 0 && this._timer === null) {
      this._timer = this._setInterval(() => this.poll(), this._pollIntervalMs);
    }
  }

  stop() {
    this._stopped = true;
    if (this._timer !== null) {
      this._clearInterval(this._timer);
      this._timer = null;
    }
    this._pending = [];
    this._flushPending = false;
  }

  async poll() {
    if (this._stopped) return;
    try {
      const result = await this._fetchEvents(this._lastSeq);
      this.handleBatch(result);
    } catch (_err) {
      this.requestResync();
    }
  }

  handleBatch(result) {
    if (!result) return;
    const events = Array.isArray(result.events) ? result.events : [];
    const resync = result.resync_required === true || result.overflow === true || result.reconnect === true || result.replacement === true;
    let gap = false;
    if (events.length > 0) {
      const firstSeq = Number.isFinite(events[0].seq) ? events[0].seq : Number.NaN;
      gap = this._seenAny && Number.isFinite(firstSeq) && firstSeq > this._lastSeq + 1;
    }
    if (resync || gap || result.runtime_replaced === true) {
      this.requestResync();
      return;
    }
    for (const event of events) {
      if (!this._seenAny || event.seq > this._lastSeq) {
        this._pending.push(event);
        this._lastSeq = Math.max(this._lastSeq, Number.isFinite(event.seq) ? event.seq : this._lastSeq);
        this._seenAny = true;
      }
    }
    this._scheduleFlush();
  }

  _scheduleFlush() {
    if (this._flushPending || this._pending.length === 0) return;
    this._flushPending = true;
    const flush = () => {
      this._flushPending = false;
      this.flush();
    };
    if (typeof queueMicrotask === 'function') queueMicrotask(flush);
    else Promise.resolve().then(flush);
  }

  flush() {
    if (this._pending.length === 0) return;
    const batch = this._pending.splice(0, this._pending.length);
    this._onEvents(batch, { last_seq: this._lastSeq });
  }

  requestResync() {
    if (this._resyncQueued) return;
    this._resyncQueued = true;
    this._pending = [];
    this._lastSeq = 0;
    this._seenAny = false;
    this._resyncQueued = false;
    this._onResync();
  }

  get lastSeq() {
    return this._lastSeq;
  }

  resetAfterReconnect() { this.requestResync(); }
  replaceRuntime() { this.requestResync(); }
  forceResync() { this.requestResync(); }
}

function createDashboardEventConsumer(options) {
  return new DashboardEventConsumer(options);
}

module.exports = {
  DashboardEventConsumer,
  createDashboardEventConsumer,
  DEFAULT_POLL_INTERVAL_MS,
  MIN_POLL_INTERVAL_MS
};
