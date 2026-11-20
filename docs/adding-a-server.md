# Adding a server

servmon installs nothing on the servers it watches. The collector opens one SSH session
per server and pipes `servmon/agent.py` into the server's `python3`. So adding a
server means three things: the monitoring machine can SSH in non-interactively, the
server has a usable Python, and there is a `[[host]]` block in `config.toml`.

## 1. Check the server

| Requirement | How to check (on the server) |
|---|---|
| Linux with `/proc` | — |
| Python ≥ 3.6 (any `python3` works; tested on 3.10–3.14) | `python3 --version` |
| You can see other users' processes | `ps -e -o user= \| sort -u` lists more than you. If it lists only you, `/proc` is mounted with `hidepid` and servmon can only report your own usage. |
| NVIDIA GPUs: `nvidia-smi` works for your user | `nvidia-smi pmon -c 1` lists processes, including other users'. No GPUs: set `no_gpu = true` in step 4. |

No root, sudo, open ports or daemons are needed. The login shell doesn't matter (fish works).

## 2. Make SSH work non-interactively

The collector runs `ssh -o BatchMode=yes <alias>`, so it can't type a password or a key
passphrase. The collector container uses the SSH directory named by `SSH_DIR` in `.env`
(default: your `~/.ssh`), mounted read-only.

1. **Key:** use a key without a passphrase, since ssh-agent isn't available inside the container.
   A dedicated key is cleanest:
   ```bash
   ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_servmon -C servmon
   ssh-copy-id -i ~/.ssh/id_servmon.pub you@newserver.example.edu
   ```
2. **Host alias** in `~/.ssh/config`. Write paths as `~/.ssh/...`, not `/home/you/.ssh/...`:
   inside the container, home is `/home/servmon`.
   ```sshconfig
   # direct
   Host lab-newserver
       HostName newserver.example.edu
       User you
       IdentityFile ~/.ssh/id_servmon

   # behind a jump host
   Host lab-inside
       HostName 10.0.0.12
       User you
       IdentityFile ~/.ssh/id_servmon
       ProxyJump lab-gateway

   # through a Cloudflare tunnel (cloudflared is included in the collector image)
   Host lab-tunnelled
       HostName newserver-ssh.example.com
       User you
       IdentityFile ~/.ssh/id_servmon
       ProxyCommand cloudflared access ssh --hostname %h
   ```
   Any other `ProxyCommand` program (e.g. `nc`) must also exist in the image; add it to
   `deploy/docker/collector.Dockerfile`.
3. **Accept the host key once, from the host machine:**
   `ssh lab-newserver true`. `known_hosts` is mounted read-only, so the container can't
   accept new keys itself.
4. **Test it the way the collector will:**
   ```bash
   ssh -o BatchMode=yes lab-newserver true && echo ok                      # from the host
   podman exec servmon-collector ssh -o BatchMode=yes lab-newserver true && echo ok   # inside the container
   # Docker: docker compose exec collector ssh -o BatchMode=yes lab-newserver true
   ```

## 3. Try the agent once

```bash
ssh -o BatchMode=yes lab-newserver python3 -u - --interval 5 < servmon/agent.py | head -n 2
```

The command prints a JSON snapshot every 5 s; `head` stops it after two. Check that it
includes `"users"` other than you, and a `"gpus"` list with no `"gpu_error"`.

## 4. Add it to `config.toml`

```toml
[[host]]
name = "newserver"          # label in every dashboard; keep it stable
ssh = "lab-newserver"       # the Host alias from step 2
# python = "/usr/bin/python3.10"   # if `python3` isn't the right interpreter
# no_gpu = true                    # no NVIDIA GPUs
```

`name` becomes the `host` label on every metric. Renaming a server later starts a new
history under the new name; the old series remain under the old name.

## 5. Apply and verify

```bash
systemctl --user restart servmon-collector   # Podman (and native) setup
docker compose restart collector             # Docker
```

There's no rebuild: `config.toml` and `servmon/` are mounted into the container.
Then check, within about 30 s:

```bash
curl -s 127.0.0.1:9877/metrics | grep 'servmon_up{host="newserver"}'   # 1 = streaming
curl -s 127.0.0.1:9877/api/snapshot | jq '.newserver.error'           # "" when healthy
journalctl --user -u servmon-collector -n 20                          # connect / error messages
```

The server then appears in the `host` filter of every dashboard. The dashboards build
their filters from the data, so nothing needs editing.

## Removing a server

Delete its `[[host]]` block and restart the collector. Its history stays until it ages
out of retention (2 years by default). To delete the history now (this can't be undone):

```bash
curl -X POST -g '127.0.0.1:9390/api/v1/admin/tsdb/delete_series?match[]={host="newserver"}'
```

## Troubleshooting

`servmon_up{host=…}` is 0 and the reason is in `servmon_collector_error_info`, the
**Server detail** dashboard, or `journalctl --user -u servmon-collector`.

| Error | Fix |
|---|---|
| `Permission denied (publickey…)` | The key isn't in the server's `authorized_keys`, it has a passphrase, or `IdentityFile` uses an absolute host path (see step 2). |
| `Host key verification failed` | Run `ssh <alias> true` once on the host (step 2.3). |
| `Bad owner or permissions on …/.ssh/config` | `UID`/`GID` in `.env` must match the owner of `SSH_DIR` (`scripts/setup.sh` sets them). |
| `python3: command not found` | Set `python = "/path/to/python3"` in the `[[host]]` block. |
| `cloudflared: …` / `websocket: bad handshake` | The tunnel or Access app for that host is down, or it requires a login. Use a service token or another route. |
| GPU panels empty, `gpu_error` = `Failed to initialize NVML: Driver/library version mismatch` | The NVIDIA driver was upgraded without a reboot. An admin has to reboot the server; servmon retries every 5 min. |
| Only your own processes appear | `/proc` has `hidepid` on that server; ask its admin, or accept own-usage-only. |

## Tuning (applies to all servers)

`[agent]` in `config.toml` sets what counts as a job and what counts as idle;
`[collector] interval` sets the sampling period. Restart the collector after changing
either. Restarting resets the agents' idle clocks, so the 10-minute "idle-held" timers
start again.
