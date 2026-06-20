/* ============================================================
   InvoiceIQ — Global JS
   ============================================================ */

// ---- Toast ----

function showToast(message, type = 'success') {
  const toast = document.getElementById('app-toast');
  const msg = document.getElementById('toast-message');
  if (!toast) return;
  toast.className = `toast align-items-center border-0 text-bg-${type}`;
  msg.textContent = message;
  new bootstrap.Toast(toast, { delay: 3500 }).show();
}

// ---- Approval badge ----

function refreshApprovalBadge() {
  fetch('/api/approval-count')
    .then(r => r.json())
    .then(d => {
      const badge = document.getElementById('approval-badge');
      if (!badge) return;
      if (d.count > 0) {
        badge.textContent = d.count;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
      }
    })
    .catch(() => {});
}

// ---- Manual email poll ----

document.addEventListener('DOMContentLoaded', () => {
  refreshApprovalBadge();
  setInterval(refreshApprovalBadge, 30000);

  const btn = document.getElementById('btn-poll-email');
  if (btn) {
    btn.addEventListener('click', () => {
      btn.disabled = true;
      btn.innerHTML = '<i class="bi bi-arrow-repeat spinning"></i> Checking…';
      fetch('/api/email/poll', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          if (d.error) {
            showToast('Email check failed: ' + d.error, 'danger');
          } else {
            const msg = d.processed > 0
              ? `${d.processed} invoice(s) processed`
              : 'No new invoices';
            showToast(msg, 'success');
            document.getElementById('last-poll-time').textContent =
              'Last checked: ' + new Date().toLocaleTimeString();
            if (typeof onEmailPollComplete === 'function') onEmailPollComplete();
          }
        })
        .catch(() => showToast('Email check failed', 'danger'))
        .finally(() => {
          btn.disabled = false;
          btn.innerHTML = '<i class="bi bi-envelope-arrow-down"></i> Check Email';
        });
    });
  }
});

// ---- Formatting helpers ----

function formatCurrency(amount, currency = 'GBP') {
  if (amount == null) return '—';
  return new Intl.NumberFormat('en-GB', { style: 'currency', currency }).format(amount);
}

function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-GB');
}

function formatDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function statusBadge(status, label, color) {
  return `<span class="status-badge badge-${color}">${label}</span>`;
}

function confidenceBar(pct) {
  const color = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';
  return `<div class="confidence-bar" title="${pct}%"><div class="confidence-fill" style="width:${pct}%;background:${color}"></div></div> <small class="text-muted">${pct}%</small>`;
}
