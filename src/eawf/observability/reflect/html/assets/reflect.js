/* Eä reflect pages: strip zoom and the session finder. Inlined; reads only its own page. */
(function () {
  'use strict';

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; });
  }

  // Zoom widens the strip inside its own horizontal viewport, never a nested vertical scroll.
  document.querySelectorAll('input[data-zoom]').forEach(function (input) {
    var target = document.getElementById(input.getAttribute('data-zoom'));
    if (!target) return;
    input.addEventListener('input', function () { target.style.width = (input.value * 100) + '%'; });
  });

  var source = document.getElementById('finder-data');
  if (!source) return;
  var data = JSON.parse(source.textContent);
  var rows = data.rows, size = data.page;
  var body = document.getElementById('finder-rows');
  var status = document.getElementById('finder-status');
  var search = document.getElementById('finder-q');
  var state = { q: '', filter: '', sort: 'c', dir: -1, page: 0 };

  function readHash() {
    var h = new URLSearchParams(location.hash.slice(1));
    state.q = h.get('q') || '';
    state.filter = h.get('f') || '';
    state.sort = h.get('s') || 'c';
    state.dir = h.get('d') === '1' ? 1 : -1;
    state.page = Math.max(0, parseInt(h.get('p') || '0', 10) || 0);
  }

  function writeHash() {
    var h = new URLSearchParams();
    if (state.q) h.set('q', state.q);
    if (state.filter) h.set('f', state.filter);
    if (state.sort !== 'c') h.set('s', state.sort);
    if (state.dir === 1) h.set('d', '1');
    if (state.page) h.set('p', String(state.page));
    history.replaceState(null, '', '#' + h.toString());
  }

  // Unknown values sort last in either direction.
  function compare(a, b) {
    var x = a[state.sort], y = b[state.sort];
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * state.dir;
  }

  function cell(value, absent) {
    return value === null ? '<span class="unk" title="' + esc(absent) + '">–</span>' : esc(value);
  }

  function rowHtml(r) {
    var badge = r.f ? '<span class="sv sv-' + r.sv + '">' + r.f + (r.f === 1 ? ' finding' : ' findings') + '</span>' : '<span class="faint">none</span>';
    return '<tr><td class="t stick"><a href="session-' + r.key + '.html">' + esc(r.t) + '</a><small>' + esc(r.s) + '</small></td>' +
      '<td>' + esc(r.dd) + '</td><td>' + esc(r.p) + '</td><td>' + esc(r.m) + '</td>' +
      '<td class="n">' + cell(r.cs, 'no verified rate for this model') + '</td>' +
      '<td class="n">' + esc(r.tks) + '</td><td>' + badge + '</td></tr>';
  }

  function visible() {
    var needle = state.q.toLowerCase();
    return rows.filter(function (r) {
      if (state.filter === 'must' && !r.must) return false;
      if (state.filter === 'has' && !r.f) return false;
      return !needle || (r.t + ' ' + r.s + ' ' + r.p + ' ' + r.m + ' ' + r.dd).toLowerCase().indexOf(needle) >= 0;
    }).sort(compare);
  }

  function render() {
    var found = visible();
    var pages = Math.max(1, Math.ceil(found.length / size));
    state.page = Math.min(state.page, pages - 1);
    var start = state.page * size, shown = found.slice(start, start + size);
    body.innerHTML = shown.length ? shown.map(rowHtml).join('') :
      '<tr><td colspan="7">No session matches these filters. <button class="btn" type="button" data-filter="reset">Reset</button></td></tr>';
    status.textContent = found.length ? (start + 1) + '-' + (start + shown.length) + ' of ' + found.length : '0 of ' + rows.length;
    document.querySelector('[data-page="prev"]').disabled = state.page === 0;
    document.querySelector('[data-page="next"]').disabled = state.page >= pages - 1;
    document.querySelectorAll('[data-filter]').forEach(function (b) {
      if (b.getAttribute('data-filter') !== 'reset') b.setAttribute('aria-pressed', String(b.getAttribute('data-filter') === state.filter));
    });
    document.querySelectorAll('th[data-sort]').forEach(function (th) {
      if (th.getAttribute('data-sort') === state.sort) th.setAttribute('aria-sort', state.dir === 1 ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
    });
    document.getElementById('finder-scope').textContent = state.filter === 'must' ? 'Must-see: the overview list' : 'sorted across every project';
    search.value = state.q;
  }

  function update() { writeHash(); render(); }

  document.addEventListener('click', function (event) {
    var el = event.target.closest('[data-filter],[data-page],th[data-sort] button');
    if (!el) return;
    if (el.hasAttribute('data-filter')) {
      var f = el.getAttribute('data-filter');
      if (f === 'reset') { state.q = ''; state.filter = ''; state.sort = 'c'; state.dir = -1; }
      else state.filter = state.filter === f ? '' : f;
      state.page = 0;
    } else if (el.hasAttribute('data-page')) {
      state.page += el.getAttribute('data-page') === 'next' ? 1 : -1;
    } else {
      var key = el.parentNode.getAttribute('data-sort');
      state.dir = state.sort === key ? -state.dir : -1;
      state.sort = key;
      state.page = 0;
    }
    update();
  });
  search.addEventListener('input', function () { state.q = search.value; state.page = 0; update(); });
  readHash();
  render();
}());
