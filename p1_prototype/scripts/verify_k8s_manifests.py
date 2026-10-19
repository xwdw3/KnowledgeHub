"""K8s 清单的静态校验：不依赖集群，检查的是**语义契约**而不是 YAML 语法。

为什么不用 `kubectl apply --dry-run=client`：
它需要连上 apiserver 才能做 API 发现（`/api`、`/openapi/v2`），
没有集群时连"这个 Kind 是否存在"都判断不了，只会吐一堆
`dial tcp 127.0.0.1:8080: connectex refused`。那不是清单的问题。

所以这里换个思路：把验收标准里"能弹性扩容、能自愈"的要求
翻译成一组可机检的结构断言。语法层用 yaml.safe_load 兜底，
语义层逐条断言——这些断言恰好是最容易被写错、且写错后
`kubectl apply` 依然会成功的地方：

  - liveness 查依赖 → 依赖抖动会把所有副本一起重启
  - HPA 缺 requests → 算不出利用率，永远不扩容（且不报错）
  - worker 用 CPU 扩缩 → 等 IO 时 CPU 接近 0，正在跑的任务被缩掉
  - 探针指向不存在的端口/未鉴权路径 → Pod 无限重启
  - 缺 startupProbe → 冷启动慢于 initialDelay 时被 liveness 杀掉
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "k8s"

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def load_all() -> list[dict]:
    docs: list[dict] = []
    for p in sorted(K8S.glob("*.yaml")):
        try:
            for d in yaml.safe_load_all(p.read_text(encoding="utf-8")):
                if isinstance(d, dict) and d.get("kind"):
                    d["__file"] = p.name
                    docs.append(d)
        except yaml.YAMLError as exc:
            FAIL.append(f"{p.name} YAML 解析失败")
            print(f"  FAIL  {p.name} YAML 解析失败   {exc}")
    return docs


def find(docs, kind, name=None):
    for d in docs:
        if d.get("kind") == kind and (name is None or d.get("metadata", {}).get("name") == name):
            return d
    return None


def containers(dep) -> list[dict]:
    spec = dep.get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("containers", []) or []


def main() -> int:
    section("0. 文件与解析")
    files = sorted(p.name for p in K8S.glob("*.yaml"))
    check("k8s 目录下有清单文件", len(files) >= 6, f"{len(files)} 个")
    docs = load_all()
    check("全部 YAML 可解析", bool(docs) and not any("解析失败" in f for f in FAIL),
          f"{len(docs)} 个对象")

    section("1. 命名空间与镜像来源")
    ns = find(docs, "Namespace")
    check("声明了独立 Namespace", ns is not None, ns.get("metadata", {}).get("name") if ns else "")
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    check("有 api 与 worker 两个 Deployment", len(deployments) >= 2,
          ", ".join(d["metadata"]["name"] for d in deployments))
    # 镜像写 latest 会导致"重启后行为变了但没人知道为什么"
    bad_tags = []
    for d in deployments:
        for c in containers(d):
            img = c.get("image", "")
            if img.endswith(":latest") or (":" not in img.split("/")[-1]):
                bad_tags.append(f"{d['metadata']['name']}:{img}")
    check("镜像不是 latest / 无 tag（可复现）", not bad_tags, "; ".join(bad_tags))

    section("2. 探针语义：liveness 绝不能查依赖")
    # 这是最容易写错、故障放大最狠的一处：
    # liveness 查 PG，PG 抖动 10 秒 → kubelet 认为"进程死了" → 全部副本同时重启
    # → 可恢复的依赖故障变成全站 502。依赖健康属于 readiness 的职责。
    for d in deployments:
        name = d["metadata"]["name"]
        for c in containers(d):
            probes = c.get("lifecycle", {}) or {}
            lv = c.get("livenessProbe") or {}
            rd = c.get("readinessProbe") or {}
            st = c.get("startupProbe") or {}
            lv_path = (lv.get("httpGet") or {}).get("path", "")
            rd_path = (rd.get("httpGet") or {}).get("path", "")
            if lv:
                check(f"{name}: liveness 不查依赖（/health 而非 /ready）",
                      "ready" not in lv_path and "stats" not in lv_path, lv_path)
                # /api/v1/stats 不在免鉴权白名单里，用它当探针会一直 401 → 无限重启
                check(f"{name}: liveness 未指向需要鉴权的路径",
                      "stats" not in lv_path and "documents" not in lv_path, lv_path)
            if rd:
                check(f"{name}: readiness 有独立判据", rd_path != lv_path,
                      f"live={lv_path or '-'} ready={rd_path or '-'}")
            if lv and not st and lv.get("initialDelaySeconds", 0) < 60:
                check(f"{name}: 慢启动组件要么有 startupProbe 要么给足 initialDelay",
                      False, f"initialDelay={lv.get('initialDelaySeconds')}")
            del probes

    section("3. 资源与弹性扩容")
    for d in deployments:
        name = d["metadata"]["name"]
        for c in containers(d):
            res = c.get("resources", {})
            check(f"{name}: 声明了 requests（HPA 算利用率的前提）",
                  bool(res.get("requests")), str(res.get("requests")))
            check(f"{name}: 声明了 limits（防单 Pod 打爆节点）",
                  bool(res.get("limits")), str(res.get("limits")))
            ml = (res.get("limits") or {}).get("memory", "")
            # 内存 limit 必须大于流式处理的峰值预算，否则 1GB 文档会被 OOMKill
            ok = bool(re.match(r"^(\d+)(Gi|Mi|G|M)$", str(ml)))
            check(f"{name}: memory limit 可解析", ok, str(ml))

    hpas = [d for d in docs if d.get("kind") == "HorizontalPodAutoscaler"]
    check("至少有一个 HPA", bool(hpas), f"{len(hpas)} 个")
    for h in hpas:
        name = h["metadata"]["name"]
        spec = h.get("spec", {})
        check(f"HPA {name}: 配置了 maxReplicas > minReplicas",
              spec.get("maxReplicas", 0) > spec.get("minReplicas", 0),
              f"{spec.get('minReplicas')}~{spec.get('maxReplicas')}")
        metrics = spec.get("metrics") or []
        check(f"HPA {name}: 有扩缩指标", bool(metrics),
              ", ".join(m.get("type", "") for m in metrics))

    section("4. worker 的扩容判据不能用 CPU")
    # 大文档在等对象存储下载、等 embedding 返回时 CPU 接近 0。
    # 用 CPU 判据 → HPA 缩容 → acks_late 的任务被重投 → 前面解析的全部白做。
    keda = find(docs, "ScaledObject")
    check("提供了基于队列积压的 ScaledObject", keda is not None,
          keda["metadata"]["name"] if keda else "")
    if keda:
        triggers = keda.get("spec", {}).get("triggers") or []
        types = [t.get("type") for t in triggers]
        check("ScaledObject 使用 redis list 长度作为判据",
              "redis" in types, ", ".join(str(t) for t in types))
        redis_triggers = [t for t in triggers if t.get("type") == "redis"]
        lists = [str((t.get("metadata") or {}).get("listName", "")) for t in redis_triggers]
        check("队列名覆盖 rag_large（大文档专用队列）",
              any("rag_large" in x for x in lists), ", ".join(lists))
        check("队列名覆盖默认 rag 队列",
              any(x == "rag" for x in lists), ", ".join(lists))
        # 两条 trigger 的阈值必须不同：大文档单条就值得扩容，
        # 若两条都用 20 条，GB 级任务会长时间排队而副本数不动。
        thr = {str((t.get("metadata") or {}).get("listName", "")):
               str((t.get("metadata") or {}).get("listLength", "")) for t in redis_triggers}
        check("大文档队列的扩容阈值更灵敏（小于默认队列）",
              thr.get("rag_large", "999").isdigit() and thr.get("rag", "0").isdigit()
              and int(thr["rag_large"]) < int(thr["rag"]), str(thr))
        check("缩容到 0 被禁用（否则会杀掉正在跑的长任务）",
              str(keda.get("spec", {}).get("minReplicaCount", 0)) != "0",
              f"minReplicaCount={keda.get('spec', {}).get('minReplicaCount')}")

    section("5. 优雅退出：不能比任务执行短")
    for d in deployments:
        name = d["metadata"]["name"]
        spec = d.get("spec", {}).get("template", {}).get("spec", {})
        grace = spec.get("terminationGracePeriodSeconds", 30)
        for c in containers(d):
            args = " ".join(str(a) for a in (c.get("args") or []))
            m = re.search(r"timeout-graceful-shutdown[= ]+(\d+)", args)
            if m:
                soft = int(m.group(1))
                check(f"{name}: terminationGrace > 任务优雅退出时限",
                      grace > soft, f"grace={grace}s > soft={soft}s")
                break
        else:
            check(f"{name}: 显式设置了 terminationGracePeriodSeconds",
                  "terminationGracePeriodSeconds" in spec, f"{grace}s")

    section("6. 不可变字段与常见 apply 失败点")
    for d in docs:
        kind = d.get("kind")
        spec = d.get("spec", {}) or {}
        name = d.get("metadata", {}).get("name")
        if kind == "Deployment":
            sel = (spec.get("selector") or {}).get("matchLabels")
            tpl = (spec.get("template") or {}).get("metadata", {}).get("labels") or {}
            check(f"{kind}/{name}: selector 与 Pod 模板 label 匹配",
                  bool(sel) and all(tpl.get(k) == v for k, v in (sel or {}).items()),
                  f"selector={sel}")
            # 只有 selector 与副本数等字段不可变；Recreate 策略在多副本下会停机
            check(f"{kind}/{name}: 有滚动更新策略或单副本",
                  bool(spec.get("strategy")) or (spec.get("replicas", 1) == 1),
                  str((spec.get("strategy") or {}).get("type", "")))

    section("7. 副本数与中断预算")
    pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    # 不按固定名字找，按 label 找：清单里的 Deployment 叫 rag-kb-api，
    # 写死 "rag-api" 会让这条断言永远失败——断言本身成了噪声。
    api_dep = next(
        (d for d in deployments
         if (d.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {})
         .get("app", "").endswith("api")), None)
    check("找到 api 的 Deployment", api_dep is not None,
          api_dep["metadata"]["name"] if api_dep else "none")
    check("api 至少 2 副本（否则滚动更新必然中断）",
          bool(api_dep) and api_dep["spec"].get("replicas", 1) >= 2,
          str(api_dep["spec"].get("replicas") if api_dep else "none"))
    check("为 api 声明了 PodDisruptionBudget", bool(pdbs),
          ", ".join(p["metadata"]["name"] for p in pdbs))
    # PDB 的 selector 必须真的选中某个 Deployment，否则它是个装饰品：
    # kubectl 不会报错，drain 时也照样把所有 Pod 赶走。
    dep_labels = {
        tuple(sorted(((d.get("spec", {}).get("selector") or {})
                      .get("matchLabels") or {}).items()))
        for d in deployments
    }
    for p in pdbs:
        ml = tuple(sorted((p.get("spec", {}).get("selector") or {})
                          .get("matchLabels", {}).items()))
        check(f"PDB {p['metadata']['name']}: selector 能选中真实 Deployment",
              ml in dep_labels, str(dict(ml)))

    section("8. 配置与密钥分离")
    secrets = [d for d in docs if d.get("kind") == "Secret" and "example" not in d["__file"]]
    check("没有把真实密钥写进清单（只保留 example）", not secrets,
          ", ".join(s["metadata"]["name"] for s in secrets))
    cms = [d for d in docs if d.get("kind") == "ConfigMap"]
    check("使用 ConfigMap 承载非敏感配置", bool(cms), f"{len(cms)} 个")

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
