# 🚀 Xianyu AutoAgent - 智能闲鱼客服机器人系统

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/) [![LLM Powered](https://img.shields.io/badge/LLM-powered-FF6F61)](https://platform.openai.com/)

专为闲鱼平台打造的AI值守解决方案，实现闲鱼平台7×24小时自动化值守，支持多专家协同决策、智能议价和上下文感知对话。 


## 🌟 核心特性

### 智能对话引擎
| 功能模块   | 技术实现            | 关键特性                                                     |
| ---------- | ------------------- | ------------------------------------------------------------ |
| 上下文感知 | 会话历史存储        | 轻量级对话记忆管理，完整对话历史作为LLM上下文输入            |
| 专家路由   | LLM prompt+规则路由 | 基于提示工程的意图识别 → 专家Agent动态分发，支持议价/技术/客服多场景切换 |

### 业务功能矩阵
| 模块     | 已实现                        | 规划中                       |
| -------- | ----------------------------- | ---------------------------- |
| 核心引擎 | ✅ LLM自动回复<br>✅ 上下文管理 | 🔄 情感分析增强               |
| 议价系统 | ✅ 阶梯降价策略                | 🔄 市场比价功能               |
| 技术支持 | ✅ 网络搜索整合                | 🔄 RAG知识库增强              |
| 运维监控 | ✅ 基础日志                    | 🔄 钉钉集成<br>🔄  Web管理界面 |

## 🎨效果图
<div align="center">
  <img src="./images/demo1.png" width="600" alt="客服">
  <br>
  <em>图1: 客服随叫随到</em>
</div>


<div align="center">
  <img src="./images/demo2.png" width="600" alt="议价专家">
  <br>
  <em>图2: 阶梯式议价</em>
</div>

<div align="center">
  <img src="./images/demo3.png" width="600" alt="技术专家"> 
  <br>
  <em>图3: 技术专家上场</em>
</div>

<div align="center">
  <img src="./images/log.png" width="600" alt="后台log"> 
  <br>
  <em>图4: 后台log</em>
</div>


## 🚴 快速开始
小白请直接查看[保姆级教学文档](https://my.feishu.cn/wiki/JtkBwkI9GiokZikVdyNceEfZncE)
### 环境要求
- Python 3.8+

### 安装步骤
```bash
1. 克隆仓库
git clone https://github.com/shaxiu/XianyuAutoAgent.git
cd XianyuAutoAgent

2. 安装依赖
pip install -r requirements.txt

3. 配置环境变量
创建一个 `.env` 文件，包含以下内容，也可直接重命名 `.env.example` ：
#必配配置
API_KEY=apikey通过模型平台获取
COOKIES_STR=填写网页端获取的cookie
MODEL_BASE_URL=模型地址
MODEL_NAME=模型名称
#可选配置
TOGGLE_KEYWORDS=接管模式切换关键词，默认为句号（输入句号切换为人工接管，再次输入则切换AI接管）
SIMULATE_HUMAN_TYPING=True/False #模拟人工回复延迟

注意：默认使用的模型是通义千问，如需使用其他API，请自行修改.env文件中的模型地址和模型名称；
COOKIES_STR自行在闲鱼网页端获取cookies(网页端F12打开控制台，选择Network，点击Fetch/XHR,点击一个请求，查看cookies)

4. 创建提示词文件prompts/*_prompt.txt（也可以直接将模板名称中的_example去掉），否则默认读取四个提示词模板中的内容
```

### 使用方法

运行主程序：
```bash
python main.py
```

### 图形界面（本地控制台，推荐）

- Windows：双击 `启动控制台.bat`（首次运行会自动安装依赖）
- 其他系统：运行 `python webui.py`

浏览器会自动打开 `http://127.0.0.1:8765`。第一次打开先注册管理员账号，之后每次需要登录。控制台只监听本机，外部无法访问；所有配置保存在 `data/console.db`，重启后不会丢失。运行期间不要关闭弹出的命令行窗口。

| 板块 | 功能 |
| ---- | ---- |
| 仪表盘 | 今日消息/回复/订单统计、近 7 天趋势、上线检查、最近动态 |
| 客服中心 | AI 自动回复开关、人工接管、营业时间与离线提示、AI 故障兜底话术；关键词回复（包含/完全一致/正则，可限定商品）；话术提示词编辑；黑名单 |
| 商品与交易 | 商品管理（同步在售商品、每日随机时间自动擦亮、单个擦亮）；自动上架（草稿/定时/队列发布，AI 一句话生成标题描述，Beta）；自动发货（固定内容或卡密库存，Beta） |
| 安全中心 | 防风控模式：标准/稳健/谨慎三档，控制回复延迟、单买家回复上限、每日上架上限与间隔、擦亮间隔、夜间静默；闲鱼返回风控信号时自动熔断暂停后台任务并通知 |
| 数据中心 | 对话记录（可一键拉黑买家）、实时运行日志 |
| 接入配置 | AI 模型（内置 16 个 OpenAI 兼容平台 + 自定义接口，多 Key 轮换、失败自动切换、一键测试）；闲鱼账号 Cookie；消息通知（钉钉/飞书/企业微信/Bark/Server 酱/Webhook） |
| 系统设置 | 自动启动、异常自动重启、修改密码、成员账号、功能规划 |

说明：
- 防风控发送延迟固定开启（每条自动回复至少等待 1.5 秒，按字数模拟打字，再按防风控模式放大），不同买家的消息并行处理、互不排队。
- 擦亮、上架由控制台后台执行，需要保持控制台运行；自动化只能降低风险，无法保证不被平台识别，请遵守闲鱼规则。
- 大部分配置保存后几秒内生效；修改提示词、Cookie 后需要点「重启」。
- 使用控制台后，人工接管关键词等设置以控制台为准，`.env` 里的 `TOGGLE_KEYWORDS`、`SIMULATE_HUMAN_TYPING` 不再使用；未在控制台配置模型时仍使用 `.env` 里的模型。

### 自定义提示词

可以通过编辑 `prompts` 目录下的文件来自定义各个专家的提示词：

- `classify_prompt.txt`: 意图分类提示词
- `price_prompt.txt`: 价格专家提示词
- `tech_prompt.txt`: 技术专家提示词
- `default_prompt.txt`: 默认回复提示词

## 🤝 参与贡献

欢迎通过 Issue 提交建议或 PR 贡献代码，请遵循 [贡献指南](https://contributing.md/)

## 🧸特别鸣谢
本项目参考了以下开源项目：
https://github.com/cv-cat/XianYuApis

感谢<a href="https://github.com/cv-cat">@CVcat</a>的技术支持

## 🛡 注意事项

⚠️ 注意：**本项目仅供学习与交流，如有侵权联系作者删除。**

鉴于项目的特殊性，开发团队可能在任何时间**停止更新**或**删除项目**。

如需学习交流，请联系：[coderxiu@qq.com](https://mailto:coderxiu@qq.com/)

## 📱 交流群
欢迎加入项目交流群，交流技术、分享经验、互助学习。
<div align="center">
  <table>
    <tr>
      <td align="center"><strong>交流群26（已满200）</strong></td>
      <td align="center"><strong>交流群27（推荐加入）</strong></td>
    </tr>
    <tr>
      <td><img src="./images/wx_group26.png" width="300px" alt="交流群26"></td>
      <td><img src="./images/wx_group27.png" width="300px" alt="交流群27"></td>
    </tr>
  </table>
</div>

## 💼 寻找机会

### <a href="https://github.com/shaxiu">@Shaxiu</a>
**🔍寻求方向**：**AI产品经理**  
**📫 联系：** **email**:coderxiu@qq.com；**wx:** coderxiu

### <a href="https://github.com/cv-cat">@CVcat</a>
**🔍寻求方向**：**研发工程师**（python、java、逆向、爬虫）  
**📫 联系：** **email:** 992822653@qq.com；**wx:** CVZC15751076989
## ☕ 请喝咖啡
您的☕和⭐将助力项目持续更新：

<div align="center">
  <img src="./images/wechat_pay.jpg" width="400px" alt="微信赞赏码"> 
  <img src="./images/alipay.jpg" width="400px" alt="支付宝收款码">
</div>


## 📈 Star 趋势
<a href="https://www.star-history.com/#shaxiu/XianyuAutoAgent&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=shaxiu/XianyuAutoAgent&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=shaxiu/XianyuAutoAgent&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=shaxiu/XianyuAutoAgent&type=Date" />
 </picture>
</a>


