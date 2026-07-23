const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const projectRoot = path.resolve(__dirname, '..');
const dataDir = path.join(projectRoot, 'tmp-validation', 'attachment-download-test');

function runDownloader(url, extraArgs = [], environment = {}, captureDir = dataDir) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        path.join(projectRoot, 'data_acquisition', 'download-daily-attachments.js'),
        `--data-dir=${captureDir}`,
        '--retries=2',
        '--timeout-ms=5000',
        ...extraArgs
      ],
      {
        cwd: projectRoot,
        env: {
          ...process.env,
          WORKORDER_ATTACHMENT_URL: url,
          WORKORDER_TOKEN: 'test-token',
          ...environment
        },
        stdio: ['ignore', 'pipe', 'pipe']
      }
    );
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

async function main() {
  fs.rmSync(dataDir, { recursive: true, force: true });
  fs.mkdirSync(dataDir, { recursive: true });
  const sharedAttachmentStore = path.join(projectRoot, 'tmp-validation', 'shared-attachment-store');
  const secondDataDir = path.join(projectRoot, 'tmp-validation', 'attachment-download-second-current');
  const imagesOnlyDataDir = path.join(projectRoot, 'tmp-validation', 'attachment-download-images-only');
  const bridgeDataDir = path.join(projectRoot, 'tmp-validation', 'attachment-download-browser-bridge');
  fs.rmSync(sharedAttachmentStore, { recursive: true, force: true });
  fs.rmSync(secondDataDir, { recursive: true, force: true });
  fs.rmSync(imagesOnlyDataDir, { recursive: true, force: true });
  fs.rmSync(bridgeDataDir, { recursive: true, force: true });
  const file = (name, filePath) => ({ fileOldName: name, filePath, type: 'test' });
  const group = (name, filePath, fileType = '2', fileTypeLabel = '') => ({
    fileType,
    ...(fileTypeLabel ? { fileTypeLabel } : {}),
    url: JSON.stringify([file(name, filePath)])
  });
  fs.writeFileSync(
    path.join(dataDir, 'management-api-details.json'),
    JSON.stringify({
      rows: [
        {
          id: '1',
          safetyCode: 'A',
          theme: '=1+1',
          dailySafetyFiles: [group('one.txt', 'one')]
        },
        { id: '2', safetyCode: 'B', dailySafetyFiles: [group('two.txt', 'two')] }
      ]
    }),
    'utf8'
  );
  fs.writeFileSync(
    path.join(dataDir, 'database-existing-safety-codes.json'),
    JSON.stringify({ codes: ['A', 'B'] }),
    'utf8'
  );

  let requests = 0;
  let alwaysFail = false;
  let bridgeMode = false;
  let bridgeAuthorization = '';
  let bridgePayload = null;
  const server = http.createServer(async (request, response) => {
    requests += 1;
    if (bridgeMode) {
      bridgeAuthorization = String(request.headers.authorization || '');
      let body = '';
      for await (const chunk of request) body += chunk;
      bridgePayload = JSON.parse(body);
      response.writeHead(200, { 'Content-Type': 'application/octet-stream' });
      response.end(Buffer.from('browser session attachment'));
      return;
    }
    if (alwaysFail || requests === 1) {
      response.writeHead(500, { 'Content-Type': 'text/plain' });
      response.end('temporary failure');
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/octet-stream' });
    response.end(Buffer.from('same attachment content'));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));

  try {
    const address = server.address();
    const result = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      [`--attachment-store=${sharedAttachmentStore}`]
    );
    assert.strictEqual(result.code, 0, `${result.stdout}\n${result.stderr}`);
    const manifest = JSON.parse(
      fs.readFileSync(path.join(dataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.strictEqual(requests, 3);
    assert.deepStrictEqual(
      manifest.rows.map((row) => row.status).sort(),
      ['downloaded', 'skipped_duplicate']
    );
    assert.strictEqual(manifest.rows.filter((row) => row.attempts === 2).length, 1);
    assert.strictEqual(manifest.rows.some((row) => 'labelDir' in row), false);
    assert.strictEqual(
      fs.readdirSync(path.join(dataDir, 'daily-attachments')).filter((name) => name.endsWith('.txt')).length,
      2
    );
    assert.strictEqual(
      fs.readdirSync(sharedAttachmentStore).filter((name) => /^[a-f0-9]{64}$/u.test(name)).length,
      1
    );
    const manifestCsv = fs.readFileSync(
      path.join(dataDir, 'daily-attachments-manifest.csv'),
      'utf8'
    );
    const manifestExcelSafeCsv = fs.readFileSync(
      path.join(dataDir, 'daily-attachments-manifest-excel-safe.csv'),
      'utf8'
    );
    assert.ok(manifestCsv.includes('"=1+1"'));
    assert.ok(manifestExcelSafeCsv.includes("\"'=1+1\""));
    console.log('attachment retry/hash integration test: ok');

    requests = 0;
    const readProbe = path.join(dataDir, 'reject-attachment-reads.js');
    fs.writeFileSync(
      readProbe,
      "const fs = require('fs'); fs.createReadStream = () => { throw new Error('historical attachment content was read'); };\n",
      'utf8'
    );
    const readProbeOption = `--require=\"${readProbe.split(path.sep).join('/')}\"`;
    const reusedResult = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      [],
      { NODE_OPTIONS: readProbeOption }
    );
    assert.strictEqual(reusedResult.code, 0, `${reusedResult.stdout}\n${reusedResult.stderr}`);
    assert.strictEqual(requests, 0);
    const reusedManifest = JSON.parse(
      fs.readFileSync(path.join(dataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.deepStrictEqual(
      reusedManifest.rows.map((row) => row.status),
      ['reused', 'reused']
    );
    console.log('attachment manifest reuse integration test: ok');

    fs.mkdirSync(secondDataDir, { recursive: true });
    fs.copyFileSync(
      path.join(dataDir, 'management-api-details.json'),
      path.join(secondDataDir, 'management-api-details.json')
    );
    requests = 0;
    const sharedReuseResult = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      [`--attachment-store=${sharedAttachmentStore}`],
      { NODE_OPTIONS: readProbeOption },
      secondDataDir
    );
    assert.strictEqual(
      sharedReuseResult.code,
      0,
      `${sharedReuseResult.stdout}\n${sharedReuseResult.stderr}`
    );
    assert.strictEqual(requests, 0);
    const sharedReuseManifest = JSON.parse(
      fs.readFileSync(path.join(secondDataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.deepStrictEqual(
      sharedReuseManifest.rows.map((row) => row.status),
      ['reused', 'reused']
    );
    assert.ok(fs.existsSync(path.join(sharedAttachmentStore, sharedReuseManifest.rows[0].sha256)));
    console.log('shared attachment-store reuse integration test: ok');

    fs.mkdirSync(imagesOnlyDataDir, { recursive: true });
    fs.writeFileSync(
      path.join(imagesOnlyDataDir, 'management-api-details.json'),
      JSON.stringify({
        rows: [
          {
            id: 'images',
            safetyCode: 'IMG',
            companyName: 'Test Company',
            dailySafetyFiles: [
              group('evidence.png', 'image-file', '2', 'Training Image'),
              group('notes.txt', 'text-file', '99')
            ]
          }
        ]
      }),
      'utf8'
    );
    alwaysFail = false;
    requests = 0;
    const imagesOnlyResult = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      ['--images-only'],
      {},
      imagesOnlyDataDir
    );
    assert.strictEqual(imagesOnlyResult.code, 0, `${imagesOnlyResult.stdout}\n${imagesOnlyResult.stderr}`);
    const imagesOnlyManifest = JSON.parse(
      fs.readFileSync(path.join(imagesOnlyDataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.strictEqual(requests, 2);
    assert.strictEqual(imagesOnlyManifest.rows.length, 1);
    assert.strictEqual(imagesOnlyManifest.rows[0].isImage, true);
    assert.strictEqual(imagesOnlyManifest.rows[0].fileOldName, 'evidence.png');
    assert.strictEqual(
      path.basename(imagesOnlyManifest.rows[0].savedPath),
      'IMG_Test Company_Training Image.png'
    );
    console.log('image-only attachment integration test: ok');

    fs.mkdirSync(bridgeDataDir, { recursive: true });
    fs.writeFileSync(
      path.join(bridgeDataDir, 'management-api-details.json'),
      JSON.stringify({
        rows: [{ id: 'bridge', safetyCode: 'BRIDGE', dailySafetyFiles: [group('bridge.bin', 'bridge-file')] }]
      }),
      'utf8'
    );
    bridgeMode = true;
    requests = 0;
    const bridgeResult = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      [`--browser-bridge-url=http://127.0.0.1:${address.port}`, '--retries=1'],
      { WORKORDER_TOKEN: '', WORKORDER_BROWSER_BRIDGE_TOKEN: 'bridge-secret' },
      bridgeDataDir
    );
    bridgeMode = false;
    assert.strictEqual(bridgeResult.code, 0, `${bridgeResult.stdout}\n${bridgeResult.stderr}`);
    assert.strictEqual(requests, 1);
    assert.strictEqual(bridgeAuthorization, 'Bearer bridge-secret');
    assert.strictEqual(bridgePayload.url, `http://127.0.0.1:${address.port}/download`);
    assert.strictEqual(bridgePayload.method, 'POST');
    const bridgeManifest = JSON.parse(
      fs.readFileSync(path.join(bridgeDataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.strictEqual(bridgeManifest.rows[0].status, 'downloaded');
    assert.strictEqual(bridgeManifest.rows[0].sha256.length, 64);
    console.log('browser bridge attachment integration test: ok');

    alwaysFail = true;
    requests = 0;
    fs.rmSync(dataDir, { recursive: true, force: true });
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(
      path.join(dataDir, 'management-api-details.json'),
      JSON.stringify({
        rows: [{ id: '3', safetyCode: 'C', dailySafetyFiles: [group('fail.txt', 'fail')] }]
      }),
      'utf8'
    );
    const failedResult = await runDownloader(
      `http://127.0.0.1:${address.port}/download`,
      ['--retries=1']
    );
    assert.strictEqual(failedResult.code, 2, failedResult.stdout + failedResult.stderr);
    const failedManifest = JSON.parse(
      fs.readFileSync(path.join(dataDir, 'daily-attachments-manifest.json'), 'utf8')
    );
    assert.strictEqual(failedManifest.rows[0].status, 'failed');
    console.log('attachment failure exit-code integration test: ok');
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(dataDir, { recursive: true, force: true });
    fs.rmSync(sharedAttachmentStore, { recursive: true, force: true });
    fs.rmSync(secondDataDir, { recursive: true, force: true });
    fs.rmSync(imagesOnlyDataDir, { recursive: true, force: true });
    fs.rmSync(bridgeDataDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
