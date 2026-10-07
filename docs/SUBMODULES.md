# 子仓库开发与集成

## 新检出与已有检出

```sh
git clone --recurse-submodules https://github.com/aiaimimi0920/Mot.git
```

已有检出在保存好主仓库和子仓库的本地修改后，可以：

```sh
git pull --ff-only
git submodule sync --recursive
git submodule update --init --recursive
```

如果存在冲突、未提交修改或本地与远程分叉，先检查，不使用 `--force`、`reset --hard` 或清理命令绕过。

## 子项目独立开发

普通 `git submodule update` 会将子仓库检出到主仓库固定提交，可能处于 detached HEAD。这是可复现检出，不是开发分支。

开始开发前检查 `git status`，在确认工作状态后切换或创建自己的分支，再在该子仓库提交、推送。子项目也可以在别处独立 clone，不必检出整个 Mot。

## 发布集成版本

1. 在相关子仓库完成提交与推送，记录 SHA。
2. 将主仓库中的对应子模块定位到已发布 SHA。
3. 在主仓库执行 `git diff --submodule`，确认引用变动。
4. 将子模块路径加入暂存区，提交并推送主仓库。

`.gitmodules` 描述路径和远程地址；真正固定的版本是主仓库树中 mode `160000` 的 gitlink。没有配置自动追随分支，不默认将各子项目最新提交当作兼容组合。

当前初始组合仅表示结构迁移完成，不表示客户端与插件已经通过运行联调。
