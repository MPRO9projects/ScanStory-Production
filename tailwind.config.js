/**
 * Tailwind CLI config for ScanStory's local production build.
 *
 * Replaces the runtime `cdn.tailwindcss.com` script across every template that used it.
 * This is a CDN-to-local-build migration only - it must not change any visual output,
 * so `content` intentionally globs the *entire* templates tree (not just the creator
 * pages this agent owns) plus static/js, so no class used anywhere in the app gets
 * purged from the compiled stylesheet.
 */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/js/**/*.js',
  ],
  // No dynamically-computed (string-concatenated) Tailwind class names were found in any
  // template's inline scripts or in static/js during the CDN migration audit - every
  // classList.add(...)/classList.toggle(...) call site uses a literal string ('hidden',
  // 'active', 'fa-plus', etc.) that already appears verbatim in the scanned file text, so
  // Tailwind's static scanner sees it without help. Safelist left empty on purpose; add an
  // entry here (with a comment citing the call site) if a future change introduces a
  // computed class name Tailwind can no longer see statically.
  safelist: [],
  theme: {
    extend: {},
  },
  plugins: [],
};
