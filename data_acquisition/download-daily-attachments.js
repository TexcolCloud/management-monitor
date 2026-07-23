const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { Readable, Transform } = require('stream');
const { pipeline } = require('stream/promises');
const { readStorageStateToken } = require('../common/auth-runtime');
const { normalizeCompany } = require('../common/company-normalization');
const { loadRuntimeConfig } = require('../common/runtime-config');

const runtimeConfig = loadRuntimeConfig();
const DOWNLOAD_URL = runtimeConfig.attachmentDownloadUrl;
const DEFAULT_DATA_DIR = runtimeConfig.captureRoot;
const DEFAULT_TOKEN_SOURCE_DIR = runtimeConfig.tokenSourceDir;
const DEFAULT_ATTACHMENT_DIR_NAME = runtimeConfig.attachmentDirName;

const IMAGE_EXTENSIONS = new Set([
  '.jpg',
  '.jpeg',
  '.png',
  '.bmp',
  '.webp',
  '.gif',
  '.tif',
  '.tiff'
]);

const FILE_TYPE_LABELS = {
  1: '会议照片',
  2: '会议记录',
  3: '会议通知',
  4: '培训照片',
  5: '签到表',
  6: '例会通知',
  7: '例会资料',
  9: '通报附件',
  19: '培训资料'
};

const IMAGE_LABEL_PATTERNS = [
  /photo/i,
  /image/i,
  /\u56fe\u7247|\u5716\u7247|\u7167\u7247|\u56fe\u50cf|\u5716\u50cf/u
];

const args = process.argv.slice(2);

if (args.includes('--help') || args.includes('-h')) {
  console.log(`
Usage:
  node download-daily-attachments.js [options]

Download daily-management attachments for a captured data directory.

Options:
  --data-dir=<dir>         Captured data directory. Defaults to capture-output/daily-management.
  --token-source=<dir>     Directory containing auth-token.json or portal-storage-state.json.
                           Defaults to capture-output/daily-management.
  --headers-file=<file>    Captured daily-management request file.
  --attachment-dir-name=<name>
                            Attachment output directory name. Defaults to daily-attachments.
  --attachment-store=<dir>  Shared content-addressed attachment store. Defaults to a store
                            inside the captured data directory.
  --browser-bridge-url=<url>
                            Download through the active logged-in browser session.
  --browser-bridge-token=<token>
                            Ephemeral bearer read from the browser ready file.
  --file-types=<ids>       Optional comma-separated fileType filter.
  --images-only            Download only image attachments.
  --exclude-codes-file=<file>
                           JSON file containing database-existing safety codes to skip.
  --timeout-ms=<n>         Timeout per download attempt. Defaults to runtime config.
  --retries=<n>            Maximum attempts per attachment. Defaults to runtime config.
  --max-failures=<n>       Exit with failure when more than N attachments fail. Defaults to 0.
  --concurrency=<n>        Concurrent downloads per retry round. Defaults to runtime config.
  --dry-run                Write manifests without downloading files.
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

function directoryName(value) {
  const text = String(value || '').trim();
  if (!text || text === '.' || text === '..' || path.basename(text) !== text || path.isAbsolute(text)) {
    throw new Error(`Attachment directory must be a single directory name: ${value}`);
  }
  return text;
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function csvEscape(value, spreadsheetSafe = false) {
  const normalized = value && typeof value === 'object' ? JSON.stringify(value) : value;
  let text = String(normalized ?? '').replace(/\r?\n/g, ' ').trim();
  if (spreadsheetSafe && /^[=+\-@]/u.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

function writeCsv(file, rows, spreadsheetSafe = false) {
  if (!rows.length) {
    fs.writeFileSync(file, '\uFEFF', 'utf8');
    return;
  }

  const keys = Array.from(
    rows.reduce((set, row) => {
      Object.keys(row).forEach((key) => set.add(key));
      return set;
    }, new Set())
  );

  const csv = [
    keys.map((value) => csvEscape(value, spreadsheetSafe)).join(','),
    ...rows.map((row) =>
      keys.map((key) => csvEscape(row[key], spreadsheetSafe)).join(',')
    )
  ].join('\r\n');

  fs.writeFileSync(file, `\uFEFF${csv}`, 'utf8');
}

function readManifestRows(file) {
  if (!fs.existsSync(file)) return [];
  try {
    const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
    return Array.isArray(payload.rows) ? payload.rows : [];
  } catch {
    return [];
  }
}

function attachmentSourceKey(value) {
  const filePath = String(value.filePath || '').trim();
  if (!filePath) return '';
  return JSON.stringify([
    String(value.id || value.safetyCode || ''),
    String(value.fileType || ''),
    String(value.fileId || ''),
    filePath,
    String(value.fileOldName || ''),
    String(value.type || '')
  ]);
}

function safeName(value, fallback = 'unnamed', maxLength = 120) {
  const text = String(value || fallback)
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '');
  const name = text || fallback;
  if (name.length <= maxLength) return name;

  const digest = Buffer.from(name).toString('hex').slice(0, 10);
  return `${name.slice(0, Math.max(12, maxLength - 11))}_${digest}`;
}

function safeFileName(value, fallback = 'unnamed.bin', maxLength = 150) {
  const fallbackName = safeName(fallback, 'unnamed.bin', maxLength);
  const cleaned = safeName(value, fallbackName, maxLength);
  const parsed = path.parse(cleaned);
  if (!parsed.ext && path.extname(fallbackName)) return `${cleaned}${path.extname(fallbackName)}`;
  return cleaned;
}

function cleanExtensionSource(value) {
  return String(value || '').split(/[?#]/, 1)[0];
}

function isImageAttachment(file) {
  const candidates = [file.fileOldName, file.filePath, file.name, file.url, file.path];
  if (
    candidates.some((value) =>
      IMAGE_EXTENSIONS.has(path.extname(cleanExtensionSource(value)).toLowerCase())
    )
  ) {
    return true;
  }

  const typeText = String(file.type || file.contentType || file.mimeType || '').toLowerCase();
  return typeText.startsWith('image/') || typeText.includes('/image');
}

function firstText(values) {
  for (const value of values) {
    const text = String(value ?? '').trim();
    if (text) return text;
  }
  return '';
}

function fileGroupLabel(fileGroup) {
  const fileType = String(fileGroup.fileType ?? '').trim();
  return firstText([
    fileGroup.fileTypeName,
    fileGroup.fileTypeText,
    fileGroup.fileTypeLabel,
    fileGroup.dictLabel,
    fileGroup.label,
    fileGroup.name,
    fileGroup.fieldName,
    fileGroup.fileName,
    fileGroup.title,
    fileGroup.remark,
    FILE_TYPE_LABELS[fileType],
    fileType ? `未知附件类型-${fileType}` : '未知附件类型'
  ]);
}

function isImageFileGroup(fileGroup) {
  const label = fileGroupLabel(fileGroup);
  return IMAGE_LABEL_PATTERNS.some((pattern) => pattern.test(label));
}

function uniquePath(dir, filename, reservedPaths = new Set()) {
  const parsed = path.parse(filename);
  let candidate = path.join(dir, filename);
  let index = 2;

  while (fs.existsSync(candidate) || reservedPaths.has(candidate)) {
    candidate = path.join(dir, `${parsed.name}_${index}${parsed.ext}`);
    index += 1;
  }

  reservedPaths.add(candidate);
  return candidate;
}

function parseAttachmentUrl(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;

  try {
    const parsed = JSON.parse(value);
    if (Array.isArray(parsed)) return parsed;
    return parsed && typeof parsed === 'object' ? [parsed] : [];
  } catch {
    return [];
  }
}

function normalizeAttachmentFile(file) {
  if (file && typeof file === 'object' && !Array.isArray(file)) return file;

  const filePath = String(file || '');
  return {
    fileOldName: path.basename(cleanExtensionSource(filePath)) || '',
    filePath,
    type: ''
  };
}

function findLatestToken(sourceDir) {
  const responseDir = path.join(sourceDir, 'responses');
  if (!fs.existsSync(responseDir)) return '';

  const candidates = fs
    .readdirSync(responseDir)
    .filter((file) => file.includes('login') && file.endsWith('.json'))
    .map((file) => path.join(responseDir, file))
    .filter((file) => {
      try {
        return Boolean(JSON.parse(fs.readFileSync(file, 'utf8')).data?.token);
      } catch {
        return false;
      }
    })
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);

  if (!candidates.length) return '';
  return JSON.parse(fs.readFileSync(candidates[0], 'utf8')).data.token;
}

function findSavedToken(dataDir) {
  const tokenFile = path.join(dataDir, 'auth-token.json');
  if (fs.existsSync(tokenFile)) {
    try {
      const token = JSON.parse(fs.readFileSync(tokenFile, 'utf8')).token || '';
      if (token) return token;
    } catch {
      // Fall back to storage state below.
    }
  }

  return findStorageStateToken(dataDir);
}

function findStorageStateToken(dataDir) {
  const storageFile = path.join(dataDir, 'portal-storage-state.json');
  return readStorageStateToken(storageFile, runtimeConfig.listApiUrl).token;
}

function readCapturedHeaders(file) {
  if (!file || !fs.existsSync(file)) return {};

  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed.headers && typeof parsed.headers === 'object' ? parsed.headers : {};
  } catch {
    return {};
  }
}

function replayHeaders(capturedHeaders) {
  const blocked = new Set([
    'accept-encoding',
    'authorization',
    'connection',
    'content-length',
    'cookie',
    'host',
    'sec-fetch-dest',
    'sec-fetch-mode',
    'sec-fetch-site'
  ]);
  const headers = {};

  for (const [key, value] of Object.entries(capturedHeaders || {})) {
    const lowerKey = key.toLowerCase();
    if (blocked.has(lowerKey)) continue;
    if (lowerKey.startsWith('sec-ch-')) continue;
    headers[key] = value;
  }

  return headers;
}

function hasHeader(headers, name) {
  const lowerName = name.toLowerCase();
  return Object.keys(headers).some((key) => key.toLowerCase() === lowerName);
}

function buildHeaders(capturedHeaders, token, extraHeaders = {}) {
  const headers = replayHeaders(capturedHeaders);

  if (token) {
    const authorizationKey = Object.keys(headers).find((key) => key.toLowerCase() === 'authorization');
    if (authorizationKey) {
      headers[authorizationKey] = `Bearer ${token}`;
    } else {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  for (const [key, value] of Object.entries(extraHeaders)) {
    if (!hasHeader(headers, key)) headers[key] = value;
  }

  return headers;
}

function capturedBearerToken(capturedHeaders) {
  const authorization =
    capturedHeaders?.Authorization ||
    capturedHeaders?.authorization ||
    capturedHeaders?.AUTHORIZATION ||
    '';
  const match = String(authorization).match(/^Bearer\s+(.+)$/i);
  return match ? match[1] : '';
}


function readExcludedSafetyCodes(file) {
  if (!file || !fs.existsSync(file)) return new Set();
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    const codes = Array.isArray(parsed) ? parsed : parsed.codes || [];
    return new Set(codes.map((item) => String(item || '').trim()).filter(Boolean));
  } catch (error) {
    throw new Error(`Cannot read exclude codes file ${file}: ${error.message || error}`);
  }
}

function parseFileTypeFilter(value) {
  return new Set(
    String(value || '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  );
}

function organizationParts(row) {
  const rawCompany = firstText([
    row.companyName,
    row.company,
    row.orgName,
    row.deptName,
    row.departmentName
  ]);
  const normalized = normalizeCompany(rawCompany);
  const segments = normalized
    .split(/[/\\>｜|]+|\s+-\s+|\s+—\s+/u)
    .map((item) => item.trim())
    .filter(Boolean);

  const explicitCompany = firstText([
    row.countyCompanyName,
    row.areaCompanyName,
    row.subCompanyName,
    row.companyShortName
  ]);
  const explicitBranch = firstText([
    row.branchName,
    row.bzName,
    row.bz_name,
    row.officeName,
    row.stationName,
    row.gridName,
    row.outletName,
    row.subDeptName
  ]);

  let companyName = explicitCompany;
  let branchName = explicitBranch;

  if (!companyName) {
    companyName = segments.find((item) => /分公司$/u.test(item)) || segments[0] || normalized;
  }
  if (!branchName && segments.length > 1) {
    const companyIndex = segments.indexOf(companyName);
    branchName = segments[companyIndex >= 0 ? companyIndex + 1 : 1] || '';
  }

  return {
    companyName: normalizeCompany(companyName) || '未知分公司',
    branchName: branchName || '未知支局'
  };
}

function flatAttachmentFileName(attachment) {
  const org = organizationParts(attachment);
  const safetyCode = safeName(attachment.safetyCode, attachment.id || '无单据编号', 40);
  const companyName = safeName(org.companyName, '未知分公司', 80);
  const branchName = safeName(org.branchName, '未知支局', 80);
  const fileLabel = safeName(attachment.fileLabel, '未知附件类型', 60);
  const sourceName = safeFileName(
    attachment.fileOldName,
    `${attachment.filePath || attachment.fileId || '附件'}.bin`,
    120
  );
  const parsed = path.parse(sourceName);
  if (attachment.isImage) {
    const imageBase = safeName(
      `${safetyCode}_${companyName}_${fileLabel}`,
      `${safetyCode}_未知分公司_未知附件类型`,
      220
    );
    return `${imageBase}${parsed.ext || ''}`;
  }
  const base = safeName(
    `${safetyCode}_${companyName}_${branchName}_${fileLabel}_${parsed.name || '附件'}`,
    `${safetyCode}_${fileLabel}_附件`,
    220
  );
  return `${base}${parsed.ext || ''}`;
}

function collectAttachments(rows, fileTypeFilter, excludedSafetyCodes) {
  const attachments = [];

  for (const row of rows) {
    const safetyCode = String(row.safetyCode || '').trim();
    if (safetyCode && excludedSafetyCodes.has(safetyCode)) continue;
    for (const fileGroup of row.dailySafetyFiles || []) {
      const fileType = String(fileGroup.fileType ?? '');
      if (fileTypeFilter.size && !fileTypeFilter.has(fileType)) continue;

      const files = parseAttachmentUrl(fileGroup.url);
      const rawLabel = fileGroupLabel(fileGroup);
      const groupLooksImage = isImageFileGroup(fileGroup);

      for (const rawFile of files) {
        const file = normalizeAttachmentFile(rawFile);
        const attachment = {
          id: row.id,
          safetyCode: row.safetyCode,
          theme: row.theme,
          companyName: row.companyName,
          manageTime: row.manageTime,
          createTime: row.createTime,
          fileId: fileGroup.fileId,
          fileType,
          fileLabel: rawLabel,
          ...organizationParts(row),
          fileOldName: file.fileOldName,
          filePath: file.filePath,
          type: file.type,
          isImage: groupLooksImage || isImageAttachment(file)
        };
        attachments.push(attachment);
      }
    }
  }

  return attachments;
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(file);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

function manifestAttachmentPath(dataDir, attachmentsDir, savedPath) {
  const relativePath = String(savedPath || '').trim();
  if (!relativePath) return null;

  const attachmentRoot = path.resolve(attachmentsDir);
  const candidate = path.resolve(dataDir, relativePath);
  const relativeToRoot = path.relative(attachmentRoot, candidate);
  if (
    !relativeToRoot ||
    relativeToRoot === '..' ||
    relativeToRoot.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relativeToRoot)
  ) {
    return null;
  }

  try {
    const resolvedRoot = fs.realpathSync(attachmentRoot);
    const resolvedFile = fs.realpathSync(candidate);
    const resolvedRelative = path.relative(resolvedRoot, resolvedFile);
    if (
      !resolvedRelative ||
      resolvedRelative === '..' ||
      resolvedRelative.startsWith(`..${path.sep}`) ||
      path.isAbsolute(resolvedRelative) ||
      !fs.statSync(resolvedFile).isFile()
    ) {
      return null;
    }
    return {
      path: resolvedFile,
      savedPath: path.relative(dataDir, resolvedFile)
    };
  } catch {
    return null;
  }
}

function manifestHashIndex(previousRows, attachmentsDir, dataDir) {
  const index = new Map();
  const recordedPaths = new Set();
  for (const row of previousRows) {
    const sha256 = String(row.sha256 || '').trim();
    if (!/^[a-f0-9]{64}$/iu.test(sha256)) continue;
    const attachment = manifestAttachmentPath(dataDir, attachmentsDir, row.savedPath);
    if (!attachment) continue;
    recordedPaths.add(attachment.path);
    if (!index.has(sha256)) index.set(sha256, attachment.savedPath);
  }
  return { index, recordedPaths };
}

function attachmentStorePath(attachmentStore, sha256) {
  const digest = String(sha256 || '').trim().toLowerCase();
  if (!/^[a-f0-9]{64}$/u.test(digest)) return null;
  const candidate = path.resolve(attachmentStore, digest);
  return path.dirname(candidate) === path.resolve(attachmentStore) ? candidate : null;
}

function readAttachmentStoreIndex(attachmentStore) {
  const index = new Map();
  const indexPath = path.join(attachmentStore, 'source-index.json');
  if (!fs.existsSync(indexPath)) return index;
  try {
    const payload = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
    for (const [sourceKey, value] of Object.entries(payload.entries || {})) {
      const sha256 = String(value?.sha256 || '').trim().toLowerCase();
      const storedPath = attachmentStorePath(attachmentStore, sha256);
      if (!storedPath || !fs.existsSync(storedPath) || !fs.statSync(storedPath).isFile()) {
        continue;
      }
      index.set(sourceKey, {
        sha256,
        contentType: String(value?.contentType || ''),
        bytes: Number(value?.bytes || 0)
      });
    }
  } catch {
    // A malformed cache is ignored; affected attachments will be downloaded again.
  }
  return index;
}

function writeAttachmentStoreIndex(attachmentStore, index) {
  const entries = Object.fromEntries(index);
  const temporary = path.join(attachmentStore, `.source-index-${crypto.randomUUID()}.tmp`);
  fs.writeFileSync(
    temporary,
    `${JSON.stringify({ version: 1, entries }, null, 2)}\n`,
    'utf8'
  );
  fs.renameSync(temporary, path.join(attachmentStore, 'source-index.json'));
}

function linkOrCopy(source, destination) {
  try {
    fs.linkSync(source, destination);
  } catch (error) {
    if (error?.code !== 'EXDEV' && error?.code !== 'EPERM') throw error;
    fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  }
}

function storeDownloadedAttachment(tempPath, attachmentStore, sha256) {
  const storedPath = attachmentStorePath(attachmentStore, sha256);
  if (!storedPath) throw new Error(`Invalid attachment SHA-256: ${sha256}`);
  try {
    fs.renameSync(tempPath, storedPath);
  } catch (error) {
    if (!fs.existsSync(storedPath)) throw error;
    fs.unlinkSync(tempPath);
  }
  return storedPath;
}

async function downloadAttachment(
  token,
  attachment,
  capturedHeaders,
  timeoutMs,
  tempPath,
  browserBridgeUrl = '',
  browserBridgeToken = ''
) {
  const body = {
    fileOldName: attachment.fileOldName,
    filePath: attachment.filePath,
    type: attachment.type
  };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const throughBridge = Boolean(browserBridgeUrl);
    const requestUrl = throughBridge ? `${browserBridgeUrl.replace(/\/$/u, '')}/binary` : DOWNLOAD_URL;
    const upstreamHeaders = buildHeaders(capturedHeaders, token, {
      'Content-Type': 'application/json;charset=utf-8'
    });
    const response = await fetch(requestUrl, {
      method: 'POST',
      headers: throughBridge
        ? {
            'Content-Type': 'application/json;charset=utf-8',
            Authorization: `Bearer ${browserBridgeToken}`
          }
        : upstreamHeaders,
      body: JSON.stringify(
        throughBridge
          ? { url: DOWNLOAD_URL, method: 'POST', headers: upstreamHeaders, body }
          : body
      ),
      signal: controller.signal
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }

    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      await response.arrayBuffer();
      throw new Error('download returned an unexpected JSON response');
    }
    if (!response.body) throw new Error('download response has no body');
    const hash = crypto.createHash('sha256');
    let bytes = 0;
    const meter = new Transform({
      transform(chunk, _encoding, callback) {
        bytes += chunk.length;
        hash.update(chunk);
        callback(null, chunk);
      }
    });
    await pipeline(
      Readable.fromWeb(response.body),
      meter,
      fs.createWriteStream(tempPath, { flags: 'wx' })
    );
    return {
      contentType,
      bytes,
      sha256: hash.digest('hex')
    };
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new Error(`download timed out after ${timeoutMs} ms`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function mapWithConcurrency(items, concurrency, handler) {
  let cursor = 0;
  const workers = Array.from(
    { length: Math.min(concurrency, items.length) },
    async () => {
      while (cursor < items.length) {
        const index = cursor;
        cursor += 1;
        await handler(items[index], index);
      }
    }
  );
  await Promise.all(workers);
}

async function main() {
  const dataDir = path.resolve(getArg('data-dir', DEFAULT_DATA_DIR));
  const tokenSourceDir = path.resolve(getArg('token-source', DEFAULT_TOKEN_SOURCE_DIR));
  const headersFile = path.resolve(
    getArg('headers-file', path.join(tokenSourceDir, 'daily-management-request.json'))
  );
  const capturedHeaders = readCapturedHeaders(headersFile);
  const browserBridgeUrl = getArg('browser-bridge-url', '');
  const browserBridgeToken = getArg(
    'browser-bridge-token',
    process.env.WORKORDER_BROWSER_BRIDGE_TOKEN || ''
  );
  const fileTypeFilter = parseFileTypeFilter(getArg('file-type', getArg('file-types', '')));
  const dryRun = hasFlag('dry-run');
  const imagesOnly = hasFlag('images-only');
  const excludeCodesArg = getArg('exclude-codes-file', '');
  const excludeCodesFile = excludeCodesArg ? path.resolve(excludeCodesArg) : '';
  const excludedSafetyCodes = readExcludedSafetyCodes(excludeCodesFile);
  const detailsFile = path.join(dataDir, 'management-api-details.json');
  const attachmentDirName = directoryName(
    getArg('attachment-dir-name', DEFAULT_ATTACHMENT_DIR_NAME)
  );
  const attachmentsDir = path.join(dataDir, attachmentDirName);
  const attachmentStore = path.resolve(
    getArg('attachment-store', path.join(dataDir, 'attachment-store'))
  );
  const manifestJson = path.join(dataDir, 'daily-attachments-manifest.json');
  const manifestCsv = path.join(dataDir, 'daily-attachments-manifest.csv');
  const manifestExcelSafeCsv = path.join(
    dataDir,
    'daily-attachments-manifest-excel-safe.csv'
  );
  const timeoutMs = Math.trunc(Number(getArg('timeout-ms', runtimeConfig.attachmentTimeoutMs)));
  const retries = Math.trunc(Number(getArg('retries', runtimeConfig.attachmentRetries)));
  const maxFailures = Math.trunc(
    Number(getArg('max-failures', runtimeConfig.attachmentMaxFailures))
  );
  const concurrency = Math.trunc(
    Number(getArg('concurrency', runtimeConfig.attachmentConcurrency))
  );

  if (!Number.isFinite(timeoutMs) || timeoutMs < 1) {
    throw new Error(`--timeout-ms must be >= 1: ${timeoutMs}`);
  }
  if (!Number.isFinite(retries) || retries < 1) {
    throw new Error(`--retries must be >= 1: ${retries}`);
  }
  if (!Number.isFinite(maxFailures) || maxFailures < 0) {
    throw new Error(`--max-failures must be >= 0: ${maxFailures}`);
  }
  if (!Number.isFinite(concurrency) || concurrency < 1) {
    throw new Error(`--concurrency must be >= 1: ${concurrency}`);
  }
  if (browserBridgeUrl && !browserBridgeToken) {
    throw new Error('--browser-bridge-token is required with --browser-bridge-url');
  }

  if (!fs.existsSync(detailsFile)) {
    throw new Error(`Missing details file: ${detailsFile}`);
  }

  ensureDir(attachmentsDir);
  ensureDir(attachmentStore);

  const details = JSON.parse(fs.readFileSync(detailsFile, 'utf8'));
  const collectedAttachments = collectAttachments(
    details.rows || [],
    fileTypeFilter,
    excludedSafetyCodes
  );
  const attachments = imagesOnly
    ? collectedAttachments.filter((attachment) => attachment.isImage)
    : collectedAttachments;
  const token =
    process.env.WORKORDER_TOKEN ||
    findSavedToken(dataDir) ||
    findSavedToken(tokenSourceDir) ||
    findLatestToken(tokenSourceDir) ||
    capturedBearerToken(capturedHeaders);
  const previousRows = readManifestRows(manifestJson);
  const manifest = [];

  if (!dryRun && attachments.length && !token && !browserBridgeUrl) {
    throw new Error(
      'No login token found. Run login capture again or set WORKORDER_TOKEN.'
    );
  }

  if (excludeCodesFile) {
    console.log(`Explicitly excluded safety codes: ${excludedSafetyCodes.size}`);
  }
  console.log(`Attachments found: ${attachments.length}`);
  console.log(`Images only: ${imagesOnly}`);
  console.log(`File type filter: ${fileTypeFilter.size ? Array.from(fileTypeFilter).join(',') : 'all'}`);
  console.log(`Captured headers used: ${Boolean(Object.keys(capturedHeaders).length)}`);
  console.log(`Flat attachment output: ${attachmentsDir}`);
  console.log(`Shared attachment store: ${attachmentStore}`);
  console.log(`Download timeout/retries: ${timeoutMs} ms / ${retries} attempts`);

  const { index: hashIndex } = manifestHashIndex(
    previousRows,
    attachmentsDir,
    dataDir
  );
  const sharedSourceIndex = readAttachmentStoreIndex(attachmentStore);
  const previousBySource = new Map();
  for (const row of previousRows) {
    const key = attachmentSourceKey(row);
    if (key && row.sha256) previousBySource.set(key, row);
  }
  const reservedPaths = new Set();
  let queue = attachments.flatMap((attachment) => {
    const sourceKey = attachmentSourceKey(attachment);
    const cached = previousBySource.get(sourceKey);
    const cachedPath = cached?.sha256 ? hashIndex.get(cached.sha256) : '';
    if (!dryRun && cachedPath) {
      const record = {
        ...attachment,
        savedPath: cachedPath,
        status: 'reused',
        contentType: cached.contentType || '',
        bytes: Number(cached.bytes || 0),
        sha256: cached.sha256,
        duplicateOf: cached.duplicateOf || '',
        attempts: 0,
        error: ''
      };
      reservedPaths.add(path.join(dataDir, cachedPath));
      manifest.push(record);
      return [];
    }

    const shared = sharedSourceIndex.get(sourceKey);
    const sharedPath = shared && attachmentStorePath(attachmentStore, shared.sha256);
    if (!dryRun && sharedPath && fs.existsSync(sharedPath)) {
      const filename = flatAttachmentFileName(attachment);
      const savedPath = uniquePath(attachmentsDir, filename, reservedPaths);
      linkOrCopy(sharedPath, savedPath);
      const record = {
        ...attachment,
        savedPath: path.relative(dataDir, savedPath),
        status: 'reused',
        contentType: shared.contentType,
        bytes: shared.bytes,
        sha256: shared.sha256,
        duplicateOf: '',
        attempts: 0,
        error: ''
      };
      hashIndex.set(shared.sha256, record.savedPath);
      manifest.push(record);
      return [];
    }

    const filename = flatAttachmentFileName(attachment);
    const savedPath = uniquePath(attachmentsDir, filename, reservedPaths);
    const record = {
      ...attachment,
      savedPath: path.relative(dataDir, savedPath),
      status: dryRun ? 'planned' : 'pending',
      contentType: '',
      bytes: 0,
      sha256: '',
      duplicateOf: '',
      attempts: 0,
      error: ''
    };
    manifest.push(record);
    return [{ attachment, savedPath, record }];
  });

  if (dryRun) queue = [];
  for (let attempt = 1; attempt <= retries && queue.length; attempt += 1) {
    if (attempt > 1) console.log(`Retry queue round ${attempt}: ${queue.length} attachments`);
    const retryQueue = [];

    await mapWithConcurrency(queue, concurrency, async ({ attachment, savedPath, record }) => {
      record.attempts = attempt;
      record.error = '';
      const tempPath = path.join(attachmentsDir, `.download-${crypto.randomUUID()}.part`);
      try {
        const result = await downloadAttachment(
          token,
          attachment,
          capturedHeaders,
          timeoutMs,
          tempPath,
          browserBridgeUrl,
          browserBridgeToken
        );
        record.contentType = result.contentType;
        record.bytes = result.bytes;
        record.sha256 = result.sha256;
        const storedPath = storeDownloadedAttachment(tempPath, attachmentStore, result.sha256);
        linkOrCopy(storedPath, savedPath);
        const relativeSavedPath = path.relative(dataDir, savedPath);
        const duplicatePath = hashIndex.get(result.sha256);
        hashIndex.set(result.sha256, relativeSavedPath);
        sharedSourceIndex.set(attachmentSourceKey(attachment), {
          sha256: result.sha256,
          contentType: result.contentType,
          bytes: result.bytes
        });
        record.savedPath = relativeSavedPath;
        record.status = duplicatePath ? 'skipped_duplicate' : 'downloaded';
        record.duplicateOf = duplicatePath || '';
      } catch (error) {
        if (fs.existsSync(tempPath)) fs.unlinkSync(tempPath);
        record.error = error.message || String(error);
        if (attempt < retries) {
          record.status = 'retrying';
          retryQueue.push({ attachment, savedPath, record });
        } else {
          record.status = 'failed';
        }
      }

      console.log(
        `${manifest.indexOf(record) + 1}/${attachments.length} ${record.status}: ` +
          `${attachment.id} ${attachment.fileLabel} ${attachment.fileOldName}`
      );
    });
    queue = retryQueue;
  }

  for (let index = 0; index < manifest.length; index += 1) {
    const record = manifest[index];
    if (record.status !== 'planned') continue;
    const attachment = attachments[index];
    console.log(
      `${index + 1}/${attachments.length} planned: ${attachment.id} ` +
        `${attachment.fileLabel} ${attachment.fileOldName}`
    );
  }

  fs.writeFileSync(
    manifestJson,
    `${JSON.stringify(
      {
        exportedAt: new Date().toISOString(),
        runId: process.env.WORKORDER_RUN_ID || '',
        downloadUrl: DOWNLOAD_URL,
        fileTypeFilter: Array.from(fileTypeFilter),
        dryRun,
        timeoutMs,
        retries,
        maxFailures,
        concurrency,
        total: manifest.length,
        skippedExistingSafetyCodes: excludedSafetyCodes.size,
        downloadableAttachments: attachments.length,
        fileTypeLabels: FILE_TYPE_LABELS,
        rows: manifest
      },
      null,
      2
    )}\n`,
    'utf8'
  );
  writeAttachmentStoreIndex(attachmentStore, sharedSourceIndex);
  writeCsv(manifestCsv, manifest);
  writeCsv(manifestExcelSafeCsv, manifest, true);

  const downloaded = manifest.filter((item) => item.status === 'downloaded').length;
  const reused = manifest.filter((item) => item.status === 'reused').length;
  const skippedDuplicate = manifest.filter((item) => item.status === 'skipped_duplicate').length;
  const failed = manifest.filter((item) => item.status === 'failed').length;
  const retryAttempts = manifest.reduce((sum, item) => sum + Math.max(0, item.attempts - 1), 0);
  const imageCount = manifest.filter((item) => item.isImage).length;
  console.log(`Downloaded: ${downloaded}`);
  console.log(`Reused from previous manifest: ${reused}`);
  console.log(`Skipped duplicate content: ${skippedDuplicate}`);
  console.log(`Failed: ${failed}`);
  console.log(`Retry attempts: ${retryAttempts}`);
  console.log(`Images among attachments: ${imageCount}`);
  console.log(`Manifest JSON: ${manifestJson}`);
  console.log(`Manifest CSV: ${manifestCsv}`);
  console.log(`Manifest Excel-safe CSV: ${manifestExcelSafeCsv}`);
  if (failed > maxFailures) {
    console.error(
      `Attachment failure threshold exceeded: failed=${failed} allowed=${maxFailures}`
    );
    process.exitCode = 2;
  }
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
