#!/usr/bin/env bash
# 共享 Gitee 资产上传脚本：幂等、并行、单文件超时 30 分钟、失败汇总不中断。
#
# 用法：
#   bash .github/scripts/gitee_upload.sh <REPO> <TAG> <PRERELEASE> <ASSETS_DIR>
#
# 环境变量：
#   GITEE_TOKEN    Gitee 私人令牌（必填；为空时跳过并退出 0）
#   GITEE_REBUILD  设为 1 时删除已存在的同 tag release 后重建（资产清空后全量上传），
#                  用于每次构建都要保证 Gitee 资产是最新产物的场景（默认空=幂等补传）。
#
# 说明：
#   - 幂等：查询 release 现有资产名，已存在的自动跳过，重跑不会重复上传。
#   - 并行：所有缺失资产同时上传（& + wait），总耗时约等于最慢单文件。
#   - 超时：单文件 --connect-timeout 30 --max-time 1800（30 分钟），防止跨洋挂起。
#   - 失败不中断其它上传：最后汇总失败清单并返回非零（已成功的不受影响，可重跑补传）。
#   - release 不存在时自动创建（需 Gitee 镜像仓库已存在同 tag，由 mirror-to-gitee.yml 保证）。
#   - 注意：curl 不允许 -F(multipart) 与 --data-urlencode 混用（exit 2），
#     因此 access_token 放 URL query string，文件用 -F 上传。

set -euo pipefail

REPO="$1"
TAG="$2"
PRERELEASE="$3"
ASSETS_DIR="$4"

if [ -z "${GITEE_TOKEN:-}" ]; then
  echo "GITEE_TOKEN 未配置，跳过 Gitee 同步"
  exit 0
fi

if [ ! -d "$ASSETS_DIR" ]; then
  echo "!! assets dir not found: $ASSETS_DIR"
  exit 1
fi

# ---- 1. 确保 Gitee release 存在（不存在则创建） ----
echo "== check existing Gitee release: $TAG =="
RESP=$(curl -sS -G "https://gitee.com/api/v5/repos/$REPO/releases/tags/$TAG" \
       --data-urlencode "access_token=$GITEE_TOKEN" || true)
RID=$(printf '%s' "$RESP" | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('id',''))" 2>/dev/null || true)
if [ -n "$RID" ] && [ "${GITEE_REBUILD:-}" = "1" ]; then
  echo "== GITEE_REBUILD=1: delete existing release id=$RID, will rebuild =="
  curl -sS -X DELETE "https://gitee.com/api/v5/repos/$REPO/releases/$RID?access_token=$GITEE_TOKEN" || true
  RID=""
fi
if [ -n "$RID" ]; then
  echo "already exists, release id=$RID"
else
  echo "== create Gitee release =="
  # Gitee 创建 release 必须带 body（缺省会报 {"messages":["body is missing"]}）。
  # 优先用 notes.md（构建流程生成），手动补传场景无该文件则用默认说明。
  BODY_ARGS=()
  if [ -f notes.md ]; then
    BODY_ARGS+=(--data-urlencode "body@notes.md")
  else
    BODY_ARGS+=(--data-urlencode "body=$TAG (auto-created by gitee_upload.sh)")
  fi
  RID=""
  for attempt in 1 2 3; do
    RESP=$(curl -sS -X POST "https://gitee.com/api/v5/repos/$REPO/releases" \
          --data-urlencode "access_token=$GITEE_TOKEN" \
          --data-urlencode "tag_name=$TAG" \
          --data-urlencode "name=$TAG" \
          --data-urlencode "target_commitish=main" \
          --data-urlencode "prerelease=$PRERELEASE" \
          "${BODY_ARGS[@]}" || true)
    RID=$(printf '%s' "$RESP" | python -c "import sys,json;d=json.load(sys.stdin);print(d.get('id',''))" 2>/dev/null || true)
    if [ -n "$RID" ]; then
      break
    fi
    echo "  create attempt $attempt failed, response: $RESP"
    sleep 3
  done
  if [ -z "$RID" ]; then
    echo "!! failed to create Gitee release after 3 attempts"
    exit 1
  fi
  echo "created, release id=$RID"
fi

# ---- 2. 现有资产名（幂等跳过依据） ----
echo "== list existing assets =="
RESP=$(curl -sS -G "https://gitee.com/api/v5/repos/$REPO/releases/$RID" \
       --data-urlencode "access_token=$GITEE_TOKEN" || true)
EXISTING=$(printf '%s' "$RESP" | python -c "import sys,json;d=json.load(sys.stdin);print('\\n'.join(a['name'] for a in (d.get('assets') or [])))" 2>/dev/null || true)
echo "existing: ${EXISTING:-none}"

# ---- 3. 并行上传缺失资产 ----
echo "== upload assets (parallel) =="
TMPDIR="${TMPDIR:-/tmp}"
PIDS=""
for f in "$ASSETS_DIR"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  if printf '%s\n' "$EXISTING" | grep -qx "$name"; then
    echo "skip $name (already uploaded)"
    continue
  fi
  size=$(stat -c%s "$f")
  echo "uploading $name (${size} bytes)"
  (
    code=$(curl -sS -X POST "https://gitee.com/api/v5/repos/$REPO/releases/$RID/attach_files?access_token=$GITEE_TOKEN" \
          --connect-timeout 30 --max-time 1800 -F "file=@$f" \
          -o "$TMPDIR/_up_${name}.json" -w "%{http_code}" || true)
    echo "  $name -> HTTP $code"
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then
      exit 0
    fi
    echo "!! failed to upload $name (HTTP $code)"
    exit 1
  ) &
  PIDS="$PIDS $!"
done

# ---- 4. 汇总失败（不中断其它上传，最后返回非零） ----
RC=0
for pid in $PIDS; do
  if ! wait "$pid"; then
    echo "!! one or more assets failed to upload"
    RC=1
  fi
done

if [ "$RC" != "0" ]; then
  echo "!! some assets failed. The script is idempotent, rerun to retry the missing ones."
  exit 1
fi
echo "== all assets uploaded =="
exit 0
