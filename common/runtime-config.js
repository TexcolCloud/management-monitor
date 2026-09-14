const fs = require('fs');
const path = require('path');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const DEFAULT_CONFIG_FILE = path.join(PROJECT_ROOT, 'config', 'runtime.json');

function nested(payload, section, key, fallback) {
  const values = payload?.[section];
  return values && typeof values === 'object' && values[key] !== undefined
    ? values[key]
    : fallback;
}

function env(name, fallback) {
  return process.env[name] === undefined || process.env[name] === ''
    ? fallback
    : process.env[name];
}

function positiveNumber(name, value, minimum = 0) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum) {
    throw new Error(`${name} must be a number >= ${minimum}: ${value}`);
  }
  return parsed;
}

function directoryName(name, value) {
  const text = String(value || '').trim();
  if (!text || text === '.' || text === '..' || path.basename(text) !== text || path.isAbsolute(text)) {
    throw new Error(`${name} must be a single directory name: ${value}`);
  }
  return text;
}

function integerList(name, value) {
  const values = typeof value === 'string' ? value.split(',') : value;
  if (!Array.isArray(values)) throw new Error(`${name} must be an integer list`);
  return values.map((item) => {
    const parsed = Number(item);
    if (!Number.isInteger(parsed)) throw new Error(`${name} must be an integer list`);
    return parsed;
  });
}

function loadRuntimeConfig(configFile = process.env.WORKORDER_CONFIG || DEFAULT_CONFIG_FILE) {
  const resolved = path.isAbsolute(configFile) ? configFile : path.join(PROJECT_ROOT, configFile);
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(resolved, 'utf8'));
  } catch (error) {
    throw new Error(`Cannot load runtime config ${resolved}: ${error.message || error}`);
  }

  return {
    listApiUrl: env('WORKORDER_LIST_API_URL', nested(payload, 'api', 'list_url', '')),
    detailApiUrl: env('WORKORDER_DETAIL_API_URL', nested(payload, 'api', 'detail_url', '')),
    attachmentDownloadUrl: env(
      'WORKORDER_ATTACHMENT_URL',
      nested(payload, 'api', 'attachment_download_url', '')
    ),
    portalLoginUrl: env('WORKORDER_PORTAL_URL', nested(payload, 'portal', 'login_url', '')),
    retryableApiCodes: integerList(
      'network.retryable_api_codes',
      env(
        'WORKORDER_RETRYABLE_API_CODES',
        nested(payload, 'network', 'retryable_api_codes', [408, 429, 500, 502, 503, 504])
      )
    ),
    loginWaitMs: positiveNumber(
      'portal.login_wait_ms',
      env('WORKORDER_LOGIN_WAIT_MS', nested(payload, 'portal', 'login_wait_ms', 300000)),
      1
    ),
    postLoginSettleMs: positiveNumber(
      'portal.post_login_settle_ms',
      env(
        'WORKORDER_POST_LOGIN_SETTLE_MS',
        nested(payload, 'portal', 'post_login_settle_ms', 5000)
      )
    ),
    replayClickDelayMs: positiveNumber(
      'portal.replay_click_delay_ms',
      env(
        'WORKORDER_REPLAY_CLICK_DELAY_MS',
        nested(payload, 'portal', 'replay_click_delay_ms', 800)
      )
    ),
    replayStepTimeoutMs: positiveNumber(
      'portal.replay_step_timeout_ms',
      env(
        'WORKORDER_REPLAY_STEP_TIMEOUT_MS',
        nested(payload, 'portal', 'replay_step_timeout_ms', 12000)
      ),
      1
    ),
    attachmentTimeoutMs: positiveNumber(
      'network.attachment_timeout_ms',
      env(
        'WORKORDER_ATTACHMENT_TIMEOUT_MS',
        nested(payload, 'network', 'attachment_timeout_ms', 60000)
      ),
      1
    ),
    attachmentRetries: positiveNumber(
      'network.attachment_retries',
      env(
        'WORKORDER_ATTACHMENT_RETRIES',
        nested(payload, 'network', 'attachment_retries', 3)
      ),
      1
    ),
    attachmentConcurrency: positiveNumber(
      'network.attachment_concurrency',
      env(
        'WORKORDER_ATTACHMENT_CONCURRENCY',
        nested(payload, 'network', 'attachment_concurrency', 3)
      ),
      1
    ),
    attachmentMaxFailures: positiveNumber(
      'network.attachment_max_failures',
      env(
        'WORKORDER_ATTACHMENT_MAX_FAILURES',
        nested(payload, 'network', 'attachment_max_failures', 0)
      )
    ),
    captureRoot: env(
      'WORKORDER_CAPTURE_ROOT',
      nested(payload, 'paths', 'capture_root', path.join('capture-output', 'daily-management'))
    ),
    exportRoot: env('WORKORDER_EXPORT_ROOT', nested(payload, 'paths', 'export_root', 'excel-output')),
    headersFile: env(
      'WORKORDER_HEADERS_FILE',
      nested(
        payload,
        'paths',
        'headers_file',
        path.join('capture-output', 'daily-management', 'daily-management-request.json')
      )
    ),
    tokenSourceDir: env(
      'WORKORDER_TOKEN_SOURCE_DIR',
      nested(payload, 'paths', 'token_source_dir', path.join('capture-output', 'daily-management'))
    ),
    attachmentDirName: directoryName(
      'paths.attachment_dir_name',
      env(
        'WORKORDER_ATTACHMENT_DIR_NAME',
        nested(payload, 'paths', 'attachment_dir_name', 'daily-attachments')
      )
    ),
    loginProfileDir: env(
      'WORKORDER_LOGIN_PROFILE_DIR',
      nested(payload, 'paths', 'login_profile_dir', path.join('.temp', 'daily-management-login-profile'))
    ),
    loginNavigationFile: env(
      'WORKORDER_LOGIN_NAVIGATION_FILE',
      nested(
        payload,
        'paths',
        'login_navigation_file',
        path.join('data_acquisition', 'recordings', 'portal-navigation.local.json')
      )
    )
  };
}

module.exports = { loadRuntimeConfig, PROJECT_ROOT };
