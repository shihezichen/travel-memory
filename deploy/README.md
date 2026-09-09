# 部署示例

这里放的是可选的运维单元，不是运行程序必需的。

## 演示账号每日重置

公开演示账号谁都能改，一天下来数据就乱了。这两个 systemd user unit 让它每天
03:00 恢复出厂状态（`scripts/reset_demo.py`）。

```bash
mkdir -p ~/.config/systemd/user
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
# 单元里用 %h/travel-memory 指代项目路径，如果你放在别处，改 WorkingDirectory 和 ExecStart

systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
systemctl --user list-timers travel-memory-demo-reset.timer   # 确认下次触发时间
```

想立刻手动跑一次：

```bash
python3 scripts/reset_demo.py
```

不需要演示账号的话，别装这个 timer 就行；也可以直接把 `test` 账号删掉。
