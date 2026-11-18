/* FinCore — Ctrl+K command palette (search + actions). */
(function () {
  'use strict';
  var backdrop = document.getElementById('paletteBackdrop');
  var palette = document.getElementById('palette');
  var input = document.getElementById('paletteInput');
  var results = document.getElementById('paletteResults');
  if (!backdrop || !palette || !input || !results) return;

  var activeIndex = -1;
  var debounceTimer = null;
  var abortCtrl = null;
  var lastQuery = null;

  function isOpen() { return palette.classList.contains('open'); }

  function openPalette() {
    if (isOpen()) { closePalette(); return; }
    backdrop.hidden = false;
    palette.hidden = false;
    // force reflow so the open transition plays
    void palette.offsetWidth;
    backdrop.classList.add('open');
    palette.classList.add('open');
    document.body.style.overflow = 'hidden';
    input.focus();
    if (lastQuery === null) runSearch('');
  }
  function closePalette() {
    backdrop.classList.remove('open');
    palette.classList.remove('open');
    document.body.style.overflow = '';
    setTimeout(function () {
      if (!isOpen()) { backdrop.hidden = true; palette.hidden = true; }
    }, 200);
  }

  function clearActive() {
    results.querySelectorAll('.pal-item.active').forEach(function (el) {
      el.classList.remove('active');
    });
    activeIndex = -1;
  }
  function flatItems() {
    return Array.prototype.slice.call(results.querySelectorAll('.pal-item'));
  }
  function setActive(index) {
    var items = flatItems();
    if (!items.length) { activeIndex = -1; return; }
    activeIndex = (index + items.length) % items.length;
    items.forEach(function (el, i) {
      el.classList.toggle('active', i === activeIndex);
      if (i === activeIndex) {
        el.scrollIntoView({ block: 'nearest' });
      }
    });
  }
  function move(delta) {
    var items = flatItems();
    if (!items.length) return;
    if (activeIndex === -1) {
      setActive(delta > 0 ? 0 : items.length - 1);
    } else {
      setActive(activeIndex + delta);
    }
  }

  function execute(el) {
    if (!el) return;
    var action = el.getAttribute('data-action');
    var question = el.getAttribute('data-question');
    var url = el.getAttribute('data-url');
    closePalette();
    if (action === 'assistant' && question) {
      window.location.href = '/assistant/?q=' + encodeURIComponent(question) + '&auto=1';
      return;
    }
    if (url) window.location.href = url;
  }

  function toneClass(tone) {
    return {blue: 'blue', amber: 'amber', violet: 'violet',
            green: 'green', slate: 'slate'}[tone] || 'slate';
  }

  function renderGroups(groups, q) {
    results.innerHTML = '';
    if (!groups || !groups.length) {
      var empty = document.createElement('div');
      empty.className = 'pal-empty';
      empty.innerHTML = '<i class="fa-solid fa-circle-question"></i>'
        + '<p></p>';
      empty.querySelector('p').textContent = q
        ? ((document.documentElement.lang === 'en' ? 'Nothing found for ' : 'পাওয়া যায়নি: ')
           + '“' + q + '”')
        : (document.documentElement.lang === 'en'
           ? 'Nothing to show' : 'কিছু নেই');
      results.appendChild(empty);
      return;
    }
    groups.forEach(function (group) {
      var lab = document.createElement('div');
      lab.className = 'pal-group-label';
      lab.textContent = group.label || '';
      results.appendChild(lab);
      (group.items || []).forEach(function (it) {
        var el = document.createElement('div');
        el.className = 'pal-item';
        el.setAttribute('role', 'option');
        if (it.url) el.setAttribute('data-url', it.url);
        if (it.action) el.setAttribute('data-action', it.action);
        if (it.question) el.setAttribute('data-question', it.question);

        var ic = document.createElement('span');
        ic.className = 'pal-ic ' + toneClass(it.tone);
        var i = document.createElement('i');
        i.className = 'fa-solid ' + (it.icon || 'fa-circle');
        ic.appendChild(i);
        el.appendChild(ic);

        var body = document.createElement('span');
        body.className = 'pal-item-body';
        var title = document.createElement('span');
        title.className = 'pal-item-title';
        title.textContent = it.title || '';
        body.appendChild(title);
        if (it.sub) {
          var sub = document.createElement('span');
          sub.className = 'pal-item-sub';
          sub.textContent = it.sub;
          body.appendChild(sub);
        }
        el.appendChild(body);
        results.appendChild(el);
      });
    });
  }

  function runSearch(q) {
    lastQuery = q;
    if (abortCtrl) abortCtrl.abort();
    abortCtrl = new AbortController();
    fetch('/search/?q=' + encodeURIComponent(q), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      signal: abortCtrl.signal
    }).then(function (r) { return r.json(); })
      .then(function (data) {
        renderGroups(data.groups, q);
        clearActive();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;
        results.innerHTML = '';
        var empty = document.createElement('div');
        empty.className = 'pal-empty';
        empty.innerHTML = '<i class="fa-solid fa-wifi"></i><p></p>';
        empty.querySelector('p').textContent =
          document.documentElement.lang === 'en'
            ? 'Could not search — check your connection'
            : 'সার্চ করা যায়নি — নেটওয়ার্ক দেখুন';
        results.appendChild(empty);
      });
  }

  input.addEventListener('input', function () {
    var q = input.value.trim();
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () { runSearch(q); }, 130);
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      var items = flatItems();
      if (activeIndex >= 0 && items[activeIndex]) { execute(items[activeIndex]); return; }
      if (items.length) execute(items[0]);
    } else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
  });

  results.addEventListener('click', function (e) {
    var item = e.target.closest('.pal-item');
    if (item) execute(item);
  });
  results.addEventListener('mousemove', function (e) {
    var item = e.target.closest('.pal-item');
    if (!item) return;
    var items = flatItems();
    var idx = items.indexOf(item);
    if (idx !== activeIndex) setActive(idx);
  });

  backdrop.addEventListener('click', closePalette);

  document.addEventListener('keydown', function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (isOpen()) { closePalette(); } else { openPalette(); }
    } else if (e.key === 'Escape' && isOpen()) {
      closePalette();
    }
  });

  ['paletteTrigger', 'paletteTriggerMobile'].forEach(function (id) {
    var btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', function (e) {
      e.preventDefault();
      openPalette();
    });
  });
})();
