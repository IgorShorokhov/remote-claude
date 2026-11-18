// Turns the microphone into 16 kHz mono 16-bit blocks of 100 ms for the server's dictation.
class DictationDown extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.ratio = options.processorOptions.rate / 16000;
    this.pos = 0;
    this.sum = 0;
    this.count = 0;
    this.block = new Int16Array(1600);
    this.filled = 0;
  }

  process(inputs) {
    var channel = inputs[0][0];
    if (!channel) return true;
    for (var i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      this.count += 1;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        var value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.block[this.filled++] = value < 0 ? value * 32768 : value * 32767;
        this.sum = 0;
        this.count = 0;
        if (this.filled === this.block.length) {
          this.port.postMessage(this.block.buffer.slice(0));
          this.filled = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("dictation-down", DictationDown);
