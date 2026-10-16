#!/bin/bash
# 域名证书（top001.xin，Let's Encrypt 90 天）续期。
#
# 休眠机制：备案通过前域名被阿里云 SNI 拦截，域名证书签不出也用不上，
# 此时服务跑的是 IP 证书（见 renew-ip.sh）。本脚本会自检当前是否处于
# 「域名模式」——不是就静默退出，不报错、不刷日志。
#
# crontab 里那条每天 03:17 的调用因此可以一直留着：
# 备案通过、切到域名模式后，它自动开始工作。
set -uo pipefail

ACME=/mnt/data/chengang/travel-memory/acme
LOG=$ACME/renew.log
DOMAIN=top001.xin
# 证书要同时覆盖裸域和 www —— 少一个，对应的入口就会报证书不匹配
WWW=www.$DOMAIN
UNIT=$HOME/.config/systemd/user/travel-memory.service

log() { echo "[$(date '+%F %T')] [domain] $*" >> "$LOG"; }
cd "$ACME" || exit 1

# 当前是否域名模式？看主服务实际加载的是哪张证书。
if ! grep -q "certificates/$DOMAIN.crt" "$UNIT" 2>/dev/null; then
    exit 0        # IP 模式，休眠
fi

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

out=$(env -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy \
          -u ALL_PROXY -u all_proxy \
      ./lego --accept-tos --email jeff@luckyluke.org \
        --path ./lego-data --domains "$DOMAIN" --domains "$WWW" \
        --http --http.webroot ./webroot \
        renew --days 30 2>&1)

if [ $? -ne 0 ]; then
    log "❌ 续期失败: $(echo "$out" | tail -3 | tr '\n' ' ')"
    exit 1
fi

# 2026-10-16 起：lenserver 降级为冷备份，web 服务已停。
# 证书的真正使用者是 top001 的travel-memory-standby.service。续期成功后
# 不再重启本机（已停，会误判成失败），而是把证书投递到 OSS；
# top001 那边有 travel-memory-pull-cert.timer 定期取走并重启自己。
#
# 为什么走 OSS：lenserver 没有 top001 的免密公钥（反向也没有），
# 而两台本来就都连同一个桶，那条通道已经在生产上跑了。
#
# 为什么放 uploads/.tls/ 而不是新建 tls/ 前缀：本机的 ossutil 用的是只读
# 子账号，**只有 uploads/ 前缀有写权限**，新建前缀一律403 AccessDenied
# （实测generated/ 也不行）。所以借用已有的授权前缀。
if echo "$out" | grep -q "Server responded with a certificate"; then
    NEW_END=$(openssl x509 -in "./lego-data/certificates/$DOMAIN.crt" -noout -enddate | cut -d= -f2)
    CERT_DIR="./lego-data/certificates"
    TLS_PREFIX=${TLS_PREFIX:-oss://travel-memory-media}/uploads/.tls
    OSSUTIL=${OSSUTIL:-$HOME/bin/ossutil}

    ok=1
    for ext in crt key; do
      $OSSUTIL cp -f "$CERT_DIR/$DOMAIN.$ext" "$TLS_PREFIX/$DOMAIN.$ext" >>"$LOG" 2>&1 || ok=0
    done
    if [ "$ok" = "1" ]; then
        log "✅ 已续期并投递到 OSS $TLS_PREFIX（新到期：$NEW_END），top001 会在 6 小时内取走"
    else
        log "⚠️ 已续期（到期 $NEW_END）但投递失败，需人工处理"
        exit 1
    fi
fi
