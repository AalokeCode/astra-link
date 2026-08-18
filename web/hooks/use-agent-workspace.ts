'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

export type AgentProvider = 'claude' | 'codex'
export type AgentState = 'running' | 'needs_input' | 'error'

export type AgentDiagnosis = {
  code: string
  summary: string
  action: string
}

export type AgentInstance = {
  id: string
  provider: AgentProvider
  project_path: string
  window_id: number
  tab_id?: number
  title: string
  state: AgentState
  terminal: string
  diagnosis: AgentDiagnosis | null
  focused: boolean
}

export type AgentCapabilities = {
  kitty: { available: boolean; connected: boolean }
  providers: Record<AgentProvider, { available: boolean }>
  default_project: string
  mcp: string
}

type StatusPayload = {
  capabilities: AgentCapabilities
  instances: AgentInstance[]
}

type ApiErrorPayload = {
  error?: { code?: string; message?: string; action?: string; detail?: string }
}

function gatewayApiBase(endpoint: string): string {
  const url = new URL(endpoint)
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:'
  url.search = ''
  url.hash = ''
  url.pathname = url.pathname.replace(/\/(?:browser\/media|v1\/live)\/?$/, '')
  return url.toString().replace(/\/$/, '')
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as ApiErrorPayload
    const issue = payload.error
    if (issue?.message) {
      return [issue.message, issue.action, issue.detail].filter(Boolean).join(' ')
    }
  } catch {
    // Fall through to the HTTP status when the gateway returned non-JSON.
  }
  return `ASTRA gateway returned ${response.status} ${response.statusText}.`
}

export function useAgentWorkspace(endpoint: string, token: string) {
  const [capabilities, setCapabilities] = useState<AgentCapabilities | null>(null)
  const [instances, setInstances] = useState<AgentInstance[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const previousStates = useRef(new Map<string, AgentState>())
  const initialized = useRef(false)

  const request = useCallback(
    async <T,>(path: string, init?: RequestInit): Promise<T> => {
      if (!token.trim()) throw new Error('Enter the ASTRA session token in Connection settings.')
      let base: string
      try {
        base = gatewayApiBase(endpoint)
      } catch {
        throw new Error('The gateway WebSocket URL is invalid.')
      }
      const response = await fetch(`${base}${path}`, {
        ...init,
        headers: {
          Authorization: `Bearer ${token.trim()}`,
          ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
          ...init?.headers,
        },
      })
      if (!response.ok) throw new Error(await readError(response))
      return (await response.json()) as T
    },
    [endpoint, token],
  )

  const notifyTransitions = useCallback((next: AgentInstance[]) => {
    for (const instance of next) {
      const prior = previousStates.current.get(instance.id)
      if (
        initialized.current &&
        prior !== instance.state &&
        ['needs_input', 'error'].includes(instance.state) &&
        typeof Notification !== 'undefined' &&
        Notification.permission === 'granted'
      ) {
        const body =
          instance.state === 'error'
            ? (instance.diagnosis?.summary ?? 'The agent reported an error.')
            : 'The agent is waiting for your input or approval.'
        new Notification(`${instance.title} needs you`, { body, tag: instance.id })
      }
      previousStates.current.set(instance.id, instance.state)
    }
    initialized.current = true
  }, [])

  const refresh = useCallback(
    async (quiet = false) => {
      if (!token.trim()) {
        setCapabilities(null)
        setInstances([])
        return
      }
      if (!quiet) setLoading(true)
      try {
        const payload = await request<StatusPayload>('/agents/status')
        setCapabilities(payload.capabilities)
        setInstances(payload.instances)
        notifyTransitions(payload.instances)
        setSelectedId((current) => {
          if (current && payload.instances.some((instance) => instance.id === current)) {
            return current
          }
          return payload.instances.at(0)?.id ?? null
        })
        setError('')
      } catch (cause) {
        if (!quiet) setError(cause instanceof Error ? cause.message : 'Could not load agents.')
      } finally {
        if (!quiet) setLoading(false)
      }
    },
    [notifyTransitions, request, token],
  )

  useEffect(() => {
    initialized.current = false
    previousStates.current.clear()
    const initial = window.setTimeout(() => void refresh(), 0)
    if (!token.trim()) return () => window.clearTimeout(initial)
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh(true)
    }, 3_000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [refresh, token])

  const launch = useCallback(
    async (provider: AgentProvider, projectPath: string, prompt: string) => {
      setLoading(true)
      setError('')
      try {
        const payload = await request<{ instance: AgentInstance }>('/agents/instances', {
          method: 'POST',
          body: JSON.stringify({
            provider,
            project_path: projectPath,
            prompt: prompt.trim() || undefined,
          }),
        })
        setSelectedId(payload.instance.id)
        await refresh(true)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Could not launch the agent.')
        throw cause
      } finally {
        setLoading(false)
      }
    },
    [refresh, request],
  )

  const act = useCallback(
    async (instanceId: string, action: 'focus' | 'interrupt') => {
      setError('')
      try {
        await request(`/agents/instances/${encodeURIComponent(instanceId)}/${action}`, {
          method: 'POST',
        })
        await refresh(true)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : `Could not ${action} the agent.`)
      }
    },
    [refresh, request],
  )

  const send = useCallback(
    async (instanceId: string, message: string, mode: 'prompt' | 'steer') => {
      setError('')
      try {
        await request(`/agents/instances/${encodeURIComponent(instanceId)}/prompt`, {
          method: 'POST',
          body: JSON.stringify({ message, mode }),
        })
        await refresh(true)
        return true
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Could not send the prompt.')
        return false
      }
    },
    [refresh, request],
  )

  const close = useCallback(
    async (instanceId: string) => {
      setError('')
      try {
        await request(`/agents/instances/${encodeURIComponent(instanceId)}`, {
          method: 'DELETE',
        })
        await refresh(true)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Could not close the agent.')
      }
    },
    [refresh, request],
  )

  const shutdown = useCallback(async () => {
    setError('')
    try {
      await request<{ shutdown: boolean; closed_instances: number }>('/agents/shutdown', {
        method: 'POST',
      })
      setInstances([])
      setSelectedId(null)
      previousStates.current.clear()
      window.setTimeout(() => void refresh(true), 350)
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not shut down the workspace.')
      return false
    }
  }, [refresh, request])

  const requestNotifications = useCallback(async () => {
    if (typeof Notification === 'undefined') return 'unsupported' as const
    return Notification.requestPermission()
  }, [])

  return {
    capabilities,
    instances,
    selectedId,
    selected: instances.find((instance) => instance.id === selectedId) ?? null,
    loading,
    error,
    setSelectedId,
    refresh,
    launch,
    focus: (id: string) => act(id, 'focus'),
    interrupt: (id: string) => act(id, 'interrupt'),
    send,
    close,
    shutdown,
    requestNotifications,
  }
}
