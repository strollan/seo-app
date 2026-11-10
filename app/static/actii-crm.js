/* Existing CRM review form, with durable owner-scoped queue status. */
(() => {
  const dialog = document.getElementById('actiiCrmDialog');
  const form = document.getElementById('actiiCrmForm');
  const config = document.getElementById('actiiCrmConfig');
  if (!dialog || !form || !config || dialog.dataset.connected) return;
  dialog.dataset.connected = 'true';
  let csrfToken = JSON.parse(config.textContent);
  const message = document.getElementById('actiiCrmMessage');
  const save = form.querySelector('.crm-save');
  const cancel = document.getElementById('actiiCrmCancel');
  // A queued job only moves when the ACT II connector on the workstation picks it up, so
  // "queued" must never look like work in progress. Only "processing" means it is importing.
  const labels = {queued: 'Pending import', processing: 'Importing\u2026', added: 'Added to ACT II',
    already_present: 'Already in ACT II', needs_review: 'Needs review', failed: 'Import failed'};
  const JUST_QUEUED = 'Queued for ACT II';  // the 202 reply on this page, until the state changes
  // Plain-language status beside the button; review keeps the server's guidance.
  const notes = {
    queued: 'Pending import. Your request is saved and will be imported when the ACT II connector on your workstation picks it up. No need to resubmit.',
    processing: 'Importing into ACT II now. This usually takes a couple of minutes.',
    added: 'Added to ACT II.', already_present: 'Already in ACT II. No duplicate was created.',
    failed: 'Import failed. Nothing was added to ACT II. Check that ACT II is running on your workstation; this lead keeps its failed request, so ask support to retry it rather than resubmitting.'};
  const JUST_QUEUED_NOTE = 'Queued for ACT II. It will be imported when the ACT II connector on your workstation picks it up. No need to resubmit.';
  const STALLED_NOTE = 'Still pending. Automatic status checks have stopped; refresh the page later to check. Your request is saved.';
  const pending = state => state === 'queued' || state === 'processing';
  const POLL_MS = 15000, POLL_LIMIT = 80;  // read-only status checks for at most ~20 minutes
  const jobs = new Map();
  let source = null;
  let busy = false;
  let checking = false;
  let pollTimer = null, polls = 0;

  // Google Maps leads are one CRM contact per listing (place ID/CID) in every scan;
  // organic leads are keyed by their saved export and domain.
  function key(file, domain, listing) { return listing ? 'listing:' + listing : JSON.stringify([file, domain]); }

  // Each page render issues a new form token, so a page left open (or a finished
  // scan that loaded its saved cards) can hold an outdated one. That is not a
  // signed-out session: fetch a fresh token for this page once and retry.
  async function refreshToken() {
    const response = await fetch('/lead-bot/csrf-token', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok || typeof data.csrf_token !== 'string' || !data.csrf_token) return false;
    csrfToken = data.csrf_token;
    config.textContent = JSON.stringify(csrfToken);
    if ('LEADBOT_CSRF_TOKEN' in window) window.LEADBOT_CSRF_TOKEN = csrfToken;
    document.querySelectorAll('input[name="csrf_token"]').forEach(input => { input.value = csrfToken; });
    return true;
  }

  function localLink(data) {
    const target = new URL(String(data.open_url || ''));
    if (target.origin !== 'http://127.0.0.1:8842' ||
        !/^\/crm\/contacts\/[1-9][0-9]*$/.test(target.pathname) ||
        target.username || target.password || target.search || target.hash) throw new Error('Invalid contact');
    const link = document.createElement('a');
    link.href = target.href;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Open ACT II on this workstation';
    return link;
  }

  function paint(button, data) {
    if (!Object.hasOwn(labels, data.state)) throw new Error('Invalid status');
    const fresh = data.state === 'queued' && button.dataset.crmQueuedHere === 'true';
    const label = fresh ? JUST_QUEUED : labels[data.state];
    button.textContent = label;
    button.disabled = true;
    button.dataset.crmState = data.state;
    if (data.state !== 'queued') delete button.dataset.crmQueuedHere;
    let result = button.parentElement.querySelector('.lead-crm-result');
    if (!result) {
      result = document.createElement('span');
      result.className = 'lead-crm-result';
      result.setAttribute('role', 'status');
      button.insertAdjacentElement('afterend', result);
    }
    const note = (fresh ? JUST_QUEUED_NOTE : notes[data.state]) || data.message || label;
    result.textContent = note;
    if (data.state === 'added' || data.state === 'already_present') {
      result.append(' ', localLink(data));
    }
    if (source && source.button === button && !busy) {
      message.textContent = note;
      if (data.state === 'added' || data.state === 'already_present') message.append(' ', localLink(data));
      save.disabled = true;
      save.textContent = label;
      cancel.textContent = 'Close';
    }
  }

  // Shown when status can't be read or checks have stopped; the button stays settled (never spinning).
  function notePending(text) {
    document.querySelectorAll('[data-crm-state="queued"], [data-crm-state="processing"]').forEach(button => {
      const result = button.parentElement.querySelector('.lead-crm-result');
      if (result) result.textContent = text;
      if (source && source.button === button && !busy) message.textContent = text;
    });
  }

  function applyJobs() {
    document.querySelectorAll('.lead-add-crm').forEach(button => {
      const job = jobs.get(key(button.dataset.sourceFile, button.dataset.sourceDomain, button.dataset.listingKey));
      if (job) paint(button, job);
    });
  }

  async function checkJobs() {
    if (document.hidden || checking) return;
    checking = true;
    try {
      const response = await fetch('/lead-bot/crm-jobs', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error('Status unavailable');
      const data = await response.json();
      if (data.ok !== true || !Array.isArray(data.jobs)) throw new Error('Invalid status');
      data.jobs.forEach(job => jobs.set(key(job.source_file, job.source_domain, job.listing_key), job));
      applyJobs();
      schedulePoll();
    } catch (_) {
      // Keep the last confirmed state; a status outage must never resubmit an import.
      notePending('Status unavailable. Refresh to check; your import request is saved.');
    } finally {
      checking = false;
    }
  }

  // While an import is still pending, re-read the existing status endpoint (GET only; it never
  // resubmits) so the button turns into "Added to ACT II" without a manual refresh. Bounded.
  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (![...jobs.values()].some(job => pending(job.state))) return;
    if (polls >= POLL_LIMIT) { notePending(STALLED_NOTE); return; }
    pollTimer = setTimeout(() => { pollTimer = null; polls += 1; checkJobs(); }, POLL_MS);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !pollTimer) schedulePoll(); });

  document.addEventListener('click', event => {
    const button = event.target.closest('.lead-add-crm');
    if (!button || button.disabled || busy) return;
    try {
      const lead = JSON.parse(button.dataset.lead);
      source = {file: button.dataset.sourceFile, domain: button.dataset.sourceDomain, listing: button.dataset.listingKey || '', button};
      form.reset();
      Object.entries(lead).forEach(([name, value]) => {
        const field = form.elements.namedItem(name === 'industry' ? 'crm_industry' : name);
        if (field) field.value = value ?? '';
      });
      const card = button.closest('.lead-card, .card');
      ['phone', 'email', 'website', 'address'].forEach(name => {
        const input = card && card.querySelector('input[name="' + name + '"]');
        if (input) form.elements.namedItem(name).value = input.value;
      });
      // A Google Maps lead keeps the website it was verified with, or none.
      form.elements.namedItem('website').readOnly = Boolean(source.listing);
      if (source.listing) form.elements.namedItem('website').value = lead.website || '';
      message.textContent = '';
      save.disabled = false;
      save.textContent = 'Add Contact';
      cancel.textContent = 'Cancel';
      dialog.showModal();
    } catch (_) {
      window.alert('This lead could not be prepared for ACT II. Refresh and try again.');
    }
  });
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!source || busy || source.button.disabled) return;
    const current = source;
    busy = true;
    save.disabled = cancel.disabled = current.button.disabled = true;
    save.textContent = current.button.textContent = 'Sending\u2026';  // bounded by the 20 s request timeout
    form.setAttribute('aria-busy', 'true');
    message.textContent = 'Sending to ACT II\u2026';
    let confirmed = false;
    try {
      const lead = Object.fromEntries(new FormData(form).entries());
      const send = () => fetch('/lead-bot/add-to-crm', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        signal: AbortSignal.timeout(20000),
        body: JSON.stringify({csrf_token: csrfToken, source_file: current.file,
          source_domain: current.domain, listing_key: current.listing, lead})
      });
      let response = await send();
      let data = await response.json();
      if (response.status === 403 && data.error_code === 'csrf_stale' && await refreshToken()) {
        response = await send();  // same request; the queue keeps one job per lead
        data = await response.json();
      }
      if (!response.ok || data.ok !== true) {
        message.textContent = data.error || ('Could not send this lead to ACT II (error ' + response.status +
          '). Nothing was queued. Try again with Retry request; if it keeps failing, refresh the page.');
        if (data.error_code === 'signed_out') {
          const link = document.createElement('a');
          link.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
          link.textContent = 'Sign in';
          message.append(' ', link);
        }
        return;
      }
      if (!data.state) {
        // Explicit local mode retains the existing synchronous contract.
        if (!['created', 'existing'].includes(data.status)) throw new Error('Invalid response');
        data.state = data.status === 'created' ? 'added' : 'already_present';
        data.message = data.status === 'created' ? 'Added to CRM.' : 'Already present in CRM.';
      } else {
        jobs.set(key(current.file, current.domain, current.listing), data);
        if (response.status === 202 && data.state === 'queued') current.button.dataset.crmQueuedHere = 'true';
      }
      paint(current.button, data);
      const fresh = data.state === 'queued' && current.button.dataset.crmQueuedHere === 'true';
      message.textContent = (fresh ? JUST_QUEUED_NOTE : notes[data.state]) || data.message;
      if (data.state === 'added' || data.state === 'already_present') message.append(' ', localLink(data));
      save.textContent = current.button.textContent;
      cancel.textContent = 'Close';
      confirmed = true;
    } catch (_) {
      message.textContent = 'Request outcome unavailable. Refresh to check before retrying; repeated queue requests keep the same job.';
    } finally {
      busy = false;
      save.disabled = current.button.disabled = confirmed;
      cancel.disabled = false;
      form.removeAttribute('aria-busy');
      if (!confirmed) { save.textContent = 'Retry request'; current.button.textContent = 'Add to CRM'; }
      if (confirmed) checkJobs();
    }
  });
  // Saved cards and live-scan cards share the same server-owned source identifiers.
  new MutationObserver(records => {
    if (records.some(record => [...record.addedNodes].some(node => node.nodeType === 1 &&
        (node.matches('.lead-add-crm') || node.querySelector('.lead-add-crm'))))) applyJobs();
  }).observe(document.body, {childList: true, subtree: true});
  // Check on page load and after submission; pending imports are re-checked by schedulePoll().
  checkJobs();
})();
