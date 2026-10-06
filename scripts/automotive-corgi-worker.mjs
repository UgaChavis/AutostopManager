// The pinned Corgi pure decode core with an explicit local, read-only SQLite adapter.
// Avoid createDecoder/getDatabasePath: those interfaces manage caches/downloads.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import dns from 'node:dns';
import tls from 'node:tls';
import dgram from 'node:dgram';
import { syncBuiltinESMExports } from 'node:module';

function denied() { throw new Error('offline_network_denied'); }
globalThis.fetch = denied;
net.connect = net.createConnection = net.Socket.prototype.connect = denied;
http.request = http.get = https.request = https.get = tls.connect = denied;
dns.lookup = dns.resolve = dns.resolve4 = dns.resolve6 = denied;
dns.promises.lookup = dns.promises.resolve = denied;
dgram.createSocket = denied;
syncBuiltinESMExports();

const runtime = path.resolve(process.argv[2]);
const manifest = JSON.parse(fs.readFileSync(path.join(runtime, 'manifest.json'), 'utf8'));
if (process.version !== manifest.node.version) throw new Error('node_runtime_version_mismatch');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const { decodeVIN } = await import(pathToFileURL(path.join(runtime, 'corgi-browser.mjs')).href);
const db = new DatabaseSync(path.join(runtime, 'vpic.lite.db'), { readOnly: true, enableLoadExtension: false });
const adapter = {
  async exec(sql, parameters = []) {
    const rows = db.prepare(sql).all(...parameters);
    const columns = rows.length ? Object.keys(rows[0]) : [];
    return [{ columns, values: rows.map(row => columns.map(key => row[key])) }];
  },
  async close() { db.close(); },
};
try {
  const options = { includeRawData: false, includePatternDetails: false, includeDiagnostics: false };
  if (input.model_year !== null && input.model_year !== undefined) options.modelYear = input.model_year;
  const decoded = await decodeVIN(input.identifier, adapter, options);
  const c = decoded.components || {};
  const vehicle = c.vehicle || {};
  const profile = {};
  const fields = {
    make: vehicle.make || c.wmi?.make,
    manufacturer: vehicle.manufacturer || c.wmi?.manufacturer,
    model: vehicle.model,
    model_year: c.modelYear?.year || vehicle.year,
    series: vehicle.series,
    trim: vehicle.trim,
    body: vehicle.bodyStyle,
    drivetrain: vehicle.driveType,
    transmission: vehicle.transmission,
    engine: c.engine?.model || c.engine?.type || vehicle.engineType,
    engine_displacement: c.engine?.displacement,
    fuel: c.engine?.fuel || vehicle.fuelType,
    country: c.plant?.country || c.wmi?.country,
  };
  for (const [key, value] of Object.entries(fields)) if (value !== undefined && value !== null && value !== '') profile[key] = value;
  const errors = (decoded.errors || []).map(error => ({ code: error.code, category: error.category, severity: error.severity }));
  process.stdout.write(JSON.stringify({ vehicle_profile: profile, decoder_valid: decoded.valid, decoder_errors: errors,
    checksum: c.checkDigit || null, model_year_source: c.modelYear?.source || null, network_calls: 0 }));
} finally { await adapter.close(); }
