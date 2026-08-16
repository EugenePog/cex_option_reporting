// pm2 process definitions. Run from the repo root: `pm2 start ecosystem.config.js`
//
// IMPORTANT: these use `python -m app.cli ...` (run as a module) so package imports resolve.
// pm2 runs each app with cwd = this file's directory (the repo root).
//
// Interpreter: set to your venv's python for reliability, e.g.
//   const PY = __dirname + "/.venv/bin/python";
// Falling back to "python3" assumes the venv is already activated in pm2's environment.
const PY = process.env.CEX_PYTHON || "python3";

const common = {
  interpreter: "none",   // we invoke the python binary directly via `script`
  cwd: __dirname,
  autorestart: true,
  max_restarts: 10,
  env: { PYTHONUNBUFFERED: "1" },
};

module.exports = {
  apps: [
    {
      // Snapshot collector: point-in-time data, fires at each SNAPSHOT_TIMES entry. Long-lived.
      name: "collector-snapshot",
      script: PY,
      args: "-m app.cli snapshot --loop",
      ...common,
    },
    {
      // History collector: fills/closed/bills, once/day at INGEST_HOUR_UTC. Long-lived.
      name: "collector-history",
      script: PY,
      args: "-m app.cli history --loop",
      ...common,
    },
    {
      name: "pipeline",
      script: PY,
      args: "-m app.cli pipeline --loop",
      ...common,
    },
    {
      name: "web",
      script: PY,
      args: "-m uvicorn app.web.main:app --host 0.0.0.0 --port 8000",
      ...common,
    },
    {
      name: "worker", // optional: alerts / reports
      script: PY,
      args: "-m app.cli worker --loop",
      ...common,
    },
  ],
};

// Mode B (manual backfill) is a one-off, not a pm2 service:
//   python -m app.cli backfill
