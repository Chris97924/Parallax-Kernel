// PM2 ecosystem for Parallax Kernel + Lane C v0.2.0-beta shadow observability.
//
// Usage:
//   pm2 start pm2/ecosystem.config.js
//   pm2 logs parallax
//   pm2 stop parallax
//
// Designed for the ZenBook deploy (<user>@<zenbook-host>). Default ports match
// the runbook (`docs/lane-c/m2-rollout-runbook.md`).
//
// Log files:
//   - /var/log/parallax/parallax.out.log   (stdout)
//   - /var/log/parallax/parallax.err.log   (stderr — JSON-formatted records
//     from parallax.obs.log; daily-rotation handled by PM2)
//   - /var/log/parallax/shadow/shadow-decisions-YYYY-MM-DD.jsonl
//     (written directly by parallax.router.shadow; not routed through PM2)
//
// To tail the WS-3 metrics endpoint:
//   curl -s http://127.0.0.1:8765/metrics | grep parallax_shadow_

module.exports = {
  apps: [
    {
      name: 'parallax',
      script: 'uvicorn',
      args: 'parallax.server.app:app --host 127.0.0.1 --port 8765',
      cwd: '/opt/parallax',
      interpreter: 'python',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '30s',
      max_memory_restart: '1G',
      kill_timeout: 5000,
      env: {
        PARALLAX_DB_PATH: '/var/lib/parallax/parallax.db',
        PARALLAX_VAULT_PATH: '/var/lib/parallax/vault',
        // PARALLAX_BIND_HOST must mirror the uvicorn `--host` arg above so
        // `assert_safe_to_start()` reads the same bind address uvicorn
        // actually binds to. The `parallax serve` CLI subcommand sets this
        // automatically; direct `uvicorn parallax.server.app:app --host X`
        // invocation (this file) does not, so we set it explicitly here.
        // If you change `--host` above, update this value too.
        PARALLAX_BIND_HOST: '127.0.0.1',
        // SHADOW_MODE / SHADOW_USER_ALLOWLIST / SHADOW_LOG_DIR are read from
        // the systemd EnvironmentFile (see runbook §Post-merge Enablement).
        // Do NOT hardcode SHADOW_MODE=true here — flag flips must remain a
        // single point of truth in the env file.
        SHADOW_LOG_DIR: '/var/log/parallax/shadow',
      },
      out_file: '/var/log/parallax/parallax.out.log',
      error_file: '/var/log/parallax/parallax.err.log',
      log_date_format: 'YYYY-MM-DDTHH:mm:ss.SSSZ',
      merge_logs: true,
    },
  ],
};
