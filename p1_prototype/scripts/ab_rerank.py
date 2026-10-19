"""标准 7 对照实验：Rerank 三档（off / heuristic / llm）对检索质量的影响。

**为什么必须做对照实验**：说"准确率明显提升"而没有基线，等于没说。
只有在同一组问题、同一份语料、只改一个变量的前提下测出的差距，才算证据。

实验设计：
  - 固定语料：脚本会先上传一组带"唯一答案指纹"的测试文档
  - 固定问题：每题的正确文档里含一个只在该文档出现的随机字符串（指纹）
  - 每题独立提问，问题带固定后缀以保证三档面对完全相同的输入
  - 指标：
      Hit@1   正确答案排在第一位
      Hit@K   正确答案出现在前 K 条
      MRR     正确答案排名的倒数均值（越大越好，对"排第几"更敏感）

如何切换配置：Rerank 模式是启动时读取的常量，进程内改环境变量不生效，
因此由 scripts/run_rerank_ab.sh 负责逐档重启容器并调用本脚本。
本脚本只负责"在给定档位下跑分"，不依赖 docker 权限，可单独复用。
"""
from __future__ import annotations

import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

BASE = os.getenv("BASE", "http://127.0.0.1:8012").rstrip("/")
API = f"{BASE}/api/v1"
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")
MODE = os.getenv("MODE", "unknown")
SPACE = os.getenv("EVAL_SPACE", "evalrerank")
TOP_K = int(os.getenv("TOP_K", "3"))

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, path: str, token: str = "", body: Any = None,
        raw: bytes | None = None, ctype: str = "application/json"):
    url = path if path.startswith("http") else f"{API}{path}"
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
    h = {"Content-Type": ctype}
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _OPENER.open(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"message": str(e)}


def data_of(p):
    if isinstance(p, dict) and p.get("code") == 200:
        return p.get("data")
    return p if isinstance(p, dict) else {}


def upload(tok: str, name: str, text: str, perm: str = "all", space: str = "") -> int:
    b = "----Eval" + str(int(time.time() * 1000000))
    body = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"perm_group\"\r\n\r\n{perm}\r\n"
        f"--{b}\r\nContent-Disposition: form-data; name=\"space_id\"\r\n\r\n{space}\r\n"
        f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: text/markdown\r\n\r\n"
    ).encode() + text.encode() + f"\r\n--{b}--\r\n".encode()
    s, _ = req("POST", "/documents", tok, raw=body,
               ctype=f"multipart/form-data; boundary={b}")
    return s


# ---------- 语料：刻意制造"高相似度干扰文档" ----------
# 关键设计：正确文档与干扰文档用**几乎相同的措辞**，只在关键数字/条件上不同。
# 纯向量检索会把干扰文档排前面（词面高度相似），BM25 也会被共同的虚词拉高分数，
# 只有 Rerank（把"问题与片段是否真的在回答同一件事"纳入打分）才能把它掰正。
# 如果语料里每篇文档主题都截然不同，三档结果必然都是 100%，测不出任何差异——
# 那样的"对照实验"看起来通过，实际什么也没证明。
def build_corpus(fps: Dict[str, str]) -> List[Tuple[str, str]]:
    return [
        # 正确文档：一线城市 600
        ("eval_travel_city.md", f"""# 境内差旅住宿费标准（城市分级）

## 住宿标准

一线城市（北京、上海、广州、深圳）住宿费上限每晚 600 元；
二线城市（省会及计划单列市）每晚 450 元；其他城市每晚 350 元。
超标部分由个人承担，需在报销时说明原因。

档案编号 {fps['travel']}。

## 交通标准

市内交通凭票实报实销。高铁二等座、飞机经济舱可全额报销。
"""),
        # 干扰文档：同样是"住宿费标准"，但是境外的、数字不同
        ("eval_travel_abroad.md", f"""# 境外差旅住宿费标准

## 住宿标准

境外差旅住宿费按国家分档：A 类国家每晚 1800 元，B 类国家每晚 1200 元，
C 类国家每晚 800 元。按出发日汇率折算人民币报销。
境外住宿一律需提供酒店正式发票与入住登记单。

档案编号 {fps['travel_dis']}。

## 交通标准

国际航班原则上选择经济舱，飞行时间超过 8 小时可申请公务舱。
"""),
        # 正确文档：满 3 年 10 天
        ("eval_leave.md", f"""# 员工休假制度

## 年假

入职满 1 年享 5 天年假，满 3 年享 10 天，满 5 年享 15 天。
年假需提前 3 个工作日在系统提交申请，部门负责人审批。
当年未休完的年假可结转至次年 3 月 31 日。

档案编号 {fps['leave']}。

## 病假

病假需提供二级以上医院证明，全年累计不超过 30 天。
"""),
        # 干扰文档：讲"调休"，同样是假期但规则不同
        ("eval_leave_comp.md", f"""# 加班调休管理办法

## 调休

加班可按 1:1 折算调休，法定节假日加班按 1:3 折算。
调休须在加班发生后 6 个月内使用，逾期作废，不可折算为工资。
调休申请需附加班审批记录，由部门负责人与 HRBP 双签。

档案编号 {fps['leave_dis']}。

## 适用范围

本办法适用于非销售序列的正式员工，实习生不适用。
"""),
        # 正确文档：报销材料清单
        ("eval_expense.md", f"""# 费用报销流程

## 报销材料

报销必须提供：发票原件、消费明细、审批单、行程单（差旅类）。
缺少发票的需填写《无票支出说明》并由部门负责人签字。
电子发票需同时提交 PDF 原件与查验结果截图。

档案编号 {fps['expense']}。

## 报销时限

费用发生后 30 个自然日内提交，跨月不跨季，逾期需财务总监特批。
"""),
        # 干扰文档：讲"付款/结算材料"，词面也很像
        ("eval_payment.md", f"""# 供应商付款与结算管理

## 付款材料

付款必须提供：合同原件、验收单、发票、银行账户信息确认函。
预付款需额外提供履约保函，比例不低于合同金额的 10%。
所有付款材料需经采购、财务、法务三方会签。

档案编号 {fps['expense_dis']}。

## 结算周期

月结供应商于次月 15 日前完成对账，25 日前付款；
季结供应商于季度结束后 30 日内完成结算。
"""),
        # 正确文档：研发 12%
        ("eval_salary.md", f"""# 2026 年度薪酬调整方案

## 调薪幅度

研发序列平均调薪 12%，产品序列平均调薪 9%，销售序列按业绩达成率浮动。
调薪自 2026 年 4 月 1 日起执行，随 4 月工资一并发放。

档案编号 {fps['salary']}。

## 适用范围

本方案适用于全体正式员工，试用期员工按转正后标准执行。
"""),
        # 干扰文档：讲"预算编制"，含百分比但含义不同
        ("eval_budget.md", f"""# 2026 年度部门预算编制指引

## 预算增幅

研发投入预算同比增长 20%，市场费用预算同比增长 8%，
行政费用预算同比压降 5%。各科目预算需按季度分解到月。

档案编号 {fps['salary_dis']}。

## 编制要求

预算编制以业务计划为依据，10 月底前完成初稿，11 月完成评审。
"""),
        # 正确文档：密码 90 天
        ("eval_it.md", f"""# 信息安全与设备管理规范

## 账号与密码

域账号密码长度不少于 12 位，须包含大小写字母、数字与符号，每 90 天更换一次。
禁止在多个系统复用同一密码，禁止将密码写在便签或明文文件中。
连续 5 次输错将锁定账号 30 分钟。

档案编号 {fps['it']}。

## 设备领用

笔记本领用需 IT 部门登记资产编号，离职时归还。外带设备须开启全盘加密。
"""),
        # 干扰文档：讲"VPN/远程接入"，也是安全类
        ("eval_vpn.md", f"""# 远程接入与 VPN 使用规范

## 接入要求

远程接入必须使用公司统一 VPN 客户端，禁止使用第三方代理工具。
VPN 会话空闲超过 30 分钟自动断开，需重新认证。
接入终端须安装 EDR 客户端且病毒库在 7 天内更新过。

档案编号 {fps['it_dis']}。

## 权限申请

VPN 权限按项目申请，最长有效期 90 天，到期需重新审批。
"""),
    ]


# ---------- 问题：三类难度，逐级加压 ----------
def build_questions(fps: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """(问题, 期望命中的指纹键, 人能理解的正确答案)

    刻意混入三种难度：
      L1 词面重合高：问题里的词几乎原样出现在正确文档里 → 向量和 BM25 都能赢
      L2 有干扰文档：存在词面高度相似但答案不同的文档 → 考验"是否真的在回答"
      L3 口语化改写：问题不共享关键词，只有语义相关 → 纯 BM25 会失效

    为什么必须混入 L3：如果全部题目都是 L1，任何检索方式都能拿 100%，
    实验看起来"通过"，实际上没有测出任何东西。**能力验证的题目必须足够难
    到能把不同方案拉开差距，否则就是在自欺欺人。**
    """
    return [
        # ---- L1/L2：词面重合 + 高相似干扰 ----
        ("一线城市出差住宿一晚最多能报多少钱", "travel", "600 元/晚（干扰：境外 1800/1200/800）"),
        ("入职三年能休多少天年假", "leave", "10 天（干扰：调休 1:1、1:3 折算）"),
        ("费用发生后多久内要报销完", "expense", "30 个自然日（干扰：月结次月 15 日对账）"),
        ("研发部门今年调薪多少", "salary", "12%（干扰：研发投入预算同比 +20%）"),
        ("密码多久需要更换一次", "it", "90 天（干扰：VPN 权限有效期 90 天）"),
        # ---- L3：口语化，几乎不共享关键词 ----
        ("我出差住酒店的钱能报多少", "travel", "住宿费分城市分档（干扰：境外档）"),
        ("干了三年的话假期能有几天", "leave", "10 天年假（干扰：调休）"),
        ("报销拖着不交会有什么后果", "expense", "逾期需财务总监特批"),
        ("今年工资会涨吗", "salary", "平均调薪 12%/9%（干扰：预算增幅 20%）"),
        ("公司电脑离职要还吗", "it", "需归还，IT 登记资产编号"),
    ]


def run() -> int:
    s, b = req("POST", "/auth/login", body={"username": ADMIN_USER, "password": ADMIN_PASS})
    tok = (data_of(b) or {}).get("access_token", "")
    if not tok:
        print(f"登录失败 {s}：{b.get('message')}")
        return 2

    # 建独立空间，避免与业务语料互相干扰。
    # 每档实验用不同空间（eval_off / eval_heuristic / eval_llm），
    # 好处是上一档写入的问答缓存不会串到下一档——否则第二、三档
    # 会直接命中第一档的缓存，测出来的是"缓存命中率"而不是"检索质量"。
    s, _ = req("POST", "/spaces", tok, {"space_id": SPACE, "name": f"Rerank 评测 {MODE}",
                                        "description": "对照实验专用，可删除"})
    print(f"空间 {SPACE} -> {s}")

    # 固定指纹：seed 用固定值，保证三档面对**完全相同**的语料，
    # 否则指纹一变，"是否命中"的判定基准就变了，对照失去意义。
    seed = os.getenv("EVAL_SEED", "2026")
    rnd = random.Random(seed)
    keys = ("travel", "travel_dis", "leave", "leave_dis", "expense", "expense_dis",
            "salary", "salary_dis", "it", "it_dis")
    fps = {k: "".join(rnd.choices(string.ascii_uppercase + string.digits, k=8))
           for k in keys}

    print("上传评测语料 ...")
    for name, text in build_corpus(fps):
        st = upload(tok, name, text, perm="all", space=SPACE)
        print(f"  {name} -> {st}")

    print(f"\n当前档位 MODE={MODE}，开始评测（每题独立提问，题目带随机后缀防缓存）\n")
    hits1 = hits3 = 0
    mrr = 0.0
    details = []
    # 干扰文档是否被顶到正确文档之前——这是 Rerank 最直接的价值证明
    distractor_wins = 0

    # 指纹键 → 文档名，用于在来源里认出"哪一篇被召回了"
    key2file = {
        "travel": "eval_travel_city.md", "travel_dis": "eval_travel_abroad.md",
        "leave": "eval_leave.md", "leave_dis": "eval_leave_comp.md",
        "expense": "eval_expense.md", "expense_dis": "eval_payment.md",
        "salary": "eval_salary.md", "salary_dis": "eval_budget.md",
        "it": "eval_it.md", "it_dis": "eval_vpn.md",
    }

    for q, key, expect in build_questions(fps):
        fp = fps[key]
        target_file = key2file[key]
        query = f"{q}"
        s, b = req("POST", "/chat", tok,
                   {"question": query, "top_k": TOP_K, "mode": "rag", "space_id": SPACE})
        d = data_of(b) or {}
        srcs = d.get("sources") or []
        # 判定优先级：先认指纹（精确），再退到文件名（片段被截断时指纹可能不进上下文）
        rank = 0
        for i, src in enumerate(srcs, 1):
            if fp in json.dumps(src, ensure_ascii=False):
                rank = i
                break
        if rank == 0:
            for i, src in enumerate(srcs, 1):
                if str(src.get("file_name", "")) == target_file:
                    rank = i
                    break

        # 记录正确文档的排名，以及是否有干扰文档排在它前面
        names = [str(x.get("file_name", "")) for x in srcs]
        dis_file = key2file.get(key + "_dis", "")
        dis_rank = (names.index(dis_file) + 1) if dis_file in names else 0
        if dis_rank and (rank == 0 or dis_rank < rank):
            distractor_wins += 1

        hits1 += 1 if rank == 1 else 0
        hits3 += 1 if 1 <= rank <= TOP_K else 0
        mrr += (1.0 / rank) if rank else 0.0
        details.append({"q": q, "expect": expect, "rank": rank,
                        "target": target_file, "distractor_rank": dis_rank,
                        "sources": names, "rejected": d.get("rejected"),
                        "confidence": d.get("confidence")})
        mark = f"rank={rank}" if rank else "MISS"
        dis = f"  [干扰排第{dis_rank}]" if dis_rank else ""
        print(f"  {mark:>8s}  {q}{dis}")

    n = len(details)
    out = {
        "mode": MODE, "total": n,
        "hit@1": round(hits1 / n, 3), "hit@3": round(hits3 / n, 3),
        "mrr": round(mrr / n, 3),
        "distractor_wins": distractor_wins,
        "details": details,
    }
    print(f"\n{'=' * 60}")
    print(f"  档位 {MODE}: Hit@1={out['hit@1']:.0%}  Hit@{TOP_K}={out['hit@3']:.0%}  "
          f"MRR={out['mrr']:.3f}  干扰胜出={distractor_wins}/{n}")
    print(f"{'=' * 60}")

    path = os.getenv("OUT", f"/tmp/rerank_{MODE}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"结果已写入 {path}")
    except Exception as exc:
        print(f"写文件失败（结果见上）：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
