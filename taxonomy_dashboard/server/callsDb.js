'use strict';

const { Pool } = require('pg');
const path = require('path');
// Root .env (../../.env from server/) holds APP_DB_* credentials for ai-calls-analysis-db
require('dotenv').config({ path: path.resolve(__dirname, '../../.env') });
// Also load taxonomy_dashboard/.env so local vars aren't lost
require('dotenv').config({ path: path.resolve(__dirname, '../.env') });

const callsPool = new Pool({
  host:     process.env.APP_DB_HOST,
  port:     parseInt(process.env.APP_DB_PORT || '5432', 10),
  database: process.env.APP_DB_NAME,
  user:     process.env.APP_DB_USER,
  password: process.env.APP_DB_PASS,
  max: 5,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: parseInt(process.env.CALLS_DB_CONNECT_TIMEOUT_MS || '8000', 10),
});

callsPool.on('error', err => console.error('[callsPool] idle client error:', err.message));

module.exports = callsPool;
