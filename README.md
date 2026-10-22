# 🐟 飞鱼双栖

**让「大肥鱼」同时活在 QQ 群和 B 站评论区——一个 AI 人格，两个平台。**

QQ 里它会被 @ 就叫、跟群友接话；B 站里它会自己刷推荐流、给视频写评论、回你 @。
两边用的是同一个人格、同一份记忆。

---

## 三步开始

### 1. 装 Python

去 https://www.python.org/downloads/ 下载安装。

> ⚠️ 安装界面**一定要勾选** `Add python.exe to PATH`，否则后面跑不起来。

需要 3.10 或更新版本。

### 2. 双击 `start.bat`

第一次会自动装依赖（1~3 分钟），然后弹出浏览器打开控制台。

中途会问你**要不要下载 NapCat**（约 35MB，QQ 接入用）：
- 想用 QQ → 按 `Y`
- 只用 B 站 → 按 `n` 跳过

### 3. 填 DeepSeek Key

控制台 →「设置」→ 填 API Key → 保存 → 点「测试连接」，看到 `连接正常` 就好了。

Key 在 https://platform.deepseek.com 申请。

> 想用别的模型也行：改「接口地址」和「模型名」，任何 OpenAI 兼容服务都能接。

---

## 连 QQ

> 需要电脑上装了 QQ 客户端。没装的话跳过这节，B 站功能照常可用。

**① 先正常登录一次 QQ**

打开 QQ 扫码登录，然后**完全退出**（托盘图标也要退）。

这一步是给 NapCat 建立登录态，只做一次。

**② 启动 NapCat**

双击 `Start-NapCat.bat` → UAC 弹窗点「是」→ 等几秒，QQ 会自动进入主界面（不用再扫码）。

**③ 开一个 OneBot 通道**

浏览器打开 `http://127.0.0.1:6099`（Token 在黑色窗口里），
左侧「网络配置」→ 新建 **WebSocket 服务器**：

| 字段 | 填 |
|---|---|
| 端口 | `3001` |
| Token | 随便设一个，比如 `feiyu123` |

**④ 连上**

回到飞鱼控制台「设置 → QQ 接入」，填 `ws://127.0.0.1:3001` 和刚才的 Token，保存。

「总览」页点「启动 QQ」，卡片变绿就通了。随便找个群 @ 它试试。

---

## 连 B 站

控制台「B站登录」→ 点「生成二维码」→ 手机 B 站 App 扫。

**默认开着「演练模式」**：它会照常刷视频、照常写评论，但**只在控制台显示、不真的发出去**。

觉得语气对了，再去设置里关掉演练。

---

## 表情包（可选）

1. 把图片放进 `data/stickers/`；
2. **文件名就是表情名**（比如 `开心.png`）；
3. 设置里勾选「启用表情包」。

模型会自己挑合适的发，不是随机贴图。自带 5 个示例可以直接替换。

---

## ⚠️ 先说清楚风险

- **B 站自动评论违反用户协议**，账号可能被限制；
- 新号（Lv2 以下）发评论基本是「仅自己可见」，得先养号；
- 安全节奏：**每天 3~5 条、间隔 5~10 分钟**。超过这个量级很容易被静默限流；
- **QQ 侧**用 NapCat 是第三方注入方案，不是腾讯官方接口，封号风险自负。

程序能做的只是不撞线，**绕不过平台风控**。建议用小号、低频率。

---

## 常见问题

**Q：双击 start.bat 一闪就没了**
没装 Python，或者装的时候没勾 PATH。重装核对一下。

**Q：QQ 一直显示「等待 NapCat」**
NapCat 没开、端口不是 3001、或 Token 对不上。先运行 `start.bat doctor` 看端口通不通。

**Q：Start-NapCat.bat 闪退**
多半是 QQ 没退干净。脚本会在 `napcat-launch.log` 里记录每一步，可以打开看卡在哪。

**Q：B 站评论发了，但别人看不到**
被限流了。降频率、养号，别硬顶。

**Q：B 站能发但 QQ 连不上 / 反过来**
两个通道是独立的，可以只跑其中一个。设置里各自有开关。

---

## 数据存在哪

全部在本机 `data/` 目录，不上传任何服务器：

```
data/config.json        配置（含你的 API Key）
data/bili_cookie.json   B站登录凭证
data/memory.db          对话与行为记录
data/logs/              运行日志
data/stickers/          表情包
```

想清空就从控制台点「清空对话记忆」，或者直接删这几个文件。

---

## 许可与致谢

代码使用 [MIT License](LICENSE)。

人格设定与接入思路参考了 [dafeiyu-bot](https://github.com/SAKURAfan1023/dafeiyu-bot) 和 [BiliFish](https://github.com/Eule-Lawrence/BiliFish)，
B 站接口参考 [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)，
QQ 协议端使用 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)。
本项目不包含这些项目的源代码。

「大肥鱼」是 DeepSeek 的社区二创形象，本项目为非官方同人作品，与 DeepSeek 官方无关。
