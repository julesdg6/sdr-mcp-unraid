# sdr-mcp-unraid

Unraid Docker wrapper and Community Applications template for [SDR MCP](https://github.com/sandraschi/sdr-mcp), bringing software defined radio capabilities to MCP-compatible AI agents.

## Project overview

This repository packages SDR MCP into an Unraid-friendly container with:

- persistent paths for `/config`, `/recordings`, and `/data`
- configurable ports and startup mode
- embedded web dashboard served on port `8766`
- optional RTL-SDR USB passthrough guidance
- Unraid 7 Community Apps template
- multi-arch GitHub Actions build and publish workflow

## Repository structure

```text
sdr-mcp-unraid/
├── .github/workflows/docker-publish.yml
├── assets/
│   ├── icon-banner.svg
│   ├── icon-radio.svg
│   ├── icon-square.png
│   └── icon-square.svg
├── docker/
│   └── entrypoint.sh
├── unraid/
│   └── sdr-mcp-unraid.xml
├── .gitignore
├── CONTRIBUTING.md
├── docker-compose.yml
├── Dockerfile
├── LICENSE
├── README.md
└── RELEASE_CHECKLIST.md
```

## Installation

### Docker CLI

```bash
docker run -d \
  --name=sdr-mcp \
  -p 10891:10891 \
  -p 8766:8766 \
  -v /mnt/user/appdata/sdr-mcp:/config \
  -v /mnt/user/data/sdr-mcp/recordings:/recordings \
  -v /mnt/user/data/sdr-mcp/data:/data \
  --restart unless-stopped \
  ghcr.io/julesdg6/sdr-mcp-unraid:latest
```

### Docker Compose

Use the included [`docker-compose.yml`](./docker-compose.yml):

```bash
docker compose up -d
```

## Unraid setup guide

1. Install the template file on Unraid:

   ```bash
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   wget -O /boot/config/plugins/dockerMan/templates-user/sdr-mcp-unraid.xml \
   https://raw.githubusercontent.com/julesdg6/sdr-mcp-unraid/main/unraid/sdr-mcp-unraid.xml
   ```

2. In Unraid, open **Docker → Add Container** and select `sdr-mcp-unraid` from the template dropdown.
3. Set:
   - **Config** path: `/mnt/user/appdata/sdr-mcp`
   - **Recordings** path: `/mnt/user/data/sdr-mcp/recordings`
   - **Data** path: `/mnt/user/data/sdr-mcp/data`
4. Optionally add SDR runtime arguments in **Extra Parameters** (examples below).
5. Start container and verify:
   - MCP endpoint: `http://<unraid-ip>:10891/mcp`
   - Web dashboard: `http://<unraid-ip>:8766`

## Port mappings

- `10891/tcp` → MCP HTTP endpoint (`/mcp`)
- `8766/tcp` → Embedded web dashboard

## Volume mappings

- `/config` → persistent config and runtime state
- `/recordings` → captured output/audio data
- `/data` → supporting datasets and exports

## SDR hardware passthrough

Preferred device passthrough:

```text
--device=/dev/bus/usb
```

Alternative for DVB devices:

```text
--device=/dev/dvb
```

If your setup needs broader USB access:

```text
--device=/dev/bus/usb --privileged=true
```

> Only use `--privileged=true` if strictly required.

## Example Extra Parameters (Unraid runtime args)

In Unraid, **Extra Parameters** are Docker runtime arguments. They should not be passed as environment variables.

```text
--device=/dev/bus/usb --security-opt=no-new-privileges:true
```

Alternative for host networking if needed:

```text
--network=host --device=/dev/bus/usb
```

## SDR hardware support notes

- **Best supported:** RTL2832U-based RTL-SDR devices (for example RTL-SDR Blog v3/v4).
- This container ships RTL-SDR tooling used by MCP operations (`sdr_list_devices`, `sdr_initialize`, etc.).
- DVB-only tuners may expose `/dev/dvb` successfully but can be limited for RTL-SDR-specific workflows.
- If your hardware only supports DVB APIs and not RTL2832U SDR mode, MCP spectrum tools may not initialize.

## Example MCP client configuration

For clients that connect over HTTP transport:

```json
{
  "mcpServers": {
    "sdr-mcp-unraid": {
      "transport": "streamable-http",
      "url": "http://unraid.local:10891/mcp"
    }
  }
}
```

## Unraid template features

The provided XML template includes:

- WebUI link (`http://[IP]:[PORT:8766]`)
- CA-compatible PNG icon URL field
- shell access (`bash`)
- bridge networking by default, with host-networking guidance
- CA-friendly metadata and categories

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Bind host for HTTP mode |
| `MCP_PORT` | `10891` | HTTP listen port |
| `FRONTEND_PORT` | `8766` | Dashboard listen port |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No RTL-SDR devices detected` | USB dongle not mapped | Add `--device` mapping and restart |
| Permission errors reading USB | Container user lacks access | Add supplemental device mapping or use privileged mode only when required |
| MCP client cannot connect | Wrong URL/port | Check `http://<host>:10891/mcp` and Unraid port mapping |
| Dashboard not reachable | Port blocked/unmapped | Confirm container exposes and maps `8766/tcp` |
| Data lost after container update | Non-persistent paths | Ensure `/config`, `/recordings`, `/data` map to host storage |

## Image metadata / labels

The Dockerfile includes OCI metadata labels suitable for registry discovery and Community Applications indexing.

## License

This wrapper is released under MIT. Upstream SDR MCP is also MIT-licensed.

See [LICENSE](./LICENSE) and [LICENSING.md](./LICENSING.md).
