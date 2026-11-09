#!/usr/bin/env bash
# Bastion step 5: did the upgrade land, and did OUR pieces survive it?
#   OPENWEBUI_KEY=sk-... ./05-verify.sh        (an admin API key; without it steps 5-7 are skipped)
# Exit code is the number of failed checks.
set -uo pipefail
. "$(dirname "$0")/00-env.sh"
need kubectl
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FAIL=0
ok()   { echo "  OK   $*"; }
bad()  { echo "  FAIL $*"; FAIL=$((FAIL+1)); }
want="${OWUI_TAG#v}"

echo "[1] 파드·이미지"
img=$(k get sts "$OWUI_STS" -o jsonpath='{.spec.template.spec.containers[0].image}')
[ "$img" = "${OWUI_IMAGE_REPO}:${OWUI_TAG}" ] && ok "image $img" || bad "image $img (기대 ${OWUI_IMAGE_REPO}:${OWUI_TAG})"
ready=$(k get pod "$OWUI_POD" -o jsonpath='{.status.containerStatuses[?(@.name=="open-webui")].ready}')
[ "$ready" = "true" ] && ok "pod $OWUI_POD ready" || bad "pod $OWUI_POD ready=$ready"
restarts=$(k get pod "$OWUI_POD" -o jsonpath='{.status.containerStatuses[?(@.name=="open-webui")].restartCount}')
[ "${restarts:-0}" -le 1 ] && ok "restarts=$restarts" || bad "restarts=$restarts (기동 중 죽었다 살아남 — 로그 확인)"

echo "[2] 버전·health (파드 안에서)"
ver=$(k exec "$OWUI_POD" -c open-webui -- curl -s http://127.0.0.1:8080/api/version 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
[ "$ver" = "$want" ] && ok "/api/version = $ver" || bad "/api/version = '$ver' (기대 $want)"
k exec "$OWUI_POD" -c open-webui -- curl -sf http://127.0.0.1:8080/health >/dev/null && ok "/health" || bad "/health"
k exec "$OWUI_POD" -c open-webui -- curl -sf http://127.0.0.1:8080/health/db >/dev/null && ok "/health/db" || bad "/health/db"

echo "[3] DB 마이그레이션이 끝났는지 (alembic head = 코드의 마지막 리비전)"
k exec -i "$OWUI_POD" -c open-webui -- python3 - <<'PY' && ok "alembic head" || bad "alembic head 가 코드와 다름"
import sqlite3, re, pathlib, sys
db = sqlite3.connect("/app/backend/data/webui.db").execute("select version_num from alembic_version").fetchone()[0]
vers = pathlib.Path("/app/backend/open_webui/migrations/versions")
revs, downs = {}, set()
for p in vers.glob("*.py"):
    t = p.read_text(errors="ignore")
    # Both spellings occur: `revision = 'x'` and `revision: str = "x"`; the base has down_revision = None.
    r = re.search(r"^revision(?:\s*:[^=\n]+)?\s*=\s*['\"]([0-9a-f]+)['\"]", t, re.M)
    d = re.search(r"^down_revision(?:\s*:[^=\n]+)?\s*=\s*['\"]([0-9a-f]+)['\"]", t, re.M)
    if r: revs[r.group(1)] = p.name
    if d: downs.add(d.group(1))
heads = [r for r in revs if r not in downs]
print("   db:", db, "| code heads:", heads)
sys.exit(0 if db in heads else 1)
PY

echo "[4] custom.css 가 서빙되는지 (ConfigMap subPath)"
size_repo=$(wc -c < "$REPO_ROOT/static/custom.css")
out=$(k exec "$OWUI_POD" -c open-webui -- curl -s -o /dev/null -w '%{http_code} %{size_download}' http://127.0.0.1:8080/static/custom.css 2>/dev/null)
[ "$out" = "200 $size_repo" ] && ok "/static/custom.css $out" || bad "/static/custom.css '$out' (기대 200 $size_repo)"
k exec "$OWUI_POD" -c open-webui -- grep -c 'static/custom.css' /app/build/index.html 2>/dev/null | grep -q '^[1-9]' \
    && ok "index.html 이 /static/custom.css 를 참조" || bad "index.html 에 custom.css 참조가 없음"

echo "[5] OIDC — 파드가 Authentik well-known 을 우리 CA 로 읽는지"
wk=$(k exec "$OWUI_POD" -c open-webui -- sh -c 'echo $OPENID_PROVIDER_URL' 2>/dev/null)
if [ -n "$wk" ]; then
    k exec "$OWUI_POD" -c open-webui -- sh -c 'curl -sf --cacert "${REQUESTS_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}" "$OPENID_PROVIDER_URL" >/dev/null' \
        && ok "well-known 200 ($wk)" || bad "well-known 실패 ($wk) — CA ConfigMap 마운트·REQUESTS_CA_BUNDLE 확인"
else
    bad "OPENID_PROVIDER_URL 환경변수가 비어 있음 (sso.oidc 값이 빠졌는지)"
fi
k exec "$OWUI_POD" -c open-webui -- curl -s http://127.0.0.1:8080/api/config 2>/dev/null \
    | python3 -c 'import json,sys; c=json.load(sys.stdin); p=c.get("oauth",{}).get("providers",{}); print("   providers:", p); sys.exit(0 if p else 1)' \
    && ok "/api/config 에 OAuth provider 있음" || bad "/api/config 에 OAuth provider 없음"

if [ -z "${OPENWEBUI_KEY:-}" ]; then
    echo "[6-7] OPENWEBUI_KEY 없음 — 함수·Valves 검사 건너뜀"
else
    H="Authorization: Bearer $OPENWEBUI_KEY"
    echo "[6] 파이프 함수 — 있는지, 켜져 있는지, 저장소 판과 같은지"
    fj=$(mktemp); trap 'rm -f "$fj"' EXIT
    curl -sk -H "$H" "$OWUI_URL/api/v1/functions/id/$FUNCTION_ID" -o "$fj"
    python3 - "$fj" "$REPO_ROOT/functions/pais_multi_agent_pipe.py" <<'PY' && ok "function $FUNCTION_ID" || bad "function $FUNCTION_ID (위 줄)"
import json, sys
d = json.load(open(sys.argv[1])) if open(sys.argv[1]).read(1) else None
if not d: print("   함수가 없음"); sys.exit(1)
same = d.get("content") == open(sys.argv[2], encoding="utf-8").read()
print(f"   active={d.get('is_active')} global={d.get('is_global')} type={d.get('type')} content==repo: {same}")
if not same: print("   -> 저장소 판과 다름: python3 scripts/update_function.py 로 맞출 수 있음 (Valves 재확인)")
sys.exit(0 if d.get("is_active") else 1)
PY
    echo "[7] Valves — Client Id / Password 가 비어 있지 않은지 (값은 찍지 않음)"
    curl -sk -H "$H" "$OWUI_URL/api/v1/functions/id/$FUNCTION_ID/valves" \
        | python3 -c 'import json,sys; v=json.load(sys.stdin) or {}; miss=[k for k in ("OIDC_CLIENT_ID","OIDC_PASSWORD") if not v.get(k)]; print("   비어 있음:", miss or "없음"); sys.exit(1 if miss else 0)' \
        && ok "valves" || bad "valves 가 비어 있음 — README 의 'Valves 재확인' (코드를 바꾸면 비워질 수 있음)"
    echo "[8] 모델 목록에 PAIS 에이전트가 보이는지"
    curl -sk -H "$H" "$OWUI_URL/api/models" \
        | python3 -c 'import json,sys; m=[x["id"] for x in json.load(sys.stdin).get("data",[]) if x["id"].startswith(sys.argv[1]+".")]; print("   PAIS 모델:", len(m), m[:3]); sys.exit(0 if m else 1)' "$FUNCTION_ID" \
        && ok "PAIS 모델 노출" || bad "PAIS 모델이 없음 — 토큰 발급(Valves)·PAIS 연결 확인"
fi

echo
echo "[9] 손으로 볼 것 (브라우저, 강력 새로고침 뒤)"
echo "   - Authentik 으로 로그인 → 채팅 하나 → 답 끝에 후속 질문 칩이 나오고, 본문에는 '이어서 물어볼 만한 것' 목록이 없음"
echo "   - 로그에 'pais pipe: Open WebUI task follow_up_generation answered locally' 가 찍힘:"
echo "       kubectl -n $OWUI_NS logs $OWUI_POD -c open-webui --since=10m | grep 'pais pipe'"
echo "   - 간트 차트 폭·두 열 표 첫 열 30% (custom.css) 가 그대로"
echo "   - 관리자 패널 → 설정 → 인터페이스: Follow Up Generation 켬, 작업 모델 비움(또는 파이프 모델)"
echo
[ "$FAIL" -eq 0 ] && echo "모두 통과" || echo "실패 $FAIL 건"
exit "$FAIL"
