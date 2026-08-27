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
  feed: { community: '', flair: '', sort: 'rank', min_score: 0,
          unscored: 'include', page: 1 },
  // The rubric textarea is uncontrolled while it is being typed in -
  // re-rendering it from state on every keystroke would fight the cursor.
  // This holds the draft across a re-render triggered by something else.
  rubricDraft: null,
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

// ------------------------------------------------------------------ charts
//
// Hand-rolled SVG. A charting library would be the largest dependency in a
// project that otherwise has none, to draw three shapes.
//
// Two encoding rules are load-bearing and easy to get wrong:
//
//   1. A one-hue ramp is only correct on ORDERED categories. Score buckets are
//      ordered, so they get the ramp. Days and communities are not ordered by
//      magnitude, so every bar is the same colour - shading those by value
//      would double-encode bar length as hue and spend the only free channel
//      on information the chart already shows.
//   2. Text never wears the data colour. The marks carry identity; values and
//      labels stay in ink.
//
// Colours come from CSS custom properties so the night edition swaps with the
// rest of the page rather than needing its own palette here.

const svgEl = (tag, attrs = {}, ...children) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
};

// A bar whose data-end is rounded and whose baseline end is square. Drawn as a
// path rather than a rect because rect rounds all four corners.
function barPath(x, y, w, hgt, r) {
  const radius = Math.max(0, Math.min(r, hgt, w / 2));
  if (hgt <= 0) return '';
  return `M${x},${y + hgt}`
    + `L${x},${y + radius}`
    + `Q${x},${y} ${x + radius},${y}`
    + `L${x + w - radius},${y}`
    + `Q${x + w},${y} ${x + w},${y + radius}`
    + `L${x + w},${y + hgt}Z`;
}

// One shared tooltip element, positioned on hover. One per chart would mean N
// stray absolutely-positioned nodes outliving the re-render that made them.
let tipEl = null;

function showTip(event, html) {
  if (!tipEl) {
    tipEl = h('div', { class: 'chart-tip' });
    document.body.append(tipEl);
  }
  tipEl.innerHTML = html;
  tipEl.style.display = 'block';
  const pad = 14;
  const rect = tipEl.getBoundingClientRect();
  let left = event.clientX + pad;
  if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - pad;
  tipEl.style.left = `${Math.max(8, left)}px`;
  tipEl.style.top = `${Math.max(8, event.clientY - rect.height - pad)}px`;
}

const hideTip = () => { if (tipEl) tipEl.style.display = 'none'; };

// Round a maximum up to something an axis tick can say out loud.
function niceMax(value) {
  if (value <= 5) return Math.max(1, value);
  const mag = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / (mag / 2)) * (mag / 2);
}

const shortDay = (iso) => {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' });
};

/**
 * Column chart for a single series over time.
 * `ramp` opts in to the ordinal ramp - only pass it for ordered categories.
 */
function columnChart(rows, {
  labelKey, valueKey, height = 132, ramp = false, tip, tickEvery = 2,
} = {}) {
  const values = rows.map((r) => r[valueKey] || 0);
  const top = niceMax(Math.max(1, ...values));
  const peak = Math.max(...values);

  // A real coordinate space, scaled uniformly. A 0-100 viewBox stretched to
  // width would distort the rounded data-ends into ellipses and make the bar
  // thickness a function of how wide the browser window happens to be.
  const W = 720, padB = 18, padT = 16;
  const plot = height - padB - padT;
  const band = W / Math.max(1, rows.length);
  // Cap the mark and let the leftover band be air, rather than filling the slot.
  const barW = Math.min(band * 0.6, 24);

  const svg = svgEl('svg', {
    class: 'chart', viewBox: `0 0 ${W} ${height}`,
    role: 'img', 'aria-label': `${rows.length} points, peak ${peak}`,
  });

  // Baseline only - a hairline, one step off the surface, and no gridlines
  // above it: the direct label on the peak carries the scale.
  svg.append(svgEl('line', {
    class: 'chart-axis', x1: 0, x2: W, y1: height - padB, y2: height - padB,
  }));

  rows.forEach((row, i) => {
    const v = row[valueKey] || 0;
    const hgt = (v / top) * plot;
    const x = i * band + (band - barW) / 2;
    const y = padT + (plot - hgt);

    const bar = svgEl('path', {
      d: barPath(x, y, barW, hgt, 4),
      class: ramp ? `chart-bar ramp-${Math.min(4, i)}` : 'chart-bar',
    });

    // The hit target is the whole band, not the bar - a 1-post day is a
    // sliver, and a sliver is not something anyone can hover.
    const hit = svgEl('rect', {
      x: i * band, y: 0, width: band, height, class: 'chart-hit',
      onmousemove: (e) => showTip(e, tip(row)),
      onmouseleave: hideTip,
    });

    svg.append(bar, hit);
  });

  const labels = h('div', { class: 'chart-labels' },
    rows.map((row, i) => h('span', {
      class: i % tickEvery === 0 || i === rows.length - 1 ? '' : 'hidden',
    }, row[labelKey])));

  return h('div', { class: 'chart-wrap' },
    // The peak is the one value worth a direct label; the rest live in the
    // tooltip and in the tables further down the page.
    h('div', { class: 'chart-peak' }, `peak ${num(peak)}`),
    svg, labels);
}

/** Horizontal magnitude bars. Nominal categories - one colour for every bar. */
function barRows(rows, { labelKey, valueKey, max }) {
  const top = max || Math.max(1, ...rows.map((r) => r[valueKey] || 0));

  return h('div', { class: 'bar-rows' },
    rows.map((row) => h('div', { class: 'bar-row' },
      h('span', { class: 'bar-label' }, row[labelKey]),
      h('span', { class: 'bar-track' },
        h('span', {
          class: 'bar-fill',
          style: `width: ${Math.max(1.5, ((row[valueKey] || 0) / top) * 100)}%`,
        })),
      h('span', { class: 'bar-value' }, num(row[valueKey])),
    )));
}

/** Sparkline: one series, 2px, with an end-dot carrying a surface ring. */
function sparkline(values, { width = 84, height = 22 } = {}) {
  const top = Math.max(1, ...values);
  const step = width / Math.max(1, values.length - 1);
  const y = (v) => height - 3 - (v / top) * (height - 6);

  const points = values.map((v, i) => `${i * step},${y(v)}`).join(' ');
  const lastX = (values.length - 1) * step;
  const lastY = y(values[values.length - 1] || 0);

  return svgEl('svg', {
    class: 'spark', viewBox: `0 0 ${width} ${height}`, width, height,
    role: 'img', 'aria-label': `${values.length} day trend, latest ${values[values.length - 1] || 0}`,
  },
    svgEl('polyline', { class: 'spark-line', points }),
    svgEl('circle', { class: 'spark-dot', cx: lastX, cy: lastY, r: 2.6 }),
  );
}

/** A headline number with a label under it. Not a one-bar bar chart. */
function statTile(label, value, note) {
  return h('div', { class: 'figure' },
    h('div', { class: 'figure-value' }, value),
    h('div', { class: 'figure-label' }, label),
    note ? h('div', { class: 'figure-note' }, note) : null);
}

// ------------------------------------------------------------------ shell

function shell(...content) {
  const tab = (id, label) => h('a', {
    href: `#${id}`,
    class: state.route === id ? 'active' : '',
  }, label);

  const today = new Date().toLocaleDateString(undefined, {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  });

  const edition = state.status && state.status.last_run
    ? `No. ${state.status.last_run.id}`
    : 'No. 1';

  return h('div', {},
    h('header', {},
      h('h1', {}, 'Community Insights'),
      h('nav', {}, tab('feed', 'Feed'), tab('monitor', 'Monitoring'), tab('debug', 'Detection')),
      statusChip(),
    ),
    // The dateline. Pure furniture, and the cheapest thing on the page that
    // says "this is an edition" rather than "this is a CRUD app".
    h('div', { class: 'dateline' },
      h('span', {}, today),
      h('span', { class: 'dateline-mid' }, 'Ranked by rubric, swept hourly'),
      h('span', {}, edition)),
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

function scoreVerdict(post) {
  // Three states, and they are genuinely different: judged, not yet judged,
  // and judged-but-failed. Collapsing the last two into "no score" would hide
  // a broken API key behind what looks like an ordinary backlog.
  if (post.ai_status && post.ai_status !== 'ok') {
    return h('div', { class: 'verdict failed' },
      h('span', { class: 'verdict-label' }, 'not scored'),
      h('span', { class: 'verdict-text' }, post.ai_error || 'scoring failed'));
  }

  if (post.ai_score === null || post.ai_score === undefined) {
    return h('div', { class: 'verdict pending' },
      h('span', { class: 'verdict-label' }, 'awaiting ranking'),
      h('span', { class: 'verdict-text' },
        'Not yet judged against the current rubric.'));
  }

  const dims = Array.isArray(post.dimensions) ? post.dimensions : [];

  return h('div', {},
    h('div', { class: 'verdict' },
      h('span', { class: 'verdict-score' }, post.ai_score),
      post.ai_verdict ? h('span', { class: 'verdict-label' }, post.ai_verdict) : null,
      h('span', { class: 'verdict-text' }, post.ai_rationale || ''),
    ),
    dims.length
      ? h('div', { class: 'dims' }, dims.map((d) => h('span', { class: 'dim' },
          d.name, ' ', h('b', {}, d.score),
          h('span', { class: 'dim-bar' },
            h('span', {
              style: `width: ${Math.max(0, Math.min(10, d.score)) * 10}%`,
            })),
        )))
      : null,
  );
}

// The flags worth surfacing on a card. Each is a fact about the post that the
// headline does not carry, and that a reader would want before clicking.
function postTags(p) {
  const tags = [];
  if (p.over18) tags.push(pill('NSFW', 'failed'));
  if (p.spoiler) tags.push(pill('spoiler'));
  if (p.locked) tags.push(pill('locked'));
  if (p.stickied) tags.push(pill('pinned'));
  if (p.archived) tags.push(pill('archived'));
  if (p.distinguished) tags.push(pill(p.distinguished, 'partial'));
  if (p.is_original_content) tags.push(pill('OC', 'ok'));
  if (p.is_gallery) tags.push(pill('gallery'));
  if (p.is_video) tags.push(pill('video'));
  if (p.total_awards) {
    tags.push(pill(`${p.total_awards} award${p.total_awards > 1 ? 's' : ''}`, 'partial'));
  }
  if (p.crosspost_origin) tags.push(pill(`x-post ${p.crosspost_origin}`));
  if (p.edited_utc) tags.push(pill('edited'));
  if (p.removed_by_category) tags.push(pill(`removed: ${p.removed_by_category}`, 'failed'));
  return tags;
}

function rankItem(p, index) {
  // The flair chip keeps Reddit's own colours where we managed to capture
  // them - the one place this theme defers to the source's palette.
  const flairStyle = p.flair_bg
    ? `background:${p.flair_bg};color:${p.flair_text_color || '#fff'};border-color:transparent`
    : null;

  const body = (p.selftext || '').trim();
  const clipped = p.selftext_chars && p.selftext_chars > body.length;

  return h('article', { class: 'rank-item' },
    h('div', { class: 'rank-num' }, String(index)),
    h('div', {},
      // Reddit's thumbnail hosts expire links, so a dead image removes itself
      // rather than leaving a broken-image glyph in the middle of the column.
      p.thumbnail
        ? h('img', {
            class: 'thumb', src: p.thumbnail, alt: '', loading: 'lazy',
            onerror: (e) => e.target.remove(),
          })
        : null,
      h('h3', { class: 'headline' },
        h('a', {
          href: p.permalink, target: '_blank', rel: 'noopener noreferrer',
        }, p.title)),
      h('div', { class: 'byline' },
        h('span', { class: 'community' }, p.community_display || `r/${p.community_name}`),
        h('span', { class: 'sep' }, '/'),
        h('span', {}, `u/${p.author}`),
        p.author_flair ? h('span', { class: 'sep' }, '/') : null,
        p.author_flair ? h('span', {}, p.author_flair) : null,
        h('span', { class: 'sep' }, '/'),
        h('span', {}, ago(p.created_utc)),
        p.domain ? h('span', { class: 'sep' }, '/') : null,
        p.domain ? h('span', {}, p.domain) : null,
      ),
      body
        ? h('p', { class: 'standfirst' },
            body.slice(0, 260) + (body.length > 260 || clipped ? '\u2026' : ''))
        : null,
      h('div', { class: 'metrics' },
        h('span', {}, h('b', {}, num(p.score)), ' points'),
        h('span', {}, h('b', {}, num(p.num_comments)), ' comments'),
        p.upvote_ratio
          ? h('span', {}, h('b', {}, `${Math.round(p.upvote_ratio * 100)}%`), ' upvoted')
          : null,
        p.selftext_chars ? h('span', {}, h('b', {}, num(p.selftext_chars)), ' chars') : null,
        h('span', {}, 'seen ', ago(p.first_seen_at)),
      ),
      (p.flair || postTags(p).length)
        ? h('div', { class: 'tags' },
            p.flair ? h('span', { class: 'pill flair', style: flairStyle }, p.flair) : null,
            ...postTags(p))
        : null,
      scoreVerdict(p),
    ),
  );
}

function leadStory(p) {
  const body = (p.selftext || '').trim();
  const dims = Array.isArray(p.dimensions) ? p.dimensions : [];

  return h('article', { class: 'lead' },
    h('div', { class: 'lead-kicker' },
      h('span', { class: 'lead-rank' }, 'Lead'),
      h('span', { class: 'community' }, p.community_display || `r/${p.community_name}`),
      h('span', { class: 'sep' }, '/'),
      h('span', {}, `u/${p.author}`),
      h('span', { class: 'sep' }, '/'),
      h('span', {}, ago(p.created_utc)),
    ),
    h('h3', { class: 'lead-headline' },
      h('a', { href: p.permalink, target: '_blank', rel: 'noopener noreferrer' }, p.title)),
    body ? h('p', { class: 'lead-standfirst' },
      body.slice(0, 340) + (body.length > 340 ? '\u2026' : '')) : null,
    h('div', { class: 'lead-foot' },
      h('div', { class: 'metrics' },
        h('span', {}, h('b', {}, num(p.score)), ' points'),
        h('span', {}, h('b', {}, num(p.num_comments)), ' comments'),
        p.upvote_ratio
          ? h('span', {}, h('b', {}, `${Math.round(p.upvote_ratio * 100)}%`), ' upvoted')
          : null,
      ),
      p.ai_score !== null && p.ai_score !== undefined
        ? h('div', { class: 'lead-verdict' },
            h('span', { class: 'lead-score' }, p.ai_score),
            h('div', {},
              p.ai_verdict ? h('div', { class: 'verdict-label' }, p.ai_verdict) : null,
              h('div', { class: 'verdict-text' }, p.ai_rationale || ''),
              dims.length ? h('div', { class: 'dims' }, dims.map((d) => h('span', { class: 'dim' },
                d.name, ' ', h('b', {}, d.score),
                h('span', { class: 'dim-bar' },
                  h('span', { style: `width: ${Math.max(0, Math.min(10, d.score)) * 10}%` })),
              ))) : null),
          )
        : h('div', { class: 'lead-verdict pending' },
            h('span', { class: 'verdict-label' }, 'awaiting ranking'),
            h('span', { class: 'verdict-text' },
              'Not yet judged against the current rubric.')),
    ),
  );
}

function feedView() {
  const container = h('div', {});
  const mount = h('div', {}, loading());

  container.append(h('h2', {}, 'The ranking'), mount);

  guard(async () => {
    const { community, flair, sort, min_score, unscored, page } = state.feed;
    const [data, communityData] = await Promise.all([
      api.posts({ community, flair, sort, min_score, unscored, page, per_page: 25 }),
      api.communities(),
    ]);

    const setFilter = (key, value) => {
      state.feed[key] = value;
      if (key !== 'page') state.feed.page = 1;
      render();
    };

    const dropdown = (key, options, current) => h('select', {
      onchange: (e) => setFilter(key, typeof current === 'number'
        ? Number(e.target.value) : e.target.value),
    }, options.map(([value, label]) => h('option', {
      value, selected: value === current,
    }, label)));

    const filters = h('form', { class: 'filters' },
      dropdown('community',
        [['', 'All communities']].concat(communityData.communities.map(
          (c) => [c.name, c.display_name || `r/${c.name}`])),
        community),
      dropdown('flair',
        [['', 'All flairs']].concat(data.flairs.map((f) => [f, f])), flair),
      dropdown('sort', [
        ['rank', 'AI rank'], ['contrarian', 'Underrated by Reddit'],
        ['score', 'Top score'], ['comments', 'Most comments'],
        ['new', 'Newest posted'], ['discovered', 'Recently discovered'],
      ], sort),
      dropdown('min_score',
        [[0, 'Any AI score'], [50, '50+'], [70, '70+'], [85, '85+']], min_score),
      dropdown('unscored', [
        ['include', 'Scored and unscored'], ['exclude', 'Scored only'],
        ['only', 'Unscored only'],
      ], unscored),
      h('span', { class: 'muted' }, `${num(data.total)} posts`),
    );

    // Page one gets a lead story: the top-ranked post set large, the way a
    // front page leads. Later pages do not - a "lead" on page 4 is just a
    // randomly enlarged row.
    const lead = page === 1 && data.posts.length ? data.posts[0] : null;
    const rest = lead ? data.posts.slice(1) : data.posts;

    const list = data.posts.length
      ? h('div', {},
          lead ? leadStory(lead) : null,
          h('div', { class: 'rank-list' },
            rest.map((p, i) => rankItem(p, data.rank_offset + i + (lead ? 2 : 1)))))
      : h('div', { class: 'card empty' }, 'No posts match those filters.');

    const pager = h('div', { class: 'pager' },
      h('button', {
        class: 'ghost', disabled: page <= 1,
        onclick: () => setFilter('page', page - 1),
      }, '\u2190 Previous'),
      h('span', { class: 'muted' }, `Page ${page} of ${data.pages}`),
      h('button', {
        class: 'ghost', disabled: page >= data.pages,
        onclick: () => setFilter('page', page + 1),
      }, 'Next \u2192'),
    );

    mount.replaceChildren(filters, list, pager);
  });

  return container;
}

// The intake panel: volume over time, where it came from, and the headline
// figures beside it. Three encodings, each chosen for its own data's job -
// magnitude over time, magnitude across nominal categories, single values.
function intakePanel(insights) {
  if (!insights) return null;

  const discovery = insights.discovery || [];
  const total = discovery.reduce((sum, d) => sum + d.posts, 0);
  const busiest = discovery.reduce((a, b) => (b.posts > a.posts ? b : a),
    { bucket: '', posts: 0 });

  // The server picks the granularity from how much history exists, so the
  // labels have to follow it rather than assume days.
  const hourly = insights.granularity === 'hour';
  const fmt = (b) => (hourly ? `${String(b).slice(11)}:00` : shortDay(b));
  const spanLabel = hourly ? 'last 48 hours' : `last ${insights.days} days`;

  const communities = (insights.communities || []).slice(0, 6).map((c) => ({
    label: c.display_name || `r/${c.name}`,
    posts: c.posts,
  }));

  return [
    h('h2', {}, `Intake · ${spanLabel}`),
    h('div', { class: 'panel-grid' },
      h('div', { class: 'card panel' },
        h('div', { class: 'panel-head' },
          h('span', {}, hourly ? 'Posts discovered per hour' : 'Posts discovered per day'),
          h('span', { class: 'muted' }, `${num(total)} total`)),
        columnChart(discovery.map((d) => ({ ...d, label: fmt(d.bucket) })), {
          labelKey: 'label', valueKey: 'posts', tickEvery: hourly ? 6 : 3,
          tip: (r) => `<b>${num(r.posts)}</b> posts<br>${fmt(r.bucket)}`,
        })),
      h('div', { class: 'card panel' },
        h('div', { class: 'panel-head' },
          h('span', {}, 'Where they came from'),
          h('span', { class: 'muted' }, 'all time')),
        // Nominal categories: every bar the same colour. Shading these by
        // value would encode length twice and say nothing new.
        barRows(communities, { labelKey: 'label', valueKey: 'posts' })),
      h('div', { class: 'card panel figures' },
        statTile(hourly ? 'Busiest hour' : 'Busiest day', num(busiest.posts),
          busiest.bucket ? fmt(busiest.bucket) : '\u2014'),
        statTile(hourly ? 'Hourly average' : 'Daily average',
          num(Math.round(total / Math.max(1, discovery.length))), 'posts discovered'),
        statTile('Communities', num((insights.communities || []).length), 'on the watchlist'),
      ),
    ),
  ];
}

// The score histogram lives with the rubric, because it answers the question
// the rubric raises: does this thing actually discriminate, or is everything
// a 70? Ordered buckets, so the one-hue ramp is the right encoding here.
function scorePanel(insights, scoringData) {
  const buckets = (insights && insights.scores) || [];
  const scored = buckets.reduce((sum, b) => sum + b.posts, 0);
  const spend = (insights && insights.spend) || {};

  // Before anything is scored, plot Reddit's own distribution instead - and
  // say so in the heading. An empty panel teaches nothing; a panel that
  // quietly passes Reddit's numbers off as the rubric's would be worse than
  // empty, so the label does the work.
  if (!scored) {
    const reddit = (insights && insights.reddit_scores) || [];
    const counted = reddit.reduce((sum, b) => sum + b.posts, 0);

    return h('div', { class: 'card panel' },
      h('div', { class: 'panel-head' },
        h('span', {}, 'Reddit score distribution'),
        h('span', { class: 'muted' }, `${num(counted)} posts`)),
      counted
        ? columnChart(reddit, {
            labelKey: 'label', valueKey: 'posts', ramp: true, tickEvery: 1,
            height: 124,
            tip: (r) => `<b>${num(r.posts)}</b> posts scored ${r.label} on Reddit`,
          })
        : h('div', { class: 'empty' }, 'No posts collected yet.'),
      h('div', { class: 'panel-foot' },
        h('span', {}, scoringData && scoringData.enabled
          ? 'The rubric’s own distribution replaces this once a scoring pass runs.'
          : 'Scoring is off — turn it on to rank these by the rubric instead.')));
  }

  const cached = spend.cached_tokens || 0;
  const billed = spend.input_tokens || 0;

  return h('div', { class: 'card panel' },
    h('div', { class: 'panel-head' },
      h('span', {}, 'Score distribution'),
      h('span', { class: 'muted' }, `${num(scored)} judged`)),
    columnChart(buckets.map((b) => ({ ...b, label: b.label })), {
      labelKey: 'label', valueKey: 'posts', ramp: true, tickEvery: 1, height: 124,
      tip: (r) => `<b>${num(r.posts)}</b> posts scored ${r.label}`,
    }),
    h('div', { class: 'panel-foot' },
      h('span', {}, 'mean ', h('b', {}, (spend.mean_score || 0).toFixed(1))),
      h('span', {}, 'input ', h('b', {}, num(billed)), ' tok'),
      h('span', {}, 'output ', h('b', {}, num(spend.output_tokens)), ' tok'),
      // The saving is the whole argument for scoring in passes, so it is
      // reported rather than folded into the total.
      h('span', {}, 'from cache ', h('b', {}, num(cached)), ' tok'),
    ));
}

// ------------------------------------------------------------ the rubric

// The editor for how posts are ranked. Saving publishes a new rubric version
// rather than editing the current one, which is why the button says how many
// posts it is about to invalidate: under a new rubric every existing score
// stops applying, and the whole archive has to be judged again.
function rubricSection(data, insights) {
  if (!data) return null;

  const prompt = data.prompt;
  const stats = data.stats || {};

  const textarea = h('textarea', {
    name: 'body',
    spellcheck: 'false',
    maxlength: String(data.max_chars || 8000),
    oninput: (e) => { state.rubricDraft = e.target.value; },
  });

  // Set as a property, not an attribute: the value belongs to the live element,
  // and setting it as an attribute would only define the initial value.
  textarea.value = state.rubricDraft !== null
    ? state.rubricDraft
    : (prompt ? prompt.body : '');

  const dirty = () => prompt && textarea.value.trim() !== prompt.body.trim();

  const coverage = h('div', { class: 'coverage' },
    h('div', {}, h('span', {}, 'Active rubric'),
      h('b', {}, prompt ? `#${prompt.id}` : '—'),
      h('div', { class: 'muted' }, prompt ? ago(prompt.created_at) : '')),
    h('div', {}, h('span', {}, 'Scored'),
      h('b', {}, `${num(stats.scored)} / ${num(stats.posts)}`),
      h('div', { class: 'muted' }, `${num(data.pending)} awaiting`)),
    h('div', {}, h('span', {}, 'Mean score'),
      h('b', {}, stats.mean_score ? stats.mean_score.toFixed(1) : '—'),
      h('div', { class: 'muted' }, stats.failed ? `${num(stats.failed)} failed` : '')),
    h('div', {}, h('span', {}, 'Model'),
      h('b', {}, data.enabled ? 'on' : 'off'),
      h('div', { class: 'muted' }, data.model || '')),
  );

  const save = (rescore) => guard(async () => {
    const result = await api.saveRubric({ body: textarea.value, rescore });
    state.rubricDraft = null;
    setBanner('notice', result.detail);
  });

  const form = h('div', { class: 'rubric' },
    h('p', { class: 'rubric-note' },
      'This text is the whole of what "good" means here. It is sent with every '
      + 'post; the model returns a 0–100 score, a verdict and the axes you '
      + 'name in it. Saving publishes a new version and re-ranks the archive '
      + 'against it — earlier versions keep their scores and can be restored.'),
    h('label', {}, 'Ranking rubric'),
    textarea,
    h('div', { class: 'rubric-foot' },
      h('button', {
        class: 'primary',
        onclick: () => save(true),
      }, 'Save and re-rank'),
      h('button', {
        class: 'ghost',
        onclick: () => save(false),
      }, 'Save only'),
      h('button', {
        class: 'ghost',
        disabled: !data.enabled || !data.pending,
        title: data.enabled
          ? 'Score the posts that have no verdict under this rubric'
          : 'Scoring is disabled — set SCORING_ENABLED=true',
        onclick: () => guard(async () => {
          const result = await api.runScoring();
          setBanner('notice', result.detail);
        }),
      }, data.pending ? `Score ${num(data.pending)} pending` : 'Nothing pending'),
      h('button', {
        class: 'ghost',
        disabled: !prompt,
        onclick: () => {
          state.rubricDraft = null;
          render();
        },
      }, 'Revert edits'),
      h('span', { class: 'muted' },
        !data.enabled
          ? 'Scoring is off. Set SCORING_ENABLED=true and ANTHROPIC_API_KEY, then restart the worker.'
          : (dirty() ? 'Unsaved changes.' : 'Saved.')),
    ),
  );

  const versions = (data.history || []).filter((v) => !v.is_active);

  const history = versions.length
    ? h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, 'Version'), h('th', {}, 'Created'),
          h('th', { class: 'num' }, 'Scores kept'), h('th', {}, 'Opening line'),
          h('th', {}, ''))),
        h('tbody', {}, versions.map((v) => h('tr', {},
          h('td', {}, `#${v.id}`),
          h('td', { class: 'muted nowrap' }, ago(v.created_at)),
          h('td', { class: 'num' }, num(v.scored_count)),
          h('td', { class: 'muted wrap' }, (v.body || '').split('\n')[0].slice(0, 80)),
          h('td', { class: 'nowrap' },
            h('button', {
              class: 'ghost',
              title: 'Make this rubric active again. Its scores are still on record.',
              onclick: () => guard(async () => {
                const result = await api.activateRubric(v.id);
                state.rubricDraft = null;
                setBanner('notice', result.detail);
              }),
            }, 'Restore')),
        ))))
    : null;

  return [
    h('h2', {}, 'How posts are ranked'),
    h('div', { class: 'rubric-grid' },
      h('div', { class: 'card' }, coverage, form),
      scorePanel(insights, data)),
    history ? h('h2', {}, 'Earlier rubrics') : null,
    history ? h('div', { class: 'card' }, history) : null,
  ];
}

// -------------------------------------------------------------- monitoring

function monitorView() {
  const container = h('div', {});
  const mount = h('div', {}, loading());
  container.append(mount);

  guard(async () => {
    const [communityData, runData, discoveryData, scoringData, insights] =
      await Promise.all([
        api.communities(), api.runs(), api.discoveries(), api.scoring(),
        api.insights(14),
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

    const dailyByName = Object.fromEntries(
      (insights.communities || []).map((c) => [c.name, c.daily]));

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
      h('td', { class: 'spark-cell' },
        dailyByName[c.name] ? sparkline(dailyByName[c.name]) : null),
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
        h('th', {}, '14 days'),
        h('th', {}, 'Last result'), h('th', {}, 'Last checked'), h('th', {}, ''),
      )),
      h('tbody', {}, communityRows.length ? communityRows : h('tr', {}, h('td', {
        colspan: '9', class: 'empty',
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
        // A scoring run never touches a scrape backend, so naming one here
        // would claim it used Chromium when it did not.
        h('td', {}, r.kind === 'score' ? 'scoring' : r.backend),
        h('td', {}, pill(r.status, r.status)),
        h('td', { class: 'num' }, r.communities_checked),
        h('td', { class: 'num' }, r.posts_new),
        h('td', { class: 'num' }, r.posts_refreshed),
        h('td', { class: 'num' }, r.posts_scored || 0),
        h('td', { class: 'muted' },
          r.error ? r.error.slice(0, 70)
            : (r.failed_items ? `${r.failed_items} issue(s) — click` : '')),
      );

      if (!open) return [row];

      const detail = state.runDetail[r.id];

      const body = !detail
        ? h('td', { colspan: '11', class: 'empty' }, 'Loading…')
        : h('td', { colspan: '11', class: 'detail-cell' },
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
        h('th', { class: 'num' }, 'Refreshed'), h('th', { class: 'num' }, 'Scored'),
        h('th', {}, 'Notes'),
      )),
      h('tbody', {}, runRows.length ? runRows : h('tr', {}, h('td', {
        colspan: '11', class: 'empty',
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
      h('td', { class: 'num' },
        p.ai_score === null || p.ai_score === undefined
          ? h('span', { class: 'muted' }, '—')
          : h('strong', {}, p.ai_score)),
      h('td', { class: 'muted wrap' }, p.ai_rationale || ''),
      h('td', { class: 'muted nowrap' }, ago(p.first_seen_at)),
    ));

    const discoveryTable = h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, 'Post'), h('th', {}, 'Community'), h('th', {}, 'Author'),
        h('th', {}, 'Flair'), h('th', { class: 'num' }, 'Score'),
        h('th', { class: 'num' }, 'Comments'), h('th', { class: 'num' }, 'AI'),
        h('th', {}, 'Verdict'), h('th', {}, 'First seen'),
      )),
      h('tbody', {}, discoveryRows.length ? discoveryRows : h('tr', {}, h('td', {
        colspan: '9', class: 'empty',
      }, 'Nothing discovered yet.'))),
    );

    mount.replaceChildren(
      ...(intakePanel(insights) || []).filter(Boolean),
      ...(rubricSection(scoringData, insights) || []).filter(Boolean),
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
    const [data, insights] = await Promise.all([
      api.debugSessions(), api.insights(14),
    ]);

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

    const t = insights.detection || {};

    mount.replaceChildren(
      h('p', { class: 'muted' },
        'Unauthenticated by design — this is a lab target. These views are '
        + 'excluded from RequestLog, so inspecting the log does not write to it.'),
      h('div', { class: 'panel-grid detection-grid' },
        h('div', { class: 'card panel' },
          h('div', { class: 'panel-head' },
            h('span', {}, 'Requests per hour'),
            h('span', { class: 'muted' }, `${num(t.requests_1h)} in the last hour`)),
          columnChart((insights.requests || []).map((r) => ({
            ...r, label: r.hour.slice(11) + ':00',
          })), {
            labelKey: 'label', valueKey: 'hits', tickEvery: 4, height: 124,
            tip: (r) => `<b>${num(r.hits)}</b> requests<br>${r.hour.slice(11)}:00 UTC`,
          })),
        h('div', { class: 'card panel figures' },
          statTile('Sessions', num(t.sessions), 'seen all time'),
          statTile('Requests', num(t.requests), 'logged'),
          statTile('Signals', num(t.signals),
            t.signals ? 'scored' : 'nothing scoring yet'),
        ),
      ),
      h('h2', {}, 'Sessions'),
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
    // The first render happens before this poll resolves, so there is no strip
    // in the DOM yet. refreshStatusChrome can only replace an element that
    // exists, so without this the strip stays missing until something else
    // forces a full render - which, on an idle worker, is never.
    const firstStatus = state.status === null;
    state.status = next;

    // A sweep finishing changes the tables underneath us, so re-render the
    // whole view rather than just the strip.
    if (firstStatus || wasActive !== isActive || isActive) {
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
