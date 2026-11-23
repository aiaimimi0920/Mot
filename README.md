# Mot

Mot 面向游戏与 AI 伙伴之间的交互，目标是形成开放、可独立实现的协议标准，并提供配套 SDK。它首先定义交互契约，而不是指定某个桌面宠物、模型供应商或中心服务。

协议关注四类问题：游戏需要向伙伴提供什么信息、允许伙伴获知什么信息、伙伴可以请求什么行动，以及哪些信息或行动必须受到限制。游戏拥有世界事实、信息开放和动作执行权；玩家授权与伙伴偏好不能突破游戏权限或其他参与者的隐私边界。协议约束可观察的数据与行动，不声称能够消除模型已有知识或保证恶意端点绝不剧透。

**当前范围只限游戏交互。** 不以扩展为通用应用自动化协议为当前目标。协议与 SDK 仍处于实验和开发阶段，尚不是已发布的稳定标准。

## 仓库与分层

本仓库提供标准方向、跨项目边界和固定版本入口。当前协议设计、机器契约、参考实现及符合性验证保存在 G2A 子仓库中；保留既有 `G2A` 名称、包名、路径和 wire 版本，不以定位调整冒充协议升级。

| 目录 / 子仓库 | 在 Mot 中的角色 | 当前状态 |
| --- | --- | --- |
| [G2A/docs/standard](G2A/docs/standard/README.md) | **G2A `V0.0.1` 正式协议规范与双语伪代码实现规范** | `V0.0.1` 基线锁定（含中英主规范与中英伪代码 API 蓝图） |
| [interaction-origin](interaction-origin/README.md) | **第一层：交互源点多引擎 SDK 套件**（Python / C# / Godot 4 GDScript / C++17 / TypeScript） | `V0.0.1` 独立源码目录（支持信号快照、空间脏差分、无入站端口出站流、多人阵营隔离与五大品类示例） |
| [intelligent-hub](intelligent-hub/README.md) | **第二层：智能枢纽独立守护进程**（Go 零依赖单文件 + 玩家安全审批 Web 中心 + NAT 出站隧道） | `V0.0.1` 独立源码目录（含持久化去重账本、整队单次确认门禁、SSE/WebSocket 双模推送、多人阵营保密隔离与临时 ECDSA TLS 1.3） |
| [computation-core](computation-core/README.md) | **第三层：演算中枢 SDK 与独立运行体**（Python + TypeScript `@g2a/computation-core` + Ollama/OpenAI 防注入规划器 + SQLite/HTTP 记忆服 + 云端中继服） | `V0.0.1` 独立源码目录（含 14 项标准错误码在线符合性套件、TLS 1.3 `wss://` 流与 `g2a-core demo` 一键演练台） |
| [G2A](G2A/README.md) | 协议演进仓库、机器契约与早期实验验证归档 | 承载 `docs/standard/` `V0.0.1` 标准文档及早期研究历史 |
| [MotGUI](MotGUI/README.md) | 可选伙伴应用的桌面呈现与用户入口 | 保留旧 Godot 客户端；拆仓后的运行路径仍待适配 |
| [MotCore](MotCore/README.md) | 可选伙伴应用的智能运行时设计归档 | 设计文档归档；不作为协议必需依赖 |
| [MotPlugin](MotPlugin/README.md) | 可选伙伴应用的功能插件及服务适配 | 保留旧插件源码，尚未恢复完整装配 |

一键编译并验证全部四语言（Go / C++17 / C# / Python）`V0.0.1` 实现与跨进程全链路测试，或构建跨平台发行压缩包与独立子仓库导出：

```sh
python scripts/verify_v0_0_1_all.py
python scripts/build_releases.py
python scripts/split_standalone_repos.py --output-dir ../g2a-standalone-repos
```

游戏及第三方伙伴可以独立实现协议，无需安装 MotGUI、MotCore 或 MotPlugin，也不依赖 Mot 账号、服务器、模型或记忆存储。桌面形象、窗口、资源传输和跨设备恢复属于可选能力，不能成为所有游戏的隐含接入门槛。

## 从哪里开始

- [架构、职责与迁移历史](docs/ARCHITECTURE.md)
- [G2A 实验规范](G2A/docs/SPECIFICATION.md)：现有 `0.1.0-dev` 契约。
- [下一版协议设计](G2A/docs/protocol/README.md)：工作草案及设计模型，不是旧 SDK 可直接使用的新 wire 版本。
- [SDK、示例与验证入口](G2A/README.md)：按对应版本和实际依赖运行，不将本地模型检查称为生产或第三方互通认证。
- [子仓库操作说明](docs/SUBMODULES.md)

## 检出

```sh
git clone --recurse-submodules https://github.com/aiaimimi0920/Mot.git
```

各子项目通过 Git submodule 固定提交，可独立检出和开发。已有检出先保护主仓库与子仓库的本地修改，再按子仓库操作说明更新；不要强制重置正在进行的工作。

## 交付边界

已存在的协议实验代码、设计文档和场景验证不是空白基础，但也不代表完整新 SDK、真实游戏生产适配或稳定标准已经交付。后续工作需分别说明规范版本、SDK 支持范围、测试环境和未验证事项。

旧伙伴应用的拆仓迁移没有恢复完整可运行性。`MotGUI/project.godot` 仍是旧客户端入口，其历史 `res://plugin/...` 等路径不能因本仓库包含 MotPlugin 就自动恢复；修复该应用不是游戏协议的前置条件。

保留各项目原有许可证与第三方声明。本次定位调整不改变许可，也不表示已完成标准治理、独立厂商采纳、公开发布或正式符合性认证。
