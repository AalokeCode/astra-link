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
  const playbackRef = useRef<AudioWorkletNode | null>(null)

  const clearPlayback = useCallback(() => {
    playbackRef.current?.port.postMessage({ type: 'clear' })
  }, [])

  const playPcm = useCallback((buffer: ArrayBuffer) => {
    if (buffer.byteLength < 2 || !playbackRef.current) return
    playbackRef.current.port.postMessage({ type: 'audio', buffer }, [buffer])
  }, [])

  const disconnect = useCallback(() => {
    socketRef.current?.close(1000, 'user ended session')
    socketRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    clearPlayback()
    playbackRef.current?.disconnect()
    playbackRef.current = null
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
        await context.audioWorklet.addModule('/pcm-playback-processor.js')
        await context.resume()
        const playback = new AudioWorkletNode(context, 'pcm-playback-processor', {
          numberOfInputs: 0,
          numberOfOutputs: 1,
          outputChannelCount: [1],
        })
        playback.connect(context.destination)
        playbackRef.current = playback

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
            playback.disconnect()
            playbackRef.current = null
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
