# Open WebUI 0.10.2 → 0.13.3 업그레이드 (air-gap, VKS 게스트 클러스터)

랩(159, `openwebui.rangers.lab`)과 고객 폐쇄망에 같은 절차로 쓰는 패키지다. 구축 자체는
`PAIS_air-gap_runbook` 19·20장 그대로이고, 여기는 **그 위에서 버전을 올리는 것**만 다룬다.
2026-11-09 에 0.13.3 wheel 과 헬름 차트 저장소를 열어 확인한 내용이 근거다.

## 무엇이 바뀌고 무엇이 그대로인가

| 항목 | 지금(159) | 올린 뒤 | 근거 |
|---|---|---|---|
| 이미지 | `sup-harbor…/open-webui/open-webui:main` (= 0.10.2, `IfNotPresent`) | `…:v0.13.3` 태그를 **박아서** | `main` 태그를 다시 밀어도 노드가 캐시한 옛 이미지를 씀 |
| 헬름 차트 | `open-webui-15.2.0` (appVersion 0.10.2) | **`18.0.0`** (appVersion 0.13.0) + `image.tag=v0.13.3` | 0.13.3 을 담은 차트 릴리스는 아직 없다. 15.2.0↔18.0.0 values 키 차이는 추가 2개(`pipelines.resources`, `route.timeouts`)뿐 — **`owui-value.yaml` 을 고칠 것이 없다** |
| values | `owui-value.yaml` | 지금 릴리스의 values(`helm get values`) + 우리 것 3가지 | 아래 `03-make-values.py` |
| Authentik OIDC | `sso.oidc.*`, CA ConfigMap, `REQUESTS_CA_BUNDLE`… | 그대로 | 환경변수 이름 전부 유지(0.13.3 `config.py`) |
| PAIS 파이프 함수 | DB 에 저장 | 그대로 — DB 마이그레이션이 옮김 | 파이프 호출 규약(`__task__`·`__task_body__`·`__metadata__`, `follow_up_generation`, Advanced Params 전달) 0.13.3 과 같음 |
| `static/custom.css` | 파드에 `kubectl cp`(재시작하면 사라짐) 또는 ConfigMap | **ConfigMap `brcm-custom-css` subPath 마운트**로 고정 | `index.html` 이 여전히 `/static/custom.css` 를 참조 |
| DB | SQLite, PVC `open-webui` 의 `webui.db` | 첫 기동에서 alembic 마이그레이션(0.11~0.13 몫) 자동 실행 | **되돌릴 수 없다** → 롤백 = 백업 복원 |
| pipelines | Harbor 의 `pipelines:main` | 그대로(선택적으로 갱신) | Open WebUI 버전과 무관 |

### 새로 넣는 것 3가지 (`03-make-values.py` 가 더한다)

1. `image.tag: v0.13.3`, `pullPolicy: IfNotPresent` — 태그를 박으면 내용이 바뀌지 않으니 안전하고, 재시작 때 Harbor 가 잠깐 안 보여도 뜬다.
2. `volumes` / `volumeMounts.container` 에 `brcm-custom-css` ConfigMap 을 `/app/backend/open_webui/static/custom.css` 로 subPath 마운트.
   Open WebUI 는 기동 때 그 디렉터리를 비우고 프런트 빌드의 static 을 다시 복사하는데(`config.py` 의 STATIC_DIR 블록), 마운트된 파일은 지우지도 덮지도 못해 로그에 `An error occurred: …custom.css` 한 줄(읽기 전용 파일)이 남는다. **정상이다** — 05 의 `200 3362` 로 확인한다.
3. `extraEnvVars` 에 `OFFLINE_MODE=true` — 파이프 머리말의 `requirements: httpx` 를 기동 때 pip 로 깔려 들지 않고(httpx 는 이미 이미지에 있음), HF 모델 자동 갱신·GitHub 버전 확인도 끈다. 폐쇄망에서 오류 로그만 내던 것들이다.

선택: `--secret-key-secret <Secret>` — 차트는 `WEBUI_SECRET_KEY` 를 주지 않는다. 그러면 Open WebUI 가 작업 디렉터리에 `.webui_secret_key` 를 만들어 쓰는데(`open_webui/__init__.py`), 그 자리는 `/app/backend`(PVC 밖)이라 **파드가 새로 뜰 때마다 키가 바뀌어 모두 로그아웃**된다. 159 에서 그런지 먼저 확인한다:
```bash
kubectl -n open-webui exec open-webui-0 -c open-webui -- sh -c 'env | grep -c ^WEBUI_SECRET_KEY=; ls -la /app/backend/.webui_secret_key /app/backend/data/.webui_secret_key 2>&1'
```
환경변수가 0이고 파일이 `/app/backend/` 에만 있으면 그 경우다. Secret 으로 고정하면 없어진다. 넣는 순간 한 번은 다 로그아웃되므로 업그레이드와 같은 날 넣는 것이 낫다.
```bash
kubectl -n open-webui create secret generic open-webui-secret-key --from-literal=WEBUI_SECRET_KEY="$(head -c 48 /dev/urandom | base64 -w0)"
```

## 주의

- **0.13.3 은 2026-11-08 공개, 0.13.1·2·3 이 하루에 나온 핫픽스다.** 데모·시연 전날에는 올리지 않는다. 올리고 적어도 하루는 지켜본 뒤 고객에 가져간다.
- 0.13.0 릴리스 노트: 스키마 변경이 있어 **복제본을 여럿 두면 전부 한 번에** 올려야 한다(롤링 불가). 우리는 1 복제본이라 해당 없지만, `replicaCount` 를 올린 사이트는 먼저 0 → 1 → N 으로 간다.
- 이미지는 Python 3.12 / Debian 13 이다. 파이프는 httpx·pydantic·loguru(모두 이미지 동봉)만 쓰므로 영향 없다.
- 0.13 의 새 기본값 중 우리 화면에 보이는 것: 공유 기본이 "복제만", 관리자 설정에 Comments·Inbox·MFA 가 생김(모두 끈 채로 둬도 됨), `ENABLE_DIRECT_INTEGRATIONS` 기본 꺼짐(개인 도구 서버 메뉴가 숨는다 — 우리는 PAIS 가 도구를 부르므로 무관).
- 런북 PDF 483쪽 파이프 코드에는 Authentik client id 와 서비스 계정 비밀번호가 평문으로 들어 있다. 고객 전달본에서는 지우고, 저장소 판(Valves 로 뺀 것)을 쓴다.

## 걸리는 시간

이미지 반입·Harbor 푸시 20~30분(5GB), 백업 5분, `helm upgrade --wait` 3~8분(마이그레이션 포함), 검증 10분. **서비스 중단은 helm upgrade 동안(파드 교체 + 마이그레이션) 5분 안팎.**

## A. 인터넷 PC 에서 — 반입 패키지 만들기

필요: `imgpkg`, `helm`, `git`, `sha256sum`. 이 저장소를 clone 한 디렉터리에서:

```bash
cd deploy/bundle
./make_bundle.sh                       # open-webui v0.13.3 이미지 tar + 차트 18.0.0 + 이 저장소 파일
WITH_PIPELINES=1 ./make_bundle.sh      # pipelines:main 도 새로 담을 때
ls -la out/                            # owui-v0.13.3-bundle.tar.gz + .sha256
```

`out/owui-v0.13.3-bundle.tar.gz` 와 `.sha256` 둘을 반입한다. 안에는 `BUNDLE-INFO.txt`(태그·차트·커밋), `SHA256SUMS`, `images/`, `charts/`, `repo/`(함수·css·스크립트) 가 있다.

## B. Bastion 에서 — 올리기

```bash
cd /root/open-webui
sha256sum -c owui-v0.13.3-bundle.tar.gz.sha256 && tar xzf owui-v0.13.3-bundle.tar.gz
cd owui-v0.13.3-bundle/repo/deploy/bastion
export BUNDLE_DIR=/root/open-webui/owui-v0.13.3-bundle
export HARBOR_PASS='…'                 # 묻게 두어도 된다
export OPENWEBUI_KEY='sk-…'            # 관리자 API 키 — 05 에서 함수·Valves 까지 보려면
. ./00-env.sh                          # 기본값은 랩. 고객 사이트는 HARBOR·OWUI_URL·HARBOR_CA 를 먼저 export
```

`00-env.sh` 의 기본값(네임스페이스 `open-webui`, 릴리스 `open-webui`, Harbor `sup-harbor.rangers.lab`, 차트 `18.0.0`, 태그 `v0.13.3`)은 환경변수로 덮어쓴다.

| 순서 | 스크립트 | 하는 일 | 되돌리기 |
|---|---|---|---|
| 1 | `./01-push-harbor.sh` | 체크섬 검증 → 이미지·차트를 Harbor 로 → 태그가 보이는지 | 없음(클러스터 변화 없음) |
| 2 | `./02-backup.sh` | `helm get values`, 이미지·버전 기록, **SQLite 온라인 백업**(`sqlite3.backup`, integrity_check), uploads tar, 함수·Valves JSON | — 마지막 줄의 `export OWUI_BACKUP=…` 을 실행해 둔다 |
| 3 | `python3 03-make-values.py $OWUI_BACKUP/values-before.json -o /root/open-webui/owui-values-v0.13.3.json` | 지금 values + 우리 것 3가지. 바뀐 줄을 찍는다. 같은 파일에 다시 돌리면 "바뀐 것 없음" | 파일 하나 |
| 4 | `./04-upgrade.sh /root/open-webui/owui-values-v0.13.3.json` | ConfigMap 적용 → 렌더링 미리보기 → **y** → `helm upgrade --wait 15m` → 기동 로그 → 버전 | 아래 6 |
| 5 | `./05-verify.sh` | 이미지·ready·재시작 수, `/api/version`·`/health`·`/health/db`, alembic head, custom.css 200, OIDC well-known, 함수·Valves·모델 목록. 실패 수가 종료 코드 | — |
| 6 | `./06-rollback.sh` | **실패 때만.** sts 0 → 도우미 파드로 `webui.db` 복원 → `helm rollback` → 버전 | — |

백업과 업그레이드는 **같은 자리에서 연달아** 한다. 롤백은 백업 시점으로 돌아가므로 그 사이의 대화·파일은 사라진다.

### 3번에서 왜 `-f overlay.yaml` 을 쓰지 않나

helm 은 values 를 합칠 때 리스트를 **통째로 바꾼다**. 우리가 더할 것(볼륨·마운트·env)이 모두 리스트이고, 그 리스트에는 사이트가 넣은 Authentik CA 마운트가 들어 있다. overlay 로 얹으면 그 마운트가 소리 없이 빠져 SSO 가 죽는다. 그래서 `helm get values` 결과(=지금 `-f` 로 준 값 그대로)에 **덧붙이는** 스크립트로 했고, 결과는 JSON 이다(helm 은 JSON 을 values 로 읽고, bastion 에 PyYAML 을 깔지 않아도 된다). `tests/test_make_values.py` 가 CA 마운트 보존·멱등성·list/map 두 스타일을 지킨다.

## C. 올린 뒤 손으로 볼 것

- 브라우저 **강력 새로고침** 뒤 Authentik 로그인 → 채팅 → 답 끝에 후속 질문 칩, 본문에는 "이어서 물어볼 만한 것" 목록이 없음.
  `kubectl -n open-webui logs open-webui-0 -c open-webui --since=10m | grep 'pais pipe'` 에 `follow_up_generation answered locally`.
- 관리자 패널 → 설정 → 인터페이스: **Follow Up Generation 켬**, 작업 모델(외부) 비움 또는 파이프 모델. (DB 설정이라 유지되지만 0.13 은 메뉴 위치가 달라 한 번 확인.)
- 함수 Valves 의 `Oidc Client Id`·`Oidc Password` 가 비어 있지 않음(05 가 본다). 비어 있으면 README 상단 "Valves 재확인".
- 간트 차트 폭·두 열 표 첫 열 30%(custom.css). 05 의 `200 3362` 가 맞으면 서빙은 되는 것이고, 안 보이면 브라우저 캐시다.
- `brcm-setup ask --webui …` 한 묶음(BRCM-MCP `deploy/photon/questions/`)으로 에이전트 경로가 그대로인지.

## D. 저장소 파일을 바꿨을 때 (업그레이드와 무관하게)

- 파이프 코드: `OPENWEBUI_URL=… OPENWEBUI_KEY=… python3 scripts/update_function.py --insecure` → Valves 재확인.
- `static/custom.css`: `kubectl -n open-webui create configmap brcm-custom-css --from-file=custom.css=static/custom.css --dry-run=client -o yaml | kubectl apply -f -` → `kubectl -n open-webui rollout restart sts/open-webui` (subPath 는 ConfigMap 이 바뀌어도 자동 반영되지 않는다).

## 다음 버전으로 또 올릴 때

`OWUI_TAG=v0.13.x OWUI_CHART_VERSION=18.y.z ./make_bundle.sh` 와 같은 변수로 01~05 를 돌린다. 차트 major 가 바뀌면(19.x = 0.14) 먼저 `helm-charts` 저장소에서 `charts/open-webui/values.yaml` 의 diff 와 `CHANGELOG.md` 를 보고, 0.13.3 wheel 로 했듯 `functions.py`(파이프 kwargs)·`constants.py`(TASKS)·`frontend/index.html`(custom.css 참조)·`config.py`(OAUTH_* 이름) 네 군데를 확인한 뒤 이 문서의 표를 고친다.
