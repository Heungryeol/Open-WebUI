#!/usr/bin/env bash
# Run on an INTERNET-connected machine. Collects everything the air-gapped bastion needs to move
# Open WebUI to $OWUI_TAG and writes one tarball plus a SHA256SUMS file.
#
#   ./make_bundle.sh                       # open-webui v0.13.3 + chart 18.0.0 + this repo's files
#   WITH_PIPELINES=1 ./make_bundle.sh      # also a fresh pipelines:main tar (independent of Open WebUI)
#
# Needs: imgpkg, helm (>=3.8), sha256sum, git, tar. Output: ./out/owui-<tag>-bundle.tar.gz
#
# Why imgpkg and not `docker save`: that is how every other image in the runbook was carried in
# (ch.19 p.430), so the bastion-side push is the same command the operators already know.
set -euo pipefail

OWUI_TAG="${OWUI_TAG:-v0.13.3}"
OWUI_CHART_VERSION="${OWUI_CHART_VERSION:-18.0.0}"
PIPELINES_TAG="${PIPELINES_TAG:-main}"
HELM_REPO="${HELM_REPO:-https://helm.openwebui.com}"
OUT="${OUT:-$(pwd)/out}"

for c in imgpkg helm sha256sum git tar; do
    command -v "$c" >/dev/null 2>&1 || { echo "오류: $c 이 없습니다" >&2; exit 1; }
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE="$OUT/owui-${OWUI_TAG}-bundle"
rm -rf "$STAGE"; mkdir -p "$STAGE/images" "$STAGE/charts" "$STAGE/repo"

echo "[1/4] 이미지 tar — ghcr.io/open-webui/open-webui:${OWUI_TAG}"
imgpkg copy -i "ghcr.io/open-webui/open-webui:${OWUI_TAG}" --to-tar "$STAGE/images/open-webui-${OWUI_TAG}.tar"
if [ "${WITH_PIPELINES:-0}" = "1" ]; then
    echo "      pipelines:${PIPELINES_TAG} (선택)"
    imgpkg copy -i "ghcr.io/open-webui/pipelines:${PIPELINES_TAG}" \
        --to-tar "$STAGE/images/pipelines-${PIPELINES_TAG}-$(date +%Y%m%d).tar"
fi

echo "[2/4] 헬름 차트 open-webui-${OWUI_CHART_VERSION}.tgz"
helm pull open-webui --repo "$HELM_REPO" --version "$OWUI_CHART_VERSION" -d "$STAGE/charts"
# The chart's own appVersion is what the bastion will compare against; record it next to the tgz.
tar -xzf "$STAGE/charts/open-webui-${OWUI_CHART_VERSION}.tgz" -O open-webui/Chart.yaml \
    | grep -E '^(version|appVersion):' > "$STAGE/charts/CHART-INFO.txt"

echo "[3/4] 이 저장소의 파일 (function, custom.css, scripts, deploy)"
# A clean export of the committed tree, not the working copy, so the bundle is reproducible.
git -C "$REPO_ROOT" archive --format=tar HEAD README.md functions static scripts deploy tests \
    | tar -x -C "$STAGE/repo"
git -C "$REPO_ROOT" rev-parse HEAD > "$STAGE/repo/GIT-COMMIT.txt"

cat > "$STAGE/BUNDLE-INFO.txt" <<EOF
open-webui image tag : ${OWUI_TAG}
helm chart           : open-webui-${OWUI_CHART_VERSION}
pipelines            : $([ "${WITH_PIPELINES:-0}" = "1" ] && echo "pipelines:${PIPELINES_TAG}" || echo "(포함 안 함 — Harbor 의 기존 것을 그대로 씀)")
repo commit          : $(cat "$STAGE/repo/GIT-COMMIT.txt")
made on              : $(date -u +%Y-%m-%dT%H:%M:%SZ) by $(whoami)@$(hostname)
EOF

echo "[4/4] 체크섬·묶기"
( cd "$STAGE" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
tar -C "$OUT" -czf "$OUT/owui-${OWUI_TAG}-bundle.tar.gz" "owui-${OWUI_TAG}-bundle"
sha256sum "$OUT/owui-${OWUI_TAG}-bundle.tar.gz" | tee "$OUT/owui-${OWUI_TAG}-bundle.tar.gz.sha256"
echo
cat "$STAGE/BUNDLE-INFO.txt"
du -sh "$OUT/owui-${OWUI_TAG}-bundle.tar.gz"
