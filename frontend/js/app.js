// ── Metric definitions ───────────────────────────────────────────────────────
// dir: +1 = higher is better, -1 = lower is better (for delta coloring).
const METRICS = [
  { key: 'kills',    label: 'Kills / game',     dir: +1, dp: 1 },
  { key: 'deaths',   label: 'Deaths / game',    dir: -1, dp: 1 },
  { key: 'assists',  label: 'Assists / game',   dir: +1, dp: 1 },
  { key: 'cspm',     label: 'CS / min',         dir: +1, dp: 2 },
  { key: 'gpm',      label: 'Gold / min',       dir: +1, dp: 0 },
  { key: 'dpm',      label: 'Damage / min',     dir: +1, dp: 0 },
  { key: 'gold_pct', label: 'Gold %',           dir: +1, dp: 1, suffix: '%' },
  { key: 'dmg_pct',  label: 'Damage %',         dir: +1, dp: 1, suffix: '%' },
  { key: 'csd15',    label: 'CS diff @15',      dir: +1, dp: 1, signed: true },
  { key: 'gd15',     label: 'Gold diff @15',    dir: +1, dp: 0, signed: true },
];

const State = { player: null, role: null, season: null, split: null,
                filters: null, champion: null, lastStats: null };

const $ = (id) => document.getElementById(id);

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

// ── Search ───────────────────────────────────────────────────────────────────
async function initPlayers() {
  const players = await Api.players();
  const dl = $('player-list');
  const chips = $('player-chips');
  dl.innerHTML = '';
  chips.innerHTML = '';
  players.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.name;
    dl.appendChild(opt);

    const chip = document.createElement('button');
    chip.className = 'quick-chip';
    chip.innerHTML = `${p.name}<span>${p.role_label} · ${p.team}</span>`;
    chip.onclick = () => { $('search-input').value = p.name; doSearch(); };
    chips.appendChild(chip);
  });
}

async function doSearch() {
  const name = $('search-input').value.trim();
  const err = $('search-error');
  err.classList.add('hidden');
  if (!name) return;
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
    $('results').classList.remove('hidden');
    $('search-error').classList.add('hidden');
  } catch (e) {
    err.textContent = `"${name}" isn't searchable. Try: Teddy, Ruler, Kiin or Zeus.`;
    err.classList.remove('hidden');
    $('results').classList.add('hidden');
  } finally {
    hideLoading();
  }
}

// ── Stats loading ────────────────────────────────────────────────────────────
async function loadStats() {
  const data = await Api.stats(State.player, {
    season: State.season, split: State.split, champion: State.champion,
  });
  State.role = data.role;
  State.lastStats = data;
  return data;
}

// ── Rendering ────────────────────────────────────────────────────────────────
function renderShell() {
  const d = State.lastStats;
  $('player-name').textContent = State.player;
  $('player-meta').innerHTML =
    `<span class="tag tag-LCK">LCK</span>` +
    `<span class="tag tag-role">${d.role_label}</span>`;
  renderSeasonRow();
  renderSplitRow();
  renderOverall();
  renderChampGrid();
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
    card.className = 'champ-card' + (c.champion === State.champion ? ' active' : '');
    card.innerHTML =
      `<img src="${c.image_url}" alt="${c.champion}" ` +
      `onerror="this.style.visibility='hidden'" />` +
      `<div class="champ-card-name">${c.champion}</div>` +
      `<div class="champ-card-games">${c.games} games</div>`;
    card.onclick = async () => {
      State.champion = (State.champion === c.champion) ? null : c.champion;
      showLoading(); await loadStats(); renderChampGrid(); renderChampDetail(); hideLoading();
    };
    grid.appendChild(card);
  });
}

function renderChampDetail() {
  const sec = $('champ-detail');
  const d = State.lastStats;
  if (!State.champion || !d.selected_champion) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  const sc = d.selected_champion;
  $('champ-detail-title').textContent =
    `${sc.champion} (${sc.games} games) — player vs LCK ${d.role_label} avg vs player overall`;

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
$('search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
$('back-btn').onclick = () => {
  $('results').classList.add('hidden');
  $('search-input').value = '';
  $('search-input').focus();
};

initPlayers();
