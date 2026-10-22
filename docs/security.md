# Security and deployment

Router passwords and private keys are kept in Home Assistant config-entry storage. This is native storage, not a separately encrypted vault. Access to the Home Assistant configuration directory and backups must be protected. Secrets are redacted from integration diagnostics and are never added to entity attributes, event payloads or the status endpoint.

SSH host keys are pinned by SHA-256 fingerprint. Setup displays a new fingerprint for explicit verification. A host-key mismatch during polling fails closed. TLS probes validate certificates. The implementation does not accept arbitrary shell commands from configuration. Interface names and target fields are validated, and each argument is shell-quoted.

The SSH account must be able to read routing/interface state and run interface-bound probes. Some operating systems require privileges for ICMP or binding sockets. A command execution error is a measurement error, not evidence of an outage.

## Optional exact-command SSH guard

`tools/ssh_guard.py` creates a POSIX shell script containing an exact allowlist for one router. The input JSON contains only its driver, channel definitions, public probe targets and ping count. It needs no credentials.

```json
{
  "driver": "keenetic",
  "channels": [{"interface": "ppp0", "logical_interface": "PPTP0"}],
  "targets": [
    {"address": "1.1.1.1", "port": 443, "server_name": "one.one.one.one"},
    {"address": "8.8.8.8", "port": 443, "server_name": "dns.google"}
  ],
  "ping_count": 5
}
```

```sh
python tools/ssh_guard.py < router-probes.json > wan-watch-readonly
```

Install the generated script as a root-owned executable at a path suitable for the router. Add a dedicated public key to `authorized_keys` with `command="/path/to/wan-watch-readonly",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`. Confirm that the SSH server supports and enforces those restrictions. Put only that key's private counterpart in the integration's settings.

The guard compares the full SSH command literally and executes a fixed argument list. It does not evaluate client-provided shell syntax. When interfaces, targets, ping counts or command formats change, regenerate and deploy the guard. Otherwise the monitor deliberately reports an execution error.

## Removal

Remove the config entry through Home Assistant, remove the integration files through HACS or manually, and restart Home Assistant. Revoke a dedicated router key by removing its exact public-key line. Remove its guard only if no other key uses it. Home Assistant Recorder retention and backups are managed separately.
