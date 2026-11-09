#!/usr/bin/env bash
# Shared settings for the bastion-side scripts. Source it; do not run it.
#   . ./00-env.sh
# Every value can be overridden from the environment before sourcing, so one copy of the
# scripts serves the lab (defaults below, from the air-gap runbook ch.19) and a customer site.

# --- target release ---------------------------------------------------------------------------
export OWUI_NS="${OWUI_NS:-open-webui}"                    # namespace
export OWUI_RELEASE="${OWUI_RELEASE:-open-webui}"          # helm release name (= StatefulSet, PVC name)
export OWUI_STS="${OWUI_STS:-$OWUI_RELEASE}"               # StatefulSet name
export OWUI_POD="${OWUI_POD:-${OWUI_STS}-0}"               # single replica
export OWUI_URL="${OWUI_URL:-https://openwebui.rangers.lab}"

# --- versions -----------------------------------------------------------------------------------
# Chart 18.0.0 carries appVersion 0.13.0; no chart release carries 0.13.3 yet (helm-charts repo,
# 2026-11-09), so the image tag is pinned explicitly. 15.2.0 -> 18.0.0 changes no values key.
export OWUI_CHART_VERSION="${OWUI_CHART_VERSION:-18.0.0}"
export OWUI_TAG="${OWUI_TAG:-v0.13.3}"
export OWUI_PREV_TAG="${OWUI_PREV_TAG:-main}"              # what 159 runs today (0.10.2 under tag "main")

# --- harbor --------------------------------------------------------------------------------------
export HARBOR="${HARBOR:-sup-harbor.rangers.lab}"
export HARBOR_USER="${HARBOR_USER:-admin}"
# Never put the password here; export HARBOR_PASS before sourcing or the scripts will prompt.
export HARBOR_CA="${HARBOR_CA:-/root/vpaif-offline-setup-vcf9/certificates/${HARBOR}.crt}"
export OWUI_IMAGE_REPO="${OWUI_IMAGE_REPO:-${HARBOR}/open-webui/open-webui}"
export PIPELINES_IMAGE_REPO="${PIPELINES_IMAGE_REPO:-${HARBOR}/open-webui/pipelines}"
export CHART_OCI="${CHART_OCI:-oci://${HARBOR}/charts/open-webui}"

# --- files ----------------------------------------------------------------------------------------
export BUNDLE_DIR="${BUNDLE_DIR:-/root/open-webui/bundle-${OWUI_TAG}}"   # where the carried-in bundle was unpacked
export BACKUP_ROOT="${BACKUP_ROOT:-/root/open-webui/backup}"
export WORK_DIR="${WORK_DIR:-/root/open-webui}"
export CSS_CONFIGMAP="${CSS_CONFIGMAP:-brcm-custom-css}"
export FUNCTION_ID="${FUNCTION_ID:-pais_multi_agent_pipe_authentik_oauth2_password_grant}"

# --- helpers --------------------------------------------------------------------------------------
here() { cd "$(dirname "${BASH_SOURCE[1]}")" && pwd; }
die()  { echo "오류: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 이 없습니다"; }
k()    { kubectl -n "$OWUI_NS" "$@"; }
harbor_pass() {
    if [ -z "${HARBOR_PASS:-}" ]; then
        read -r -s -p "Harbor ${HARBOR_USER} 비밀번호: " HARBOR_PASS; echo
        export HARBOR_PASS
    fi
}
