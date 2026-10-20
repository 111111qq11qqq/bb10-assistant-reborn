#!/bin/bash
# BB10 Assistant reborn - deploy script (run from Git Bash on Windows/Linux)
#
# Deploys the patched voice-assistant stack to a rooted BB10 device.
#
# Usage:
#   ./deploy.sh <device-ip> <model> <server-ip> [server-port] [ssh-key]
#
#   device-ip   - phone IP (USB: 169.254.0.1, Wi-Fi: e.g. 192.168.0.166)
#   model       - z30 | passport | q10 | q5 | classic | leap | z3
#                 (z30 uses a fixed mic path; others start from the factory
#                 /base/etc/asr.cfg pulled off the device)
#   server-ip   - IP of the PC running server.py (assistant server)
#   server-port - default 8080
#   ssh-key     - default ~/.ssh/bb10_root_rsa
#
# Example:
#   ./deploy.sh 192.168.0.166 z30 192.168.0.157
#
# Prerequisites: rooted BB10 (Term49/getroot autoloader), ssh as root,
# python3 on the PC (for the binary patch step).

set -e
DEV=${1:?usage: deploy.sh <device-ip> <model> <server-ip> [port] [ssh-key]}
MODEL=${2:?model: z30|passport|q10|q5|classic|leap|z3}
SRV=${3:?server-ip}
PORT=${4:-8080}
KEY=${5:-~/.ssh/bb10_root_rsa}

HERE=$(cd "$(dirname "$0")" && pwd)
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o BatchMode=yes -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o IdentitiesOnly=yes -o ConnectTimeout=10 root@$DEV"

echo "[1/6] patching binary for http://$SRV:$PORT ..."
BIN="$HERE/files/io-asr-patched.bin"
if [ ! -f "$BIN" ] && [ -f "$BIN.b64" ]; then
  echo "    decoding io-asr-patched.bin from base64 ..."
  base64 -d "$BIN.b64" > "$BIN"
fi
if [ ! -f "$BIN" ]; then
  echo "ERROR: $BIN missing. Get it from an already-deployed phone:"
  echo "  ssh -i ~/.ssh/bb10_root_rsa -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa root@<phone> 'cat /var/rootdata/io-asr-patched-final' > $BIN"
  echo "or patch a stock io-asr-bb10 per KNOWLEDGE.md section 3."
  exit 1
fi
python3 "$HERE/patch_server_ip.py" "$BIN" "$SRV" "$PORT" -o /tmp/io-asr-patched-final.$$ || \
python "$HERE/patch_server_ip.py" "$BIN" "$SRV" "$PORT" -o /tmp/io-asr-patched-final.$$

echo "[2/6] choosing audio config for $MODEL ..."
if [ "$MODEL" = "z30" ]; then
  cp "$HERE/files/asr_z30.cfg" /tmp/asr.cfg.$$
else
  # factory config always matches the model's audio routing
  $SSH 'cat /base/etc/asr.cfg' > /tmp/asr.cfg.$$
fi

echo "[3/6] uploading files ..."
sed "s/SERVER_IP/$SRV/; s/SERVER_PORT/$PORT/" "$HERE/files/asr_boot.sh" > /tmp/asr_boot.sh.$$
$SSH 'cat > /var/rootdata/io-asr-patched-final' < /tmp/io-asr-patched-final.$$
$SSH 'cat > /var/rootdata/asr.cfg'             < /tmp/asr.cfg.$$
$SSH 'cat > /var/rootdata/asr_boot.sh'         < /tmp/asr_boot.sh.$$
sed "s/SERVER_IP/$SRV/; s/SERVER_PORT/$PORT/" "$HERE/files/my_prompts.cfg" > /tmp/my_prompts.cfg.$$
$SSH 'mkdir -p /accounts/1000/sysdata/asr; cat > /accounts/1000/sysdata/asr/my_prompts.cfg' < /tmp/my_prompts.cfg.$$

echo "[4/6] stopping services and writing PPS settings ..."
$SSH "slay -f -Q io-asr-bb10 2>/dev/null; slay -f -Q io-asr-patched-final 2>/dev/null; sleep 2
echo 'MaluubaEnabled:n:1' >> /pps/services/asr/settings
echo 'RecognitionMode::Network' >> /pps/services/asr/settings
echo 'MaluubaHost::http://$SRV:$PORT' >> /pps/services/asr/settings"

echo "[5/6] starting patched io-asr ..."
$SSH 'sh < /var/rootdata/asr_boot.sh'
sleep 4

echo "[6/6] verifying ..."
N=$($SSH 'pidin | grep -c asr-patched' || true)
if [ "$N" -gt 0 ]; then
  echo "OK: io-asr-patched-final running ($N threads)"
  $SSH 'cat /pps/services/asr/control'
else
  echo "ERROR: service did not start; check /var/rootdata/ioasr_out.log on the phone"
  exit 1
fi
rm -f /tmp/io-asr-patched-final.$$ /tmp/asr.cfg.$$ /tmp/asr_boot.sh.$$ /tmp/my_prompts.cfg.$$
echo "Done. On the phone: open the assistant, speak, watch the server log."
