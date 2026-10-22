# Changelog

Changes a user of this integration would notice, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the version numbers are the ones in `manifest.json`.

## [0.1.1] - 2026-10-22

### Changed

- Server, certificate and API key ids are read as UUIDs, as the instance id is; an answer holding another form is refused as malformed. A certificate id is sent as one escaped path segment.

## [0.1.0] - 2026-10-21

### Added

- A config flow that takes the control plane's address and an API key, checks both against `/api/instance` and `/api/whoami`, and keys the entry on the installation's instance id.
- An installation device with the proxy's state, active rules, 24-hour traffic counts, response times and the API key's expiry.
- A device per proxied server with its status and upstream health, and a device per certificate with its expiry, status and renewal state. Both follow the control plane's lists.
- Reload and Renew buttons, and the `sslreverseproxy.reload` and `sslreverseproxy.renew_certificate` actions, offered when the key grants them and run for administrators, automations and scripts. A certificate has a Renew button while Caddy manages it.
- Reauthentication when the key is refused, a repair issue with a fix flow seven days before the key expires, and reconfiguration of the address, key and certificate check.
- Diagnostics with the key, the address, ids, names, hosts, domains and error messages redacted.
