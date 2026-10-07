# 跨项目职责与独立仓库迁移

日期：2026-10-07。

## 结构

主仓库 Mot 通过 Git submodule 固定 MotGUI、MotCore、G2A、MotPlugin 的提交。子项目独立维护源码和文档；主仓库只描述边界、集成流程与版本组合。

- MotGUI：图形界面、呈现、用户入口、本地执行支持。
- MotCore：智能运行时、会话、记忆、模型和插件编排，部署位置可为云端或本地。
- G2A：游戏与伙伴 Agent 的独立交互规范，不作为 Mot 的全部内部通信协议。
- MotPlugin：具体插件及所需适配层、外部服务、配置与资源。

## 本轮迁移

原 `mot_gui/` 更名为 `MotGUI/`，原 `mot_core/` 更名为 `MotCore/`，原 `g2a/` 更名为 `G2A/`，原 `mot_plugin/` 更名为 `MotPlugin/`。

原 Godot 工程内 `plugin/`、`external_service_adapter/`、`external_service/` 的全部 806 个文件转入 MotPlugin，GUI 不再保存副本。旧 Godot 工程的插件模板和加载器仍属于 MotGUI 的历史运行时部分，尚未重构。

项目内部文档随所有权移动。旧 GUI 文档及恢复证据进入 MotGUI/docs；旧插件分支许可进入 MotPlugin/docs。恢复清单包含当时的跨来源历史，作为恢复旧 Godot 工程的原始证据保存在 MotGUI，而非当前多仓库目录清单；不得把其旧路径与哈希当作当前仓库布局。

## 验收边界

本轮以结构调整为目的：保全文件、核对源码字节、建立独立 Git 仓库、推送子仓库并在主仓库固定引用。没有恢复可运行性。

已知断点：MotGUI 中旧 `res://plugin/...`、`res://external_service_adapter/...`、`res://external_service/...` 不能直接访问仓库外的 MotPlugin。跨目录加载、打包、资源装配、Python 依赖以及定制引擎兼容性都需后续设计。没有用软链接、复制源码或临时同步脚本掩盖这些断点。

## 历史与回退

主仓库保留初始提交 `812b5f56f77b19626340de1b713838117009da17`，不重写远程历史。四个子仓库以各自的新初始提交开始；历史来源记录随相关项目保留。

迁移前工作树完整快照与主仓库 bundle 保存在本机 `C:/Users/Public/nas_home/AI/GameEditor/linshi/mot-submodules-20261007/`。恢复操作应在独立目标目录先验证，不对当前工作树执行强制覆盖或递归清理。

更新集成版本时，先发布子仓库提交，再更新主仓库引用，不能推送一个引用尚未发布对象的集成提交。
