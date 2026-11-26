import { Player } from 'yt-cast-receiver';

export class BridgePlayer extends Player {
  constructor(worker) {
    super();
    this.worker = worker;
    this.replacing = 0;
    this.yielding = false;
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
    return this.request('play', {
      video_id: video.id, position,
      context: { ctt: video.context?.ctt, params: video.context?.params }
    });
  }
  async doPause() { return this.request('pause'); }
  async doResume() { return this.request('resume'); }
  async doStop() {
    if (this.yielding) return true;
    return this.request(this.replacing ? 'track_stop' : 'finish');
  }
  async doSeek(position) { return this.request('seek', { position }); }
  async doSetVolume(volume) { return this.request('volume', volume); }
  async doGetVolume() { return this.worker.state.volume; }
  async doGetPosition() { return this.worker.state.position; }
  async doGetDuration() { return this.worker.state.duration; }
  async reset(AID) {
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
