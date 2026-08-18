'use client'

import { FormEvent, useMemo, useState } from 'react'
import {
  ArrowUp,
  Bell,
  Bot,
  CircleAlert,
  ExternalLink,
  LoaderCircle,
  Pause,
  Plus,
  Power,
  RefreshCw,
  Terminal,
  X,
} from 'lucide-react'

import { AgentProvider, AgentState, useAgentWorkspace } from '@/hooks/use-agent-workspace'
import { cn } from '@/lib/utils'

type AgentWorkspaceProps = {
  endpoint: string
  token: string
  onOpenSettings: () => void
}

const stateLabel: Record<AgentState, string> = {
  running: 'Working',
  needs_input: 'Needs you',
  error: 'Blocked',
}

function shortPath(path: string) {
  const parts = path.split('/').filter(Boolean)
  return parts.slice(-2).join('/') || path
}

export default function AgentWorkspace({ endpoint, token, onOpenSettings }: AgentWorkspaceProps) {
  const workspace = useAgentWorkspace(endpoint, token)
  const [provider, setProvider] = useState<AgentProvider>('claude')
  const [projectPath, setProjectPath] = useState('')
  const [initialPrompt, setInitialPrompt] = useState('')
  const [message, setMessage] = useState('')
  const [mode, setMode] = useState<'prompt' | 'steer'>('prompt')
  const [launchOpen, setLaunchOpen] = useState(false)
  const [closingId, setClosingId] = useState<string | null>(null)
  const [shutdownArmed, setShutdownArmed] = useState(false)
  const [notificationState, setNotificationState] = useState<NotificationPermission | 'unsupported'>(
    'default',
  )

  const availableProviders = workspace.capabilities?.providers
  const resolvedProjectPath = projectPath || workspace.capabilities?.default_project || ''
  const selected = workspace.selected
  const promptPlaceholder = useMemo(
    () =>
      mode === 'steer'
        ? 'Change the approach, constraints, or next step…'
        : 'Give this instance its next task…',
    [mode],
  )

  const submitLaunch = async (event: FormEvent) => {
    event.preventDefault()
    try {
      await workspace.launch(provider, resolvedProjectPath, initialPrompt)
      setInitialPrompt('')
      setLaunchOpen(false)
    } catch {
      // The hook exposes the actionable error in the workspace.
    }
  }

  const submitMessage = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected || !message.trim()) return
    if (await workspace.send(selected.id, message.trim(), mode)) setMessage('')
  }

  const enableNotifications = async () => {
    setNotificationState(await workspace.requestNotifications())
  }

  if (!token.trim()) {
    return (
      <section className="agents-gate">
        <div className="gate-index">01</div>
        <div>
          <p className="eyebrow">Local authorization required</p>
          <h2>Connect ASTRA before opening agent control.</h2>
          <p>
            Add the gateway session token once. It stays in this browser tab and never enters a
            Claude or Codex prompt.
          </p>
          <button className="text-action" type="button" onClick={onOpenSettings}>
            Open connection settings <ArrowUp size={15} />
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="agents-workspace" aria-label="Coding agent workspace">
      <div className="agents-commandbar">
        <div>
          <p className="eyebrow">Visible local sessions</p>
          <h2>Agent workspace</h2>
        </div>
        <div className="commandbar-actions">
          {notificationState !== 'granted' && notificationState !== 'unsupported' && (
            <button className="quiet-button" type="button" onClick={enableNotifications}>
              <Bell size={15} /> Notify me
            </button>
          )}
          <button
            className="quiet-button"
            type="button"
            onClick={() => void workspace.refresh()}
            disabled={workspace.loading}
          >
            <RefreshCw className={cn(workspace.loading && 'is-spinning')} size={15} /> Refresh
          </button>
          {shutdownArmed ? (
            <button
              className="quiet-button is-danger"
              type="button"
              onBlur={() => setShutdownArmed(false)}
              onClick={() => {
                void workspace.shutdown()
                setShutdownArmed(false)
              }}
            >
              <Power size={15} /> Confirm shutdown
            </button>
          ) : (
            <button
              className="quiet-button"
              type="button"
              disabled={!workspace.capabilities?.kitty.connected}
              onClick={() => setShutdownArmed(true)}
            >
              <Power size={15} /> Shut down
            </button>
          )}
          <button className="launch-button" type="button" onClick={() => setLaunchOpen(true)}>
            <Plus size={17} /> New instance
          </button>
        </div>
      </div>

      {launchOpen && (
        <form className="launch-drawer" onSubmit={submitLaunch}>
          <div className="drawer-heading">
            <div>
              <span>New Kitty tab</span>
              <strong>Launch a coding partner</strong>
            </div>
            <button type="button" aria-label="Close launch form" onClick={() => setLaunchOpen(false)}>
              <X size={18} />
            </button>
          </div>
          <div className="provider-switch" aria-label="Agent provider">
            {(['claude', 'codex'] as const).map((name) => (
              <button
                key={name}
                className={cn(provider === name && 'is-selected')}
                type="button"
                disabled={availableProviders ? !availableProviders[name].available : false}
                onClick={() => setProvider(name)}
              >
                {name === 'claude' ? <Bot size={16} /> : <Terminal size={16} />}
                {name === 'claude' ? 'Claude Code' : 'Codex'}
              </button>
            ))}
          </div>
          <label className="field-label">
            Project directory
            <input
              value={resolvedProjectPath}
              onChange={(event) => setProjectPath(event.target.value)}
              spellCheck={false}
              autoCapitalize="none"
              required
            />
          </label>
          <label className="field-label launch-prompt">
            First task <span>optional</span>
            <textarea
              value={initialPrompt}
              onChange={(event) => setInitialPrompt(event.target.value)}
              placeholder="Describe the outcome. The agent opens visibly and starts here."
              maxLength={20_000}
            />
          </label>
          <div className="launch-notes">
            <span>Workspace-write sandbox</span>
            <span>Approvals stay in Kitty</span>
            <span>MCP inherited locally</span>
          </div>
          <button className="drawer-submit" type="submit" disabled={workspace.loading}>
            {workspace.loading ? <LoaderCircle className="is-spinning" size={17} /> : <ArrowUp size={17} />}
            Launch {provider === 'claude' ? 'Claude Code' : 'Codex'}
          </button>
        </form>
      )}

      {workspace.error && (
        <div className="workspace-error" role="alert">
          <CircleAlert size={17} />
          <p>{workspace.error}</p>
        </div>
      )}

      <div className="agents-grid">
        <aside className="instance-rail" aria-label="Open instances">
          <div className="rail-heading">
            <span>Open</span>
            <b>{workspace.instances.length.toString().padStart(2, '0')}</b>
          </div>
          {workspace.instances.length ? (
            <div className="instance-list">
              {workspace.instances.map((instance, index) => (
                <button
                  key={instance.id}
                  className={cn('instance-row', workspace.selectedId === instance.id && 'is-selected')}
                  type="button"
                  onClick={() => workspace.setSelectedId(instance.id)}
                >
                  <span className="instance-index">{String(index + 1).padStart(2, '0')}</span>
                  <span className="instance-copy">
                    <strong>{instance.provider === 'claude' ? 'Claude Code' : 'Codex'}</strong>
                    <small>{shortPath(instance.project_path)}</small>
                  </span>
                  <span className={cn('agent-state-dot', `is-${instance.state}`)} />
                </button>
              ))}
            </div>
          ) : (
            <div className="rail-empty">
              <span>No tabs yet.</span>
              <p>Launch one here or ask ASTRA by voice.</p>
            </div>
          )}
          <div className="rail-foot">
            <span className={cn('connection-pip', workspace.capabilities?.kitty.connected && 'is-online')} />
            Kitty {workspace.capabilities?.kitty.connected ? 'connected' : 'standby'}
          </div>
        </aside>

        <div className="instance-stage">
          {selected ? (
            <>
              <header className="instance-header">
                <div>
                  <div className="instance-kicker">
                    <span className={cn('agent-state-dot', `is-${selected.state}`)} />
                    {stateLabel[selected.state]}
                    <i>#{selected.id.slice(0, 7)}</i>
                  </div>
                  <h3>{selected.title}</h3>
                  <p title={selected.project_path}>{selected.project_path}</p>
                </div>
                <div className="instance-actions">
                  <button type="button" onClick={() => void workspace.focus(selected.id)}>
                    <ExternalLink size={15} /> Focus tab
                  </button>
                  <button type="button" onClick={() => void workspace.interrupt(selected.id)}>
                    <Pause size={15} /> Pause
                  </button>
                  {closingId === selected.id ? (
                    <button
                      className="danger-button"
                      type="button"
                      onBlur={() => setClosingId(null)}
                      onClick={() => {
                        void workspace.close(selected.id)
                        setClosingId(null)
                      }}
                    >
                      Confirm close
                    </button>
                  ) : (
                    <button type="button" onClick={() => setClosingId(selected.id)}>
                      <X size={15} /> Close
                    </button>
                  )}
                </div>
              </header>

              {selected.diagnosis && (
                <div className="diagnosis-panel">
                  <span>{selected.diagnosis.code}</span>
                  <div>
                    <strong>{selected.diagnosis.summary}</strong>
                    <p>{selected.diagnosis.action}</p>
                  </div>
                </div>
              )}

              <div className="terminal-frame">
                <div className="terminal-chrome">
                  <span>Current terminal</span>
                  <span>kitty / {selected.provider}</span>
                </div>
                <pre>{selected.terminal.trim() || 'Agent terminal is starting…'}</pre>
              </div>

              <form className="agent-composer" onSubmit={submitMessage}>
                <div className="composer-mode" aria-label="Message mode">
                  <button
                    className={cn(mode === 'prompt' && 'is-selected')}
                    type="button"
                    onClick={() => setMode('prompt')}
                  >
                    Prompt
                  </button>
                  <button
                    className={cn(mode === 'steer' && 'is-selected')}
                    type="button"
                    onClick={() => setMode('steer')}
                  >
                    Steer
                  </button>
                </div>
                <label className="sr-only" htmlFor="agent-message">
                  Message this coding agent
                </label>
                <textarea
                  id="agent-message"
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder={promptPlaceholder}
                  maxLength={20_000}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                      event.currentTarget.form?.requestSubmit()
                    }
                  }}
                />
                <button type="submit" aria-label={`Send ${mode}`} disabled={!message.trim()}>
                  <ArrowUp size={18} />
                </button>
              </form>
            </>
          ) : (
            <div className="stage-empty">
              <span className="empty-crosshair" />
              <p className="eyebrow">Nothing selected</p>
              <h3>One task. One visible tab.</h3>
              <p>
                Launch Claude Code or Codex, then watch its real terminal, redirect the work, or
                jump into Kitty whenever an approval needs you.
              </p>
              <button type="button" onClick={() => setLaunchOpen(true)}>
                <Plus size={16} /> Launch first instance
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
