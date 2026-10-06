import { DatabaseSync } from "node:sqlite";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

// Upstream logs may contain query parameters. Only our JSON protocol is emitted.
for (const method of ["log", "info", "warn", "error", "debug", "trace"]) {
  console[method] = () => {};
}

const maxInputBytes = 64 * 1024;
let input = "";
try {
  for await (const chunk of process.stdin) {
    input += chunk.toString("utf8");
    if (Buffer.byteLength(input, "utf8") > maxInputBytes) throw new Error("input_too_large");
  }
  const request = JSON.parse(input);
  const resolver = createRequire(join(request.runtime_dir, "package.json"));
  const { CoreVINDecoder } = await import(pathToFileURL(resolver.resolve("@cardog/corgi/browser")).href);
  const database = new DatabaseSync(request.database_path, { readOnly: true });
  const adapter = {
    async exec(query, params = []) {
      const statement = database.prepare(query);
      const columns = statement.columns().map((column) => column.name);
      const rows = statement.all(...params);
      return [{ columns, values: rows.map((row) => columns.map((name) => row[name])) }];
    },
    async close() {
      database.close();
    },
  };
  const decoder = new CoreVINDecoder(adapter);
  try {
    const options = {
      includePatternDetails: true,
      includeRawData: false,
      includeDiagnostics: false,
    };
    if (request.model_year !== null && request.model_year !== undefined) {
      options.modelYear = request.model_year;
    }
    const result = await decoder.decode(request.identifier, options);
    process.stdout.write(JSON.stringify({ ok: true, result }));
  } finally {
    await decoder.close();
  }
} catch {
  process.stdout.write(JSON.stringify({ ok: false, error: "decoder_failed" }));
  process.exitCode = 1;
}
