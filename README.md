# BB10 Assistant Reborn — deployment package

One-command deployment of the patched BB10 voice-assistant stack to any rooted
BlackBerry 10 device (Z30, Passport, Q10, Q5, Classic, Leap, Z3).

Full technical documentation: [KNOWLEDGE.md](KNOWLEDGE.md)
Protocol specs: [bb10-assistant-protocol docs](https://github.com/damianolampisti00/bb10-assistant-protocol/tree/main/docs)

## Quickstart

```bash
# 1. Run the assistant server on your PC (see KNOWLEDGE.md §2)

# 2. Deploy to the phone (Git Bash / Linux):
./deploy.sh <phone-ip> <model> <server-ip>

# example: Z30 over Wi-Fi, server on 192.168.0.157
./deploy.sh 192.168.0.166 z30 192.168.0.157

# example: Passport over USB
./deploy.sh 169.254.0.1 passport 192.168.0.157
```

## Requirements

- Rooted BB10 (Term49/getroot autoloader), ssh as root with key `~/.ssh/bb10_root_rsa`
- Python 3 on the PC (for the binary patch step; no extra packages)
- The assistant server (`server.py`) running and reachable from the phone

## Contents

| File | Purpose |
|---|---|
| `deploy.sh` | full deployment: patch → upload → configure → start → verify |
| `patch_server_ip.py` | rewrites the server URL inside the io-asr binary |
| `files/io-asr-patched.bin` | patched io-asr binary — **not in git** (too large for API push). Get it from an already-deployed phone: `ssh root@<phone> 'cat /var/rootdata/io-asr-patched-final' > files/io-asr-patched.bin`, or patch a stock `io-asr-bb10` per KNOWLEDGE.md §3. Expected sha256: `4dc6f97689ac4c78a64af93e6ee8a97b239f5fd80a90f8d70313336d0cbb7b73` |
| `files/asr_z30.cfg` | audio config with Z30 microphone fix (`pcmPreferredc`) |
| `files/asr_boot.sh` | post-boot restore script (template; SERVER_IP/SERVER_PORT substituted at deploy time) |
| `files/my_prompts.cfg` | Dragon prompts overlay (template) |

After placing the binary in `files/`, `deploy.sh` picks it up automatically.
