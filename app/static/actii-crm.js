/* Existing CRM review form, with durable owner-scoped queue status. */
(() => {
  const dialog = document.getElementById('actiiCrmDialog');
  const form = document.getElementById('actiiCrmForm');
  const config = document.getElementById('actiiCrmConfig');
  if (!dialog || !form || !config || dialog.dataset.connected) return;
  dialog.dataset.connected = 'true';
  const csrfToken = JSON.parse(config.textContent);
  const message = document.getElementById('actiiCrmMessage');
  const save = form.querySelector('.crm-save');
  const cancel = document.getElementById('actiiCrmCancel');
  const labels = {queued: 'Queued', processing: 'Processing', added: 'Added',
    already_present: 'Already Present', needs_review: 'Needs Review', failed: 'Failed'};
  const jobs = new Map();
  let source = null;
  let busy = false;
  let checking = false;

  function key(file, domain) { return JSON.stringify([file, domain]); }

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
    button.textContent = labels[data.state];
    button.disabled = true;
    button.dataset.crmState = data.state;
    let result = button.parentElement.querySelector('.lead-crm-result');
    if (!result) {
      result = document.createElement('span');
      result.className = 'lead-crm-result';
      result.setAttribute('role', 'status');
      button.insertAdjacentElement('afterend', result);
    }
    result.textContent = data.message || labels[data.state];
    if (data.state === 'added' || data.state === 'already_present') {
      result.append(' ', localLink(data));
    }
    if (source && source.button === button && !busy) {
      message.textContent = data.message || labels[data.state];
      save.disabled = true;
      save.textContent = labels[data.state];
      cancel.textContent = 'Close';
    }
  }

  function applyJobs() {
    document.querySelectorAll('.lead-add-crm').forEach(button => {
      const job = jobs.get(key(button.dataset.sourceFile, button.dataset.sourceDomain));
      if (job) paint(button, job);
    });
  }

  async function checkJobs() {
    if (document.hidden || checking) return;
    checking = true;
    try {
      const response = await fetch('/lead-bot/crm-jobs', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
      if (!response.ok) return;
      const data = await response.json();
      if (data.ok !== true || !Array.isArray(data.jobs)) return;
      data.jobs.forEach(job => jobs.set(key(job.source_file, job.source_domain), job));
      applyJobs();
    } catch (_) {
      // Keep the last confirmed state; a status outage must never resubmit an import.
      document.querySelectorAll('[data-crm-state="queued"], [data-crm-state="processing"]').forEach(button => {
        const result = button.parentElement.querySelector('.lead-crm-result');
        if (result) result.textContent = 'Status unavailable. Refresh to check; your import request is saved.';
      });
    } finally {
      checking = false;
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('.lead-add-crm');
    if (!button || button.disabled || busy) return;
    try {
      const lead = JSON.parse(button.dataset.lead);
      source = {file: button.dataset.sourceFile, domain: button.dataset.sourceDomain, button};
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
      message.textContent = '';
      save.disabled = false;
      save.textContent = 'Add to ACT II';
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
    save.textContent = current.button.textContent = 'Submitting…';
    form.setAttribute('aria-busy', 'true');
    message.textContent = 'Submitting your CRM request…';
    let confirmed = false;
    try {
      const response = await fetch('/lead-bot/add-to-crm', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        signal: AbortSignal.timeout(20000),
        body: JSON.stringify({csrf_token: csrfToken, source_file: current.file,
          source_domain: current.domain, lead: Object.fromEntries(new FormData(form).entries())})
      });
      const data = await response.json();
      if (!response.ok || data.ok !== true) {
        message.textContent = data.error || 'CRM request failed. Refresh and check the connection.';
        return;
      }
      if (!data.state) {
        // Explicit local mode retains the existing synchronous contract.
        if (!['created', 'existing'].includes(data.status)) throw new Error('Invalid response');
        data.state = data.status === 'created' ? 'added' : 'already_present';
        data.message = data.status === 'created' ? 'Added to CRM.' : 'Already present in CRM.';
      } else {
        jobs.set(key(current.file, current.domain), data);
      }
      paint(current.button, data);
      message.textContent = data.message;
      if (data.state === 'added' || data.state === 'already_present') message.append(' ', localLink(data));
      save.textContent = labels[data.state];
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
  // Check once on page load and after submission. Refresh for later status; no polling.
  checkJobs();
})();
