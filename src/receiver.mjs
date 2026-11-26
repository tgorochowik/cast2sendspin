import dial from '@patrickkfkan/peer-dial';
import YouTubeCastReceiver from 'yt-cast-receiver';

// yt-cast-receiver currently drops the uuid option when constructing peer-dial.
// Inject it only during synchronous construction, restoring the constructor
// immediately. Each receiver has a separate process, so no cross-route state
// is shared. This keeps discovery identity stable across restarts/renames.
export function createReceiver(player, options, uuid, Receiver = YouTubeCastReceiver) {
  const Original = dial.Server;
  dial.Server = class extends Original {
    constructor(dialOptions) { super({ ...dialOptions, uuid }); }
  };
  try { return new Receiver(player, options); }
  finally { dial.Server = Original; }
}
