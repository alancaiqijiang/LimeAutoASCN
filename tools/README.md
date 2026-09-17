# LimeAuto release tools

这些 CLI 只服务于离线 qpren 证据、不可变 catalog release 和独立媒体树。网页运行时不连接 qpren，也不写 release。

## 工具

- `audit_qpren_source.py`：只读审计 qpren 源结构与覆盖。
- `profile_qpren_nodes.py`：只读分析节点路径和变体。
- `build_catalog_release.py`：从只读 qpren 构建普通 catalog draft。
- `build_epc_enriched_catalog_revision.py`：唯一 canonical EPC 增强 builder，包含真实媒体 magic/MIME 归一化。
- `build_material_backfill_revision.py`：物料回补阶段的独立 draft builder。
- `materialize_catalog_assets.py`：按 release manifest 物化独立资产树，不修改 release。
- `materialize_release_from_sources.py`：从 qpren hash 索引和已验证 material 根为一个 release 建立硬链接资产树。
- `prepare_compat_asset_root.py`：将已存在资产安全映射到独立 staging root。
- `normalize_catalog_media.py`：从只读候选生成新的媒体规范化 draft，按真实 magic 修正扩展名/MIME，并用硬链接复用资产。
- `stream_validate_epc.py`：流式校验 release asset rows 与实际文件。
- `validate_catalog_assets_shard.py`：按 `catalog_assets.rowid` 的半开区间逐片校验资产文件；每片独立报告，可中断后从下一片继续。
- `merge_catalog_asset_shards.py`：只读合并分片报告，要求 rowid 区间无重叠、无缺口且覆盖全部资产行。
- `validate_catalog_release.py`：只读校验 SQLite、外键、节点树、fitment 和资产约束；大表按游标流式扫描，CLI 默认只保留 128 条成功资产证据，失败证据始终保留。
- `compare_release_to_qpren.py`：对照 release 与只读 qpren 证据。
- `code_manifest.py`：生成或比较稳定的源码/部署文件 SHA-256 清单。

## 通用约束

```text
source qpren（只读）
→ draft release
→ validator
→ 独立 asset root
→ HTTP/浏览器 staging
→ 人工发布评审
```

构建和物化不能切换 current、修改 Nginx、修改 qpren 或把 draft 标记为生产 release。`--source-root` 与 `--output-root` 必须分离；资产必须通过路径、存在性、非空、大小、SHA-256、MIME 和真实 magic 检查。`validate_catalog_release.py` 的 `checks.assets` 是全量聚合结果；报告中的 `asset_checks` 仅是有界证据样本（`--asset-check-limit 0` 可关闭成功样本）。

资源受限服务器上的资产验证使用半开 rowid 区间 `[start, end)`，一次只运行一个分片：

```bash
.venv/bin/python tools/validate_catalog_assets_shard.py \
  --database /path/to/release.sqlite \
  --asset-root /path/to/assets \
  --rowid-start 1 --rowid-end 50001 \
  --artifact-sha256 KNOWN_RELEASE_SHA256 \
  --output reports/assets-000001-050001.json

.venv/bin/python tools/merge_catalog_asset_shards.py \
  --reports reports/assets-*.json \
  --require-complete \
  --output reports/assets-merged.json
```

`--artifact-sha256` 是调用者对不可变 release 的已知指纹，不由每个分片重复计算 5GB 级 SQLite。分片工具只读打开数据库和资产根；合并器只读 JSON，不替代 quick/full SQLite integrity check，也不允许跨数据库、资产根或文件元数据合并。

合并结果中的 `files_checked` / `inode_reused` 是各分片内部的去重计数之和；由于同一硬链接可能跨分片出现，它们不是全树唯一物理文件数。合并结果会显式标记 `asset_hash_dedup_scope=within_shard_only`。

## 测试

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app tools tests
.venv/bin/python tools/code_manifest.py --output /tmp/limeauto-code-manifest.json
```

需要 live qpren 的测试或工具使用显式环境变量：

```bash
export LIMEAUTO_QPREN_READ_DSN='postgresql://USER:***@HOST:5432/DATABASE'
```

不要把密码、cookie、完整 DSN、源 URL 或绝对源路径写入报告、代码或版本库。

## Staging 应用

```bash
env \
  LIMEAUTO_CATALOG_BROWSE=1 \
  LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT=0 \
  LIMEAUTO_CATALOG_RELEASE_PATH=/srv/limeauto/artifacts/releases/catalog.sqlite \
  LIMEAUTO_CATALOG_ASSET_ROOT=/srv/limeauto/artifacts/catalog-assets \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766
```

默认目录关闭。只有 `validated`/`published` release 才允许进入正式运行配置。