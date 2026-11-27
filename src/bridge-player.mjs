import { Player } from 'yt-cast-receiver';

export class BridgePlayer extends Player {
  constructor(worker) {
    super();
    this.worker = worker;
    this.replacing = 0;
    this.yielding = false;
    this.loadedSelection = null;
  }
  async play(video, position, AID) {
    this.replacing++;
    try { return await super.play(video, position, AID); }
    finally { this.replacing--; }
  }
  async request(method, args) {
    try {
      await this.worker.call(method, args);
      return true;
    } catch {
      return false;
    }
  }
  async doPlay(video, position) {
    // Only transfer fields needed for sender-authorized media resolution.
    // Never persist this context or include it in diagnostics.
    const result = await this.request('play', {
      video_id: video.id, position,
      context: { ctt: video.context?.ctt, params: video.context?.params }
    });
    if (result) this.loadedSelection = { id: video.id, index: video.context?.index };
    return result;
  }
  async doPause() { return this.request('pause'); }
  async doResume() { return this.request('resume'); }
  async doStop() {
    if (this.yielding) return true;
    const selected = this.queue.current;
    // setPlaylist changes selection and explicitly stops before calling play(),
    // outside our play() wrapper. Keep the MA input selected during replacement.
    const queueReplacement = selected && this.loadedSelection && (
      selected.id !== this.loadedSelection.id
      || selected.context?.index !== this.loadedSelection.index
    );
    const replacing = this.replacing || queueReplacement;
    if (!replacing) this.loadedSelection = null;
    return this.request(replacing ? 'track_stop' : 'finish');
  }
  async doSeek(position) { return this.request('seek', { position }); }
  async doSetVolume(volume) { return this.request('volume', volume); }
  async doGetVolume() { return this.worker.state.volume; }
  async doGetPosition() { return this.worker.state.position; }
  async doGetDuration() { return this.worker.state.duration; }
  async reset(AID) {
    this.loadedSelection = null;
    // Even if Player is already idle (e.g. Stop Casting during extraction),
    // invalidate the pending load and relinquish its MA destination.
    if (!this.yielding) await this.request('finish');
    return super.reset(AID);
  }
  async yieldToMA() {
    this.yielding = true;
    try { await super.reset(); }
    finally { this.yielding = false; }
  }
}
