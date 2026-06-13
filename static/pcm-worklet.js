// AudioWorklet processor that downsamples mic input to 16 kHz Int16 PCM
// and posts batched chunks back to the main thread.
//
// Browsers typically run AudioContext at 48000 Hz. We resample on the fly
// using a simple linear interpolator. Quality is fine for ASR.

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = (options && options.processorOptions && options.processorOptions.targetSampleRate) || 16000;
    this.targetRate = target;
    this.ratio = sampleRate / target;
    this.inputBuf = [];      // accumulated input samples (Float32, source rate)
    this.outBuf = [];        // resampled samples queued for emission (Int16)
    this.flushEvery = Math.round(target * 0.1); // ~100 ms chunks
    this.muted = false;
    this.port.onmessage = (ev) => {
      if (ev.data && typeof ev.data.muted === 'boolean') {
        this.muted = ev.data.muted;
      }
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch0 = input[0];
    if (!ch0) return true;

    if (this.muted) {
      // Drop samples but keep returning true so the node stays alive.
      return true;
    }

    // Append source samples.
    for (let i = 0; i < ch0.length; i++) this.inputBuf.push(ch0[i]);

    // Resample by simple linear interp at non-integer ratio.
    // We track a fractional read position across process() calls.
    if (this._pos === undefined) this._pos = 0;
    while (this._pos + 1 < this.inputBuf.length) {
      const i0 = Math.floor(this._pos);
      const i1 = i0 + 1;
      const frac = this._pos - i0;
      const sample = this.inputBuf[i0] * (1 - frac) + this.inputBuf[i1] * frac;
      // Float32 [-1,1] → Int16
      const s = Math.max(-1, Math.min(1, sample));
      this.outBuf.push(s < 0 ? s * 0x8000 : s * 0x7fff);
      this._pos += this.ratio;
    }

    // Discard consumed input samples to keep buffer small.
    const consumed = Math.floor(this._pos);
    if (consumed > 0) {
      this.inputBuf.splice(0, consumed);
      this._pos -= consumed;
    }

    // Flush in ~100ms batches.
    while (this.outBuf.length >= this.flushEvery) {
      const chunk = this.outBuf.splice(0, this.flushEvery);
      const ab = new ArrayBuffer(chunk.length * 2);
      const dv = new DataView(ab);
      for (let i = 0; i < chunk.length; i++) dv.setInt16(i * 2, chunk[i] | 0, true);
      this.port.postMessage(ab, [ab]);
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
