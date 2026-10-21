"""`btlog web`: a read-only dashboard over the database, standard library only.

One page: the devices on the left, and for the selected one (or all of them)
presence by hour of day and by weekday, a day-by-hour heatmap, signal strength
and sensor readings per day, and the new devices of the week. The same data is
served as JSON under /api/. Device names come from strangers' advertisements,
so the page escapes everything it puts into HTML.

The server opens the database read-only, once per request: SQLite connections
are per thread and ThreadingHTTPServer gives every request its own. There is no
authentication; keep it on localhost or a trusted network. Requests whose Host
header is a DNS name other than the bind address are refused, so a web page
cannot rebind its own name to 127.0.0.1 and read the API from the browser.
"""

import ipaddress
import json
import logging
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from btlog import trends, when
from btlog.store import Store, public

log = logging.getLogger("btlog")

PAGE ="""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>btlog</title>
<style>
  :root { color-scheme: dark; --bg: #14171c; --fg: #d8dce3; --dim: #7d8590; --bar: #4c9be8; --sel: #263040; --line: #2a303a; }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; }
  header { padding: 10px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  header b { font-size: 16px; } header span { color: var(--dim); }
  main { display: grid; grid-template-columns: minmax(280px, 1fr) 2fr; gap: 0; min-height: calc(100vh - 44px); }
  @media (max-width: 800px) { main { grid-template-columns: 1fr; } }
  #list { border-right: 1px solid var(--line); overflow: auto; max-height: calc(100vh - 44px); }
  #list div { padding: 6px 16px; cursor: pointer; border-bottom: 1px solid var(--line); }
  #list div:hover { background: #1c2129; } #list div.sel { background: var(--sel); }
  #list small, .dim { color: var(--dim); display: block; }
  .mono { font-family: ui-monospace, monospace; }
  section { padding: 12px 16px; } h2 { font-size: 14px; margin: 14px 0 6px; color: var(--dim); font-weight: normal; }
  .chart { display: flex; align-items: flex-end; gap: 2px; height: 90px; }
  .chart div { flex: 1; background: var(--bar); min-width: 4px; position: relative; }
  .chart div.none { background: var(--line); height: 2px !important; }
  .chart div:hover::after { content: attr(title); position: absolute; bottom: 100%; left: 0; background: var(--sel); padding: 2px 6px; white-space: nowrap; font-size: 12px; }
  .labels { display: flex; gap: 2px; font-size: 11px; color: var(--dim); } .labels span { flex: 1; text-align: center; min-width: 4px; }
  input { background: var(--sel); color: var(--fg); border: 1px solid var(--line); padding: 4px 8px; border-radius: 4px; }
  table { border-collapse: collapse; } td { padding: 2px 10px 2px 0; }
  .heat { display: grid; grid-template-columns: 5em repeat(24, 1fr); gap: 2px; font-size: 11px; color: var(--dim); }
  .heat i { display: block; height: 14px; background: var(--bar); border-radius: 2px; }
  .heat i.none { background: var(--line); opacity: 1; }
  .heat span { text-align: center; }
</style>
<header><b>btlog</b><span id="status">loading</span>
  <label>since <input id="since" value="30d" size="8"></label></header>
<main>
  <div id="list"></div>
  <section>
    <div id="title">All devices</div>
    <h2 id="hourstitle">Presence by hour of day</h2><div class="chart" id="hours"></div><div class="labels" id="hourlabels"></div>
    <h2>By weekday</h2><div class="chart" id="days"></div><div class="labels" id="daylabels"></div>
    <h2 id="heattitle">Day by hour</h2><div class="heat" id="heat"></div>
    <div id="rssiwrap" hidden><h2>Signal per day (average dBm)</h2><div class="chart" id="rssi"></div><div class="labels" id="rssilabels"></div></div>
    <div id="readings" hidden></div>
    <h2>New devices in the last 7 days</h2><div id="new" class="mono"></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
// Names and labels come from BLE advertisements, i.e. from strangers: never raw HTML.
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = (path, q) => fetch(path + '?' + new URLSearchParams(q)).then(r => r.ok ? r.json() : r.text().then(t => { throw t; }));
const name = d => esc(d.label || d.name || (d.vendor && d.vendor + ' device') || '');
let selected = null;
const WD = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function chart(el, labels, values, fmt, lo, hi) {
  const vals = values.filter(v => v != null);
  lo = lo ?? 0; hi = hi ?? vals.reduce((m, v) => Math.max(m, v), 1e-9);
  el.chart.innerHTML = values.map((v, i) => v == null ? `<div class="none" title="${labels[i]}: no data"></div>`
    : `<div style="height:${Math.min(100, Math.max(2, (v - lo) / (hi - lo) * 100))}%" title="${labels[i]}: ${fmt(v, i)}"></div>`).join('');
  el.labels.innerHTML = labels.map((l, i) => `<span>${labels.length > 12 && i % 2 ? '' : l}</span>`).join('');
}
const pct = v => Math.round(v * 100) + '%', num = v => v.toFixed(1);
// Every calendar day from the first to the last, so a day the scanner was off
// shows as a row of gaps instead of vanishing. Noon keeps the step clear of DST.
function allDays(days) {
  const keys = [...days.keys()].sort(); if (!keys.length) return days;
  const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const out = new Map();
  for (let d = new Date(keys[0] + 'T12:00:00'); ymd(d) <= keys[keys.length - 1]; d.setDate(d.getDate() + 1)) out.set(ymd(d), days.get(ymd(d)) || Array(24).fill(null));
  return out;
}
function heat(rows, fmt) {
  const seen = new Map();
  rows.forEach(r => { if (!seen.has(r.day)) seen.set(r.day, Array(24).fill(null)); seen.get(r.day)[r.hour] = r.presence; });
  const days = allDays(seen);
  const hi = selected ? 1 : rows.reduce((m, r) => Math.max(m, r.presence), 1e-9);
  let html = '<span></span>' + [...Array(24).keys()].map(h => `<span>${h % 3 ? '' : String(h).padStart(2, '0')}</span>`).join('');
  for (const [day, cells] of days) html += `<span>${day.slice(5)}</span>` + cells.map((v, h) => v == null
    ? `<i class="none" title="${day} ${h}h: scanner off"></i>` : `<i style="opacity:${0.08 + 0.92 * v / hi}" title="${day} ${h}h: ${fmt(v)}"></i>`).join('');
  $('#heat').innerHTML = html;
  $('#heattitle').textContent = selected ? 'Day by hour (share of scan cycles)' : `Day by hour (devices per scan cycle, full = ${hi.toFixed(1)})`;
}
async function load() {
  const q = { since: $('#since').value }, dev = selected ? { ...q, device: selected } : q;
  try {
    const [hours, days, rows] = await Promise.all([api('/api/hours', dev), api('/api/days', dev), api('/api/timeline', dev)]);
    const fmt = selected ? pct : num;
    // A device's presence is a share, drawn against 100%; counts against their maximum.
    const hi = selected ? 1 : undefined;
    chart({ chart: $('#hours'), labels: $('#hourlabels') }, [...Array(24).keys()].map(h => String(h).padStart(2, '0')), hours, fmt, 0, hi);
    chart({ chart: $('#days'), labels: $('#daylabels') }, WD, days, fmt, 0, hi);
    heat(rows, fmt);
    $('#hourstitle').textContent = selected ? 'Presence by hour of day (share of scan cycles)' : 'Devices heard per scan cycle, by hour of day';
    let rd = [];
    if (selected) {
      let r;
      [r, rd] = await Promise.all([api('/api/rssi', dev), api('/api/readings', dev)]);
      chart({ chart: $('#rssi'), labels: $('#rssilabels') }, r.map(x => x.when.slice(5)), r.map(x => x.avg), v => v.toFixed(1) + ' dBm', -100, -30);
      readings(rd);
    }
    $('#rssiwrap').hidden = !selected;
    $('#readings').hidden = !rd.length;
  } catch (e) { $('#status').textContent = 'error: ' + e; }
}
// One chart per kind, each spanning its own lowest to highest value: from zero,
// a day at 19 °C and one at 23 °C would draw the same bar. A flat series (a
// battery at 94 % all week) gets full bars rather than empty-looking ones.
function readings(rows) {
  const kinds = [...new Set(rows.map(r => r.kind))];
  $('#readings').innerHTML = kinds.map((k, i) => `<h2>Readings: ${esc(k)} per day (average ${esc(rows.find(r => r.kind === k).unit)}, min / avg / max on hover)</h2>
    <div class="chart" id="rk${i}"></div><div class="labels" id="rl${i}"></div>`).join('');
  kinds.forEach((k, i) => {
    const rs = rows.filter(r => r.kind === k), lo = Math.min(...rs.map(r => r.min)), hi = Math.max(...rs.map(r => r.max));
    chart({ chart: $('#rk' + i), labels: $('#rl' + i) }, rs.map(r => esc(r.when.slice(5))), rs.map(r => r.avg),
      (v, j) => esc(`${rs[j].min.toFixed(2)} / ${v.toFixed(2)} / ${rs[j].max.toFixed(2)} ${rs[j].unit}`), lo < hi ? lo : lo - 1, hi);
  });
}
async function devices() {
  const list = await api('/api/devices', {}), st = await api('/api/status', {});
  $('#status').textContent = `${st.devices} devices (+${st.private} rotating private), ${st.cycles} scan cycles since ${st.first_day || '-'}, last ${st.last_cycle ? new Date(st.last_cycle * 1000).toLocaleString() : '-'}`;
  const all = `<div class="${selected ? '' : 'sel'}" data-a="">All devices<small>average per scan cycle</small></div>`;
  $('#list').innerHTML = all + list.map(d => `<div class="${selected === d.address ? 'sel' : ''}" data-a="${esc(d.address)}" data-t="${esc(d.address)} ${name(d)}">
    <span class="mono">${esc(d.address)}</span> ${name(d)}<small>${esc(d.kind)}${d.vendor ? ', ' + esc(d.vendor) : ''}, ${d.hours} h, last ${new Date(d.last_seen * 1000).toLocaleString()}</small></div>`).join('');
  const fresh = await api('/api/new', { since: '7d' });
  $('#new').innerHTML = fresh.length ? '<table>' + fresh.map(d => `<tr><td>${esc(d.address)}</td><td>${name(d)}</td><td class="dim">${new Date(d.first_seen * 1000).toLocaleString()}</td></tr>`).join('') + '</table>' : '<span class="dim">none</span>';
}
$('#list').addEventListener('click', e => {
  const row = e.target.closest('[data-a]'); if (!row) return;
  selected = row.dataset.a || null;
  $('#title').textContent = selected ? row.dataset.t : 'All devices';
  document.querySelectorAll('#list div').forEach(d => d.classList.toggle('sel', (d.dataset.a || null) === selected));
  load();
});
$('#since').addEventListener('change', load);
const refresh = () => devices().catch(e => { $('#status').textContent = 'error: ' + e; }).then(load);
refresh();
setInterval(refresh, 60000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    db = None
    names = frozenset()  # see host_names()
    server_version = "btlog"
    timeout = 10  # a client that connects and says nothing does not pin a thread

    def log_message(self, *args):
        pass

    def host_ok(self):
        """True for a Host a browser sends when the user typed the address:
        an IP literal or one of the names from host_names(). A rebinding page
        arrives under its own DNS name, which is none of those."""
        host = self.headers.get("Host", "")
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        if name.lower().rstrip(".") in self.names:
            return True
        try:
            ipaddress.ip_address(name.strip("[]"))
            return True
        except ValueError:
            return False

    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if not self.host_ok():
                return self.error(HTTPStatus.MISDIRECTED_REQUEST, "use the address the dashboard is bound to")
            if url.path == "/":
                return self.reply(PAGE.encode(), "text/html; charset=utf-8")
            with Store(self.db, readonly=True) as store:
                data = self.api(store, url.path, q)
            if data is None:
                return self.error(HTTPStatus.NOT_FOUND, "not found")
            self.reply(json.dumps(data).encode(), "application/json")
        except (ValueError, LookupError) as e:
            self.error(HTTPStatus.BAD_REQUEST, f"bad request: {e}")
        except Exception as e:  # noqa: BLE001 - a locked or missing database, absurd input
            log.error("%s %s: %s", self.command, self.path, e)
            self.error(HTTPStatus.INTERNAL_SERVER_ERROR, f"error: {e}")

    def api(self, store, path, q):
        since = when.parse(q.get("since", "30d"))
        until = when.parse(q["until"]) if q.get("until") else None
        did = store.find(q["device"])["id"] if q.get("device") else None
        if path == "/api/status":
            return store.status(sightings=False)  # counting every sighting is CLI-only work
        if path == "/api/devices":
            return [public(d) for d in store.devices(private=q.get("private") == "1")]
        if path == "/api/hours":
            return trends.hours(store, did, since, until)
        if path == "/api/days":
            return trends.weekdays(store, did, since, until)
        if path == "/api/timeline":
            return trends.timeline(store, did, since, until)
        if path == "/api/rssi":
            if did is None:
                raise ValueError("device required")
            return trends.rssi(store, did, since, until, by=q.get("by", "day"))
        if path == "/api/readings":
            if did is None:
                raise ValueError("device required")
            return trends.readings(store, did, q.get("kind") or None, since, until, by=q.get("by", "day"))
        if path == "/api/now":
            return store.now()
        if path == "/api/new":
            if since is None:
                raise ValueError("since required")
            return [public(d) for d in trends.new(store, since, until)]
        return None

    def reply(self, body, ctype, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, text):
        self.reply(text.encode(), "text/plain; charset=utf-8", status)


def host_names(bind_host, extra=()):
    """The names the dashboard answers to besides IP literals: localhost, the
    bind host and the extra ones from web_names (a Tailscale MagicDNS name,
    say). Listening beyond loopback, it also answers to this machine's own
    names: hostname, FQDN and hostname.local for mDNS. Exact names only:
    accepting any name that resolves to this machine would let a rebinding
    page back in, since its owner decides what it resolves to."""
    names = {"localhost", bind_host, *extra}
    try:
        loopback = bind_host == "localhost" or ipaddress.ip_address(bind_host.strip("[]")).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        short = socket.gethostname()
        names |= {short, socket.getfqdn(), short.split(".")[0] + ".local"}
    return frozenset(n.lower().rstrip(".") for n in names if n)


def make_server(db, bind, names=()):
    host, _, port = bind.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"bind must be HOST:PORT, not {bind!r}")
    handler = type("Handler", (Handler,), {"db": db, "names": host_names(host, names)})
    return ThreadingHTTPServer((host, int(port)), handler)


def serve(db, bind, names=()):
    srv = make_server(db, bind, names)
    print(f"btlog web on http://{srv.server_address[0]}:{srv.server_address[1]} (read-only, no authentication)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
