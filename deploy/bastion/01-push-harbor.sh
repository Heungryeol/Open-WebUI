#!/usr/bin/env bash
# Bastion step 1: verify the carried-in bundle and push its image(s) and chart into Harbor.
# Idempotent — pushing the same tag twice is harmless. Nothing in the cluster changes yet.
set -euo pipefail
. "$(dirname "$0")/00-env.sh"
need imgpkg; need helm; need sha256sum

[ -d "$BUNDLE_DIR" ] || die "번들 디렉터리가 없습니다: $BUNDLE_DIR (tar.gz 를 풀고 BUNDLE_DIR 을 맞추세요)"
[ -f "$HARBOR_CA" ]  || die "Harbor CA 가 없습니다: $HARBOR_CA"
harbor_pass

echo "[1/4] 체크섬 검증"
( cd "$BUNDLE_DIR" && sha256sum --quiet -c SHA256SUMS ) || die "체크섬이 맞지 않습니다 — 반입 파일이 깨졌습니다"
cat "$BUNDLE_DIR/BUNDLE-INFO.txt"

echo "[2/4] open-webui 이미지 → ${OWUI_IMAGE_REPO}:${OWUI_TAG}"
# --to-repo keeps the tag that is inside the tar (imgpkg records it when the tar is made).
imgpkg copy --tar "$BUNDLE_DIR/images/open-webui-${OWUI_TAG}.tar" \
    --to-repo "$OWUI_IMAGE_REPO" \
    --registry-ca-cert-path "$HARBOR_CA" \
    --registry-username "$HARBOR_USER" --registry-password "$HARBOR_PASS"

shopt -s nullglob
for t in "$BUNDLE_DIR"/images/pipelines-*.tar; do
    echo "      pipelines 이미지 → ${PIPELINES_IMAGE_REPO} ($(basename "$t"))"
    imgpkg copy --tar "$t" --to-repo "$PIPELINES_IMAGE_REPO" \
        --registry-ca-cert-path "$HARBOR_CA" \
        --registry-username "$HARBOR_USER" --registry-password "$HARBOR_PASS"
done

echo "[3/4] 헬름 차트 → ${CHART_OCI} (${OWUI_CHART_VERSION})"
echo "$HARBOR_PASS" | helm registry login "$HARBOR" --username "$HARBOR_USER" --password-stdin --ca-file "$HARBOR_CA"
helm push "$BUNDLE_DIR/charts/open-webui-${OWUI_CHART_VERSION}.tgz" "${CHART_OCI%/open-webui}" --ca-file "$HARBOR_CA"

echo "[4/4] 확인 — Harbor 에서 태그가 보이는지"
helm show chart "$CHART_OCI" --version "$OWUI_CHART_VERSION" --ca-file "$HARBOR_CA" | grep -E '^(version|appVersion):'
imgpkg tag list -i "$OWUI_IMAGE_REPO" --registry-ca-cert-path "$HARBOR_CA" \
    --registry-username "$HARBOR_USER" --registry-password "$HARBOR_PASS" | grep -E "^(${OWUI_TAG}|${OWUI_PREV_TAG})$" \
    || die "Harbor 에 ${OWUI_TAG} 태그가 보이지 않습니다"
echo "끝. 다음: 02-backup.sh"
