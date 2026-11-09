#!/usr/bin/env bash
# Bastion step 4: the upgrade itself.
#   OWUI_BACKUP=/root/open-webui/backup/<stamp> ./04-upgrade.sh /root/open-webui/owui-values-v0.13.3.json
#
# Order matters: ConfigMap first (the new pod mounts it), then `helm upgrade --wait`. The first
# start of the new image runs the DB migrations before it answers /health, so --wait is also the
# migration wait. One replica -> no rolling-update problem (0.13.0 forbids mixed versions).
set -euo pipefail
. "$(dirname "$0")/00-env.sh"
need kubectl; need helm
VALUES="${1:-}"
[ -f "${VALUES}" ] || die "values 파일을 주세요 (03-make-values.py 결과): $0 <owui-values-${OWUI_TAG}.json>"
[ -n "${OWUI_BACKUP:-}" ] && [ -f "$OWUI_BACKUP/webui.db" ] || die "OWUI_BACKUP 이 02-backup.sh 의 디렉터리를 가리켜야 합니다 (webui.db 가 있어야 함)"
[ -f "$HARBOR_CA" ] || die "Harbor CA 가 없습니다: $HARBOR_CA"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "[0/5] 사전 확인"
k get pod "$OWUI_POD" -o jsonpath='{.status.phase}{"\n"}' | grep -q Running || die "$OWUI_POD 가 Running 이 아닙니다"
python3 -c "import json,sys; v=json.load(open(sys.argv[1])); t=v.get('image',{}).get('tag'); sys.exit(0 if t=='$OWUI_TAG' else print(f'values 의 image.tag 가 {t!r} 입니다 (기대: $OWUI_TAG)') or 1)" "$VALUES"
helm show chart "$CHART_OCI" --version "$OWUI_CHART_VERSION" --ca-file "$HARBOR_CA" >/dev/null || die "Harbor 에 차트 ${OWUI_CHART_VERSION} 이 없습니다 (01-push-harbor.sh)"
# Space: alembic rewrites tables; SQLite may need up to ~2x the db size while it does.
k exec "$OWUI_POD" -c open-webui -- sh -c 'df -h /app/backend/data | tail -1; ls -la /app/backend/data/webui.db'

echo "[1/5] custom.css ConfigMap ($CSS_CONFIGMAP) — 저장소의 static/custom.css 로"
k create configmap "$CSS_CONFIGMAP" --from-file=custom.css="$REPO_ROOT/static/custom.css" \
    --dry-run=client -o yaml | k apply -f -

echo "[2/5] 렌더링 미리보기 (적용 전 diff 성격) — 이미지·마운트·env 만 추림"
helm -n "$OWUI_NS" template "$OWUI_RELEASE" "$CHART_OCI" --version "$OWUI_CHART_VERSION" \
    -f "$VALUES" --ca-file "$HARBOR_CA" 2>/dev/null \
    | grep -E '^\s+(image|mountPath|subPath|name: (OFFLINE_MODE|WEBUI_URL|OAUTH_PROVIDER_NAME|REQUESTS_CA_BUNDLE)|claimName):' | sort -u

read -r -p "위 내용으로 ${OWUI_RELEASE} 를 차트 ${OWUI_CHART_VERSION} / 이미지 ${OWUI_TAG} 로 올립니다. 계속? [y/N] " yn
[ "${yn:-}" = "y" ] || die "중단"

echo "[3/5] helm upgrade (--wait, 15분) — 새 파드의 첫 기동에서 DB 마이그레이션이 돕니다"
START=$(date +%s)
helm upgrade "$OWUI_RELEASE" "$CHART_OCI" --version "$OWUI_CHART_VERSION" -n "$OWUI_NS" \
    -f "$VALUES" --ca-file "$HARBOR_CA" --wait --timeout 15m --history-max 10 \
    || { echo "helm upgrade 실패 — 06-rollback.sh 로 가십시오 (OWUI_BACKUP=$OWUI_BACKUP)"; exit 1; }
k rollout status "sts/$OWUI_STS" --timeout=5m
echo "  걸린 시간: $(( $(date +%s) - START ))초"

echo "[4/5] 기동 로그에서 마이그레이션·오류 줄"
k logs "$OWUI_POD" -c open-webui --since=20m 2>/dev/null \
    | grep -iE 'alembic|migrat|error|traceback|custom.css|static' | tail -25 || true

echo "[5/5] 버전"
k exec "$OWUI_POD" -c open-webui -- curl -s http://127.0.0.1:8080/api/version; echo
echo
echo "끝. 다음: 05-verify.sh (OPENWEBUI_KEY 를 export 해 두면 함수·Valves 까지 봅니다)"
