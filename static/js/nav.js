/**
 * Shared mobile menu (bottom-sheet/drawer) behavior for ScanStory user pages.
 * Presentation-only utility: no scanner/auth/business logic here.
 *
 * Expects markup:
 *   <button id="mobileMenuToggle" aria-controls="mobileMenuPanel" aria-expanded="false">
 *   <div id="mobileMenuPanel" class="ss-mobile-menu"> ... <button data-menu-close> ... </div>
 */
(function () {
  function initScanStoryMenu() {
    var toggle = document.getElementById('mobileMenuToggle');
    var panel = document.getElementById('mobileMenuPanel');
    if (!toggle || !panel) return;

    function isOpen() { return panel.classList.contains('is-open'); }

    function open() {
      panel.classList.add('is-open');
      document.body.classList.add('ss-menu-open');
      toggle.setAttribute('aria-expanded', 'true');
      toggle.setAttribute('aria-label', 'Close menu');
    }

    function close() {
      panel.classList.remove('is-open');
      document.body.classList.remove('ss-menu-open');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', 'Open menu');
    }

    toggle.addEventListener('click', function () {
      isOpen() ? close() : open();
    });

    panel.querySelectorAll('[data-menu-close]').forEach(function (el) {
      el.addEventListener('click', close);
    });

    panel.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', close);
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen()) close();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initScanStoryMenu);
  } else {
    initScanStoryMenu();
  }
})();
