#!/usr/bin/env bash
# Bastion step 2: a restorable snapshot of what runs today, taken BEFORE the upgrade.
#
# The database is SQLite inside the PVC (/app/backend/data/webui.db). 0.11 -> 0.13 run alembic
# migrations on it at first start and alembic has no safe way back, so the ONLY rollback is this
# file. The copy is taken with SQLite's online backup API (consistent while the pod serves users),
# not `cp` of a file that is being written.
set -euo pipefail
. "$(dirname "$0")/00-env.sh"
need kubectl; need helm

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"
echo "백업 위치: $DEST"

echo "[1/5] 지금 상태 기록 (helm 릴리스·이미지·버전)"
helm -n "$OWUI_NS" status "$OWUI_RELEASE" -o json > "$DEST/helm-status.json"
helm -n "$OWUI_NS" history "$OWUI_RELEASE" > "$DEST/helm-history.txt"
# User-supplied values only — exactly what the last `helm install/upgrade -f` passed in.
# 03-make-values.py reads this file; keep it even if you also keep owui-value.yaml.
helm -n "$OWUI_NS" get values "$OWUI_RELEASE" -o json > "$DEST/values-before.json"
helm -n "$OWUI_NS" get values "$OWUI_RELEASE" -o yaml > "$DEST/values-before.yaml"
k get sts "$OWUI_STS" -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}' | tee "$DEST/image-before.txt"
k exec "$OWUI_POD" -c open-webui -- curl -s http://127.0.0.1:8080/api/version | tee "$DEST/version-before.txt"; echo
k get configmap,secret,pvc -o name > "$DEST/objects.txt"
k get configmap "$CSS_CONFIGMAP" -o yaml > "$DEST/configmap-${CSS_CONFIGMAP}.yaml" 2>/dev/null || echo "(custom.css ConfigMap 없음 — 아직 kubectl cp 로만 넣었던 상태)"

echo "[2/5] SQLite 온라인 백업 (파드 안에서 python sqlite3.backup)"
k exec -i "$OWUI_POD" -c open-webui -- python3 - <<'PY'
import sqlite3, os
src = sqlite3.connect("/app/backend/data/webui.db")
dst_path = "/app/backend/data/webui.db.pre-upgrade"
if os.path.exists(dst_path):
    os.remove(dst_path)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close(); src.close()
chk = sqlite3.connect(dst_path).execute("pragma integrity_check").fetchone()[0]
print("backup integrity:", chk, "| size:", os.path.getsize(dst_path))
PY
k cp -c open-webui "$OWUI_POD:/app/backend/data/webui.db.pre-upgrade" "$DEST/webui.db"
k exec "$OWUI_POD" -c open-webui -- rm -f /app/backend/data/webui.db.pre-upgrade
python3 - "$DEST/webui.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print("local integrity:", db.execute("pragma integrity_check").fetchone()[0])
print("alembic revision:", db.execute("select version_num from alembic_version").fetchone()[0])
print("users:", db.execute("select count(*) from user").fetchone()[0],
      "| chats:", db.execute("select count(*) from chat").fetchone()[0],
      "| functions:", db.execute("select count(*) from function").fetchone()[0])
PY

echo "[3/5] 나머지 데이터 (uploads, vector_db, cache 제외) — tar 스트림"
# Streaming avoids a second copy inside the (small, 2Gi default) PVC. cache/ is re-creatable.
k exec "$OWUI_POD" -c open-webui -- sh -c \
    'cd /app/backend && tar cf - --exclude=data/cache --exclude=data/webui.db --exclude="data/webui.db-*" data' \
    > "$DEST/data-without-db.tar"
tar tf "$DEST/data-without-db.tar" | wc -l | xargs echo "  파일 수:"

echo "[4/5] 파이프 함수와 Valves (DB 에도 있지만 따로 한 벌 더)"
# Valve values include the Authentik service-account password; the file is root-only.
if [ -n "${OPENWEBUI_KEY:-}" ]; then
    umask 077
    curl -sk -H "Authorization: Bearer $OPENWEBUI_KEY" "$OWUI_URL/api/v1/functions/id/$FUNCTION_ID" > "$DEST/function.json"
    curl -sk -H "Authorization: Bearer $OPENWEBUI_KEY" "$OWUI_URL/api/v1/functions/id/$FUNCTION_ID/valves" > "$DEST/function-valves.json"
    umask 022
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('  function:', d.get('id'), '| content chars:', len(d.get('content','')))" "$DEST/function.json"
else
    echo "  OPENWEBUI_KEY 가 없어 건너뜀 (DB 백업에 포함되어 있음)"
fi

echo "[5/5] 체크섬"
( cd "$DEST" && sha256sum webui.db data-without-db.tar values-before.json > SHA256SUMS )
du -sh "$DEST"
echo
echo "끝. 이 디렉터리를 bastion 밖(다른 디스크)에도 한 벌 두십시오. 다음: 03-make-values.py"
echo "export OWUI_BACKUP=$DEST"
