class Mic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
  }
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) {
      for (const sample of channel) this.buffer.push(Math.max(-1, Math.min(1, sample)) * 0x7fff);
      if (this.buffer.length >= sampleRate / 10) {
        this.port.postMessage(Int16Array.from(this.buffer).buffer);
        this.buffer = [];
      }
    }
    return true;
  }
}

class Speaker extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.port.onmessage = (e) => {
      if (e.data === "clear") {
        this.queue = [];
        this.offset = 0;
      } else this.queue.push(new Int16Array(e.data));
    };
  }
  process(_, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      const chunk = this.queue[0];
      if (!chunk) {
        out[i] = 0;
        continue;
      }
      out[i] = chunk[this.offset++] / 0x8000;
      if (this.offset >= chunk.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic", Mic);
registerProcessor("speaker", Speaker);
