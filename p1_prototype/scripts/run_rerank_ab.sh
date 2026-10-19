#!/usr/bin/env bash
# 标准 7 对照实验编排：依次切换 RERANK_MODE=off / heuristic / llm，
# 每档重启 api 容器后跑同一组问题，最后汇总三档指标。
#
# 为什么必须重启：RERANK_MODE 在 settings 里是启动时读取的常量，
# 进程内改环境变量不生效。重启是唯一诚实的做法——
# 用运行时注入的方式"假装切换"，测出来的就不是真实部署形态。
set -u
cd "$(dirname "$0")/.." || exit 1

# Git Bash(MSYS) 会把 docker exec 里的 /app/... 当成 Windows 路径改写，
# 导致容器内报 "can't open file 'E:/.../app/ab_rerank.py'"。
# 关掉路径转换，让参数原样传给容器。
export MSYS_NO_PATHCONV=1

ADMIN_PASS="${ADMIN_PASS:-Adm1n@2026kb}"
MT="${METRICS_TOKEN:-pk-metrics-5c1f8a7d2e9b4c60}"
# 结果目录放在项目内（相对路径）：
# MSYS_NO_PATHCONV=1 下 docker cp 会把 /tmp/... 当成容器内路径，
# 用在宿主机 /tmp 上会导致"文件写成功但宿主机看不到"。
OUTDIR="${OUTDIR:-data_v2/rerank_eval}"
mkdir -p "$OUTDIR"
echo "结果目录：$OUTDIR"

# 一次实验 = 一个 RUNID，三档共享同一 RUNID 但空间各不相同。
# 这样"三档之间"可比较（同语料同问题），"多次实验之间"互不污染。
RUNID="$(date +%H%M%S)"
echo "本次实验 RUNID=$RUNID"

echo "==================================================================="
echo " Rerank 三档对照实验：off / heuristic / llm"
echo "==================================================================="

for MODE in off heuristic llm; do
  echo ""
  echo "-------------------------------------------------------------------"
  echo ">>> 切换到 RERANK_MODE=$MODE"
  echo "-------------------------------------------------------------------"
  ( cd app_v2 && RERANK_MODE="$MODE" docker compose up -d api >/dev/null 2>&1 )
  # 等健康：容器 healthy 才算真的起来了
  for i in $(seq 1 60); do
    st=$(docker inspect -f '{{.State.Health.Status}}' rag_p3_api 2>/dev/null || echo unknown)
    [ "$st" = "healthy" ] && break
    sleep 2
  done
  echo "    api health = $st"
  sleep 3

  # 每档用带运行时间戳的独立评测空间：
  # 1) 上一档写入的问答缓存不会串过来（否则测的是缓存命中率而非检索质量）
  # 2) 历史残留语料不会累积——同名文件重复上传会产生大批重复切片，
  #    把真正正确的那条挤到后面，实验结论就不可信了。
  export EVAL_SPACE="eval_${MODE}_${RUNID}"
  export MODE
  export OUT="$OUTDIR/rerank_${MODE}.json"
  docker cp scripts/ab_rerank.py rag_p3_api:/app/ab_rerank.py >/dev/null 2>&1
  docker exec -e ADMIN_PASS="$ADMIN_PASS" -e METRICS_TOKEN="$MT" \
      -e MODE="$MODE" -e EVAL_SPACE="$EVAL_SPACE" -e OUT="/app/out_${MODE}.json" \
      rag_p3_api python /app/ab_rerank.py
  docker cp "rag_p3_api:/app/out_${MODE}.json" "$OUT" >/dev/null 2>&1 || true
done

echo ""
echo "==================================================================="
echo " 汇总"
echo "==================================================================="
python - "$OUTDIR" <<'PY'
import json, os, sys
d = sys.argv[1]
rows = []
for m in ("off", "heuristic", "llm"):
    p = os.path.join(d, f"rerank_{m}.json")
    if os.path.exists(p):
        rows.append(json.load(open(p, encoding="utf-8")))
if not rows:
    print("  未找到结果文件"); raise SystemExit(1)
print(f"  {'档位':<12}{'Hit@1':>10}{'Hit@3':>10}{'MRR':>10}")
for r in rows:
    print(f"  {r['mode']:<12}{r['hit@1']:>10.0%}{r['hit@3']:>10.0%}{r['mrr']:>10.3f}")
base = next((r for r in rows if r["mode"] == "off"), None)
best = max(rows, key=lambda r: r["mrr"])
if base:
    print(f"\n  相对 off 基线：Hit@3 {base['hit@3']:.0%} -> {best['hit@3']:.0%}，"
          f"MRR {base['mrr']:.3f} -> {best['mrr']:.3f}")
    lift = best["mrr"] - base["mrr"]
    print(f"  MRR 提升 {lift:+.3f}" + ("（明显提升）" if lift >= 0.05 else "（提升不明显）"))
PY
