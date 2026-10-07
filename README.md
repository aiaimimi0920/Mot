# Mot

Mot 的集成主仓库：提供项目总入口、跨项目边界和各子项目的固定版本组合，不直接维护子项目源码。

| 子仓库 | 职责 | 当前状态 |
| --- | --- | --- |
| [MotGUI](https://github.com/aiaimimi0920/MotGUI) | 桌面客户端与图形交互 | 已迁入旧 Godot 客户端，插件已拆出，运行路径待适配 |
| [MotCore](https://github.com/aiaimimi0920/MotCore) | 智能核心、记忆、模型与工具编排 | 设计阶段，尚无运行时实现 |
| [G2A](https://github.com/aiaimimi0920/G2A) | 独立的游戏与伙伴 Agent 交互规范 | 已有实验规范、Python/JavaScript、Godot 游戏、本地授权/窗口、来源记忆及出站桥接示例；完整协议仍在开发 |
| [MotPlugin](https://github.com/aiaimimi0920/MotPlugin) | 具体插件、适配层及外部服务 | 已承接旧插件源码，尚未恢复独立运行与装配 |

每个子项目的文档保存在自己的 `docs/`，本仓库只保留跨项目架构与集成说明。子项目使用 Git submodule 引用确定提交，可以分别检出、开发与发布。

## 检出

```sh
git clone --recurse-submodules https://github.com/aiaimimi0920/Mot.git
```

已有主仓库请先确认本地修改已妥善保存，再按 [子仓库操作说明](docs/SUBMODULES.md) 更新。不要用强制重置覆盖正在进行的工作。

## 当前边界

本轮仅完成目录、文档与 Git 所有权迁移，用户明确不要求恢复代码可用性。`MotGUI/project.godot` 仍是客户端工程入口，但旧 `res://plugin/...` 等路径尚未适配到独立的 MotPlugin；没有运行 Godot、插件、构建或历史业务服务。

- [整体职责与迁移记录](docs/ARCHITECTURE.md)
- [子仓库开发与集成版本操作](docs/SUBMODULES.md)
- [MotGUI 文档](https://github.com/aiaimimi0920/MotGUI/tree/main/docs)
- [MotCore 文档](https://github.com/aiaimimi0920/MotCore/tree/main/docs)
- [G2A 文档](https://github.com/aiaimimi0920/G2A/tree/main/docs)
- [MotPlugin 文档](https://github.com/aiaimimi0920/MotPlugin/tree/main/docs)

G2A 不依赖 Mot 的账号或服务；被集成主仓库引用不影响其协议独立性。本轮未重新授权各项目，保留原有许可证与第三方声明；后续若要变更协议发布许可，需要单独决定。
