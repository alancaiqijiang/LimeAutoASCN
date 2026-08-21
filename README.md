# LimeAuto 售后经销商 OA 系统（LimeAuto After-Sales Portal）

> **Project ID:** L001-AOSS_001　|　所属客户：L001-TMLD 杭州通木朗科技（LimeAuto 自有公司）· 公司子项目

面向 **海外经销商（Dealer Portal）+ LimeAuto 内部（Admin Console）** 的售后协同 Web 系统，核心场景：**海外车辆故障 → 经销商质保索赔 → 查质保记录 → 核销 → 备件发货**，并提供 BYD/多品牌 OE 配件查询库。

## 技术栈

| 层 | 选型 |
|----|------|
| 前端 | 原生 HTML5 + CSS3 + JavaScript（fetch / AJAX） |
| 后端 | Laravel（PHP 8.3+） |
| 数据库 | MySQL 8.0 |
| 鉴权 | Laravel Sanctum（Token） |

> 架构模式：**前后端半分离** —— 静态 HTML + AJAX 调 Laravel JSON API，业务规则全部在后端 Service 层。

## 目录结构

```
L001-AOSS_001_售后经销商OA系统/
├── L001-AOSS_001_..._产品规格文档.md/.html   # 业务 PRD（需求源头）
├── L001-AOSS_002_..._网站需求文档.md/.html   # 网页端 IA / 页面清单 / 流程权限
├── L001-AOSS_003_..._技术架构文档.md/.html   # 技术栈 / 数据库 / API / 部署
└── frontend/                                 # 前端 HTML 骨架（可点击原型）
    ├── index.html                            # 登录页
    ├── dealer/                               # 经销商门户（工作台/车辆/索赔/OE/发货）
    ├── admin/                                # 内部后台（审核/核销/备件）
    └── assets/                               # css / js（共享样式 + mock 数据 + fetch 封装）
```

## 文档索引

| 编号 | 文档 | 说明 |
|------|------|------|
| L001-AOSS_001 | 产品规格文档 | 业务需求：8 大模块、用户故事、P0/P1/P2、OE 查询库专项、分期 |
| L001-AOSS_002 | 网站需求文档 | 站点地图、页面清单、关键流程、RBAC 路由映射、NFR |
| L001-AOSS_003 | 技术架构文档 | 技术栈、目录结构、MySQL 20 表、AJAX API 契约、安全、部署、迭代 |

## 协作方式

1. **Clone**：`git clone <repo-url>`
2. **分支规范**：`main`（稳定）→ 功能分支 `feature/xxx` → 提 Pull Request → 审核合并
3. **需求管理**：把 PRD 中的 P0/P1 拆成 GitHub Issue 分配
4. **前端骨架说明**：`frontend/assets/js/app.js` 内置 mock 数据，`USE_MOCK=true` 时无需后端即可本地预览；接后端后设为 `false` 即对接 `/api/*`

## 本地预览前端骨架

```bash
cd frontend
python3 -m http.server 8930
# 打开 http://localhost:8930
```

## 当前进度

- [x] 三份文档（产品 / 网站 / 技术架构）
- [x] 前端 HTML 骨架（可点击原型，mock 数据）
- [ ] Iter 0：Laravel 工程初始化 + Sanctum 登录 + 核心 API
- [ ] MySQL 建表 DDL（20 张表）
- [ ] Iter 1：索赔状态机 + 质保校验 + 核销/发货闭环
