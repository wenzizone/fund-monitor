#!/usr/bin/env python3
"""本地实时看盘小工具:轮询公开行情接口,展示几只 ETF/期货的实时价格和当日分时折线图。

纯本地用,不部署到 K8s、不进 fund-analyzer 那套服务——这里监控的是这几只 OTC
联接基金(见 ../assistant-k8s/fund-analyzer/analyze_fund.py 的 FUNDS)各自对应的
交易所场内 ETF,场内 ETF 有真正的盘中实时报价和分时数据,场外联接基金本身只有
每日一次的净值,没有"实时"这回事。另外加了几只不对应任何场外基金、纯粹是看盘
需要的品种(黄金 ETF、沪金主连期货、云计算 ETF)。

数据源:
- A股场内 ETF: 东方财富(push2/push2his)为主,腾讯(gtimg.cn/ifzq.gtimg.cn)为
  备——实测东方财富这个免费接口会成串抽风(6只同时502,不是单只随机失败),
  重试解决不了根本问题,所以东财失败时自动切到腾讯这套完全独立的行情基础
  设施,两边同时故障的概率低得多。
- 沪金主连(元/克,银行App那种"人民币实时金价"图对应的品种): 新浪期货接口
  (hq.sinajs.cn 报价 / stock2.finance.sina.com.cn 分时),有真正的分时历史
  (含夜盘)。

都免费、不需要 key,也都没有稳定的浏览器端 CORS 支持,所以搞了这个小 server
做服务端代理——浏览器只跟 localhost 打交道,没有跨域问题。

用法:
    python3 server.py
    浏览器打开 http://localhost:8899
"""
import datetime
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import os

PORT = 8899

# 展示顺序就是这个列表书写的顺序,想调整卡片顺序/增删品种直接改这里。
# kind 默认是 "etf"(A股场内 ETF,market: 1=上交所/0=深交所);"futures" 是走
# 新浪期货接口的品种(目前只有沪金主连)。
#
# 黄金相关的(ETF + 期货)放最前面。跟 fund-analyzer 监控的 6 只 OTC 联接
# 基金(000950/001594/001344/000248/015876/018103)对应关系,逐个查证过
# 全称/跟踪指数完全匹配,不是凭基金公司/主题猜的:
#   000950 易方达沪深300非银ETF联接A       -> 512070 易方达沪深300非银行金融ETF
#   001594 天弘中证银行指数A               -> 515290 天弘中证银行ETF
#   001344 易方达沪深300医药ETF联接         -> 512010 易方达沪深300医药ETF
#   000248 汇添富中证主要消费ETF联接        -> 159928 汇添富中证主要消费ETF
#   015876 富国中证消费电子主题ETF联接A     -> 561100 富国中证消费电子主题ETF
#   018103 易方达中证港股通消费主题ETF联接A -> 513070 易方达中证港股通消费主题ETF
# 下面这几只不对应任何场外基金,纯粹是看盘需要加的:
#   518880 黄金ETF(华安)      场内规模最大的黄金 ETF
#   AUM    沪金主连(元/克)    上期所黄金期货主力连续合约,银行App里那种"人民币
#                            实时金价"图对应的是这个,不是国际现货金价(美元/
#                            盎司)——之前加过一版美元现货(hf_XAU),腾讯的
#                            历史分时接口不支持这类外盘代码,只能前端攒点凑
#                            合画图,体验明显不如有真实历史的沪金主连,已经
#                            拿掉了。
#   516510 云计算ETF(易方达)  "算力"目前没有对应的场内 ETF 产品(搜过东方
#                            财富的基金全量列表,基金名称里没有"算力"二字;
#                            2023年有多家基金公司申报过算力/算力基础设施
#                            主题ETF,但没找到已上市、名称含"算力"的产品),
#                            云计算 ETF 是目前 A股市场里最接近"算力"主题的
#                            真实存在的产品(数据中心、云服务器等算力基础
#                            设施是其重仓方向),经 push2/gtimg 接口核实过
#                            名称是"云计算ETF易方达"。
INSTRUMENTS = [
    {"code": "518880", "market": 1, "name": "黄金ETF(华安)"},
    {
        "code": "AUM",
        "name": "沪金主连(元/克)",
        "kind": "futures",
        "sina_quote_symbol": "nf_AU0",
        "sina_trend_symbol": "AU0",
    },
    {"code": "512070", "market": 1, "name": "非银ETF(易方达)"},
    {"code": "515290", "market": 1, "name": "银行ETF(天弘)"},
    {"code": "512010", "market": 1, "name": "医药ETF(易方达)"},
    {"code": "159928", "market": 0, "name": "主要消费ETF(汇添富)"},
    {"code": "561100", "market": 1, "name": "消费电子ETF(富国)"},
    {"code": "513070", "market": 1, "name": "港股通消费ETF(易方达)"},
    {"code": "516510", "market": 1, "name": "云计算ETF(易方达)"},
]


def _fetch_text(url: str, retries: int = 3, encoding: str = "utf-8", headers: dict | None = None) -> str:
    # 东方财富/腾讯/新浪的接口偶发抽风,而且实测是成串地失败(短时间内所有
    # 请求都失败,过一会儿又恢复),不是单次请求随机丢包那种——重试间隔给够,
    # 但也不宜太长,失败得快才能尽快切到备用数据源。
    last_err = None
    delay = 0.3
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode(encoding)
        except (OSError, ValueError) as e:
            # OSError 覆盖 urllib.error.URLError、socket 超时,以及抽风时常见的
            # http.client.RemoteDisconnected(它是 OSError 的子类,不是 URLError
            # 的子类,之前只抓 URLError 漏掉了这种、导致请求线程被未捕获异常
            # 干掉、浏览器端直接收到空响应)。
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 1.8, 1.5)
    raise last_err


def _fetch_json(url: str, retries: int = 3, encoding: str = "utf-8") -> dict:
    return json.loads(_fetch_text(url, retries=retries, encoding=encoding))


# ---- 数据源 1: 东方财富(A股场内ETF主源) ----

def _fetch_quote_eastmoney(etf: dict) -> dict:
    secid = f"{etf['market']}.{etf['code']}"
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields=f43,f60,f169,f170,f57,f58"
    )
    data = _fetch_json(url).get("data")
    if not data:
        raise ValueError("eastmoney: empty data")
    return {
        "code": etf["code"],
        "name": etf["name"],
        "price": data.get("f43", 0) / 1000,
        "prevClose": data.get("f60", 0) / 1000,
        "changeAmt": data.get("f169", 0) / 1000,
        "changePct": data.get("f170", 0) / 100,
    }


def _fetch_trend_eastmoney(etf: dict) -> list:
    secid = f"{etf['market']}.{etf['code']}"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1"
    )
    data = _fetch_json(url).get("data") or {}
    trends = data.get("trends") or []
    points = []
    for t in trends:
        # 分时数据每行格式: 时间,开盘,收盘,最高,最低,成交量,成交额,均价
        parts = t.split(",")
        if len(parts) >= 3:
            points.append({"time": parts[0][-5:], "price": float(parts[2])})
    if not points:
        raise ValueError("eastmoney: empty trends")
    return points


# ---- 数据源 2: 腾讯(A股场内ETF备源,东财失败时自动切换) ----
# gtimg.cn 跟东方财富是完全不相关的另一套行情基础设施,两边同时故障的概率
# 远低于单边故障。经 curl 实测确认不需要 Referer(跟 Sina 的接口不一样),
# 且响应带 Access-Control-Allow-Origin: *。

def _tencent_symbol(etf: dict) -> str:
    prefix = "sh" if etf["market"] == 1 else "sz"
    return f"{prefix}{etf['code']}"


def _fetch_quote_tencent(etf: dict) -> dict:
    sym = _tencent_symbol(etf)
    url = f"https://qt.gtimg.cn/q={sym}"
    text = _fetch_text(url, encoding="gbk")
    # 格式: v_sh512070="1~名称~代码~现价~昨收~今开~...(20个五档买卖盘字段)...
    #        ~时间戳~涨跌额~涨跌幅~最高~最低~...";
    # 字段下标(从0开始): 3=现价 4=昨收 31=涨跌额 32=涨跌幅,逐个用真实数据核对过。
    raw = text.split('"', 1)[1].rsplit('"', 1)[0]
    parts = raw.split("~")
    return {
        "code": etf["code"],
        "name": etf["name"],
        "price": float(parts[3]),
        "prevClose": float(parts[4]),
        "changeAmt": float(parts[31]),
        "changePct": float(parts[32]),
        "source": "tencent",
    }


def _fetch_trend_tencent(etf: dict) -> list:
    sym = _tencent_symbol(etf)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    obj = _fetch_json(url)
    rows = obj["data"][sym]["data"]["data"]
    points = []
    for row in rows:
        # 格式: "HHMM 价格 成交量(手) 累计成交额"
        parts = row.split()
        if len(parts) >= 2 and len(parts[0]) == 4:
            hhmm = parts[0]
            points.append({"time": f"{hhmm[:2]}:{hhmm[2:]}", "price": float(parts[1])})
    if not points:
        raise ValueError("tencent: empty trends")
    return points


# ---- 数据源 3: 新浪期货(沪金主连,元/克——银行App那种"人民币金价"图) ----
# 新浪的股票接口需要 Referer(在别处验证过),期货接口一样需要,已用 curl
# 实测确认。东方财富的搜索接口显示这个合约其实也有 secid(113.aum),但
# push2 接口调试期间一直 502,没法验证它的价格换算比例(f43 到底除以100
# 还是1000,不同品种不一样),为了不引入没验证过的错误数据,先只接新浪
# 这一个源,不做东财/新浪的自动切换(等以后新浪也不稳定了再说)。
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}


def _fetch_quote_sina_futures(item: dict) -> dict:
    url = f"https://hq.sinajs.cn/list={item['sina_quote_symbol']}"
    text = _fetch_text(url, encoding="gbk", headers=SINA_HEADERS)
    raw = text.split('"', 1)[1].rsplit('"', 1)[0]
    if not raw:
        raise ValueError("sina: empty quote")
    parts = raw.split(",")
    # 格式: 名称,时间,昨结算,今开盘,最高,最低,买价,卖价,最新价,...
    # 期货涨跌幅按"昨结算价"算,不是股票那种"昨收盘价"。
    prev_settle = float(parts[2])
    last = float(parts[8])
    return {
        "code": item["code"],
        "name": item["name"],
        "price": last,
        "prevClose": prev_settle,
        "changeAmt": last - prev_settle,
        "changePct": (last - prev_settle) / prev_settle * 100 if prev_settle else 0.0,
        "source": "sina",
        "unit": "CNY/g",
        "digits": 2,
    }


def _fetch_trend_sina_futures(item: dict) -> list:
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20a/"
        f"InnerFuturesNewService.getFewMinLine?symbol={item['sina_trend_symbol']}&type=1"
    )
    text = _fetch_text(url, headers=SINA_HEADERS)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("sina: unexpected trend response")
    rows = json.loads(text[start:end + 1])
    if not rows:
        raise ValueError("sina: empty trend rows")
    # 这个接口返回的是最近好几天的连续1分钟线(含夜盘,跨日期),不是只有
    # 今天的——找最后一次超过60分钟的间隔,只保留这次间隔之后的部分,就是
    # "当前这个连续交易时段"(不管现在是日盘还是夜盘)。
    times = [datetime.datetime.strptime(r["d"], "%Y-%m-%d %H:%M:%S") for r in rows]
    cut = 0
    for i in range(1, len(times)):
        if (times[i] - times[i - 1]).total_seconds() > 3600:
            cut = i
    session_rows = rows[cut:]
    return [{"time": r["d"][-8:-3], "price": float(r["c"])} for r in session_rows]


_FALLBACK_ERRORS = (OSError, ValueError, TypeError, IndexError, KeyError)


def fetch_one_quote(etf: dict) -> dict:
    try:
        return _fetch_quote_eastmoney(etf)
    except _FALLBACK_ERRORS:
        pass
    try:
        return _fetch_quote_tencent(etf)
    except _FALLBACK_ERRORS as e:
        return {"code": etf["code"], "name": etf["name"], "error": str(e)}


def fetch_one_trend(etf: dict) -> dict:
    try:
        return {"code": etf["code"], "points": _fetch_trend_eastmoney(etf)}
    except _FALLBACK_ERRORS:
        pass
    try:
        return {"code": etf["code"], "points": _fetch_trend_tencent(etf), "source": "tencent"}
    except _FALLBACK_ERRORS as e:
        return {"code": etf["code"], "points": [], "error": str(e)}


def fetch_one(item: dict) -> dict:
    if item.get("kind") == "futures":
        try:
            return _fetch_quote_sina_futures(item)
        except _FALLBACK_ERRORS as e:
            return {"code": item["code"], "name": item["name"], "error": str(e)}
    return fetch_one_quote(item)


def fetch_one_trend_for_code(code: str) -> dict:
    item = next((i for i in INSTRUMENTS if i["code"] == code), None)
    if not item:
        return {"code": code, "points": [], "error": "unknown code"}
    if item.get("kind") == "futures":
        try:
            return {"code": code, "points": _fetch_trend_sina_futures(item)}
        except _FALLBACK_ERRORS as e:
            return {"code": code, "points": [], "error": str(e)}
    return fetch_one_trend(item)


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._serve_index()
            return

        if parsed.path == "/api/quotes":
            self._handle_quotes()
            return

        if parsed.path == "/api/trend":
            self._handle_trend(parse_qs(parsed.query))
            return

        self._send_text(404, "not found")

    def _serve_index(self) -> None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_text(500, "index.html not found next to server.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_quotes(self) -> None:
        # 全部并发拉取,而不是挨个排队——每只自己的重试/切换预算不会因为排在
        # 后面而被前面的重试拖慢,整体延迟取决于最慢的一只而不是总和。展示
        # 顺序就是 INSTRUMENTS 里写的顺序,pool.map 保证结果顺序跟输入一致。
        with ThreadPoolExecutor(max_workers=len(INSTRUMENTS)) as pool:
            results = list(pool.map(fetch_one, INSTRUMENTS))
        self._send_json({"data": results})

    def _handle_trend(self, qs: dict) -> None:
        code = qs.get("code", [""])[0]
        self._send_json(fetch_one_trend_for_code(code))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        print(f"{self.address_string()} - {format % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"看盘面板: http://localhost:{PORT}  (Ctrl+C 停止)", flush=True)
    server.serve_forever()
