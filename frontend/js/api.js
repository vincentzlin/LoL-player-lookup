// Thin fetch wrapper around the backend API.
const Api = {
  async _get(url) {
    const res = await fetch(url);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  },
  players() { return this._get('/api/players'); },
  match(gameid) { return this._get(`/api/match/${encodeURIComponent(gameid)}`); },
  filters(name) { return this._get(`/api/player/${encodeURIComponent(name)}/filters`); },
  stats(name, { season, split, champion } = {}) {
    const p = new URLSearchParams();
    if (season != null) p.set('season', season);
    if (split) p.set('split', split);
    if (champion) p.set('champion', champion);
    const qs = p.toString();
    return this._get(`/api/player/${encodeURIComponent(name)}/stats${qs ? '?' + qs : ''}`);
  },
};
