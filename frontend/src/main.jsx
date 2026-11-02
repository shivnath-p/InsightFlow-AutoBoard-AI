import React, { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, getToken, setToken } from './api';
import DashboardView from './DashboardView';
import Login from './Login';
import RunView from './RunView';
import './style.css';

function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const [user, setUser] = useState(null);
  const [datasets, setDatasets] = useState([]);
  const [dashboards, setDashboards] = useState([]);
  const [selected, setSelected] = useState(null);
  const [question, setQuestion] = useState('');
  const [run, setRun] = useState(null);
  const [openDash, setOpenDash] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const guard = useCallback(async (fn) => {
    setError('');
    setBusy(true);
    try {
      return await fn();
    } catch (e) {
      if (e.status === 401) setAuthed(false);
      setError(e.message);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);

  const load = useCallback(() => guard(async () => {
    setUser(await api.me());
    setDatasets(await api.datasets());
    setDashboards(await api.dashboards());
  }), [guard]);

  useEffect(() => { if (authed) load(); }, [authed, load]);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  const logout = () => { setToken(null); setAuthed(false); setSelected(null); setRun(null); };

  const upload = (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) guard(async () => { await api.upload(file); await load(); });
  };
  const remove = (d) => {
    if (!window.confirm(`Delete "${d.name}" with its dashboards and runs?`)) return;
    guard(async () => {
      await api.deleteDataset(d.id);
      if (selected?.id === d.id) { setSelected(null); setRun(null); }
      await load();
    });
  };
  const rename = (d) => {
    const name = window.prompt('New dataset name', d.name);
    if (name) guard(async () => { await api.renameDataset(d.id, name); await load(); });
  };
  const ask = (q = question) => {
    if (!selected || !q.trim()) return;
    guard(async () => setRun((await api.ask(selected.id, q)).run));
  };
  const makeDashboard = (r) => guard(async () => {
    const res = await api.dashboardFromRun(r.id);
    await load();
    setOpenDash(res.id);
  });
  const autoDashboard = () => guard(async () => { const res = await api.autoDashboard(selected.id); await load(); setOpenDash(res.id); });
  const dashboardsHere = dashboards.filter((d) => d.dataset_id === selected?.id);

  return (
    <div className="app">
      <header>
        <h1>InsightFlow AutoBoard AI</h1>
        <span>{user?.email} <button className="secondary" onClick={logout}>Sign out</button></span>
      </header>
      <main>
        <aside>
          <h3>Datasets</h3>
          <input type="file" accept=".csv,.xlsx" onChange={upload} disabled={busy} />
          {datasets.map((d) => (
            <div className={'item ' + (selected?.id === d.id ? 'active' : '')} key={d.id}>
              <button onClick={() => { setSelected(d); setRun(null); setOpenDash(null); }}>{d.name}</button>
              <button className="icon" title="Rename" onClick={() => rename(d)}>Edit</button>
              <button className="icon" title="Delete" onClick={() => remove(d)}>Del</button>
            </div>
          ))}
        </aside>
        <section>
          {error && <div className="error" role="alert">{error}</div>}
          {busy && <div className="muted">Working...</div>}
          {!selected ? <div className="empty">Upload or select a dataset to start.</div> : (
            <>
              <h2>{selected.name} <small className="muted">{selected.row_count.toLocaleString()} rows</small></h2>
              <div className="card">
                <div className="toolbar"><h3>Ask AI</h3><button onClick={autoDashboard} disabled={busy}>✨ Generate Auto Dashboard</button></div>
                <textarea value={question} maxLength={1000} onChange={(e) => setQuestion(e.target.value)}
                  placeholder="e.g. Which product has the highest total sales?" />
                <button onClick={() => ask()} disabled={busy}>Run analysis</button>
                <RunView run={run} onPickOption={(o) => { const q = `total ${o}`; setQuestion(q); ask(q); }}
                  onExport={(r, f) => guard(() => api.exportRun(r.id, f))} onDashboard={makeDashboard} />
              </div>
              {openDash && <DashboardView id={openDash} onClose={() => setOpenDash(null)} onError={setError} />}
              <div className="card">
                <h3>Dashboards</h3>
                {dashboardsHere.length === 0 && <span className="muted">None yet. Run an analysis and click "Create dashboard".</span>}
                {dashboardsHere.map((d) => (
                  <div className="dash" key={d.id}>
                    <span>{d.name} <small className="muted">v{d.version}</small></span>
                    <span>
                      <button className="secondary" onClick={() => setOpenDash(d.id)}>Open</button>{' '}
                      <button className="secondary" onClick={() => {
                        if (window.confirm('Delete dashboard?')) guard(async () => { await api.deleteDashboard(d.id); setOpenDash(null); await load(); });
                      }}>Delete</button>
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
