import { getQuickJS } from 'quickjs-emscripten';
import { Platform, Log } from 'youtubei.js';

// Library diagnostics may contain signed URLs or credential-bearing requests.
Log.setLevel(Log.Level.NONE);

export async function sandboxEvaluate(data) {
  const engine = await getQuickJS();
  const runtime = engine.newRuntime();
  runtime.setMemoryLimit(64 * 1024 * 1024);
  runtime.setMaxStackSize(1024 * 1024);
  const deadline = Date.now() + 3000;
  runtime.setInterruptHandler(() => Date.now() > deadline);
  const context = runtime.newContext();
  try {
    // Execute only in QuickJS: no host callbacks, filesystem, network or env.
    const result = context.evalCode(`(function(){${data.output}\n})()`);
    if (result.error) {
      result.error.dispose();
      throw new Error('DECIPHER_FAILED');
    }
    try { return context.dump(result.value); }
    finally { result.value.dispose(); }
  } finally {
    context.dispose();
    runtime.dispose();
  }
}

Platform.shim.eval = sandboxEvaluate;
