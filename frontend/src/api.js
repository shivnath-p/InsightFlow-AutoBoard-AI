// Small API client: attaches the JWT, turns HTTP errors into readable messages.
const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const TOKEN_KEY = 'insightflow_token';

// sessionStorage (not localStorage): the token disappears when the tab closes. It is readable by
// scripts on this origin, so the strict Content-Security-Policy in nginx.conf.template matters.
export const getToken = () => {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
};
export const setToken = (t) => {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage blocked: the user simply has to sign in again */ }
};

const DEFAULT_TIMEOUT_MS = 30_000;
const SLOW_TIMEOUT_MS = 120_000; // uploads, analysis and exports

// FastAPI sends validation errors as a list of {loc, msg}: turn it into one readable line.
function describe(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((x) => x !== 'body').join('.')}: ${d.msg}`).join('; ');
  }
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, form, raw, slow = false } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), slow ? SLOW_TIMEOUT_MS : DEFAULT_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(API + path, {
      method,
      headers,
      signal: controller.signal,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
    });
  } catch (e) {
    if (e.name === 'AbortError') throw new ApiError(0, 'The request took too long. Please try again.');
    throw new ApiError(0, 'Cannot reach the server. Is the backend running?');
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = describe(data.detail);
    } catch { /* keep statusText */ }
    if (res.status === 401) setToken(null);
    throw new ApiError(res.status, detail);
  }
  return raw ? res : res.json();
}

async function download(path, filename) {
  const res = await request(path, { raw: true, slow: true });
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  // revoking straight away can cancel the download in some browsers
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export const api = {
  register: (email, password) => request('/auth/register', { method: 'POST', body: { email, password } }),
  login: (email, password) => request('/auth/login', { method: 'POST', body: { email, password } }),
  me: () => request('/auth/me'),
  datasets: () => request('/datasets'),
  upload: (file) => {
    const form = new FormData();
    form.append('file', file);
    return request('/datasets/upload', { method: 'POST', form, slow: true });
  },
  dataset: (id) => request(`/datasets/${id}`),
  renameDataset: (id, name) => request(`/datasets/${id}`, { method: 'PUT', body: { name } }),
  deleteDataset: (id) => request(`/datasets/${id}`, { method: 'DELETE' }),
  ask: (dataset_id, question) =>
    request('/ask', { method: 'POST', body: { dataset_id, question }, slow: true }),
  dashboards: () => request('/dashboards'),
  autoDashboard: (datasetId) =>
    request(`/dashboards/auto/${datasetId}`, { method: 'POST', slow: true }),
  updateDashboard: (id, payload) => request(`/dashboards/${id}`, { method: 'PUT', body: payload }),
  dashboard: (id) => request(`/dashboards/${id}`),
  dashboardFromRun: (runId, name) =>
    request(`/dashboards/from-run/${runId}`, { method: 'POST', body: { name } }),
  versions: (id) => request(`/dashboards/${id}/versions`),
  restore: (id, version) => request(`/dashboards/${id}/restore`, { method: 'POST', body: { version } }),
  deleteDashboard: (id) => request(`/dashboards/${id}`, { method: 'DELETE' }),
  exportRun: (runId, fmt) => download(`/runs/${runId}/export/${fmt}`, `insightflow-${runId.slice(0, 8)}.${fmt}`),
  exportDashboard: (id, fmt) => download(`/dashboards/${id}/export/${fmt}`, `insightflow-dashboard-${id.slice(0, 8)}.${fmt}`),
};
