const fs = require('fs');

const ADMIN_TOKEN_NAMES = new Set(['Admin-Token', 'token', 'Token']);

function targetAddress(value) {
  const text = String(value || '').trim();
  const url = new URL(text.includes('://') ? text : `http://${text}`);
  return { host: url.host.toLowerCase(), hostname: url.hostname.toLowerCase() };
}

function cookieMatchesHost(domain, hostname) {
  const normalized = String(domain || '').toLowerCase().replace(/^\./, '');
  return normalized === hostname || normalized.endsWith(`.${hostname}`);
}

function extractAdminToken(storage, adminUrl) {
  const target = targetAddress(adminUrl);
  for (const origin of storage?.origins || []) {
    let host = '';
    try {
      host = new URL(String(origin.origin || '')).host.toLowerCase();
    } catch {
      continue;
    }
    if (host !== target.host) continue;
    for (const item of origin.localStorage || []) {
      if (ADMIN_TOKEN_NAMES.has(item.name) && item.value) {
        return { token: item.value, source: `localStorage:${origin.origin}:${item.name}` };
      }
    }
  }

  for (const cookie of storage?.cookies || []) {
    if (!cookieMatchesHost(cookie.domain, target.hostname)) continue;
    if (ADMIN_TOKEN_NAMES.has(cookie.name) && cookie.value) {
      return { token: cookie.value, source: `cookie:${cookie.domain}:${cookie.name}` };
    }
  }
  return { token: '', source: '' };
}

function readStorageStateToken(file, adminUrl) {
  if (!file || !fs.existsSync(file)) return { token: '', source: '' };
  try {
    return extractAdminToken(JSON.parse(fs.readFileSync(file, 'utf8')), adminUrl);
  } catch {
    return { token: '', source: '' };
  }
}

module.exports = { extractAdminToken, readStorageStateToken };
