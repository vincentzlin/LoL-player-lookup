// Thin fetch wrapper around the backend API.
const Api = {
  async _get(url) {
    let res;
    try {
      res = await fetch(url);
    } catch (_) {
      // fetch only rejects on network-level failures (server down, no connection).
      throw new Error('SERVER_UNREACHABLE');
    }
    if (!res.ok) {
      if (res.status >= 500) throw new Error('SERVER_UNREACHABLE');
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  },
  players() { return this._get('/api/players'); },
  teams() { return this._get('/api/teams'); },
  roles() { return this._get('/api/roles'); },
  champions() { return this._get('/api/champions'); },
  championGraph(name, { season, split, role } = {}) {
    const p = new URLSearchParams();
    if (season != null) p.set('season', season);
    if (split) p.set('split', split);
    if (role) p.set('role', role);
    const qs = p.toString();
    return this._get(`/api/champion/${encodeURIComponent(name)}/graph${qs ? '?' + qs : ''}`);
  },
  championPairing(name, { other, kind, season, split, role } = {}) {
    const p = new URLSearchParams();
    p.set('other', other);
    p.set('kind', kind);
    if (season != null) p.set('season', season);
    if (split) p.set('split', split);
    if (role) p.set('role', role);
    return this._get(`/api/champion/${encodeURIComponent(name)}/pairing?${p.toString()}`);
  },
  teamGroup(team) { return this._get(`/api/team/${encodeURIComponent(team)}`); },
  roleGroup(role) { return this._get(`/api/role/${encodeURIComponent(role)}`); },
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
