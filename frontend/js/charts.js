// ── Tiny SVG charting module (no dependencies) ───────────────────────────────
// Renders the champion-page visualisations directly as inline SVG so they scale
// inside the existing `.cmp-wrap` containers and match the dark theme. Each entry
// point clears its host element and draws into it; data shapes come straight from
// the `/api/champion/{name}/graph` response (see renderChampionGraph in app.js).
const Charts = (function () {
  // Hex mirrors the CSS variables in style.css (--accent, --purple, …).
  const C = {
    accent: '#58a6ff', purple: '#a371f7', gold: '#d29922',
    win: '#3fb950', loss: '#f85149',
    border: '#30363d', muted: '#8b949e', text: '#e6edf3', bg: '#0d1117',
  };
  const SVGNS = 'http://www.w3.org/2000/svg';

  // Shared geometry for every chart (viewBox units; CSS scales to container width).
  const W = 640, H = 280;
  const PAD = { l: 46, r: 18, t: 18, b: 40 };
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;

  function svgEl(tag, attrs) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) {
      if (attrs[k] !== '' && attrs[k] != null) e.setAttribute(k, attrs[k]);
    }
    return e;
  }
  function text(x, y, str, attrs) {
    const t = svgEl('text', Object.assign(
      { x, y, fill: C.muted, 'font-size': 11 }, attrs));
    t.textContent = str;
    return t;
  }
  function newSvg() {
    return svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`, class: 'chart-svg',
      preserveAspectRatio: 'xMidYMid meet', role: 'img',
    });
  }
  function mount(elId) {
    const host = document.getElementById(elId);
    if (host) host.innerHTML = '';
    return host;
  }
  function noData(host, msg) {
    host.innerHTML = `<div class="no-data">${msg}</div>`;
  }

  // ── Shared tooltip ─────────────────────────────────────────────────────────
  let tipEl = null;
  function tip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'chart-tip hidden';
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }
  function attachTip(node, html) {
    node.addEventListener('mouseenter', () => {
      const t = tip(); t.innerHTML = html; t.classList.remove('hidden');
    });
    node.addEventListener('mousemove', (e) => {
      const t = tip();
      t.style.left = (e.clientX + 14) + 'px';
      t.style.top = (e.clientY + 14) + 'px';
    });
    node.addEventListener('mouseleave', () => tip().classList.add('hidden'));
  }

  // ── Shared bits for the two win-rate charts (y = 0–100%) ─────────────────────
  const yWR = (v) => PAD.t + plotH * (1 - v / 100);
  const catX = (i, n) => PAD.l + (plotW / n) * (i + 0.5); // evenly spaced categories

  function wrGrid(svg) {
    [0, 25, 50, 75, 100].forEach((v) => {
      const y = yWR(v);
      const mid = v === 50;
      svg.appendChild(svgEl('line', {
        x1: PAD.l, y1: y, x2: W - PAD.r, y2: y,
        stroke: C.border, 'stroke-width': 1,
        'stroke-dasharray': mid ? '4 4' : '', opacity: mid ? 0.9 : 0.5,
      }));
      svg.appendChild(text(PAD.l - 8, y + 4, v + '%', { 'text-anchor': 'end' }));
    });
  }
  function xTitle(svg, str) {
    svg.appendChild(text(PAD.l + plotW / 2, H - 5, str, { 'text-anchor': 'middle' }));
  }
  // Small legend in the top-right, laid out right-to-left.
  function legend(svg, items) {
    let x = W - PAD.r;
    const y = PAD.t - 4;
    items.slice().reverse().forEach((it) => {
      x -= it.label.length * 6.4 + 26;
      svg.appendChild(svgEl('line', {
        x1: x, y1: y, x2: x + 16, y2: y, stroke: it.color, 'stroke-width': 2,
        'stroke-dasharray': it.dash || '',
      }));
      svg.appendChild(svgEl('circle', {
        cx: x + 8, cy: y, r: 3, fill: it.fill === false ? C.bg : it.color,
        stroke: it.color, 'stroke-width': it.fill === false ? 2 : 0,
      }));
      svg.appendChild(text(x + 21, y + 4, it.label, { 'text-anchor': 'start', 'font-size': 10 }));
    });
  }

  const pct = (v) => (v == null ? '—' : v.toFixed(1) + '%');

  // ── Side stats panel (restores the per-row numbers the old tables showed) ────
  // headRow: array of column headers; rows: array of cell-string arrays (first
  // cell = left-aligned label). Reuses the existing table.cmp styling.
  function sideTable(headRow, rows) {
    const head = headRow.map((h) => `<th>${h}</th>`).join('');
    const body = rows.map((cells) =>
      '<tr>' + cells.map((c, i) =>
        i === 0 ? `<td>${c}</td>` : `<td class="val-strong">${c}</td>`).join('') + '</tr>'
    ).join('');
    return `<table class="cmp chart-side-table"><thead><tr>${head}</tr></thead>`
      + `<tbody>${body}</tbody></table>`;
  }
  // Lay out the chart on the left; a "Show more" toggle on the right reveals the
  // per-row stats as a collapsible side panel. chartNode may be the <svg> or any
  // fallback node (e.g. a no-data message). Collapsed by default.
  function renderRow(host, chartNode, sideHTML) {
    host.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'chart-row';
    row.appendChild(chartNode);

    const aside = document.createElement('div');
    aside.className = 'chart-aside';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chart-toggle';
    btn.textContent = 'Show more';
    btn.setAttribute('aria-expanded', 'false');
    const side = document.createElement('div');
    side.className = 'chart-side hidden';
    side.innerHTML = sideHTML;
    btn.addEventListener('click', () => {
      const open = row.classList.toggle('expanded');
      side.classList.toggle('hidden', !open);
      btn.textContent = open ? 'Show less' : 'Show more';
      btn.setAttribute('aria-expanded', String(open));
    });
    aside.append(btn, side);
    row.appendChild(aside);
    host.appendChild(row);
  }

  // ── Line chart: win rate by game length ─────────────────────────────────────
  function lineChart(elId, points) {
    const host = mount(elId);
    if (!host) return;
    if (!points.length) return noData(host, 'No games in this timeframe.');

    const svg = newSvg();
    wrGrid(svg);
    const n = points.length;
    const series = [
      { key: 'win_rate', color: C.accent, dash: '' },
      { key: 'adjusted_win_rate', color: C.purple, dash: '5 4' },
    ];

    // Polylines across the points that have a value for each series.
    series.forEach((s) => {
      const valid = points
        .map((p, i) => ({ x: catX(i, n), y: p[s.key] == null ? null : yWR(p[s.key]) }))
        .filter((p) => p.y != null);
      if (valid.length > 1) {
        svg.appendChild(svgEl('polyline', {
          points: valid.map((p) => `${p.x},${p.y}`).join(' '),
          fill: 'none', stroke: s.color, 'stroke-width': 2,
          'stroke-dasharray': s.dash, 'stroke-linejoin': 'round',
        }));
      }
    });

    // X labels + hoverable dots (one per series, both showing the full row).
    points.forEach((p, i) => {
      const x = catX(i, n);
      svg.appendChild(text(x, H - PAD.b + 18, `>${p.min_minutes}m`, { 'text-anchor': 'middle' }));
      const html = `<strong>&gt; ${p.min_minutes} min</strong><br>${p.games} games`
        + `<br><span style="color:${C.accent}">Win ${pct(p.win_rate)}</span>`
        + `<br><span style="color:${C.purple}">Adj ${pct(p.adjusted_win_rate)}</span>`;
      series.forEach((s) => {
        if (p[s.key] == null) return;
        const dot = svgEl('circle', {
          cx: x, cy: yWR(p[s.key]), r: 4.5, fill: s.color,
          stroke: C.bg, 'stroke-width': 1.5, class: 'chart-dot',
        });
        attachTip(dot, html);
        svg.appendChild(dot);
      });
    });

    xTitle(svg, 'Game length');
    legend(svg, [
      { color: C.accent, label: 'Win Rate' },
      { color: C.purple, label: 'Adjusted', dash: '5 4' },
    ]);
    const side = sideTable(['Game length', 'Games', 'Win', 'Adj'],
      points.map((p) => [`> ${p.min_minutes} min`, `${p.games}`,
        pct(p.win_rate), pct(p.adjusted_win_rate)]));
    renderRow(host, svg, side);
  }

  // ── Ordered lollipop chart: win rate by dragons slain ───────────────────────
  function lollipopChart(elId, points) {
    const host = mount(elId);
    if (!host) return;
    if (!points.length) return noData(host, 'No games in this timeframe.');

    const svg = newSvg();
    wrGrid(svg);
    const n = points.length;
    const baseY = yWR(0);

    points.forEach((p, i) => {
      const x = catX(i, n);
      svg.appendChild(text(x, H - PAD.b + 18, p.bucket, { 'text-anchor': 'middle' }));
      if (p.win_rate == null) return;

      const html = `<strong>${p.bucket} dragons</strong><br>${p.games} games`
        + `<br><span style="color:${C.accent}">Win ${pct(p.win_rate)}</span>`
        + `<br><span style="color:${C.purple}">Adj ${pct(p.adjusted_win_rate)}</span>`;

      // Stem from baseline up to the raw win-rate head.
      svg.appendChild(svgEl('line', {
        x1: x, y1: baseY, x2: x, y2: yWR(p.win_rate),
        stroke: C.accent, 'stroke-width': 2, opacity: 0.55,
      }));
      // Adjusted WR as a smaller hollow marker.
      if (p.adjusted_win_rate != null) {
        const adj = svgEl('circle', {
          cx: x, cy: yWR(p.adjusted_win_rate), r: 4, fill: C.bg,
          stroke: C.purple, 'stroke-width': 2, class: 'chart-dot',
        });
        attachTip(adj, html);
        svg.appendChild(adj);
      }
      // Raw WR head (drawn last so it sits on top for hover).
      const head = svgEl('circle', {
        cx: x, cy: yWR(p.win_rate), r: 6, fill: C.accent,
        stroke: C.bg, 'stroke-width': 1.5, class: 'chart-dot',
      });
      attachTip(head, html);
      svg.appendChild(head);
    });

    xTitle(svg, 'Dragons slain');
    legend(svg, [
      { color: C.accent, label: 'Win Rate' },
      { color: C.purple, label: 'Adjusted', fill: false },
    ]);
    const side = sideTable(['Dragons', 'Games', 'Win', 'Adj'],
      points.map((p) => [p.bucket, `${p.games}`,
        pct(p.win_rate), pct(p.adjusted_win_rate)]));
    renderRow(host, svg, side);
  }

  // ── Scatter: When Ahead (gold lead = Y, xp lead = X) ────────────────────────
  const MIN_COLORS = [C.accent, C.gold, C.win, C.purple];

  function signed(v, capped, unit) {
    if (v == null) return 'N/A';
    const r = Math.round(v);
    const sign = r > 0 ? '+' : (r < 0 ? '−' : '');
    return sign + Math.abs(r) + (capped ? '+' : '') + unit;
  }

  function scatterChart(elId, points) {
    const host = mount(elId);
    if (!host) return;
    const all = points || [];
    if (!all.length) return noData(host, 'Not enough games to estimate.');

    // Side table shows every checkpoint (N/A for nulls), matching the old table.
    const side = sideTable(['Min', 'Gold', 'XP', 'Team'],
      all.map((p) => [`@ ${p.minute} min`,
        signed(p.break_even_gold, p.break_even_gold_capped, ' g'),
        signed(p.break_even_xp, p.break_even_xp_capped, ' xp'),
        signed(p.break_even_team_gold, p.break_even_team_gold_capped, ' g')]));

    // A scatter dot needs both coords; if none qualify, keep the table and show
    // a note where the chart would be (so the section stays in the uniform grid).
    const pts = all.filter(
      (p) => p.break_even_xp != null && p.break_even_gold != null);
    if (!pts.length) {
      const note = document.createElement('div');
      note.className = 'no-data';
      note.textContent = 'Not enough games to plot.';
      return renderRow(host, note, side);
    }

    const svg = newSvg();
    // Symmetric domains around 0 so the zero crosshair sits centred.
    const maxX = Math.max(50, ...pts.map((p) => Math.abs(p.break_even_xp))) * 1.15;
    const maxY = Math.max(50, ...pts.map((p) => Math.abs(p.break_even_gold))) * 1.15;
    const sx = (v) => PAD.l + plotW * (v + maxX) / (2 * maxX);
    const sy = (v) => PAD.t + plotH * (1 - (v + maxY) / (2 * maxY));

    // Plot border + zero crosshair lines.
    svg.appendChild(svgEl('rect', {
      x: PAD.l, y: PAD.t, width: plotW, height: plotH,
      fill: 'none', stroke: C.border, 'stroke-width': 1, opacity: 0.5,
    }));
    svg.appendChild(svgEl('line', {
      x1: PAD.l, y1: sy(0), x2: W - PAD.r, y2: sy(0),
      stroke: C.border, 'stroke-width': 1,
    }));
    svg.appendChild(svgEl('line', {
      x1: sx(0), y1: PAD.t, x2: sx(0), y2: PAD.t + plotH,
      stroke: C.border, 'stroke-width': 1,
    }));

    // Y (gold) labels: top / 0 / bottom.
    svg.appendChild(text(PAD.l - 8, PAD.t + 10, '+' + Math.round(maxY), { 'text-anchor': 'end' }));
    svg.appendChild(text(PAD.l - 8, sy(0) + 4, '0', { 'text-anchor': 'end' }));
    svg.appendChild(text(PAD.l - 8, PAD.t + plotH, '−' + Math.round(maxY), { 'text-anchor': 'end' }));
    // X (xp) labels along the bottom.
    svg.appendChild(text(PAD.l, H - PAD.b + 18, '−' + Math.round(maxX), { 'text-anchor': 'start' }));
    svg.appendChild(text(sx(0), H - PAD.b + 18, '0', { 'text-anchor': 'middle' }));
    svg.appendChild(text(W - PAD.r, H - PAD.b + 18, '+' + Math.round(maxX), { 'text-anchor': 'end' }));

    pts.forEach((p, i) => {
      const x = sx(p.break_even_xp), y = sy(p.break_even_gold);
      const col = MIN_COLORS[i % MIN_COLORS.length];
      const dot = svgEl('circle', {
        cx: x, cy: y, r: 6, fill: col, stroke: C.bg, 'stroke-width': 1.5,
        class: 'chart-dot',
      });
      attachTip(dot, `<strong>@ ${p.minute} min</strong>`
        + `<br>Gold ${signed(p.break_even_gold, p.break_even_gold_capped, ' g')}`
        + `<br>XP ${signed(p.break_even_xp, p.break_even_xp_capped, ' xp')}`
        + `<br>Team ${signed(p.break_even_team_gold, p.break_even_team_gold_capped, ' g')}`);
      svg.appendChild(dot);
      svg.appendChild(text(x + 9, y + 4, p.minute + 'm', { 'text-anchor': 'start', fill: C.text }));
    });

    xTitle(svg, 'XP lead');
    svg.appendChild(text(0, 0, 'Gold lead', {
      'text-anchor': 'middle', transform: `translate(13 ${PAD.t + plotH / 2}) rotate(-90)`,
    }));
    renderRow(host, svg, side);
  }

  return { lineChart, lollipopChart, scatterChart };
})();
