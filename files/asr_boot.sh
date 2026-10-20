#!/bin/sh
# BB10 Assistant reborn - post-boot restore (run as root after each reboot)
# 1. Dragon URL overlay
ln -sPf /accounts/1000/sysdata/asr/my_prompts.cfg /base/usr/asr/recognition/en-US/dragon/prompts.cfg
# 2. Patched io-asr (host strings + curl fixes)
slay -f -Q io-asr-bb10 2>/dev/null
slay -f -Q io-asr-patched-final 2>/dev/null
sleep 1
on -u 109:109,50,200 /var/rootdata/io-asr-patched-final -c /var/rootdata/asr.cfg </dev/null >/var/rootdata/ioasr_out.log 2>&1
# 3. PPS host override (backup path)
echo 'MaluubaHost::http://SERVER_IP:SERVER_PORT' >> /pps/services/asr/settings
