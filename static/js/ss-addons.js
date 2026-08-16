/* ScanStory V1.1 - shared add-on purchase surface.
 *
 * One implementation drives both commercial surfaces because they are the
 * same three calls in the same order:
 *
 *   GET  /api/addons/catalog                    -> what is actually on sale
 *   POST /api/addons/orders                     -> Razorpay order
 *   POST /api/addons/purchases/<id>/verify      -> the EXISTING verifier
 *
 * Nothing here prices, totals or names an item locally: every rupee, every
 * pack size and every label is read from the catalog response, so a catalog
 * change is live without touching this file. After a successful verification
 * the caller's authoritative summary endpoint is re-fetched rather than the
 * client patching its own numbers from the success callback.
 *
 * ponytail: no bundler, no framework - two pages need this, a plain IIFE on
 * window is the whole requirement. Promote to a module when a third does.
 */
(function (global) {
  'use strict';

  function money(amount, currency) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: currency || 'INR',
        maximumFractionDigits: 2,
      }).format(Number(amount));
    } catch (e) {
      // Unknown currency code: show the code rather than inventing a symbol.
      return (currency || '') + ' ' + Number(amount).toFixed(2);
    }
  }

  function bytes(amount) {
    const value = Number(amount || 0);
    if (!Number.isFinite(value) || value <= 0) return '';
    let size = value;
    const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
    for (let i = 0; i < units.length; i += 1) {
      if (size < 1024 || i === units.length - 1) {
        return (size % 1 === 0 ? String(size) : size.toFixed(1)) + ' ' + units[i];
      }
      size /= 1024;
    }
    return '';
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setStatus(root, message, tone) {
    const box = root.querySelector('[data-ss-addon-status]');
    if (!box) return;
    box.textContent = message || '';
    box.hidden = !message;
    box.dataset.tone = tone || 'info';
  }

  function jsonFetch(url, options) {
    return fetch(url, options).then(async (response) => {
      let payload = null;
      try {
        payload = await response.json();
      } catch (e) {
        /* A permission redirect or an error page - not JSON. */
      }
      return { ok: response.ok, status: response.status, payload: payload };
    });
  }

  function SSAddons() {}

  /**
   * config:
   *   root         container element (must contain [data-ss-addon-list] and
   *                [data-ss-addon-status])
   *   addonType    'PROJECT_CAPACITY' | 'PROJECT_SERVICE_COVERAGE' | 'ACCOUNT_STORAGE'
   *   projectId    required for project-targeted types, otherwise omitted
   *   csrfToken    value of {{ csrf_token() }}
   *   summaryUrl   authoritative state endpoint re-fetched after purchase
   *   onSummary    fn(summaryPayload) -> re-render the numbers on screen
   *   disabled     when true, show disabledReason and never offer a CTA
   *   disabledReason
   */
  SSAddons.mount = function (config) {
    const root = config.root;
    if (!root) return;
    const list = root.querySelector('[data-ss-addon-list]');
    if (!list) return;

    if (config.disabled) {
      list.replaceChildren(el('p', 'ss-addon-note', config.disabledReason || 'Not available right now.'));
      return;
    }

    setStatus(root, 'Loading options…', 'info');
    list.replaceChildren();

    jsonFetch('/api/addons/catalog', { headers: { Accept: 'application/json' } })
      .then(({ ok, payload }) => {
        if (!ok || !payload || !payload.success) {
          setStatus(root, 'Could not load purchase options. Please refresh and try again.', 'error');
          return;
        }
        // The backend already filters to active + commercially available; this
        // only narrows to the type this surface sells.
        const items = (payload.addons || []).filter((item) => item.addon_type === config.addonType);
        if (!items.length) {
          setStatus(root, '', 'info');
          list.replaceChildren(el('p', 'ss-addon-note', 'No options are on sale right now.'));
          return;
        }
        setStatus(root, '', 'info');
        items.forEach((item) => list.appendChild(renderItem(item, config, root)));
      })
      .catch(() => setStatus(root, 'Network error while loading purchase options.', 'error'));
  };

  function renderItem(item, config, root) {
    const card = el('div', 'ss-addon-item');

    const head = el('div', 'ss-addon-item-head');
    head.appendChild(el('span', 'ss-addon-item-name', item.name));
    head.appendChild(el('span', 'ss-addon-item-price', money(item.unit_amount, item.currency)));
    card.appendChild(head);

    if (item.description) card.appendChild(el('p', 'ss-addon-item-desc', item.description));
    if (config.addonType === 'ACCOUNT_STORAGE' && item.storage_bytes_delta) {
      card.appendChild(el('p', 'ss-addon-item-desc', 'Adds ' + bytes(item.storage_bytes_delta) + ' account storage.'));
    }

    const button = el('button', 'ss-addon-buy', 'Continue');
    button.type = 'button';
    button.setAttribute(
      'aria-label',
      'Buy ' + item.name + ' for ' + money(item.unit_amount, item.currency)
    );
    button.addEventListener('click', () => confirmAndBuy(item, config, root, button));
    card.appendChild(button);
    return card;
  }

  /* Pre-payment confirmation. The viewer sees item, what it gives them, the
     project it applies to (when project-targeted), price and currency before
     any checkout opens. */
  function confirmAndBuy(item, config, root, button) {
    const lines = ['You are buying:', '', item.name];
    if (item.description) lines.push(item.description);
    if (config.addonType === 'PROJECT_CAPACITY' && item.project_delta) {
      lines.push('Adds ' + item.project_delta + ' project slot(s) to your account.');
    }
    if (config.addonType === 'ACCOUNT_STORAGE' && item.storage_bytes_delta) {
      lines.push('Adds ' + bytes(item.storage_bytes_delta) + ' account storage.');
      lines.push('Existing projects and QR codes are not deleted when storage changes.');
    }
    if (config.addonType === 'PROJECT_SERVICE_COVERAGE') {
      if (item.validity_days_delta) lines.push('Extends coverage by ' + item.validity_days_delta + ' day(s).');
      lines.push('Applies to: ' + (config.projectName || 'this ScanStory') + ' only.');
      lines.push('This does not change your account plan or its renewal date.');
    }
    lines.push('', 'Total: ' + money(item.unit_amount, item.currency));
    if (!global.confirm(lines.join('\n'))) return;

    startCheckout(item, config, root, button);
  }

  function startCheckout(item, config, root, button) {
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = 'Starting…';
    setStatus(root, 'Preparing secure checkout…', 'info');

    const body = { catalog_id: item.id, quantity: 1 };
    // Project-targeted add-ons MUST carry the project id: the server re-binds
    // user + project + catalog item + amount at verification time, but only
    // if the client actually sent the project it means.
    if (config.projectId) body.project_id = config.projectId;

    jsonFetch('/api/addons/orders', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': config.csrfToken,
      },
      body: JSON.stringify(body),
    })
      .then(({ ok, status, payload }) => {
        button.disabled = false;
        button.textContent = originalText;
        if (!ok || !payload || !payload.success) {
          setStatus(root, orderErrorMessage(status, payload), 'error');
          return;
        }
        if (typeof global.Razorpay === 'undefined') {
          setStatus(root, 'Secure checkout could not load. Please refresh and try again.', 'error');
          return;
        }
        const checkout = new global.Razorpay({
          key: payload.key,
          amount: payload.amount,
          currency: payload.currency,
          name: payload.name,
          description: payload.description,
          order_id: payload.order_id,
          theme: { color: '#ff007a' },
          handler: function (response) {
            verify(payload.purchase_id, response, config, root);
          },
          modal: {
            ondismiss: function () {
              setStatus(root, 'Payment cancelled. Nothing was charged.', 'warn');
            },
          },
        });
        checkout.on('payment.failed', function (response) {
          const detail = (response && response.error && response.error.description) || '';
          setStatus(root, 'Payment failed. ' + (detail || 'Please try again.'), 'error');
        });
        checkout.open();
      })
      .catch(() => {
        button.disabled = false;
        button.textContent = originalText;
        setStatus(root, 'Network error. Please check your connection and try again.', 'error');
      });
  }

  function orderErrorMessage(status, payload) {
    const code = payload && payload.code;
    if (code === 'PAYMENT_NOT_CONFIGURED') return 'Payments are temporarily unavailable. Please try again later.';
    if (code === 'COVERAGE_ALREADY_INDEFINITE') return 'This ScanStory does not currently require standalone renewal.';
    if (code === 'PROJECT_NOT_FOUND') return 'This ScanStory is no longer available to you.';
    if (status === 403) return 'You do not have permission to make this purchase.';
    return (payload && payload.error) || 'Could not start the purchase. Please try again.';
  }

  function verify(purchaseId, response, config, root) {
    setStatus(root, 'Verifying payment…', 'info');
    const form = new URLSearchParams({
      razorpay_payment_id: response.razorpay_payment_id,
      razorpay_order_id: response.razorpay_order_id,
      razorpay_signature: response.razorpay_signature,
    });

    jsonFetch('/api/addons/purchases/' + purchaseId + '/verify', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': config.csrfToken,
      },
      body: form.toString(),
    })
      .then(({ ok, payload }) => {
        if (!ok || !payload || !payload.success) {
          setStatus(
            root,
            (payload && payload.error) || 'We could not confirm this payment. Please contact support before retrying.',
            'error'
          );
          return;
        }
        setStatus(root, 'Payment confirmed. Updating your account…', 'success');
        refreshSummary(config, root);
      })
      .catch(() =>
        setStatus(root, 'Payment taken but confirmation failed. Please refresh - do not pay again.', 'error')
      );
  }

  /* Never trust the success callback for the numbers on screen: re-read them
     from the server, which is the only place capacity/coverage is decided. */
  function refreshSummary(config, root) {
    if (!config.summaryUrl || typeof config.onSummary !== 'function') {
      global.location.reload();
      return;
    }
    jsonFetch(config.summaryUrl, { headers: { Accept: 'application/json' } })
      .then(({ ok, payload }) => {
        if (!ok || !payload || !payload.success) {
          global.location.reload();
          return;
        }
        config.onSummary(payload);
        setStatus(root, 'Done. Your updated details are shown above.', 'success');
      })
      .catch(() => global.location.reload());
  }

  global.SSAddons = SSAddons;
})(window);
