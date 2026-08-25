# cloudflared-tunnel-proxy

通过本地 HTTP 代理（mihomo / Clash 等）连接 Cloudflare Edge 的 Cloudflare Tunnel 方案，适用于服务器无法直连 Cloudflare Edge 的网络环境。

## 适用场景

- 服务器到 Cloudflare Edge 的 TCP/UDP 7844 被运营商网络丢弃或极不稳定（例如中国大陆 VPS）。
- cloudflared 的隧道拨号不读取 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量（实测直接设置环境变量仍会直连 Edge IP 并超时），因此需要一个本地桥接层把流量送进代理。

## 架构

```text
                                      ┌──────────────────────────────┐
                                      │ Cloudflare Edge               │
                                      │ region1/region2:7844         │
                                      └──────────────┬───────────────┘
                                                     │
                                                     │ 同一个 Tunnel token
                                                     │
                                      ┌──────────────▼───────────────┐
                                      │ cloudflared-proxy            │
                                      │ Python supervisor            │
                                      │ HTTP/2，最多 4 条 HA 连接     │
                                      │ metrics: 127.0.0.1:20241     │
                                      └──────────────┬───────────────┘
                                                     │
                                                     │ 目标域名映射到回环地址
                                                     │ 127.0.0.2~127.0.0.9:7844
                                                     ▼
                                      ┌───────────────────────────────┐
                                      │ cloudflared-proxy-bridge      │
                                      │ 监听 127.0.0.1~127.0.0.9:7844 │
                                      │ 解析 TLS SNI                  │
                                      │ 发起 HTTP CONNECT             │
                                      └──────────────┬────────────────┘
                                                     │ HTTP CONNECT
                                                     ▼
                                      ┌───────────────────────────────┐
                                      │ mihomo / 本地代理              │
                                      │ 127.0.0.1:7890               │
                                      └───────────────────────────────┘
```

## 组件

| 组件 | 作用 |
| --- | --- |
| `proxy-bridge` | 监听 `127.0.0.1~127.0.0.9:7844`，解析 TLS SNI，向本地代理发起 HTTP CONNECT |
| `proxy-supervisor` | 运行 cloudflared 子进程，监控 `/ready`，连接长时间为 0 时自动重启 |
| `docker-compose.yml` | 两个组件的编排 |

## 为什么用多个回环 IP

cloudflared 对每个 (hostname, IP) 只建一条连接。把 `region1/region2.v2.argotunnel.com` 各映射到 4 个不同的回环地址，可以让单实例稳定建立 4 条 HA 连接，并避免 `there are no free edge addresses left to resolve to` 错误。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，填入 CLOUDFLARED_TUNNEL_TOKEN
docker compose up -d --build
curl -sS http://127.0.0.1:20241/ready
```

健康时返回 `{"status":200,"readyConnections":4,...}`。

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLOUDFLARED_TUNNEL_TOKEN` | 无 | Cloudflare Tunnel token（必需） |
| `LISTEN_IPS` | `127.0.0.1` | bridge 监听的回环地址列表（逗号分隔） |
| `LISTEN_PORT` | `7844` | bridge 监听端口 |
| `PROXY_HOST` / `PROXY_PORT` | `127.0.0.1` / `7890` | 本地 HTTP 代理地址 |
| `METRICS_ADDRESS` | `127.0.0.1:20241` | cloudflared metrics / ready 地址 |
| `CHECK_INTERVAL_SECONDS` | `10` | supervisor 健康检查间隔 |
| `STARTUP_GRACE_SECONDS` | `120` | 子进程启动宽限期 |
| `UNREADY_TIMEOUT_SECONDS` | `200` | 持续无连接多久后重启 |

## 排障

- `readyConnections` 为 0：依次检查 `7890` 是否监听、`cloudflared-proxy-bridge` 日志、mihomo 节点连通性。
- 日志出现 `no free edge addresses`：确认 `extra_hosts` 里每个域名映射了多个不同的回环 IP。
- 日志出现 UDP/QUIC 失败：属预期，桥接只转发 TCP，cloudflared 固定使用 HTTP/2。
- 不要修改 bridge 监听地址为 `0.0.0.0`，避免把代理入口暴露到公网。

## 安全

- `.env` 包含 Tunnel token，已加入 `.gitignore`，不要提交。
- 所有组件默认只监听回环地址，不对外暴露。
