// The SPA. Hash routing, no build step, no framework - the whole UI is tables
// and forms, and a toolchain would cost more than it returns here.

import { api } from '/api.js';

const root = document.getElementById('root');

const state = {
  route: 'feed',
  routeArg: null,
  banner: null,
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
    ),
    h('main', {}, banner(), ...content),
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
        h('th', {}, 'Last checked'), h('th', {}, ''),
      )),
      h('tbody', {}, communityRows.length ? communityRows : h('tr', {}, h('td', {
        colspan: '7', class: 'empty',
      }, 'Nothing monitored yet. Add a subreddit above.'))),
    );

    const runRows = runData.runs.map((r) => h('tr', {},
      h('td', {}, `#${r.id}`),
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
        r.error ? r.error.slice(0, 70) : (r.failed_items ? `${r.failed_items} issue(s)` : '')),
    ));

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
})();
