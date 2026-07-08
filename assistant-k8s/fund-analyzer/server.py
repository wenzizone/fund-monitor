#!/usr/bin/env python3
"""fund-analyzer HTTP 服务: 把 analyze_fund 包一层,供集群内其他服务(如 OpenClaw)调用。

GET /healthz        -> 存活探针
GET /report?codes=017234,010392  -> 纯文本报告,多个代码用逗号分隔
GET /sector-report?sectors=银行保险,医药消费  -> 板块估值报告(代表股篮子),多个板块用逗号分隔
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from analyze_fund import analyze, format_report, get_sector_report, format_sector_report

PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    def _send_text(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_text(200, "ok")
            return

        if parsed.path == "/report":
            codes = parse_qs(parsed.query).get("codes", [""])[0]
            codes = [c.strip() for c in codes.split(",") if c.strip()]
            if not codes:
                self._send_text(400, "missing ?codes=<基金代码,逗号分隔>")
                return
            reports = []
            for code in codes:
                try:
                    reports.append(format_report(analyze(code)))
                except Exception as e:  # noqa: BLE001 - 单只基金失败不影响其余基金的报告
                    reports.append(f"=== {code} ===\n分析失败: {type(e).__name__}: {e}")
            self._send_text(200, "\n\n".join(reports))
            return

        if parsed.path == "/sector-report":
            sectors = parse_qs(parsed.query).get("sectors", [""])[0]
            sectors = [s.strip() for s in sectors.split(",") if s.strip()]
            if not sectors:
                self._send_text(400, "missing ?sectors=<板块名,逗号分隔>")
                return
            reports = []
            for sector in sectors:
                try:
                    reports.append(format_sector_report(get_sector_report(sector)))
                except Exception as e:  # noqa: BLE001 - 单个板块失败不影响其余板块的报告
                    reports.append(f"=== {sector} ===\n分析失败: {type(e).__name__}: {e}")
            self._send_text(200, "\n\n".join(reports))
            return

        self._send_text(404, "not found")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 匹配基类签名
        print(f"{self.address_string()} - {format % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fund-analyzer listening on :{PORT}", flush=True)
    server.serve_forever()
