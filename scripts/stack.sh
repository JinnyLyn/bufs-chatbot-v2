#!/usr/bin/env bash
# stack.sh — 스택(ollama + backend + frontend) 기동·종료·재기동, 그리고 systemd 유닛의 ExecStart.
# (옛 start-all.sh / stop-all.sh / restart-all.sh / run-*.sh 를 한 파일로 합친 것, 2026-09-14.)
#
#   scripts/stack.sh start                                   # 안 떠 있는 것만 띄우고 /health 를 기다림
#   scripts/stack.sh stop [--with-ollama]                    # 우리 스택만 내림 (ollama 는 기본 유지)
#   scripts/stack.sh restart [--with-ollama] [--build|--no-build]
#                                                            # 필요하면 프론트 재빌드 → stop → start → /health·/health/llm
#   scripts/stack.sh run backend|frontend|ollama             # 유닛 ExecStart — 포그라운드 프로세스 하나 (systemd 전용)
#
# 릴리스의 정문은 scripts/deploy.sh (태그 체크아웃 → 여기 restart → 기록 → 실패 시 롤백, RELEASE.md).
# 이미 체크아웃된 코드를 그냥 재기동할 때만 restart 를 직접 친다.
#
# 공유 서버 원칙: 포트로 죽이지 않는다. 우리 것이라고 식별된 프로세스만 건드린다 (pidfile, 또는 소유자 +
# 명령줄 + cwd — _common.sh 의 find_service_pids). 유닛(camchat.target)이 이 폴더를 서비스하면 start/stop 은
# systemctl 로 위임하고, 다른 폴더(운영) 것이면 자기 프로세스만 직접 관리한다 (staging 의 stop 이 운영을
# 내리면 안 되니까). 개발 폴더에서 기본 포트로 start 를 치면 거부한다.
#
# 환경 (scripts/env.local 이 있으면 먼저 읽는다 — 서버별 설정, gitignored):
#   BACKEND_PORT   (기본 8000)   project/server.py 에 PORT 로도 전달
#   FRONTEND_PORT  (기본 3000)
#   OLLAMA_PORT    (기본: project/.env 의 OLLAMA_BASE_URL 포트, 없으면 11434) — 명시하면 백엔드의
#                  OLLAMA_BASE_URL 도 그리로 덮어쓴다 (안 그러면 .env 의 다른 ollama 에 붙는다)
#   START_OLLAMA   (기본 auto)   auto|yes|no — auto 는 포트가 비어 있을 때만 띄움; no = 남(운영)의 ollama 를 빌려 씀
#   FRONTEND_MODE  (기본 auto)   auto|prod|dev — auto 는 standalone 빌드가 있으면 prod
#   PYTHON         (기본 <repo>/.venv/bin/python, 없으면 python3)
#   CUDA_VISIBLE_DEVICES         ollama 를 띄우려면 필수 (공유 GPU — MIG 슬라이스 UUID)
# 다른 BACKEND_PORT 를 쓰면 cloudflared ingress 도 바꾸고 프론트를 BACKEND_ORIGIN=http://localhost:<포트> 로
# 다시 빌드해야 한다 (/api rewrite 는 빌드 때 박힌다). 다른 사용자가 기본 포트를 쓰고 있을 때:
#   BACKEND_PORT=8010 FRONTEND_PORT=3010 OLLAMA_PORT=11500 scripts/stack.sh start
#
# restart 종료 코드: 0 정상; 3 사전 점검·프론트 빌드·유닛 파일 검증 실패 (서버 안 건드림);
#   4 백엔드는 응답하지만 LLM 확인 실패 (떠 있음, 성능 저하); 1 재기동 뒤 백엔드 무응답.
#   deploy.sh: 3 → 체크아웃만 복원, 4 → 배포 유지 + 경고, 1 → 롤백.

set -Eeuo pipefail

# OLLAMA_PORT 를 호출자가 줬는지 — 그때만 백엔드의 OLLAMA_BASE_URL 을 덮어쓴다 (아니면 project/.env 가
# 결정한다: 일부러 원격 URL 을 쓸 수도 있으니). env.local 을 읽는 _common.sh 를 source 하기 전에 본다.
ollama_port_explicit="${OLLAMA_PORT:+1}"

# shellcheck source=scripts/_common.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
derive_ollama_port
START_OLLAMA="${START_OLLAMA:-auto}"
FRONTEND_MODE="${FRONTEND_MODE:-auto}"
FRONTEND="$REPO/frontend"
STANDALONE_DIR="$FRONTEND/.next/standalone"

usage() { usage_from_header "${BASH_SOURCE[0]}"; }

# stop 이 우리가 띄운 것만 정확히 내리도록 pid 를 기록한다 — 공유 서버에서 포트로 죽이지 않는다.
write_pid() { echo "$2" >"$RUN_DIR/$1.pid"; }
drop_pid()  { rm -f "$RUN_DIR/$1.pid"; }

# 이미 떠 있는 서비스가 우리 것(systemd 나 옛 셸이 띄웠고 pidfile 만 없음)이면 pid 를 입양해 stop/restart 가
# 계속 통하게 한다; 식별되는 게 없을 때만 pidfile 을 지운다. (예전엔 무조건 지워서 "stop 뒤 start" 가 옛
# 코드를 그대로 서비스하게 두는 일이 있었다.)
adopt_pid() {
    local name="$1" pids=()
    mapfile -t pids < <(find_service_pids "$name")
    if [ "${#pids[@]}" -eq 1 ]; then
        write_pid "$name" "${pids[0]}"
        echo "        (떠 있는 $name pid ${pids[0]} 를 $name.pid 로 입양)"
    else
        drop_pid "$name"
    fi
}

# URL 이 200 을 줄 때까지 기다린다. pidfile 을 주면 그 프로세스가 죽는 즉시 포기한다 (부팅 중 죽는 백엔드는
# 타임아웃까지 기다릴 게 아니라 몇 초 만에 실패해야 한다). 4번째 인자는 pidfile 대신 볼 systemd 유닛 —
# "failed"/"inactive" 면 포기 ("activating" 은 아직 뜨는 중이거나 Restart= 사이클).
wait_http_200() {
    local url="$1" timeout="${2:-180}" pidfile="${3:-}" unit="${4:-}" waited=0 pid="" state
    [ -n "$pidfile" ] && [ -f "$pidfile" ] && pid="$(cat "$pidfile")"
    while [ "$waited" -lt "$timeout" ]; do
        if curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then return 0; fi
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            echo "[fail]  pid $pid ($(basename "$pidfile")) 가 기동 중에 죽었습니다." >&2
            return 1
        fi
        if [ -n "$unit" ]; then
            state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
            case "$state" in
                failed|inactive)
                    echo "[fail]  $unit 이 기동 중 $state — journalctl --user -u ${unit%.service} -n 50" >&2
                    return 1 ;;
            esac
        fi
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

# ---------------------------------------------------------------------------------------
# run — 유닛 ExecStart (scripts/systemd/*.service). 포그라운드에서 프로세스 하나를 exec 한다.
# ---------------------------------------------------------------------------------------
cmd_run() {
    case "${1:-}" in
        backend)
            # 레포 venv 로 FastAPI 서버. 첫 로그 줄의 커밋은 deploy.sh status 가 "실제 도는 코드" 로 읽는다.
            resolve_python || exit 1
            cd "$REPO"
            echo "[backend] starting on :$BACKEND_PORT ($(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
            exec env PORT="$BACKEND_PORT" "$PYTHON" project/server.py ;;
        frontend)
            # Next.js standalone 서버. 번들엔 .next/static 과 public/ 이 없어 매번 곁에 스테이징한다 —
            # 그래서 배포 = `npm run build` + 이 유닛 재기동.
            if [ ! -f "$STANDALONE_DIR/server.js" ]; then
                echo "[frontend] standalone 빌드가 없습니다 ($STANDALONE_DIR/server.js) — cd frontend && npm run build" >&2
                exit 1
            fi
            command -v node >/dev/null 2>&1 || { echo "[frontend] PATH 에 node 가 없습니다 (scripts/env.local 의 PATH 확인)" >&2; exit 1; }
            stage_standalone
            cd "$STANDALONE_DIR"
            echo "[frontend] standalone 서버 시작 127.0.0.1:$FRONTEND_PORT (빌드 $(cat "$FRONTEND/.next/BUILD_ID" 2>/dev/null || echo '?'))"
            exec env PORT="$FRONTEND_PORT" HOSTNAME=127.0.0.1 node server.js ;;
        ollama)
            # project/.env 가 dial 하는 포트와 팀의 MIG 슬라이스(CUDA_VISIBLE_DEVICES, env.local)에 고정한
            # 포그라운드 `ollama serve`. .env 가 원격 ollama 를 가리키면 아무것도 안 띄우고 0 으로 끝난다
            # (유닛은 restart-loop 대신 inactive 로 남는다). systemd 밖에서 띄운 ollama 가 포트를 쥐고
            # 있으면 바인드에 실패한다 — 그게 신호다; `stack.sh stop --with-ollama` 로 먼저 내린다.
            if [ "$OLLAMA_LOCAL" != 1 ]; then
                echo "[ollama] project/.env 의 OLLAMA_BASE_URL 이 원격 — 여기서 띄울 것 없음."
                exit 0
            fi
            if [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
                echo "[ollama] 기동 거부: CUDA_VISIBLE_DEVICES 가 없습니다 (공유 GPU 서버)." >&2
                echo "         scripts/env.local 에 MIG 슬라이스 UUID 를 넣으세요." >&2
                exit 1
            fi
            command -v ollama >/dev/null 2>&1 || { echo "[ollama] PATH 에 ollama 가 없습니다 (scripts/env.local 의 PATH 확인)" >&2; exit 1; }
            echo "[ollama] 127.0.0.1:$OLLAMA_PORT 에서 서비스 (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
            exec env OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama serve ;;
        *)
            echo "사용법: $0 run backend|frontend|ollama" >&2; exit 2 ;;
    esac
}

# ---------------------------------------------------------------------------------------
# start — 안 떠 있는 것만 띄운다 (멱등). 이미 리슨 중인 건 그대로 두고, 우리 것이면 pid 를 입양한다.
# ---------------------------------------------------------------------------------------
# start 전에 확인할 것 — 아무것도 바꾸지 않는다. restart 는 stop 하기 전에 이걸 먼저 돌려 "내렸는데 못
# 올리는" 일을 막는다. 반환: 0 통과, 1 인터프리터 문제, 2 폴더·포트·override 문제.
start_preflight() {
    # 레포 venv 우선 — 이 서버의 맨 python3 는 miniconda 라 fastapi 가 없어, venv 안 켠 셸에서 띄우면
    # 백엔드가 "No module named 'fastapi'" 로 죽었다. run backend 와 같은 resolve_python 을 쓴다.
    resolve_python || return 1

    # --- 유닛이 다른 폴더(운영)를 서비스 중인데 그 폴더와 같은 포트라면: 개발 폴더에서 실수로 친 것 ----
    # 운영 포트를 뺏거나 "이미 떠 있음" 이라며 운영을 자기 것처럼 입양하게 된다. 포트 하나만 겹쳐도 거부.
    if units_present && ! units_installed; then
        local sb sf
        read -r sb sf < <(units_serving_ports)
        if [ "$BACKEND_PORT" = "$sb" ] || [ "$FRONTEND_PORT" = "$sf" ]; then
            echo "[error] 유닛이 $(units_serving_dir) 를 :$sb/:$sf 로 서비스 중입니다 — 이 폴더($REPO)에서 스택을 띄우려면" >&2
            echo "        scripts/env.local 에 다른 포트를 지정하세요 (export BACKEND_PORT=8020 FRONTEND_PORT=3020). RELEASE.md '폴더 셋'." >&2
            return 2
        fi
    fi

    # --- 유닛이 이 폴더 것이면: 유닛은 이 셸이 아니라 scripts/env.local 을 읽는다 — 명령줄에서 준 포트/모드
    # override 는 우리는 한 포트를 보는데 유닛은 다른 포트에 바인드하게 만든다.
    if units_installed; then
        unit_env() { ( unset "$1"; [ -f "$_COMMON_DIR/env.local" ] && . "$_COMMON_DIR/env.local"; echo "${!1:-$2}" ); }
        local spec var def
        for spec in BACKEND_PORT:8000 FRONTEND_PORT:3000 START_OLLAMA:auto FRONTEND_MODE:auto; do
            var="${spec%%:*}"; def="${spec#*:}"
            if [ "${!var}" != "$(unit_env "$var" "$def")" ]; then
                echo "[error] $var=${!var} 는 셸에서 준 값인데 systemd 유닛은 scripts/env.local 을 읽습니다 — 거기에 적으세요." >&2
                return 2
            fi
        done
    fi
    return 0
}

cmd_start() {
    [ $# -eq 0 ] || { echo "사용법: $0 start" >&2; exit 2; }
    local prc=0
    start_preflight || prc=$?
    [ "$prc" = 0 ] || exit "$prc"

    # 서비스마다 setsid 로 자기 프로세스 그룹/세션을 준다: stop 이 서비스별로 그룹을 죽이는데, setsid 없이는
    # 셋이 이 스크립트의 그룹을 같이 써서 하나를 멈추면 나머지도 내려간다 (실제로 확인된 동작).
    local SETSID
    SETSID="$(command -v setsid || true)"
    [ -n "$SETSID" ] || echo "[warn]  setsid 가 없습니다 — 'stack.sh stop' 이 자식 프로세스를 깔끔히 못 거둘 수 있습니다."

    # --- 유닛이 이 폴더 것이면 프로세스는 유닛이 소유한다 (scripts/systemd/, setup.sh units) ----------
    # 여기서 systemctl 로 띄워 이 스크립트가 유일한 입구로 남는다; 준비 확인은 똑같이 한다.
    if units_installed; then
        # 유닛 파일 동기화는 restart 와 deploy.sh 의 몫이다 — start 는 옛 start-all.sh 처럼 프로세스만 띄운다.
        echo "[units] camchat.target 이 설치돼 있음 — systemd 로 기동 (프로세스별 Restart=on-failure)"
        systemctl --user start camchat.target
        local ok=0
        wait_http_200 "http://127.0.0.1:$BACKEND_PORT/health" 300 "" camchat-backend.service || ok=1
        wait_port "$FRONTEND_PORT" 90 || ok=1
        systemctl --user --no-pager --no-legend list-units 'camchat-*' | sed 's/^/        /'
        return "$ok"
    fi

    # --- 0) Qdrant 는 임베디드(단일 writer): 두 번째 writer 는 로드 때 실패한다. 락 파일은 늘 주인보다 오래
    #        남으므로(락 자체는 프로세스가 죽으면 풀리는 OS advisory lock) 파일이 있다는 것만으론 문제가
    #        아니다 — 지금 누가 실제로 잡고 있을 때만 경고한다.
    local qlock="$REPO/qdrant_db/.lock" holders
    if [ -e "$qlock" ] && ! port_open "$BACKEND_PORT"; then
        holders="$(lock_holders "$qlock")" || holders="?"
        if [ "$holders" = "?" ]; then
            echo "[warn]  qdrant_db/.lock 은 있는데 :$BACKEND_PORT 에 아무것도 없음 (누가 잡고 있는지 모름: /proc 없음)."
            echo "        ingest/reindex 가 돌고 있지 않다면 지우세요: rm $qlock"
        elif [ -n "$holders" ]; then
            echo "[warn]  qdrant_db 를 pid $(echo "$holders" | tr '\n' ' ')가 잡고 있음 — ingest/reindex 나 옛 백엔드."
            echo "        그게 끝나기 전엔 백엔드가 로드에 실패합니다."
        fi
        # 아무도 안 잡고 있음: 남은 파일일 뿐, 무해 — Qdrant 가 재사용한다. 조용히.
    fi

    # --- 1) Ollama (로컬 GPU) ---
    if [ "$OLLAMA_LOCAL" != 1 ]; then
        echo "[skip]  project/.env 의 OLLAMA_BASE_URL 이 원격 — ollama 는 관리하지 않음."
    elif port_open "$OLLAMA_PORT"; then
        echo "[ok]    Ollama 이미 :$OLLAMA_PORT 에 떠 있음"
        # START_OLLAMA=no = 남(운영)의 ollama 를 빌려 쓰는 폴더(staging) — pid 를 입양하면 stop --with-ollama 가
        # 운영 LLM 을 내리게 되므로 기록하지 않는다.
        if [ "$START_OLLAMA" = "no" ]; then drop_pid ollama; else adopt_pid ollama; fi
    elif [ "$START_OLLAMA" = "no" ]; then
        echo "[skip]  :$OLLAMA_PORT 에 Ollama 없음 (START_OLLAMA=no)"
        drop_pid ollama
    elif ! command -v ollama >/dev/null 2>&1; then
        echo "[warn]  PATH 에 ollama 가 없고 :$OLLAMA_PORT 도 비어 있음 — 백엔드가 LLM 에 못 닿습니다."
        drop_pid ollama
    elif [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
        # 공유 GPU: MIG 슬라이스 UUID 없이 뜬 ollama 는 아무 GPU 나 잡거나(CPU 로 떨어지거나) 남의 장치를
        # 가로챌 수 있다. 거부.
        echo "[error] Ollama 기동 거부: CUDA_VISIBLE_DEVICES 가 없습니다."
        echo "        scripts/env.local 에 MIG 슬라이스 UUID 를 넣으세요. 예:"
        echo "        export CUDA_VISIBLE_DEVICES=MIG-xxxxxxxx-...."
        drop_pid ollama
    else
        echo "[start] Ollama :$OLLAMA_PORT (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
        OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" $SETSID nohup ollama serve \
            >"$LOG_DIR/ollama/ollama.out" 2>"$LOG_DIR/ollama/ollama.err" &
        write_pid ollama $!
        wait_port "$OLLAMA_PORT" 30 || echo "[warn]  Ollama 가 30초 안에 안 떴습니다 — logs/ollama/ 확인"
    fi

    # --- 2) Backend (FastAPI) ---
    if port_open "$BACKEND_PORT"; then
        echo "[ok]    backend 이미 :$BACKEND_PORT 에 떠 있음"
        adopt_pid backend
    else
        echo "[start] backend :$BACKEND_PORT"
        if [ -n "$ollama_port_explicit" ]; then
            echo "        (OLLAMA_PORT 지정됨 — 백엔드 OLLAMA_BASE_URL=http://127.0.0.1:$OLLAMA_PORT 로 덮어씀)"
            export OLLAMA_BASE_URL="http://127.0.0.1:$OLLAMA_PORT"
        fi
        (
            cd "$REPO"
            PORT="$BACKEND_PORT" $SETSID nohup "$PYTHON" project/server.py \
                >"$LOG_DIR/backend/server.out" 2>"$LOG_DIR/backend/server.err" &
            write_pid backend $!
        )
    fi

    # --- 3) Frontend (Next.js) ---
    # next.config.ts 가 output:"standalone" 이라 `next start` 는 안 된다 — standalone 서버를 직접 띄우고
    # 정적 파일을 곁에 스테이징한다.
    if port_open "$FRONTEND_PORT"; then
        echo "[ok]    frontend 이미 :$FRONTEND_PORT 에 떠 있음"
        adopt_pid frontend
    else
        local mode="$FRONTEND_MODE"
        if [ "$mode" = "auto" ]; then
            if [ -f "$STANDALONE_DIR/server.js" ]; then mode=prod; else mode=dev; fi
        fi

        if [ "$mode" = "prod" ]; then
            [ -f "$STANDALONE_DIR/server.js" ] || { echo "[build] npm run build (standalone 번들이 아직 없음)"; (cd "$FRONTEND" && npm run build >"$LOG_DIR/frontend/build.log" 2>&1); }
            stage_standalone
            echo "[start] frontend :$FRONTEND_PORT (standalone)"
            (
                cd "$STANDALONE_DIR"
                PORT="$FRONTEND_PORT" HOSTNAME=127.0.0.1 $SETSID nohup node server.js \
                    >"$LOG_DIR/frontend/frontend.out" 2>"$LOG_DIR/frontend/frontend.err" &
                write_pid frontend $!
            )
        else
            echo "[start] frontend :$FRONTEND_PORT (npm run dev)"
            (
                cd "$FRONTEND"
                PORT="$FRONTEND_PORT" $SETSID nohup npm run dev \
                    >"$LOG_DIR/frontend/frontend.out" 2>"$LOG_DIR/frontend/frontend.err" &
                write_pid frontend $!
            )
        fi
    fi

    # --- 준비 확인 -------------------------------------------------------------
    # /health 는 임베딩 모델 + 그래프가 다 만들어진 뒤에야 200 이라 넉넉히 기다린다.
    local backend_up=0 frontend_up=0 ollama_up=1
    wait_http_200 "http://127.0.0.1:$BACKEND_PORT/health" 300 "$RUN_DIR/backend.pid" && backend_up=1
    wait_port "$FRONTEND_PORT" 90 && frontend_up=1
    if [ "$OLLAMA_LOCAL" = 1 ] && [ "$START_OLLAMA" != "no" ]; then
        ollama_up=0; port_open "$OLLAMA_PORT" && ollama_up=1
    fi

    echo
    if [ "$OLLAMA_LOCAL" = 1 ]; then
        echo "Ollama   :$OLLAMA_PORT   -> $(port_open "$OLLAMA_PORT" && echo up || echo down)"
    else
        echo "Ollama   원격 (project/.env OLLAMA_BASE_URL) — 여기서 관리 안 함"
    fi
    echo "Backend  :$BACKEND_PORT  -> $([ "$backend_up" = 1 ] && echo up || echo down)   (health: http://127.0.0.1:$BACKEND_PORT/health)"
    echo "Frontend :$FRONTEND_PORT -> $([ "$frontend_up" = 1 ] && echo up || echo down)   (open:   http://127.0.0.1:$FRONTEND_PORT)"

    if [ "$backend_up" != 1 ] || [ "$frontend_up" != 1 ] || [ "$ollama_up" != 1 ]; then
        echo "일부 서비스가 준비되지 않았습니다 — $LOG_DIR/ 확인." >&2
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------------------
# stop — 우리 스택 프로세스만 내린다. 두 가지 경로로 "우리 것" 을 정한다:
#   1) start 가 logs/run/*.pid 에 기록한 pid 이고 명령줄이 아직 그 서비스처럼 보일 때 (재부팅 뒤 pid 재활용 대비)
#   2) pidfile 이 없거나 낡았으면 소유자 + 정체로 — 이 사용자 소유이고 이 체크아웃에 묶인 프로세스
#      (_common.sh find_service_pids). systemd 나 옛 셸이 띄워 pidfile 이 없던 서비스가 stop 을 살아남고
#      start 가 "이미 떠 있음" 이라 하던 배포 함정을 이게 막는다.
# ollama 는 기본으로 둔다 — 모델이 VRAM 에 상주하고(LLM_KEEP_ALIVE=-1) 다른 사용자가 같이 쓸 수도 있다.
# --with-ollama 여도 팀 소유 인스턴스(project/.env 의 OLLAMA_BASE_URL 포트)만 건드린다.
# ---------------------------------------------------------------------------------------
expected_cmd() {   # 기록된 pid 의 명령줄이 담고 있어야 할 것 (start 참고)
    case "$1" in
        frontend) echo 'node|npm|next' ;;     # node server.js (standalone, argv 를 next-server 로 바꿈) 또는 npm run dev
        backend)  echo 'server\.py|python' ;;
        ollama)   echo 'ollama' ;;
    esac
}

service_port() {
    case "$1" in
        frontend) echo "$FRONTEND_PORT" ;;
        backend)  echo "$BACKEND_PORT" ;;
        ollama)   echo "$OLLAMA_PORT" ;;
    esac
}

# 프로세스 그룹에 TERM (setsid 로 서비스마다 그룹이 따로다; `npm run dev` 는 자식을 친다), 10초 기다린 뒤 KILL.
# 우리 자신의 그룹은 절대 그룹-kill 하지 않는다.
kill_pid() {
    local name="$1" pid="$2" pgid own_pgid
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    own_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ] && [ "$pgid" != "$own_pgid" ]; then
        kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
        pgid=""
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "$name: pid $pid 가 TERM 에 안 죽어 KILL 보냄"
        [ -n "$pgid" ] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "$name: 내림 (pid $pid)"
}

cmd_stop() {
    # 인자부터 검사 — 모르는 플래그가 전체 stop 으로 흘러가면 안 된다 (서비스를 내리는 명령이다).
    case "$#:${1:-}" in 0:|1:--with-ollama) ;; *) echo "사용법: $0 stop [--with-ollama]" >&2; exit 2 ;; esac
    local targets=(frontend backend) with_ollama=0
    if [ "${1:-}" = --with-ollama ]; then
        if [ "$START_OLLAMA" = no ]; then
            # staging 처럼 남의 ollama 를 빌려 쓰는 폴더: find_service_pids ollama 는 포트로만 찾으니
            # 여기서 --with-ollama 를 받아 주면 운영 LLM 을 내린다.
            echo "ollama: 이 폴더는 ollama 를 관리하지 않습니다 (START_OLLAMA=no) — --with-ollama 무시"
        elif [ "$OLLAMA_LOCAL" = 1 ]; then targets+=(ollama); with_ollama=1
        else echo "ollama: project/.env 기준 원격 — 여기서 관리 안 함"; fi
    fi

    # 유닛이 이 폴더 것이면 유닛으로 내리고, 아래 정체 기반 정리로 이어진다: 유닛 소속이 아닌 스택(셸에서 띄운
    # 프로세스)이 포트를 쥔 채 남으면 다음 start 가 옛 코드를 "떠 있음" 이라 하게 된다.
    local name units=()
    if units_installed; then
        for name in "${targets[@]}"; do units+=("camchat-$name.service"); done
        echo "[units] systemd 로 내림: ${units[*]}"
        systemctl --user stop "${units[@]}"
    fi

    local pidfile pid args port stopped_any
    for name in "${targets[@]}"; do
        pidfile="$RUN_DIR/$name.pid"
        stopped_any=0

        # --- 경로 1: 기록된 pidfile, 정체 확인 ---
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile")"
            if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
                echo "$name: pidfile 이 낡음 (pid $pid 없음)"
                rm -f "$pidfile"
            else
                args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
                if ! grep -qE "$(expected_cmd "$name")" <<<"$args"; then
                    echo "$name: pid $pid 는 지금 '${args:-?}' — 우리 것 아님 (재활용된 PID), 죽이지 않고 pidfile 만 지움."
                    rm -f "$pidfile"
                else
                    kill_pid "$name" "$pid"
                    rm -f "$pidfile"
                    stopped_any=1
                fi
            fi
        fi

        # --- 경로 2: 추적 안 된 생존자를 정체로 찾는다 (경로 1 뒤에도 돈다 — `npm run dev` 자식이 그룹 kill 을 비껴갈 수 있다) ---
        for pid in $(find_service_pids "$name"); do
            kill -0 "$pid" 2>/dev/null || continue
            echo "$name: pidfile 없이 떠 있는 이 체크아웃의 $name 프로세스 발견 (pid $pid)"
            kill_pid "$name" "$pid"
            stopped_any=1
        done

        # --- 포트가 실제로 비었는지 — 여기서 포트가 살아 있으면 다음 start 가 거기 붙은 것을 그대로 서비스한다.
        port="$(service_port "$name")"
        if [ -n "$port" ] && port_open "$port"; then
            echo "$name: 경고 — :$port 에 아직 무언가 떠 있음 (다른 사용자의 프로세스?)." >&2
            echo "$name: 'stack.sh start' 는 그걸 '이미 떠 있음' 으로 봅니다 — 재기동 전에 확인하세요." >&2
        elif [ "$stopped_any" = 0 ] && ! units_installed; then
            echo "$name: 내릴 것 없음"
        fi
    done

    if [ "$with_ollama" != 1 ]; then
        echo "참고: Ollama 는 그대로 둠 (모델이 VRAM 에 상주). 같이 내리려면 --with-ollama."
    fi
    return 0
}

# ---------------------------------------------------------------------------------------
# restart — 순서대로:
#   1. 배포 점검: 서비스할 커밋을 찍는다. 릴리스 태그면 그걸로 끝; 브랜치면 main 이 아닐 때, origin/main 보다
#      뒤처졌을 때, 워크트리가 더러울 때(커밋 안 된 수정을 서비스) 경고.
#   2. 사전 점검(인터프리터·폴더·포트·override) — 실패하면 아무것도 내리지 않고 3 으로 끝난다.
#   3. 필요할 때만 프론트 재빌드: `npm run build` 는 아무것도 멈추기 전에 돈다 — 빌드 중에도 옛 스택이 계속
#      서비스하고, 다운타임은 재기동 순간뿐. "필요" = 프론트 소스가 .next/BUILD_ID 보다 새로움
#      (--build 로 강제, --no-build 로 생략). 실패하면 3.
#   4. 유닛 파일 동기화: scripts/systemd/ 가 설치본과 다르면 검증 → 교체 → daemon-reload (_common.sh units_refresh).
#      검증·설치 실패면 3.
#   5. stop → start (그동안 healthcheck 타이머가 "복구" 하겠다고 끼어들지 않게 maintenance 플래그를 세운다).
#   6. /health 와 /health/llm 을 찔러 본다 — "재기동됨" 이 "프로세스 있음" 이 아니라 "응답하고 LLM 에도 닿음"
#      을 뜻하도록.
# ---------------------------------------------------------------------------------------
cmd_restart() {
    local with_ollama="" build=auto arg
    for arg in "$@"; do
        case "$arg" in
            --with-ollama) with_ollama="--with-ollama" ;;
            --build)       build=yes ;;
            --no-build)    build=no ;;
            *) echo "사용법: $0 restart [--with-ollama] [--build|--no-build]" >&2; exit 2 ;;
        esac
    done

    # --- 1) 배포 점검 ------------------------------------------------------------
    local branch head head_sha tag behind at_main_tip=0
    branch="$(git -C "$REPO" branch --show-current 2>/dev/null || echo '?')"
    head="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
    head_sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    tag="$(head_release_tag)"
    behind="$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
    # 둘 다 빈 값(git 이 답을 못 함)이면 "main 최신" 이 아니다.
    [ -n "$head_sha" ] && [ "$head_sha" = "$(git -C "$REPO" rev-parse origin/main 2>/dev/null || true)" ] && at_main_tip=1
    if [ -n "$tag" ]; then
        # 릴리스 태그는 deploy.sh 가 체크아웃한 바로 그것 — main 이 아닌 게 정상.
        echo "[deploy] 릴리스 $tag ($head) 서비스"
    elif units_installed; then
        # 운영 폴더인데 릴리스 태그가 아니다 — 사용자가 릴리스 안 된 코드를 보게 된다. 첫 배포 전
        # (setup.sh units --move 직후) 에만 정상.
        echo "[deploy] $head 서비스 (브랜치 '${branch:-detached}')"
        echo "[warn]   운영 폴더인데 릴리스 태그가 아닙니다 — 릴리스는 ./scripts/deploy.sh <태그> 로 (RELEASE.md)."
        [ "$behind" = 0 ] || echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
    elif [ "$branch" = main ] || [ "$at_main_tip" = 1 ]; then
        # main 이거나 origin/main 최신을 detached 로 꺼낸 것(staging.sh) — 경고할 게 없다.
        echo "[deploy] main ($head) 서비스"
        [ "$behind" = 0 ] || echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
    else
        echo "[deploy] $head 서비스 (브랜치 '${branch:-detached}')"
        echo "[warn]   main 도 릴리스 태그도 아님 — 이 폴더가 '${branch:-$head}' 를 서비스하게 됩니다."
        echo "[warn]   origin/main 보다 $behind 커밋 뒤처짐 (마지막 fetch 기준)."
    fi
    if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
        echo "[warn]   워크트리가 더러움 — 커밋 안 된 코드를 서비스합니다."
    fi

    # --- 2) 사전 점검 — 여기서 걸리면 서버는 건드리지 않았다 (3: deploy.sh 가 체크아웃만 되돌린다) --------
    local prc=0
    start_preflight || prc=$?
    [ "$prc" = 0 ] || { echo "[error] 사전 점검 실패 — 서버는 건드리지 않았습니다." >&2; exit 3; }

    # --- 3) 프론트 소스가 바뀌었으면 재빌드 ---------------------------------------
    local BUILD_ID="$FRONTEND/.next/BUILD_ID" stale
    if [ "$build" = auto ]; then
        build=no
        if [ ! -f "$BUILD_ID" ]; then
            build=yes
        else
            stale="$(find "$FRONTEND/src" "$FRONTEND/public" \
                          "$FRONTEND/package.json" "$FRONTEND/package-lock.json" \
                          "$FRONTEND/next.config.ts" "$FRONTEND/tsconfig.json" \
                          -newer "$BUILD_ID" -print -quit 2>/dev/null || true)"
            [ -n "$stale" ] && { echo "[build]  마지막 빌드 이후 프론트 소스가 바뀜 (예: ${stale#"$FRONTEND/"})"; build=yes; }
        fi
    fi
    if [ "$build" = yes ]; then
        echo "[build]  npm run build (그동안 옛 스택이 계속 서비스) -> logs/frontend/build.log"
        (cd "$FRONTEND" && npm run build >"$LOG_DIR/frontend/build.log" 2>&1) \
            || { echo "[error] 프론트 빌드 실패 — 서버는 건드리지 않았습니다. logs/frontend/build.log 확인"; exit 3; }
    fi

    # --- 4) 유닛 파일 동기화 (아직 아무것도 안 멈췄다 — 검증·설치 실패면 여기서 그만) ----
    local urc=0; units_refresh || urc=$?
    [ "$urc" -lt 2 ] || { echo "[error] 유닛 파일 갱신 실패 (rc=$urc) — 서버는 건드리지 않았습니다. scripts/systemd/ 확인" >&2; exit 3; }

    # --- 5) stop → start ---------------------------------------------------------
    maint_on "stack.sh restart"; trap maint_off EXIT
    cmd_stop ${with_ollama:+"$with_ollama"}
    echo
    cmd_start
    maint_off; trap - EXIT

    # --- 6) 끝에서 끝까지 찔러 보기 -------------------------------------------------
    echo
    if curl -fsS --max-time 10 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
        if curl -fsS --max-time 20 "http://127.0.0.1:$BACKEND_PORT/health/llm" >/dev/null 2>&1; then
            echo "[done]   백엔드 정상, LLM 도 닿음 — $head 운영 중."
        else
            echo "[warn]   백엔드는 떴지만 /health/llm 실패 — LLM 에 닿지 않음 (ollama 확인)." >&2
            exit 4
        fi
    else
        echo "[error]  재기동 뒤 /health 무응답 — logs/backend/server.err 확인" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------------------
cmd="${1:-}"; [ $# -eq 0 ] || shift
case "$cmd" in
    start)     cmd_start "$@" ;;
    stop)      cmd_stop "$@" ;;
    restart)   cmd_restart "$@" ;;
    run)       cmd_run "$@" ;;
    -h|--help) usage ;;
    "")        usage >&2; exit 2 ;;
    *)         echo "알 수 없는 명령: $cmd" >&2; usage >&2; exit 2 ;;
esac
