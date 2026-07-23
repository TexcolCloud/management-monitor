const { chromium } = require('playwright');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const path = require('path');
const readline = require('readline');
const { extractAdminToken } = require('../common/auth-runtime');
const { loadRuntimeConfig } = require('../common/runtime-config');

const runtimeConfig = loadRuntimeConfig();
const DEFAULT_PORTAL_URL = runtimeConfig.portalLoginUrl;
const DEFAULT_DATA_DIR = runtimeConfig.captureRoot;
const DEFAULT_PROFILE_DIR = runtimeConfig.loginProfileDir;
const DEFAULT_AUTO_WAIT_MS = runtimeConfig.loginWaitMs;
const DEFAULT_POST_LOGIN_SETTLE_MS = runtimeConfig.postLoginSettleMs;
const DEFAULT_REPLAY_CLICK_DELAY_MS = runtimeConfig.replayClickDelayMs;
const DEFAULT_REPLAY_STEP_TIMEOUT_MS = runtimeConfig.replayStepTimeoutMs;
const managementApiUrl = new URL(runtimeConfig.listApiUrl);
const MANAGEMENT_API_PATH = managementApiUrl.pathname;
const BRIDGE_REQUEST_HEADER = 'x-workorder-browser-bridge';
const MAX_BRIDGE_REQUEST_BYTES = 1024 * 1024;
const SENSITIVE_HEADER_NAMES = new Set([
  'authorization',
  'cookie',
  'proxy-authorization',
  'set-cookie',
  'x-auth-token',
  'x-access-token'
]);
const SENSITIVE_FIELD_PATTERN = /password|passwd|secret|token|cookie|authorization|open.?id|user.?id/i;
const PORTAL_AUTH_COOKIE_NAMES = new Set(['ffToken', 'LtpaToken', 'MssSsoToken', 'user']);
const LOGIN_SELECTOR_MARKERS = [
  'account-login-form',
  'login-form',
  'staffaccount',
  'password',
  'tosendmsg',
  'input#mac'
];

const args = process.argv.slice(2);

if (args.includes('--help') || args.includes('-h')) {
  console.log(`
Usage:
  node record-portal-clicks.js [options]

Open the portal in Playwright Chromium and record your clicks, navigations,
and the daily-management list request body after you reach the target page.

Options:
  --portal-url=<url>       Defaults to the portal.login_url runtime setting.
  --data-dir=<dir>         Defaults to capture-output/daily-management.
  --profile-dir=<dir>      Deprecated compatibility option; browser state is never persisted.
  --out-file=<file>        Defaults to <data-dir>/portal-click-trace.json.
  --filter-file=<file>     Defaults to <data-dir>/daily-management-request.json.
  --auto-wait-ms=<n>       Wait this long instead of pressing Enter. Defaults to 300000.
  --target-url=<url>       After portal login is detected, open this URL.
                           Debug only; daily capture should use portal click replay/manual clicks.
  --post-login-settle-ms=<n>
                           Wait this long after target login is detected. Defaults to 5000.
  --replay-file=<file>     Replay recorded portal clicks after login.
  --save-replay-file=<file>
                           Save replayable clicks here after a successful capture.
  --overwrite-replay-file  Allow replacing an existing replay file.
  --freeze-replay-file     Set the replay file to read-only after saving.
  --replay-click-delay-ms=<n>
                           Delay after each replayed click. Defaults to 800.
  --replay-step-timeout-ms=<n>
                           Timeout for each replayed selector/text click. Defaults to 12000.
  --require-token          Exit with an error if no Admin-Token is detected in browser memory.
  --require-request        Exit with an error if no daily-management request is captured.
  --stop-on-request        Stop recording after a daily-management request is captured.
  --headless               Run headless.
  --keep-window-after-login
                            Keep the visible browser window open after login for debugging.
  --keep-session           Keep the logged-in browser session running after capture.
  --wait-for-login         Wait indefinitely for login and the required portal request.
  --bridge-port=<n>        Local browser request bridge port. Use 0 for a free port.
  --bridge-ready-file=<file>
                            Write the local bridge URL here after the session is ready.
`);
  process.exit(0);
}

function getArg(name, fallback) {
  const prefix = `--${name}=`;
  const found = args.find((item) => item.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}

function hasFlag(name) {
  return args.includes(`--${name}`);
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function waitForEnter(message) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(message, () => {
      rl.close();
      resolve();
    });
  });
}

function parseJsonBody(value) {
  if (!value) return null;

  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function sanitizeUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return '';
  }
}

function sanitizeLogText(value) {
  return String(value || '')
    .replace(/https?:\/\/[^\s"'<>]+/gi, (url) => sanitizeUrl(url))
    .replace(
      /([?&#](?:access[_-]?token|token|app[_-]?secret|password|signature|ticket|code|auth(?:orization)?|session|jwt|key)=)[^&#\s"'<>]*/gi,
      '$1<redacted>'
    )
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer <redacted>')
    .replace(
      /((?:authorization|cookie|set-cookie)\s*[:=]\s*)[^\s,;]+/gi,
      '$1<redacted>'
    );
}

function extractBearerAuthorization(headers) {
  const value = String(
    headers?.Authorization || headers?.authorization || headers?.AUTHORIZATION || ''
  ).trim();
  if (!/^Bearer\s+[^\r\n]+$/i.test(value) || value.length > 8192) return '';
  return value;
}

function sanitizeHeaders(headers) {
  const sanitized = {};
  for (const [key, value] of Object.entries(headers || {})) {
    const normalized = key.toLowerCase();
    if (SENSITIVE_HEADER_NAMES.has(normalized) || normalized.includes('token')) continue;
    sanitized[key] = sanitizeLogText(value);
  }
  return sanitized;
}

function sanitizeReplaySelector(value) {
  const selector = sanitizeLogText(value);
  if (/\b(?:href|src|action)\s*=/i.test(selector)) return '';
  return selector;
}

function sanitizeStructuredValue(value, key = '') {
  if (SENSITIVE_FIELD_PATTERN.test(String(key || ''))) return '<redacted>';
  if (Array.isArray(value)) return value.map((item) => sanitizeStructuredValue(item));
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, childValue]) => [
        childKey,
        sanitizeStructuredValue(childValue, childKey)
      ])
    );
  }
  if (typeof value === 'string') return sanitizeLogText(value);
  return value;
}

function sanitizeTraceEvent(event) {
  const sanitized = sanitizeStructuredValue(event);
  delete sanitized.value;
  if (['input', 'textarea', 'select'].includes(String(sanitized.tagName || '').toLowerCase())) {
    sanitized.text = '';
  }
  for (const key of ['url', 'pageUrl', 'frameUrl', 'href']) {
    if (sanitized[key]) sanitized[key] = sanitizeUrl(sanitized[key]);
  }
  if (sanitized.selector) sanitized.selector = sanitizeReplaySelector(sanitized.selector);
  if (sanitized.headers) sanitized.headers = sanitizeHeaders(sanitized.headers);
  delete sanitized.responsePreview;
  return sanitized;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractCapturedRequestToken(trace) {
  const headers = trace.latestDailyManagementRequest?.headers || {};
  const authorization = extractBearerAuthorization(headers);
  const match = String(authorization).match(/^Bearer\s+(.+)$/i);
  return match
    ? { token: match[1], source: 'captured-request:authorization' }
    : { token: '', source: '' };
}

function hasPortalLoginCookie(storage) {
  return (storage.cookies || []).some((cookie) => {
    const domain = String(cookie.domain || '');
    return domain.includes('workorder.com') && PORTAL_AUTH_COOKIE_NAMES.has(cookie.name) && cookie.value;
  });
}

function normalizeSpaces(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function isTargetLoginUrl(value) {
  const text = String(value || '');
  return text.includes(`${managementApiUrl.origin}/admin/#/login`);
}

function isLoginFormClick(event) {
  const haystack = [
    event.selector,
    event.id,
    event.name,
    event.text,
    event.titleAttr,
    event.ariaLabel
  ]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');

  return LOGIN_SELECTOR_MARKERS.some((marker) => haystack.includes(marker));
}

function hasReplayTarget(event) {
  return Boolean(
    event.selector ||
      event.text ||
      event.href ||
      (Number.isFinite(Number(event.x)) && Number.isFinite(Number(event.y)))
  );
}

function isReplayableClick(event) {
  if (!event || event.type !== 'user-click') return false;
  if (isLoginFormClick(event)) return false;
  if (isTargetLoginUrl(event.pageUrl) || isTargetLoginUrl(event.url) || isTargetLoginUrl(event.frameUrl)) {
    return false;
  }
  return hasReplayTarget(event);
}

function loadReplayEvents(file) {
  if (!file) return [];
  if (!fs.existsSync(file)) {
    console.log(`Replay file not found; waiting for manual portal clicks: ${file}`);
    return [];
  }

  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    const events = Array.isArray(parsed.events)
      ? parsed.events.map((event) => sanitizeTraceEvent(event))
      : [];
    const replayable = events.filter(isReplayableClick);
    console.log(`Loaded ${replayable.length} replayable portal clicks from ${file}`);
    if (!replayable.length) {
      console.log('Replay file has no usable portal clicks; waiting for manual navigation.');
    }
    return replayable;
  } catch (error) {
    console.log(
      `Failed to read replay file; waiting for manual portal clicks: ${sanitizeLogText(error.message || error)}`
    );
    return [];
  }
}

function setReplayFileWritable(file) {
  try {
    fs.chmodSync(file, 0o666);
  } catch {
    // Best effort only. The no-overwrite guard below is the real protection.
  }
}

function freezeReplayFile(file) {
  try {
    fs.chmodSync(file, 0o444);
  } catch (error) {
    console.log(
      `Replay file saved, but read-only flag could not be set: ${sanitizeLogText(error.message || error)}`
    );
  }
}

function compactReplayEvent(event) {
  const compact = {
    type: 'user-click',
    selector: sanitizeReplaySelector(event.selector || ''),
    text: normalizeSpaces(event.text || event.titleAttr || event.ariaLabel),
    href: sanitizeUrl(event.href || '')
  };

  if (Number.isFinite(Number(event.x)) && Number.isFinite(Number(event.y))) {
    compact.x = Math.round(Number(event.x));
    compact.y = Math.round(Number(event.y));
  }

  return compact;
}

function writeReplayFile(file, trace, outFile, options) {
  if (!file || !trace.latestDailyManagementRequest) return;

  const events = (trace.events || []).filter(isReplayableClick).map(compactReplayEvent);
  if (!events.length) {
    console.log('No replayable portal clicks recorded; navigation replay file was not updated.');
    return;
  }

  ensureDir(path.dirname(file));
  if (fs.existsSync(file) && !options.overwrite) {
    console.log(`Navigation replay file already exists and was not overwritten: ${file}`);
    return;
  }
  if (fs.existsSync(file) && options.overwrite) {
    setReplayFileWritable(file);
  }

  fs.writeFileSync(
    file,
    `${JSON.stringify(
      {
        version: 1,
        name: 'workorder-daily-management-navigation',
        note: `Replay these clicks only after logging in through ${sanitizeUrl(DEFAULT_PORTAL_URL)}. This file intentionally contains no credentials.`,
        events
      },
      null,
      2
    )}\n`,
    'utf8'
  );
  console.log(`Navigation replay file saved: ${file}`);
  if (options.freeze) {
    freezeReplayFile(file);
    console.log(`Navigation replay file marked read-only: ${file}`);
  }
}

async function portalLooksLoggedIn(page) {
  if (!page || page.isClosed()) return false;
  if (!page.url().includes('workorder.com')) return false;

  try {
    const loginFields = await page
      .locator('form#account-login-form, #staffAccount, #passWord, #toSendMsg')
      .count();
    return loginFields === 0;
  } catch {
    return false;
  }
}

async function isPortalLoginReady(context, page) {
  const storage = await context.storageState().catch(() => ({ cookies: [], origins: [] }));
  return hasPortalLoginCookie(storage) || (await portalLooksLoggedIn(page));
}

function playableText(event) {
  const text = normalizeSpaces(event.text || event.titleAttr || event.ariaLabel);
  if (!text || text.length > 80) return '';
  return text;
}

async function clickSelectorInFrame(frame, selector, timeout) {
  if (!selector) return false;
  try {
    const locator = frame.locator(selector).first();
    await locator.waitFor({ state: 'visible', timeout });
    await locator.click({ timeout });
    return true;
  } catch {
    return false;
  }
}

async function clickTextInFrame(frame, text, timeout) {
  if (!text) return false;
  try {
    const locator = frame.getByText(text, { exact: false }).first();
    await locator.waitFor({ state: 'visible', timeout });
    await locator.click({ timeout });
    return true;
  } catch {
    return false;
  }
}

async function clickRecordedEvent(page, event, timeout) {
  for (const frame of page.frames()) {
    if (await clickSelectorInFrame(frame, event.selector, timeout)) return 'selector';
  }

  const text = playableText(event);
  for (const frame of page.frames()) {
    if (await clickTextInFrame(frame, text, timeout)) return 'text';
  }

  if (Number.isFinite(Number(event.x)) && Number.isFinite(Number(event.y))) {
    try {
      await page.mouse.click(Number(event.x), Number(event.y));
      return 'coordinates';
    } catch {
      return '';
    }
  }

  return '';
}

async function replayPortalClicks(context, getActivePage, replayEvents, options) {
  if (!replayEvents.length) return;

  console.log(`Replaying ${replayEvents.length} recorded portal clicks.`);
  for (let index = 0; index < replayEvents.length; index += 1) {
    const event = replayEvents[index];
    let page = getActivePage();
    if (!page || page.isClosed()) {
      page = context.pages().find((candidate) => !candidate.isClosed());
    }
    if (!page) {
      console.log(`Replay stopped at step ${index + 1}: no active page.`);
      return;
    }

    await page.waitForLoadState('domcontentloaded', { timeout: 5000 }).catch(() => undefined);
    const beforePages = new Set(context.pages());
    const method = await clickRecordedEvent(page, event, options.stepTimeoutMs);
    if (!method) {
      console.log(
        `Replay skipped step ${index + 1}: ${sanitizeLogText(event.text || event.selector || event.href || '(unknown)')}`
      );
      continue;
    }

    console.log(
      `Replay step ${index + 1}/${replayEvents.length} via ${method}: ${sanitizeLogText(event.text || event.selector)}`
    );
    await sleep(options.clickDelayMs);

    const openedPages = context.pages().filter((candidate) => !beforePages.has(candidate));
    const newestPage = openedPages[openedPages.length - 1];
    if (newestPage) {
      await newestPage.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => undefined);
    }
  }
}

async function gotoWithHttpFallback(page, url, options) {
  try {
    await page.goto(url, options);
    return url;
  } catch (error) {
    if (!url.startsWith('https://')) throw error;

    const httpUrl = `http://${url.slice('https://'.length)}`;
    console.log(`HTTPS failed for ${sanitizeUrl(url)}; retrying ${sanitizeUrl(httpUrl)}`);
    await page.goto(httpUrl, options);
    return httpUrl;
  }
}

function shouldMinimizeAfterLogin(headless, keepWindowAfterLogin, replayEvents, targetUrl) {
  return !headless && !keepWindowAfterLogin && Boolean(replayEvents.length || targetUrl);
}

function isBrowserBridgeRequest(headers) {
  return String(headers[BRIDGE_REQUEST_HEADER] || '').trim() === '1';
}

function frameMatchesTargetOrigin(frame, targetUrl) {
  try {
    return (
      frame &&
      !frame.isDetached() &&
      new URL(frame.url()).origin === new URL(String(targetUrl || '')).origin
    );
  } catch {
    return false;
  }
}

function findBrowserRequestContext(browserContext, preferredFrame, targetUrl) {
  if (frameMatchesTargetOrigin(preferredFrame, targetUrl)) return preferredFrame;
  const pages = browserContext.pages().filter((page) => !page.isClosed()).reverse();
  for (const page of pages) {
    const frames = page.frames().slice().reverse();
    const matching = frames.find((frame) => frameMatchesTargetOrigin(frame, targetUrl));
    if (matching) return matching;
  }
  return null;
}

async function minimizeBrowserWindow(page) {
  try {
    const session = await page.context().newCDPSession(page);
    const { windowId } = await session.send('Browser.getWindowForTarget');
    await session.send('Browser.setWindowBounds', {
      windowId,
      bounds: { windowState: 'minimized' }
    });
    await session.detach();
    return true;
  } catch (error) {
    console.log(
      `Could not minimize browser window after login: ${sanitizeLogText(error.message || error)}`
    );
    return false;
  }
}

function bridgeTokenMatches(headers, expectedToken) {
  const supplied = String(headers.authorization || '').replace(/^Bearer\s+/i, '');
  const expected = Buffer.from(String(expectedToken || ''));
  const actual = Buffer.from(supplied);
  return expected.length > 0 && expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}

function isAllowedBridgeTarget(value, allowedTargets) {
  let target;
  try {
    target = new URL(String(value || ''));
  } catch {
    return false;
  }
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) return false;
  return allowedTargets.some((allowedValue) => {
    const allowed = new URL(allowedValue);
    return (
      target.origin === allowed.origin &&
      (target.pathname === allowed.pathname || target.pathname.startsWith(`${allowed.pathname}/`))
    );
  });
}

async function readBridgeRequestBody(request) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > MAX_BRIDGE_REQUEST_BYTES) throw new Error('bridge request body is too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function executeBrowserRequest(
  page,
  payload,
  binary,
  fallbackAuthorization = '',
  requireAuthorization = false
) {
  return page.evaluate(
    async ({
      url,
      method,
      headers,
      body: requestBody,
      returnBinary,
      timeoutMs,
      capturedAuthorization,
      requireAuthorization
    }) => {
      const browserHeaders = { ...headers };
      for (const key of Object.keys(browserHeaders)) {
        if (['cookie', 'host', 'authorization', 'proxy-authorization', 'content-length'].includes(key.toLowerCase())) {
          delete browserHeaders[key];
        }
      }
      let adminToken = '';
      try {
        adminToken =
          window.localStorage.getItem('Admin-Token') ||
          window.localStorage.getItem('token') ||
          window.localStorage.getItem('Token') ||
          window.sessionStorage.getItem('Admin-Token') ||
          window.sessionStorage.getItem('token') ||
          window.sessionStorage.getItem('Token') ||
          '';
      } catch {
        adminToken = '';
      }
      if (adminToken) {
        browserHeaders.Authorization = /^Bearer\s+/i.test(adminToken)
          ? adminToken
          : `Bearer ${adminToken}`;
      } else if (/^Bearer\s+[^\r\n]+$/i.test(capturedAuthorization)) {
        browserHeaders.Authorization = capturedAuthorization;
      }
      if (requireAuthorization && !browserHeaders.Authorization) {
        throw new Error('authenticated browser request credentials are unavailable');
      }
      browserHeaders['x-workorder-browser-bridge'] = '1';
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const upstream = await fetch(url, {
          method,
          headers: browserHeaders,
          body: requestBody === null ? undefined : JSON.stringify(requestBody),
          credentials: 'include',
          signal: controller.signal
        });
        if (!returnBinary) {
          return { status: upstream.status, text: await upstream.text() };
        }
        const bytes = new Uint8Array(await upstream.arrayBuffer());
        let encoded = '';
        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
          encoded += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
        }
        return {
          status: upstream.status,
          contentType: upstream.headers.get('content-type') || 'application/octet-stream',
          bodyBase64: btoa(encoded)
        };
      } finally {
        clearTimeout(timeout);
      }
    },
    {
      ...payload,
      headers: sanitizeHeaders(payload.headers),
      returnBinary: binary,
      capturedAuthorization: extractBearerAuthorization({ authorization: fallbackAuthorization }),
      requireAuthorization: Boolean(requireAuthorization)
    }
  );
}

async function startBrowserRequestBridge(getPage, port, options) {
  const allowedTargets = options.allowedTargets.map((value) => String(value));
  const requestTimeoutMs = Number.isFinite(options.requestTimeoutMs)
    ? Math.max(1, Math.trunc(options.requestTimeoutMs))
    : 60000;
  const server = http.createServer(async (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    if (!bridgeTokenMatches(request.headers, options.token)) {
      response.writeHead(401, { 'Content-Type': 'application/json' });
      response.end('{"error":"unauthorized"}');
      return;
    }
    if (request.method === 'GET' && request.url === '/health') {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{"ready":true}');
      return;
    }
    const binary = request.url === '/binary';
    if (request.method !== 'POST' || (!binary && request.url !== '/request')) {
      response.writeHead(404).end();
      return;
    }
    try {
      const payload = JSON.parse(await readBridgeRequestBody(request));
      const method = String(payload.method || '').toUpperCase();
      if (!['GET', 'POST'].includes(method)) throw new Error('bridge method is not allowed');
      if (!isAllowedBridgeTarget(payload.url, allowedTargets)) {
        response.writeHead(403, { 'Content-Type': 'application/json' });
        response.end('{"error":"target is not allowed"}');
        return;
      }
      const executionContext = getPage(payload.url);
      const unavailable =
        !executionContext ||
        (typeof executionContext.isClosed === 'function' && executionContext.isClosed()) ||
        (typeof executionContext.isDetached === 'function' && executionContext.isDetached());
      if (unavailable) throw new Error('logged-in browser page is unavailable');
      let fallbackAuthorization = '';
      try {
        fallbackAuthorization = extractBearerAuthorization({
          authorization:
            typeof options.getAuthorization === 'function' ? options.getAuthorization() : ''
        });
      } catch {
        fallbackAuthorization = '';
      }
      const result = await executeBrowserRequest(
        executionContext,
        { ...payload, method, timeoutMs: requestTimeoutMs },
        binary,
        fallbackAuthorization,
        Boolean(options.requireUpstreamAuthorization)
      );
      if (binary) {
        response.writeHead(result.status, { 'Content-Type': result.contentType });
        response.end(Buffer.from(result.bodyBase64, 'base64'));
      } else {
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify(result));
      }
    } catch (error) {
      response.writeHead(502, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: sanitizeLogText(error.message || error).slice(0, 300) }));
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  const address = server.address();
  return { server, url: `http://127.0.0.1:${address.port}` };
}

function waitForSessionShutdown(server) {
  return new Promise((resolve) => {
    const shutdown = () => server.close(resolve);
    process.once('SIGINT', shutdown);
    process.once('SIGTERM', shutdown);
  });
}

function summarizeBody(body) {
  return [
    `pageNo=${body.pageNo ?? ''}`,
    `pageSize=${body.pageSize ?? ''}`,
    `institution=${body.institution ?? ''}`,
    `deptId=${body.deptId ?? ''}`,
    `newDeptId=${body.newDeptId ?? ''}`,
    `theme=${body.theme ?? ''}`,
    `safetyCode=${body.safetyCode ?? ''}`
  ].join(', ');
}

async function main() {
  const portalUrl = getArg('portal-url', DEFAULT_PORTAL_URL);
  const dataDir = path.resolve(getArg('data-dir', DEFAULT_DATA_DIR));
  getArg('profile-dir', DEFAULT_PROFILE_DIR); // Accepted for CLI compatibility; never used.
  const outFile = path.resolve(getArg('out-file', path.join(dataDir, 'portal-click-trace.json')));
  const filterFile = path.resolve(
    getArg('filter-file', path.join(dataDir, 'daily-management-request.json'))
  );
  const autoWaitMs = Number(getArg('auto-wait-ms', String(DEFAULT_AUTO_WAIT_MS)));
  const postLoginSettleMs = Number(getArg('post-login-settle-ms', String(DEFAULT_POST_LOGIN_SETTLE_MS)));
  const targetUrl = getArg('target-url', '');
  const replayFile = getArg('replay-file', '');
  const saveReplayFile = getArg('save-replay-file', '');
  const replayClickDelayMs = Number(getArg('replay-click-delay-ms', String(DEFAULT_REPLAY_CLICK_DELAY_MS)));
  const replayStepTimeoutMs = Number(getArg('replay-step-timeout-ms', String(DEFAULT_REPLAY_STEP_TIMEOUT_MS)));
  const requireToken = hasFlag('require-token');
  const requireRequest = hasFlag('require-request');
  const stopOnRequest = hasFlag('stop-on-request');
  const overwriteReplayFile = hasFlag('overwrite-replay-file');
  const shouldFreezeReplayFile = hasFlag('freeze-replay-file');
  const headless = hasFlag('headless');
  const keepWindowAfterLogin = hasFlag('keep-window-after-login');
  const keepSession = hasFlag('keep-session');
  const waitForLogin = hasFlag('wait-for-login');
  const bridgePort = Number(getArg('bridge-port', '0'));
  const bridgeReadyFile = getArg('bridge-ready-file', '');
  const replayEvents = loadReplayEvents(replayFile ? path.resolve(replayFile) : '');

  ensureDir(dataDir);
  ensureDir(path.dirname(outFile));
  ensureDir(path.dirname(filterFile));

  const trace = {
    startedAt: new Date().toISOString(),
    portalUrl: sanitizeUrl(portalUrl),
    events: [],
    latestDailyManagementRequest: null
  };

  function saveTrace() {
    fs.writeFileSync(outFile, `${JSON.stringify(trace, null, 2)}\n`, 'utf8');
  }

  function addEvent(event) {
    trace.events.push({
      index: trace.events.length + 1,
      time: new Date().toISOString(),
      ...sanitizeTraceEvent(event)
    });
    saveTrace();
  }

  const browser = await chromium.launch({ headless });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    ignoreHTTPSErrors: true
  });

  await context.exposeBinding('__portalClickRecorder', async (source, payload) => {
    addEvent({
      type: 'user-click',
      pageUrl: source.page?.url(),
      frameUrl: source.frame?.url(),
      ...payload
    });
    console.log(
      `Click: ${sanitizeLogText(payload.text || payload.selector || payload.tagName || '(unknown)')}`
    );
  });

  await context.addInitScript(() => {
    if (window.__portalClickRecorderInstalled) return;
    window.__portalClickRecorderInstalled = true;

    function shorten(value, length) {
      return String(value || '').replace(/\s+/g, ' ').trim().slice(0, length);
    }

    function cssEscape(value) {
      if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
      return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
    }

    function simpleSelector(element) {
      if (!element || !element.tagName) return '';
      const tag = element.tagName.toLowerCase();
      const id = element.getAttribute('id');
      if (id) return `${tag}#${cssEscape(id)}`;

      const stableAttrs = ['data-v', 'data-id', 'data-key', 'name', 'title', 'aria-label'];
      for (const attr of stableAttrs) {
        const value = element.getAttribute(attr);
        if (value && value.length < 120) return `${tag}[${attr}="${value.replace(/"/g, '\\"')}"]`;
      }

      const classes = Array.from(element.classList || [])
        .filter((name) => !/^is-|^active$|^hover$/.test(name))
        .slice(0, 3);
      if (classes.length) return `${tag}.${classes.map(cssEscape).join('.')}`;

      return tag;
    }

    function domPath(element) {
      const parts = [];
      let current = element;

      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
        let part = simpleSelector(current);
        const parent = current.parentElement;
        if (parent) {
          const siblings = Array.from(parent.children).filter(
            (item) => item.tagName === current.tagName
          );
          if (siblings.length > 1 && !current.id) {
            part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
          }
        }
        parts.unshift(part);
        current = parent;
      }

      return parts.join(' > ');
    }

    document.addEventListener(
      'pointerdown',
      (event) => {
        const target =
          event.target && event.target.nodeType === Node.ELEMENT_NODE
            ? event.target
            : event.target?.parentElement;
        if (!target) return;

        const element =
          target.closest(
            'a,button,[role="button"],[onclick],input,select,textarea,.el-menu-item,.el-submenu__title,.ant-menu-item,.ant-menu-submenu-title'
          ) || target;
        const rect = element.getBoundingClientRect();
        const valueBearingControl = ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName);
        const payload = {
          url: location.href,
          title: document.title,
          tagName: element.tagName?.toLowerCase() || '',
          text: valueBearingControl
            ? ''
            : shorten(element.innerText || element.getAttribute('title'), 160),
          id: element.id || '',
          className: shorten(element.className, 160),
          role: element.getAttribute('role') || '',
          name: element.getAttribute('name') || '',
          ariaLabel: element.getAttribute('aria-label') || '',
          titleAttr: element.getAttribute('title') || '',
          href: element.href || element.getAttribute('href') || '',
          selector: domPath(element),
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2)
        };

        window.__portalClickRecorder(payload).catch(() => {});
      },
      true
    );
  });

  let activePage = null;
  let activeManagementFrame = null;
  let livePortalAuthorization = '';
  context.on('page', (page) => {
    activePage = page;
    addEvent({ type: 'page-created', url: page.url() });

    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame()) return;
      addEvent({
        type: 'navigation',
        url: frame.url()
      });
      console.log(`Navigation: ${sanitizeUrl(frame.url())}`);
    });
  });

  context.on('request', async (request) => {
    if (request.method() !== 'POST') return;
    if (!request.url().includes(MANAGEMENT_API_PATH)) return;
    if (isBrowserBridgeRequest(request.headers())) return;

    const body = parseJsonBody(request.postData());
    if (!body || String(body.pageType ?? '1') !== '1') return;

    const requestHeaders = await request.allHeaders().catch(() => request.headers());
    const authorization = extractBearerAuthorization(requestHeaders);
    if (authorization) livePortalAuthorization = authorization;
    try {
      const requestFrame = request.frame();
      if (requestFrame && !requestFrame.isDetached()) activeManagementFrame = requestFrame;
    } catch {
      // Service-worker requests do not expose a frame. A same-origin frame is resolved later.
    }

    const captured = {
      capturedAt: new Date().toISOString(),
      url: sanitizeUrl(request.url()),
      method: request.method(),
      headers: sanitizeHeaders(requestHeaders),
      body: sanitizeStructuredValue(body)
    };

    trace.latestDailyManagementRequest = captured;
    addEvent({
      type: 'daily-management-request',
      url: sanitizeUrl(request.url()),
      headers: sanitizeHeaders(requestHeaders),
      body: sanitizeStructuredValue(body)
    });

    fs.writeFileSync(filterFile, `${JSON.stringify(captured, null, 2)}\n`, 'utf8');
    console.log(`Captured daily-management request: ${summarizeBody(body)}`);
    console.log(`Filter body saved: ${filterFile}`);
  });

  context.on('response', async (response) => {
    const request = response.request();
    if (request.method() !== 'POST') return;
    if (!response.url().includes(MANAGEMENT_API_PATH)) return;
    if (isBrowserBridgeRequest(request.headers())) return;

    const body = parseJsonBody(request.postData());
    if (!body || String(body.pageType ?? '1') !== '1') return;

    addEvent({
      type: 'daily-management-response',
      url: sanitizeUrl(response.url()),
      status: response.status(),
      statusText: response.statusText()
    });
    console.log(`Daily-management response: ${response.status()} ${response.statusText()}`);
  });

  const restoredPages = context.pages();
  const page = restoredPages[0] || (await context.newPage());
  activePage = page;
  for (const restoredPage of restoredPages.slice(1)) {
    await restoredPage.close().catch(() => undefined);
  }

  console.log(`Opening portal: ${sanitizeUrl(portalUrl)}`);
  console.log(`Browser: Playwright Chromium (${chromium.executablePath()})`);
  console.log(`Trace file: ${outFile}`);
  console.log(`Filter request file: ${filterFile}`);
  console.log('Use the browser normally: portal login -> portal click/replay -> target page -> query.');

  await gotoWithHttpFallback(page, portalUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });

  let tokenInfo = { token: '', source: '' };
  if ((targetUrl || replayEvents.length) && (autoWaitMs > 0 || waitForLogin)) {
    const deadline = waitForLogin ? Number.POSITIVE_INFINITY : Date.now() + autoWaitMs;
    let targetOpened = false;
    let replayStarted = false;
    let tokenLogged = false;
    let windowMinimized = false;
    console.log(
      waitForLogin
        ? 'Waiting indefinitely for login.'
        : `Waiting up to ${autoWaitMs} ms for login.`
    );
    while (Date.now() < deadline) {
      if (stopOnRequest && trace.latestDailyManagementRequest) {
        break;
      }

      const storage = await context.storageState().catch(() => ({ cookies: [], origins: [] }));
      tokenInfo = extractAdminToken(storage, runtimeConfig.listApiUrl);
      if (tokenInfo.token) {
        if (!tokenLogged) {
          console.log(`Admin token detected: ${tokenInfo.source}`);
          tokenLogged = true;
        }
        if (!stopOnRequest && (!replayEvents.length || replayStarted || targetOpened)) {
          break;
        }
      }

      const loginReady = hasPortalLoginCookie(storage) || (await portalLooksLoggedIn(activePage));
      if (
        !windowMinimized &&
        loginReady &&
        shouldMinimizeAfterLogin(headless, keepWindowAfterLogin, replayEvents, targetUrl)
      ) {
        windowMinimized = true;
        if (await minimizeBrowserWindow(activePage || page)) {
          console.log('Portal login detected; browser window minimized before automatic navigation.');
        }
      }

      if (!replayStarted && replayEvents.length && loginReady) {
        replayStarted = true;
        console.log('Portal login detected; replaying recorded portal clicks.');
        await replayPortalClicks(
          context,
          () => activePage || page,
          replayEvents,
          {
            clickDelayMs: replayClickDelayMs,
            stepTimeoutMs: replayStepTimeoutMs
          }
        );
      }

      if (!targetOpened && targetUrl && loginReady) {
        targetOpened = true;
        console.log(`Portal login detected; opening target page: ${sanitizeUrl(targetUrl)}`);
        await gotoWithHttpFallback(page, targetUrl, {
          waitUntil: 'domcontentloaded',
          timeout: 60000
        }).catch((error) => {
          console.log(`Target page open failed: ${sanitizeLogText(error.message || error)}`);
        });
      }

      await sleep(1000);
    }

    if ((tokenInfo.token || replayStarted || targetOpened) && postLoginSettleMs > 0) {
      console.log(`Waiting ${postLoginSettleMs} ms for target page requests.`);
      await sleep(postLoginSettleMs);
    }
    if (stopOnRequest && !trace.latestDailyManagementRequest) {
      console.log('Waiting for daily-management request after target navigation.');
      while (Date.now() < deadline && !trace.latestDailyManagementRequest) {
        await sleep(1000);
      }
    }
  } else if ((autoWaitMs > 0 || waitForLogin) && stopOnRequest) {
    const deadline = waitForLogin ? Number.POSITIVE_INFINITY : Date.now() + autoWaitMs;
    console.log(
      waitForLogin
        ? 'Waiting for manual portal login, navigation, and daily-management request.'
        : `Waiting up to ${autoWaitMs} ms for manual portal navigation and daily-management request.`
    );
    while (Date.now() < deadline && !trace.latestDailyManagementRequest) {
      await sleep(1000);
    }
  } else if (autoWaitMs > 0) {
    console.log(`Recording for ${autoWaitMs} ms.`);
    await sleep(autoWaitMs);
  } else {
    await waitForEnter('\nAfter the target request has been captured, press Enter here to stop recording: ');
  }

  trace.finishedAt = new Date().toISOString();
  const storageState = await context.storageState().catch(() => null);
  tokenInfo = storageState
    ? extractAdminToken(storageState, runtimeConfig.listApiUrl)
    : tokenInfo;
  if (!tokenInfo.token) {
    tokenInfo = extractCapturedRequestToken(trace);
  }
  if (!tokenInfo.token && livePortalAuthorization) {
    tokenInfo = extractCapturedRequestToken({
      latestDailyManagementRequest: { headers: { authorization: livePortalAuthorization } }
    });
  }
  saveTrace();
  writeReplayFile(
    saveReplayFile ? path.resolve(saveReplayFile) : '',
    trace,
    outFile,
    {
      overwrite: overwriteReplayFile,
      freeze: shouldFreezeReplayFile
    }
  );

  if (requireToken && !tokenInfo.token) {
    await context.close();
    await browser.close();
    throw new Error('No Admin-Token was detected. Please log in and wait for the target page to open.');
  }

  if (requireRequest && !trace.latestDailyManagementRequest) {
    await context.close();
    await browser.close();
    throw new Error('No daily-management request was captured.');
  }

  if (keepSession) {
    if (!Number.isInteger(bridgePort) || bridgePort < 0 || bridgePort > 65535) {
      throw new Error(`--bridge-port must be between 0 and 65535: ${bridgePort}`);
    }
    if (!findBrowserRequestContext(context, activeManagementFrame, runtimeConfig.listApiUrl)) {
      throw new Error('The authenticated daily-management browser frame is unavailable.');
    }
    const bridgeToken = crypto.randomBytes(32).toString('base64url');
    const bridge = await startBrowserRequestBridge(
      (targetUrl) => findBrowserRequestContext(context, activeManagementFrame, targetUrl),
      bridgePort,
      {
        token: bridgeToken,
        allowedTargets: [
          runtimeConfig.listApiUrl,
          runtimeConfig.detailApiUrl,
          runtimeConfig.attachmentDownloadUrl
        ],
        requestTimeoutMs: runtimeConfig.attachmentTimeoutMs,
        requireUpstreamAuthorization: true,
        getAuthorization: () =>
          livePortalAuthorization ||
          (tokenInfo.token
            ? /^Bearer\s+/i.test(tokenInfo.token)
              ? tokenInfo.token
              : `Bearer ${tokenInfo.token}`
            : '')
      }
    );
    if (bridgeReadyFile) {
      const readyPath = path.resolve(bridgeReadyFile);
      ensureDir(path.dirname(readyPath));
      fs.writeFileSync(readyPath, `${JSON.stringify({ url: bridge.url, token: bridgeToken })}\n`, {
        encoding: 'utf8',
        mode: 0o600
      });
    }
    console.log(`Browser request bridge ready: ${bridge.url}`);
    await waitForSessionShutdown(bridge.server);
  }

  await context.close();
  await browser.close();

  console.log(`Trace saved: ${outFile}`);
  if (tokenInfo.token) {
    console.log('Authentication was detected and remains only in browser memory.');
  } else {
    console.log('No Admin-Token was detected.');
  }
  if (trace.latestDailyManagementRequest) {
    console.log(`Latest daily-management body: ${summarizeBody(trace.latestDailyManagementRequest.body)}`);
  } else {
    console.log('No daily-management request was captured.');
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error(sanitizeLogText(error.message || error));
    process.exit(1);
  });
}

module.exports = {
  bridgeTokenMatches,
  extractBearerAuthorization,
  findBrowserRequestContext,
  isAllowedBridgeTarget,
  isBrowserBridgeRequest,
  minimizeBrowserWindow,
  sanitizeHeaders,
  sanitizeLogText,
  sanitizeStructuredValue,
  sanitizeTraceEvent,
  sanitizeUrl,
  startBrowserRequestBridge,
  shouldMinimizeAfterLogin
};
