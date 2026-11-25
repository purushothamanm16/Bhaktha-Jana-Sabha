/* Manikandan Bhaktha Jana Sabha – frontend (vanilla JS). No secrets live here. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const nf = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const money = (v) => (typeof v === 'number' ? '₹' + nf.format(v) : String(v ?? ''));
  const label = (t) => (t === 'credit' ? 'Credit' : 'Debit');

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  // ---------------------------------------------------------------- toasts
  function toast(message, kind = 'success') {
    const box = $('#toasts');
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    t.textContent = message;
    box.appendChild(t);
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 400); }, kind === 'error' ? 7000 : 4500);
  }

  // ---------------------------------------------------------------- API helper
  class ApiError extends Error {
    constructor(message, status, duplicate) { super(message); this.status = status; this.duplicate = !!duplicate; }
  }

  async function api(method, url, body) {
    let res;
    try {
      res = await fetch(url, {
        method,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new ApiError('Network error. Please check your connection and try again.', 0);
    }
    if (res.status === 401) {
      window.location.href = '/login?expired=1';
      throw new ApiError('Your session has expired.', 401);
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) throw new ApiError(data.error || 'Something went wrong. Please try again.', res.status, data.duplicate);
    return data;
  }

  // ---------------------------------------------------------------- form helpers
  function typeToggle(root) {
    const buttons = [...root.querySelectorAll('.type-btn')];
    let value = '';
    const set = (v) => {
      value = v;
      buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.type === v)));
    };
    buttons.forEach((b) => b.addEventListener('click', () => { set(b.dataset.type); root.dispatchEvent(new Event('change')); }));
    return { get value() { return value; }, set, reset: () => set('') };
  }

  function showErrors(form, errors) {
    form.querySelectorAll('.field-error').forEach((p) => { p.textContent = errors[p.dataset.for] || ''; });
    form.querySelectorAll('input, textarea').forEach((i) => i.removeAttribute('aria-invalid'));
    Object.keys(errors).forEach((k) => {
      const input = form.querySelector(`[data-for="${k}"]`)?.closest('.field')?.querySelector('input, textarea');
      if (input) input.setAttribute('aria-invalid', 'true');
    });
  }

  function validate(v) {
    const errors = {};
    if (!v.name.trim()) errors.name = 'Name is required.';
    const amt = v.amount.trim().replace(/,/g, '');
    if (!amt) errors.amount = 'Amount is required.';
    else if (amt.startsWith('-')) errors.amount = 'Amount must be greater than zero.';
    else if (!/^(\d+(\.\d*)?|\.\d+)$/.test(amt)) errors.amount = 'Enter a valid amount, for example 1500 or 250.50.';
    else if (Number(amt) <= 0) errors.amount = 'Amount must be greater than zero.';
    else if (!/^\d*\.?\d{0,2}$/.test(amt)) errors.amount = 'Amount can have at most 2 decimal places.';
    if (!v.timestamp) errors.timestamp = 'Date and time is required.';
    if (!v.type) errors.type = 'Please select Credit or Debit.';
    return errors;
  }

  // "25-11-2025 09:37" (as stored in the sheet) -> "2025-11-25T09:37" (what the picker needs)
  function toInputValue(s) {
    const m = /^(\d{2})-(\d{2})-(\d{4}) (\d{2}):(\d{2})/.exec(s || '');
    return m ? `${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}` : '';
  }

  function setBusy(btn, busy, text) {
    btn.disabled = busy;
    btn.classList.toggle('loading', busy);
    if (text) btn.textContent = busy ? 'Please wait…' : text;
  }

  function showFormError(el, msg) {
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  function focusFirstError(form, errors) {
    const k = Object.keys(errors)[0];
    if (k === 'type') form.querySelector('.type-btn')?.focus();
    else form.querySelector(`[data-for="${k}"]`)?.closest('.field')?.querySelector('input, textarea')?.focus();
  }

  // ================================================================ DASHBOARD
  function initDashboard() {
    const form = $('#tx-form');
    const toggle = typeToggle($('#type-toggle'));
    const btn = $('#submit-btn');
    const formError = $('#form-error');
    let clientId = uuid();      // same id is reused on retry, so a retry can never double-post
    let busy = false;

    const read = () => ({
      name: $('#name').value, amount: $('#amount').value, timestamp: $('#timestamp').value,
      type: toggle.value, description: $('#description').value,
    });

    $('#type-toggle').addEventListener('change', () => {
      form.querySelector('[data-for="type"]').textContent = '';
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (busy) return;
      const data = read();
      const errors = validate(data);
      showErrors(form, errors);
      showFormError(formError, '');
      if (Object.keys(errors).length) { focusFirstError(form, errors); return; }

      busy = true; setBusy(btn, true, 'Submit');
      try {
        const res = await api('POST', '/api/transactions', { ...data, client_id: clientId });
        toast(`${label(data.type)} of ${money(Number(data.amount.replace(/,/g, '')))} recorded for ${data.name.trim().replace(/\s+/g, ' ')} (No. ${res.transaction.no}).`);
        form.reset(); toggle.reset(); showErrors(form, {}); clientId = uuid();
        $('#name').focus();
      } catch (err) {
        if (err.duplicate) {
          toast('This entry was already recorded.', 'info');
          form.reset(); toggle.reset(); clientId = uuid();
        } else if (err.status !== 401) {
          showFormError(formError, err.message);   // form values are kept
        }
      } finally {
        busy = false; setBusy(btn, false, 'Submit');
      }
    });
  }

  // ================================================================ RECENT TRANSACTIONS
  function initTransactions() {
    const status = $('#list-status');
    const tableWrap = $('#table-wrap');
    const body = $('#tx-body');
    const cards = $('#tx-cards');
    const dialog = $('#edit-dialog');
    const form = $('#edit-form');
    const toggle = typeToggle($('#edit-type-toggle'));
    const saveBtn = $('#edit-save');
    const editError = $('#edit-error');
    const notice = $('#move-notice');
    let items = [];
    let editing = null;
    let busy = false;

    const el = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    };

    function editButton(tx) {
      const b = el('button', 'btn-edit', 'Edit');
      b.type = 'button';
      b.setAttribute('aria-label', `Edit ${label(tx.type)} entry for ${tx.name}`);
      b.addEventListener('click', () => openEdit(tx));
      return b;
    }

    function render() {
      body.replaceChildren(); cards.replaceChildren();
      items.forEach((tx) => {
        // desktop table row
        const tr = el('tr', tx.type);
        tr.append(el('td', 'no', tx.no), el('td', 'name', tx.name), el('td', `amount ${tx.type}`, money(tx.amount)));
        const typeTd = el('td'); typeTd.append(el('span', `badge ${tx.type}`, label(tx.type)));
        tr.append(typeTd, el('td', 'desc', tx.description || '—'));
        const act = el('td'); act.append(editButton(tx)); tr.append(act);
        body.append(tr);

        // mobile card
        const c = el('article', `tx-card ${tx.type}`);
        const top = el('div', 'tx-top');
        top.append(el('span', 'tx-no', `No. ${tx.no}`), el('span', `badge ${tx.type}`, label(tx.type)));
        c.append(top, el('div', 'tx-name', tx.name), el('div', `tx-amount ${tx.type}`, money(tx.amount)));
        if (tx.description) c.append(el('div', 'tx-desc', tx.description));
        c.append(editButton(tx));
        cards.append(c);
      });
    }

    async function load() {
      status.hidden = false; status.className = 'status'; status.textContent = 'Loading…';
      try {
        const data = await api('GET', '/api/transactions');
        items = data.transactions || [];
        if (!items.length) {
          status.textContent = 'No transactions have been recorded through this website yet.';
          tableWrap.hidden = true; cards.replaceChildren();
          return;
        }
        status.hidden = true; tableWrap.hidden = false; render();
      } catch (err) {
        if (err.status === 401) return;
        tableWrap.hidden = true; cards.replaceChildren();
        status.className = 'status error';
        status.textContent = err.message + ' ';
        const retry = el('button', 'btn-edit', 'Try again');
        retry.type = 'button'; retry.addEventListener('click', load);
        status.append(retry);
      }
    }

    function openEdit(tx) {
      editing = tx;
      $('#edit-name').value = tx.name;
      $('#edit-amount').value = typeof tx.amount === 'number' ? String(tx.amount) : String(tx.amount ?? '');
      $('#edit-timestamp').value = toInputValue(tx.timestamp);
      $('#edit-description').value = tx.description || '';
      toggle.set(tx.type);
      showErrors(form, {}); showFormError(editError, ''); notice.hidden = true;
      dialog.showModal();
    }

    $('#edit-type-toggle').addEventListener('change', () => {
      notice.hidden = !editing || toggle.value === editing.type;
    });
    $('#edit-cancel').addEventListener('click', () => { if (!busy) dialog.close(); });
    dialog.addEventListener('cancel', (e) => { if (busy) e.preventDefault(); });

    const deleteBtn = $('#edit-delete');
    deleteBtn.addEventListener('click', async () => {
      if (busy || !editing) return;
      if (!confirm(`Delete the ${label(editing.type)} entry for ${editing.name} (${money(editing.amount)})? ` +
                   'This removes it from the spreadsheet and cannot be undone.')) return;
      busy = true; deleteBtn.disabled = true; saveBtn.disabled = true;
      showFormError(editError, '');
      try {
        await api('DELETE', `/api/transactions/${encodeURIComponent(editing.id)}`);
        dialog.close();
        toast('Transaction deleted and numbering updated.');
        await load();
      } catch (err) {
        if (err.status === 404 || err.status === 403) load();   // list was out of date
        if (err.status !== 401) showFormError(editError, err.message);
      } finally {
        busy = false; deleteBtn.disabled = false; saveBtn.disabled = false;
      }
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (busy || !editing) return;
      const data = {
        name: $('#edit-name').value, amount: $('#edit-amount').value,
        timestamp: $('#edit-timestamp').value,
        type: toggle.value, description: $('#edit-description').value,
      };
      const errors = validate(data);
      showErrors(form, errors); showFormError(editError, '');
      if (Object.keys(errors).length) { focusFirstError(form, errors); return; }

      busy = true; setBusy(saveBtn, true, 'Save Changes');
      try {
        await api('PUT', `/api/transactions/${encodeURIComponent(editing.id)}`, data);
        dialog.close();
        toast(editing.type !== data.type
          ? `Entry moved to ${label(data.type)} and both sheets renumbered.`
          : 'Transaction updated.');
        await load();
      } catch (err) {
        if (err.status === 404 || err.status === 403) load();   // list is out of date
        if (err.status !== 401) showFormError(editError, err.message);   // modal stays open, values kept
      } finally {
        busy = false; setBusy(saveBtn, false, 'Save Changes');
      }
    });

    load();
  }

  // ================================================================ boot
  const page = document.body.dataset.page;
  if (page === 'dashboard') initDashboard();
  else if (page === 'transactions') initTransactions();
})();
