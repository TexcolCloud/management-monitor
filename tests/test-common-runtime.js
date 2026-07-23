const assert = require('assert');

const { extractAdminToken } = require('../common/auth-runtime');
const { normalizeCompany } = require('../common/company-normalization');

const adminUrl = 'http://api.example.invalid:31051/api/example';
const storage = {
  origins: [
    {
      origin: 'http://api.example.invalid:31051',
      localStorage: [{ name: 'Admin-Token', value: 'local-token' }]
    }
  ],
  cookies: []
};

assert.deepStrictEqual(extractAdminToken(storage, adminUrl), {
  token: 'local-token',
  source: 'localStorage:http://api.example.invalid:31051:Admin-Token'
});
assert.strictEqual(
  extractAdminToken(
    { ...storage, origins: [{ ...storage.origins[0], origin: 'http://api.example.invalid:9999' }] },
    adminUrl
  ).token,
  ''
);
assert.strictEqual(
  extractAdminToken(
    { origins: [], cookies: [{ domain: 'api.example.invalid', name: 'Token', value: 'cookie-token' }] },
    adminUrl
  ).token,
  'cookie-token'
);
assert.strictEqual(normalizeCompany('示例分公司/城区分公司'), '城区分公司');
assert.strictEqual(normalizeCompany('示例分公司｜城区分公司'), '城区分公司');

console.log('common Node runtime tests: ok');
