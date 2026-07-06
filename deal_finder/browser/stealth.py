"""Fingerprint hardening applied via an init script that runs before page scripts.

The decisive levers against modern bot detection are: a REAL Chrome channel
(channel="chrome"), headful, a persistent on-disk profile, and a real residential IP.
This JS only closes the cheap "headless automation" tells. If a site still detects,
the escalation path (documented, not a default dependency) is patchright / a residential
proxy — see BrowserConfig.proxy.
"""

from __future__ import annotations

# Runs via context.add_init_script — before any page script, on every document.
STEALTH_JS = r"""
(() => {
  try {
    // navigator.webdriver -> undefined
    Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['de-CH', 'de', 'en'] });
  } catch (e) {}
  try {
    // Non-empty plugins/mimeTypes (empty arrays are a headless tell)
    const make = (n) => { const a = new Array(n); for (let i=0;i<n;i++) a[i]={name:'Plugin'+i}; return a; };
    Object.defineProperty(navigator, 'plugins', { get: () => make(3) });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => make(2) });
  } catch (e) {}
  try {
    if (!window.chrome) { window.chrome = { runtime: {} }; }
  } catch (e) {}
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) {
      navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission === 'denied' ? 'denied' : 'prompt' })
          : orig(p);
    }
  } catch (e) {}
  try {
    // WebGL vendor/renderer -> realistic Apple values
    const patch = (proto) => {
      const gp = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return 'Google Inc. (Apple)';                 // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return 'ANGLE (Apple, Apple M-series, OpenGL 4.1)'; // UNMASKED_RENDERER_WEBGL
        return gp.apply(this, [p]);
      };
    };
    if (window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  } catch (e) {}
})();
"""

# A current, realistic macOS Safari UA, used when BrowserConfig.engine == "webkit".
# Real WebKit (Playwright's webkit browser type -- Safari's actual engine, not Chrome
# pretending to be Safari) was confirmed via direct testing to sail through Ricardo's
# Cloudflare challenge with zero stealth patches, while patched/sandboxed Chromium (even
# with a real Chrome binary) still gets challenged. Cloudflare's bot management appears to
# specifically target Chrome-family CDP automation; a genuinely different browser engine
# sidesteps that class of detection entirely. Overridable via DF_BROWSER_USER_AGENT.
DEFAULT_SAFARI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)

# A current, realistic macOS Chrome UA, used when BrowserConfig.engine == "chromium".
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
