# K8s 部署清单

对应验收标准 **AC-P4-07：K8s 集群部署，支持弹性扩容**。

## 目录结构

| 文件 | 作用 | 是否必须 |
|------|------|----------|
| `00-namespace-config.yaml` | 命名空间、ConfigMap、外部依赖接入 | 必须 |
| `secret.example.yaml` | 密钥**清单模板**（不提交真实值） | 必须 |
| `10-api.yaml` | api Deployment + Service + HPA | 必须 |
| `20-worker.yaml` | worker Deployment（按队列积压扩容，见 30） | 必须 |
| `30-worker-keda.yaml` | KEDA 队列积压扩容（推荐） | 推荐 |
| `40-alert.yaml` | 告警评估器（**单副本**） | 必须 |
| `50-ingress.yaml` | 对外入口、上传体积与超时 | 必须 |
| `60-backup-cronjob.yaml` | 对象存储原件每日备份 | 必须 |

## 部署顺序

```bash
# 1. 命名空间与配置
kubectl apply -f k8s/00-namespace-config.yaml

# 2. 密钥（推荐用 External Secrets，不要 apply 示例文件）
kubectl create secret generic rag-kb-secret -n rag-kb \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql://..." \
  --from-literal=REDIS_URL="redis://..." \
  --from-literal=REDIS_ADDRESS="redis:6379" \
  --from-literal=MINIO_ACCESS_KEY="..." \
  --from-literal=MINIO_SECRET_KEY="..." \
  --from-literal=BOOTSTRAP_ADMIN_PASSWORD="..." \
  --from-literal=METRICS_TOKEN="$(openssl rand -hex 16)" \
  --from-literal=EMBED_API_KEY="..." \
  --from-literal=CHAT_API_KEY="..."

# 3. 有状态依赖（托管服务则跳过，只改 00 里的 ExternalName）
#    自建时见下一节

# 4. 应用
kubectl apply -f k8s/30-worker-keda.yaml   # 若有 KEDA
kubectl apply -f k8s/10-api.yaml -f k8s/20-worker.yaml -f k8s/40-alert.yaml
kubectl apply -f k8s/50-ingress.yaml -f k8s/60-backup-cronjob.yaml

# 5. 验证
kubectl -n rag-kb rollout status deploy/rag-kb-api
kubectl -n rag-kb get pods,hpa
```

## 三个"看起来能用、实际会出事"的配置

### 1. 三个副本 = 三次告警

api 副本的 `ALERT_EVAL_IN_PROCESS` 必须是 `false`，告警评估由 `40-alert.yaml` 那个
**单副本** Deployment 承担。两个条件缺一不可：

- api 侧忘了关 → N 个副本各评估一遍，同一次故障报 N 次
- alert 侧副本数 > 1 → 同上

`40-alert.yaml` 用的是 `strategy: Recreate` 而不是 RollingUpdate，就是为了避免
"新旧 Pod 同时存在的几秒内各发一遍"。

### 2. liveness 查依赖 = 一次依赖故障放大成全站重启

三个探针语义必须分开：

| 探针 | 路径 | 依赖挂掉时 |
|------|------|-----------|
| startup | `/api/v1/ready` | 启动期兜底，给足 120 秒 |
| liveness | `/api/v1/health` | **仍返回 200**，Pod 不重启 |
| readiness | `/api/v1/ready` | 返回 503，Pod 摘出 Service |

如果把 liveness 也指向 `/ready`，PostgreSQL 抖动 10 秒 → 所有副本同时被判死 →
全部重启 → 重启期间无人服务 → 用户看到的是"整个系统崩了"，而根因只是一次数据库抖动。

**另一个坑**：`/api/v1/stats` 不在免鉴权白名单里，用它当探针会一直 401，
Pod 无限重启，日志里只有一串 401，看起来像"鉴权配错了"。

worker 没有 HTTP 端口，两个探针都用 `celery inspect ping`（exec 探针）。
它额外配了 `startupProbe`：冷启动要 import 整个服务栈并建 Redis/PG 连接，
节点 IO 抖动时容易超过 `initialDelaySeconds`，没有 startupProbe 就是"启动中被判死 →
重来 → CrashLoopBackOff"，看起来像镜像坏了。

### 4. 没有 PDB 时，集群升级 = 全站中断一次

PDB 管的是**自愿中断**（`kubectl drain`、节点缩容、集群升级）。不配的时候，
排空会按顺序把 Pod 全部驱逐而**不等新 Pod 起来**——调度器没有义务在乎你的可用性。
表现是"升级期间知识库整体 502，升级完自动恢复"，而且不会触发任何告警
（服务确实在跑，只是没有副本）。

两个 PDB 都用 `minAvailable: 1` 而不是百分比：副本数在 2~10 之间变化时，
百分比会带来进位取整的意外（`50%` 在 2 副本下等价于 1，在 3 副本下也是 1，
看起来一样但语义完全不同）。代价是排空变慢，这是有意的取舍。

### 5. worker 的滚动更新要反过来配置

api 用 `maxUnavailable: 0 / maxSurge: 1`（先起新的再停旧的，保可用性）。
worker 必须反过来：`maxSurge: 0 / maxUnavailable: 1`。

原因是 worker 的任务动辄跑十几分钟，`maxSurge: 1` 会让发布期间**同时多出一批
worker 抢同一个队列**，而旧的还要跑完才退——短时间内副本数翻倍，Redis 连接数和
节点内存一起被顶上去。选择"先停一个再起一个"：短暂少一个消费者，任务留在队列里不丢
（`acks_late` + 队列持久化），代价只是吞吐略降，比资源翻倍安全。

### 3. 上传体积要在网关放开

三层都有默认上限，任何一层没放开，大文件就死在那个关口，而且**应用日志里什么都没有**：

| 层 | 配置 | 默认值 |
|----|------|--------|| Ingress | `proxy-body-size` | 1MB |
| 上游 Nginx | `client_max_body_size` | 1MB |
| 应用 | `MAX_ASYNC_UPLOAD_MB` | 4096MB（本项目） |

失败表现是 `413 Request Entity Too Large`，但请求根本没到应用。运维第一反应
通常是"后端限流拦了"，去改 `MAX_ASYNC_UPLOAD_MB`，改了也没用。

## 弹性扩容：两个组件用两套判据

```
api     → HPA（CPU 70%）        交互式负载，按资源利用率扩
worker  → KEDA（队列长度）      批处理负载，按积压任务数扩
```

**为什么 worker 不能用 CPU 判据**：worker 处理大文档时会长时间等对象存储下载、
等 embedding 返回，这些"等待期"里 CPU 接近 0。HPA 会判定"不需要那么多副本"开始缩容，
**正在执行的任务被中断**——虽然有 acks_late 会重投，但意味着前面的解析全部白做。
表现为"大文档永远处理不完"，而指标上看 CPU 一直不高。

队列长度不会骗人：`redis list 长度 / 单 worker 吞吐` 直接等于需要几个副本。

## 有状态组件：优先托管

清单里用 `ExternalName` Service 把托管实例接进集群（见 `00-namespace-config.yaml`），
业务配置只认 `postgres` / `redis` / `minio` / `qdrant` 这几个名字。

这样做的实际好处：日后把托管换成自建 StatefulSet，**业务配置一个字都不用改**。

自建时的最低要求（清单未包含，按需补充）：

| 组件 | 必须做到 | 漏了会怎样 |
|------|---------|-----------|
| PostgreSQL | 主从 + 定时全量备份 + PITR | 单点，宕机即全站不可用 |
| Redis | 持久化（AOF）+ 哨兵或集群 | 重启后 JWT 黑名单、OIDC state 全丢，用户被踢出 |
| MinIO | 至少 4 节点纠删码 | 单盘故障即丢原件 |
| Qdrant | 副本数 ≥ 2 | 向量库单点，检索全断 |

## 扩容时的容量估算

单副本实测基线（2C / 2Gi 限制）：

| 场景 | 单副本吞吐 | 说明 |
|------|-----------|------|
| 检索 + 问答 | ~15 QPS | 不含 LLM 生成时间 |
| 小文档入库（5MB） | ~1.5 份/分钟 | 含 embedding |
| 大文档入库（270MB） | ~1 份 / 3 分钟 | 流式，内存峰值 34MB |

按上表反推副本数：**并发用户数 ≥ 15 时 api 至少要 2 副本**；
**每日新增文档 > 200 份时 worker 至少要 3 副本**。

## 已知限制

1. **HPA 缩容窗口 300 秒是刻意的保守**。缩容太快会导致"扩了又缩"抖动，
   而每次缩容都可能打断最长 120 秒的问答请求。代价是低峰期资源利用率偏低。
2. **worker 的 `--concurrency=2` 不宜调大**。每个任务都可能解析 GB 级文档，
   并发 8 在 4Gi 内存下会互相挤爆。需要更高吞吐时加副本，顺带获得故障隔离。
3. **备份默认写到 PVC**，这是为了不绑定云厂商。真正的异地备份需要把
   `60-backup-cronjob.yaml` 的 volume 换成对象存储 CSI，或在脚本里 rclone 到异地桶。
   备份到**同一个 MinIO** 是伪备份——桶被误删时备份和目标一起消失。
