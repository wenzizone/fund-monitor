#!/usr/bin/env bash
# 部署 fund-analyzer(独立于 OpenClaw 的量化分析服务,同 namespace,不同 Deployment)。
#
# ConfigMap 由 kustomization.yaml 的 configMapGenerator 声明式生成(直接读取本目录内的
# analyze_fund.py / server.py——这两个文件的"正本"实际放在这里,仓库根目录那两个是
# 软链接,方便本地 `python3 analyze_fund.py` 照旧使用,不影响 Kustomize 自包含)。
# 内容变化会自动带新的哈希后缀,Deployment 引用和滚动重启都是 Kustomize 自动处理,
# 不需要手动 kubectl create configmap 或 rollout restart。
#
# 这套 kustomization 可以原样交给 ArgoCD 的 Application 指向本目录,不需要任何
# 额外的 ArgoCD/Kustomize 全局配置(特意把文件放本目录内,就是为了避免依赖
# --load-restrictor LoadRestrictionsNone 这种要改公司共享 ArgoCD 配置的前提)。
#
# 用法:
#   ./deploy.sh            # 部署/更新
#   ./deploy.sh --delete   # 删除 fund-analyzer 的资源(不影响 OpenClaw)
#
# 环境:
#   OPENCLAW_NAMESPACE   Kubernetes 命名空间(默认: gateway,需和 OpenClaw 部署一致)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="${OPENCLAW_NAMESPACE:-gateway}"

command -v kubectl &>/dev/null || { echo "Missing: kubectl" >&2; exit 1; }
kubectl cluster-info &>/dev/null || { echo "Cannot connect to cluster. Check kubeconfig." >&2; exit 1; }

if [[ "${1:-}" == "--delete" ]]; then
  echo "Deleting fund-analyzer resources in namespace '$NS'..."
  kubectl kustomize "$SCRIPT_DIR" | kubectl delete -n "$NS" --ignore-not-found -f -
  echo "Done."
  exit 0
fi

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "Building kustomize output and applying..."
kubectl kustomize "$SCRIPT_DIR" | kubectl apply -n "$NS" -f -

echo ""
echo "Waiting for rollout (first run installs akshare, can take a few minutes)..."
kubectl rollout status deployment/fund-analyzer -n "$NS" --timeout=600s

echo ""
echo "Done. From another pod in the same namespace:"
echo "  curl http://fund-analyzer:8080/report?codes=017234,010392"
