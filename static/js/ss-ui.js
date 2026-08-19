/*
 * ScanStory shared Creator UI helpers: an accessible confirmation dialog and a
 * toast, replacing window.confirm()/window.alert() on the Creator pages.
 *
 * SCOPE, deliberately narrow. This changes only PRESENTATION. Every action that
 * a native confirm() used to gate is still gated by exactly the same question
 * with exactly the same default (nothing destructive happens until the creator
 * presses the destructive button), and every form still submits through the
 * same POST it always did. No confirmation is removed, weakened, or
 * auto-answered.
 *
 * ponytail: no dependency, no framework, ~120 lines. <dialog> would be shorter
 * still, but its backdrop/scroll behaviour is inconsistent on the older mobile
 * Safari this product is actually scanned on, so this uses a plain div with the
 * focus handling done by hand.
 */
(function (window, document) {
  'use strict';

  var FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

  function toastStack() {
    var stack = document.getElementById('ssToastStack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'ssToastStack';
      stack.className = 'ss-toast-stack';
      stack.setAttribute('role', 'status');
      stack.setAttribute('aria-live', 'polite');
      document.body.appendChild(stack);
    }
    return stack;
  }

  /**
   * ssToast(message, tone) - tone: 'success' | 'error' | undefined.
   * Announced politely rather than interrupting, and self-dismissing: a copied
   * link does not deserve a modal the creator has to acknowledge.
   */
  function ssToast(message, tone) {
    var el = document.createElement('div');
    el.className = 'ss-toast' + (tone === 'success' ? ' ss-toast-success' : tone === 'error' ? ' ss-toast-error' : '');
    el.textContent = message;
    toastStack().appendChild(el);
    window.setTimeout(function () {
      el.setAttribute('data-leaving', 'true');
      window.setTimeout(function () { el.remove(); }, 300);
    }, 3200);
  }

  /**
   * ssConfirm({title, body, confirmLabel, cancelLabel, destructive}) -> Promise<boolean>
   * Resolves true only on an explicit press of the confirm button. Escape,
   * backdrop click and Cancel all resolve false, which is the safe answer for
   * every call site that uses this.
   */
  function ssConfirm(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var previouslyFocused = document.activeElement;
      var overlay = document.createElement('div');
      overlay.className = 'ss-dialog';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');

      var card = document.createElement('div');
      card.className = 'ss-dialog-card';

      var title = document.createElement('h2');
      title.className = 'ss-dialog-title';
      title.id = 'ssDialogTitle-' + Date.now();
      title.textContent = opts.title || 'Are you sure?';
      overlay.setAttribute('aria-labelledby', title.id);

      var body = document.createElement('p');
      body.className = 'ss-dialog-body';
      body.textContent = opts.body || '';

      var actions = document.createElement('div');
      actions.className = 'ss-dialog-actions';

      var cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'ss-btn ss-btn-secondary';
      cancel.textContent = opts.cancelLabel || 'Cancel';

      var confirm = document.createElement('button');
      confirm.type = 'button';
      confirm.className = 'ss-btn ' + (opts.destructive ? 'ss-btn-destructive' : 'ss-btn-primary');
      confirm.textContent = opts.confirmLabel || 'Continue';

      actions.appendChild(cancel);
      actions.appendChild(confirm);
      card.appendChild(title);
      if (body.textContent) card.appendChild(body);
      card.appendChild(actions);
      overlay.appendChild(card);
      document.body.appendChild(overlay);

      function close(answer) {
        document.removeEventListener('keydown', onKeydown, true);
        overlay.remove();
        if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
        resolve(answer);
      }

      function onKeydown(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          close(false);
          return;
        }
        if (event.key !== 'Tab') return;
        // Keep Tab inside the dialog: a modal question whose focus can walk out
        // behind the backdrop is not answerable from the keyboard.
        var items = Array.prototype.filter.call(
          card.querySelectorAll(FOCUSABLE),
          function (node) { return !node.disabled && node.offsetParent !== null; }
        );
        if (!items.length) return;
        var first = items[0];
        var last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }

      cancel.addEventListener('click', function () { close(false); });
      confirm.addEventListener('click', function () { close(true); });
      overlay.addEventListener('mousedown', function (event) {
        if (event.target === overlay) close(false);
      });
      document.addEventListener('keydown', onKeydown, true);
      // Cancel, not the destructive action, receives focus.
      cancel.focus();
    });
  }

  /**
   * Copy helper. navigator.clipboard is unavailable on insecure origins and in
   * some in-app browsers, so a selection-based fallback keeps Copy working
   * rather than silently doing nothing.
   */
  function ssCopy(text, successMessage) {
    function done() { ssToast(successMessage || 'Copied.', 'success'); }
    function fail() { ssToast('Could not copy automatically. Select the link and copy it.', 'error'); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fail);
      return;
    }
    try {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', 'readonly');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand('copy');
      area.remove();
      if (ok) { done(); } else { fail(); }
    } catch (error) {
      fail();
    }
  }

  /**
   * Declarative wiring so pages do not each hand-roll a submit handler:
   *
   *   <form data-ss-confirm="Delete X? This cannot be undone."
   *         data-ss-confirm-title="Delete this ScanStory?"
   *         data-ss-confirm-label="Delete"
   *         data-ss-confirm-destructive>
   *
   * The submit is cancelled, the question is asked, and the form is only
   * re-submitted if the creator says yes - the same gate window.confirm gave,
   * with a focus-trapped, screen-reader-announced dialog instead.
   */
  function wireConfirmForms(root) {
    (root || document).querySelectorAll('form[data-ss-confirm]').forEach(function (form) {
      if (form.dataset.ssConfirmWired === '1') return;
      form.dataset.ssConfirmWired = '1';
      form.addEventListener('submit', function (event) {
        if (form.dataset.ssConfirmed === '1') return;
        event.preventDefault();
        ssConfirm({
          title: form.getAttribute('data-ss-confirm-title') || 'Are you sure?',
          body: form.getAttribute('data-ss-confirm'),
          confirmLabel: form.getAttribute('data-ss-confirm-label') || 'Continue',
          destructive: form.hasAttribute('data-ss-confirm-destructive')
        }).then(function (answer) {
          if (!answer) return;
          form.dataset.ssConfirmed = '1';
          form.requestSubmit ? form.requestSubmit() : form.submit();
        });
      });
    });
  }

  window.ssToast = ssToast;
  window.ssConfirm = ssConfirm;
  window.ssCopy = ssCopy;
  window.ssWireConfirmForms = wireConfirmForms;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { wireConfirmForms(document); });
  } else {
    wireConfirmForms(document);
  }
})(window, document);
