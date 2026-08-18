const GEMINI_SAMPLE_RATE = 24000
const INITIAL_PREBUFFER_SECONDS = 0.18
const MAX_PREBUFFER_SECONDS = 0.42
const BUFFER_STEP_SECONDS = 0.04
const STABLE_RECOVERY_SECONDS = 15
const RAMP_SAMPLES = 64

class PcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.chunks = []
    this.chunkIndex = 0
    this.chunkOffset = 0
    this.queuedSamples = 0
    this.playing = false
    this.ending = false
    this.rampRemaining = 0
    this.lastSample = 0
    this.underruns = 0
    this.targetBufferSamples = Math.round(sampleRate * INITIAL_PREBUFFER_SECONDS)
    this.lastUnderrunFrame = 0
    this.lastMetricsFrame = 0
    this.port.onmessage = ({ data }) => {
      if (data.type === 'clear') {
        this.reset()
      } else if (data.type === 'end-turn') {
        this.ending = true
      } else if (data.type === 'audio' && data.buffer instanceof ArrayBuffer) {
        this.enqueue(data.buffer)
      }
    }
  }

  reset() {
    this.chunks = []
    this.chunkIndex = 0
    this.chunkOffset = 0
    this.queuedSamples = 0
    this.playing = false
    this.ending = false
    this.rampRemaining = 0
    this.lastSample = 0
    this.underruns = 0
    this.targetBufferSamples = Math.round(sampleRate * INITIAL_PREBUFFER_SECONDS)
    this.lastUnderrunFrame = 0
    this.publishMetrics(true)
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
    this.ending = false
  }

  nextSample() {
    const chunk = this.chunks[this.chunkIndex]
    if (!chunk) return null
    const value = chunk[this.chunkOffset]
    this.chunkOffset += 1
    this.queuedSamples -= 1
    if (this.chunkOffset >= chunk.length) {
      this.chunkIndex += 1
      this.chunkOffset = 0
      if (this.chunkIndex >= 32 && this.chunkIndex * 2 >= this.chunks.length) {
        this.chunks = this.chunks.slice(this.chunkIndex)
        this.chunkIndex = 0
      }
    }
    return value
  }

  publishMetrics(force = false) {
    if (!force && currentFrame - this.lastMetricsFrame < sampleRate) return
    this.lastMetricsFrame = currentFrame
    if (
      this.targetBufferSamples > Math.round(sampleRate * INITIAL_PREBUFFER_SECONDS) &&
      currentFrame - this.lastUnderrunFrame > sampleRate * STABLE_RECOVERY_SECONDS
    ) {
      this.targetBufferSamples = Math.max(
        Math.round(sampleRate * INITIAL_PREBUFFER_SECONDS),
        this.targetBufferSamples - Math.round(sampleRate * BUFFER_STEP_SECONDS),
      )
      this.lastUnderrunFrame = currentFrame
    }
    this.port.postMessage({
      type: 'metrics',
      queuedMs: Math.round((this.queuedSamples * 1000) / sampleRate),
      targetMs: Math.round((this.targetBufferSamples * 1000) / sampleRate),
      underruns: this.underruns,
    })
  }

  process(_inputs, outputs) {
    const channel = outputs[0]?.[0]
    if (!channel) return true
    channel.fill(0)
    this.publishMetrics()

    if (!this.playing) {
      if (
        this.queuedSamples < this.targetBufferSamples &&
        !(this.ending && this.queuedSamples > 0)
      ) {
        return true
      }
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
        if (!this.ending) {
          this.underruns += 1
          this.lastUnderrunFrame = currentFrame
          this.targetBufferSamples = Math.min(
            Math.round(sampleRate * MAX_PREBUFFER_SECONDS),
            this.targetBufferSamples + Math.round(sampleRate * BUFFER_STEP_SECONDS),
          )
          this.publishMetrics(true)
        }
        this.ending = false
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
