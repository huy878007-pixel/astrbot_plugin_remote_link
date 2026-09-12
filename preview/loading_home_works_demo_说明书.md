# 载入动画 · 首界面 · 作品展示 Demo 说明书

> 文件：`preview/loading_home_works_demo.html`  
> 类型：独立 HTML 原型 / 视觉 Demo  
> 主题：浅色 + 深色双版本

---

## 1. 这个 Demo 是做什么的

这是一个纯前端静态原型，用于集中展示《万象世界》项目最关心的三个前端板块：

1. 载入动画
2. 首界面
3. 作品展示界面

所有内容都放在一个 HTML 文件里，不需要安装依赖，不需要后端。

---

## 2. 文件清单

| 文件 | 说明 |
| --- | --- |
| `preview/loading_home_works_demo.html` | 主 Demo 页面，包含全部样式、结构和交互 |
| `preview/loading_home_works_demo_说明书.md` | 本说明书 |

---

## 3. 页面板块说明

### 3.1 载入动画

位置：页面第 01 区。

包含：

- 品牌 Logo：渐变方块 + 呼吸浮动
- 标题：正在进入万象世界
- 副标题：正在加载地图、角色与世界观数据…
- 进度条：CSS 无限循环加载动画
- 状态文字：加载中 36%
- 操作：↻ 重播按钮

用途：

> 展示游戏/应用启动时的载入体验，以及品牌进入氛围。

---

### 3.2 首界面

位置：页面第 02 区。

包含：

- 顶部导航：首页 / 世界 / 创作者 / 关于 / 开始体验
- 品牌区：云 + 万象世界
- Hero 标题：创造属于你的万象世界
- 副标题：人负责想象，创作标准表达世界；Core 负责执行规则，让每个世界真正可玩。
- 主 CTA：立即创建 / 浏览作品
- 三个能力入口卡片：
  - 世界地图
  - 规则引擎
  - 内容包

用途：

> 展示用户进入产品后看到的第一个正式界面。

---

### 3.3 作品展示界面

位置：页面第 03 区。

包含：

- 筛选标签：全部 / 幻想 / 科幻 / 现代
- 作品卡片网格
- 每个卡片包含：
  - 封面渐变图
  - 作品名称
  - 类型 / 省份 / 势力信息
- 悬停效果：卡片上浮 + 阴影增强
- 筛选交互：点击标签后只显示对应分类

用途：

> 展示已创建世界的作品画廊，方便用户浏览和进入。

---

## 4. 浅色 / 深色版本

页面右上角提供：

```text
🌗 浅色 / 深色
```

- 默认进入浅色模式。
- 点击按钮切换深色模式。
- 主题选择会保存在浏览器 `localStorage` 中。
- 刷新页面后保持上一次选择。

配色变量集中在 `<style>` 顶部的 `:root` 与 `html[data-theme="dark"]` 中。

浅色核心变量：

```css
--bg: #f4f6fb;
--surface: #ffffff;
--text: #1a1f2e;
--primary: #6366f1;
```

深色核心变量：

```css
--bg: #0b0e17;
--surface: #131827;
--text: #eef2ff;
--primary: #818cf8;
```

---

## 5. 如何运行

### 方式一：直接用浏览器打开

双击或拖拽到浏览器：

```text
preview/loading_home_works_demo.html
```

### 方式二：本地静态服务器

在仓库根目录执行：

```bash
python -m http.server 8080
```

然后访问：

```text
http://127.0.0.1:8080/preview/loading_home_works_demo.html
```

### 方式三：使用已有 preview server

```bash
python preview/preview_server.py
```

---

## 6. 如何修改

### 6.1 修改文案

直接搜索 HTML 中的中文文字，例如：

- “正在进入万象世界”
- “创造属于你的”
- “暮色王国”

替换成你需要的文案即可。

### 6.2 修改颜色

在 `<style>` 中修改：

- `:root`：浅色配色
- `html[data-theme="dark"]`：深色配色

常用变量：

```css
--bg
--surface
--surface-2
--text
--text-2
--border
--primary
--primary-2
--accent
--shadow
--ring
```

### 6.3 增加作品卡片

复制一个：

```html
<div class="work-card" data-cat="fantasy">
  <div class="work-thumb t1">🏰</div>
  <div class="work-info">
    <h4>作品名</h4>
    <p>分类 · 省份 · 势力</p>
  </div>
</div>
```

注意：

- `data-cat` 必须是：`fantasy` / `sci-fi` / `modern`
- 新分类需要在筛选按钮中同步增加
- `t1` ~ `t6` 是现有封面渐变样式，可复用或新增

### 6.4 修改载入动画速度

搜索：

```css
@keyframes loading
```

修改 `animation: loading 1.4s ease-in-out infinite;` 中的时间即可。

### 6.5 修改重播进度速度

搜索 JS 中：

```js
const timer = setInterval(() => {
  p = Math.min(100, p + 13);
```

修改 `p + 13` 可以改变加载速度。

---

## 7. 交互说明

| 操作 | 效果 |
| --- | --- |
| 点击右上角主题按钮 | 浅色 / 深色切换 |
| 点击载入动画“↻ 重播” | 重新播放加载进度 |
| 点击作品筛选标签 | 筛选作品卡片 |
| 鼠标悬停作品卡片 | 卡片上浮并显示阴影 |

---

## 8. 当前为 Demo，不是正式产品页

本文件仅用于：

- 视觉确认
- 结构讨论
- 浅色/深色方案对比
- 后续正式前端开发的参考

正式版本还需要根据产品需求接入真实数据、路由、组件库和设计系统。

---

## 9. 相关文件

- `preview/loading_home_works_demo.html`
- `preview/ui-skills/`：仓库内已有的 UI 风格参考
- `pages/console/`：现有 AstrBot 插件控制台页面

---

## 10. 版本记录

| 版本 | 说明 |
| --- | --- |
| v1.0 | 新增载入动画、首界面、作品展示三个板块，支持浅色/深色切换 |
