# RF-BM-ND05 / ND05I — nRF52840 SWD / APPROTECT 排障与解锁实录

> 适用：RF-star RF-BM-ND05、RF-BM-ND05I；本案例芯片为 nRF52840 `AAD0`（旧版 APPROTECT）。调试器：Luxiaoban Flash Pro (CMSIS-DAP)，Windows + PyOCD。  
> **严重警告：解锁通常会触发整片擦除（Mass Erase），清除原固件及 UICR 配置。默认先检查；需要保留原厂固件时绝对不要解锁。**

## 一、故障摘要与最终结论

最初对模组供电 **3.3V** 后无法通过 SWD 正常连接。P0.18 悬空且测得约 3.3V；SWDIO 空闲约 3.3V，SWCLK 空闲约 0V。

`pyocd commander -t nrf52840 -f 100000` 显示：

```text
NRF52840 APPROTECT enabled: not automatically unlocking
Error: 'SoCTarget has no selected core'
```

经 CTRL-AP 检查，`APPROTECTSTATUS` 最初为 `0`（锁定）。启用 PyOCD 的解锁功能后，值变为 `1`（解锁），且可读取 AHB-AP、Cortex-M4 CPUID 和 Flash。
|

> **范围限制**：以上保证的是这颗 AAD0 芯片当前已恢复 SWD，以及实际断电复测成功；不意味着所有 ND05 模组或后续任何固件都永远不可能重新启用 APPROTECT。后续固件仍可修改 UICR 使其锁定。新版 nRF52840（例如 F0 及之后）具有增强型 APPROTECT，不能照搬 AAD0 的 解法。

## 二、硬件与接线

- 正常 SWD：目标板稳定 3.3V、公共 GND、SWDIO、SWCLK；DAPLink 的 VTref 如有，连接到目标 VCC。
- ND05I 与 ND05 的共同焊盘功能/位置可兼容，差异在 ND05I 天线端额外焊盘引发的**引脚编号偏移**。

## 三、准备环境

Windows python3 & cmd

```cmd
python -m pip install -U pyocd
pyocd list
```

调试器识别例子为：

```text
Luxiaoban Flash Pro(CMSIS-DAP) ... Target n/a
```

`Target n/a` 表示探针没有自动选中芯片型号，不等于探针故障。显式使用 `-t nrf52840` 即可。

## 四、手工只读检测（推荐先做）

启动无初始化模式，**务必关闭自动解锁**：

```cmd
pyocd commander -t nrf52840 -f 100000 -N -O auto_unlock=false
```

进入交互界面后逐行输入：

```text
initdp
makeap 1
readap 1 0x008
readap 1 0x00C
```

| CTRL-AP 偏移 | 寄存器 | 值的解释 |
|---|---|---|
| `0x008` | ERASEALLSTATUS | `0` 空闲；`1` 忙 |
| `0x00C` | APPROTECTSTATUS | `0` 保护启用；`1` 保护关闭 |

未解锁时：

```text
AP#1 IDR = 0x02880000
AP register 0x100000c = 0x00000000
```

解锁后：

```text
AP register 0x1000008 = 0x00000000
AP register 0x100000c = 0x00000001
```


## 五、解锁（会清空原固件）

仅在确认不需要保留固件后执行：

```cmd
pyocd commander -t nrf52840 -f 100000 -O auto_unlock=true
```

当检测到 APPROTECT，PyOCD 会尝试 Nordic CTRL-AP **Mass Erase**。如：

```text
NRF52840 APPROTECT enabled: will try to unlock via mass erase
Connected to NRF52840 [Lockup]
```

**不要仅依据 `Connected` 或 `Lockup` 认定结果。** 先重新检查 `APPROTECTSTATUS`；`1` 才表示当前保护关闭。空白 Flash 可能让 CPU 出现 `Lockup`，但 `Lockup` 也不能证明擦除成功。



`read32 0xE000ED00` 成功是 **CPU 调试系统可读**的证据；`0xFFFFFFFF` 只能证明 Flash 起始字已擦除，不能代表整个 Flash 的每个扇区都已验证。

## 六、断电后的持久性验证

1. 退出 PyOCD；**完全断开目标模组的 3.3V 供电**（同时避免调试器信号线反向供电）。
2. 重新给目标上电。
3. 使用不自动解锁的命令连接：

    ```cmd
    pyocd commander -t nrf52840 -f 100000 -O auto_unlock=false
    ```

4. 读取：

    ```text
    read32 0xE000ED00
    ```

    若断电后仍返回 `0x410FC241`，确认无需重新 Mass Erase 也能使用 SWD。

## 八、配套 Python 自动化工具

脚本 `nrf52_unlock_check.py` 使用已经安装的 **PyOCD CLI**；不需要第三方 Python 库（除 PyOCD 本身）。请先关闭其他占用调试器的应用。

```cmd
python nrf52_unlock_check.py
python nrf52_unlock_check.py verify --verbose
```

这两条**只读**：检查 CTRL-AP、AHB-AP、CPUID、FICR、UICR 和 Flash 起始字；**始终传递 `auto_unlock=false`**。

如确需破坏性解锁：

```cmd
python nrf52_unlock_check.py unlock --erase-all
```

脚本会先检查已锁定与否；若锁定，须在真实交互终端输入 **`ERASE`** 才会执行 PyOCD 的 `unlock`（Mass Erase）；若已解锁，会跳过擦除。**不建议用此脚本试图备份受保护的原厂固件。**

多个调试器时：

```cmd
python nrf52_unlock_check.py verify --uid 000000805059ed4720a4400013c48702a5a5a5a59796990e
```

如需验证是否持久：先执行 `verify`，完整断电重启后**再次**执行 `verify`。本工具无法替你物理切断 VCC，绝不把连续两次软件连接当成断电验证。

脚本退出码：`0` = 当前读数通过；`1` = 检测到锁定或用户取消；`2` = 通信异常或部分验证失败。建议使用 `--verbose` 查看原始输出。由于无法在此环境连接你的真实 CMSIS-DAP，脚本通过语法和离线模拟验证，具体 PyOCD 版本的命令输出格式仍需在你的机器上实际测试。

## 九、资料与版本说明

- [Nordic nRF52840 Debug and Trace / CTRL-AP](https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/dif.html)
- [PyOCD Security and protection](https://pyocd.io/docs/security.html)
- [PyOCD Command reference](https://pyocd.io/docs/command_reference.html)
- [PyOCD nRF52 target implementation](https://github.com/pyocd/pyOCD/blob/main/pyocd/target/family/target_nRF52.py)

记录整理日期：2026-11-21。本文寄存器数值来自上述本次实测；不是所有 nRF52840 模组的统一出厂状态。
