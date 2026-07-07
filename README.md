# fund-monitor

场外基金择时分析工具,配套 K8s 部署方案,定时把分析结果通过微信推送。

## 项目介绍

日常场外基金(申购赎回按净值成交,不是场内实时交易)投资时,最大的痛点是"不知道现在算贵还是便宜",容易追高。这个项目把几类公开数据源(乐咕乐股指数估值、天天基金净值/持仓/同类排名、东方财富财报)拼起来,对给定的基金代码做多维度分析,而不是只看单一指标。

核心脚本是 [analyze_fund.py](analyze_fund.py),纯 Python + [akshare](https://github.com/akfamily/akshare),可以本地直接跑,也可以包成 HTTP 服务([server.py](server.py))部署到 K8s,供 [OpenClaw](https://github.com/openclaw/openclaw) 定时调用后推送微信。

## 要实现的效果

最终交付的效果是:**微信每天收到一条你关注的基金/板块的信号简报,每周额外收到一条结合近期财经新闻的综合解读**,不用自己每天盯盘算估值。两边分工不同,刻意分开:

- **每天(OpenClaw cron,command 类型,不调用大模型)**:调用 `fund-analyzer` 对指定基金/板块代码跑一遍下面这6个维度的分析,原样把结果推微信——纯数据、零 token 成本,保证稳定不间断
- **每周(OpenClaw cron,agent message 类型,调用大模型)**:在每日数据基础上,再用 `web_search` 查这些板块本周的相关新闻,给出**每个板块的洞见和变化方向判断**——不是简单复述新闻,而是结合数据(估值分位有没有变、净值乖离有没有收敛、同类排名有没有变化)和新闻(政策/资金面/行业事件),判断这个板块最近是在往"更贵/更热"还是"降温/修复"的方向走,有没有出现值得关注的边际变化——"理解新闻+判断方向"需要判断力,所以特意放低频、才用模型,不是每天都让模型对新闻发表意见

`fund-analyzer` 每次分析给出的6个维度:

1. **业绩比较基准估值百分位**——解析基金的业绩比较基准(如"沪深300×70%+..."),取里面能匹配到的宽基指数,算最近10年 PE 百分位,判断当前大盘/风格是贵是便宜
2. **净值乖离率**——最新净值相对自身 MA250 的偏离幅度,判断短期是否涨/跌过头
3. **实际持仓**——最新季度前10大重仓股 + 行业配置(不只看基准,看基金真实在买什么)
4. **同类排名**——近3月/6月/1年/今年以来/成立以来的收益和同类排名,排名越极端(前1-2%或后1-2%)信号意义越大
5. **全市场机构仓位情绪**——股票型基金平均仓位的历史分位,判断机构整体是不是已经很满仓
6. 综合以上给出一句话建议(加仓/维持/减仓/观察)

**已经验证过的用法**:批量对比不同基金/板块(比如 AI算力 vs 银行保险 vs 医药消费),能看出"哪个板块估值高、哪个基金持仓过度集中在哪个主题",辅助判断"要不要现在上车"和"要不要分散一下"。

### 当前进度

- [x] `analyze_fund.py` 分析逻辑,本地验证通过
- [x] `server.py` HTTP 包装,本地验证通过
- [x] `openclaw-k8s/fund-analyzer/` K8s manifests(Kustomize,ArgoCD-ready),本地 `kubectl kustomize` 渲染验证通过
- [x] `openclaw-k8s/manifests/` OpenClaw 官方 K8s manifests,已改 `cron.enabled: true`
- [ ] 实际部署到目标集群
- [ ] OpenClaw 微信 channel 配置(扫码登录)
- [ ] OpenClaw cron job 配置(每日/每周,含下方"每周任务限流设计"待落地)
- [ ] (可选)接入 ArgoCD

### 每周任务的限流设计(待细化)

每周任务要跑 `web_search` + 大模型综合多个板块,存在撞上模型 API 限流(RPM/TPM,均按分钟滚动窗口计算)的风险,尤其 TPM——如果对每个板块都用 `web_fetch` 抓新闻全文,几个板块叠加容易在同一分钟内堆到几万甚至十几万 token。

初步方案(还没实现,后续再调整):

- **不要指望 prompt 里让模型"自己悠着点"**——模型控制不了自己的调用节奏,单次大请求该占多少 TPM 还是占多少,拆分对这个没用
- **靠 OpenClaw 调度器本身把请求错开到不同分钟**,而不是一个 agent turn 里处理所有板块。计划拆成几个错峰的小 cron job,比如:
  - `08:00` 板块组1(AI算力+光模块)
  - `08:05` 板块组2(银行保险)
  - `08:10` 板块组3(医药消费)+ 用 OpenClaw 的 `session:custom-id` 自定义会话读取前两组的历史,汇总成一条完整消息再发微信(避免微信收到好几条零散消息)
- prompt 里优先只用 `web_search` 的摘要判断方向,不逐条 `web_fetch` 抓全文;并限制"每个板块最多搜索1-2次"
- cron job 配 `--fallbacks` 备用模型,真撞上限流时自动切换重试

## 架构

```
              本地开发/命令行
              python3 analyze_fund.py <代码>
                      │
                      ▼
            ┌─────────────────────┐
            │   analyze_fund.py     │  核心分析逻辑,akshare 数据源
            └───────────┬─────────┘
                        │ import
            ┌───────────▼─────────┐
            │      server.py        │  纯标准库 HTTP 包装
            └───────────┬─────────┘
                        │ 打包进 ConfigMap(kustomize configMapGenerator)
   ================== K8s 集群(同一 namespace) ==================
            ┌─────────────────────┐         ┌─────────────────────┐
            │  Deployment           │  HTTP   │  Deployment           │
            │  fund-analyzer        │◀───────│  openclaw             │──▶ 微信
            │  (python:3.12-slim)   │  :8080  │  (官方镜像 + cron)     │
            └─────────────────────┘         └─────────────────────┘
```

两个 Deployment 故意拆开、互不依赖:改分析脚本只重启 `fund-analyzer`,不影响 OpenClaw 的微信会话状态。

## 部署

### 本地开发/调试

```bash
cd fund-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install akshare
python3 analyze_fund.py 600003 010374        # 命令行跑分析
python3 server.py                             # 或者跑成本地 HTTP 服务,默认 :8080
```

### K8s 部署

前提:一个能连到目标集群的 `kubectl`(比如跳板机上配好的 kubeconfig),且已选定 `OPENCLAW_NAMESPACE`。

```bash
# 1. OpenClaw 本体——先创建密钥(至少一个 provider 的 API Key),再部署
export GEMINI_API_KEY="..."          # 或 ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY
export OPENCLAW_NAMESPACE=openclaw   # 按需修改
cd openclaw-k8s
./deploy.sh --create-secret
./deploy.sh

# 2. fund-analyzer——独立部署,不依赖上一步是否成功
cd fund-analyzer
export OPENCLAW_NAMESPACE=openclaw   # 必须和上面一致,两者要在同一 namespace
./deploy.sh
```

验证:

```bash
kubectl port-forward svc/openclaw 18789:18789 -n $OPENCLAW_NAMESPACE   # 打开控制台配置微信登录
kubectl run -it --rm debug --image=curlimages/curl -n $OPENCLAW_NAMESPACE -- \
  curl "http://fund-analyzer:8080/report?codes=017234"                # 验证分析服务
```

### (可选)接入 ArgoCD

`openclaw-k8s/manifests/` 和 `openclaw-k8s/fund-analyzer/` 都是独立的 Kustomize root,可以各建一个 ArgoCD Application 指过去。唯一前提:在 `argocd-cm` 里加一条

```yaml
kustomize.buildOptions: "--load-restrictor LoadRestrictionsNone"
```

因为 `fund-analyzer` 的 `configMapGenerator` 引用了 kustomization 根目录之外的 `analyze_fund.py`/`server.py`,默认会被 Kustomize 的安全限制拦下。

**OpenClaw 的密钥(`openclaw-secrets`)不要交给 ArgoCD 管**,继续保持现在这样手动 `--create-secret` 一次性创建——密钥不应该以任何形式进 Git。

## 配置参数

### 你需要自己填/大概率要改的

| 参数 | 位置 | 说明 |
|---|---|---|
| `OPENCLAW_NAMESPACE` | 部署时的环境变量 | 两个 `deploy.sh` 共用,默认 `openclaw`,必须两边一致 |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | 部署时的环境变量,至少填一个 | 决定 OpenClaw 每周综合解读用哪个大模型;**不要**写进任何文件提交到仓库 |
| 要分析/监控的基金代码 | 调用 `analyze_fund.py`/`server.py` 时的参数,如 `?codes=017234,010392` | 目前没有写死在配置文件里,后续配 OpenClaw cron job 时把代码列表放进 cron 的 command/prompt 里 |
| `pvc.yaml` 里的 `storage: 10Gi` | `openclaw-k8s/manifests/pvc.yaml` | OpenClaw 会话/插件数据的持久化大小,按实际需求调 |
| `deployment.yaml` 里的 `resources.requests/limits` | 两个 Deployment 都有 | 默认值是官方/我给的保守估计,实际跑一段时间后可以按真实占用调整 |
| `startupProbe.failureThreshold`(fund-analyzer) | `fund-analyzer/deployment.yaml` | 每次重启都要重新 `pip install akshare`,如果你的集群网络到 PyPI 比较慢,可以调大这个值 |

### 固定参数,不建议改

| 参数 | 位置 | 为什么别动 |
|---|---|---|
| `image: ghcr.io/openclaw/openclaw:slim` | `manifests/deployment.yaml` | OpenClaw 官方镜像,这套方案的前提是不自建镜像 |
| `image: python:3.12-slim` | `fund-analyzer/deployment.yaml` | 同上,fund-analyzer 也不自建镜像 |
| `"cron": { "enabled": true }` | `manifests/configmap.yaml` | 我们特意从官方默认的 `false` 改成 `true`,关掉的话定时任务全部失效 |
| `gateway.port: 18789` / `bind: loopback` | `manifests/configmap.yaml` | 微信登录、控制台访问都依赖这个端口/绑定方式,改了要连带改 Service |
| `containerPort: 8080` 及对应 `service.yaml` 的 `port/targetPort` | `fund-analyzer/` | 改端口必须 Deployment 和 Service 一起改,且 OpenClaw 调用的 URL 也要跟着改 |
| ConfigMap 名称的哈希后缀(如 `fund-analyzer-scripts-mcdk9h585b`) | Kustomize 自动生成 | 不要手动指定或硬编码这个名字,Deployment 的引用由 Kustomize 自动同步 |
| `strategy: Recreate` | 两个 `deployment.yaml` | 单副本应用,滚动升级(RollingUpdate)在这种场景下没有意义,且可能导致重复的会话/端口冲突 |

## 已知限制

- 窄行业(如"半导体""医药"细分)缺乏可靠的多年历史 PE 时间序列,`analyze_fund.py` 目前只能对业绩比较基准里能匹配到的宽基指数(沪深300、中证500等12个)给出真正的历史百分位;窄行业只能做"当前时点横向对比"(参考同期其他行业贵不贵),不能确认是否处于自身历史低位
- 部分 akshare 数据源(东方财富行业板块列表、雪股个股信息等接口)在这次开发过程中遇到过间歇性失效,`analyze_fund.py` 里每个数据维度都做了独立 try/except,单个数据源失败不影响其余维度输出,但也意味着报告里偶尔会有某一项显示"获取失败"
