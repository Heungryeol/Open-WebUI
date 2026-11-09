#!/usr/bin/env bash
# Bastion step 6 (only on failure): back to the pre-upgrade release AND the pre-upgrade database.
#   OWUI_BACKUP=/root/open-webui/backup/<stamp> ./06-rollback.sh [helm revision]
#
# `helm rollback` alone is NOT enough: the 0.13 pod has already migrated webui.db, and the
# 0.10.2 code cannot open a database whose alembic_version it has never heard of. So the order is
#   1. scale the StatefulSet to 0 (nothing may write the PVC)
#   2. put the backed-up webui.db on the PVC with a throw-away helper pod
#   3. helm rollback to the previous revision (old chart, old image, old values)
#   4. check the version
# Files uploaded after the backup are lost; chats after the backup are lost. That is the price of
# SQLite and why 02-backup.sh runs right before 04-upgrade.sh, not the day before.
set -euo pipefail
. "$(dirname "$0")/00-env.sh"
need kubectl; need helm
[ -n "${OWUI_BACKUP:-}" ] && [ -f "$OWUI_BACKUP/webui.db" ] || die "OWUI_BACKUP 이 02-backup.sh 의 디렉터리(webui.db 포함)를 가리켜야 합니다"
( cd "$OWUI_BACKUP" && sha256sum --quiet -c SHA256SUMS ) || die "백업 체크섬이 맞지 않습니다"

PREV_REV="${1:-}"
if [ -z "$PREV_REV" ]; then
    # The last DEPLOYED revision before the current one — the one 02-backup.sh recorded.
    PREV_REV=$(helm -n "$OWUI_NS" history "$OWUI_RELEASE" -o json \
        | python3 -c 'import json,sys; h=[r for r in json.load(sys.stdin) if r["status"]=="superseded"]; print(h[-1]["revision"] if h else "")')
fi
[ -n "$PREV_REV" ] || die "되돌릴 helm 리비전을 찾지 못했습니다 — helm history 를 보고 인자로 주세요"
helm -n "$OWUI_NS" history "$OWUI_RELEASE" | tail -5
read -r -p "리비전 ${PREV_REV} 로 되돌리고 DB 를 ${OWUI_BACKUP}/webui.db 로 바꿉니다 (백업 뒤의 대화·파일은 사라짐). 계속? [y/N] " yn
[ "${yn:-}" = "y" ] || die "중단"

echo "[1/4] StatefulSet 0 으로 (PVC 에 아무도 쓰지 않게)"
k scale "sts/$OWUI_STS" --replicas=0
k wait --for=delete "pod/$OWUI_POD" --timeout=3m || true

echo "[2/4] 도우미 파드로 webui.db 복원"
# The helper uses the image that is already on the node (the pre-upgrade tag) so it needs no pull.
HELPER="owui-restore-$(date +%s)"
k run "$HELPER" --restart=Never --image="${OWUI_IMAGE_REPO}:${OWUI_PREV_TAG}" --command -- sleep 1800 \
    --overrides="{\"spec\":{\"containers\":[{\"name\":\"$HELPER\",\"image\":\"${OWUI_IMAGE_REPO}:${OWUI_PREV_TAG}\",\"command\":[\"sleep\",\"1800\"],\"volumeMounts\":[{\"name\":\"data\",\"mountPath\":\"/data\"}]}],\"volumes\":[{\"name\":\"data\",\"persistentVolumeClaim\":{\"claimName\":\"$OWUI_RELEASE\"}}]}}"
k wait --for=condition=Ready "pod/$HELPER" --timeout=3m
k exec "$HELPER" -- sh -c 'ls -la /data/webui.db*; mv /data/webui.db /data/webui.db.failed-upgrade-$(date +%s); rm -f /data/webui.db-wal /data/webui.db-shm'
k cp "$OWUI_BACKUP/webui.db" "$HELPER:/data/webui.db"
k exec "$HELPER" -- python3 -c 'import sqlite3; db=sqlite3.connect("/data/webui.db"); print("integrity:", db.execute("pragma integrity_check").fetchone()[0], "| alembic:", db.execute("select version_num from alembic_version").fetchone()[0])'
k delete pod "$HELPER" --wait=false

echo "[3/4] helm rollback → 리비전 $PREV_REV"
helm -n "$OWUI_NS" rollback "$OWUI_RELEASE" "$PREV_REV" --wait --timeout 10m
k scale "sts/$OWUI_STS" --replicas=1 >/dev/null 2>&1 || true   # rollback restores the spec; harmless if already 1
k rollout status "sts/$OWUI_STS" --timeout=5m

echo "[4/4] 버전"
k exec "$OWUI_POD" -c open-webui -- curl -s http://127.0.0.1:8080/api/version; echo
k get sts "$OWUI_STS" -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
echo "끝. 실패한 DB 는 PVC 의 webui.db.failed-upgrade-* 로 남겨 두었습니다(원인 분석용, 확인 뒤 지우세요)."
