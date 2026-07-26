#!/usr/bin/env bash
set -euo pipefail

OUTPUT_ROOT=${1:-/home/zry/桌面/0702/0725/raw/strict-barrier-n10}
COUNT=${COUNT:-10}
PROJECT_ROOT=/home/zry/桌面/cubesandbox/CubeSandbox
TOOL_ROOT=/home/zry/桌面/0702/0725/tools
TEMPLATE_ID=${CUBE_TEMPLATE_ID:-tpl-c33298206d4846deac922031}
API_URL=${E2B_API_URL:-http://127.0.0.1:3000}
API_KEY=${E2B_API_KEY:-e2b_000000}
CA_FILE=${SSL_CERT_FILE:-/home/zry/.local/share/mkcert/rootCA.pem}
CUBELET_ADDRESS=127.0.0.1:9999
PROM_INTERVAL=${PROM_INTERVAL:-0.1}
BRIDGE_DIR=$(mktemp -d /tmp/cubelet-observe.XXXXXX)
BRIDGE_SOCKET="$BRIDGE_DIR/cubelet.sock"

mkdir -p "$OUTPUT_ROOT"/{logs,pprof,system,metrics,state}

declare -A LOG_OFFSETS
LOG_FILES=(
  /data/log/CubeMaster/cubemaster-req.log
  /data/log/Cubelet/Cubelet-req.log
  /data/log/Cubelet/Cubelet-stat.log
  /data/log/CubeShim/cube-shim-req.log
  /data/log/CubeShim/cube-shim-stat.log
  /data/log/CubeVmm/vmm.log
  /data/log/network-agent/network-agent-req.log
  /data/log/cubecow/cubecow.log
)

for log_file in "${LOG_FILES[@]}"; do
  if [[ -f "$log_file" ]]; then
    LOG_OFFSETS["$log_file"]=$(stat -c %s "$log_file")
  else
    LOG_OFFSETS["$log_file"]=0
  fi
done

cleanup() {
  if [[ -n ${BRIDGE_PID:-} ]]; then
    kill "$BRIDGE_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
  unlink "$BRIDGE_SOCKET" 2>/dev/null || true
  rmdir "$BRIDGE_DIR" 2>/dev/null || true
}
trap cleanup EXIT

socat "UNIX-LISTEN:${BRIDGE_SOCKET},fork" "TCP:${CUBELET_ADDRESS}" &
BRIDGE_PID=$!
for _ in $(seq 1 50); do
  [[ -S "$BRIDGE_SOCKET" ]] && break
  sleep 0.02
done

{
  date --iso-8601=ns
  uname -a
  cat /usr/local/services/cubetoolbox/VERSION.txt
  systemctl is-active \
    cube-sandbox-control.target \
    cube-sandbox-cube-api.service \
    cube-sandbox-cubemaster.service \
    cube-sandbox-cubelet.service \
    cube-sandbox-network-agent.service
  curl -fsS "${API_URL}/health"
  printf '\n'
  curl -fsS http://127.0.0.1:8089/notify/health
  printf '\n'
} >"$OUTPUT_ROOT/state/environment.txt"

curl -fsS http://127.0.0.1:9998/v1/metrics \
  -o "$OUTPUT_ROOT/metrics/cubelet-before.prom"
curl -fsS http://127.0.0.1:9966/debug/pprof/goroutine?debug=2 \
  -o "$OUTPUT_ROOT/pprof/goroutine-before.txt"

(
  cd "$PROJECT_ROOT/Cubelet"
  go run "$TOOL_ROOT/cubelet_storage_metrics.go" \
    --address "$CUBELET_ADDRESS" \
    --output "$OUTPUT_ROOT/state/cubecow-before.json"
)
/usr/local/bin/cubecli --address "$BRIDGE_SOCKET" cubebox list \
  >"$OUTPUT_ROOT/state/cubelet-sandboxes-before.txt"
/usr/local/bin/cubemastercli cubebox list \
  >"$OUTPUT_ROOT/state/cubemaster-sandboxes-before.txt"
find /data/cubelet/network-agent/state -maxdepth 1 -type f -printf '%f\n' \
  | sort >"$OUTPUT_ROOT/state/network-agent-state-before.txt"
ip -o link show type tun >"$OUTPUT_ROOT/state/tap-interfaces-before.txt"
ps -eo pid,comm,args \
  | awk '$2 ~ /containerd-shim-cube|cube-runtime|cloud-hypervisor|qemu/ {print}' \
  >"$OUTPUT_ROOT/state/sandbox-processes-before.txt"

python3 "$TOOL_ROOT/sample_cubelet_metrics.py" \
  --duration 8 \
  --interval "$PROM_INTERVAL" \
  -o "$OUTPUT_ROOT/metrics/cubelet-samples.jsonl" &
PROM_PID=$!
curl -fsS "http://127.0.0.1:9966/debug/pprof/profile?seconds=8" \
  -o "$OUTPUT_ROOT/pprof/cpu.pprof" &
CPU_PID=$!
curl -fsS "http://127.0.0.1:9966/debug/pprof/trace?seconds=3" \
  -o "$OUTPUT_ROOT/pprof/trace.out" &
TRACE_PID=$!

CUBELET_PID=$(pgrep -xo cubelet)
CUBEMASTER_PID=$(pgrep -xo cubemaster)
CUBE_API_PID=$(pgrep -xo cube-api)
NETWORK_AGENT_PID=$(pgrep -xo network-agent)
PIDS="${CUBELET_PID},${CUBEMASTER_PID},${CUBE_API_PID},${NETWORK_AGENT_PID}"

(timeout 8 iostat -x -d 1 >"$OUTPUT_ROOT/system/iostat.txt" 2>&1 || true) &
IOSTAT_PID=$!
(timeout 8 pidstat -h -u -r -d -w -p "$PIDS" 1 \
  >"$OUTPUT_ROOT/system/pidstat.txt" 2>&1 || true) &
PIDSTAT_PID=$!
(timeout 8 mpstat -P ALL 1 >"$OUTPUT_ROOT/system/mpstat.txt" 2>&1 || true) &
MPSTAT_PID=$!
(timeout 8 vmstat -w 1 >"$OUTPUT_ROOT/system/vmstat.txt" 2>&1 || true) &
VMSTAT_PID=$!

E2B_API_URL="$API_URL" \
E2B_API_KEY="$API_KEY" \
CUBE_TEMPLATE_ID="$TEMPLATE_ID" \
SSL_CERT_FILE="$CA_FILE" \
conda run -n pytorch python "$TOOL_ROOT/lifecycle_destroy_case.py" \
  --count "$COUNT" \
  --settle-seconds 1 \
  --output "$OUTPUT_ROOT/lifecycle.json" \
  2>&1 | tee "$OUTPUT_ROOT/lifecycle.stdout.txt"

wait "$PROM_PID" || true
wait "$CPU_PID" || true
wait "$TRACE_PID" || true
wait "$IOSTAT_PID" || true
wait "$PIDSTAT_PID" || true
wait "$MPSTAT_PID" || true
wait "$VMSTAT_PID" || true

curl -fsS http://127.0.0.1:9998/v1/metrics \
  -o "$OUTPUT_ROOT/metrics/cubelet-after.prom"
curl -fsS http://127.0.0.1:9966/debug/pprof/goroutine?debug=2 \
  -o "$OUTPUT_ROOT/pprof/goroutine-after.txt"
curl -fsS http://127.0.0.1:9966/debug/pprof/mutex \
  -o "$OUTPUT_ROOT/pprof/mutex.pprof"
curl -fsS http://127.0.0.1:9966/debug/pprof/block \
  -o "$OUTPUT_ROOT/pprof/block.pprof"
curl -fsS http://127.0.0.1:9966/debug/pprof/heap \
  -o "$OUTPUT_ROOT/pprof/heap.pprof"

(
  cd "$PROJECT_ROOT/Cubelet"
  go run "$TOOL_ROOT/cubelet_storage_metrics.go" \
    --address "$CUBELET_ADDRESS" \
    --output "$OUTPUT_ROOT/state/cubecow-after.json"
)
/usr/local/bin/cubecli --address "$BRIDGE_SOCKET" cubebox list \
  >"$OUTPUT_ROOT/state/cubelet-sandboxes-after.txt"
/usr/local/bin/cubecli --address "$BRIDGE_SOCKET" storage ls --raw \
  >"$OUTPUT_ROOT/state/cubelet-storage-after.txt"
/usr/local/bin/cubemastercli cubebox list \
  >"$OUTPUT_ROOT/state/cubemaster-sandboxes-after.txt"
find /data/cubelet/network-agent/state -maxdepth 1 -type f -printf '%f\n' \
  | sort >"$OUTPUT_ROOT/state/network-agent-state-after.txt"
ip -o link show type tun >"$OUTPUT_ROOT/state/tap-interfaces-after.txt"
ps -eo pid,comm,args \
  | awk '$2 ~ /containerd-shim-cube|cube-runtime|cloud-hypervisor|qemu/ {print}' \
  >"$OUTPUT_ROOT/state/sandbox-processes-after.txt"

REDIS_PASSWORD=$(awk '
  /^redis:/ { seen=1; next }
  seen && /^[^ ]/ { exit }
  seen && /^  password:/ {
    gsub(/["[:space:]]/, "", $2)
    print $2
    exit
  }
' /usr/local/services/cubetoolbox/CubeMaster/conf.yaml)
while IFS= read -r sandbox_id; do
  exists=$(docker exec \
    -e REDISCLI_AUTH="$REDIS_PASSWORD" \
    cube-sandbox-redis \
    redis-cli --no-auth-warning EXISTS \
    "cube:v1:shared:sandbox:proxy:${sandbox_id}")
  printf '%s %s\n' "$sandbox_id" "$exists"
done < <(jq -r '.verification.created_ids[]' "$OUTPUT_ROOT/lifecycle.json") \
  >"$OUTPUT_ROOT/state/redis-proxy-exists-after.txt"

for log_file in "${LOG_FILES[@]}"; do
  if [[ ! -f "$log_file" ]]; then
    continue
  fi
  base_name=$(basename "$log_file")
  parent_name=$(basename "$(dirname "$log_file")")
  start_byte=${LOG_OFFSETS["$log_file"]}
  tail -c "+$((start_byte + 1))" "$log_file" \
    >"$OUTPUT_ROOT/logs/${parent_name}-${base_name}"
done

python3 "$TOOL_ROOT/analyze_destroy_logs.py" \
  --report "$OUTPUT_ROOT/lifecycle.json" \
  --master-req "$OUTPUT_ROOT/logs/CubeMaster-cubemaster-req.log" \
  --cubelet-stat "$OUTPUT_ROOT/logs/Cubelet-Cubelet-stat.log" \
  --shim-req "$OUTPUT_ROOT/logs/CubeShim-cube-shim-req.log" \
  --shim-stat "$OUTPUT_ROOT/logs/CubeShim-cube-shim-stat.log" \
  --output-json "$OUTPUT_ROOT/destroy-breakdown.json" \
  --output-csv "$OUTPUT_ROOT/destroy-breakdown.csv"

jq -s '{
  sample_count: length,
  max_realtime_create: (
    map(.metrics.cube_cubebox_scheduler_realtime_create_num // 0) | max
  ),
  max_realtime_destroy: (
    map(.metrics.cube_cubebox_scheduler_realtime_destroy_num // 0) | max
  ),
  max_mvm: (
    map(.metrics.cube_cubebox_scheduler_mvm_num // 0) | max
  ),
  max_go_goroutines: (
    map(.metrics.go_goroutines // 0) | max
  )
}' "$OUTPUT_ROOT/metrics/cubelet-samples.jsonl" \
  >"$OUTPUT_ROOT/metrics/cubelet-samples-summary.json"

python3 "$TOOL_ROOT/analyze_system_metrics.py" \
  "$OUTPUT_ROOT/system" \
  --output "$OUTPUT_ROOT/system/summary.json"

printf 'collection complete: %s\n' "$OUTPUT_ROOT"
