// The SPA. Hash routing, no build step, no framework - the whole UI is tables
// and forms, and a toolchain would cost more than it returns here.

import { api } from '/api.js';

const root = document.getElementById('root');

const state = {
  route: 'feed',
  routeArg: null,
  banner: null,
  status: null,
  // Run ids the operator has expanded to see per-community detail.
  openRuns: new Set(),
  runDetail: {},
  // Feed filters survive navigation away and back.
  feed: { community: '', flair: '', sort: 'score', page: 1 },
};

// ---------------------------------------------------------------- helpers

const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);

  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'html') el.innerHTML = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2).toLowerCase(), value);
    else el.setAttribute(key, value);
  }

  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  return el;
};

const num = (value) => (value ?? 0).toLocaleString();

// Timestamps arrive as ISO-ish strings from both sqlite (space separator) and
// python isoformat (T separator, +00:00). Normalise before display.
const when = (value) => {
  if (!value) return '—';
  return String(value).replace('T', ' ').replace(/(\+00:00|Z)$/, '').slice(0, 16);
};

const ago = (value) => {
  if (!value) return 'never';
  const raw = String(value).includes('T') ? value : `${value}Z`.replace(' ', 'T');
  const delta = (Date.now() - new Date(raw).getTime()) / 1000;
  if (Number.isNaN(delta)) return when(value);
  if (delta < 90) return 'just now';
  if (delta < 5400) return `${Math.round(delta / 60)}m ago`;
  if (delta < 172800) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
};

const pill = (text, kind) => h('span', { class: `pill ${kind || ''}` }, text);

const banner = () => {
  if (!state.banner) return null;
  const { kind, text } = state.banner;
  return h('div', { class: `banner ${kind}` }, text);
};

function setBanner(kind, text) {
  state.banner = { kind, text };
  render();
  if (kind === 'notice') {
    setTimeout(() => {
      if (state.banner && state.banner.text === text) {
        state.banner = null;
        render();
      }
    }, 6000);
  }
}

async function guard(fn) {
  try {
    await fn();
  } catch (error) {
    setBanner('error', error.message);
  }
}

// ------------------------------------------------------------------ shell

function shell(...content) {
  const tab = (id, label) => h('a', {
    href: `#${id}`,
    class: state.route === id ? 'active' : '',
  }, label);

  return h('div', {},
    h('header', {},
      h('h1', {}, 'Community Insights'),
      h('nav', {}, tab('feed', 'Feed'), tab('monitor', 'Monitoring'), tab('debug', 'Detection')),
      statusChip(),
    ),
    h('main', {}, statusStrip(), alertList(), banner(), ...content),
  );
}

// ------------------------------------------------------------------ status

// state.status is refreshed by a single poller (see startStatusPolling) rather
// than per-view, so every page shows the same live picture and a slow sweep
// doesn't leave one tab stale.
function statusChip() {
  const s = state.status;

  if (!s) return h('span', { class: 'chip' }, 'connecting…');

  const w = s.worker;
  const kind = !w.online ? 'bad' : (w.state === 'sweeping' ? 'busy' : 'good');
  const label = !w.online ? 'worker offline'
    : (w.state === 'sweeping' ? 'sweeping' : 'worker idle');

  return h('span', { class: `chip ${kind}`, title: w.message || '' },
    h('span', { class: 'dot' }), label);
}

function statusStrip() {
  const s = state.status;
  if (!s) return null;

  const w = s.worker;
  const t = s.totals;

  const cell = (label, value, extra) =>
    h('div', { class: 'stat' },
      h('div', { class: 'stat-label' }, label),
      h('div', { class: 'stat-value' }, value),
      extra ? h('div', { class: 'muted' }, extra) : null);

  const activeRun = s.active_run;
  const lastRun = s.last_run;

  let nowDoing;
  if (!w.online) {
    nowDoing = 'not running';
  } else if (activeRun && activeRun.status === 'running') {
    nowDoing = w.current_community ? `r/${w.current_community}` : 'sweeping';
  } else if (activeRun) {
    nowDoing = 'queued, starting shortly';
  } else {
    nowDoing = 'idle';
  }

  return h('div', { class: 'status-strip' },
    cell('Worker', w.online ? (w.state || 'idle') : 'OFFLINE',
      w.online && w.seconds_since_heartbeat !== null
        ? `beat ${w.seconds_since_heartbeat}s ago` : w.message),
    cell('Doing now', nowDoing, w.online ? (w.message || '') : ''),
    cell('Next sweep', w.next_sweep_at ? when(w.next_sweep_at) : '—',
      `every ${s.config.sweep_interval_minutes} min · ${s.config.default_backend}`),
    cell('Last sweep', lastRun ? `#${lastRun.id} ${lastRun.status}` : 'never',
      lastRun ? `${lastRun.posts_new} new · ${ago(lastRun.finished_at)}` : ''),
    cell('Posts', num(t.posts), `${num(t.new_24h)} in last 24h`),
    cell('Communities', `${t.monitored}/${t.communities}`, 'monitored / total'),
  );
}

function alertList() {
  const s = state.status;
  if (!s || !s.alerts.length) return null;

  return h('div', { class: 'alerts' },
    s.alerts.map((a) => h('div', { class: `banner ${a.level === 'error' ? 'error' : 'warn'}` },
      h('strong', {}, a.level === 'error' ? 'Problem: ' : 'Warning: '), a.text)),
  );
}

const loading = () => h('div', { class: 'card empty' }, 'Loading…');

// ------------------------------------------------------------------- feed

function feedView() {
  const container = h('div', {});
  const mount = h('div', {}, loading());

  container.append(h('h2', {}, 'Community feed'), mount);

  guard(async () => {
    const { community, flair, sort, page } = state.feed;
    const [data, communityData] = await Promise.all([
      api.posts({ community, flair, sort, page, per_page: 25 }),
      api.communities(),
    ]);

    const setFilter = (key, value) => {
      state.feed[key] = value;
      if (key !== 'page') state.feed.page = 1;
      render();
    };

    const filters = h('form', { class: 'filters' },
      h('select', { onchange: (e) => setFilter('community', e.target.value) },
        h('option', { value: '' }, 'All communities'),
        communityData.communities.map((c) => h('option', {
          value: c.name, selected: c.name === community,
        }, c.display_name || `r/${c.name}`)),
      ),
      h('select', { onchange: (e) => setFilter('flair', e.target.value) },
        h('option', { value: '' }, 'All flairs'),
        data.flairs.map((f) => h('option', { value: f, selected: f === flair }, f)),
      ),
      h('select', { onchange: (e) => setFilter('sort', e.target.value) },
        [['score', 'Top score'], ['comments', 'Most comments'],
         ['new', 'Newest posted'], ['discovered', 'Recently discovered']]
          .map(([v, label]) => h('option', { value: v, selected: v === sort }, label)),
      ),
      h('span', { class: 'muted' }, `${num(data.total)} posts`),
    );

    const rows = data.posts.map((p) => h('tr', {},
      h('td', { class: 'title-cell' },
        h('a', { href: p.permalink, target: '_blank', rel: 'noopener noreferrer' }, p.title),
        p.over18 ? pill('NSFW', 'failed') : null,
        h('div', { class: 'muted' }, p.domain || ''),
      ),
      h('td', { class: 'nowrap' }, p.community_display || `r/${p.community_name}`),
      h('td', { class: 'nowrap' }, `u/${p.author}`),
      h('td', {}, p.flair ? pill(p.flair, 'flair') : null),
      h('td', { class: 'num' }, num(p.score)),
      h('td', { class: 'num' }, p.upvote_ratio ? `${Math.round(p.upvote_ratio * 100)}%` : '—'),
      h('td', { class: 'num' }, num(p.num_comments)),
      h('td', { class: 'muted nowrap' }, when(p.created_utc)),
    ));

    const table = h('table', {},
      h('thead', {}, h('tr', {},
        ...['Post', 'Community', 'Author', 'Flair', 'Score', 'Ratio', 'Comments', 'Posted']
          .map((label, i) => h('th', { class: i >= 4 && i <= 6 ? 'num' : '' }, label)),
      )),
      h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', {
        colspan: '8', class: 'empty',
      }, 'No posts match those filters.'))),
    );

    const pager = h('div', { class: 'pager' },
      h('button', {
        class: 'ghost', disabled: page <= 1,
        onclick: () => setFilter('page', page - 1),
      }, '← Previous'),
      h('span', { class: 'muted' }, `Page ${page} of ${data.pages}`),
      h('button', {
        class: 'ghost', disabled: page >= data.pages,
        onclick: () => setFilter('page', page + 1),
      }, 'Next →'),
    );

    mount.replaceChildren(filters, h('div', { class: 'card' }, table), pager);
  });

  return container;
}

// -------------------------------------------------------------- monitoring

function monitorView() {
  const container = h('div', {});
  const mount = h('div', {}, loading());
  container.append(mount);

  guard(async () => {
    const [communityData, runData, discoveryData] = await Promise.all([
      api.communities(), api.runs(), api.discoveries(),
    ]);

    const addForm = h('form', {
      class: 'add-form',
      onsubmit: (event) => {
        event.preventDefault();
        const data = new FormData(event.target);
        guard(async () => {
          const result = await api.addCommunity({
            name: data.get('name'),
            monitor_sort: data.get('monitor_sort'),
            monitor_limit: Number(data.get('monitor_limit')),
          });
          setBanner('notice', result.detail);
        });
      },
    },
      h('div', {}, h('label', {}, 'Subreddit'),
        h('input', { name: 'name', placeholder: 'python — or r/python, or a full URL', required: 'required', autocomplete: 'off' })),
      h('div', {}, h('label', {}, 'Watch'),
        h('select', { name: 'monitor_sort' },
          ['new', 'hot', 'top', 'rising'].map((s) => h('option', { value: s }, s)))),
      h('div', {}, h('label', {}, 'Posts / sweep'),
        h('input', { name: 'monitor_limit', type: 'number', value: '50', min: '10', max: '500' })),
      h('button', { class: 'primary', type: 'submit' }, 'Add to monitoring'),
    );

    const active = runData.active;

    const toolbar = h('div', { class: 'toolbar' },
      h('button', {
        class: 'primary', disabled: !!active,
        onclick: () => guard(async () => {
          const result = await api.queueRun(runData.default_backend);
          setBanner('notice', result.detail);
        }),
      }, active ? `Sweep ${active.status}…` : 'Sweep all now'),
      h('span', { class: 'muted' },
        `Scheduled every ${runData.interval_minutes} minutes via the worker container.`
        + ` Backend: ${runData.default_backend}.`),
    );

    const communityRows = communityData.communities.map((c) => h('tr', {},
      h('td', {},
        h('strong', {}, c.display_name || `r/${c.name}`),
        c.active_users
          ? h('div', { class: 'muted' }, `${num(c.active_users)} weekly active`)
          : (!c.post_count ? h('div', { class: 'muted' }, 'not fetched yet') : null),
      ),
      h('td', {}, pill(c.monitor_enabled ? 'monitoring' : 'paused', c.monitor_enabled ? 'ok' : 'off')),
      h('td', { class: 'nowrap' }, `${c.monitor_sort || 'new'} · ${c.monitor_limit || 50}`),
      h('td', { class: 'num' }, num(c.post_count)),
      h('td', { class: 'num' }, num(c.new_24h)),
      h('td', {},
        c.last_status
          ? h('span', {},
              pill(c.last_status, c.last_status === 'ok' ? 'ok'
                : (c.last_status === 'blocked' ? 'failed' : 'partial')),
              c.last_error ? h('div', { class: 'muted wrap' }, c.last_error) : null)
          : h('span', { class: 'muted' }, 'never swept')),
      h('td', { class: 'muted nowrap' }, ago(c.last_checked_at)),
      h('td', { class: 'nowrap' },
        h('button', {
          class: 'ghost', disabled: !!active,
          title: 'Sweep just this community now — runs even if paused',
          onclick: () => guard(async () => {
            const result = await api.queueRun(runData.default_backend, c.id);
            setBanner('notice', result.detail);
          }),
        }, 'Sweep'),
        h('button', {
          class: 'ghost',
          onclick: () => guard(async () => { await api.toggleCommunity(c.id); render(); }),
        }, c.monitor_enabled ? 'Pause' : 'Resume'),
        h('button', {
          class: 'ghost danger',
          onclick: () => guard(async () => { await api.removeCommunity(c.id); render(); }),
        }, 'Remove'),
      ),
    ));

    const communityTable = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, 'Community'), h('th', {}, 'Status'), h('th', {}, 'Watching'),
        h('th', { class: 'num' }, 'Posts'), h('th', { class: 'num' }, 'New 24h'),
        h('th', {}, 'Last result'), h('th', {}, 'Last checked'), h('th', {}, ''),
      )),
      h('tbody', {}, communityRows.length ? communityRows : h('tr', {}, h('td', {
        colspan: '8', class: 'empty',
      }, 'Nothing monitored yet. Add a subreddit above.'))),
    );

    const runRows = runData.runs.flatMap((r) => {
      const open = state.openRuns.has(r.id);

      const toggle = () => guard(async () => {
        if (open) {
          state.openRuns.delete(r.id);
        } else {
          state.openRuns.add(r.id);
          // Fetched on demand: the list endpoint stays cheap, and most runs
          // are never expanded.
          state.runDetail[r.id] = await api.runDetail(r.id);
        }
        render();
      });

      const row = h('tr', { class: 'clickable', onclick: toggle },
        h('td', {}, h('span', { class: 'caret' }, open ? '▾' : '▸'), ` #${r.id}`),
      h('td', { class: 'muted nowrap' }, when(r.started_at || r.queued_at)),
      h('td', {}, r.trigger),
      h('td', { class: 'nowrap' },
        r.only_community_name ? `r/${r.only_community_name}` : 'all'),
      h('td', {}, r.backend),
      h('td', {}, pill(r.status, r.status)),
      h('td', { class: 'num' }, r.communities_checked),
      h('td', { class: 'num' }, r.posts_new),
      h('td', { class: 'num' }, r.posts_refreshed),
        h('td', { class: 'muted' },
          r.error ? r.error.slice(0, 70)
            : (r.failed_items ? `${r.failed_items} issue(s) — click` : '')),
      );

      if (!open) return [row];

      const detail = state.runDetail[r.id];

      const body = !detail
        ? h('td', { colspan: '10', class: 'empty' }, 'Loading…')
        : h('td', { colspan: '10', class: 'detail-cell' },
            r.error ? h('div', { class: 'banner error' },
              h('strong', {}, 'Run error: '), r.error) : null,
            detail.items.length
              ? h('table', { class: 'inner' },
                  h('thead', {}, h('tr', {},
                    h('th', {}, 'Community'), h('th', {}, 'Result'),
                    h('th', { class: 'num' }, 'New'), h('th', { class: 'num' }, 'Refreshed'),
                    h('th', {}, 'Detail'))),
                  h('tbody', {}, detail.items.map((it) => h('tr', {},
                    h('td', { class: 'nowrap' }, `r/${it.community_name}`),
                    h('td', {}, pill(it.status, it.status === 'ok' ? 'ok'
                      : (it.status === 'blocked' ? 'failed' : 'partial'))),
                    h('td', { class: 'num' }, num(it.posts_new)),
                    h('td', { class: 'num' }, num(it.posts_refreshed)),
                    h('td', { class: 'muted wrap' }, it.error || ''),
                  ))))
              : h('div', { class: 'muted' },
                  'No per-community records — the run failed before it reached any.'),
          );

      return [row, h('tr', { class: 'detail-row' }, body)];
    });

    const runTable = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '#'), h('th', {}, 'Started'), h('th', {}, 'Trigger'),
        h('th', {}, 'Scope'), h('th', {}, 'Backend'), h('th', {}, 'Status'),
        h('th', { class: 'num' }, 'Checked'), h('th', { class: 'num' }, 'New'),
        h('th', { class: 'num' }, 'Refreshed'), h('th', {}, 'Notes'),
      )),
      h('tbody', {}, runRows.length ? runRows : h('tr', {}, h('td', {
        colspan: '10', class: 'empty',
      }, 'No sweeps yet.'))),
    );

    const discoveryRows = discoveryData.discoveries.map((p) => h('tr', {},
      h('td', { class: 'title-cell' },
        h('a', { href: p.permalink, target: '_blank', rel: 'noopener noreferrer' }, p.title),
        h('div', { class: 'muted' }, p.domain || ''),
      ),
      h('td', { class: 'nowrap' }, p.community_display || `r/${p.community_name}`),
      h('td', { class: 'nowrap' }, `u/${p.author}`),
      h('td', {}, p.flair ? pill(p.flair, 'flair') : null),
      h('td', { class: 'num' }, num(p.score)),
      h('td', { class: 'num' }, num(p.num_comments)),
      h('td', { class: 'muted nowrap' }, when(p.created_utc)),
      h('td', { class: 'muted nowrap' }, ago(p.first_seen_at)),
    ));

    const discoveryTable = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, 'Post'), h('th', {}, 'Community'), h('th', {}, 'Author'),
        h('th', {}, 'Flair'), h('th', { class: 'num' }, 'Score'),
        h('th', { class: 'num' }, 'Comments'), h('th', {}, 'Posted'), h('th', {}, 'First seen'),
      )),
      h('tbody', {}, discoveryRows.length ? discoveryRows : h('tr', {}, h('td', {
        colspan: '8', class: 'empty',
      }, 'Nothing discovered yet.'))),
    );

    mount.replaceChildren(
      h('h2', {}, 'Add a community'),
      h('div', { class: 'card' }, addForm),
      h('h2', {}, 'Monitored communities'),
      toolbar,
      h('div', { class: 'card' }, communityTable),
      h('h2', {}, 'Recent sweeps'),
      h('div', { class: 'card' }, runTable),
      h('h2', {}, 'Latest discoveries'),
      h('div', { class: 'card' }, discoveryTable),
    );

    // A queued or running sweep resolves on its own; poll so the operator
    // doesn't have to guess when to refresh.
    if (active && state.route === 'monitor') {
      setTimeout(() => { if (state.route === 'monitor') render(); }, 8000);
    }
  });

  return container;
}

// -------------------------------------------------------------- detection

function debugView() {
  const container = h('div', {});
  const mount = h('div', {}, loading());
  container.append(h('h2', {}, 'Detection'), mount);

  if (state.routeArg) {
    guard(async () => {
      const data = await api.debugSession(state.routeArg);
      const s = data.session;

      const rows = data.timeline.map((entry) => h('tr', {},
        h('td', { class: 'muted nowrap' }, when(entry.at)),
        h('td', {}, pill(entry.kind, entry.kind === 'signal' ? 'partial' : 'off')),
        h('td', {}, entry.summary),
        h('td', { class: 'muted wrap' }, entry.detail),
        h('td', { class: 'num' }, entry.weight ?? ''),
        h('td', { class: 'num' }, entry.running_score),
      ));

      mount.replaceChildren(
        h('a', { href: '#debug', class: 'backlink' }, '← All sessions'),
        h('div', { class: 'card meta' },
          h('div', {}, h('strong', {}, `Session #${s.id}`),
            h('div', { class: 'muted' }, s.username ? `u/${s.username}` : 'anonymous')),
          h('div', {}, h('strong', {}, 'IP'), h('div', { class: 'muted' }, s.ip_address || '—')),
          h('div', {}, h('strong', {}, 'Score'), h('div', { class: 'muted' }, data.total_score)),
          h('div', {}, h('strong', {}, 'Verdict'), h('div', { class: 'muted' }, s.verdict)),
          h('div', { class: 'wide' }, h('strong', {}, 'User agent'),
            h('div', { class: 'muted wrap' }, s.user_agent || '—')),
        ),
        h('div', { class: 'card' }, h('table', {},
          h('thead', {}, h('tr', {},
            h('th', {}, 'At'), h('th', {}, 'Kind'), h('th', {}, 'Summary'),
            h('th', {}, 'Detail'), h('th', { class: 'num' }, 'Weight'),
            h('th', { class: 'num' }, 'Running'),
          )),
          h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', {
            colspan: '6', class: 'empty',
          }, 'No events recorded for this session.'))),
        )),
      );
    });

    return container;
  }

  guard(async () => {
    const data = await api.debugSessions();

    const rows = data.sessions.map((s) => h('tr', {},
      h('td', {}, h('a', { href: `#debug/${s.id}` }, `#${s.id}`)),
      h('td', {}, s.username ? `u/${s.username}` : h('span', { class: 'muted' }, 'anonymous')),
      h('td', { class: 'muted nowrap' }, s.ip_address || '—'),
      h('td', { class: 'muted wrap ua' }, (s.user_agent || '—').slice(0, 90)),
      h('td', { class: 'num' }, s.request_count),
      h('td', { class: 'num' }, s.signal_count),
      h('td', { class: 'num' }, s.fingerprint_count),
      h('td', { class: 'num' }, s.bot_score),
      h('td', {}, pill(s.verdict, s.verdict === 'unscored' ? 'off' : 'partial')),
      h('td', { class: 'muted nowrap' }, ago(s.created_at)),
    ));

    mount.replaceChildren(
      h('p', { class: 'muted' },
        'Unauthenticated by design — this is a lab target. These views are '
        + 'excluded from RequestLog, so inspecting the log does not write to it.'),
      h('div', { class: 'card' }, h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, 'Session'), h('th', {}, 'User'), h('th', {}, 'IP'),
          h('th', {}, 'User agent'), h('th', { class: 'num' }, 'Reqs'),
          h('th', { class: 'num' }, 'Signals'), h('th', { class: 'num' }, 'FPs'),
          h('th', { class: 'num' }, 'Score'), h('th', {}, 'Verdict'), h('th', {}, 'Started'),
        )),
        h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', {
          colspan: '10', class: 'empty',
        }, 'No sessions yet.'))),
      )),
    );
  });

  return container;
}

// ----------------------------------------------------------------- polling

// One poller for the whole app. Fast while a sweep is live so progress is
// visible as it happens, slow when idle so an open tab isn't chatty.
let statusTimer = null;

async function pollStatus() {
  try {
    const next = await api.status();
    const wasActive = !!(state.status && state.status.active_run);
    const isActive = !!next.active_run;
    state.status = next;

    // A sweep finishing changes the tables underneath us, so re-render the
    // whole view rather than just the strip.
    if (wasActive !== isActive || isActive) {
      render();
    } else {
      refreshStatusChrome();
    }
  } catch (error) {
    state.status = null;
    refreshStatusChrome();
  }

  const active = state.status && state.status.active_run;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(pollStatus, active ? 3000 : 15000);
}

// Repaint just the strip/chip/alerts, so polling doesn't nuke a filter
// dropdown the operator is mid-way through using.
function refreshStatusChrome() {
  const chip = document.querySelector('header .chip');
  if (chip) chip.replaceWith(statusChip());

  const strip = document.querySelector('.status-strip');
  const freshStrip = statusStrip();
  if (strip && freshStrip) strip.replaceWith(freshStrip);

  const alerts = document.querySelector('.alerts');
  const freshAlerts = alertList();
  if (alerts && freshAlerts) alerts.replaceWith(freshAlerts);
  else if (alerts && !freshAlerts) alerts.remove();
}

// ------------------------------------------------------------------ router

const VIEWS = { feed: feedView, monitor: monitorView, debug: debugView };

function readHash() {
  const [route, arg] = (location.hash.slice(1) || 'feed').split('/');
  state.route = VIEWS[route] ? route : 'feed';
  state.routeArg = arg || null;
}

function render() {
  root.replaceChildren(shell(VIEWS[state.route]()));
}

window.addEventListener('hashchange', () => {
  readHash();
  state.banner = null;
  render();
});

(function boot() {
  readHash();
  render();
  pollStatus();
})();
