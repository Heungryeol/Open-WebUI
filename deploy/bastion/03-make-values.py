#!/usr/bin/env python3
"""Bastion step 3: write the values file for the upgrade from the values that run today.

    python3 03-make-values.py $OWUI_BACKUP/values-before.json -o /root/open-webui/owui-values-v0.13.3.json
    python3 03-make-values.py ... --secret-key-secret open-webui-secret-key   # optional, see README

Input is `helm get values <release> -o json` (02-backup.sh writes it): the exact values the running
release was installed with, so whatever the site added by hand (the Authentik CA mount, extra env,
ingress class) is carried over untouched. Output is JSON, which helm reads as a values file.

Why not a second `-f overlay.yaml`: helm replaces lists instead of merging them, and the three
things we need to add (a volume, a volumeMount, env vars) all live in lists that the site already
uses for the Authentik CA. An overlay would silently drop that mount. This script appends instead,
is idempotent (running it on its own output changes nothing) and prints every change it makes.

Why JSON and not YAML: the bastion has no PyYAML and we do not want to install anything there;
the JSON stdlib is enough and helm accepts it. The human-readable copy stays values-before.yaml.
"""
import argparse
import json
import sys

CSS_MOUNT_PATH = "/app/backend/open_webui/static/custom.css"   # served as /static/custom.css (main.py mount)
CSS_VOLUME = "brcm-custom-css"

# Env the air-gapped site should run with. OFFLINE_MODE stops pip-installing the pipe's
# `requirements: httpx` at load time (httpx ships with Open WebUI anyway), stops HF model
# auto-update downloads and the GitHub version check — all of which only produce errors offline.
DEFAULT_ENV = {
    "OFFLINE_MODE": "true",
}


def env_as_list(items):
    """extraEnvVars/commonEnvVars accept list or map style; work on a list, keep the style on output."""
    if items is None:
        return [], "list"
    if isinstance(items, dict):
        return [{"name": k, "value": v} if not isinstance(v, dict) else {"name": k, **v} for k, v in items.items()], "map"
    return list(items), "list"


def env_back(lst, style):
    if style == "map":
        out = {}
        for e in lst:
            e = dict(e)
            name = e.pop("name")
            out[name] = e["value"] if set(e) == {"value"} else e
        return out
    return lst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("values_before", help="helm get values -o json 의 파일")
    ap.add_argument("-o", "--out", required=True, help="만들 values 파일(.json)")
    ap.add_argument("--repo", default=None, help="image.repository 를 바꿀 때만 (기본: 그대로)")
    ap.add_argument("--tag", default="v0.13.3", help="image.tag (기본 v0.13.3)")
    ap.add_argument("--css-configmap", default=CSS_VOLUME, help="custom.css 를 담은 ConfigMap 이름 ('' 이면 마운트 안 함)")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="extraEnvVars 에 더할 변수 (여러 번). 기본: " + ", ".join(f"{k}={v}" for k, v in DEFAULT_ENV.items()))
    ap.add_argument("--no-default-env", action="store_true", help="기본 env(OFFLINE_MODE) 를 더하지 않음")
    ap.add_argument("--secret-key-secret", default="", metavar="SECRET",
                    help="WEBUI_SECRET_KEY 를 이 k8s Secret(key: WEBUI_SECRET_KEY) 에서 읽게 함 (선택)")
    a = ap.parse_args()

    with open(a.values_before, encoding="utf-8") as f:
        v = json.load(f)
    if not isinstance(v, dict):
        print("values-before 가 객체가 아닙니다 (helm get values -o json 결과여야 함)", file=sys.stderr)
        return 2
    changes = []

    # --- image -------------------------------------------------------------------------------
    img = v.setdefault("image", {})
    if a.repo and img.get("repository") != a.repo:
        changes.append(f"image.repository: {img.get('repository')!r} -> {a.repo!r}")
        img["repository"] = a.repo
    if img.get("tag") != a.tag:
        changes.append(f"image.tag: {img.get('tag')!r} -> {a.tag!r}")
        img["tag"] = a.tag
    if img.get("pullPolicy") != "IfNotPresent":
        # A pinned tag never changes content, so IfNotPresent is both safe and the only policy that
        # works when Harbor is briefly unreachable at pod restart.
        changes.append(f"image.pullPolicy: {img.get('pullPolicy')!r} -> 'IfNotPresent'")
        img["pullPolicy"] = "IfNotPresent"

    # --- custom.css ConfigMap, mounted over the one file the frontend references ------------
    if a.css_configmap:
        vols = v.setdefault("volumes", []) or []
        if not any(isinstance(x, dict) and x.get("name") == CSS_VOLUME for x in vols):
            vols.append({"name": CSS_VOLUME, "configMap": {"name": a.css_configmap}})
            changes.append(f"volumes += {CSS_VOLUME} (configMap {a.css_configmap})")
        v["volumes"] = vols
        vm = v.setdefault("volumeMounts", {}) or {}
        cont = vm.setdefault("container", []) or []
        if not any(isinstance(x, dict) and x.get("mountPath") == CSS_MOUNT_PATH for x in cont):
            cont.append({"name": CSS_VOLUME, "mountPath": CSS_MOUNT_PATH, "subPath": "custom.css", "readOnly": True})
            changes.append(f"volumeMounts.container += {CSS_MOUNT_PATH} (subPath custom.css)")
        vm["container"] = cont
        vm.setdefault("initContainer", [])
        v["volumeMounts"] = vm

    # --- env ---------------------------------------------------------------------------------
    wanted = {} if a.no_default_env else dict(DEFAULT_ENV)
    for item in a.env:
        if "=" not in item:
            print(f"--env 는 NAME=VALUE 꼴이어야 합니다: {item!r}", file=sys.stderr)
            return 2
        k, _, val = item.partition("=")
        wanted[k.strip()] = val
    lst, style = env_as_list(v.get("extraEnvVars"))
    common, _ = env_as_list(v.get("commonEnvVars"))
    present = {e.get("name") for e in lst + common if isinstance(e, dict)}
    for k, val in wanted.items():
        if k in present:
            continue   # the site already decided this one; never override a value they set
        lst.append({"name": k, "value": val})
        changes.append(f"extraEnvVars += {k}={val}")
    if a.secret_key_secret:
        if "WEBUI_SECRET_KEY" in present:
            print("WEBUI_SECRET_KEY 가 이미 values 에 있어 --secret-key-secret 를 건너뜁니다")
        else:
            lst.append({"name": "WEBUI_SECRET_KEY",
                        "valueFrom": {"secretKeyRef": {"name": a.secret_key_secret, "key": "WEBUI_SECRET_KEY"}}})
            changes.append(f"extraEnvVars += WEBUI_SECRET_KEY (from Secret {a.secret_key_secret})")
    v["extraEnvVars"] = env_back(lst, style)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    if changes:
        print("바뀐 것:")
        for c in changes:
            print("  -", c)
    else:
        print("바뀐 것 없음 (이미 반영된 values)")
    print("썼음:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
