const assert = require('assert');
const http = require('http');
const { chromium } = require('playwright');

const {
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
} = require('../data_acquisition/record-portal-clicks');

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

function bridgeRequest(url, path, token, payload) {
  return new Promise((resolve, reject) => {
    const target = new URL(path, url);
    const body = payload === undefined ? '' : JSON.stringify(payload);
    const request = http.request(
      target,
      {
        method: payload === undefined ? 'GET' : 'POST',
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {})
        }
      },
      (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(chunks) }));
      }
    );
    request.on('error', reject);
    request.end(body);
  });
}

async function runBrowserAuthenticationIntegrationTests() {
  const upstreamCalls = [];
  const targetServer = http.createServer((request, response) => {
    const target = new URL(request.url, 'http://127.0.0.1');
    if (target.pathname === '/session-page') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end(
        '<script>sessionStorage.setItem("Admin-Token", "session-secret")</script>ready'
      );
      return;
    }
    if (target.pathname === '/frame-page') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end(
        '<script>localStorage.setItem("Admin-Token", "frame-secret")</script>frame-ready'
      );
      return;
    }
    if (target.pathname === '/empty-page') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('ready');
      return;
    }

    const expected = {
      '/api-session': 'Bearer session-secret',
      '/api-frame': 'Bearer frame-secret',
      '/api-fallback': 'Bearer captured-secret',
      '/file-fallback': 'Bearer captured-secret'
    }[target.pathname];
    if (!expected) {
      response.writeHead(404).end();
      return;
    }
    upstreamCalls.push({ path: target.pathname, authorization: request.headers.authorization || '' });
    const authorized = request.headers.authorization === expected;
    if (target.pathname === '/file-fallback') {
      response.writeHead(authorized ? 200 : 401, { 'Content-Type': 'application/octet-stream' });
      response.end(authorized ? 'attachment-evidence' : 'unauthorized');
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ code: authorized ? 200 : 401 }));
  });
  const targetOrigin = await listen(targetServer);
  const outerServer = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(`<iframe src="${targetOrigin}/frame-page"></iframe>`);
  });
  const outerOrigin = await listen(outerServer);

  const browser = await chromium.launch({ headless: true });
  const bridges = [];
  try {
    const storageContext = await browser.newContext();
    const sessionPage = await storageContext.newPage();
    await sessionPage.goto(`${targetOrigin}/session-page`);
    const sessionBridge = await startBrowserRequestBridge(
      (targetUrl) => findBrowserRequestContext(storageContext, sessionPage.mainFrame(), targetUrl),
      0,
      {
        token: 'bridge-secret',
        allowedTargets: [`${targetOrigin}/api-session`],
        requestTimeoutMs: 5000,
        requireUpstreamAuthorization: true
      }
    );
    bridges.push(sessionBridge.server);
    const sessionResult = await bridgeRequest(sessionBridge.url, '/request', 'bridge-secret', {
      url: `${targetOrigin}/api-session`, method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(JSON.parse(JSON.parse(sessionResult.body.toString('utf8')).text).code, 200);

    const outerPage = await storageContext.newPage();
    await outerPage.goto(outerOrigin);
    await outerPage.waitForSelector('iframe');
    await outerPage.waitForTimeout(50);
    const frameBridge = await startBrowserRequestBridge(
      (targetUrl) => findBrowserRequestContext(storageContext, null, targetUrl),
      0,
      {
        token: 'bridge-secret',
        allowedTargets: [`${targetOrigin}/api-frame`],
        requestTimeoutMs: 5000,
        requireUpstreamAuthorization: true
      }
    );
    bridges.push(frameBridge.server);
    const frameResult = await bridgeRequest(frameBridge.url, '/request', 'bridge-secret', {
      url: `${targetOrigin}/api-frame`, method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(JSON.parse(JSON.parse(frameResult.body.toString('utf8')).text).code, 200);

    const fallbackContext = await browser.newContext();
    const fallbackPage = await fallbackContext.newPage();
    await fallbackPage.goto(`${targetOrigin}/empty-page`);
    const fallbackBridge = await startBrowserRequestBridge(() => fallbackPage.mainFrame(), 0, {
      token: 'bridge-secret',
      allowedTargets: [`${targetOrigin}/api-fallback`, `${targetOrigin}/file-fallback`],
      requestTimeoutMs: 5000,
      requireUpstreamAuthorization: true,
      getAuthorization: () => 'Bearer captured-secret'
    });
    bridges.push(fallbackBridge.server);
    const fallbackResult = await bridgeRequest(fallbackBridge.url, '/request', 'bridge-secret', {
      url: `${targetOrigin}/api-fallback`,
      method: 'POST',
      headers: { Authorization: 'Bearer caller-controlled' },
      body: {}
    });
    assert.strictEqual(JSON.parse(JSON.parse(fallbackResult.body.toString('utf8')).text).code, 200);
    const binaryResult = await bridgeRequest(fallbackBridge.url, '/binary', 'bridge-secret', {
      url: `${targetOrigin}/file-fallback`, method: 'GET', headers: {}, body: null
    });
    assert.strictEqual(binaryResult.status, 200);
    assert.strictEqual(binaryResult.body.toString('utf8'), 'attachment-evidence');

    const callsBeforeFailure = upstreamCalls.length;
    const closedContext = await browser.newContext();
    const closedPage = await closedContext.newPage();
    await closedPage.goto(`${targetOrigin}/empty-page`);
    const failClosedBridge = await startBrowserRequestBridge(() => closedPage.mainFrame(), 0, {
      token: 'bridge-secret',
      allowedTargets: [`${targetOrigin}/api-fallback`],
      requestTimeoutMs: 5000,
      requireUpstreamAuthorization: true
    });
    bridges.push(failClosedBridge.server);
    const rejected = await bridgeRequest(failClosedBridge.url, '/request', 'bridge-secret', {
      url: `${targetOrigin}/api-fallback`, method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(rejected.status, 502);
    assert.strictEqual(upstreamCalls.length, callsBeforeFailure);

    assert.strictEqual(
      upstreamCalls.find((call) => call.path === '/api-fallback').authorization,
      'Bearer captured-secret'
    );
    await Promise.all([storageContext.close(), fallbackContext.close(), closedContext.close()]);
  } finally {
    for (const server of bridges) await closeServer(server);
    await browser.close();
    await Promise.all([closeServer(outerServer), closeServer(targetServer)]);
  }
}

async function main() {
  assert.strictEqual(shouldMinimizeAfterLogin(false, false, [{ selector: '#menu' }], ''), true);
  assert.strictEqual(shouldMinimizeAfterLogin(false, false, [], 'https://example.test/target'), true);
  assert.strictEqual(shouldMinimizeAfterLogin(false, true, [{ selector: '#menu' }], ''), false);
  assert.strictEqual(shouldMinimizeAfterLogin(true, false, [{ selector: '#menu' }], ''), false);
  assert.strictEqual(shouldMinimizeAfterLogin(false, false, [], ''), false);
  assert.strictEqual(isBrowserBridgeRequest({ 'x-workorder-browser-bridge': '1' }), true);
  assert.strictEqual(isBrowserBridgeRequest({ 'x-workorder-browser-bridge': '0' }), false);
  assert.strictEqual(
    extractBearerAuthorization({ authorization: 'Bearer memory-secret' }),
    'Bearer memory-secret'
  );
  assert.strictEqual(extractBearerAuthorization({ authorization: 'Basic unsafe' }), '');
  assert.strictEqual(sanitizeUrl('https://user:pass@example.test/path?token=secret#fragment'), 'https://example.test/path');
  assert.strictEqual(
    sanitizeLogText('open https://user:pass@example.test/path?ticket=secret#fragment'),
    'open https://example.test/path'
  );

  const calls = [];
  const session = {
    send: async (method, params) => {
      calls.push([method, params]);
      return method === 'Browser.getWindowForTarget' ? { windowId: 42 } : {};
    },
    detach: async () => calls.push(['detach'])
  };
  const page = { context: () => ({ newCDPSession: async () => session }) };
  assert.strictEqual(await minimizeBrowserWindow(page), true);
  assert.deepStrictEqual(calls, [
    ['Browser.getWindowForTarget', undefined],
    ['Browser.setWindowBounds', { windowId: 42, bounds: { windowState: 'minimized' } }],
    ['detach']
  ]);

  assert.deepStrictEqual(
    sanitizeHeaders({ Authorization: 'Bearer secret', Cookie: 'session=secret', 'X-Trace': 'ok' }),
    { 'X-Trace': 'ok' }
  );
  assert.deepStrictEqual(
    sanitizeHeaders({ Referer: 'https://example.test/path?signature=secret#fragment' }),
    { Referer: 'https://example.test/path' }
  );
  assert.deepStrictEqual(
    sanitizeStructuredValue({ password: 'secret', nested: { openId: 'sensitive', query: 'ok' } }),
    { password: '<redacted>', nested: { openId: '<redacted>', query: 'ok' } }
  );
  assert.deepStrictEqual(
    sanitizeTraceEvent({ tagName: 'input', text: 'typed secret', value: 'typed secret' }),
    { tagName: 'input', text: '' }
  );
  assert.ok(
    !sanitizeTraceEvent({ selector: 'a[href="https://example.test/?ticket=secret"]' })
      .selector.includes('secret')
  );
  assert.strictEqual(bridgeTokenMatches({ authorization: 'Bearer bridge-secret' }, 'bridge-secret'), true);
  assert.strictEqual(bridgeTokenMatches({ authorization: 'Bearer wrong' }, 'bridge-secret'), false);
  assert.strictEqual(
    isAllowedBridgeTarget('https://portal.test/api/list/123', ['https://portal.test/api/list']),
    true
  );
  assert.strictEqual(
    isAllowedBridgeTarget('https://attacker.test/api/list', ['https://portal.test/api/list']),
    false
  );

  const evaluatedPayloads = [];
  const fakePage = {
    isClosed: () => false,
    evaluate: async (_function, payload) => {
      evaluatedPayloads.push(payload);
      return payload.returnBinary
        ? {
            status: 200,
            contentType: 'application/octet-stream',
            bodyBase64: Buffer.from('binary-evidence').toString('base64')
          }
        : { status: 200, text: '{"code":200}' };
    }
  };
  const bridge = await startBrowserRequestBridge(() => fakePage, 0, {
    token: 'bridge-secret',
    allowedTargets: ['https://portal.test/api/list', 'https://portal.test/api/attachment'],
    requestTimeoutMs: 1234
  });
  try {
    const unauthorized = await bridgeRequest(bridge.url, '/health', '', undefined);
    assert.strictEqual(unauthorized.status, 401);
    const forbidden = await bridgeRequest(bridge.url, '/request', 'bridge-secret', {
      url: 'https://attacker.test/api/list', method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(forbidden.status, 403);
    const allowed = await bridgeRequest(bridge.url, '/request', 'bridge-secret', {
      url: 'https://portal.test/api/list', method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(allowed.status, 200);
    assert.strictEqual(JSON.parse(allowed.body.toString('utf8')).text, '{"code":200}');
    const binary = await bridgeRequest(bridge.url, '/binary', 'bridge-secret', {
      url: 'https://portal.test/api/attachment', method: 'POST', headers: {}, body: {}
    });
    assert.strictEqual(binary.status, 200);
    assert.strictEqual(binary.body.toString('utf8'), 'binary-evidence');
    assert.deepStrictEqual(evaluatedPayloads.map((payload) => payload.timeoutMs), [1234, 1234]);
  } finally {
    await new Promise((resolve) => bridge.server.close(resolve));
  }
  await runBrowserAuthenticationIntegrationTests();
  console.log('portal window minimization tests: ok');
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
