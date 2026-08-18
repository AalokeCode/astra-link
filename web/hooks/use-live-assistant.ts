'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

export type AssistantState =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'working'
  | 'speaking'
  | 'error'

export type Turn = { id: number; input: string; output: string }

export type TransportQuality = 'good' | 'fair' | 'poor'

export type TransportMetrics = {
  quality: TransportQuality
  rttMs: number | null
  bufferMs: number
  targetBufferMs: number
  underruns: number
  droppedInputFrames: number
}

type LiveMessage = {
  type: string
  state?: AssistantState
  message?: string
  role?: 'user' | 'assistant'
  text?: string
  input?: string
  output?: string
  id?: number
  sentAt?: number
}

const MAX_SOCKET_BACKLOG_BYTES = 64 * 1024
const PING_INTERVAL_MS = 5_000

const initialMetrics: TransportMetrics = {
  quality: 'good',
  rttMs: null,
  bufferMs: 0,
  targetBufferMs: 180,
  underruns: 0,
  droppedInputFrames: 0,
}

export function useLiveAssistant() {
  const [state, setState] = useState<AssistantState>('idle')
  const [error, setError] = useState('')
  const [partial, setPartial] = useState({ user: '', assistant: '' })
  const [turns, setTurns] = useState<Turn[]>([])
  const socketRef = useRef<WebSocket | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const captureRef = useRef<AudioWorkletNode | null>(null)
  const playbackRef = useRef<AudioWorkletNode | null>(null)
  const pingTimerRef = useRef<number | null>(null)
  const wakeLockRef = useRef<WakeLockSentinel | null>(null)
  const [transport, setTransport] = useState<TransportMetrics>(initialMetrics)

  const clearPlayback = useCallback(() => {
    playbackRef.current?.port.postMessage({ type: 'clear' })
  }, [])

  const playPcm = useCallback((buffer: ArrayBuffer) => {
    if (buffer.byteLength < 2 || !playbackRef.current) return
    playbackRef.current.port.postMessage({ type: 'audio', buffer }, [buffer])
  }, [])

  const releaseWakeLock = useCallback(() => {
    const lock = wakeLockRef.current
    wakeLockRef.current = null
    if (lock) void lock.release().catch(() => undefined)
  }, [])

  const acquireWakeLock = useCallback(async () => {
    if (!('wakeLock' in navigator) || document.visibilityState !== 'visible') return
    try {
      wakeLockRef.current = await navigator.wakeLock.request('screen')
    } catch {
      // Voice still works without a wake lock; Android may simply pause it in the background.
    }
  }, [])

  const disconnect = useCallback(() => {
    socketRef.current?.close(1000, 'user ended session')
    socketRef.current = null
    if (pingTimerRef.current !== null) window.clearInterval(pingTimerRef.current)
    pingTimerRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    captureRef.current?.disconnect()
    captureRef.current = null
    clearPlayback()
    playbackRef.current?.disconnect()
    playbackRef.current = null
    void contextRef.current?.close()
    contextRef.current = null
    releaseWakeLock()
    setState('idle')
    setPartial({ user: '', assistant: '' })
    setTransport(initialMetrics)
  }, [clearPlayback, releaseWakeLock])

  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === 'visible' && socketRef.current?.readyState === WebSocket.OPEN) {
        void acquireWakeLock()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [acquireWakeLock])

  const connect = useCallback(
    async (endpoint: string, token: string) => {
      setError('')
      setState('connecting')
      try {
        if (!token.trim()) throw new Error('Enter the gateway token first.')
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        })
        streamRef.current = stream
        const context = new AudioContext({ latencyHint: 'interactive' })
        contextRef.current = context
        await context.audioWorklet.addModule('/pcm-capture-processor.js')
        await context.audioWorklet.addModule('/pcm-playback-processor.js')
        await context.resume()
        const playback = new AudioWorkletNode(context, 'pcm-playback-processor', {
          numberOfInputs: 0,
          numberOfOutputs: 1,
          outputChannelCount: [1],
        })
        playback.connect(context.destination)
        playbackRef.current = playback
        let droppedInputFrames = 0
        let lastReportedDroppedFrames = 0
        let lastUnderruns = 0
        playback.port.onmessage = (
          event: MessageEvent<{
            type?: string
            queuedMs?: number
            targetMs?: number
            underruns?: number
          }>,
        ) => {
          if (event.data.type !== 'metrics') return
          const underruns = event.data.underruns ?? 0
          const newUnderrun = underruns > lastUnderruns
          const dropped = droppedInputFrames > lastReportedDroppedFrames
          lastUnderruns = underruns
          lastReportedDroppedFrames = droppedInputFrames
          setTransport((current) => ({
            ...current,
            quality:
              newUnderrun || dropped || (current.rttMs ?? 0) > 450
                ? 'poor'
                : (current.rttMs ?? 0) > 220 || (event.data.targetMs ?? 180) > 220
                  ? 'fair'
                  : 'good',
            bufferMs: event.data.queuedMs ?? 0,
            targetBufferMs: event.data.targetMs ?? 180,
            underruns,
            droppedInputFrames,
          }))
        }

        const socket = new WebSocket(new URL(endpoint))
        socket.binaryType = 'arraybuffer'
        socketRef.current = socket
        let captureStarted = false
        let reportedError = false

        socket.onopen = () => {
          socket.send(JSON.stringify({ type: 'auth', token: token.trim() }))
        }
        const startCapture = () => {
          if (captureStarted) return
          captureStarted = true
          const source = context.createMediaStreamSource(stream)
          const capture = new AudioWorkletNode(context, 'pcm-capture-processor')
          captureRef.current = capture
          const silence = context.createGain()
          silence.gain.value = 0
          source.connect(capture).connect(silence).connect(context.destination)
          capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
            if (socket.readyState !== WebSocket.OPEN) return
            if (socket.bufferedAmount > MAX_SOCKET_BACKLOG_BYTES) {
              droppedInputFrames += 1
              return
            }
            socket.send(event.data)
          }
        }
        socket.onmessage = (event) => {
          if (event.data instanceof ArrayBuffer) {
            playPcm(event.data)
            return
          }
          let message: LiveMessage
          try {
            message = JSON.parse(String(event.data)) as LiveMessage
          } catch {
            setError('The gateway sent an invalid response.')
            setState('error')
            return
          }
          if (message.type === 'authenticated') {
            startCapture()
            void acquireWakeLock()
            let pingId = 0
            const ping = () => {
              if (socket.readyState !== WebSocket.OPEN) return
              pingId += 1
              socket.send(JSON.stringify({ type: 'ping', id: pingId, sentAt: performance.now() }))
            }
            ping()
            pingTimerRef.current = window.setInterval(ping, PING_INTERVAL_MS)
          }
          if (message.type === 'pong' && typeof message.sentAt === 'number') {
            const rttMs = Math.max(0, Math.round(performance.now() - message.sentAt))
            setTransport((current) => ({
              ...current,
              rttMs,
              quality:
                rttMs > 450 ? 'poor' : rttMs > 220 || current.targetBufferMs > 220 ? 'fair' : 'good',
            }))
          }
          if (message.type === 'state' && message.state) setState(message.state)
          if (message.type === 'clear') clearPlayback()
          if (message.type === 'error') {
            reportedError = true
            setError(message.message ?? 'The live session failed.')
            setState('error')
          }
          if (message.type === 'transcript' && message.role) {
            setPartial((current) => ({ ...current, [message.role!]: message.text ?? '' }))
          }
          if (message.type === 'turn') {
            playback.port.postMessage({ type: 'end-turn' })
            setTurns((current) => [
              ...current.slice(-5),
              { id: Date.now(), input: message.input ?? '', output: message.output ?? '' },
            ])
            setPartial({ user: '', assistant: '' })
          }
        }
        socket.onerror = () => {
          reportedError = true
          setError('Could not reach the ASTRA gateway.')
          setState('error')
        }
        socket.onclose = () => {
          stream.getTracks().forEach((track) => track.stop())
          if (socketRef.current === socket) {
            socketRef.current = null
            if (pingTimerRef.current !== null) window.clearInterval(pingTimerRef.current)
            pingTimerRef.current = null
            clearPlayback()
            captureRef.current?.disconnect()
            captureRef.current = null
            playback.disconnect()
            playbackRef.current = null
            void context.close()
            contextRef.current = null
            streamRef.current = null
            releaseWakeLock()
            if (!reportedError) {
              setError('The live connection closed. Check the Mac gateway and network, then reconnect.')
            }
            setState('error')
          }
        }
      } catch (cause) {
        disconnect()
        setError(cause instanceof Error ? cause.message : 'Unable to start a voice session.')
        setState('error')
      }
    },
    [acquireWakeLock, clearPlayback, disconnect, playPcm, releaseWakeLock],
  )

  const sendText = useCallback((text: string) => {
    const socket = socketRef.current
    if (socket?.readyState !== WebSocket.OPEN || !text.trim()) return false
    socket.send(JSON.stringify({ type: 'text', text: text.trim() }))
    return true
  }, [])

  return { state, error, partial, turns, transport, connect, disconnect, sendText }
}
