# 第三方组件声明 · Third-Party Notices

「云信互联」（astrbot_plugin_remote_link）本体以 **MIT License** 发布（见 [LICENSE](LICENSE)）。
本文件列出项目分发或依赖的第三方组件及其许可，感谢这些开源项目。

---

## 一、随仓库分发的第三方代码

### LiteGraph.js

- **用途**：云端控制台「工作流」页的节点图只读渲染
- **文件**：`pages/console/assets/litegraph.js`、`pages/console/assets/litegraph.css`
- **上游**：https://github.com/jagenjo/litegraph.js
- **协议**：MIT License
- **版权**：Copyright (c) 2013 Javi Agenjo (@tamat)
- **说明**：原样分发，未做功能修改（文件头部已补回上游版权与许可声明）

```
The MIT License (MIT)
Copyright (c) 2013 Javi Agenjo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
```

---

## 二、云端插件运行时依赖

| 组件 | 协议 | 用途 |
| --- | --- | --- |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 | WebSocket 隧道服务端、HTTP 代理、异步请求 |

> 由 AstrBot 环境安装，不随插件分发。

---

## 三、本地代理（YunxinAgent）依赖

本地端发行版 `YunxinAgent-*-win64.zip` 由 PyInstaller 打包，内含以下组件：

| 组件 | 协议 | 用途 | 备注 |
| --- | --- | --- | --- |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 | 隧道客户端、ComfyUI/LLM 转发 | |
| [ttkbootstrap](https://github.com/israel-dryer/ttkbootstrap) | MIT | 桌面 GUI 主题 | |
| [pystray](https://github.com/moses-palmer/pystray) | **LGPL-3.0** | 系统托盘 | 见下方说明 |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) | 托盘图标与图像处理 | pystray 依赖 |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | 系统资源监控（CPU/内存） | |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0 with exception | 打包工具 | 其例外条款允许打包专有/任意协议程序 |
| CPython 运行时 | PSF License | 运行时 | |

### 关于 pystray（LGPL-3.0）的合规说明

pystray 以 LGPL-3.0 发布。本项目：

1. **未修改** pystray 源码，仅通过其公开 API 调用；
2. 本项目**全部源码以 MIT 公开**（本仓库），用户可自行替换 pystray 版本后按 `YunxinAgent.spec` 重新打包：
   ```bash
   pip install aiohttp ttkbootstrap pystray pillow psutil pyinstaller
   python -m PyInstaller --noconfirm YunxinAgent.spec
   ```
3. 因此使用者拥有 LGPL 第 4 条要求的「替换该库并重新链接」的实际能力。

如果你分发修改过的 YunxinAgent 二进制，请同样保留本声明并提供对应源码。

---

## 四、设计与思路致谢（未包含其代码）

| 项目 | 协议 | 致谢内容 |
| --- | --- | --- |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | AGPL-3.0 | 插件宿主平台。本项目为**独立插件**，通过其公开 API（Star / filter / LLM Tool / 插件页面）交互，不修改也不静态链接其代码 |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | GPL-3.0 | 本地推理引擎。本项目通过其 **HTTP/WebSocket API** 调用，**未引用任何 ComfyUI 源码**；UI→API 工作流转换逻辑为独立重写（等价于前端 `graphToPrompt` 的通用子集） |
| [astrbot_plugin_comfyui](https://github.com/cjxzdzh/astrbot_plugin_comfyui) | 见其仓库 | 入口节点约定（`ETN_LoadImageBase64` / `Simple String` / `VHS_LoadVideo`）的设计思路参考，**独立实现，未包含其代码** |
| [comfyui-tooling-nodes](https://github.com/Acly/comfyui-tooling-nodes) | MIT | `ETN_LoadImageBase64` 节点（用户自行安装，非本项目分发） |
| [ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite) | MIT | `VHS_LoadVideo` / VHS 视频输出（用户自行安装） |
| [cg-use-everywhere](https://github.com/chrisgoringe/cg-use-everywhere) | MIT | `Simple String` 文本入口（用户自行安装） |

---

## 五、如何反馈许可问题

如果你认为本项目的某处使用方式不符合上游协议，请在
[Issues](https://github.com/huy878007-pixel/astrbot_plugin_remote_link/issues)
提出，本喵会尽快修正喵。
