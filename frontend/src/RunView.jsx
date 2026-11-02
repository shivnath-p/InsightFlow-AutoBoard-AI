import React from 'react';
import Chart, { FactTable, Kpi } from './Chart';

// Shows one agent run: findings with evidence ids, verified facts, warnings, clarification.
export default function RunView({ run, onPickOption, onExport, onDashboard }) {
  if (!run) return null;
  return (
    <div className="run">
      <p><span className={`badge ${run.status}`}>{run.status}</span> <span className="muted">run {run.id.slice(0, 8)}</span></p>
      {run.status === 'needs_input' && (
        <div className="notice">
          <p>{run.clarification}</p>
          {(run.options || []).map((o) => (
            <button key={o} className="secondary" onClick={() => onPickOption(o)}>{o}</button>
          ))}
        </div>
      )}
      {run.claims.length > 0 && (
        <ul className="claims">
          {run.claims.map((c, i) => (
            <li key={i}>{c.text} <small className="muted">[{c.fact_ids.join(', ')}]</small></li>
          ))}
        </ul>
      )}
      {run.facts.map((f) => (
        <div className="fact" key={f.fact_id}>
          <h4>{f.fact_id}: {f.description}</h4>
          <small className="muted">
            {f.source_rows.toLocaleString()} rows, verified by recomputation, query {f.query_hash}
          </small>
          {f.chart === 'kpi' ? <Kpi fact={f} /> : <Chart type={f.chart} fact={f} />}
          <details><summary>Show table</summary><FactTable fact={f} /></details>
        </div>
      ))}
      {run.warnings.length > 0 && (
        <div className="notice warn">{run.warnings.map((w, i) => <div key={i}>{w}</div>)}</div>
      )}
      {run.facts.length > 0 && (
        <div className="toolbar">
          <button onClick={() => onDashboard(run)}>Create dashboard</button>
          {['pdf', 'pptx', 'xlsx', 'csv'].map((f) => (
            <button key={f} className="secondary" onClick={() => onExport(run, f)}>Export {f.toUpperCase()}</button>
          ))}
        </div>
      )}
      <details><summary>Agent trace</summary>
        <ol>{run.trace.map((t) => <li key={t.step}>{t.name} <small className="muted">{t.detail}</small></li>)}</ol>
      </details>
    </div>
  );
}
