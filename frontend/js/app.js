// ── Metric definitions ───────────────────────────────────────────────────────
// dir: +1 = higher is better, -1 = lower is better (for delta coloring).
const METRICS = [
  { key: 'kills',    label: 'Kills / game',     dir: +1, dp: 1 },
  { key: 'deaths',   label: 'Deaths / game',    dir: -1, dp: 1 },
  { key: 'assists',  label: 'Assists / game',   dir: +1, dp: 1 },
  { key: 'kda',      label: 'KDA',              dir: +1, dp: 2 },
  { key: 'cspm',     label: 'CS / min',         dir: +1, dp: 2 },
  { key: 'gpm',      label: 'Gold / min',       dir: +1, dp: 0 },
  { key: 'dpm',      label: 'Damage / min',     dir: +1, dp: 0 },
  { key: 'gold_pct', label: 'Gold %',           dir: +1, dp: 1, suffix: '%' },
  { key: 'dmg_pct',  label: 'Damage %',         dir: +1, dp: 1, suffix: '%' },
  { key: 'csd15',    label: 'CS diff @15',      dir: +1, dp: 1, signed: true },
  { key: 'gd15',     label: 'Gold diff @15',    dir: +1, dp: 0, signed: true },
];

const State = { player: null, role: null, team: null, season: null, split: null,
                filters: null, champion: null, lastStats: null };

const $ = (id) => document.getElementById(id);

// True when an API call failed because the backend is unreachable (down / 5xx),
// as opposed to a genuine 404 for an unknown player/team/champion.
const SERVER_DOWN_MSG = "Can't reach the server — is run.py running?";
const isServerDown = (e) => !!e && e.message === 'SERVER_UNREACHABLE';

function fmt(v, m) {
  if (v == null) return '—';
  const n = Number(v).toFixed(m.dp);
  const sign = (m.signed && v > 0) ? '+' : '';
  return `${sign}${n}${m.suffix || ''}`;
}

function deltaCell(playerVal, baseVal, m) {
  if (playerVal == null || baseVal == null) return '<td class="delta delta-flat">—</td>';
  const diff = playerVal - baseVal;
  const good = diff * m.dir > 0;
  const flat = Math.abs(diff) < 1e-9;
  const cls = flat ? 'delta-flat' : (good ? 'delta-pos' : 'delta-neg');
  const sign = diff > 0 ? '+' : '';
  return `<td class="delta ${cls}">${sign}${diff.toFixed(m.dp)}${m.suffix || ''}</td>`;
}

// ── Loading helpers ──────────────────────────────────────────────────────────
function showLoading(msg) { $('loading-msg').textContent = msg || 'Loading…'; $('loading').classList.remove('hidden'); }
function hideLoading() { $('loading').classList.add('hidden'); }

// ── Search index (players + teams + roles) ───────────────────────────────────
// Each entry: { kind:'player'|'team'|'role', key, label, sub }. `key` is what the
// API needs (player name / team name / role code); `label` is what we match & show.
let SEARCH_INDEX = [];

const KIND_LABEL = { player: 'Player', team: 'Team', role: 'Role', champion: 'Champion' };
const KIND_ORDER = { player: 0, team: 1, role: 2, champion: 3 };

function plural(n) { return `${n} player${n === 1 ? '' : 's'}`; }

async function initSearch() {
  const [players, teams, roles] = await Promise.all([
    Api.players(), Api.teams(), Api.roles(),
  ]);
  // Champions are an enhancement — never let their failure break player search.
  let champions = [];
  try { champions = await Api.champions(); } catch (e) { champions = []; }
  SEARCH_INDEX = [
    ...players.map((p) => ({ kind: 'player', key: p.name, label: p.name,
                             sub: `${p.role_label} · ${p.team}` })),
    ...teams.map((t) => ({ kind: 'team', key: t.team, label: t.team,
                           sub: plural(t.player_count) })),
    ...roles.map((r) => ({ kind: 'role', key: r.role, label: r.role_label,
                           sub: plural(r.player_count) })),
    ...champions.map((c) => ({ kind: 'champion', key: c.champion, label: c.champion,
                              sub: 'synergies & counters' })),
  ];

  const chips = $('player-chips');
  chips.innerHTML = '';
  players.forEach((p) => {
    const chip = document.createElement('button');
    chip.className = 'quick-chip';
    chip.innerHTML = `${p.name}<span>${p.role_label} · ${p.team}</span>`;
    chip.onclick = () => selectPlayer(p.name);
    chips.appendChild(chip);
  });
}

// ── Autocomplete ─────────────────────────────────────────────────────────────
let acIndex = -1;            // index of the keyboard-highlighted row (-1 = none)

// Returns true if every char of `q` appears in `name` in order (typo/gap tolerant).
function isSubsequence(q, name) {
  let i = 0;
  for (let j = 0; j < name.length && i < q.length; j++) {
    if (name[j] === q[i]) i++;
  }
  return i === q.length;
}

// Rank the index against the typed text, matching on each entry's label. Ties
// break players → teams → roles, then alphabetically. Empty query returns all.
function matchEntries(query) {
  const q = query.trim().toLowerCase();
  if (!q) return SEARCH_INDEX.slice();
  const scored = [];
  SEARCH_INDEX.forEach((e) => {
    const label = e.label.toLowerCase();
    let rank;
    if (label === q) rank = 0;
    else if (label.startsWith(q)) rank = 1;
    else if (label.includes(q)) rank = 2;
    else if (isSubsequence(q, label)) rank = 3;
    else return;
    scored.push({ e, rank });
  });
  scored.sort((a, b) => a.rank - b.rank
    || KIND_ORDER[a.e.kind] - KIND_ORDER[b.e.kind]
    || a.e.label.localeCompare(b.e.label));
  return scored.map((s) => s.e);
}

function hideAutocomplete() {
  const list = $('ac-list');
  list.classList.add('hidden');
  $('search-input').setAttribute('aria-expanded', 'false');
  acIndex = -1;
}

function showAutocomplete(query) {
  const matches = matchEntries(query);
  const list = $('ac-list');
  list.innerHTML = '';
  acIndex = -1;
  if (!matches.length) { hideAutocomplete(); return; }
  matches.forEach((e) => {
    const li = document.createElement('li');
    li.className = 'ac-item';
    li.setAttribute('role', 'option');
    li.dataset.kind = e.kind;
    li.dataset.key = e.key;
    li.innerHTML =
      `<span class="ac-kind ac-kind-${e.kind}">${KIND_LABEL[e.kind]}</span>` +
      `<span class="name">${e.label}</span>` +
      `<span class="team">${e.sub}</span>`;
    // mousedown (not click) so it fires before the input's blur.
    li.addEventListener('mousedown', (ev) => { ev.preventDefault(); chooseEntry(e); });
    list.appendChild(li);
  });
  list.classList.remove('hidden');
  $('search-input').setAttribute('aria-expanded', 'true');
}

function moveAcHighlight(delta) {
  const items = $('ac-list').querySelectorAll('.ac-item');
  if (!items.length) return;
  if (acIndex >= 0) items[acIndex].classList.remove('active');
  acIndex = (acIndex + delta + items.length) % items.length;
  items[acIndex].classList.add('active');
  items[acIndex].scrollIntoView({ block: 'nearest' });
}

// Route a chosen index entry to the right view.
function chooseEntry(e) {
  $('search-input').value = e.label;
  hideAutocomplete();
  if (e.kind === 'player') loadPlayer(e.key);
  else if (e.kind === 'champion') openChampion(e.key, { fromSearch: true });
  else openGroup(e.kind, e.key);
}

// Fill the box with a player name and open their profile (used by chips/cards).
function selectPlayer(name) {
  $('search-input').value = name;
  hideAutocomplete();
  loadPlayer(name);
}

// Resolve free-typed text (Search button / plain Enter) to the best match.
function doSearch() {
  hideAutocomplete();
  const text = $('search-input').value.trim();
  const err = $('search-error');
  err.classList.add('hidden');
  if (!text) return;
  const best = matchEntries(text)[0];
  if (!best) {
    err.textContent = `No player, team or role matches "${text}".`;
    err.classList.remove('hidden');
    return;
  }
  chooseEntry(best);
}

// ── Player profile loading ───────────────────────────────────────────────────
async function loadPlayer(name) {
  const err = $('search-error');
  err.classList.add('hidden');
  showLoading('Loading player…');
  try {
    State.filters = await Api.filters(name);
    State.player = name;
    State.champion = null;
    // Default: most recent season, all splits.
    State.season = State.filters.seasons.length ? State.filters.seasons[0].season : null;
    State.split = null;
    await loadStats();
    renderShell();
    $('group-view').classList.add('hidden');
    $('results').classList.remove('hidden');
  } catch (e) {
    err.textContent = isServerDown(e)
      ? SERVER_DOWN_MSG
      : `"${name}" isn't searchable. Try: Teddy, Ruler, Kiin or Zeus.`;
    err.classList.remove('hidden');
    $('results').classList.add('hidden');
  } finally {
    hideLoading();
  }
}

// ── Team / role group view ───────────────────────────────────────────────────
const RATING_LABEL = { strong: 'Strong', average: 'Average', struggling: 'Struggling' };

async function openGroup(kind, key) {
  hideAutocomplete();
  $('search-error').classList.add('hidden');
  showLoading('Loading…');
  try {
    const data = kind === 'team' ? await Api.teamGroup(key) : await Api.roleGroup(key);
    renderGroup(kind, data);
    $('results').classList.add('hidden');
    $('group-view').classList.remove('hidden');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    const err = $('search-error');
    err.textContent = isServerDown(e) ? SERVER_DOWN_MSG : `Couldn't load that ${kind}.`;
    err.classList.remove('hidden');
  } finally {
    hideLoading();
  }
}

function renderGroup(kind, data) {
  const players = data.players || [];
  $('group-title').textContent = kind === 'team' ? data.team : data.role_label;
  $('group-sub').textContent =
    `${kind === 'team' ? 'Team' : 'Role'} · ${plural(players.length)}`;

  const list = $('group-list');
  list.innerHTML = '';
  if (!players.length) {
    list.innerHTML = '<div class="no-data">No players in this group yet.</div>';
    return;
  }
  players.forEach((p) => {
    const card = document.createElement('button');
    card.className = 'group-card';
    const wr = p.win_pct == null ? '—' : `${p.win_pct}%`;
    const streak = p.streak
      ? `<span class="streak-badge ${p.streak.type}">` +
        `${p.streak.length}-${p.streak.type === 'win' ? 'win' : 'loss'} streak</span>`
      : '';
    card.innerHTML =
      `<div class="group-card-main">` +
        `<span class="group-card-name">${p.name}</span>` +
        `<span class="group-card-meta">${p.role_label} · ${p.team}</span>` +
      `</div>` +
      `<div class="group-card-perf">` +
        `<span class="group-card-wr">${wr} WR <em>(${p.games} games)</em></span>` +
        streak +
        `<span class="rating-tag rating-${p.rating}">${RATING_LABEL[p.rating] || p.rating}</span>` +
      `</div>`;
    card.onclick = () => selectPlayer(p.name);
    list.appendChild(card);
  });
}

// ── Champion draft graph (VIN-20) ─────────────────────────────────────────────
// Where "← Back" returns to: a player profile (if opened from there) or home.
let champReturn = 'home';
// Selected champion + timeframe + role (null = all roles) for the champion view.
const champState = { name: null, tf: {}, role: null };

function openChampion(name, { fromSearch = false } = {}) {
  champReturn = fromSearch ? 'home' : (State.player ? 'results' : 'home');
  champState.name = name;
  // Inherit the player's current timeframe; a search-opened champion uses all data.
  champState.tf = fromSearch ? {} : { season: State.season, split: State.split };
  champState.role = null;
  return loadChampionGraph(true);
}

async function loadChampionGraph(switchView) {
  hideAutocomplete();
  $('search-error').classList.add('hidden');
  showLoading('Loading champion…');
  try {
    const data = await Api.championGraph(champState.name,
      { ...champState.tf, role: champState.role });
    renderChampionGraph(data);
    if (switchView) {
      $('results').classList.add('hidden');
      $('group-view').classList.add('hidden');
      $('champion-view').classList.remove('hidden');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  } catch (e) {
    const err = $('search-error');
    err.textContent = isServerDown(e)
      ? SERVER_DOWN_MSG
      : `Couldn't load the champion graph for "${champState.name}".`;
    err.classList.remove('hidden');
  } finally {
    hideLoading();
  }
}

// One ranked edge row: champion portrait, name, signed win-margin %, sample size.
function edgeRow(e, kind) {
  const cls = e.weight > 0 ? 'delta-pos' : (e.weight < 0 ? 'delta-neg' : 'delta-flat');
  const sign = e.weight > 0 ? '+' : '';
  return `<button type="button" class="edge-row" data-other="${e.champion}" data-kind="${kind}">` +
    `<img src="${e.image_url}" alt="${e.champion}" onerror="this.style.visibility='hidden'" />` +
    `<span class="edge-name">${e.champion}</span>` +
    `<span class="edge-weight ${cls}">${sign}${e.weight.toFixed(1)}%</span>` +
    `<span class="edge-games">${e.games} game${e.games === 1 ? '' : 's'}</span>` +
  `</button>`;
}

// Render an edge list into `containerId`, wiring each row to open its pairing detail.
function renderEdges(containerId, edges, emptyMsg, kind) {
  const el = $(containerId);
  if (!edges || !edges.length) { el.innerHTML = `<div class="no-data">${emptyMsg}</div>`; return; }
  el.innerHTML = `<div class="edge-list">${edges.map((e) => edgeRow(e, kind)).join('')}</div>`;
  el.querySelectorAll('.edge-row').forEach((b) => {
    b.onclick = () => openPairing(b.dataset.other, b.dataset.kind);
  });
}

// One hero stat: label + value, optionally coloured vs a 50% midpoint.
function statChip(label, value, { pct = false, vs50 = false } = {}) {
  const txt = value == null ? '—' : (pct ? `${value.toFixed(1)}%` : value);
  let cls = '';
  if (vs50 && value != null) cls = value > 50 ? ' delta-pos' : (value < 50 ? ' delta-neg' : '');
  return `<div class="champ-stat"><span class="champ-stat-label">${label}</span>` +
    `<span class="champ-stat-val${cls}">${txt}</span></div>`;
}

// Small 3-column table (label | win rate | adjusted WR) for the split sections.
function splitTable(firstHead, rows) {
  if (!rows.length) return '<div class="no-data">No games in this timeframe.</div>';
  let html = `<table class="cmp"><thead><tr><th>${firstHead}</th><th>Games</th>` +
    `<th>Win rate</th><th>Adjusted WR</th></tr></thead><tbody>`;
  rows.forEach((r) => {
    const pct = (v) => (v == null ? '—' : `${v.toFixed(1)}%`);
    html += `<tr><td>${r.label}</td><td>${r.games}</td>` +
      `<td class="val-strong">${pct(r.win_rate)}</td><td>${pct(r.adjusted_win_rate)}</td></tr>`;
  });
  return html + '</tbody></table>';
}

function renderChampRoleRow(d) {
  const row = $('champ-role-row');
  row.innerHTML = '';
  const addPill = (label, roleVal) => {
    const b = document.createElement('button');
    b.className = 'pill' + (champState.role === roleVal ? ' active' : '');
    b.textContent = label;
    b.onclick = () => { champState.role = roleVal; loadChampionGraph(false); };
    row.appendChild(b);
  };
  addPill('All roles', null);
  (d.roles || []).forEach((r) => addPill(`${r.role_label} · ${r.games}g`, r.role));
}

function renderChampionGraph(d) {
  const tf = champState.tf;
  const s = d.stats || {};
  $('champ-hero-img').src = d.image_url || '';
  $('champ-hero-name').textContent = d.champion;
  const seasonTxt = tf && tf.season != null ? `Season ${tf.season}` : 'All seasons';
  const splitTxt = tf && tf.split ? ` · ${tf.split}` : '';
  $('champ-hero-sub').textContent = `LCK draft graph · ${seasonTxt}${splitTxt}`;
  $('champ-hero-stats').innerHTML =
    statChip('Games', s.games) +
    statChip('Win rate', s.win_rate, { pct: true, vs50: true }) +
    statChip('Adjusted WR', s.adjusted_win_rate, { pct: true, vs50: true }) +
    statChip('Gold diff @15', s.gd15 == null ? null : Math.round(s.gd15));
  renderChampRoleRow(d);

  $('champ-duration').innerHTML = splitTable('Game length',
    (s.duration_splits || []).map((x) => ({ label: `> ${x.min_minutes} min`, ...x })));
  $('champ-dragons').innerHTML = splitTable('Dragons',
    (s.dragon_splits || []).map((x) => ({ label: x.bucket, ...x })));

  // Selecting a new champion/role closes any open pairing detail.
  $('champ-pairing').classList.add('hidden');
  renderEdges('champ-synergies', d.synergies,
    'No teammates with 3+ games together in this timeframe.', 'synergy');
  renderEdges('champ-counters', d.counters,
    'No matchups with 3+ games in this timeframe.', 'counter');
}

// ── Pairing detail (recomputed over games where the other champion appears) ────
async function openPairing(other, kind) {
  showLoading('Loading pairing…');
  try {
    const d = await Api.championPairing(champState.name,
      { other, kind, ...champState.tf, role: champState.role });
    renderPairing(d);
    $('champ-pairing').classList.remove('hidden');
    $('champ-pairing').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    const err = $('search-error');
    err.textContent = isServerDown(e) ? SERVER_DOWN_MSG : `Couldn't load that pairing.`;
    err.classList.remove('hidden');
  } finally {
    hideLoading();
  }
}

function renderPairing(d) {
  const verb = d.kind === 'synergy' ? 'with' : 'vs';
  $('champ-pairing-title').innerHTML =
    `${d.champion} ${verb} ${d.other.champion} ` +
    `<span class="hint">— recomputed for shared games vs overall</span> ` +
    `<button type="button" class="graph-link" id="champ-pairing-close">Close ✕</button>`;
  $('champ-pairing-close').onclick = () => $('champ-pairing').classList.add('hidden');

  const sub = d.stats || {}, all = d.overall || {};
  const pct = (v) => (v == null ? '—' : `${v.toFixed(1)}%`);
  const num = (v) => (v == null ? '—' : Math.round(v));
  const durWR = (st, m) => {
    const x = (st.duration_splits || []).find((q) => q.min_minutes === m);
    return x ? x.win_rate : null;
  };
  const delta = (a, b, suffix = '%') => {
    if (a == null || b == null) return '<td class="delta delta-flat">—</td>';
    const diff = a - b;
    const cls = Math.abs(diff) < 1e-9 ? 'delta-flat' : (diff > 0 ? 'delta-pos' : 'delta-neg');
    const sign = diff > 0 ? '+' : '';
    return `<td class="delta ${cls}">${sign}${diff.toFixed(1)}${suffix}</td>`;
  };
  const rows = [
    ['Games', sub.games, all.games, ''],
    ['Win rate', sub.win_rate, all.win_rate, 'pct'],
    ['Adjusted WR', sub.adjusted_win_rate, all.adjusted_win_rate, 'pct'],
    ['Gold diff @15', sub.gd15, all.gd15, 'gold'],
    ['Win rate > 25 min', durWR(sub, 25), durWR(all, 25), 'pct'],
    ['Win rate > 30 min', durWR(sub, 30), durWR(all, 30), 'pct'],
    ['Win rate > 35 min', durWR(sub, 35), durWR(all, 35), 'pct'],
  ];
  let html = `<table class="cmp"><thead><tr><th>Metric</th>` +
    `<th class="col-player">${verb} ${d.other.champion}</th><th>Overall</th><th>Δ</th></tr></thead><tbody>`;
  rows.forEach(([label, a, b, type]) => {
    const fmt = type === 'pct' ? pct : (type === 'gold' ? num : (v) => (v == null ? '—' : v));
    const dCell = type === 'pct' ? delta(a, b, '%')
      : type === 'gold' ? delta(a, b, '') : '<td class="delta delta-flat">—</td>';
    html += `<tr><td>${label}</td><td class="val-strong col-player">${fmt(a)}</td>` +
      `<td>${fmt(b)}</td>${dCell}</tr>`;
  });
  $('champ-pairing-table').innerHTML = html + '</tbody></table>';
}

function champBack() {
  $('champion-view').classList.add('hidden');
  if (champReturn === 'results' && State.player) {
    $('results').classList.remove('hidden');
  } else {
    goHome();
  }
}

// ── Stats loading ────────────────────────────────────────────────────────────
async function loadStats() {
  const data = await Api.stats(State.player, {
    season: State.season, split: State.split, champion: State.champion,
  });
  State.role = data.role;
  State.team = data.team;
  State.lastStats = data;
  return data;
}

// ── Rendering ────────────────────────────────────────────────────────────────
function renderShell() {
  const d = State.lastStats;
  $('player-name').textContent = State.player;
  const streakTag = d.streak
    ? `<span class="streak-badge ${d.streak.type}">` +
      `${d.streak.length}-${d.streak.type === 'win' ? 'win' : 'loss'} streak</span>`
    : '';
  $('player-meta').innerHTML =
    `<span class="tag tag-LCK">LCK</span>` +
    `<button type="button" class="tag tag-role tag-link" data-kind="role" data-key="${d.role}">${d.role_label}</button>` +
    `<button type="button" class="tag tag-team tag-link" data-kind="team" data-key="${d.team}">${d.team}</button>` +
    streakTag;
  // Role/team tags jump to that group's player list.
  $('player-meta').querySelectorAll('.tag-link').forEach((b) => {
    b.onclick = () => openGroup(b.dataset.kind, b.dataset.key);
  });
  renderSeasonRow();
  renderSplitRow();
  renderOverall();
  renderChampGrid();
  renderMatchHistory();
  renderChampDetail();
}

function renderSeasonRow() {
  const row = $('season-row');
  row.innerHTML = '';
  State.filters.seasons.forEach((s) => {
    const b = document.createElement('button');
    b.className = 'pill' + (s.season === State.season ? ' active' : '');
    b.textContent = s.label;
    b.onclick = async () => {
      State.season = s.season; State.split = null; State.champion = null;
      showLoading(); await loadStats(); renderShell(); hideLoading();
    };
    row.appendChild(b);
  });
}

function renderSplitRow() {
  const row = $('split-row');
  row.innerHTML = '';
  const splits = (State.filters.splits_by_season[State.season]) || [];

  const all = document.createElement('button');
  all.className = 'pill' + (State.split == null ? ' active' : '');
  all.textContent = 'All splits';
  all.onclick = async () => {
    State.split = null; State.champion = null;
    showLoading(); await loadStats(); renderShell(); hideLoading();
  };
  row.appendChild(all);

  splits.forEach((s) => {
    const b = document.createElement('button');
    b.className = 'pill' + (s.value === State.split ? ' active' : '');
    b.textContent = s.label;
    b.onclick = async () => {
      State.split = s.value; State.champion = null;
      showLoading(); await loadStats(); renderShell(); hideLoading();
    };
    row.appendChild(b);
  });
}

function cmpTable(columns, getRow) {
  // columns: [{label}], getRow(m) -> array of cell HTML strings aligned to columns
  let html = '<table class="cmp"><thead><tr><th>Metric</th>';
  columns.forEach((c) => { html += `<th class="${c.cls || ''}">${c.label}</th>`; });
  html += '</tr></thead><tbody>';
  METRICS.forEach((m) => {
    html += `<tr><td>${m.label}</td>${getRow(m).join('')}</tr>`;
  });
  html += '</tbody></table>';
  return html;
}

function renderOverall() {
  const d = State.lastStats;
  $('overall-title').textContent =
    `Overall (${d.overall.games} games) — vs LCK ${d.role_label} average`;
  if (!d.overall.games) {
    $('overall-table').innerHTML = '<div class="no-data">No games in this timeframe.</div>';
    return;
  }
  const cols = [
    { label: State.player, cls: 'col-player' },
    { label: `LCK ${d.role_label} avg` },
    { label: 'Δ' },
  ];
  $('overall-table').innerHTML = cmpTable(cols, (m) => {
    const p = d.overall[m.key], b = d.lck_role_baseline[m.key];
    return [
      `<td class="val-strong col-player">${fmt(p, m)}</td>`,
      `<td>${fmt(b, m)}</td>`,
      deltaCell(p, b, m),
    ];
  });
}

function renderChampGrid() {
  const grid = $('champ-grid');
  const champs = State.lastStats.champions;
  $('champ-hint').textContent = champs.length
    ? `— click a champion to compare (${champs.length})` : '';
  grid.innerHTML = '';
  if (!champs.length) {
    grid.innerHTML = '<div class="no-data">No champions in this timeframe.</div>';
    return;
  }
  champs.forEach((c) => {
    const card = document.createElement('div');
    const tierCls = c.tier === 'top' ? ' tier-top'
                  : c.tier === 'bottom' ? ' tier-bottom' : '';
    card.className = 'champ-card' + (c.champion === State.champion ? ' active' : '') + tierCls;
    const arrow = c.tier === 'top'
      ? '<span class="tier-arrow up" title="Top performer (≥15% above LCK role avg)">▲</span>'
      : c.tier === 'bottom'
      ? '<span class="tier-arrow down" title="Underperforming (≥15% below LCK role avg)">▼</span>'
      : '';
    card.innerHTML =
      `<img src="${c.image_url}" alt="${c.champion}" ` +
      `onerror="this.style.visibility='hidden'" />` +
      arrow +
      `<div class="champ-card-name">${c.champion}</div>` +
      `<div class="champ-card-games">${c.games} games</div>`;
    card.onclick = async () => {
      State.champion = (State.champion === c.champion) ? null : c.champion;
      showLoading(); await loadStats();
      renderChampGrid(); renderMatchHistory(); renderChampDetail(); hideLoading();
    };
    grid.appendChild(card);
  });
}

// Format an item-completion time (seconds) as mm:ss, or "N/A" when absent.
function itemTime(s) {
  if (s == null) return 'N/A';
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

// Gold rounded to the nearest hundred, shown as "13.7k" (or "N/A" when absent).
function goldK(g) {
  if (g == null) return 'N/A';
  return (Math.round(g / 100) * 100 / 1000).toFixed(1) + 'k';
}

// Just the calendar date portion of Oracle's "YYYY-MM-DD HH:MM:SS" string.
function matchDate(d) { return d ? String(d).split(' ')[0] : '—'; }

function renderMatchHistory() {
  const wrap = $('match-history');
  const matches = State.lastStats.matches || [];
  $('match-title').textContent = State.champion
    ? `Match history — ${State.champion}` : 'Match history';
  wrap.innerHTML = '';
  if (!matches.length) {
    wrap.innerHTML = '<div class="no-data">No matches in this timeframe.</div>';
    return;
  }
  matches.forEach((mt) => {
    const won = mt.result === 'Win';
    const card = document.createElement('div');
    card.className = 'match-card ' + (won ? 'win' : 'loss');
    const sideCls = (mt.side || '').toLowerCase() === 'blue' ? 'side-blue' : 'side-red';
    card.innerHTML =
      `<div class="match-summary" role="button" tabindex="0" aria-expanded="false">` +
        `<div class="match-header">` +
          `<span class="match-date">${matchDate(mt.date)}</span>` +
          `<span class="match-tournament">${mt.tournament || ''}</span>` +
          `<span class="match-chevron">▾</span>` +
        `</div>` +
        `<div class="match-top">` +
          `<span class="result-tag ${won ? 'win' : 'loss'}">${won ? 'W' : 'L'}</span>` +
          `<span class="side-badge ${sideCls}">${mt.side || '—'}</span>` +
          `<span class="match-teams">${mt.team || '—'} <em>vs</em> ${mt.opponent_team || '—'}</span>` +
        `</div>` +
        `<div class="match-mid">` +
          `<div class="match-champ">` +
            `<img src="${mt.image_url}" alt="${mt.champion || ''}" onerror="this.style.visibility='hidden'" />` +
            `<span>${mt.champion || '—'}</span>` +
          `</div>` +
          `<span class="match-vs">vs</span>` +
          `<div class="match-champ opp">` +
            `<img src="${mt.opponent_image_url}" alt="${mt.opponent_champion || ''}" onerror="this.style.visibility='hidden'" />` +
            `<span>${mt.opponent_champion || '—'}</span>` +
          `</div>` +
          `<span class="match-score">${mt.kills ?? 0} / ${mt.deaths ?? 0} / ${mt.assists ?? 0}</span>` +
        `</div>` +
        `<div class="match-items">Item timing — ` +
          `1st <b>${itemTime(mt.item1_completed_s)}</b> · ` +
          `2nd <b>${itemTime(mt.item2_completed_s)}</b> · ` +
          `3rd <b>${itemTime(mt.item3_completed_s)}</b></div>` +
      `</div>` +
      `<div class="match-detail hidden"></div>`;

    const summary = card.querySelector('.match-summary');
    const detail = card.querySelector('.match-detail');
    const toggle = () => toggleMatchDetail(mt, card, summary, detail);
    summary.addEventListener('click', toggle);
    summary.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
    wrap.appendChild(card);
  });
}

// Expand/collapse a match card, lazily loading the full scoreboard once.
async function toggleMatchDetail(mt, card, summary, detail) {
  const open = !detail.classList.contains('hidden');
  if (open) {
    detail.classList.add('hidden');
    summary.setAttribute('aria-expanded', 'false');
    card.classList.remove('expanded');
    return;
  }
  detail.classList.remove('hidden');
  summary.setAttribute('aria-expanded', 'true');
  card.classList.add('expanded');
  if (mt._detail === undefined) {
    detail.innerHTML = '<div class="md-loading">Loading game details…</div>';
    try {
      mt._detail = await Api.match(mt.gameid);
    } catch (e) {
      mt._detail = null;
    }
  }
  detail.innerHTML = mt._detail
    ? matchDetailHtml(mt._detail)
    : '<div class="no-data">Couldn’t load game details.</div>';
}

function matchDetailHtml(d) {
  return (d.teams || []).map((t) => {
    const won = t.result === 'Win';
    const sideCls = (t.side || '').toLowerCase() === 'blue' ? 'side-blue' : 'side-red';
    const me = (State.player || '').toLowerCase();
    const rows = (t.players || []).map((p) => {
      const isYou = p.playername && p.playername.toLowerCase() === me;
      return `<tr${isYou ? ' class="is-you"' : ''}>` +
        `<td class="md-champ">` +
          `<img src="${p.image_url}" alt="${p.champion || ''}" onerror="this.style.visibility='hidden'" />` +
          `<span class="md-names">` +
            `<span class="md-pname">${p.playername || '—'}</span>` +
            `<span class="md-cname">${p.champion || '—'}</span>` +
          `</span>` +
        `</td>` +
        `<td>${p.kills ?? 0} / ${p.deaths ?? 0} / ${p.assists ?? 0}</td>` +
        `<td>${p.cs ?? '—'}</td>` +
        `<td>${goldK(p.gold)}</td>` +
        `<td>${p.level == null ? 'N/A' : p.level}</td>` +
      `</tr>`;
    }).join('');
    return (
      `<div class="md-team ${sideCls}">` +
        `<div class="md-team-head">` +
          `<span class="result-tag ${won ? 'win' : 'loss'}">${won ? 'W' : 'L'}</span>` +
          `<span class="md-teamname">${t.teamname || t.side || '—'}</span>` +
          `<span class="md-obj">` +
            `Kills <b>${t.kills ?? 0}</b> · Towers <b>${t.towers ?? 0}</b> · ` +
            `Dragons <b>${t.dragons ?? 0}</b> · Barons <b>${t.barons ?? 0}</b></span>` +
        `</div>` +
        `<table class="md-board">` +
          `<thead><tr><th>Champion</th><th>K / D / A</th><th>CS</th><th>Gold</th><th>Lvl</th></tr></thead>` +
          `<tbody>${rows}</tbody>` +
        `</table>` +
      `</div>`);
  }).join('');
}

function renderChampDetail() {
  const sec = $('champ-detail');
  const d = State.lastStats;
  if (!State.champion || !d.selected_champion) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  const sc = d.selected_champion;
  $('champ-detail-title').innerHTML =
    `${sc.champion} (${sc.games} games) — player vs LCK ${d.role_label} avg vs player overall ` +
    `<button type="button" class="graph-link" data-champ="${sc.champion}">` +
    `View synergies &amp; counters →</button>`;
  $('champ-detail-title').querySelector('.graph-link').onclick =
    () => openChampion(sc.champion);

  const cols = [
    { label: `On ${sc.champion}`, cls: 'col-player' },
    { label: `LCK ${sc.champion} avg` },
    { label: 'Player overall' },
    { label: 'Δ vs LCK' },
  ];
  $('champ-table').innerHTML = cmpTable(cols, (m) => {
    const pv = sc[m.key];
    const cb = d.lck_champion_baseline ? d.lck_champion_baseline[m.key] : null;
    const ov = d.overall[m.key];
    return [
      `<td class="val-strong col-player">${fmt(pv, m)}</td>`,
      `<td>${fmt(cb, m)}</td>`,
      `<td>${fmt(ov, m)}</td>`,
      deltaCell(pv, cb, m),
    ];
  });
}

// ── Wire up ──────────────────────────────────────────────────────────────────
$('search-btn').onclick = doSearch;

const searchInput = $('search-input');
searchInput.addEventListener('input', () => showAutocomplete(searchInput.value));
searchInput.addEventListener('focus', () => showAutocomplete(searchInput.value));
searchInput.addEventListener('keydown', (e) => {
  switch (e.key) {
    case 'ArrowDown': e.preventDefault(); moveAcHighlight(+1); break;
    case 'ArrowUp':   e.preventDefault(); moveAcHighlight(-1); break;
    case 'Escape':    hideAutocomplete(); break;
    case 'Enter': {
      const items = $('ac-list').querySelectorAll('.ac-item');
      const it = (acIndex >= 0) ? items[acIndex] : null;
      if (it) chooseEntry({ kind: it.dataset.kind, key: it.dataset.key,
                            label: it.querySelector('.name').textContent });
      else doSearch();
      break;
    }
  }
});

// Close the dropdown when clicking anywhere outside the search box.
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-input-wrap')) hideAutocomplete();
});

function resetSearch() {
  $('search-input').value = '';
  hideAutocomplete();
  $('search-input').focus();
}

function goHome() {
  $('results').classList.add('hidden');
  $('group-view').classList.add('hidden');
  $('champion-view').classList.add('hidden');
  resetSearch();
}

$('home-btn').onclick = goHome;
$('champ-back').onclick = champBack;

initSearch();
