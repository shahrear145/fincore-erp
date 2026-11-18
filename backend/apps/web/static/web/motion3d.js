/* ============================================================
   motion3d.js — depth layer for FinCore
   Pointer tilt, scroll reveal, count-up, hero parallax,
   chart bar depth. Purely visual: reads values, never mutates
   data or layout. Skipped on touch devices and for users with
   prefers-reduced-motion.
   ============================================================ */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var finePointer = window.matchMedia('(pointer: fine)').matches;
  if (reduce) return;

  /* ---------- 1) pointer-tracked tilt on cards -------------- */
  function tiltables() {
    return document.querySelectorAll('.kpi, .card:has(canvas), .hero');
  }
  if (finePointer && 'IntersectionObserver' in window) {
    var raf = null, pending = [];
    function flush() {
      raf = null;
      pending.forEach(function (fn) { fn(); });
      pending = [];
    }
    document.addEventListener('pointermove', function (e) {
      if (raf) { return; }
      raf = requestAnimationFrame(flush);
      pending.push(function () {
        var card = e.target.closest && e.target.closest('.tilt3d');
        document.querySelectorAll('.tilt3d.tracking').forEach(function (el) {
          if (el !== card) {
            el.style.setProperty('--rx', '0deg');
            el.style.setProperty('--ry', '0deg');
            el.style.setProperty('--ty', '0px');
            el.classList.remove('tracking');
          }
        });
        if (!card) return;
        var r = card.getBoundingClientRect();
        var dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
        var dy = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
        if (dx < -1.6 || dx > 1.6 || dy < -1.6 || dy > 1.6) return;
        card.classList.add('tracking');
        if (card.classList.contains('hero')) {
          /* hero: parallax on the glow orb, no body tilt */
          card.style.setProperty('--px', (dx * 26) + 'px');
          card.style.setProperty('--py', (dy * 18) + 'px');
        } else {
          /* the period bar only leans a little - it holds form inputs */
          var soft = card.classList.contains('period-bar');
          var k = soft ? 1.1 : 3.2;
          card.style.setProperty('--ry', (dx * k) + 'deg');
          card.style.setProperty('--rx', (dy * -k) + 'deg');
          card.style.setProperty('--ty', soft ? '-2px' : '-4px');
        }
      });
    }, { passive: true });
    document.addEventListener('pointerleave', function () {
      document.querySelectorAll('.tilt3d.tracking').forEach(function (el) {
        el.style.setProperty('--rx', '0deg'); el.style.setProperty('--ry', '0deg'); el.style.setProperty('--ty', '0px');
      });
    }, { passive: true });
  }

  /* ---------- 2) scroll reveal + count-up on entry ---------- */
  function parseMoney(text) {
    var m = String(text).replace(/[\s\u00A0]/g, '').match(/^[^\d\-]*(-?[\d,]+(?:\.\d+)?)/);
    if (!m) return null;
    var n = parseFloat(m[1].replace(/,/g, ''));
    return isFinite(n) ? n : null;
  }

  function countUp(el) {
    var target = parseMoney(el.textContent);
    if (target === null || Math.abs(target) < 1) { el.classList.add('val-pop'); setTimeout(function(){ el.classList.remove('val-pop'); }, 500); return; }
    var prefix = (el.textContent.match(/^[^\d\-]*/) || [''])[0];
    var t0 = null, dur = 850;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      var val = target * eased;
      el.textContent = prefix + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      if (p < 1) { requestAnimationFrame(step); }
      else {
        el.textContent = prefix + target.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        el.classList.add('val-pop');
        setTimeout(function () { el.classList.remove('val-pop'); }, 500);
      }
    }
    requestAnimationFrame(step);
  }

  function initReveal() {
    var targets = document.querySelectorAll('.g4 > .card, .g5 > .card');
    if (!targets.length) return;
    targets.forEach(function (el) { el.classList.add('pre3d', 'tilt3d'); });
    if (!('IntersectionObserver' in window)) {
      targets.forEach(function (el) { el.classList.add('in3d'); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target;
        el.classList.add('in3d');
        io.unobserve(el);
        var v = el.querySelector('.value');
        if (v && !v.dataset.counted) { v.dataset.counted = '1'; countUp(v); }
      });
    }, { threshold: .15, rootMargin: '0px 0px -30px 0px' });
    targets.forEach(function (el) { io.observe(el); });
  }

  /* ---------- 3) chart bar depth (shadow) ------------------- */
  function initChartDepth() {
    if (typeof Chart === 'undefined') return;
    var shadow = {
      id: 'barDepth3d',
      beforeDatasetDraw: function (chart) {
        if (chart.config.type !== 'bar') return;
        var ctx = chart.ctx;
        ctx.save();
        ctx.shadowColor = 'rgba(8,17,32,.30)';
        ctx.shadowBlur = 12;
        ctx.shadowOffsetY = 5;
      },
      afterDatasetDraw: function (chart) {
        if (chart.config.type !== 'bar') return;
        chart.ctx.restore();
      }
    };
    if (!Chart.registry.plugins.get('barDepth3d')) {
      Chart.register(shadow);
    }
  }

  /* ---------- boot ------------------------------------------ */
  function boot() {
    /* tilt targets: hero (parallax), chart cards, and the KPI tiles
       (tiles get the class inside initReveal). */
    try {
      document.querySelectorAll('.hero, .card:has(canvas)').forEach(function (el) {
        el.classList.add('tilt3d');
      });
    } catch (err) { /* older engine without :has */ }
    document.querySelectorAll('.period-bar').forEach(function (el) {
      el.classList.add('tilt3d');
    });
    initReveal();
    initChartDepth();
    /* safety: never leave cards invisible if something above failed */
    setTimeout(function () {
      document.querySelectorAll('.pre3d:not(.in3d)').forEach(function (el) {
        el.classList.add('in3d');
      });
    }, 3000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
