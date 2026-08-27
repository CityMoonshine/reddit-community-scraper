// Thin fetch wrapper. Same-origin: nginx serves this bundle at / and proxies
// /api to the api container, so the httpOnly session cookie rides along on its
// own and there is no token for client JS to hold or leak.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
    ...options,
  });

  let payload = null;
  const type = response.headers.get('content-type') || '';

  if (type.includes('application/json')) {
    payload = await response.json().catch(() => null);
  }

  if (!response.ok) {
    // FastAPI puts validation errors in `detail` as an array; flatten those to
    // something a human can read rather than dumping [object Object].
    let detail = payload && payload.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
    }
    throw new ApiError(response.status, detail);
  }

  return payload;
}

const body = (data) => ({ method: 'POST', body: JSON.stringify(data) });

export const api = {
  me: () => request('/me'),
  login: (username, password) => request('/login', body({ username, password })),
  logout: () => request('/logout', { method: 'POST' }),

  posts: (params) => request(`/posts?${new URLSearchParams(params)}`),

  communities: () => request('/communities'),
  addCommunity: (data) => request('/communities', body(data)),
  toggleCommunity: (community_id) => request('/communities/toggle', body({ community_id })),
  removeCommunity: (community_id) => request('/communities/remove', body({ community_id })),

  status: () => request('/status'),
  runs: () => request('/runs'),
  runDetail: (id) => request(`/runs/${id}`),
  queueRun: (backend, community_id = null) => request('/runs', body({ backend, community_id })),
  discoveries: () => request('/discoveries'),

  scoring: () => request('/scoring'),
  saveRubric: (data) => request('/scoring', body(data)),
  activateRubric: (prompt_id) => request('/scoring/activate', body({ prompt_id })),
  runScoring: () => request('/scoring/run', { method: 'POST' }),

  debugSessions: () => request('/debug/sessions'),
  debugSession: (id) => request(`/debug/sessions/${id}`),
};
