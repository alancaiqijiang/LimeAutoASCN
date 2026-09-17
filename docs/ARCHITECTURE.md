# LimeAuto 架构

## 产品面

小型 FastAPI 服务，两条受控路径：

- `/ops`：员工登录后的售后事实（车辆、维保、审计、人工目录快捷引用）。
- `/catalog`：可选的只读浏览，绑定一份不可变 catalog release 与已验证本地媒体。

运行入口：`app.main:app`。

## 数据边界

```text
aftercare.sqlite3          可写业务事实（账号、车辆、维保、审计、快捷引用）
catalog release SQLite     不可变只读投影（车系/车型/节点/物料/fitment/媒体索引）
catalog asset root         已校验本地媒体
translation sqlite         可选英文显示层，指纹必须匹配当前 release
qpren                      仅离线构建输入，网站请求路径不连接
```

三层互不写入对方。缺目录或目录关闭时，VIN 登记与维保记录仍可进行。

## 运行时约束

- 默认 `LIMEAUTO_CATALOG_BROWSE=0`、`LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT=0`。
- 目录开启后仍须员工会话；匿名 `/catalog` 与媒体应被拒绝。
- 生产只接受 `validated` 或 `published` release。
- 不把源绝对路径、DSN、价格、库存或上游 URL 写入公开响应。
- 缺图保留固定空白媒体位，不隐藏记录、不塌缩版式。

## 售后事实模型

VIN 在后端规范化、校验和查重；不进入 URL 或普通日志。维护记录只保存发生过的事实，不保存政策结论。车型快捷入口是人工指定的站内 `/catalog` 导航，不是解码、适配或授权。

## 发布流程

```text
只读源审计 → contract → draft SQLite → validator
→ 独立资产物化 → 流式校验 → staging 验收 → 人工发布
```

draft 不是生产。构建成功不等于切换 current、开启公网路由或替换业务库。生产售后库应新建空库并单独建管理员，不要复制 staging 业务数据或密钥。

## 本仓库之外

catalog sqlite、资产树、售后库、译本运行库、翻译语料和内部运维证据不进 Git。源码清单由 `tools/code_manifest.py` 生成，覆盖 `app/`、`tests/`、`tools/`、`release_schema/` 与根配置文件。
