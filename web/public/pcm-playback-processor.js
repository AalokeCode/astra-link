const GEMINI_SAMPLE_RATE = 24000
const PREBUFFER_SECONDS = 0.1
const RAMP_SAMPLES = 64

class PcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.chunks = []
    this.chunkOffset = 0
    this.queuedSamples = 0
    this.playing = false
    this.rampRemaining = 0
    this.lastSample = 0
    this.prebufferSamples = Math.round(sampleRate * PREBUFFER_SECONDS)
    this.port.onmessage = ({ data }) => {
      if (data.type === 'clear') {
        this.reset()
      } else if (data.type === 'audio' && data.buffer instanceof ArrayBuffer) {
        this.enqueue(data.buffer)
      }
    }
  }

  reset() {
    this.chunks = []
    this.chunkOffset = 0
    this.queuedSamples = 0
    this.playing = false
    this.rampRemaining = 0
    this.lastSample = 0
  }

  enqueue(buffer) {
    const pcm = new Int16Array(buffer)
    if (pcm.length === 0) return
    const outputLength = Math.max(1, Math.round((pcm.length * sampleRate) / GEMINI_SAMPLE_RATE))
    const output = new Float32Array(outputLength)
    const ratio = GEMINI_SAMPLE_RATE / sampleRate
    const last = pcm.length - 1
    for (let index = 0; index < outputLength; index += 1) {
      const position = index * ratio
      const left = Math.min(Math.floor(position), last)
      const right = Math.min(left + 1, last)
      const fraction = position - left
      output[index] = (pcm[left] + (pcm[right] - pcm[left]) * fraction) / 32768
    }
    this.chunks.push(output)
    this.queuedSamples += output.length
  }

  nextSample() {
    const chunk = this.chunks[0]
    if (!chunk) return null
    const value = chunk[this.chunkOffset]
    this.chunkOffset += 1
    this.queuedSamples -= 1
    if (this.chunkOffset >= chunk.length) {
      this.chunks.shift()
      this.chunkOffset = 0
    }
    return value
  }

  process(_inputs, outputs) {
    const channel = outputs[0]?.[0]
    if (!channel) return true
    channel.fill(0)

    if (!this.playing) {
      if (this.queuedSamples < this.prebufferSamples) return true
      this.playing = true
      this.rampRemaining = RAMP_SAMPLES
    }

    for (let index = 0; index < channel.length; index += 1) {
      const sample = this.nextSample()
      if (sample === null) {
        const remaining = Math.min(RAMP_SAMPLES, channel.length - index)
        for (let fade = 0; fade < remaining; fade += 1) {
          channel[index + fade] = this.lastSample * (1 - (fade + 1) / remaining)
        }
        this.playing = false
        this.rampRemaining = 0
        this.lastSample = 0
        break
      }
      const gain = this.rampRemaining > 0 ? 1 - this.rampRemaining / RAMP_SAMPLES : 1
      channel[index] = sample * gain
      this.rampRemaining = Math.max(0, this.rampRemaining - 1)
      this.lastSample = channel[index]
    }
    return true
  }
}

registerProcessor('pcm-playback-processor', PcmPlaybackProcessor)
