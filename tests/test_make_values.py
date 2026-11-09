"""deploy/bastion/03-make-values.py — the values merge must keep what the site set and add ours once."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "deploy", "bastion", "03-make-values.py")

# What `helm get values -o json` gives for the lab today (runbook ch.19 screenshots, pp.455-459):
# image tag "main", Authentik CA mounted from a ConfigMap, list-style extraEnvVars.
LAB = {
    "image": {"repository": "sup-harbor.rangers.lab/open-webui/open-webui", "tag": "main", "pullPolicy": "IfNotPresent"},
    "ingress": {"enabled": True, "class": "avi-lb", "host": "openwebui.rangers.lab", "tls": True,
                "existingSecret": "openwebui-tls-cert"},
    "sso": {"enabled": True, "oidc": {"enabled": True, "clientId": "x", "clientSecret": "y",
                                      "providerUrl": "https://kr-auth.rangers.lab:9443/application/o/open-webui/.well-known/openid-configuration",
                                      "providerName": "Authentik", "scopes": "openid email profile"}},
    "extraEnvVars": [
        {"name": "BYPASS_OLLAMA_CHECK", "value": "true"},
        {"name": "REQUESTS_CA_BUNDLE", "value": "/app/backend/certs/authentik-ca.crt"},
        {"name": "WEBUI_URL", "value": "https://openwebui.rangers.lab"},
    ],
    "volumes": [{"name": "authentik-ca", "configMap": {"name": "authentik-ca-cert"}}],
    "volumeMounts": {"initContainer": [], "container": [{"name": "authentik-ca", "mountPath": "/app/backend/certs"}]},
}


def run(values, *args):
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "before.json")
        out = os.path.join(d, "after.json")
        with open(src, "w", encoding="utf-8") as f:
            json.dump(values, f)
        p = subprocess.run([sys.executable, SCRIPT, src, "-o", out, *args], capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(p.stdout + p.stderr)
        with open(out, encoding="utf-8") as f:
            return json.load(f), p.stdout


class TestMakeValues(unittest.TestCase):
    def test_tag_is_pinned_and_site_values_survive(self):
        after, log = run(LAB)
        self.assertEqual(after["image"]["tag"], "v0.13.3")
        self.assertEqual(after["image"]["repository"], LAB["image"]["repository"])
        self.assertEqual(after["sso"], LAB["sso"])
        self.assertEqual(after["ingress"], LAB["ingress"])
        self.assertIn("image.tag: 'main' -> 'v0.13.3'", log)

    def test_ca_mount_is_kept_and_css_mount_added_once(self):
        after, _ = run(LAB)
        names = [v["name"] for v in after["volumes"]]
        self.assertEqual(names, ["authentik-ca", "brcm-custom-css"])
        mounts = after["volumeMounts"]["container"]
        self.assertEqual(mounts[0], LAB["volumeMounts"]["container"][0])
        self.assertEqual(mounts[1]["mountPath"], "/app/backend/open_webui/static/custom.css")
        self.assertEqual(mounts[1]["subPath"], "custom.css")
        self.assertTrue(mounts[1]["readOnly"])
        again, log = run(after)
        self.assertEqual(again, after)
        self.assertIn("바뀐 것 없음", log)

    def test_env_added_without_touching_existing(self):
        after, _ = run(LAB)
        env = {e["name"]: e.get("value") for e in after["extraEnvVars"]}
        self.assertEqual(env["REQUESTS_CA_BUNDLE"], "/app/backend/certs/authentik-ca.crt")
        self.assertEqual(env["OFFLINE_MODE"], "true")
        # A value the site already set is never overridden, even when we would default it differently.
        site = dict(LAB); site["extraEnvVars"] = LAB["extraEnvVars"] + [{"name": "OFFLINE_MODE", "value": "false"}]
        after2, _ = run(site)
        self.assertEqual([e for e in after2["extraEnvVars"] if e["name"] == "OFFLINE_MODE"],
                         [{"name": "OFFLINE_MODE", "value": "false"}])

    def test_map_style_env_keeps_its_style(self):
        site = dict(LAB); site["extraEnvVars"] = {"WEBUI_URL": "https://openwebui.rangers.lab"}
        after, _ = run(site)
        self.assertIsInstance(after["extraEnvVars"], dict)
        self.assertEqual(after["extraEnvVars"]["OFFLINE_MODE"], "true")
        self.assertEqual(after["extraEnvVars"]["WEBUI_URL"], "https://openwebui.rangers.lab")

    def test_secret_key_option_and_no_css(self):
        after, log = run(LAB, "--secret-key-secret", "open-webui-secret-key", "--css-configmap", "", "--tag", "v0.13.4")
        self.assertEqual(after["image"]["tag"], "v0.13.4")
        self.assertEqual([v["name"] for v in after["volumes"]], ["authentik-ca"])
        sk = [e for e in after["extraEnvVars"] if e["name"] == "WEBUI_SECRET_KEY"]
        self.assertEqual(sk[0]["valueFrom"]["secretKeyRef"], {"name": "open-webui-secret-key", "key": "WEBUI_SECRET_KEY"})

    def test_empty_values_still_produce_a_usable_file(self):
        after, _ = run({})
        self.assertEqual(after["image"], {"tag": "v0.13.3", "pullPolicy": "IfNotPresent"})
        self.assertEqual(after["volumes"][0]["configMap"]["name"], "brcm-custom-css")


if __name__ == "__main__":
    unittest.main()
