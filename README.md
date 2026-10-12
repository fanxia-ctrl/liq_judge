# liq_judge

强平 judge：对客服「强平」类问答逐轮判定，给出总判定、问题出在哪一层（强平服务 / 客服 Agent / 路由），附回答原文引用和理由。

- 调用方式、输入格式、结果说明、部署：见 [强平judge.md](强平judge.md)
- 服务器上启动：`cp config.env.example config.env`，填模型端点的 key，然后 `bash serve.sh check && bash serve.sh`
