import React, { useState, useEffect, useCallback } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'

const API_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')

async function apiFetch(path) {
  const res = await fetch(`${API_BASE}/api/v1${path}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

function StatusBadge({ status }) {
  const colors = {
    healthy: { bg: '#d1fae5', text: '#065f46', dot: '#10b981' },
    degraded: { bg: '#fef3c7', text: '#92400e', dot: '#f59e0b' },
    circuit_open: { bg: '#fee2e2', text: '#991b1b', dot: '#ef4444' },
    operator_disabled: { bg: '#f3f4f6', text: '#6b7280', dot: '#9ca3af' },
  }
  const c = colors[status] || colors.operator_disabled
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '3px 10px', borderRadius: 999, fontSize: 12, fontWeight: 600, background: c.bg, color: c.text }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: c.dot, display: 'inline-block' }} />
      {status.replace('_', ' ').toUpperCase()}
    </span>
  )
}

function ProviderCard({ provider, isPrimary, onToggle, metricsData }) {
  const [toggling, setToggling] = useState(false)
  async function handleToggle() {
    setToggling(true)
    try { await onToggle(provider.provider_name, !provider.operator_disabled) }
    finally { setToggling(false) }
  }
  const successPct = provider.success_rate != null ? `${(provider.success_rate * 100).toFixed(1)}%` : '—'
  const chartData = (metricsData || []).map((b, i) => ({
    name: `T-${(metricsData.length - 1 - i) * 5}m`,
    rate: b.success_rate != null ? +(b.success_rate * 100).toFixed(1) : null,
  }))
  return (
    <div style={{ border: isPrimary ? '2px solid #6366f1' : '1px solid #e5e7eb', borderRadius: 12, padding: 24, background: '#fff', boxShadow: isPrimary ? '0 0 0 4px #eef2ff' : '0 1px 3px rgba(0,0,0,0.08)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, textTransform: 'capitalize' }}>{provider.provider_name}</h3>
            {isPrimary && <span style={{ background: '#6366f1', color: '#fff', fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 999 }}>PRIMARY</span>}
          </div>
          <div style={{ marginTop: 6 }}><StatusBadge status={provider.status} /></div>
        </div>
        <button onClick={handleToggle} disabled={toggling} style={{ padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: 13, background: provider.operator_disabled ? '#10b981' : '#ef4444', color: '#fff', opacity: toggling ? 0.6 : 1 }}>
          {toggling ? '...' : provider.operator_disabled ? 'Enable' : 'Disable'}
        </button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
        <Metric label="Success Rate" value={successPct} />
        <Metric label="Avg Latency" value={provider.avg_latency_ms ? `${provider.avg_latency_ms.toFixed(0)}ms` : '—'} />
        <Metric label="Consec. Fails" value={provider.consecutive_failures ?? '—'} />
      </div>
      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={100}>
          <LineChart data={chartData} margin={{ top: 0, right: 0, bottom: 0, left: -20 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="name" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} domain={[0, 100]} />
            <Tooltip formatter={v => [`${v}%`, 'Success']} />
            <Line type="monotone" dataKey="rate" stroke="#6366f1" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      )}
      {provider.recent_failures?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#6b7280', marginBottom: 4 }}>Recent Failures</div>
          {provider.recent_failures.slice(0, 3).map((f, i) => (
            <div key={i} style={{ fontSize: 11, color: '#ef4444', background: '#fef2f2', padding: '4px 8px', borderRadius: 4, marginBottom: 3 }}>
              {f.error_reason || 'Unknown'} — {new Date(f.timestamp * 1000).toLocaleTimeString()}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Metric({ label, value }) {
  return (
    <div style={{ background: '#f9fafb', borderRadius: 8, padding: '10px 12px' }}>
      <div style={{ fontSize: 11, color: '#9ca3af', marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: '#111827' }}>{value}</div>
    </div>
  )
}

function TestPanel() {
  const [prompt, setPrompt] = useState('A beautiful mountain landscape at sunset')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [polling, setPolling] = useState(false)

  async function handleSubmit() {
    setLoading(true)
    setResult({ status: 'pending', message: 'Submitting...' })
    try {
      const data = await apiPost('/generate', { prompt, width: 512, height: 512 })
      setResult({ status: 'pending', message: `Job submitted (${data.job_id?.slice(0, 8)}...). Waiting for result...` })
      setLoading(false)
      setPolling(true)
      // Poll for result
      let attempts = 0
      const interval = setInterval(async () => {
        attempts++
        try {
          const job = await apiFetch(`/jobs/${data.job_id}`)
          if (job.status === 'completed') {
            clearInterval(interval)
            setPolling(false)
            setResult({ success: true, data: job })
          } else if (job.status === 'failed') {
            clearInterval(interval)
            setPolling(false)
            setResult({ success: false, error: job.error_message })
          } else if (attempts > 60) {
            clearInterval(interval)
            setPolling(false)
            setResult({ success: false, error: 'Timed out waiting for result' })
          }
        } catch (e) {
          clearInterval(interval)
          setPolling(false)
          setResult({ success: false, error: e.message })
        }
      }, 3000)
    } catch (e) {
      setLoading(false)
      setResult({ success: false, error: e.message })
    }
  }

  return (
    <div style={{ background: '#f9fafb', borderRadius: 12, padding: 20 }}>
      <h4 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 700, color: '#374151' }}>Test Generation</h4>
      <div style={{ display: 'flex', gap: 8 }}>
        <input value={prompt} onChange={e => setPrompt(e.target.value)} style={{ flex: 1, padding: '8px 12px', borderRadius: 8, border: '1px solid #d1d5db', fontSize: 13 }} />
        <button onClick={handleSubmit} disabled={loading || polling || !prompt} style={{ padding: '8px 20px', borderRadius: 8, border: 'none', background: '#6366f1', color: '#fff', fontWeight: 600, cursor: 'pointer', fontSize: 13, opacity: (loading || polling) ? 0.7 : 1 }}>
          {loading ? 'Submitting...' : polling ? 'Generating...' : 'Generate'}
        </button>
      </div>
      {result && (
        <div style={{ marginTop: 12, padding: '10px 14px', borderRadius: 8, background: result.success ? '#d1fae5' : result.status === 'pending' ? '#eff6ff' : '#fee2e2', fontSize: 13, color: result.success ? '#065f46' : result.status === 'pending' ? '#1e40af' : '#991b1b' }}>
          {result.success
            ? `✓ Done via ${result.data.provider_used} in ${result.data.latency_ms?.toFixed(0)}ms`
            : result.status === 'pending' ? `⏳ ${result.message}` : `✗ ${result.error}`}
          {result.success && result.data.image_urls?.[0] && (
            <div style={{ marginTop: 8 }}>
              <img src={result.data.image_urls[0]} alt="Generated" style={{ maxWidth: '100%', maxHeight: 300, borderRadius: 6 }} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ActivityTable({ rows }) {
  if (!rows?.length) return <div style={{ color: '#9ca3af', fontSize: 14, padding: 12 }}>No activity yet</div>
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ borderBottom: '1px solid #e5e7eb' }}>
            {['Job ID', 'Provider', 'Status', 'Latency', 'Time'].map(h => (
              <th key={h} style={{ padding: '8px 12px', textAlign: 'left', color: '#6b7280', fontWeight: 600 }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} style={{ borderBottom: '1px solid #f3f4f6', background: i % 2 === 0 ? '#fff' : '#fafafa' }}>
              <td style={{ padding: '8px 12px', fontFamily: 'monospace', fontSize: 11, color: '#6b7280' }}>{r.job_id?.slice(0, 8)}...</td>
              <td style={{ padding: '8px 12px', fontWeight: 600, textTransform: 'capitalize' }}>{r.provider_name}</td>
              <td style={{ padding: '8px 12px' }}>
                <span style={{ color: r.success ? '#10b981' : '#ef4444', fontWeight: 600, fontSize: 12 }}>{r.success ? '✓' : '✗'}</span>
              </td>
              <td style={{ padding: '8px 12px' }}>{r.latency_ms ? `${r.latency_ms.toFixed(0)}ms` : '—'}</td>
              <td style={{ padding: '8px 12px', color: '#9ca3af' }}>{new Date(r.created_at * 1000).toLocaleTimeString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function FailoverLog({ failovers }) {
  if (!failovers?.length) return <div style={{ color: '#9ca3af', fontSize: 14 }}>No failovers recorded</div>
  return failovers.map((f, i) => (
    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 8, marginBottom: 6, background: '#fef3c7', fontSize: 13 }}>
      <span style={{ fontWeight: 700, textTransform: 'capitalize' }}>{f.from_provider}</span>
      <span style={{ color: '#f59e0b' }}>→</span>
      <span style={{ fontWeight: 700, textTransform: 'capitalize' }}>{f.to_provider}</span>
      <span style={{ color: '#92400e', marginLeft: 8, flex: 1 }}>{f.reason}</span>
      <span style={{ color: '#9ca3af', fontSize: 11 }}>{new Date(f.created_at * 1000).toLocaleTimeString()}</span>
    </div>
  ))
}

function SummaryCard({ label, value, color }) {
  return (
    <div style={{ background: '#fff', borderRadius: 12, padding: 20, border: '1px solid #e5e7eb', boxShadow: '0 1px 3px rgba(0,0,0,0.06)' }}>
      <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6, fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 800, color }}>{value}</div>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [recent, setRecent] = useState(null)
  const [metrics, setMetrics] = useState({})
  const [error, setError] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [h, r] = await Promise.all([apiFetch('/dashboard/health'), apiFetch('/dashboard/recent')])
      setHealth(h)
      setRecent(r)
      setLastRefresh(new Date())
      const names = h.providers?.map(p => p.provider_name) || []
      const results = await Promise.all(names.map(n => apiFetch(`/dashboard/metrics/${n}`).then(d => [n, d.success_rate_over_time])))
      setMetrics(Object.fromEntries(results))
      setError(null)
    } catch (e) { setError(e.message) }
  }, [])

  useEffect(() => { refresh(); const t = setInterval(refresh, 10000); return () => clearInterval(t) }, [refresh])

  async function handleToggle(name, disabled) {
    await apiPost(`/operator/providers/${name}/override`, { disabled })
    await refresh()
  }

  const summary = recent?.summary || {}

  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', background: '#f8fafc', minHeight: '100vh' }}>
      <div style={{ background: '#fff', borderBottom: '1px solid #e5e7eb', padding: '0 32px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 60 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontWeight: 900, fontSize: 22, letterSpacing: -1 }}>TRYPIX</span>
          <span style={{ color: '#9ca3af', fontSize: 14 }}>Operator Dashboard</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {health?.primary_provider && (
            <span style={{ fontSize: 13, color: '#6b7280' }}>Primary: <strong style={{ color: '#6366f1', textTransform: 'capitalize' }}>{health.primary_provider}</strong></span>
          )}
          <button onClick={refresh} style={{ padding: '6px 14px', borderRadius: 8, border: '1px solid #e5e7eb', background: '#fff', cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>↻ Refresh</button>
          {lastRefresh && <span style={{ fontSize: 11, color: '#d1d5db' }}>{lastRefresh.toLocaleTimeString()}</span>}
        </div>
      </div>

      <div style={{ maxWidth: 1200, margin: '0 auto', padding: 32 }}>
        {error && <div style={{ background: '#fee2e2', color: '#991b1b', borderRadius: 10, padding: '12px 20px', marginBottom: 24, fontSize: 14 }}>⚠ {error}</div>}

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 32 }}>
          <SummaryCard label="Total Jobs" value={summary.total_jobs ?? '—'} color="#6366f1" />
          <SummaryCard label="Success Rate" value={summary.overall_success_rate != null ? `${(summary.overall_success_rate * 100).toFixed(1)}%` : '—'} color="#10b981" />
          <SummaryCard label="Failovers" value={summary.total_failovers ?? '—'} color="#f59e0b" />
          <SummaryCard label="Active Providers" value={health?.provider_order?.length ?? '—'} color="#3b82f6" />
        </div>

        <h2 style={{ fontSize: 16, fontWeight: 700, color: '#374151', marginBottom: 16 }}>Provider Health & Controls</h2>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(440px, 1fr))', gap: 20, marginBottom: 32 }}>
          {health?.providers?.map(p => (
            <ProviderCard key={p.provider_name} provider={p} isPrimary={health.primary_provider === p.provider_name} onToggle={handleToggle} metricsData={metrics[p.provider_name]} />
          )) ?? <div style={{ color: '#9ca3af', padding: 20 }}>Loading...</div>}
        </div>

        <div style={{ marginBottom: 32 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: '#374151', marginBottom: 16 }}>Test Generation</h2>
          <TestPanel />
        </div>

        <div style={{ marginBottom: 32 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: '#374151', marginBottom: 16 }}>Failover Log</h2>
          <div style={{ background: '#fff', borderRadius: 12, padding: 20, border: '1px solid #e5e7eb' }}>
            <FailoverLog failovers={recent?.recent_failovers} />
          </div>
        </div>

        <div>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: '#374151', marginBottom: 16 }}>Recent Generations</h2>
          <div style={{ background: '#fff', borderRadius: 12, border: '1px solid #e5e7eb', overflow: 'hidden' }}>
            <ActivityTable rows={recent?.recent_generations} />
          </div>
        </div>
      </div>
    </div>
  )
}
