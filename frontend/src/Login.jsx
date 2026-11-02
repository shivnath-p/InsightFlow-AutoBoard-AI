import React, { useState } from 'react';
import { api, setToken } from './api';

export default function Login({ onDone }) {
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = mode === 'login' ? await api.login(email, password) : await api.register(email, password);
      setToken(res.access_token);
      onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <h2>InsightFlow AutoBoard AI</h2>
        <p className="muted">{mode === 'login' ? 'Sign in to your workspace' : 'Create your workspace'}</p>
        <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" /></label>
        <label>Password (min 10 characters)
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={10}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
        </label>
        {error && <div className="error" role="alert">{error}</div>}
        <button disabled={busy}>{busy ? 'Please wait...' : mode === 'login' ? 'Sign in' : 'Register'}</button>
        <p className="muted">
          {mode === 'login' ? 'New here? ' : 'Already registered? '}
          <a href="#" onClick={(e) => { e.preventDefault(); setMode(mode === 'login' ? 'register' : 'login'); }}>
            {mode === 'login' ? 'Create an account' : 'Sign in'}
          </a>
        </p>
      </form>
    </div>
  );
}
