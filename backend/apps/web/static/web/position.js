/* FinCore — হিসাব কমান্ড সেন্টার: expand/collapse + what-if simulator.
   Read-only by design: this file never posts anything. */
(function () {
  'use strict';

  var expand = document.getElementById('psExpand');
  var collapse = document.getElementById('psCollapse');
  if (expand) {
    expand.addEventListener('click', function () {
      document.querySelectorAll('details.ps-sec, details.ps-cust').forEach(function (d) {
        d.open = true;
      });
    });
  }
  if (collapse) {
    collapse.addEventListener('click', function () {
      document.querySelectorAll('details.ps-sec, details.ps-cust').forEach(function (d) {
        d.open = false;
      });
    });
  }

  var raw = document.getElementById('psData');
  if (!raw) return;
  var data;
  try { data = JSON.parse(raw.textContent); } catch (e) { return; }
  if (!data) return;

  var collect = document.getElementById('wiCollect');
  var writeoff = document.getElementById('wiWriteoff');
  var repayInput = document.getElementById('wiRepay');
  var newLoanInput = document.getElementById('wiNewLoan');
  var collectOut = document.getElementById('wiCollectOut');
  var writeoffOut = document.getElementById('wiWriteoffOut');
  var cashEl = document.getElementById('wiCash');
  var dueEl = document.getElementById('wiDue');
  var oweEl = document.getElementById('wiOwe');
  var netEl = document.getElementById('wiNet');
  var ratioEl = document.getElementById('wiRatio');
  var msgEl = document.getElementById('wiMsg');
  if (!collect || !cashEl || !netEl) return;

  var base = {
    cash: parseFloat(data.cash) || 0,
    due: parseFloat(data.due) || 0,
    loans: parseFloat(data.loans) || 0,
    advances: parseFloat(data.advances) || 0,
    payable: parseFloat(data.payable) || 0,
    net: parseFloat(data.net) || 0,
    nextInstallments: parseFloat(data.next_installments) || 0
  };
  var lang = (document.documentElement.lang || 'bn').toLowerCase().indexOf('en') === 0 ? 'en' : 'bn';

  function fmt(value) {
    var n = Math.round((value + Number.EPSILON) * 100) / 100;
    var body = Math.abs(n).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
    return (n < 0 ? '-৳' : '৳') + body;
  }
  function pct(value) { return Math.round(value) + '%'; }
  function setSign(el, value) {
    el.classList.toggle('neg', value < 0);
    el.classList.toggle('pos', value > 0);
  }

  function recalc() {
    var collectPct = Math.min(100, Math.max(0, parseFloat(collect.value) || 0)) / 100;
    var writePct = Math.min(100, Math.max(0, parseFloat(writeoff.value) || 0)) / 100;
    var repay = Math.max(0, parseFloat(repayInput && repayInput.value) || 0);
    repay = Math.min(repay, base.cash, base.loans);
    var newLoan = Math.max(0, parseFloat(newLoanInput && newLoanInput.value) || 0);

    var collected = base.due * collectPct;
    var remainingDue = base.due - collected;
    var written = remainingDue * writePct;
    var newCash = base.cash + collected + newLoan - repay;
    var newDue = remainingDue - written;
    var newLoans = base.loans + newLoan - repay;
    var newOwe = newLoans + base.advances + base.payable;
    var newNet = newCash + newDue - newOwe;
    var delta = newNet - base.net;

    if (collectOut) collectOut.textContent = pct(collectPct * 100);
    if (writeoffOut) writeoffOut.textContent = pct(writePct * 100);

    cashEl.textContent = fmt(newCash);
    dueEl.textContent = fmt(newDue);
    oweEl.textContent = fmt(newOwe);
    netEl.textContent = fmt(newNet);
    setSign(netEl, newNet);
    setSign(cashEl, 0);

    var shortTerm = base.payable + base.nextInstallments;
    if (shortTerm > 0.005) {
      ratioEl.textContent = (newCash / shortTerm).toFixed(2) + '×';
    } else {
      ratioEl.textContent = '—';
    }

    var notes = [];
    if (Math.abs(written) > 0.005) {
      notes.push(lang === 'en'
        ? 'Writing off ' + fmt(written) + ' lowers the net position by that amount.'
        : 'মাফ করা ' + fmt(written) + ' নিট অবস্থান ঠিক ততটাই কমিয়েছে।');
    }
    if (Math.abs(repay) > 0.005) {
      notes.push(lang === 'en'
        ? 'Repaying ' + fmt(repay) + ' reduces cash and loans equally — the net is unchanged.'
        : 'ঋণ পরিশোধে নগদ ও দেনা সমান কমে — নিট অপরিবর্তিত থাকে।');
    }
    if (Math.abs(newLoan) > 0.005) {
      notes.push(lang === 'en'
        ? 'A new loan adds cash and debt equally — the net is unchanged, but liquidity rises.'
        : 'নতুন ঋণে নগদ ও দেনা সমান বাড়ে — নিট অপরিবর্তিত, তবে তারল্য বাড়ে।');
    }
    if (!notes.length) {
      notes.push(lang === 'en'
        ? 'Collecting baki moves money from "due" to "cash" — the net stays the same.'
        : 'বাকি আদায়ে টাকা "বাকি" থেকে "নগদে" যায় — নিট বদলায় না।');
    }
    if (Math.abs(delta) > 0.005) {
      notes.push(lang === 'en'
        ? 'Net change: ' + fmt(delta) + '.'
        : 'নিট পরিবর্তন: ' + fmt(delta) + '।');
    }
    if (msgEl) msgEl.textContent = notes.join(' ');
  }

  [collect, writeoff, repayInput, newLoanInput].forEach(function (el) {
    if (el) {
      el.addEventListener('input', recalc);
      el.addEventListener('change', recalc);
    }
  });
  recalc();
})();
