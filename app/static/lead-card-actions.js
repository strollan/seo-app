// Per-lead Block / Delete with Undo for Lead Finder cards (Live Scan + Saved Results).
// Server side: POST /lead-bot/lead-action (agents/lead_card_actions_agent.py).
// Neither action touches ACT II contacts or the CRM import queue.
(function () {
  'use strict';
  if (window.__leadCardActionsInstalled) return;
  window.__leadCardActionsInstalled = true;

  function currentToken() {
    const config = document.getElementById('actiiCrmConfig');
    try { return config ? String(JSON.parse(config.textContent) || '') : ''; } catch (_) { return ''; }
  }

  async function refreshToken() {
    try {
      const response = await fetch('/lead-bot/csrf-token', {credentials: 'same-origin', cache: 'no-store'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok || typeof data.csrf_token !== 'string' || !data.csrf_token) return '';
      const config = document.getElementById('actiiCrmConfig');
      if (config) config.textContent = JSON.stringify(data.csrf_token);
      if ('LEADBOT_CSRF_TOKEN' in window) window.LEADBOT_CSRF_TOKEN = data.csrf_token;
      document.querySelectorAll('input[name="csrf_token"]').forEach(input => { input.value = data.csrf_token; });
      return data.csrf_token;
    } catch (_) { return ''; }
  }

  async function send(action, group) {
    const post = token => fetch('/lead-bot/lead-action', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      body: new URLSearchParams({
        action: action, filename: group.dataset.sourceFile || '',
        lead_key: group.dataset.leadKey || '', csrf_token: token || ''
      })
    });
    let response = await post(currentToken());
    let data = await response.json().catch(() => ({}));
    if (response.status === 403 && data.error_code === 'csrf_stale') {
      const token = await refreshToken();
      if (token) {
        response = await post(token);
        data = await response.json().catch(() => ({}));
      }
    }
    if (!response.ok || !data.ok) throw new Error(data.error || 'That change could not be saved. Try again.');
    return data;
  }

  function cardOf(el) { return el.closest('.lead-card, .card, article'); }

  function label(group) {
    const title = String(group.dataset.title || '').trim();
    return title ? '“' + title + '”' : 'this lead';
  }

  function isListing(group) { return !String(group.dataset.leadKey || '').startsWith('domain:'); }

  function showError(group, message) {
    let note = group.querySelector('.lead-card-action-error');
    if (!note) {
      note = document.createElement('small');
      note.className = 'lead-card-action-error';
      note.setAttribute('role', 'alert');
      group.appendChild(note);
    }
    note.textContent = message;
  }

  function setBusy(group, busy) {
    group.querySelectorAll('button').forEach(b => { b.disabled = busy; });
  }

  function showUndo(card, group, action) {
    const undoAction = action === 'delete' ? 'restore' : 'unblock';
    const bar = document.createElement('div');
    bar.className = 'lead-card-undo';
    bar.setAttribute('role', 'status');
    const text = document.createElement('span');
    text.textContent = action === 'delete'
      ? 'Deleted ' + label(group) + ' from these results.'
      : 'Blocked ' + label(group) + (isListing(group)
          ? ': this Google Maps listing will be left out of your future scans.'
          : ': this website will be left out of your future scans.');
    const undo = document.createElement('button');
    undo.type = 'button';
    undo.textContent = 'Undo';
    undo.addEventListener('click', async function () {
      undo.disabled = true;
      try {
        await send(undoAction, group);
        bar.remove();
        card.hidden = false;
        card.style.display = '';
        setBusy(group, false);
        const focusTarget = group.querySelector(action === 'delete' ? '.lead-card-delete' : '.lead-card-block');
        if (focusTarget) focusTarget.focus();
      } catch (err) {
        undo.disabled = false;
        text.textContent = err.message;
      }
    });
    bar.append(text, undo);
    card.parentNode.insertBefore(bar, card);
    card.hidden = true;
    card.style.display = 'none';
    undo.focus();
  }

  document.addEventListener('click', async function (event) {
    const button = event.target.closest('.lead-card-manage .lead-card-block, .lead-card-manage .lead-card-delete');
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const group = button.closest('.lead-card-manage');
    const card = cardOf(group);
    if (!card || button.disabled) return;
    const action = button.classList.contains('lead-card-delete') ? 'delete' : 'block';
    const leadKey = String(group.dataset.leadKey || '');
    const question = action === 'delete'
      ? 'Delete ' + label(group) + ' from these saved results?\n\nYou can undo this. Anything already added to ACT II is not changed.'
      : (isListing(group)
          ? 'Block ' + label(group) + '?\n\nOnly this Google Maps listing is blocked: other locations of the same business are not affected. It is hidden now and left out of your future scans.'
          : 'Block ' + leadKey.slice('domain:'.length) + '?\n\nThis website is hidden now and left out of your future scans.');
    if (!window.confirm(question)) return;
    setBusy(group, true);
    try {
      await send(action, group);
      const old = group.querySelector('.lead-card-action-error');
      if (old) old.remove();
      showUndo(card, group, action);
    } catch (err) {
      setBusy(group, false);
      showError(group, err.message);
    }
  }, true);
})();
