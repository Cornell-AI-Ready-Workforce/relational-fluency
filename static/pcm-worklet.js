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
    // Muting substitutes silence for the participant's voice; it NEVER stops
    // the stream. Whatever arrives at the server is appended straight onto
    // user_audio.wav (server/storage.py append_user_audio), so a mute that
    // dropped samples excised that stretch of time from the file and turned the
    // recording into a compacted stream instead of a timeline that the
    // per-turn timestamps in encounter_record.py can index into.
    //
    // Both participant-facing pages load this same module (static/v2.html and
    // the legacy static/participant.html), and as of this change neither one
    // asserts mute any more: an agent turn the participant talks over is
    // precisely the behaviour the study scores, so it has to reach the
    // recording. The mute path stays because the port message is the contract
    // with those pages, and because a caller that does use it should still get
    // a continuous stream rather than a hole.
    this.muted = false;
    this.port.onmessage = (ev) => {
      if (ev.data && typeof ev.data.muted === 'boolean') {
        this.muted = ev.data.muted;
        // Deliberately no buffer flush here. outBuf only ever drains in whole
        // flushEvery batches, so up to ~100 ms of genuine pre-mute participant
        // audio can be sitting in it when this message arrives, and it goes out
        // after the mute, ahead of the zeros. That is the right outcome: it is
        // real speech, it is emitted in order, and the sample count per unit of
        // wall-clock time is unchanged — which is the property the recorded
        // timeline actually rests on. Clearing the buffers would have deleted
        // that speech and shortened the file by up to a tenth of a second at
        // every mute.
      }
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch0 = input[0];
    if (!ch0) return true;

    // Append source samples. While muted we still append one sample per input
    // sample — a zero one — so the emitted byte count per unit of wall-clock
    // time is unchanged and the recorded WAV stays a true timeline.
    if (this.muted) {
      for (let i = 0; i < ch0.length; i++) this.inputBuf.push(0);
    } else {
      for (let i = 0; i < ch0.length; i++) this.inputBuf.push(ch0[i]);
    }

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
    // Clamp to what can actually be spliced so _pos does not slip when the
    // read position ran past the end of the buffer.
    const consumed = Math.min(Math.floor(this._pos), this.inputBuf.length);
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
