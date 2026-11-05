#!/bin/bash
# core.sh up | down | status | config | diff | install-config <nf> | build | subs | logs - the Open5GS core network of this host
#   The core runs as the host's native systemd services (Ubuntu package open5gs 2.8.0~jammy5, the hsw setup: AMF 127.0.0.5, PLMN 999/70,
#   TAC 1, UE subnet 10.45.0.0/16, WebUI :9999 for subscribers). Normally it is simply left running (a restart re-issues GUTIs and can make
#   a phone that kept an old GUTI fail with "Authentication failure" until it is power-cycled).
#   up / down        start the 18 services + UE NAT (core/routing.sh) / NAT off + stop         (sudo)
#   status           units, ogstun, NAT rules
#   config           list this package's editable copies of the configs (core/etc/, copied from /etc/open5gs on 2026-11-05)
#   diff [nf]        core/etc/<nf>.yaml vs the live /etc/open5gs/<nf>.yaml
#   install-config <nf>   copy core/etc/<nf>.yaml to /etc/open5gs (backup kept) and restart that unit   (sudo; the lab shares this core!)
#   build            build the Open5GS source copy (open5gs/, v2.8.0 = the installed version) with meson/ninja into open5gs/install
#                    (for patched NFs; run one with: open5gs/install/bin/open5gs-<nf>d -c core/etc/<nf>.yaml after stopping the unit)
#   subs             subscriber list from MongoDB (imsi, APN) if mongosh/mongo is available
#   logs             follow AMF/SMF/UPF journal (./logs.sh core is the filtered view)
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env
UNITS="open5gs-amfd open5gs-ausfd open5gs-bsfd open5gs-hssd open5gs-mmed open5gs-nrfd open5gs-nssfd open5gs-pcfd open5gs-pcrfd open5gs-scpd open5gs-seppd open5gs-sgwcd open5gs-sgwud open5gs-smfd open5gs-udmd open5gs-udrd open5gs-upfd open5gs-webui"
E=$D/core/etc; SRC=$D/open5gs
case "${1:-status}" in
up)     sudo systemctl start $UNITS || exit 1; for i in $(seq 1 15); do ip -br addr show ogstun 2>/dev/null | grep -q '10.45.0.1' && break; sleep 1; done
        sudo UE_SUBNET=$UE_SUBNET bash $D/core/routing.sh || echo "!! routing.sh failed (UE internet access needs the NAT rules)"; "$0" status ;;
down)   sudo bash $D/core/routing_restore.sh 2>/dev/null || true; sudo systemctl stop $UNITS; "$0" status ;;
status) act=$(systemctl is-active $UNITS 2>/dev/null | grep -c '^active')
        if sudo -n true 2>/dev/null; then nat=$(sudo -n iptables -t nat -S POSTROUTING 2>/dev/null | grep -c OPEN5GS); else nat="? (sudo: iptables -t nat -S POSTROUTING | grep -c OPEN5GS)"; fi
        echo "Open5GS: $act/18 units active | ogstun: $(ip -br addr show ogstun 2>/dev/null | awk '{print $2, $3}' | tr '\n' ' ') | UE NAT rules: $nat | WebUI http://<host>:9999 | version $(open5gs-amfd -v 2>&1 | head -1)" ;;
config) echo "editable copies: $E/ ($(ls $E/*.yaml | wc -l) yaml + hnet/ tls/)"; ls $E; echo "live: /etc/open5gs/ | source: $SRC ($(cd $SRC 2>/dev/null && git describe --tags 2>/dev/null || echo missing))"
        for f in $E/*.yaml; do n=$(basename $f); cmp -s $f /etc/open5gs/$n || echo "   differs from live: $n"; done ;;
diff)   for n in ${2:-amf smf upf}; do n=${n%.yaml}; echo "== $n.yaml (core/etc vs /etc/open5gs)"; diff -u /etc/open5gs/$n.yaml $E/$n.yaml && echo "   identical"; done ;;
install-config)
        n=${2:?nf name, e.g. amf}; n=${n%.yaml}; [ -f $E/$n.yaml ] || { echo "no $E/$n.yaml"; exit 1; }
        echo "== installing core/etc/$n.yaml -> /etc/open5gs/$n.yaml (backup /etc/open5gs/$n.yaml.bak_$(date +%m%d_%H%M)) and restarting open5gs-${n}d"
        sudo cp -p /etc/open5gs/$n.yaml /etc/open5gs/$n.yaml.bak_$(date +%m%d_%H%M) && sudo cp $E/$n.yaml /etc/open5gs/$n.yaml && sudo systemctl restart open5gs-${n}d && systemctl is-active open5gs-${n}d ;;
build)  [ -d $SRC ] || { echo "no source at $SRC (git clone --branch v2.8.0 https://github.com/open5gs/open5gs.git open5gs)"; exit 1; }
        cd $SRC; LOG=$D/runs/build_open5gs_$(date +%m%d_%H%M).log; echo "== Open5GS build: meson setup build --prefix=$SRC/install ; ninja -C build (log $LOG)"
        if [ ! -f build/build.ninja ]; then nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} meson setup build --prefix=$SRC/install > $LOG 2>&1 || { echo "!! meson setup failed - missing build dependencies? tail:"; tail -12 $LOG
             echo "   Ubuntu packages Open5GS needs (sudo apt install): python3-pip python3-setuptools python3-wheel ninja-build build-essential flex bison git cmake libsctp-dev libgnutls28-dev libgcrypt-dev libssl-dev libidn11-dev libmongoc-dev libbson-dev libyaml-dev libnghttp2-dev libmicrohttpd-dev libcurl4-gnutls-dev libtins-dev libtalloc-dev meson"; exit 1; }; fi
        nice -n 10 taskset -c ${BUILD_CPUS:-0-3,65-71} ninja -C build >> $LOG 2>&1 && ninja -C build install >> $LOG 2>&1 && echo "   built: $(ls $SRC/install/bin 2>/dev/null | tr '\n' ' ')" || { echo "!! build failed:"; tail -12 $LOG; exit 1; } ;;
subs)   if command -v mongosh > /dev/null; then mongosh --quiet open5gs --eval 'db.subscribers.find({}, {imsi:1, "slice.session.name":1, _id:0}).forEach(d => print(d.imsi, (d.slice||[]).map(s => (s.session||[]).map(x => x.name).join(",")).join(";")))'
        elif command -v mongo > /dev/null; then mongo --quiet open5gs --eval 'db.subscribers.find({}, {imsi:1, _id:0}).forEach(printjson)'; else echo "no mongosh/mongo client; use the WebUI :9999"; fi ;;
logs)   J="journalctl -f -n 30 -u open5gs-amfd -u open5gs-smfd -u open5gs-upfd --no-pager"; journalctl -n 1 -u open5gs-amfd --no-pager > /dev/null 2>&1 && $J || sudo $J ;;
*)      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
