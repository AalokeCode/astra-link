class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.sourceChunkSize = Math.max(128, Math.round(sampleRate * 0.02))
    this.targetChunkSize = 320
    this.pending = new Float32Array(this.sourceChunkSize)
    this.pendingLength = 0
  }

  emitChunk() {
    const pcm = new Int16Array(this.targetChunkSize)
    const ratio = this.sourceChunkSize / this.targetChunkSize
    for (let index = 0; index < pcm.length; index += 1) {
      const position = index * ratio
      const left = Math.floor(position)
      const right = Math.min(left + 1, this.sourceChunkSize - 1)
      const fraction = position - left
      const value = this.pending[left] + (this.pending[right] - this.pending[left]) * fraction
      pcm[index] = Math.max(-32768, Math.min(32767, value * 32767))
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer])
  }

  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input) return true

    let offset = 0
    while (offset < input.length) {
      const count = Math.min(input.length - offset, this.sourceChunkSize - this.pendingLength)
      this.pending.set(input.subarray(offset, offset + count), this.pendingLength)
      this.pendingLength += count
      offset += count
      if (this.pendingLength === this.sourceChunkSize) {
        this.emitChunk()
        this.pendingLength = 0
      }
    }
    return true
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor)
