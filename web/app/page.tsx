'use client'

import { FormEvent, useMemo, useState } from 'react'
import { ArrowUp, Mic, Settings2, Square, X } from 'lucide-react'

import FluidOrb from '@/components/fluid-orb'
import { AssistantState, useLiveAssistant } from '@/hooks/use-live-assistant'
import { cn } from '@/lib/utils'

const stateCopy: Record<AssistantState, string> = {
  idle: 'Ready when you are',
  connecting: 'Opening a private session',
  listening: 'Listening',
  working: 'Working on it',
  speaking: 'Speaking',
  error: 'Needs attention',
}

const stateColor: Record<AssistantState, string> = {
  idle: '#6f7b89',
  connecting: '#8ea7c9',
  listening: '#1A73F2',
  working: '#6d5dfc',
  speaking: '#36a8ff',
  error: '#e86666',
}

function defaultEndpoint() {
  if (typeof window === 'undefined') return 'ws://127.0.0.1:8080/v1/live'
  if (window.location.port === '3000') return 'ws://127.0.0.1:8080/v1/live'
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/v1/live`
}

export default function Home() {
  const assistant = useLiveAssistant()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [endpoint, setEndpoint] = useState(() =>
    typeof window === 'undefined'
      ? defaultEndpoint()
      : (localStorage.getItem('astra_link_endpoint') ?? defaultEndpoint()),
  )
  const [token, setToken] = useState(() =>
    typeof window === 'undefined' ? '' : (sessionStorage.getItem('astra_link_token') ?? ''),
  )
  const [text, setText] = useState('')
  const active = !['idle', 'error'].includes(assistant.state)

  const transcript = useMemo(() => {
    const completed = assistant.turns.at(-1)
    return {
      user: assistant.partial.user || completed?.input || '',
      assistant: assistant.partial.assistant || completed?.output || '',
    }
  }, [assistant.partial, assistant.turns])

  const toggleSession = async () => {
    if (active) {
      assistant.disconnect()
      return
    }
    sessionStorage.setItem('astra_link_token', token)
    localStorage.setItem('astra_link_endpoint', endpoint)
    setSettingsOpen(false)
    await assistant.connect(endpoint, token)
  }

  const submitText = (event: FormEvent) => {
    event.preventDefault()
    if (assistant.sendText(text)) setText('')
  }

  return (
    <main className="assistant-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Personal intelligence</p>
          <h1>ASTRA</h1>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label={settingsOpen ? 'Close connection settings' : 'Open connection settings'}
          onClick={() => setSettingsOpen((open) => !open)}
        >
          {settingsOpen ? <X size={19} /> : <Settings2 size={19} />}
        </button>
      </header>

      {settingsOpen && (
        <section className="settings-panel" aria-label="Connection settings">
          <label>
            ASTRA Link WebSocket
            <input
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              spellCheck={false}
              autoCapitalize="none"
            />
          </label>
          <label>
            Session token
            <input
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              autoComplete="off"
            />
          </label>
          <p>The token stays in this browser tab. Your Gemini key never leaves the Mac.</p>
        </section>
      )}

      <section className="voice-stage" aria-live="polite">
        <div className={cn('orb-field', `is-${assistant.state}`)}>
          <div className="orb-halo" />
          <FluidOrb size={312} color={stateColor[assistant.state]} />
        </div>
        <div className="state-line">
          <span className={cn('state-dot', `is-${assistant.state}`)} />
          {stateCopy[assistant.state]}
        </div>
        <button
          className={cn('session-button', active && 'is-active')}
          type="button"
          onClick={toggleSession}
          disabled={assistant.state === 'connecting'}
        >
          {active ? <Square size={17} fill="currentColor" /> : <Mic size={19} />}
          {active ? 'End conversation' : 'Start conversation'}
        </button>
        {assistant.error && <p className="error-message">{assistant.error}</p>}
      </section>

      <section className="conversation" aria-label="Current conversation">
        {transcript.user || transcript.assistant ? (
          <>
            {transcript.user && (
              <div className="turn user-turn">
                <span>You</span>
                <p>{transcript.user}</p>
              </div>
            )}
            {transcript.assistant && (
              <div className="turn assistant-turn">
                <span>ASTRA</span>
                <p>{transcript.assistant}</p>
              </div>
            )}
          </>
        ) : (
          <p className="empty-copy">Ask naturally. Interrupt whenever you need to.</p>
        )}
      </section>

      <form className="text-composer" onSubmit={submitText}>
        <label className="sr-only" htmlFor="message">
          Message ASTRA
        </label>
        <input
          id="message"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={active ? 'Type instead…' : 'Start voice to send a message'}
          disabled={!active}
        />
        <button type="submit" aria-label="Send message" disabled={!active || !text.trim()}>
          <ArrowUp size={18} />
        </button>
      </form>

      <footer>
        <span>Gemini Live</span>
        <span>Private web link · Installable</span>
      </footer>
    </main>
  )
}
