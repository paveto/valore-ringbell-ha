class DoorbellMicrophone extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this.rate = options?.processorOptions?.rate ?? 8000;
        this.frameSamples = this.rate / 50;
        this.phase = 0;
        this.total = 0;
        this.count = 0;
        this.reset();
    }

    reset() {
        this.block = new ArrayBuffer(this.frameSamples * 2);
        this.output = new DataView(this.block);
        this.index = 0;
    }

    process(inputs) {
        const samples = inputs[0]?.[0];
        if (!samples) return true;
        for (const sample of samples) {
            this.total += sample;
            this.count++;
            this.phase += this.rate;
            if (this.phase < sampleRate) continue;
            this.phase -= sampleRate;
            const value = Math.max(-1, Math.min(1, this.total / this.count));
            this.output.setInt16(this.index * 2, Math.round(value * 32767), true);
            this.total = this.count = 0;
            if (++this.index === this.frameSamples) {
                this.port.postMessage(this.block, [this.block]);
                this.reset();
            }
        }
        return true;
    }
}

registerProcessor("doorbell-microphone", DoorbellMicrophone);
