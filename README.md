# LimeAuto Aftercare & Catalog

LimeAuto 员工售后工作台与只读车型目录。本仓库是 **2026-09-16 生产冻结版** 源码（对应提交 `395b7cd`），供独立部署或备份。

两条正式产品路径：

```text
/ops
  员工登录 → VIN 登记 → 车辆记录 → 维护事实 → 审计
  → 车辆总览
  → 人工关联只读车型目录（仅导航引用）

/catalog
  车系 → 车型 → 系统节点 → 物料 → 物料详情与已验证媒体
  物料检索（编码/名称，不含价格）
```

`/catalog` 只读，只读取本地不可变 SQLite catalog release 和本地 asset tree。网站运行时不依赖、连接、加载、跳转或链接任何外部站点。`/ops` 使用独立可写 `aftercare.sqlite3` 保存内部业务事实。

目录与媒体默认受员工会话保护。生产入口：

- 员工登录：`https://limeauto.cn/ops/login`
- 目录：`https://limeauto.cn/catalog`（需登录）

静态官网仍独立部署，不在本仓库。

## 本仓库包含 / 不包含

包含：

- FastAPI 应用（`app/`）
- catalog release schema（`release_schema/`）
- 离线构建、校验、译本与员工建号工具（`tools/`）
- 离线测试（`tests/`）
- 运行配置示例（`catalog.env.example`）
- 已纳入版本库的英文命名/饰面政策词表（`translation/glossary/`）

**不包含（体积大或含运行态/内部证据，须单独保管）：**

- catalog release SQLite 与媒体资产树
- 售后业务库 `aftercare.sqlite3` 与附件
- 英文译本运行库
- 翻译语料 `translation/runs/`、`translation/reviews/`
- 内部 staging 运维记录、开发日志、截图与校验 JSON

缺语料时，依赖语料的合同测试会 skip，不会把源码判为损坏。

## 真实性边界

- 内部 catalog release 提供已载入的 `series_code + model_code`、EPC 节点、物料和本地媒体关系。
- 不执行任意 VIN 到精确车型的自动解码。
- 人工 catalog shortcut 不是 VIN 解码、fitment、保修/政策或权益结论。
- 维护记录是事实记录，不自动判断政策。
- 不实现客户账户、公开 VIN 查询、报价、价格、库存、订单、支付或履约。

## 默认安全配置

```text
LIMEAUTO_CATALOG_BROWSE=0
LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT=0
```

默认只开放 `/ops/login` 和内部售后入口。目录关闭时 `/catalog*` 与 `/media/catalog*` 返回 `410 catalog_browse_disabled`。

生产必须使用 HTTPS、足够长的随机 `LIMEAUTO_AFTERCARE_HMAC_SECRET`、`LIMEAUTO_AFTERCARE_COOKIE_SECURE=1`，且 release 状态必须是 `validated` 或 `published`。不要把 draft 或 `ALLOW_DRAFT=1` 带到生产。

## 目录结构

```text
app/                   FastAPI 入口、售后、目录读取、模板与静态文件
release_schema/        catalog release SQLite schema
tools/                 离线审计、构建、物化、校验、manifest、员工建号
tests/                 售后、release、媒体、构建和校验测试
translation/glossary/  已纳入版本库的命名/饰面政策
catalog.env.example    运行配置示例（不含密钥）
docs/ARCHITECTURE.md   数据边界与发布流程
```

运行入口：`app.main:app`。

## 本地安装与验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app tools tests
.venv/bin/python tools/code_manifest.py --output /tmp/limeauto-code-manifest.json
```

## 创建员工账号

```bash
.venv/bin/python tools/create_aftercare_staff.py \
  --email staff@example.com --role admin
```

口令走交互提示或 `--password-stdin`，不要写进命令行、日志或仓库。

## 运行

复制 `catalog.env.example` 为本地 `.env`（已 gitignore），填入真实路径和密钥。目录预览必须显式指定已验证或已发布 release、独立 asset root，并只绑定 loopback：

```bash
env \
  LIMEAUTO_CATALOG_BROWSE=1 \
  LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT=0 \
  LIMEAUTO_CATALOG_RELEASE_PATH=/srv/limeauto/artifacts/releases/catalog.sqlite \
  LIMEAUTO_CATALOG_TRANSLATION_PATH=/srv/limeauto/artifacts/translations/catalog_translation.sqlite \
  LIMEAUTO_CATALOG_ASSET_ROOT=/srv/limeauto/artifacts/catalog-assets \
  LIMEAUTO_AFTERCARE_DB_PATH=/srv/limeauto/data/aftercare.sqlite3 \
  LIMEAUTO_AFTERCARE_HMAC_SECRET='use-a-long-random-secret' \
  LIMEAUTO_AFTERCARE_COOKIE_SECURE=1 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766
```

健康探针：`GET /health/live` 检查进程，`GET /health/ready` 检查当前目录配置。生产边缘不应公开 `/health/*`。

## Release 工作流

```text
只读内部目录审计
→ catalog contract
→ draft SQLite build
→ release validator
→ 独立 asset materialization
→ 流式资产校验
→ staging HTTP/浏览器验收
→ 人工发布评审
```

构建器只生成 `draft`，不修改内部源数据、不切换 current、不修改生产 Nginx。所有资产必须通过 object key、大小、SHA-256、MIME/magic 和路径边界检查。

离线构建工具只处理项目内部保存的数据，不属于网站运行时。

## 不在当前范围

自动 VIN 解码、自动适配/政策判断、客户账户、批量导入与复杂重复合并、报价/库存/交易、外部源运行时查询，均保持关闭。运营备份（售后库与附件的定期备份、保留策略、恢复验证）尚未实现。

## 版本

| 项 | 值 |
| --- | --- |
| 冻结提交 | `395b7cd8bf51769f28d04ccc62a98fcc89264055` |
| 冻结日期 | 2026-09-16 |
| 导出说明 | 由维护工作树按生产源码清单导出，不含 translation/runs、local-reports、业务库与资产 |
