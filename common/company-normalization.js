const fs = require('fs');
const path = require('path');

const DEFAULT_RULES_FILE = path.resolve(__dirname, '..', 'config', 'classification_rules.json');
const FALLBACK_PREFIXES = ['示例分公司'];

function loadCompanyPrefixes(file = DEFAULT_RULES_FILE) {
  try {
    const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
    const prefixes = Array.isArray(payload.company_prefixes)
      ? payload.company_prefixes.map((value) => String(value).trim()).filter(Boolean)
      : [];
    return prefixes.length ? prefixes : FALLBACK_PREFIXES;
  } catch {
    return FALLBACK_PREFIXES;
  }
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const DEFAULT_COMPANY_PREFIXES = loadCompanyPrefixes();

function normalizeCompany(value, prefixes = DEFAULT_COMPANY_PREFIXES) {
  const original = String(value || '').trim();
  let normalized = original;
  for (const prefix of prefixes) {
    normalized = normalized
      .replace(new RegExp(`^${escapeRegExp(prefix)}(?:[/\\\\>｜|_\\-\\s]+)?`, 'u'), '')
      .trim();
  }
  return normalized || original;
}

module.exports = { loadCompanyPrefixes, normalizeCompany };
