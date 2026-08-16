'use client'

import { useCallback, useRef, useState } from 'react'

export type AssistantState =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'working'
  | 'speaking'
  | 'error'

export type Turn = { id: number; input: string; output: string }

const PLAYBACK_LEAD_SECONDS = 0.06

type LiveMessage = {
  type: string
  state?: AssistantState
  message?: string
  role?: 'user' | 'assistant'
  text?: string
  input?: string
  output?: string
}

export function useLiveAssistant() {
  const [state, setState] = useState<AssistantState>('idle')
  const [error, setError] = useState('')
  const [partial, setPartial] = useState({ user: '', assistant: '' })
  const [turns, setTurns] = useState<Turn[]>([])
  const socketRef = useRef<WebSocket | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const sourcesRef = useRef(new Set<AudioBufferSourceNode>())
  const nextPlaybackRef = useRef(0)

  const clearPlayback = useCallback(() => {
    for (const source of sourcesRef.current) {
      try {
        source.stop()
      } catch {
        // Already stopped.
      }
    }
    sourcesRef.current.clear()
    nextPlaybackRef.current = contextRef.current?.currentTime ?? 0
  }, [])

  const playPcm = useCallback((buffer: ArrayBuffer) => {
    const context = contextRef.current
    if (!context || buffer.byteLength < 2) return
    const pcm = new Int16Array(buffer)
    const audio = context.createBuffer(1, pcm.length, 24_000)
    const channel = audio.getChannelData(0)
    for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768
    const source = context.createBufferSource()
    source.buffer = audio
    source.connect(context.destination)
    const startAt = Math.max(context.currentTime + PLAYBACK_LEAD_SECONDS, nextPlaybackRef.current)
    source.start(startAt)
    nextPlaybackRef.current = startAt + audio.duration
    sourcesRef.current.add(source)
    source.onended = () => sourcesRef.current.delete(source)
  }, [])

  const disconnect = useCallback(() => {
    socketRef.current?.close(1000, 'user ended session')
    socketRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    clearPlayback()
    void contextRef.current?.close()
    contextRef.current = null
    setState('idle')
    setPartial({ user: '', assistant: '' })
  }, [clearPlayback])

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
        await context.resume()

        const socket = new WebSocket(new URL(endpoint))
        socket.binaryType = 'arraybuffer'
        socketRef.current = socket
        let captureStarted = false

        socket.onopen = () => {
          socket.send(JSON.stringify({ type: 'auth', token: token.trim() }))
        }
        const startCapture = () => {
          if (captureStarted) return
          captureStarted = true
          const source = context.createMediaStreamSource(stream)
          const capture = new AudioWorkletNode(context, 'pcm-capture-processor')
          const silence = context.createGain()
          silence.gain.value = 0
          source.connect(capture).connect(silence).connect(context.destination)
          capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
            if (socket.readyState === WebSocket.OPEN) socket.send(event.data)
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
          if (message.type === 'authenticated') startCapture()
          if (message.type === 'state' && message.state) setState(message.state)
          if (message.type === 'clear') clearPlayback()
          if (message.type === 'error') {
            setError(message.message ?? 'The live session failed.')
            setState('error')
          }
          if (message.type === 'transcript' && message.role) {
            setPartial((current) => ({ ...current, [message.role!]: message.text ?? '' }))
          }
          if (message.type === 'turn') {
            setTurns((current) => [
              ...current.slice(-5),
              { id: Date.now(), input: message.input ?? '', output: message.output ?? '' },
            ])
            setPartial({ user: '', assistant: '' })
          }
        }
        socket.onerror = () => {
          setError('Could not reach the ASTRA gateway.')
          setState('error')
        }
        socket.onclose = () => {
          stream.getTracks().forEach((track) => track.stop())
          if (socketRef.current === socket) {
            socketRef.current = null
            clearPlayback()
            void context.close()
            contextRef.current = null
            streamRef.current = null
            setState((current) => (current === 'error' ? current : 'idle'))
          }
        }
      } catch (cause) {
        disconnect()
        setError(cause instanceof Error ? cause.message : 'Unable to start a voice session.')
        setState('error')
      }
    },
    [clearPlayback, disconnect, playPcm],
  )

  const sendText = useCallback((text: string) => {
    const socket = socketRef.current
    if (socket?.readyState !== WebSocket.OPEN || !text.trim()) return false
    socket.send(JSON.stringify({ type: 'text', text: text.trim() }))
    return true
  }, [])

  return { state, error, partial, turns, connect, disconnect, sendText }
}
