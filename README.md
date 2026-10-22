# WAN Watch for Home Assistant

WAN Watch monitors individual internet connections, router chains and the internet connection seen by Home Assistant. Each provider is tested through its own router interface, including standby interfaces.

## What it measures

| Measurement | Meaning |
| --- | --- |
| Internet available | At least one TLS/HTTPS target answered through this channel. All targets must fail before the channel is considered down. |
| Physical link | The interface link state reported by the router. |
| Availability measurement valid | The monitor obtained an availability result. An SSH or probe execution error means unknown, not down. |
| Packet loss, latency and jitter | ICMP measurements across configured targets. Missing replies count as packet loss; a failed measurement leaves quality unavailable. |
| Quality measurement valid | All configured quality probes executed successfully. |
| Active provider | The router's default route, followed through configured upstream routers. Multiple equal-cost routes can name multiple providers. |
| Router Ping Check | The router's own health-check opinion, kept separate from the independent checks. |
| Availability, downtime and coverage over 24 hours | Availability excludes unobserved time. Coverage exposes how much of the window was actually measured. |
| Interface counters and router uptime | Traffic, errors, drops and reboot evidence. |
| Router resources | CPU utilization between polls, memory used/available/total and one-minute load average. |
| Bounded download | Opt-in, interface-pinned HTTPS download with separate result validity and measurement time. |
| Last outage | The latest confirmed outage, with recovery time when observed. |

Availability is measured using IPv4 TLS/HTTPS endpoints with DNS resolution bypassed. It proves access to those endpoints, not every website or DNS service. Quality averages include every configured target. Per-target results are available in the status API. Jitter is the mean absolute difference between consecutive reply times within each target, then averaged across targets.

The Home Assistant probe runs on the Home Assistant host. A client computer's Wi-Fi or corporate VPN can fail while Home Assistant still has internet access. These client failures are outside this measurement location.

## Supported routers

- Keenetic with an SSH shell, Entware `/opt/bin/busybox`, `curl` with CA certificates, and `ndmc`.
- Linux routers with an SSH shell, `ip`, `ping`, `curl` with CA certificates, and `/proc`.

There is no fixed limit of two routers or two providers. Every router has its own credentials and every channel names its interface. A channel may lead to another configured router instead of directly to an internet provider. Cycles and duplicate interfaces are rejected.

The first version reads the main IPv4 routing table. Policy routing, client-specific routing policies, routing marks and VPN routing tables require a dedicated adapter before their active provider can be identified reliably. Provider-bound probes still require the operating system to have a working route through that interface. The integration never changes routes or failover settings.

## Install

Requires Home Assistant 2026.10 or later. The implementation is tested against 2026.10.1.

For manual installation, copy `custom_components/wan_watch` into the `custom_components` directory of your Home Assistant configuration, then restart Home Assistant.

For HACS, add this GitHub repository as a custom repository of type Integration, install WAN Watch, and restart Home Assistant. This project is not part of the HACS default catalog.

Open **Settings > Devices & services > Add integration > WAN Watch**.

1. Name the monitored network.
2. Add each router. Confirm its SSH host fingerprint against a trusted connection. Passwords or unencrypted private keys are stored in the native Home Assistant config entry. Blank secret fields preserve existing credentials when editing.
3. Add each provider channel with its Linux interface, such as `eth0` or `ppp0`. For Keenetic, also specify its logical interface, such as `PPTP0`, to read link and Ping Check state.
4. For a router-to-router uplink, select the next router in **Upstream router**. For a direct provider, select **Provider**.
5. Select the router immediately serving Home Assistant as the egress router.
6. Configure probe targets and notifications, then save.

Each target has `address`, `port` and `server_name`. The address must be IPv4. The server must support valid public TLS certificates and HTTPS HEAD requests. Defaults use three endpoints across two operators. Some networks block HTTPS to public DNS addresses; the third target is an ordinary website. Targets are configurable because no endpoint is reachable from every network. The probe validates certificates and never disables host-key or TLS verification.

Configuration can be edited through the integration's options. Identifiers are stable entity identities; renaming a display name does not change them.

## Router resources and bounded downloads

CPU utilization is calculated from successive `/proc/stat` samples. The first sample after startup or a router reboot has no CPU percentage. Memory use excludes memory reported as available by the kernel; older kernels use free memory, buffers and reclaimable cache. Load average is a task count, not CPU percentage. Failure to read optional resource files does not invalidate independent channel probes.

A bounded download is disabled by default. In options, select channels, a size (1-20 MB), an interval (at least one hour) and an HTTPS endpoint. The default requests 5,000,000 bytes from Cloudflare every three hours per selected channel. Two channels consume about 80 MB/day of payload, plus protocol overhead. Tests run sequentially in a background task, independently of availability polling. No upload test or automatic retries run. Attempts are stored before execution so reloads and restarts preserve the interval, including after a failure.

The endpoint object contains `address`, `port`, `server_name`, and `path`, with one `{bytes}` placeholder. The IP address is pinned to avoid relying on the active provider's DNS. The default address may need updating if the endpoint moves. The request binds strictly to the selected interface, verifies TLS, disables proxies and redirects, writes to `/dev/null`, and stops after 20 seconds. **curl 8.4.0 or later is required for bounded downloads** so the size cap also applies when the response has no known length. A range header and maximum file size both limit the response. No test files are stored on the router.

Only a complete HTTP 200/206 response of exactly the requested size yields a speed value. Partial responses, timeouts and HTTP errors produce `failed` with unavailable speed, never zero speed or an internet outage. A result older than the interval plus two minutes becomes `stale`. Status and timestamp entities distinguish a previous short test from current connectivity. Measurements include connection setup and server performance; a small single-stream download is not a maximum-capacity benchmark. Reference: [curl options](https://curl.se/docs/manpage.html) and [Cloudflare measurement API](https://github.com/cloudflare/speedtest).

When using the exact-command SSH guard, regenerate it after enabling downloads or changing their endpoint/size. Set `download: true` on selected guard channels and optionally set top-level `download_target` and `download_bytes`; these must match integration options. New resource readings also require the updated guard. Existing keys and credentials remain unchanged.

## Notifications

Select both **channels** and **event types**. Empty channel or event selections disable alerts. Select **Whole network** to include Home Assistant internet availability and all route changes. A route change also matches selected providers that appear on either side of the switch.

Events include outage, recovery, degraded quality, restored quality, measurement failure, measurement recovery, route change and whole-network internet loss/recovery. A configurable number of consecutive observations confirms changes. The first valid observation establishes a baseline without sending an alert. Unknown measurements break confirmation streaks.

Local persistent notifications work without a bot. Optional `notify` entities use Home Assistant's `notify.send_message` service. For Telegram, configure the official Telegram Bot integration separately and select its notification entity. WAN Watch does not store a Telegram token or depend on any external secret manager.

Notifications are queued in native Home Assistant storage and retried with bounded backoff. A restored connection can deliver an outage and recovery together, with their original timestamps. The queue keeps at most 256 events. Delivery is at least once: an interruption after sending can produce a duplicate. Local notifications reuse one notification per network to limit clutter. Changing alert filters removes queued events that no longer match.

The event bus emits `wan_watch_event` for all confirmed changes, independently of notification filters. Automations can filter `entry_id`, `subject` and `type`. Notifications during a complete internet outage require an independently working delivery path; otherwise the queue delivers them later.

## History and status

Entities use Home Assistant Recorder and numeric sensors support long-term statistics. Recorder retention is controlled by Home Assistant. WAN Watch also keeps 24 hours of availability observations, the latest outage for each channel and the last 256 events in its own native store.

The authenticated `GET /api/wan_watch/status` endpoint returns schema version 1, the current measurements and recent events. It excludes router credentials and host addresses. It includes configured display names, interface names, probe target addresses and measurement metadata; treat the response as private operational data. No anonymous endpoint or public bot is installed.

Changing probe targets or router topology starts a new measurement baseline and a new 24-hour window. Event history is retained. Changing notification filters preserves measurements.

After a gap longer than two collection intervals, elapsed unobserved time is counted as unknown. An outage's wall-clock duration is not the same as measured downtime; `contains_unknown` identifies observed gaps within an open outage. A recovery observed after a restart is recorded at observation time.

## SSH permissions

Prefer a dedicated monitoring key. An optional exact-command guard can limit the key to the configured read-only checks. See [the security and deployment notes](docs/security.md). The integration does not install SSH keys or router software automatically.

## Development

```sh
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Transport tests require `asyncssh==2.24.0`. Home Assistant adapter tests additionally require the supported Home Assistant version. Tests without these dependencies skip only the affected test modules. CI installs both dependencies and runs the complete suite.

The pure state engine is independent of Home Assistant and network I/O. The SSH transport owns command construction and bounded probe execution. The coordinator owns polling, persistence, notifications and Home Assistant events.

The integration is read-only on routers. Live tests do not intentionally disconnect a provider or change failover configuration.
