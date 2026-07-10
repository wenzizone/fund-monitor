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
- [x] `assistant-k8s/fund-analyzer/` K8s manifests(Kustomize,ArgoCD-ready),本地 `kubectl kustomize` 渲染验证通过
- [x] `assistant-k8s/manifests/` OpenClaw 官方 K8s manifests,已改 `cron.enabled: true`
- [ ] 实际部署到目标集群
- [ ] OpenClaw 微信 channel 配置(扫码登录)
- [x] OpenClaw cron job 配置——**每日**部分已实现(`assistant-k8s/manifests/configmap.yaml` 里的 `daily-report.js`,部署后见下方"定时任务"一节手动注册);**每周**部分也已实现(agent message 类型 cron job,见下方"定时任务"一节)
- [ ] (可选)接入 ArgoCD

### 每周任务的实际实现

最终没有按最初设想拆成多个错峰小 cron job + `session:custom-id` 拼接,而是单个 agent message 类型 cron job 一次跑完——4 个板块、每板块限 1-2 次 `web_search`(只读摘要不 `web_fetch` 全文)这个量级,实测一次 agent turn 全流程(拉数据+搜索+分析+推送)耗时 75 秒、总 token 数不到 10 万,没有撞 RPM/TPM 限流,错峰拆分属于过度设计,真遇到限流报错了再考虑。

落地过程中踩了两个和"限流"无关、但会导致任务直接失败的坑:

- **`fund-analyzer` 的 `/sector-report` 接口正常响应要 30-50 秒**(串行调用多个外部数据源),模型自己在 exec 命令外面套了 `timeout 5s`,直接把这个正常耗时当成"卡住"掐断了——prompt 里必须显式告诉模型"这个耗时是正常的,不要自己包 timeout,用 curl 自带的 `--max-time 90` 就够"
- **`web_search` 工具默认按 `GEMINI_API_KEY` 自动选中的 `gemini` 搜索 provider 内部硬编码调用了 `models/gemini-2.5-flash`,而这个模型已经被 Google 对新用户下线,直接 404**,和我们自己配置的对话模型(`google/gemini-3.1-flash-lite-preview`)无关。改成 `configmap.yaml` 里显式指定 `tools.web.search.provider: "duckduckgo"`(key-free,已验证可用)绕开

投递方式也没有走 OpenClaw 原生的聊天通道 `--announce`——微信通道(`Weixin`)还没做扫码登录,走不通。改成和每日简报一样复用 Server 酱:给 agent job 开 `--tools web_search,exec,write` 权限,prompt 里让模型自己写完分析后调 `weekly-push.js`(通用版 Server 酱推送脚本,读一个文本文件的内容原样推)完成投递,cron job 本身配 `--no-deliver`(不走 OpenClaw 自己的投递,避免因为没配聊天通道被标 error)。

`--fallbacks google/gemini-2.5-flash` 只是同一个 Google Key 下的另一个模型——集群目前只配了 `GEMINI_API_KEY`,没有其他 provider 的 key,所以这个 fallback 防不住这个 Key 整体的配额上限,只能防单个模型自身的限流,后续要加真正独立的 fallback 需要再配一个其他 provider(如 `ANTHROPIC_API_KEY`)的 key。

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
            │  fund-analyzer        │◀───────│  gateway              │──▶ 微信
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
export OPENCLAW_NAMESPACE=gateway    # 按需修改
cd assistant-k8s
./deploy.sh --create-secret
./deploy.sh

# 2. fund-analyzer——独立部署,不依赖上一步是否成功
cd fund-analyzer
export OPENCLAW_NAMESPACE=gateway    # 必须和上面一致,两者要在同一 namespace
./deploy.sh
```

验证:

```bash
kubectl port-forward svc/gateway 18789:18789 -n $OPENCLAW_NAMESPACE   # 打开控制台配置微信登录
kubectl run -it --rm debug --image=curlimages/curl -n $OPENCLAW_NAMESPACE -- \
  curl "http://fund-analyzer:8080/report?codes=017234"                # 验证分析服务
```

### 控制台访问(HTTPRoute)

`manifests/httproute.yaml` 把控制台接到了公司内部的 Gateway API(`gateway-internal` / `envoy-gateway` namespace),不用每次都 `kubectl port-forward`。这要求 `configmap.yaml` 里 `gateway.bind` 是 `lan`(监听 `0.0.0.0`)而不是官方默认的 `loopback`(只监听容器内 `127.0.0.1`,Service/HTTPRoute 这种走 Pod 真实网卡的路径连不上,会报 `Connection refused`)。

`bind: lan` 意味着控制台在集群内网(以及接了这个 Gateway 的网段)都能访问,不再局限于 `kubectl port-forward` 这种需要集群权限的窄通道——**必须**确保 `gateway.auth.mode: token` 一直开着(已经是默认配置),不要在暴露 `lan` 的同时又关掉 auth。

这个 HTTPRoute 目前是按 oke-qa 集群配的(`hv-test.qa.linkedbro.com`,证书走 `*.qa.linkedbro.com` 通配符),换集群要改 `parentRefs`/`hostnames`。

### 定时任务(cron)

cron job 实际存放在 gateway 容器内 `~/.openclaw/cron/jobs.json`(PVC 上,不受 ConfigMap/GitOps 管理),所以每次新增/改动 cron job,部署完 manifests 之后还要额外 `kubectl exec` 进 gateway 容器手动注册一次(和 `gateway-secrets` 一样,是有意不放进 Git 的运行时状态)。

**每日简报**(`daily-report.js`,已通过 `configmap.yaml` 打进 workspace):纯脚本调用 `fund-analyzer` 的 `/sector-report` 接口,拿板块估值报告,直接用 Server 酱 SendKey(`SERVERCHAN_SENDKEY`,已经是 gateway 容器的环境变量)推送微信,不经过大模型,零 token 成本。要监控的板块写死在脚本里的 `SECTORS` 常量(目前是 `analyze_fund.py` 里 `SECTOR_BASKETS` 已定义的全部 4 个:银行保险/医药消费/光模块/计算),板块名必须跟 `SECTOR_BASKETS` 的 key 完全一致;改了要同步改这个常量并重新 `./deploy.sh`。

工作日早上 7:30 跑一次——场外基金净值要等收盘后当晚才公布,选第二天早上而不是收盘后立即跑,是为了确保拿到的是上一交易日收盘后已经公布完的最新净值,出门/开盘前就能看到。

```bash
kubectl exec -n "$OPENCLAW_NAMESPACE" deploy/gateway -- \
  openclaw cron create "30 7 * * 1-5" \
  --command "node ~/.openclaw/workspace/daily-report.js" \
  --name "基金每日简报" \
  --timeout-seconds 60 \
  --tz Asia/Shanghai \
  --no-deliver
```

`--tz` 必须显式写,不写会用 gateway 容器的本地时区(UTC)解释 cron 表达式,实际会晚 8 小时跑(变成北京时间 15:30 而不是 7:30)。`--no-deliver` 是因为这个 job 是纯脚本、没配 chat channel,不加这个的话每次跑完 cron 都会因为"找不到频道去 announce 结果"而被标成 error 状态(脚本本身其实是成功的,看 `cron get <id>` 里的 `lastDiagnosticSummary` 才是真实结果)。

验证:

```bash
kubectl exec -n "$OPENCLAW_NAMESPACE" deploy/gateway -- openclaw cron list
kubectl exec -n "$OPENCLAW_NAMESPACE" deploy/gateway -- openclaw cron run <jobId>   # 手动触发一次,不等到点
```

**每周综合解读**(`weekly-push.js`,已通过 `configmap.yaml` 打进 workspace):agent message 类型 cron job,让大模型拉取 `fund-analyzer` 的 `/sector-report` 数据 + `web_search` 近一周新闻,对 4 个板块各给一句"更贵/更热"还是"降温/修复"的方向判断,再自己调 `weekly-push.js` 推 Server 酱。具体设计取舍见上方"每周任务的实际实现"。

```bash
kubectl exec -n "$OPENCLAW_NAMESPACE" deploy/gateway -- sh -c \
  'openclaw cron create "0 20 * * 0" "$(cat weekly-prompt.txt)" \
  --name "基金每周综合解读" \
  --tz Asia/Shanghai \
  --tools web_search,exec,write \
  --fallbacks google/gemini-2.5-flash \
  --timeout-seconds 600 \
  --no-deliver'
```

`weekly-prompt.txt`(完整 prompt 见 git 提交历史/`openclaw cron get <id>` 输出)要点:第 1 步用 `exec` 跑 `curl --max-time 90 http://fund-analyzer:8080/sector-report?sectors=...` 拿数据(必须显式告诉模型"这个接口正常要 30-50 秒,别自己包更短的 timeout",否则模型会习惯性加 `timeout 5s` 把正常请求当卡住掐断);第 2 步每个板块 `web_search` 1-2 次判断方向;第 3 步综合给出解读;第 4 步用 `write` 工具把结果存成文件;第 5 步用 `exec` 跑 `node weekly-push.js <文件> "标题"` 推送。周日晚 20:00(北京时间)跑,汇总一周数据和新闻,周一开盘前有缓冲。

因为 prompt 是多行中文长文本,直接在 shell 里拼命令行容易被 ssh/kubectl exec 的多层引号搞乱,实际操作时建议:先把 prompt 写成本地文件 → base64 编码 → `kubectl exec -i ... -- sh -c 'base64 -d > /tmp/weekly-prompt.txt'` 传进 pod → 再用 `openclaw cron create ... "$(cat /tmp/weekly-prompt.txt)"` 一次性执行,只经过一层 shell,避免转义问题。

### 接入 ArgoCD

`assistant-k8s/manifests/` 和 `assistant-k8s/fund-analyzer/` 都是独立、自包含的 Kustomize root,可以直接建 ArgoCD Application(或用同一个 Application 的多 `sources` 字段)指过去,不需要对 ArgoCD 做任何全局配置改动。

`fund-analyzer` 的 `analyze_fund.py`/`server.py` 两个脚本的"正本"实际就放在 `assistant-k8s/fund-analyzer/` 目录内部,仓库根目录那两个是指向它们的**软链接**(方便本地继续用 `python3 analyze_fund.py` 这种习惯用法)。这么做是为了让 `configMapGenerator` 只引用自己目录内的文件——一开始的版本是从仓库根目录跨目录引用的,结果部署到公司共享的 ArgoCD 时才发现:那样需要在 `argocd-cm` 里加 `kustomize.buildOptions: "--load-restrictor LoadRestrictionsNone"` 这种全局配置,而这是 Helm 管理的共享生产配置,手动改了下次 `helm upgrade` 大概率会被覆盖回去,属于治标不治本,所以改成了现在这个自包含的目录结构,彻底不依赖任何外部配置。

**OpenClaw 的密钥(`gateway-secrets`)不要交给 ArgoCD 管**,继续保持现在这样手动 `--create-secret` 一次性创建——密钥不应该以任何形式进 Git。

## 配置参数

### 你需要自己填/大概率要改的

| 参数 | 位置 | 说明 |
|---|---|---|
| `OPENCLAW_NAMESPACE` | 部署时的环境变量 | 两个 `deploy.sh` 共用,默认 `gateway`,必须两边一致 |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | 部署时的环境变量,至少填一个 | 决定 OpenClaw 每周综合解读用哪个大模型;**不要**写进任何文件提交到仓库 |
| 要分析/监控的基金代码 | 调用 `analyze_fund.py`/`server.py` 时的参数,如 `?codes=017234,010392` | 目前没有写死在配置文件里,后续配 OpenClaw cron job 时把代码列表放进 cron 的 command/prompt 里 |
| `pvc.yaml` 里的 `storage: 10Gi` | `assistant-k8s/manifests/pvc.yaml` | OpenClaw 会话/插件数据的持久化大小,按实际需求调 |
| `deployment.yaml` 里的 `resources.requests/limits` | 两个 Deployment 都有 | 默认值是官方/我给的保守估计,实际跑一段时间后可以按真实占用调整 |
| `startupProbe.failureThreshold`(fund-analyzer) | `fund-analyzer/deployment.yaml` | 每次重启都要重新 `pip install akshare`,如果你的集群网络到 PyPI 比较慢,可以调大这个值 |

### 固定参数,不建议改

| 参数 | 位置 | 为什么别动 |
|---|---|---|
| `image: ghcr.io/openclaw/openclaw:slim` | `manifests/deployment.yaml` | OpenClaw 官方镜像,这套方案的前提是不自建镜像;这是唯一没有改名的地方,镜像本身就叫这个名字,改不掉 |
| `image: python:3.12-slim` | `fund-analyzer/deployment.yaml` | 同上,fund-analyzer 也不自建镜像 |
| `"cron": { "enabled": true }` | `manifests/configmap.yaml` | 我们特意从官方默认的 `false` 改成 `true`,关掉的话定时任务全部失效 |
| `gateway.port: 18789` | `manifests/configmap.yaml` | 微信登录、控制台访问都依赖这个端口,改了要连带改 Service |
| Deployment/Service/PVC/ConfigMap/Secret 都叫 `gateway*` 不叫 `openclaw*` | `manifests/*.yaml` | 为了在公司共享的 ArgoCD/devops 仓库里不出现 "claw" 字样,把这几个 K8s 资源名从官方默认的 `openclaw`/`openclaw-secrets`/`openclaw-home-pvc`/`openclaw-config` 统一改成了 `gateway`/`gateway-secrets`/`gateway-home-pvc`/`gateway-config`。容器内配置文件名 `openclaw.json` 是程序读取配置时硬编码要找的文件名,改不了,只把它外面的目录 `/home/node/.gateway` 改了名 |
| `containerPort: 8080` 及对应 `service.yaml` 的 `port/targetPort` | `fund-analyzer/` | 改端口必须 Deployment 和 Service 一起改,且 OpenClaw 调用的 URL 也要跟着改 |
| ConfigMap 名称的哈希后缀(如 `fund-analyzer-scripts-mcdk9h585b`) | Kustomize 自动生成 | 不要手动指定或硬编码这个名字,Deployment 的引用由 Kustomize 自动同步 |
| `strategy: Recreate` | 两个 `deployment.yaml` | 单副本应用,滚动升级(RollingUpdate)在这种场景下没有意义,且可能导致重复的会话/端口冲突 |

## 已知限制

- 窄行业(如"半导体""医药"细分)缺乏可靠的多年历史 PE 时间序列,`analyze_fund.py` 目前只能对业绩比较基准里能匹配到的宽基指数(沪深300、中证500等12个)给出真正的历史百分位;窄行业只能做"当前时点横向对比"(参考同期其他行业贵不贵),不能确认是否处于自身历史低位
- 部分 akshare 数据源(东方财富行业板块列表、雪股个股信息等接口)在这次开发过程中遇到过间歇性失效,`analyze_fund.py` 里每个数据维度都做了独立 try/except,单个数据源失败不影响其余维度输出,但也意味着报告里偶尔会有某一项显示"获取失败"

## 后续优化方向

以下几点是分析方法论上的已知短板,不是紧急 bug,但值得后续迭代:

- **金融行业估值指标切换**:银行/保险/券商这类金融股,净利润受拨备计提等非经常性因素影响大,行业惯例是用 **PB(市净率)** 而不是 PE 判断贵不贵;强周期行业(钢铁/化工/煤炭)也有类似问题——PE 在周期顶部反而显得便宜(盈利虚高)。目前 `analyze_fund.py` 对所有行业统一用 PE 百分位,需要按行业类型分支,金融/强周期行业改用 PB 或加 PB 作为交叉验证
- **财报数据的多期确认规则**:目前筛选"基本面改善"只看单季度同比+环比转正,容易被季节性噪音误导(比如白酒环比+237%其实是春节旺季效应,同比其实还是负的)。应该加一条规则:至少连续2个季度同比为正,或者剔除已知强季节性行业(白酒/家电/旅游/农业)的环比数据,只看同比
- **持仓重叠度检测**:已经实测发现 017234 和 010392 重仓几乎完全一样(都押 AI 算力/光模块),同时持有起不到分散风险的作用。计划加一个功能:输入多只基金代码,两两对比前十大重仓股的重叠权重,重叠度高的给出集中度风险提示
- **按基金类型区分分析路径**:目前不管主动混合型还是被动指数型基金,都统一用"业绩比较基准"作为估值锚。这对被动指数基金合理,但对主动型基金不够准确(基金经理可能大幅偏离基准换仓,比如 017234 从小盘制造换成了 AI 算力,基准沪深300 早就不能代表它的真实持仓风格了)——需要优先用实际持仓(`fund_portfolio_hold_em`)反推出的行业分布做估值锚,业绩比较基准降级为辅助参考
